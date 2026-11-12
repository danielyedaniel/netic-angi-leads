"""Angi payload -> NormalizedLead: format detection, strict mapping, drift, recovery.

Covers app/angi/parser.py and app/angi/formats.py. Most tests call the parser directly
(it is a pure function); the few at the end that need the database go through the webhook.
"""

from __future__ import annotations

import copy
import json
import random
import re
from typing import Any

import pytest

from app.angi.formats import ALIASES, ANGI_ADS, ANGI_LEADS, FORMATS, norm_key
from app.angi.parser import (
    Drift,
    ParseResult,
    detect_format,
    flatten,
    parse_angi_payload,
    split_category,
    split_crammed_address,
)
from app.models import NormalizedLead
from tests.conftest import (
    ANGI_ADS_EXAMPLE,
    ANGI_LEADS_EXAMPLE,
    ads_payload,
    db_one,
    leads_payload,
    post_lead,
)

MODES = {"strict", "tolerant", "failed"}
ADS_REQUIRED = [field.key for field in ANGI_ADS.fields if field.required]


def ads(**overrides: Any) -> dict[str, Any]:
    """The Angi Ads spec example (same ids every time) with top-level overrides."""
    payload = copy.deepcopy(ANGI_ADS_EXAMPLE)
    payload.update(overrides)
    return payload


def ads_address(**overrides: Any) -> dict[str, Any]:
    """The Angi Ads spec example with fields of its PostalAddress replaced."""
    payload = copy.deepcopy(ANGI_ADS_EXAMPLE)
    payload["PostalAddress"].update(overrides)
    return payload


def leads(**overrides: Any) -> dict[str, Any]:
    """The Angi Leads example (same ids every time) with top-level overrides."""
    payload = copy.deepcopy(ANGI_LEADS_EXAMPLE)
    payload.update(overrides)
    return payload


def without(payload: dict[str, Any], *keys: str) -> dict[str, Any]:
    trimmed = copy.deepcopy(payload)
    for key in keys:
        del trimmed[key]
    return trimmed


def assert_well_formed(result: Any) -> None:
    """The contract every caller of parse_angi_payload relies on, whatever the input."""
    assert isinstance(result, ParseResult)
    assert result.mode in MODES
    assert (result.lead is None) == (result.mode == "failed")
    assert result.detected_format in {"angi_ads", "angi_leads", "unknown"}
    assert result.signature.startswith(f"{result.detected_format}:")
    if result.mode == "failed":
        assert result.detected_format == "unknown"  # a known format is never given up on
    for entries in result.drift.as_dict().values():
        assert all(isinstance(entry, str) for entry in entries)
    assert all(isinstance(issue, str) for issue in result.issues)
    if result.lead is not None:
        assert isinstance(result.lead, NormalizedLead)
        assert result.lead.source_format == result.detected_format
        if result.detected_format == "unknown":
            assert result.lead.is_contactable  # an unknown format is only kept if we can reach the consumer


# --- the format tables ---------------------------------------------------------------------


def test_norm_key_ignores_case_and_separators():
    # A key that only changed casing or separators is still the same key.
    assert norm_key("spEntityId") == norm_key("SP_Entity_Id") == norm_key("sp-entity-id") == "spentityid"
    assert norm_key("ALAccountId") == norm_key("al account id") == "alaccountid"
    assert norm_key("!!!") == ""


@pytest.mark.parametrize("spec", FORMATS, ids=lambda spec: spec.name)
def test_format_table_is_internally_consistent(spec):
    # The table is the single source of truth, so it must not contradict itself.
    keys = [field.key for field in spec.fields]
    assert len(keys) == len(set(keys))  # no key documented twice
    lead_fields = set(NormalizedLead.model_fields) | {"fee"}  # "fee" becomes fee_cents
    assert {field.target for field in spec.fields if field.target} <= lead_fields
    top_level = {norm_key(key.split(".")[0]) for key in keys}
    assert spec.signature_keys <= top_level  # a format is identified by keys it documents
    assert len(spec.signature_keys) >= 2  # detection needs two hits
    assert spec.distinctive_keys  # ... one of which has to be a key only this feed uses
    assert spec.distinctive_keys <= spec.signature_keys


def test_formats_do_not_share_identifying_keys():
    # If two formats shared a signature key, detection could pick the wrong mapping.
    assert not (ANGI_ADS.signature_keys & ANGI_LEADS.signature_keys)


def test_aliases_are_normalised_and_unambiguous():
    # Aliases are compared after norm_key, so an un-normalised alias could never match;
    # an alias claimed by two fields would make recovery depend on dict order.
    seen: dict[str, str] = {}
    for target, aliases in ALIASES.items():
        assert target in set(NormalizedLead.model_fields) | {"fee"}
        for alias in aliases:
            assert norm_key(alias) == alias
            assert alias not in seen, f"{alias!r} is an alias of both {seen.get(alias)} and {target}"
            seen[alias] = target


@pytest.mark.parametrize("spec", FORMATS, ids=lambda spec: spec.name)
def test_every_documented_key_is_recoverable_after_a_casing_change(spec):
    # Tolerant recovery finds a re-cased key by alias; a mapped key with no alias would be lost.
    for field in spec.fields:
        if field.target:
            leaf = norm_key(field.key.rsplit(".", 1)[-1])
            assert leaf in ALIASES[field.target], f"{field.key} cannot be recovered by alias"


@pytest.mark.parametrize(
    ("spec", "example"),
    [(ANGI_ADS, ANGI_ADS_EXAMPLE), (ANGI_LEADS, ANGI_LEADS_EXAMPLE)],
    ids=["angi_ads", "angi_leads"],
)
def test_documentation_example_carries_every_always_present_key(spec, example):
    # Angi's own samples must be clean against the table built from Angi's own docs.
    present = set(flatten(example))
    assert [field.key for field in spec.fields if field.required and field.key not in present] == []


# --- detect_format -------------------------------------------------------------------------


def test_detect_format_recognises_the_angi_ads_example():
    # PascalCase + PostalAddress + CorrelationId/ALAccountId is the Angi Ads feed.
    assert detect_format(ANGI_ADS_EXAMPLE) is ANGI_ADS


def test_detect_format_recognises_the_angi_leads_example():
    # camelCase + leadOid/srOid/spEntityId is the Angi Leads feed.
    assert detect_format(ANGI_LEADS_EXAMPLE) is ANGI_LEADS


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"hello": "world"},
        {"id": 1, "customer": {"name": "Pat"}, "notes": "call me"},
        {"email": "pat@example.com", "phone": "5554332646", "name": "Pat Lee"},
    ],
)
def test_detect_format_returns_none_for_unknown_and_empty_objects(payload):
    # Generic keys (email, phone, name) are not evidence of either Angi feed.
    assert detect_format(payload) is None


@pytest.mark.parametrize(
    "key",
    ["ALAccountId", "CorrelationId", "PostalAddress", "PhoneNumber", "Urgency", "Category"]
    + ["spEntityId", "spCompanyName", "leadOid", "srOid", "primaryPhone", "stateProvince", "taskName", "matchType"],
)
def test_detect_format_needs_more_than_one_identifying_key(key):
    # One coincidental key is not enough to apply a format's mapping.
    assert detect_format({key: "x"}) is None
    assert detect_format({key: "x", "email": "pat@example.com", "somethingElse": 1}) is None


def test_detect_format_accepts_two_identifying_keys():
    # Two identifying keys are the threshold.
    assert detect_format({"CorrelationId": "x", "ALAccountId": "1"}) is ANGI_ADS
    assert detect_format({"leadOid": 1, "srOid": 2}) is ANGI_LEADS


@pytest.mark.parametrize(
    "payload",
    [
        {"urgency": "low", "category": "heartbeat", "sequence": 7},
        {"Urgency": "This Week", "Category": "Cleaning", "PhoneNumber": "5554332646"},
        {"taskName": "x", "matchType": "Lead"},
        {"primaryPhone": "3035550123", "stateProvince": "CO", "taskName": "x", "leadDescription": "y"},
    ],
)
def test_detect_format_needs_a_key_only_that_feed_uses(payload):
    # "urgency" and "category" could be any system's JSON. Calling that Angi Ads would
    # mean it can never be quarantined as unrecognised, and becomes an empty lead instead.
    assert detect_format(payload) is None


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"PostalAddress": {}, "Urgency": "now"}, ANGI_ADS),  # the spec's field table has no ids
        ({"ALAccountId": "1", "Category": "x"}, ANGI_ADS),
        ({"spCompanyName": "Summit", "taskName": "x"}, ANGI_LEADS),  # spEntityId is only "sometimes" sent
        ({"srOid": 1, "primaryPhone": "3035550123"}, ANGI_LEADS),
    ],
)
def test_detect_format_accepts_one_distinctive_key_plus_a_generic_one(payload, expected):
    # One key that only this feed uses, backed by a second signature key, is enough.
    assert detect_format(payload) is expected


def test_heartbeat_shaped_payload_is_not_an_angi_ads_lead():
    # The whole point of the stricter detection: no format, no contact details, no lead.
    result = parse_angi_payload({"category": "heartbeat", "urgency": "low", "sequence": 7})

    assert result.detected_format == "unknown"
    assert result.mode == "failed"
    assert result.lead is None


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"correlationId": "x", "alAccountId": "1"}, ANGI_ADS),
        ({"correlation_id": "x", "AL-ACCOUNT-ID": "1"}, ANGI_ADS),
        ({"CORRELATIONID": "x", "phone number": "5554332646"}, ANGI_ADS),
        ({"postal_address": {}, "urgency": "now"}, ANGI_ADS),
        ({"LeadOid": 1, "SrOid": 2}, ANGI_LEADS),
        ({"LEAD_OID": 1, "sp-entity-id": 2}, ANGI_LEADS),
        ({"task_name": "x", "sp_company_name": "Summit"}, ANGI_LEADS),
    ],
)
def test_detect_format_survives_case_and_separator_drift(payload, expected):
    # A casing change alone must not make the feed unrecognisable.
    assert detect_format(payload) is expected


def test_detect_format_picks_the_format_sharing_the_most_keys():
    # A payload mixing keys of both feeds is treated as the one it resembles most.
    mostly_leads = {"leadOid": 1, "srOid": 2, "taskName": "x", "CorrelationId": "c", "ALAccountId": "a"}
    mostly_ads = {"leadOid": 1, "srOid": 2, "Urgency": "x", "CorrelationId": "c", "ALAccountId": "a"}
    assert detect_format(mostly_leads) is ANGI_LEADS
    assert detect_format(mostly_ads) is ANGI_ADS


# --- Angi Ads: strict mapping ----------------------------------------------------------------


def test_angi_ads_example_maps_onto_every_lead_field():
    # The spec example is the contract: each field lands in exactly one canonical place.
    result = parse_angi_payload(ANGI_ADS_EXAMPLE)

    expected = {
        "source": "angi",
        "source_format": "angi_ads",
        "external_lead_id": "61a7de56-dba3-4e59-8e2a-3fa827f84f7f",
        "external_request_id": None,
        "external_account_id": "123456",
        "external_account_name": None,
        "crm_key": None,
        "first_name": "Bob",
        "last_name": "Builder",
        "full_name": "Bob Builder",
        "email": "bob.builder@gmail.com",
        "email_normalized": "bob.builder@gmail.com",
        "phone": "5554332646",
        "phone_normalized": "+15554332646",
        "phone_ext": None,
        "phone_is_masked": None,
        "secondary_phone": None,
        "secondary_phone_normalized": None,
        "address_line1": "123 Main St.",
        "address_line2": None,
        "city": "Indianapolis",
        "state": "IN",
        "postal_code": "46203",
        "task_name": "House Cleaning",
        "market": "Indianapolis",
        "comments": "I'm Looking for recurring house cleaning services please.",
        "urgency": "This Week",
        "match_type": None,
        "lead_description": None,
        "lead_source_detail": "Angie's List Quote Request",
        "contact_status": None,
        "fee_cents": None,  # the Angi Ads feed carries no price
        "interview": [],
        "trusted_form_url": None,
        "automated_contact_ok": None,
        "extras": {"Category": "Indianapolis – House Cleaning"},
    }
    assert set(expected) == set(NormalizedLead.model_fields)  # a new lead field must be added here
    assert result.lead.model_dump() == expected
    assert result.lead.is_contactable
    assert result.lead.display_name == "Bob Builder"


def test_angi_ads_example_is_strict_and_clean():
    # A payload exactly as documented raises no drift and no data-quality issue.
    result = parse_angi_payload(ANGI_ADS_EXAMPLE)

    assert result.detected_format == "angi_ads"
    assert result.mode == "strict"
    assert result.is_clean
    assert result.drift.is_clean
    assert result.drift.severity is None
    assert result.drift.as_dict() == {"unknown": [], "missing": [], "mistyped": [], "recovered": []}
    assert result.issues == []
    assert result.signature == "angi_ads:clean"


def test_parsing_does_not_modify_the_payload():
    # The raw request is kept for replay; parsing must not change the object it was given.
    for example in (ANGI_ADS_EXAMPLE, ANGI_LEADS_EXAMPLE, {"lead": ANGI_ADS_EXAMPLE}):
        payload = copy.deepcopy(example)
        parse_angi_payload(payload)
        assert payload == example


# --- Category -> market + service ------------------------------------------------------------


@pytest.mark.parametrize(
    ("category", "city", "expected"),
    [
        ("Indianapolis – House Cleaning", "Indianapolis", ("Indianapolis", "House Cleaning")),  # en dash
        ("Indianapolis – House Cleaning", None, ("Indianapolis", "House Cleaning")),
        ("Indianapolis – House Cleaning", "Carmel", ("Indianapolis", "House Cleaning")),
        ("Indianapolis — House Cleaning", "Indianapolis", ("Indianapolis", "House Cleaning")),  # em dash
        ("Indianapolis — House Cleaning", None, ("Indianapolis", "House Cleaning")),
        ("  Indianapolis  –  House Cleaning  ", None, ("Indianapolis", "House Cleaning")),
        ("Winston-Salem – Roof Repair", None, ("Winston-Salem", "Roof Repair")),
        ("Denver – Central A/C - Repair", None, ("Denver", "Central A/C - Repair")),
    ],
)
def test_split_category_on_a_typographic_dash(category, city, expected):
    # The spec writes Category as "<market> – <service>"; a long dash is always the separator.
    assert split_category(category, city) == expected


@pytest.mark.parametrize(
    ("category", "city", "expected"),
    [
        ("Indianapolis - House Cleaning", "Indianapolis", ("Indianapolis", "House Cleaning")),
        ("Indianapolis - House Cleaning", "indianapolis", ("Indianapolis", "House Cleaning")),
        ("INDIANAPOLIS - House Cleaning", "Indianapolis", ("INDIANAPOLIS", "House Cleaning")),
        ("Indianapolis - House Cleaning", "  Indianapolis ", ("Indianapolis", "House Cleaning")),
        ("Indianapolis - Central A/C - Repair", "Indianapolis", ("Indianapolis", "Central A/C - Repair")),
        ("Winston-Salem - Roof Repair", "Winston-Salem", ("Winston-Salem", "Roof Repair")),
    ],
)
def test_split_category_on_a_plain_hyphen_when_the_left_side_is_the_city(category, city, expected):
    # A keyboard hyphen is only trusted as the separator if what precedes it is the lead's own city.
    assert split_category(category, city) == expected


@pytest.mark.parametrize(
    ("category", "city"),
    [
        ("Heating - Repair", "Denver"),
        ("Heating - Repair", None),
        ("Heating - Repair", ""),
        ("Central A/C - Repair", "Indianapolis"),
        ("Indianapolis - House Cleaning", "Carmel"),
        ("Indianapolis - House Cleaning", None),
    ],
)
def test_split_category_leaves_a_hyphenated_service_name_alone(category, city):
    # "Heating - Repair" is one service, not a market called "Heating".
    assert split_category(category, city) == (None, category)


@pytest.mark.parametrize("category", ["House Cleaning", "Roof-Repair", "Indianapolis–House Cleaning", "–", "A –", "– B"])
def test_split_category_without_a_spaced_separator_is_all_service(category):
    # No "<space>dash<space>" means there is no market part.
    assert split_category(category, "Indianapolis") == (None, category)


@pytest.mark.parametrize("category", [None, ""])
def test_split_category_of_nothing_is_nothing(category):
    # No category, no market and no service.
    assert split_category(category, "Indianapolis") == (None, None)


def test_parser_splits_the_ads_category_and_keeps_the_original_wording():
    # The email says "House Cleaning", the market is stored, and Angi's text is not lost.
    for dash in ("–", "—"):
        category = f"Indianapolis {dash} House Cleaning"
        result = parse_angi_payload(ads(Category=category))
        assert result.lead.task_name == "House Cleaning"
        assert result.lead.market == "Indianapolis"
        assert result.lead.extras["Category"] == category
        assert result.is_clean  # splitting is normal mapping, not drift


def test_parser_splits_a_hyphenated_category_only_when_it_starts_with_the_city():
    # Same rule as split_category, applied with the city from the payload's own address.
    matching = parse_angi_payload(ads(Category="indianapolis - House Cleaning"))
    assert (matching.lead.market, matching.lead.task_name) == ("indianapolis", "House Cleaning")
    assert matching.lead.extras["Category"] == "indianapolis - House Cleaning"

    other = parse_angi_payload(ads(Category="Heating - Repair"))
    assert (other.lead.market, other.lead.task_name) == (None, "Heating - Repair")
    assert "Category" not in other.lead.extras  # nothing was split, so nothing to preserve


def test_parser_keeps_a_category_without_separator_as_the_service():
    # A plain category is the service name as-is.
    result = parse_angi_payload(ads(Category="House Cleaning"))
    assert (result.lead.market, result.lead.task_name) == (None, "House Cleaning")
    assert result.lead.extras == {}


@pytest.mark.parametrize("category", ["", "   ", None])
def test_parser_accepts_an_empty_category(category):
    # An empty Category is a data gap in one lead, not a format change.
    result = parse_angi_payload(ads(Category=category))
    assert (result.lead.market, result.lead.task_name) == (None, None)
    assert result.mode == "strict"
    assert result.is_clean


def test_parser_does_not_split_the_task_name_of_the_angi_leads_feed():
    # Only the Angi Ads spec packs a market into the category.
    result = parse_angi_payload(leads(taskName="Denver – Roof Repair"))
    assert (result.lead.market, result.lead.task_name) == (None, "Denver – Roof Repair")


# --- addresses, numbers sent for strings -------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("123 Main St. Indianapolis, IN 46203", ("IN", "46203")),
        ("123 Main St. Indianapolis IN 46203", ("IN", "46203")),
        ("123 Main St. Indianapolis,IN 46203", ("IN", "46203")),
        ("123 Main St. Indianapolis, IN 46203  ", ("IN", "46203")),
        ("123 Main St. Indianapolis, IN 46203-1234", ("IN", "46203")),
        ("123 Main St.", (None, None)),
        ("123 Main St. Indianapolis, Indiana 46203", (None, None)),
        ("123 Main St. Indianapolis, IN", (None, None)),
        ("123 Main St. Apt 46203", (None, None)),
        ("IN 46203", (None, None)),
        ("", (None, None)),
        (None, (None, None)),
    ],
)
def test_split_crammed_address(line, expected):
    # State and ZIP are only read from the end of the line when both are unmistakably there.
    assert split_crammed_address(line) == expected


def test_crammed_address_yields_state_and_postal_code():
    # The spec's own example puts the whole address in AddressFirstLine and leaves the rest empty.
    result = parse_angi_payload(
        ads_address(AddressFirstLine="123 Main St. Indianapolis, IN 46203", City="", State="", PostalCode="")
    )
    assert result.lead.state == "IN"
    assert result.lead.postal_code == "46203"
    assert result.lead.address_line1 == "123 Main St. Indianapolis, IN 46203"  # the original line is kept
    assert result.mode == "strict"
    assert result.is_clean  # empty strings are still the documented shape


def test_explicit_state_and_postal_code_win_over_the_address_line():
    # Structured fields are trusted over anything guessed from free text.
    result = parse_angi_payload(ads_address(AddressFirstLine="123 Main St. Springfield, IL 62701"))
    assert (result.lead.state, result.lead.postal_code) == ("IN", "46203")


def test_ids_phone_and_zip_sent_as_json_numbers_are_accepted():
    # Angi documents these as strings in one feed and numbers in the other; neither is drift.
    payload = ads(ALAccountId=123456, PhoneNumber=5554332646)
    payload["PostalAddress"] = {
        "AddressFirstLine": "1 Beacon St",
        "AddressSecondLine": "",
        "City": "Boston",
        "State": "MA",
        "PostalCode": 2134,
    }
    result = parse_angi_payload(payload)

    assert result.lead.external_account_id == "123456"
    assert result.lead.phone == "5554332646"
    assert result.lead.phone_normalized == "+15554332646"
    assert result.lead.postal_code == "02134"  # a numeric ZIP loses its leading zero in JSON
    assert result.mode == "strict"
    assert result.is_clean
    assert result.signature == "angi_ads:clean"


def test_numbers_sent_as_floats_do_not_grow_a_decimal_point():
    # Some JSON encoders write every number as a float; "123456.0" would not match the tenant mapping.
    payload = ads(ALAccountId=123456.0, PhoneNumber=5554332646.0, CorrelationId=42.0)
    payload["PostalAddress"]["PostalCode"] = 2134.0
    result = parse_angi_payload(payload)

    assert result.lead.external_account_id == "123456"
    assert result.lead.external_lead_id == "42"
    assert result.lead.phone_normalized == "+15554332646"
    assert result.lead.postal_code == "02134"


@pytest.mark.parametrize(
    ("postal_code", "expected"),
    [("02134", "02134"), ("2134", "2134"), ("46203-1234", "46203-1234"), ("K1A 0B1", "K1A 0B1"), (46203, "46203"), (501, "00501")],
)
def test_postal_code_is_only_zero_padded_when_it_arrived_as_a_number(postal_code, expected):
    # A string is stored as sent; only a number can have lost leading zeros.
    result = parse_angi_payload(ads_address(PostalCode=postal_code))
    assert result.lead.postal_code == expected


@pytest.mark.parametrize("second_line", ["", "   ", None])
def test_empty_address_second_line_is_not_drift(second_line):
    # Most homes have no unit number.
    result = parse_angi_payload(ads_address(AddressSecondLine=second_line))
    assert result.lead.address_line2 is None
    assert result.is_clean
    assert result.mode == "strict"


def test_absent_address_second_line_is_not_drift():
    # The spec example includes the key, but it is the one optional field of the feed.
    payload = ads()
    del payload["PostalAddress"]["AddressSecondLine"]
    result = parse_angi_payload(payload)
    assert result.lead.address_line2 is None
    assert result.is_clean
    assert result.mode == "strict"
    assert result.signature == "angi_ads:clean"


def test_address_second_line_is_kept_when_present():
    # A unit number matters to the technician.
    result = parse_angi_payload(ads_address(AddressSecondLine="Apt 4B"))
    assert result.lead.address_line2 == "Apt 4B"
    assert result.is_clean


def test_text_fields_are_trimmed_and_capped():
    # Padding is noise, and absurdly long values must not reach the database or an email.
    result = parse_angi_payload(
        ads(FirstName="  Bob  ", LastName="\tBuilder\n", Description="x" * 6000, Urgency="  This Week ")
    )
    assert (result.lead.first_name, result.lead.last_name, result.lead.full_name) == ("Bob", "Builder", "Bob Builder")
    assert result.lead.urgency == "This Week"
    assert len(result.lead.comments) == 5000
    assert len(parse_angi_payload(ads(FirstName="n" * 500)).lead.first_name) == 100


def test_unicode_names_survive_untouched():
    # Consumers are not all called Bob.
    result = parse_angi_payload(ads(FirstName="José", LastName="Ünal-李 😀"))
    assert result.lead.first_name == "José"
    assert result.lead.last_name == "Ünal-李 😀"
    assert result.lead.full_name == "José Ünal-李 😀"
    assert result.is_clean


def test_email_keeps_its_original_form_next_to_the_normalised_one():
    # Nothing the lead source sent is lost; matching uses the normalised value.
    result = parse_angi_payload(ads(Email="  Bob.Builder@Gmail.COM "))
    assert result.lead.email == "Bob.Builder@Gmail.COM"
    assert result.lead.email_normalized == "bob.builder@gmail.com"
    assert result.issues == []


# --- Angi Leads: strict mapping ----------------------------------------------------------------


def test_angi_leads_example_maps_onto_every_lead_field():
    # The help-centre example is the contract for the second feed.
    result = parse_angi_payload(ANGI_LEADS_EXAMPLE)

    expected = {
        "source": "angi",
        "source_format": "angi_leads",
        "external_lead_id": "987654321",
        "external_request_id": "123456789",
        "external_account_id": "12345678",
        "external_account_name": "Summit Heating & Air",
        "crm_key": None,
        "first_name": "Jane",
        "last_name": "Doe",
        "full_name": "Jane Doe",
        "email": "janedoe@example.com",
        "email_normalized": "janedoe@example.com",
        "phone": "3035550123",
        "phone_normalized": "+13035550123",
        "phone_ext": None,
        "phone_is_masked": False,
        "secondary_phone": None,
        "secondary_phone_normalized": None,
        "address_line1": "123 Main Street",
        "address_line2": None,
        "city": "Denver",
        "state": "CO",
        "postal_code": "80210",
        "task_name": "Central A/C - Repair",
        "market": None,
        "comments": "AC is blowing warm air.",
        "urgency": None,
        "match_type": "Lead",
        "lead_description": "Standard",
        "lead_source_detail": "HomeAdvisor",
        "contact_status": None,
        "fee_cents": 2567,
        "interview": [
            {"question": "What kind of location is this?", "answer": "Home/Residence"},
            {"question": "When do you need this work done?", "answer": "Within 1 week"},
        ],
        "trusted_form_url": "https://cert.trustedform.com/abc123",
        "automated_contact_ok": True,
        "extras": {},
    }
    assert set(expected) == set(NormalizedLead.model_fields)  # a new lead field must be added here
    assert result.lead.model_dump() == expected


def test_angi_leads_example_is_strict_and_clean():
    # Including its null secondaryPhone and empty-string extensions.
    result = parse_angi_payload(ANGI_LEADS_EXAMPLE)

    assert result.detected_format == "angi_leads"
    assert result.mode == "strict"
    assert result.is_clean
    assert result.issues == []
    assert result.signature == "angi_leads:clean"


@pytest.mark.parametrize(
    ("lead_oid", "sr_oid", "sp_entity_id"),
    [
        (987654321, 123456789, 12345678),
        (987654321.0, 123456789.0, 12345678.0),
        ("987654321", "123456789", "12345678"),
        (" 987654321 ", "123456789 ", " 12345678"),
    ],
)
def test_numeric_ids_become_plain_strings(lead_oid, sr_oid, sp_entity_id):
    # The tenant mapping and idempotency key compare ids as text: 12345678, 12345678.0 and "12345678" are one account.
    result = parse_angi_payload(leads(leadOid=lead_oid, srOid=sr_oid, spEntityId=sp_entity_id))
    assert result.lead.external_lead_id == "987654321"
    assert result.lead.external_request_id == "123456789"
    assert result.lead.external_account_id == "12345678"
    assert result.is_clean


def test_huge_integer_ids_keep_every_digit():
    # Ids are identifiers, not quantities: no rounding, no exponent notation.
    result = parse_angi_payload(leads(leadOid=12345678901234567890123))
    assert result.lead.external_lead_id == "12345678901234567890123"


@pytest.mark.parametrize(
    ("fee", "expected"),
    [(25.67, 2567), ("25.67", 2567), ("$25.67", 2567), (28, 2800), (0, 0), (0.1 + 0.2, 30), (19.99, 1999)],
)
def test_fee_is_stored_in_cents(fee, expected):
    # The fee is what a credit claim asks back, so it is exact integer cents.
    result = parse_angi_payload(leads(fee=fee))
    assert result.lead.fee_cents == expected
    assert result.issues == []
    assert result.is_clean


@pytest.mark.parametrize("fee", [None, "", "  "])
def test_blank_fee_is_unknown_without_being_an_issue(fee):
    # "No fee given" is not the same as "a bad fee".
    result = parse_angi_payload(leads(fee=fee))
    assert result.lead.fee_cents is None
    assert result.issues == []


def test_interview_is_parsed_into_questions_and_answers():
    # The consumer's answers are shown to the business.
    result = parse_angi_payload(ANGI_LEADS_EXAMPLE)
    assert [(item.question, item.answer) for item in result.lead.interview] == [
        ("What kind of location is this?", "Home/Residence"),
        ("When do you need this work done?", "Within 1 week"),
    ]


def test_malformed_interview_items_are_skipped_and_reported():
    # One broken entry must not cost the lead or the usable answers - and items that are
    # not {question, answer} objects mean the shape changed, which has to be visible.
    interview = [
        {"question": "Good one?", "answer": "Yes"},
        "just a string",
        None,
        42,
        ["question", "answer"],
        {"question": "No answer given"},
        {"answer": "No question given"},
        {"question": "", "answer": "blank question"},
        {"question": "Blank answer", "answer": "   "},
        {"question": {"nested": "q"}, "answer": "a"},
        {"Question": "Different casing?", "Answer": "Still read"},
        {"question": "How many rooms?", "answer": 3},
    ]
    result = parse_angi_payload(leads(interview=interview))

    assert [(item.question, item.answer) for item in result.lead.interview] == [
        ("Good one?", "Yes"),
        ("Different casing?", "Still read"),
        ("How many rooms?", "3"),
    ]
    assert result.mode == "strict"
    assert result.drift.mistyped == ["interview: expected a list of {question, answer} objects"]
    assert not result.is_clean
    assert result.lead.extras["interview"] == interview  # nothing the consumer said is lost


def test_interview_with_renamed_item_keys_is_mistyped_drift_and_kept_in_extras():
    # {"q": .., "a": ..} instead of {"question": .., "answer": ..}: every answer would
    # silently vanish while the payload still looked clean.
    interview = [{"q": "What kind of location?", "a": "Home"}, {"q": "When?", "a": "ASAP"}]
    result = parse_angi_payload(leads(interview=interview))

    assert result.lead.interview == []
    assert result.drift.mistyped == ["interview: expected a list of {question, answer} objects"]
    assert result.drift.severity == "warning"
    assert result.signature != "angi_leads:clean"
    assert result.lead.extras["interview"] == interview
    # The same change on another lead is the same deviation: one alert, not one per lead.
    other = parse_angi_payload(leads(interview=[{"q": "Different?", "a": "Yes"}]))
    assert other.signature == result.signature


@pytest.mark.parametrize(
    "interview",
    [
        [],
        [{"question": "Blank answer", "answer": ""}],
        [{"question": "Extra key?", "answer": "Fine", "answerId": 7}],
        [{"Question": "Different casing?", "Answer": "Still read"}],
    ],
)
def test_interview_items_of_the_documented_shape_are_not_drift(interview):
    # An empty list, a blank answer or an extra key inside an item is not a shape change.
    result = parse_angi_payload(leads(interview=interview))

    assert result.is_clean
    assert "interview" not in result.lead.extras


@pytest.mark.parametrize("interview", ["Q: location? A: home", 5, True, {"question": "q", "answer": "a"}, None, []])
def test_interview_that_is_not_a_list_is_ignored(interview):
    # Whatever arrives instead of the array, the lead is still parsed, with no answers.
    result = parse_angi_payload(leads(interview=interview))
    assert result.lead.interview == []
    assert result.lead.email_normalized == "janedoe@example.com"
    assert result.mode == "strict"


def test_interview_is_capped_at_fifty_answers():
    # A runaway array must not bloat the lead.
    interview = [{"question": f"Question {n}?", "answer": f"Answer {n}"} for n in range(60)]
    result = parse_angi_payload(leads(interview=interview))
    assert len(result.lead.interview) == 50
    assert result.lead.interview[0].question == "Question 0?"
    assert result.lead.interview[-1].question == "Question 49?"


@pytest.mark.parametrize(
    ("masked", "expected"),
    [(True, True), (False, False), ("true", True), ("false", False), ("False", False), (" false ", False), ("maybe", None), (None, None)],
)
def test_masked_number_flag(masked, expected):
    # A masked number is an Angi proxy that must not be used for duplicate matching; "false" means not masked.
    result = parse_angi_payload(leads(primaryPhoneDetails={"maskedNumber": masked}))
    assert result.lead.phone_is_masked is expected


def test_masked_number_is_unknown_when_phone_details_are_absent_or_null():
    # Angi omits the object for many leads; that is neither "masked" nor drift.
    for payload in (without(ANGI_LEADS_EXAMPLE, "primaryPhoneDetails"), leads(primaryPhoneDetails=None)):
        result = parse_angi_payload(payload)
        assert result.lead.phone_is_masked is None
        assert result.is_clean
        assert result.mode == "strict"


def test_other_phone_details_are_kept_as_extras():
    # Documented fields we do not model yet are preserved, not reported as unknown.
    details = {"maskedNumber": True, "smsEnabled": False, "expirationDateTime": "2026-12-01T00:00:00Z"}
    result = parse_angi_payload(leads(primaryPhoneDetails=details))
    assert result.lead.phone_is_masked is True
    assert result.lead.extras == {
        "primaryPhoneDetails.smsEnabled": False,
        "primaryPhoneDetails.expirationDateTime": "2026-12-01T00:00:00Z",
    }
    assert result.is_clean


@pytest.mark.parametrize(
    ("compliant", "expected"),
    [(True, True), (False, False), ("true", True), ("false", False), ("FALSE", False)],
)
def test_automated_contact_consent_flag(compliant, expected):
    # Consent sent as the string "false" must never be read as consent.
    result = parse_angi_payload(leads(automatedContactCompliant=compliant))
    assert result.lead.automated_contact_ok is expected
    assert result.is_clean


def test_automated_contact_consent_is_unknown_when_absent():
    # "Angi did not say" stays None so nobody assumes consent.
    result = parse_angi_payload(without(ANGI_LEADS_EXAMPLE, "automatedContactCompliant"))
    assert result.lead.automated_contact_ok is None
    assert result.is_clean
    assert result.mode == "strict"


@pytest.mark.parametrize("key", ["trustedFormUrl", "trustedFormURL"])
def test_both_spellings_of_the_trusted_form_url_are_documented(key):
    # Angi's field table says trustedFormUrl, its example says trustedFormURL.
    payload = without(ANGI_LEADS_EXAMPLE, "trustedFormUrl")
    payload[key] = "https://cert.trustedform.com/xyz789"
    result = parse_angi_payload(payload)
    assert result.lead.trusted_form_url == "https://cert.trustedform.com/xyz789"
    assert result.is_clean
    assert result.mode == "strict"


def test_both_trusted_form_spellings_together_are_still_clean():
    # Sending both is redundant, not a format change, and neither ends up as an unknown field.
    result = parse_angi_payload(leads(trustedFormURL="https://cert.trustedform.com/abc123"))
    assert result.lead.trusted_form_url == "https://cert.trustedform.com/abc123"
    assert result.is_clean
    assert result.lead.extras == {}


def test_crm_key_is_carried_through():
    # crmKey is our own routing value echoed back by Angi; tenant mapping needs it verbatim.
    assert parse_angi_payload(leads(crmKey="tnt_summit")).lead.crm_key == "tnt_summit"
    assert parse_angi_payload(leads(crmKey=4711)).lead.crm_key == "4711"
    assert parse_angi_payload(leads(crmKey="")).lead.crm_key is None
    assert parse_angi_payload(leads(crmKey="tnt_summit")).is_clean


def test_null_secondary_phone_is_simply_absent():
    # The example sends "secondaryPhone": null.
    result = parse_angi_payload(leads(secondaryPhone=None))
    assert result.lead.secondary_phone is None
    assert result.lead.secondary_phone_normalized is None
    assert result.is_clean
    assert result.issues == []


def test_secondary_phone_is_normalised_too():
    # Duplicate matching looks at both numbers.
    result = parse_angi_payload(leads(secondaryPhone="(303) 555-0188", phoneExt=12))
    assert result.lead.secondary_phone == "(303) 555-0188"
    assert result.lead.secondary_phone_normalized == "+13035550188"
    assert result.lead.phone_ext == "12"
    assert result.is_clean


def test_appointment_object_lands_in_extras():
    # When the homeowner already picked a time on Angi, that information must not be lost.
    appointment = {
        "appointmentOid": 5551,
        "status": "SCHEDULED",
        "type": "IN_HOME",
        "start": "2026-11-20T15:00:00Z",
        "end": "2026-11-20T17:00:00Z",
        "external_id": "appt-1",
    }
    result = parse_angi_payload(leads(appointment=appointment))

    assert result.lead.extras == {f"appointment.{key}": value for key, value in appointment.items()}
    assert result.is_clean  # documented, so not drift
    assert result.mode == "strict"


def test_other_documented_but_unmodelled_fields_land_in_extras():
    # Recognised keys without a lead column are preserved without raising drift.
    result = parse_angi_payload(leads(secondaryPhoneExt="9", automatedContactConsentId="consent-1", contactStatus="New"))
    assert result.lead.extras == {"secondaryPhoneExt": "9", "automatedContactConsentId": "consent-1"}
    assert result.lead.contact_status == "New"
    assert result.is_clean


@pytest.mark.parametrize(
    ("name", "first", "last"),
    [
        ("Jane Doe", "Jane", "Doe"),
        ("  Jane   Doe  ", "Jane", "Doe"),
        ("Mary Jane Watson", "Mary", "Jane Watson"),
        ("Cher", "Cher", None),
    ],
)
def test_name_is_split_when_first_and_last_are_absent(name, first, last):
    # firstName/lastName are optional in this feed; the greeting still needs a first name.
    payload = without(ANGI_LEADS_EXAMPLE, "firstName", "lastName")
    payload["name"] = name
    result = parse_angi_payload(payload)

    assert (result.lead.first_name, result.lead.last_name) == (first, last)
    assert result.lead.full_name == name.strip()
    assert result.is_clean
    assert result.mode == "strict"


def test_explicit_first_and_last_name_win_over_splitting_the_full_name():
    # When Angi does send the parts, they are not second-guessed.
    result = parse_angi_payload(leads(name="Dr. Jane Q. Doe", firstName="Jane", lastName="Doe"))
    assert (result.lead.first_name, result.lead.last_name, result.lead.full_name) == ("Jane", "Doe", "Dr. Jane Q. Doe")


# --- drift -------------------------------------------------------------------------------------


def test_drift_severity_and_cleanliness():
    # New fields are informational; anything that breaks the documented mapping is a warning.
    assert Drift().is_clean and Drift().severity is None
    assert Drift(unknown=["x"]).severity == "info"
    assert Drift(missing=["x"]).severity == "warning"
    assert Drift(mistyped=["x: expected string, got number"]).severity == "warning"
    assert Drift(recovered=["email <- x"]).severity == "warning"
    assert Drift(unknown=["x"], missing=["y"]).severity == "warning"
    assert not Drift(unknown=["x"]).is_clean


def test_extra_unknown_field_is_reported_but_stays_strict():
    # Angi adding a field must not degrade parsing, but someone should hear about it.
    result = parse_angi_payload(ads(HomeownerRating="5 stars"))

    assert result.mode == "strict"
    assert result.drift.unknown == ["HomeownerRating"]
    assert result.drift.missing == [] and result.drift.mistyped == [] and result.drift.recovered == []
    assert result.drift.severity == "info"
    assert not result.is_clean
    assert result.signature != "angi_ads:clean"
    assert result.lead.extras["HomeownerRating"] == "5 stars"  # kept, not dropped
    assert result.lead.email_normalized == "bob.builder@gmail.com"


def test_unknown_nested_field_is_reported_with_its_path():
    # The path tells the engineer where the new field sits.
    payload = ads_address(County="Marion")
    payload["Campaign"] = {"Id": 7, "Tags": ["a", "b"]}
    result = parse_angi_payload(payload)

    assert result.drift.unknown == ["Campaign.Id", "Campaign.Tags", "PostalAddress.County"]
    assert result.mode == "strict"
    assert result.lead.extras["PostalAddress.County"] == "Marion"
    assert result.lead.extras["Campaign.Tags"] == ["a", "b"]


def test_blank_unknown_field_is_still_drift():
    # The key appearing is the format change, whatever its value in this one lead.
    result = parse_angi_payload(ads(HomeownerRating=""))
    assert result.drift.unknown == ["HomeownerRating"]
    assert "HomeownerRating" not in result.lead.extras  # nothing worth keeping


@pytest.mark.parametrize("key", [key for key in ADS_REQUIRED if "." not in key])
def test_absent_always_present_key_is_missing_drift(key):
    # A documented key vanishing means the mapping no longer fits: warn, and fall back to tolerant mode.
    result = parse_angi_payload(without(ANGI_ADS_EXAMPLE, key))

    assert result.drift.missing == [key]
    assert result.drift.severity == "warning"
    assert result.mode == "tolerant"
    assert not result.is_clean
    assert result.lead is not None  # the lead is still delivered


@pytest.mark.parametrize("key", ["AddressFirstLine", "City", "State", "PostalCode"])
def test_absent_nested_always_present_key_is_missing_drift(key):
    # Same rule inside PostalAddress.
    payload = ads()
    del payload["PostalAddress"][key]
    result = parse_angi_payload(payload)
    assert result.drift.missing == [f"PostalAddress.{key}"]
    assert result.mode == "tolerant"


@pytest.mark.parametrize("address", [None, {}])
def test_missing_postal_address_object_reports_each_of_its_fields(address):
    # Without the address object the lead is still usable; the drift names what is gone.
    payload = ads(PostalAddress=address) if address is not None else without(ANGI_ADS_EXAMPLE, "PostalAddress")
    result = parse_angi_payload(payload)

    assert result.drift.missing == [
        "PostalAddress.AddressFirstLine",
        "PostalAddress.City",
        "PostalAddress.State",
        "PostalAddress.PostalCode",
    ]
    assert result.mode == "tolerant"
    assert result.lead.email_normalized == "bob.builder@gmail.com"
    assert (result.lead.city, result.lead.state, result.lead.postal_code) == (None, None, None)


def test_null_or_empty_value_of_a_documented_key_is_not_drift():
    # The key is there; this lead just has no value for it.
    for value in (None, ""):
        result = parse_angi_payload(ads(Urgency=value))
        assert result.is_clean
        assert result.mode == "strict"
        assert result.lead.urgency is None


def test_absent_optional_key_of_the_leads_feed_is_not_drift():
    # spEntityId is documented as sent "sometimes".
    result = parse_angi_payload(without(ANGI_LEADS_EXAMPLE, "spEntityId", "leadSource", "trustedFormUrl", "interview"))
    assert result.is_clean
    assert result.mode == "strict"
    assert result.lead.external_account_id is None


@pytest.mark.parametrize(
    ("key", "value", "entry"),
    [
        ("FirstName", 42, "FirstName: expected string, got number"),
        ("FirstName", True, "FirstName: expected string, got boolean"),
        ("Email", ["bob.builder@gmail.com"], "Email: expected string, got array"),
        ("Email", {}, "Email: expected string, got object"),
        ("Email", 5, "Email: expected string, got number"),
        ("Description", ["a", "b"], "Description: expected string, got array"),
        ("PhoneNumber", True, "PhoneNumber: expected string/number, got boolean"),
        ("ALAccountId", ["123456"], "ALAccountId: expected string/number, got array"),
    ],
)
def test_wrong_json_type_is_mistyped_drift(key, value, entry):
    # A type change is a format change even when the key name is unchanged.
    result = parse_angi_payload(ads(**{key: value}))

    assert result.drift.mistyped == [entry]
    assert result.drift.severity == "warning"
    assert not result.is_clean
    assert result.lead is not None


def test_wrong_json_type_in_the_leads_feed_is_mistyped_drift():
    # Same rule for the second feed, including the flag and array fields.
    result = parse_angi_payload(leads(interview="none", fee=True, automatedContactCompliant=1))
    assert sorted(result.drift.mistyped) == [
        "automatedContactCompliant: expected boolean/string, got number",
        "fee: expected number/string, got boolean",
        "interview: expected array, got string",
    ]
    assert result.lead.fee_cents is None
    assert result.lead.interview == []


def test_number_in_a_name_field_is_used_but_flagged():
    # The lead still goes out with what was sent; the drift report says the type changed.
    result = parse_angi_payload(ads(FirstName=42))
    assert result.lead.first_name == "42"
    assert result.lead.email_normalized == "bob.builder@gmail.com"


def test_email_sent_as_an_array_is_flagged_and_not_guessed_from():
    # Never email an address dug out of a structure we do not understand.
    result = parse_angi_payload(ads(Email=["bob.builder@gmail.com"]))
    assert result.lead.email is None
    assert result.lead.email_normalized is None
    assert "no email address in the payload" in result.issues
    assert result.lead.is_contactable  # the phone is still there


def test_scalar_field_sent_as_an_object_is_mistyped_drift():
    # "Email": {"address": ...} is a type change of Email, and should be reported as one.
    result = parse_angi_payload(ads(Email={"address": "bob.builder@gmail.com", "verified": True}))
    assert any(entry.startswith("Email:") for entry in result.drift.mistyped)


def test_documented_object_sent_as_a_string_is_mistyped_drift_and_its_text_is_used():
    # "PostalAddress" is documented as an object. As one string it is a type change (reported
    # as such, with the fields it should have had reported missing), and the text is the address.
    text = "123 Main St. Indianapolis, IN 46203"
    result = parse_angi_payload(ads(PostalAddress=text))

    assert result.drift.mistyped == ["PostalAddress: expected object, got string"]
    assert "PostalAddress.City" in result.drift.missing
    assert result.lead.address_line1 == text
    assert (result.lead.state, result.lead.postal_code) == ("IN", "46203")


def test_documented_object_sent_as_a_list_is_kept_in_extras():
    # Nothing usable as an address, but nothing thrown away either.
    result = parse_angi_payload(ads(PostalAddress=["123 Main St.", "Indianapolis"]))

    assert result.drift.mistyped == ["PostalAddress: expected object, got array"]
    assert result.lead.extras["PostalAddress"] == ["123 Main St.", "Indianapolis"]
    assert result.lead.address_line1 is None


def test_known_format_is_only_searched_for_the_fields_it_documents():
    # Angi Ads has no fee and no single "name" field. A new "Price" or "Campaign.Name" is a
    # new field to report, not the lead's fee or the consumer's name.
    result = parse_angi_payload(ads(Price="45.00", Campaign={"Name": "Fall Promo"}))

    assert result.lead.fee_cents is None
    assert result.lead.full_name == "Bob Builder"
    assert result.drift.recovered == []
    assert result.mode == "strict"
    assert result.lead.extras["Price"] == "45.00"
    assert result.lead.extras["Campaign.Name"] == "Fall Promo"


def test_postal_address_sent_as_a_string_is_not_silently_dropped():
    # Nothing in the payload may vanish: the address must reach the lead, the extras or the drift report.
    text = "123 Main St. Indianapolis, IN 46203"
    result = parse_angi_payload(ads(PostalAddress=text))

    kept = text in (result.lead.address_line1, *result.lead.extras.values())
    reported = any("PostalAddress" in entry for entry in result.drift.unknown + result.drift.mistyped)
    assert kept or reported


def test_renamed_key_is_recovered_by_alias():
    # Email -> EmailAddress: the consumer must still get the intro email while a human looks at the alert.
    payload = without(ANGI_ADS_EXAMPLE, "Email")
    payload["EmailAddress"] = "bob.builder@gmail.com"
    result = parse_angi_payload(payload)

    assert result.detected_format == "angi_ads"
    assert result.mode == "tolerant"
    assert result.drift.recovered == ["email <- EmailAddress"]
    assert result.drift.missing == ["Email"]
    assert result.drift.unknown == ["EmailAddress"]
    assert result.drift.severity == "warning"
    assert result.lead.email == "bob.builder@gmail.com"
    assert result.lead.email_normalized == "bob.builder@gmail.com"
    assert "EmailAddress" not in result.lead.extras  # it was understood, so it is not an "extra"
    assert result.issues == []


@pytest.mark.parametrize(
    ("old", "new", "attribute", "expected"),
    [
        ("PhoneNumber", "Phone", "phone_normalized", "+15554332646"),
        ("PhoneNumber", "mobile_phone", "phone_normalized", "+15554332646"),
        ("FirstName", "first_name", "first_name", "Bob"),
        ("LastName", "Surname", "last_name", "Builder"),
        ("CorrelationId", "LeadId", "external_lead_id", "61a7de56-dba3-4e59-8e2a-3fa827f84f7f"),
        ("ALAccountId", "AccountId", "external_account_id", "123456"),
        ("Description", "Comments", "comments", "I'm Looking for recurring house cleaning services please."),
        ("Urgency", "Timeframe", "urgency", "This Week"),
    ],
)
def test_other_renamed_keys_are_recovered_by_alias(old, new, attribute, expected):
    # The alias table covers the obvious renames of every field that matters.
    payload = without(ANGI_ADS_EXAMPLE, old)
    payload[new] = ANGI_ADS_EXAMPLE[old]
    result = parse_angi_payload(payload)

    assert result.detected_format == "angi_ads"
    assert result.mode == "tolerant"
    assert getattr(result.lead, attribute) == expected
    assert result.drift.missing == [old]
    assert len(result.drift.recovered) == 1 and result.drift.recovered[0].endswith(f"<- {new}")


def test_documented_key_wins_over_an_alias_lookalike():
    # Recovery only fills gaps; it never overrides a value the documented key provided.
    result = parse_angi_payload(ads(EmailAddress="someone.else@example.com"))
    assert result.lead.email_normalized == "bob.builder@gmail.com"
    assert result.drift.recovered == []
    assert result.drift.unknown == ["EmailAddress"]
    assert result.mode == "strict"
    assert result.lead.extras["EmailAddress"] == "someone.else@example.com"


def test_fully_case_drifted_angi_ads_payload_is_still_understood():
    # If Angi switched the feed to camelCase overnight, leads must keep flowing.
    payload = {
        "firstName": "Bob",
        "lastName": "Builder",
        "phoneNumber": "5554332646",
        "postalAddress": {
            "addressFirstLine": "123 Main St.",
            "city": "Indianapolis",
            "state": "IN",
            "postalCode": "46203",
        },
        "email": "bob.builder@gmail.com",
        "source": "Angie's List Quote Request",
        "description": "I'm Looking for recurring house cleaning services please.",
        "category": "Indianapolis – House Cleaning",
        "urgency": "This Week",
        "correlationId": "61a7de56-dba3-4e59-8e2a-3fa827f84f7f",
        "alAccountId": "123456",
    }
    result = parse_angi_payload(payload)

    assert result.detected_format == "angi_ads"
    assert result.mode == "tolerant"
    lead = result.lead
    assert lead.email_normalized == "bob.builder@gmail.com"
    assert lead.phone_normalized == "+15554332646"
    assert (lead.first_name, lead.last_name, lead.full_name) == ("Bob", "Builder", "Bob Builder")
    assert lead.external_lead_id == "61a7de56-dba3-4e59-8e2a-3fa827f84f7f"
    assert lead.external_account_id == "123456"
    assert (lead.address_line1, lead.city, lead.state, lead.postal_code) == ("123 Main St.", "Indianapolis", "IN", "46203")
    assert (lead.market, lead.task_name) == ("Indianapolis", "House Cleaning")
    assert lead.urgency == "This Week"
    assert lead.comments == "I'm Looking for recurring house cleaning services please."
    assert lead.lead_source_detail == "Angie's List Quote Request"
    # Everything it carries is the same as the documented example would give.
    assert lead.model_dump() == parse_angi_payload(ANGI_ADS_EXAMPLE).lead.model_dump()

    assert sorted(result.drift.missing) == sorted(ADS_REQUIRED)
    assert "email <- email" in result.drift.recovered
    assert "external_account_id <- alAccountId" in result.drift.recovered
    assert "postal_code <- postalAddress.postalCode" in result.drift.recovered
    assert result.drift.severity == "warning"
    assert not result.is_clean


def test_snake_cased_angi_ads_payload_is_still_understood():
    # Separators are ignored the same way casing is.
    payload = {
        "first_name": "Bob",
        "last_name": "Builder",
        "phone_number": "5554332646",
        "postal_address": {"address_first_line": "123 Main St.", "city": "Indianapolis", "state": "IN", "postal_code": "46203"},
        "email": "bob.builder@gmail.com",
        "source": "Angie's List Quote Request",
        "description": "I'm Looking for recurring house cleaning services please.",
        "category": "Indianapolis – House Cleaning",
        "urgency": "This Week",
        "correlation_id": "61a7de56-dba3-4e59-8e2a-3fa827f84f7f",
        "al_account_id": "123456",
    }
    result = parse_angi_payload(payload)

    assert result.detected_format == "angi_ads"
    assert result.mode == "tolerant"
    assert result.lead.model_dump() == parse_angi_payload(ANGI_ADS_EXAMPLE).lead.model_dump()


def test_pascal_cased_angi_leads_payload_is_still_understood():
    # The same resilience for the second feed, including its nested flag and interview array.
    def pascal(value: Any) -> Any:
        if isinstance(value, dict):
            return {key[0].upper() + key[1:]: pascal(inner) for key, inner in value.items()}
        if isinstance(value, list):
            return [pascal(inner) for inner in value]
        return value

    result = parse_angi_payload(pascal(ANGI_LEADS_EXAMPLE))

    assert result.detected_format == "angi_leads"
    assert result.mode == "tolerant"
    assert result.lead.model_dump() == parse_angi_payload(ANGI_LEADS_EXAMPLE).lead.model_dump()


@pytest.mark.parametrize(
    ("payload", "wrappers"),
    [
        ({"lead": ANGI_ADS_EXAMPLE}, ["<wrapper:lead>"]),
        ({"data": {"lead": ANGI_ADS_EXAMPLE}}, ["<wrapper:data>", "<wrapper:lead>"]),
        ({"payload": {"data": {"lead": ANGI_ADS_EXAMPLE}}}, ["<wrapper:payload>", "<wrapper:data>", "<wrapper:lead>"]),
    ],
)
def test_wrapper_object_is_unwrapped_and_reported(payload, wrappers):
    # An envelope around the documented object changes nothing about the lead, but is a format change.
    result = parse_angi_payload(payload)

    assert result.detected_format == "angi_ads"
    assert result.mode == "strict"
    assert result.drift.unknown == wrappers
    assert result.drift.severity == "info"
    assert not result.is_clean
    assert result.signature != "angi_ads:clean"
    assert result.lead.model_dump() == parse_angi_payload(ANGI_ADS_EXAMPLE).lead.model_dump()


def test_wrapped_angi_leads_payload_is_unwrapped_too():
    # Unwrapping is independent of the format inside.
    result = parse_angi_payload({"lead": ANGI_LEADS_EXAMPLE})
    assert result.detected_format == "angi_leads"
    assert result.drift.unknown == ["<wrapper:lead>"]
    assert result.lead.external_lead_id == "987654321"


def test_envelope_named_by_the_caller_is_reported_before_wrappers():
    # A body of [{"lead": {...}}] reports both layers, outermost first.
    result = parse_angi_payload({"lead": ANGI_ADS_EXAMPLE}, envelope=("array",))
    assert result.drift.unknown == ["<array>", "<wrapper:lead>"]
    assert result.mode == "strict"
    assert not result.is_clean


def test_lead_buried_under_many_wrappers_and_siblings_is_still_recovered():
    # Beyond what unwrapping handles, alias matching still finds the contact details.
    deep = {"a": {"b": {"c": {"d": {"e": {"f": ANGI_ADS_EXAMPLE}}}}}}
    with_sibling = {"meta": {"version": 2}, "lead": ANGI_ADS_EXAMPLE}
    for payload in (deep, with_sibling):
        result = parse_angi_payload(payload)
        assert result.mode == "tolerant"
        assert result.lead.email_normalized == "bob.builder@gmail.com"
        assert result.lead.phone_normalized == "+15554332646"


def test_unknown_format_with_generic_contact_fields_is_tolerant_with_a_lead():
    # An unrecognisable payload that still tells us how to reach the consumer is not thrown away.
    payload = {
        "contact": {"email_address": "Pat.Lee@Example.com", "mobile": "(555) 433-2646"},
        "full_name": "Pat Lee",
    }
    result = parse_angi_payload(payload)

    assert result.detected_format == "unknown"
    assert result.mode == "tolerant"
    assert result.lead is not None
    assert result.lead.source_format == "unknown"
    assert result.lead.email_normalized == "pat.lee@example.com"
    assert result.lead.phone_normalized == "+15554332646"
    assert (result.lead.first_name, result.lead.last_name, result.lead.full_name) == ("Pat", "Lee", "Pat Lee")
    assert sorted(result.drift.recovered) == [
        "email <- contact.email_address",
        "full_name <- full_name",
        "phone <- contact.mobile",
    ]
    assert not result.is_clean  # an unknown format is never "clean"
    assert re.fullmatch(r"unknown:[0-9a-f]{12}", result.signature)


def test_alias_recovery_takes_the_consumer_not_the_business():
    # Both parties have a name, a phone and an email. Taking the business's would send the
    # business its own introduction and leave the real consumer unanswered.
    payload = {
        "provider_id": "123456",
        "company": {"name": "Sparkle Home Cleaning", "email": "office@sparkle.example.com", "phone": "317-555-0100"},
        "customer": {"name": "Olivia Chen", "email": "olivia.chen@example.com", "phone": "317-555-0184"},
    }
    result = parse_angi_payload(payload)

    lead = result.lead
    assert (lead.full_name, lead.email_normalized, lead.phone_normalized) == (
        "Olivia Chen",
        "olivia.chen@example.com",
        "+13175550184",
    )
    assert lead.external_account_id == "123456"
    assert lead.external_account_name is None  # "company.name" is not one of the account-name aliases
    assert lead.extras["company.email"] == "office@sparkle.example.com"  # kept, just not used


@pytest.mark.parametrize(
    "branch",
    ["provider", "pro", "sp", "spContact", "business", "company", "vendor", "advertiser", "account", "tenant", "partner"]
    + ["serviceProvider", "sp_contact", "SPInfo", "businessOwner"],
)
def test_consumer_details_are_never_read_from_a_branch_that_names_the_business(branch):
    # With nothing else to go on, there is no consumer email: better none than the wrong one.
    payload = {branch: {"name": "Dispatcher Dan", "email": "dispatch@example.com", "phone": "303-555-0100"}}
    result = parse_angi_payload({**payload, "taskName": "Furnace repair"})

    assert result.mode == "failed"
    assert result.drift.recovered == ["task_name <- taskName"]


@pytest.mark.parametrize("branch", ["customer", "consumer", "homeowner", "contact", "lead", "client", "primaryContact"])
def test_branch_that_names_the_consumer_is_preferred_over_an_unlabelled_one(branch):
    # A value under "customer" is certainly the consumer's; one under "meta" only might be.
    payload = {
        "meta": {"email": "noreply@example.com", "phone": "303-555-0100"},
        branch: {"email": "pat@example.com", "phone": "303-555-0123"},
        "request": {"id": 1},
    }
    result = parse_angi_payload(payload)

    assert result.lead.email_normalized == "pat@example.com"
    assert result.lead.phone_normalized == "+13035550123"


def test_unlabelled_branch_is_still_used_when_nothing_names_the_consumer():
    # The preference is only a preference: a renamed or re-nested key is still recovered.
    result = parse_angi_payload({"details": {"email": "pat@example.com"}, "request": {"id": 1}})

    assert result.lead.email_normalized == "pat@example.com"
    assert result.drift.recovered == ["email <- details.email"]


def test_account_fields_are_read_from_the_business_branch_never_the_consumers():
    # The opposite rule for the fields that identify the business.
    payload = {
        "customer": {"email": "pat@example.com", "accountId": "consumer-login-77", "companyName": "Pat's Employer"},
        "provider": {"accountId": "123456", "companyName": "Sparkle Home Cleaning"},
        "meta": {"accountId": "999"},
    }
    result = parse_angi_payload(payload)

    assert result.lead.external_account_id == "123456"
    assert result.lead.external_account_name == "Sparkle Home Cleaning"
    assert result.lead.email_normalized == "pat@example.com"


def test_known_format_with_a_blank_email_does_not_take_the_business_contacts_email():
    # Angi Leads lead with no consumer email plus a new object describing the pro's contact:
    # the consumer must not be "found" there.
    payload = leads(email="", spContact={"name": "Dispatcher Dan", "email": "dispatch@example.com"})
    result = parse_angi_payload(payload)

    assert result.lead.email_normalized is None
    assert result.drift.recovered == []
    assert result.lead.extras["spContact.email"] == "dispatch@example.com"
    assert "no email address in the payload" in result.issues


@pytest.mark.parametrize(
    "payload",
    [
        {"email": "pat@example.com"},
        {"phone": "555-433-2646"},
        {"customer": {"contactEmail": "pat@example.com"}},
        {"CorrelationId": "abc", "Email": "pat@example.com"},  # one Angi key is not a format
    ],
)
def test_unknown_format_needs_only_one_way_to_reach_the_consumer(payload):
    # Email or phone alone is enough to keep the lead.
    result = parse_angi_payload(payload)
    assert result.detected_format == "unknown"
    assert result.mode == "tolerant"
    assert result.lead.is_contactable


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"hello": "world"},
        {"order": {"id": 17, "total": 99.5}, "items": [1, 2, 3]},
        {"full_name": "Pat Lee", "city": "Denver"},  # a name but no way to reach them
        {"email": "not-an-address", "phone": "12345"},  # contact fields present but unusable
        {"email": None, "phone": ""},
        {"leads": [ANGI_ADS_EXAMPLE, ANGI_ADS_EXAMPLE]},  # a batch of several leads is not guessed at
    ],
)
def test_unknown_format_without_contact_details_fails(payload):
    # Nothing to map and nobody to contact: give up, keep the raw event for a human.
    result = parse_angi_payload(payload)

    assert result.detected_format == "unknown"
    assert result.mode == "failed"
    assert result.lead is None
    assert not result.is_clean
    assert "no usable contact details (no valid email or phone)" in result.issues


def test_single_lead_inside_a_list_wrapper_is_unwrapped():
    # {"leads": [{...}]} carries exactly one lead: use it, and report the wrapper as drift.
    result = parse_angi_payload({"leads": [ANGI_ADS_EXAMPLE]})

    assert result.detected_format == "angi_ads"
    assert result.lead is not None
    assert result.lead.email_normalized == "bob.builder@gmail.com"
    assert "<wrapper:leads[]>" in result.drift.unknown
    assert not result.is_clean


# --- signatures ----------------------------------------------------------------------------------


def test_same_deviation_gives_the_same_signature_whatever_the_values():
    # One format change must raise one alert with a counter, not one alert per lead.
    first = parse_angi_payload(ads(HomeownerRating="5 stars"))
    second = parse_angi_payload(
        ads(
            HomeownerRating="1 star",
            FirstName="Alice",
            Email="alice@example.com",
            CorrelationId="another-lead",
            Category="Plumbing",
        )
    )
    assert first.signature == second.signature
    assert re.fullmatch(r"angi_ads:[0-9a-f]{12}", first.signature)


def test_same_missing_key_gives_the_same_signature_for_different_leads():
    # Missing-key drift is about the shape, not the lead.
    first = parse_angi_payload(without(ANGI_ADS_EXAMPLE, "Urgency"))
    second = parse_angi_payload(without(ads(FirstName="Alice", CorrelationId="another-lead"), "Urgency"))
    assert first.signature == second.signature


def test_same_type_change_gives_the_same_signature_for_different_values():
    # FirstName arriving as a number is one deviation, whichever number it is.
    assert parse_angi_payload(ads(FirstName=42)).signature == parse_angi_payload(ads(FirstName=43)).signature


def test_same_rename_gives_the_same_signature_for_different_values():
    # Email -> EmailAddress is one deviation for every lead that has an email.
    base = without(ANGI_ADS_EXAMPLE, "Email")
    first = parse_angi_payload({**base, "EmailAddress": "bob.builder@gmail.com"})
    second = parse_angi_payload({**base, "EmailAddress": "alice@example.org", "FirstName": "Alice"})
    assert first.signature == second.signature


def test_signature_does_not_depend_on_key_order():
    # JSON objects are unordered; a reshuffled payload is the same shape.
    payload = ads(HomeownerRating="5 stars", Zebra=1, Apple=2)
    reversed_payload = dict(reversed(list(payload.items())))
    assert parse_angi_payload(payload).signature == parse_angi_payload(reversed_payload).signature


def test_different_deviations_give_different_signatures():
    # Each kind of change is its own alert.
    signatures = {
        "clean": parse_angi_payload(ANGI_ADS_EXAMPLE).signature,
        "extra field": parse_angi_payload(ads(HomeownerRating="5")).signature,
        "another extra field": parse_angi_payload(ads(HomeownerScore="5")).signature,
        "two extra fields": parse_angi_payload(ads(HomeownerRating="5", HomeownerScore="5")).signature,
        "missing Email": parse_angi_payload(without(ANGI_ADS_EXAMPLE, "Email")).signature,
        "missing Urgency": parse_angi_payload(without(ANGI_ADS_EXAMPLE, "Urgency")).signature,
        "mistyped FirstName": parse_angi_payload(ads(FirstName=42)).signature,
        "mistyped LastName": parse_angi_payload(ads(LastName=42)).signature,
        "renamed Email": parse_angi_payload({**without(ANGI_ADS_EXAMPLE, "Email"), "EmailAddress": "a@example.com"}).signature,
        "wrapper": parse_angi_payload({"lead": ANGI_ADS_EXAMPLE}).signature,
        "other wrapper": parse_angi_payload({"data": ANGI_ADS_EXAMPLE}).signature,
        "array envelope": parse_angi_payload(ANGI_ADS_EXAMPLE, envelope=("array",)).signature,
        "leads clean": parse_angi_payload(ANGI_LEADS_EXAMPLE).signature,
        "leads extra field": parse_angi_payload(leads(HomeownerRating="5")).signature,
        "unknown": parse_angi_payload({"email": "a@example.com"}).signature,
        "empty": parse_angi_payload({}).signature,
    }
    assert len(set(signatures.values())) == len(signatures), signatures


def test_only_a_known_format_without_drift_is_called_clean():
    # "<format>:clean" is what monitoring treats as healthy.
    assert parse_angi_payload(ANGI_ADS_EXAMPLE).signature == "angi_ads:clean"
    assert parse_angi_payload(ANGI_LEADS_EXAMPLE).signature == "angi_leads:clean"
    assert not parse_angi_payload({}).signature.endswith(":clean")
    assert not parse_angi_payload({"email": "a@example.com"}).signature.endswith(":clean")


def test_same_rename_gives_the_same_signature_when_the_value_is_blank():
    # A lead without an email is still the same format change (Email -> EmailAddress): one alert, not two.
    base = without(ANGI_ADS_EXAMPLE, "Email")
    with_value = parse_angi_payload({**base, "EmailAddress": "bob.builder@gmail.com"})
    blank = parse_angi_payload({**base, "EmailAddress": ""})
    assert with_value.signature == blank.signature


# --- data quality (issues, not drift) ---------------------------------------------------------------


@pytest.mark.parametrize("email", ["not-an-address", "bob@", "@gmail.com", "bob builder@gmail.com", "bob@gmail"])
def test_invalid_email_is_an_issue_not_drift(email):
    # One consumer's typo says nothing about the format.
    result = parse_angi_payload(ads(Email=email))

    assert result.issues == ["email is not a valid address"]
    assert result.lead.email == email  # kept as sent, for a human to fix
    assert result.lead.email_normalized is None
    assert result.is_clean
    assert result.mode == "strict"
    assert result.signature == "angi_ads:clean"
    assert result.lead.is_contactable  # by phone


@pytest.mark.parametrize("email", ["", "   ", None])
def test_blank_email_is_an_issue_not_drift(email):
    # The key is present, the value is not: a data gap in this lead only.
    result = parse_angi_payload(ads(Email=email))

    assert result.issues == ["no email address in the payload"]
    assert result.lead.email is None
    assert result.lead.email_normalized is None
    assert result.is_clean
    assert result.mode == "strict"


@pytest.mark.parametrize("phone", ["123", "555-0100", "0554332646", "call me", "+44 20 7946 0958"])
def test_invalid_phone_is_an_issue_not_drift(phone):
    # An unusable number is flagged; it must never be used for duplicate matching.
    result = parse_angi_payload(ads(PhoneNumber=phone))

    assert result.issues == ["primary phone is not a valid US/Canada number"]
    assert result.lead.phone == phone
    assert result.lead.phone_normalized is None
    assert result.is_clean
    assert result.mode == "strict"
    assert result.lead.is_contactable  # by email


@pytest.mark.parametrize("fee", ["abc", "free", "12 USD", -5, "-25.67"])
def test_non_numeric_or_negative_fee_is_an_issue_not_drift(fee):
    # A fee we cannot read is "unknown", never zero, and never blocks the lead.
    result = parse_angi_payload(leads(fee=fee))

    assert result.issues == ["fee is not a valid amount"]
    assert result.lead.fee_cents is None
    assert result.is_clean
    assert result.mode == "strict"


def test_known_format_without_any_contact_details_still_returns_a_lead():
    # The tenant paid for this lead and Angi will not resend it, so it is stored for a human to chase.
    result = parse_angi_payload(ads(Email="", PhoneNumber=""))

    assert result.mode == "strict"
    assert result.lead is not None
    assert result.lead.is_contactable is False
    assert result.issues == [
        "no email address in the payload",
        "no usable contact details (no valid email or phone)",
    ]
    assert result.is_clean  # still exactly the documented shape
    assert result.lead.external_lead_id == "61a7de56-dba3-4e59-8e2a-3fa827f84f7f"


def test_known_format_with_only_unusable_contact_details_still_returns_a_lead():
    # Invalid email + invalid phone is the same situation with more to tell the human.
    result = parse_angi_payload(leads(email="nope", primaryPhone="123"))
    assert result.mode == "strict"
    assert result.lead.is_contactable is False
    assert result.issues == [
        "email is not a valid address",
        "primary phone is not a valid US/Canada number",
        "no usable contact details (no valid email or phone)",
    ]


def test_secondary_phone_alone_makes_a_lead_contactable():
    # Any one valid channel is enough.
    result = parse_angi_payload(leads(email="", primaryPhone="", secondaryPhone="303-555-0188"))
    assert result.lead.is_contactable
    assert result.issues == ["no email address in the payload"]


# --- robustness --------------------------------------------------------------------------------------

FUZZ_VALUES: list[Any] = [
    None,
    True,
    False,
    0,
    1,
    -1,
    2134,
    3.14,
    -0.0,
    1e308,
    float("nan"),
    float("inf"),
    float("-inf"),
    2**63,
    -(2**63) - 1,
    10**30,
    10**400,
    [],
    [None],
    [1, "two", None, [3]],
    [{"question": None, "answer": 5}],
    {},
    {"a": 1},
    {"": ""},
    {"a": {"b": {"c": {"d": {"e": {"f": 1}}}}}},
    "",
    " ",
    "\t\r\n",
    "x" * 20_000,
    "\x00",
    "\x1f\x7f",
    "‮​",
    "\ud83d",  # half of an emoji, as a truncating encoder produces
    "José Ünal",
    "李小龙",
    "😀👍🏽",
    "<script>alert(1)</script>",
    "'; DROP TABLE leads; --",
    "null",
    "true",
    "{}",
    "–",
    " – ",
    "a – b",
    "a - b",
    "@",
    "a@b",
    "pat@example.com",
    "5554332646",
    "$1,234.50",
    "1e400",
    "NaN",
]
FUZZ_KEYS = [
    "",
    " ",
    ".",
    "..",
    "a.b",
    "-",
    "!!!",
    "\x00",
    "😀",
    "email",
    "EMAIL",
    "e-mail",
    "phone",
    "name",
    "lead",
    "data",
    "PostalAddress",
    "PostalAddress.City",
    "interview",
    "fee",
    "leadOid",
    "CorrelationId",
    "category",
    "k" * 500,
]
FUZZ_BASES = [ANGI_ADS_EXAMPLE, ANGI_LEADS_EXAMPLE, {"contact": {"email": "pat@example.com"}, "name": "Pat"}]


def mutate(rng: random.Random, payload: dict[str, Any]) -> dict[str, Any]:
    """Apply one to five random edits: replace / delete / add / nest / wrap / re-case."""
    payload = copy.deepcopy(payload)
    for _ in range(rng.randint(1, 5)):
        action = rng.choice(["replace", "delete", "add", "nest", "wrap", "recase"])
        keys = list(payload)
        if action == "add":
            payload[rng.choice(FUZZ_KEYS)] = rng.choice(FUZZ_VALUES)
        elif action == "wrap":
            payload = {rng.choice(FUZZ_KEYS): payload}
        elif not keys:
            continue
        elif action == "replace":
            payload[rng.choice(keys)] = rng.choice(FUZZ_VALUES)
        elif action == "delete":
            del payload[rng.choice(keys)]
        elif action == "nest":
            key = rng.choice(keys)
            value = payload[key]
            for _ in range(rng.randint(1, 7)):
                value = {rng.choice(FUZZ_KEYS): value}
            payload[key] = value
        elif action == "recase":
            key = rng.choice(keys)
            value = payload.pop(key)
            payload[rng.choice([key.lower(), key.upper(), f"{key}_", f"_{key}", key[::-1]])] = value
    return payload


def test_fuzzed_payloads_never_raise_and_always_give_a_consistent_result():
    # Whatever Angi (or anyone else) posts, parsing must end in strict, tolerant or failed - never an exception.
    rng = random.Random(20261112)
    seen_modes: dict[str, int] = {mode: 0 for mode in MODES}
    for _ in range(4000):
        payload = mutate(rng, rng.choice(FUZZ_BASES))
        before = repr(payload)

        result = parse_angi_payload(payload)

        assert_well_formed(result)
        assert repr(payload) == before  # the input is never modified
        again = parse_angi_payload(payload)
        assert (again.mode, again.signature, again.issues) == (result.mode, result.signature, result.issues)
        seen_modes[result.mode] += 1
    assert all(count > 100 for count in seen_modes.values()), seen_modes  # every outcome was exercised


def test_every_field_of_both_examples_survives_every_junk_value():
    # Systematic version of the fuzz: each documented key, one at a time, set to each nasty value.
    for example in (ANGI_ADS_EXAMPLE, ANGI_LEADS_EXAMPLE):
        for key in example:
            for value in FUZZ_VALUES:
                payload = copy.deepcopy(example)
                payload[key] = value
                result = parse_angi_payload(payload)
                assert_well_formed(result)
                assert result.detected_format != "unknown"  # one bad value does not hide the format
                assert result.lead is not None  # ... and never costs the lead
    for key in ANGI_ADS_EXAMPLE["PostalAddress"]:
        for value in FUZZ_VALUES:
            result = parse_angi_payload(ads_address(**{key: value}))
            assert_well_formed(result)
            assert result.lead is not None


NASTY_PAYLOADS: dict[str, dict[str, Any]] = {
    "empty object": {},
    "empty key and value": {"": ""},
    "empty key null value": {"": None},
    "blank key and value": {" ": " "},
    "punctuation-only keys": {"!!!": "???", "...": "---", "@#$%": "^&*", "{}": "[]", '"': "'"},
    "dots all the way down": {".": {".": {".": {".": "."}}}},
    "six levels of nesting": {"a": {"b": {"c": {"d": {"e": {"f": "deep"}}}}}},
    "six levels around a lead": {"a": {"b": {"c": {"d": {"e": {"f": ANGI_ADS_EXAMPLE}}}}}},
    "six levels inside a lead": {**ANGI_ADS_EXAMPLE, "Extra": {"a": {"b": {"c": {"d": {"e": {"f": 1}}}}}}},
    "wrapper inside wrapper inside wrapper": {"lead": {"lead": {"lead": {"lead": {"lead": {"lead": {}}}}}}},
    "address inside address": {"PostalAddress": {"PostalAddress": {"PostalAddress": {"City": "X"}}}},
    "1 MB string value": {**ANGI_ADS_EXAMPLE, "Description": "x" * 1_000_000},
    "1 MB email": {**ANGI_ADS_EXAMPLE, "Email": "x" * 1_000_000},
    "1 MB category": {**ANGI_ADS_EXAMPLE, "Category": "– " * 500_000},
    "1 MB address": {**ANGI_ADS_EXAMPLE, "PostalAddress": {"AddressFirstLine": " ,IN" * 250_000}},
    "1 MB unknown value": {"blob": "x" * 1_000_000},
    "1 MB key": {"k" * 1_000_000: "v"},
    "a thousand unknown keys": {**ANGI_LEADS_EXAMPLE, **{f"junk{n}": n for n in range(1000)}},
    "a thousand interview items": {**ANGI_LEADS_EXAMPLE, "interview": [None, {"question": 1}] * 500},
    "interview of nested lists": {**ANGI_LEADS_EXAMPLE, "interview": [[[[]]]]},
    "every value null": dict.fromkeys(ANGI_ADS_EXAMPLE),
    "every value an empty list": dict.fromkeys(ANGI_LEADS_EXAMPLE, []),
    "every value an empty object": dict.fromkeys(ANGI_ADS_EXAMPLE, {}),
    "every value a boolean": dict.fromkeys(ANGI_LEADS_EXAMPLE, True),
    "every value a huge integer": dict.fromkeys(ANGI_ADS_EXAMPLE, 10**400),
    "every value not-a-number": dict.fromkeys(ANGI_LEADS_EXAMPLE, float("nan")),
    "control characters everywhere": {key: "\x00\x01\x1f\r\n\t" for key in ANGI_ADS_EXAMPLE},
    "emoji everywhere": {key: "😀🏳️‍🌈👨‍👩‍👧‍👦" for key in ANGI_LEADS_EXAMPLE},
    "contact details under punctuation keys": {"@": "pat@example.com", "#": "5554332646"},
    "keys that normalise to nothing": {"___": "pat@example.com", "---": "5554332646", "": "x"},
    "dotted top-level key": {"PostalAddress.City": "Boston", "CorrelationId": "x", "ALAccountId": "1"},
}


@pytest.mark.parametrize("name", list(NASTY_PAYLOADS))
def test_hand_written_nasty_payloads_never_raise(name):
    # The inputs most likely to trip a parser: empty, punctuation, deep nesting, enormous values.
    payload = copy.deepcopy(NASTY_PAYLOADS[name])
    result = parse_angi_payload(payload)
    assert_well_formed(result)
    # The same input with an envelope is handled just as calmly.
    assert_well_formed(parse_angi_payload(payload, envelope=("array", "malformed-json")))


def test_nasty_payloads_without_contact_details_fail_cleanly():
    # Spot checks on the outcome, not only on "did not crash".
    for name in ("empty object", "empty key and value", "punctuation-only keys", "six levels of nesting", "1 MB unknown value"):
        result = parse_angi_payload(NASTY_PAYLOADS[name])
        assert (result.detected_format, result.mode, result.lead) == ("unknown", "failed", None), name
    buried = parse_angi_payload(NASTY_PAYLOADS["six levels around a lead"])
    assert buried.mode == "tolerant" and buried.lead.email_normalized == "bob.builder@gmail.com"
    huge = parse_angi_payload(NASTY_PAYLOADS["1 MB string value"])
    assert huge.mode == "strict" and len(huge.lead.comments) == 5000


# --- what the parse result means end to end --------------------------------------------------------


def test_account_ids_sent_as_numbers_still_route_to_the_mapped_tenant(client, ctx):
    # ALAccountId 123456 and spEntityId 12345678.0 are the same accounts as their string forms.
    ads_response = post_lead(client, ads_payload(ALAccountId=123456, Email="numeric.ads@example.com"))
    leads_response = post_lead(
        client, leads_payload(spEntityId=12345678.0, email="numeric.leads@example.com", primaryPhone="3035550111")
    )

    assert ads_response.json()["result"] == "processed"
    assert ads_response.json()["tenant"]["id"] == "tnt_sparkle"
    assert leads_response.json()["result"] == "processed"
    assert leads_response.json()["tenant"]["id"] == "tnt_summit"
    stored = db_one(ctx, "SELECT * FROM leads WHERE id = ?", (leads_response.json()["leadId"],))
    assert stored["external_account_id"] == "12345678"
    assert (stored["fee_cents"], stored["fee_source"]) == (2567, "payload")


def test_known_format_without_contact_details_is_stored_for_review_and_not_emailed(client, ctx, mailer, clock):
    # The tenant paid for the lead: keep it and flag it, but there is nobody to send an intro to.
    response = post_lead(client, ads_payload(Email="", PhoneNumber=""))

    assert response.status_code == 200
    body = response.json()
    assert (body["status"], body["result"]) == ("success", "processed")
    assert body["email"] == {"status": "skipped", "reason": "no_valid_email"}
    assert "no usable contact details (no valid email or phone)" in body["warnings"]
    stored = db_one(ctx, "SELECT * FROM leads WHERE id = ?", (body["leadId"],))
    assert stored["needs_review"] == 1
    assert (stored["email_normalized"], stored["phone_normalized"]) == (None, None)
    clock.advance(seconds=1)
    ctx.worker.deliver_due()
    assert mailer.sent == []


def test_unrecognised_payload_without_contact_details_is_quarantined_not_rejected(client, ctx):
    # Parse mode "failed": acknowledged to Angi (a retry cannot fix it), kept for a human, no lead created.
    response = post_lead(client, {"hello": "world", "order": {"id": 17}})

    assert response.status_code == 200
    body = response.json()
    assert (body["status"], body["result"], body["reason"]) == ("success", "quarantined", "unrecognised_payload")
    event = db_one(ctx, "SELECT * FROM webhook_events WHERE id = ?", (body["eventId"],))
    assert (event["status"], event["status_reason"], event["lead_id"]) == ("quarantined", "unrecognised_payload", None)
    assert db_one(ctx, "SELECT COUNT(*) AS n FROM leads")["n"] == 0


def test_absurdly_long_integer_does_not_raise():
    # Not reachable through json.loads (it has the same limit), but the parser promises never to raise.
    result = parse_angi_payload(ads(ALAccountId=10**5000))
    assert_well_formed(result)


def test_not_a_number_is_never_used_as_a_lead_id():
    # json.loads accepts NaN; every such lead would share the idempotency key "...:nan" and collapse into one.
    payload = json.loads(json.dumps(ANGI_ADS_EXAMPLE).replace('"61a7de56-dba3-4e59-8e2a-3fa827f84f7f"', "NaN"))
    result = parse_angi_payload(payload)
    assert result.lead.external_lead_id is None


def test_absurd_fee_does_not_cost_the_lead(client, ctx):
    # A junk value in a secondary field must be flagged, not turn the lead into a failed event.
    response = post_lead(client, leads_payload(fee=1e30))

    assert response.status_code == 200
    assert response.json()["result"] == "processed"
    assert db_one(ctx, "SELECT COUNT(*) AS n FROM leads")["n"] == 1


def test_half_an_emoji_in_free_text_does_not_cost_the_lead(client, ctx):
    # Encoders that truncate text mid-emoji emit "\ud83d"; that is valid JSON and must not lose the lead.
    body = json.dumps(ads_payload(Description="Deep clean please \ud83d"))
    assert body.isascii()  # the surrogate travels as a JSON escape

    response = post_lead(client, body)

    assert response.status_code == 200
    assert response.json()["result"] == "processed"
    assert db_one(ctx, "SELECT COUNT(*) AS n FROM leads")["n"] == 1
