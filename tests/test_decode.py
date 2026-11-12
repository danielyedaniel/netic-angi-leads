"""Request body -> JSON object: decode_body and salvage_json in app/ingest.py.

The body Angi posts is untrusted text. It is either decoded into one JSON object (with
a note of anything that had to be undone, the "envelope"), salvaged from almost-JSON,
or rejected with an error message. The example in Angi's own PDF spec is the main
salvage case, so it is checked end to end through the webhook as well.
"""

from __future__ import annotations

import json
import random
from typing import Any

import pytest

from app.angi.parser import parse_angi_payload
from app.ingest import decode_body, salvage_json
from tests.conftest import (
    ANGI_ADS_EXAMPLE,
    ANGI_LEADS_EXAMPLE,
    ads_payload,
    db_one,
    db_rows,
    leads_payload,
    post_lead,
)

# Copied verbatim from Angi's PDF spec. NOT valid JSON: curly quotes, an unquoted
# email, and no comma after the Description line.
SPEC_EXAMPLE_TEXT = """{
“FirstName”:”Bob”, “LastName”:”Builder”, “PhoneNumber”:”5554332646”, “PostalAddress”:{
“AddressFirstLine”:”123 Main St.”, “AddressSecondLine”:””, “City”:”Indianapolis”, “State”:”IN”, “PostalCode”:”46203”},
“Email”:bob.builder@gmail.com,
“Source”:”Angie’s List Quote Request”,
“Description”:”I’m Looking for recurring house cleaning services please.”
“Category”:”Indianapolis – House Cleaning”,
“Urgency”:”This Week”,
“CorrelationId”:”61a7de56-dba3-4e59-8e2a-3fa827f84f7f”,
“ALAccountId”:”123456”
}"""

ENVELOPES = {(), ("array",), ("malformed-json",)}


def assert_decoded_consistently(decoded: Any) -> None:
    """decode_body's contract: a JSON object and no error, or no object and an error."""
    assert isinstance(decoded, tuple) and len(decoded) == 3
    payload, envelope, error = decoded
    if payload is None:
        assert isinstance(error, str) and error
        assert envelope == ()
    else:
        assert isinstance(payload, dict)
        assert error is None
        assert envelope in ENVELOPES


# --- decode_body: valid JSON -------------------------------------------------------------------


def test_valid_object_is_returned_as_is():
    # The normal case: one JSON object, nothing to undo, nothing to report.
    raw = json.dumps(ANGI_ADS_EXAMPLE)
    assert decode_body(raw) == (ANGI_ADS_EXAMPLE, (), None)


def test_valid_object_keeps_nesting_types_and_unicode():
    # Decoding must not flatten or stringify anything.
    raw = json.dumps(ANGI_LEADS_EXAMPLE, ensure_ascii=False)
    payload, envelope, error = decode_body(raw)
    assert payload == ANGI_LEADS_EXAMPLE
    assert payload["fee"] == 25.67 and payload["leadOid"] == 987654321
    assert payload["primaryPhoneDetails"] == {"maskedNumber": False}
    assert (envelope, error) == ((), None)


@pytest.mark.parametrize("padding", ["\n", "  ", "\r\n\t "])
def test_whitespace_around_a_valid_object_is_ignored(padding):
    # A trailing newline is the most common harmless variation.
    assert decode_body(f'{padding}{{"a": 1}}{padding}') == ({"a": 1}, (), None)


def test_empty_object_is_still_an_object():
    # "{}" is valid JSON; whether it is a lead is the parser's decision, not the decoder's.
    assert decode_body("{}") == ({}, (), None)


def test_one_element_array_wrapping_an_object_is_unwrapped_and_reported():
    # Some senders batch; a batch of exactly one lead is unambiguous.
    raw = json.dumps([ANGI_ADS_EXAMPLE])
    assert decode_body(raw) == (ANGI_ADS_EXAMPLE, ("array",), None)


@pytest.mark.parametrize(
    ("raw", "kind"),
    [
        (json.dumps([ANGI_ADS_EXAMPLE, ANGI_ADS_EXAMPLE]), "array"),  # two leads: which one?
        ("[]", "array"),
        ("[1]", "array"),
        ('["text"]', "array"),
        ("[null]", "array"),
        (json.dumps([[ANGI_ADS_EXAMPLE]]), "array"),  # only one level of array is undone
        ('"just a string"', "string"),
        ('""', "string"),
        ("42", "number"),
        ("-1.5", "number"),
        ("0", "number"),
        ("null", "null"),
        ("true", "boolean"),
        ("false", "boolean"),
    ],
)
def test_valid_json_that_is_not_one_object_is_an_error(raw, kind):
    # Valid JSON of the wrong shape is rejected with a message naming what arrived.
    assert decode_body(raw) == (None, (), f"expected a JSON object, got {kind}")


@pytest.mark.parametrize("raw", ["", " ", "\n", "\t\r\n  ", " ", " \n"])
def test_empty_and_whitespace_only_body_is_an_error(raw):
    # Nothing was sent; say so plainly.
    assert decode_body(raw) == (None, (), "request body is empty")


@pytest.mark.parametrize(
    "raw",
    [
        "{{{{",
        "}",
        "{",
        "[",
        '{"a": ',
        "<lead><FirstName>Bob</FirstName></lead>",
        "FirstName=Bob&LastName=Builder&Email=bob%40example.com",
        "undefined",
        "NULL",
        '{"a": "1", "b": "2"',  # broken and too little to salvage
        "\x00\x01\x02",
        "😀",
    ],
)
def test_invalid_json_that_cannot_be_salvaged_is_an_error(raw):
    # Not JSON and nothing recognisable inside: reject, with the decoder's reason for the log.
    payload, envelope, error = decode_body(raw)
    assert payload is None
    assert envelope == ()
    assert error.startswith("request body is not valid JSON (")


# --- salvage: Angi's own spec example -------------------------------------------------------------


def test_the_spec_example_really_is_invalid_json():
    # Guard for the tests below: if this ever parsed, they would not be testing the salvage path.
    with pytest.raises(ValueError):
        json.loads(SPEC_EXAMPLE_TEXT)
    assert "“" in SPEC_EXAMPLE_TEXT and "”" in SPEC_EXAMPLE_TEXT and "’" in SPEC_EXAMPLE_TEXT
    assert "“Email”:bob.builder@gmail.com," in SPEC_EXAMPLE_TEXT  # the unquoted email
    assert "please.”\n“Category”" in SPEC_EXAMPLE_TEXT  # the missing comma


def test_the_spec_example_is_salvaged():
    # Angi's documentation example must be accepted even though it is not valid JSON.
    payload, envelope, error = decode_body(SPEC_EXAMPLE_TEXT)

    assert error is None
    assert envelope == ("malformed-json",)
    assert payload == {
        "FirstName": "Bob",
        "LastName": "Builder",
        "PhoneNumber": "5554332646",
        "AddressFirstLine": "123 Main St.",
        "AddressSecondLine": "",
        "City": "Indianapolis",
        "State": "IN",
        "PostalCode": "46203",
        "Email": "bob.builder@gmail.com",
        "Source": "Angie's List Quote Request",
        "Description": "I'm Looking for recurring house cleaning services please.",
        "Category": "Indianapolis – House Cleaning",
        "Urgency": "This Week",
        "CorrelationId": "61a7de56-dba3-4e59-8e2a-3fa827f84f7f",
        "ALAccountId": "123456",
    }


def test_the_salvaged_spec_example_parses_into_the_right_lead():
    # Salvaging loses the PostalAddress nesting; the tolerant parser must put it back together.
    payload, envelope, _ = decode_body(SPEC_EXAMPLE_TEXT)
    result = parse_angi_payload(payload, envelope=envelope)

    assert result.detected_format == "angi_ads"
    assert result.mode == "tolerant"
    lead = result.lead
    assert lead.email == "bob.builder@gmail.com"
    assert lead.email_normalized == "bob.builder@gmail.com"
    assert lead.first_name == "Bob"
    assert lead.last_name == "Builder"
    assert lead.external_account_id == "123456"
    assert lead.external_lead_id == "61a7de56-dba3-4e59-8e2a-3fa827f84f7f"
    assert lead.city == "Indianapolis"
    assert lead.state == "IN"
    assert lead.postal_code == "46203"
    assert lead.address_line1 == "123 Main St."
    assert lead.phone_normalized == "+15554332646"
    assert (lead.market, lead.task_name) == ("Indianapolis", "House Cleaning")
    assert lead.lead_source_detail == "Angie's List Quote Request"
    assert lead.comments == "I'm Looking for recurring house cleaning services please."
    # Apart from the nesting, it is the same lead the well-formed example gives.
    assert lead.model_dump() == parse_angi_payload(ANGI_ADS_EXAMPLE).lead.model_dump()
    # ... and the repair is reported, so a human hears that Angi is sending broken JSON.
    assert "<malformed-json>" in result.drift.unknown
    assert not result.is_clean
    assert result.issues == []


def test_the_spec_example_posted_to_the_webhook_becomes_a_lead_and_an_intro_email(client, ctx, mailer, clock):
    # End to end: the documentation example, byte for byte, must produce a lead and reach the consumer.
    response = post_lead(client, SPEC_EXAMPLE_TEXT.encode("utf-8"))

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["result"] == "processed"
    assert body["format"] == "angi_ads"
    assert body["parseMode"] == "tolerant"
    assert body["tenant"]["id"] == "tnt_sparkle"  # ALAccountId 123456
    assert body["email"]["status"] == "queued"
    assert body["email"]["to"] == "bob.builder@gmail.com"

    lead = db_one(ctx, "SELECT * FROM leads WHERE id = ?", (body["leadId"],))
    assert lead["tenant_id"] == "tnt_sparkle"
    assert lead["first_name"] == "Bob"
    assert lead["last_name"] == "Builder"
    assert lead["email_normalized"] == "bob.builder@gmail.com"
    assert lead["phone_normalized"] == "+15554332646"
    assert lead["external_account_id"] == "123456"
    assert lead["external_lead_id"] == "61a7de56-dba3-4e59-8e2a-3fa827f84f7f"
    assert (lead["address_line1"], lead["city"], lead["state"], lead["postal_code"]) == (
        "123 Main St.",
        "Indianapolis",
        "IN",
        "46203",
    )
    assert (lead["market"], lead["task_name"]) == ("Indianapolis", "House Cleaning")
    assert lead["parse_mode"] == "tolerant"
    assert lead["needs_review"] == 1  # fields were recovered by alias, so a human should glance at it

    # The request is stored exactly as sent, for replay and forensics.
    event = db_one(ctx, "SELECT * FROM webhook_events WHERE id = ?", (body["eventId"],))
    assert event["raw_body"] == SPEC_EXAMPLE_TEXT
    assert event["status"] == "processed"
    assert event["lead_id"] == body["leadId"]

    # The repair is surfaced as drift, not swallowed.
    assert any("<malformed-json>" in warning for warning in body["warnings"])
    drift_alerts = db_rows(ctx, "SELECT * FROM alerts WHERE kind = 'schema_drift'")
    assert len(drift_alerts) == 1

    # The intro email goes out on the next worker pass.
    assert mailer.sent == []
    clock.advance(seconds=1)
    ctx.worker.deliver_due()
    assert len(mailer.sent) == 1
    email = mailer.sent[0]
    assert email.to == "bob.builder@gmail.com"
    assert email.from_name == "Sparkle Home Cleaning"
    assert "House Cleaning" in email.subject
    assert f"/book/{lead['booking_token']}" in email.text
    intro = db_one(ctx, "SELECT * FROM outbox_messages WHERE lead_id = ? AND sequence = 0", (lead["id"],))
    assert (intro["template"], intro["status"]) == ("intro", "sent")


def test_the_spec_example_posted_twice_is_one_lead_and_one_intro(client, ctx, mailer, clock):
    # Angi retries; a salvaged body must be just as idempotent as a clean one.
    first = post_lead(client, SPEC_EXAMPLE_TEXT.encode("utf-8"))
    second = post_lead(client, SPEC_EXAMPLE_TEXT.encode("utf-8"))

    assert (first.status_code, second.status_code) == (200, 200)
    assert first.json()["result"] == "processed"
    assert second.json()["status"] == "success"
    assert second.json()["result"] == "duplicate_delivery"
    assert second.json()["leadId"] == first.json()["leadId"]
    assert db_one(ctx, "SELECT COUNT(*) AS n FROM leads")["n"] == 1

    clock.advance(seconds=1)
    ctx.worker.deliver_due()
    assert [email.to for email in mailer.sent] == ["bob.builder@gmail.com"]


def test_salvaged_and_well_formed_delivery_of_the_same_lead_are_one_lead(client, ctx):
    # The lead id decides identity, not the byte-level shape of the request.
    well_formed = post_lead(client, ANGI_ADS_EXAMPLE)
    salvaged = post_lead(client, SPEC_EXAMPLE_TEXT.encode("utf-8"))

    assert well_formed.json()["result"] == "processed"
    assert salvaged.json()["status"] == "success"
    assert salvaged.json()["leadId"] == well_formed.json()["leadId"]
    assert db_one(ctx, "SELECT COUNT(*) AS n FROM leads")["n"] == 1


# --- salvage: edge cases ----------------------------------------------------------------------------


def test_curly_quotes_alone_are_repaired_without_losing_nesting():
    # When straightening the quotes is enough, the result is the real JSON object, nested and typed.
    raw = '{“FirstName”: “Bob”, “PostalAddress”: {“City”: “Indianapolis”}, “Count”: 2, “Tags”: [“a”, “b”]}'
    expected = {"FirstName": "Bob", "PostalAddress": {"City": "Indianapolis"}, "Count": 2, "Tags": ["a", "b"]}

    assert salvage_json(raw) == expected
    assert decode_body(raw) == (expected, ("malformed-json",), None)


def test_typographic_apostrophe_survives_a_structural_repair():
    # A body that only needed its trailing comma removed is read exactly like the valid
    # version of itself: the text is kept as the consumer wrote it.
    payload = salvage_json('{"Source": "Angie’s List", "Description": "I’m here", "Urgency": "Now",}')
    assert payload == {"Source": "Angie’s List", "Description": "I’m here", "Urgency": "Now"}


def test_typographic_apostrophe_becomes_a_plain_one_in_the_pair_by_pair_fallback():
    # "Angie’s" in the spec example (which also lacks a comma, so only the fallback can read it).
    payload = salvage_json('{"Source": "Angie’s List" "Description": "I’m here", "Urgency": "Now"}')
    assert payload == {"Source": "Angie's List", "Description": "I'm here", "Urgency": "Now"}


def test_escaped_quotes_inside_a_value_do_not_end_the_value():
    # An escaped quote is part of the text; the pairs after it must still be found.
    raw = '{"Description": "He said \\"call me\\" twice", "Email": "bob@example.com", "FirstName": "Bob",}'
    payload = salvage_json(raw)

    assert set(payload) == {"Description", "Email", "FirstName"}
    assert payload["Description"].startswith("He said ") and payload["Description"].endswith("twice")
    assert "call me" in payload["Description"]
    assert payload["Email"] == "bob@example.com"
    assert payload["FirstName"] == "Bob"


def test_escaped_characters_in_a_salvaged_value_are_decoded():
    # The consumer wrote: He said "call me" - not: He said \"call me\".
    raw = (
        '{"Description": "He said \\"call me\\"\\nafter 5", "Category": "Indianapolis \\u2013 House Cleaning", '
        '"Url": "https:\\/\\/example.com\\/a", "FirstName": "Bob",}'
    )
    payload = salvage_json(raw)

    assert payload["Description"] == 'He said "call me"\nafter 5'
    assert payload["Category"] == "Indianapolis – House Cleaning"
    assert payload["Url"] == "https://example.com/a"


@pytest.mark.parametrize(
    "description",
    [
        "Call at 10:30, or 11:00, thanks",
        "Leak: under the sink, behind the wall",
        "a,b,c",
        "key: value",
        "ends with a colon:",
        ", starts with a comma",
        "braces } and ] and [ and {",
    ],
)
def test_colons_commas_and_brackets_inside_a_quoted_value_are_kept(description):
    # Free text is full of punctuation that also means something in JSON.
    raw = '{"Description": ' + json.dumps(description) + ', "Email": "bob@example.com", "FirstName": "Bob",}'
    payload = salvage_json(raw)

    assert payload == {"Description": description, "Email": "bob@example.com", "FirstName": "Bob"}


def test_json_looking_text_inside_a_value_is_not_mistaken_for_fields():
    # A consumer can type anything; '"Email": "x"' inside their message is not the lead's email.
    message = 'My old quote said "Email": "other@example.com", "Phone": "5550000000" - ignore it'
    raw = '{"Description": ' + json.dumps(message) + ', "Email": "bob@example.com", "FirstName": "Bob",}'
    payload = salvage_json(raw)

    assert set(payload) == {"Description", "Email", "FirstName"}
    assert payload["Email"] == "bob@example.com"


def test_bare_true_false_and_null_keep_their_json_meaning():
    # An unquoted false must stay False: as the string "false" it could be read as consent.
    raw = '{"maskedNumber": true, "automatedContactCompliant": false, "secondaryPhone": null, "name": "Jane",}'
    payload = salvage_json(raw)

    assert payload == {"maskedNumber": True, "automatedContactCompliant": False, "secondaryPhone": None, "name": "Jane"}


def test_bare_numbers_are_recovered_as_text_the_parser_understands():
    # The pair-by-pair fallback does not guess number types; ids and money are accepted as
    # strings downstream. (The unquoted n/a is what makes the body unrepairable as a whole.)
    raw = '{"leadOid": 987654321, "srOid": 123456789, "fee": 25.67, "spEntityId": 12345678, "balance": -3, "note": n/a}'
    payload = salvage_json(raw)
    assert payload == {
        "leadOid": "987654321",
        "srOid": "123456789",
        "fee": "25.67",
        "spEntityId": "12345678",
        "balance": "-3",
        "note": "n/a",
    }

    result = parse_angi_payload(payload, envelope=("malformed-json",))
    assert result.detected_format == "angi_leads"
    assert result.lead.external_lead_id == "987654321"
    assert result.lead.external_request_id == "123456789"
    assert result.lead.external_account_id == "12345678"
    assert result.lead.fee_cents == 2567


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"a": "1", "b": "2", "Email": bob@example.com}', "bob@example.com"),
        ('{"a": "1", "b": "2", "Email": bob@example.com\n}', "bob@example.com"),
        ('{"Email": bob@example.com , "a": "1", "b": "2"}', "bob@example.com"),
        ('{"Email":bob@example.com,"a":"1","b":"2"}', "bob@example.com"),
        ('{"Email": bob@example.com\r\n"a": "1", "b": "2"}', "bob@example.com"),
    ],
)
def test_unquoted_value_ends_at_the_comma_brace_or_line_end(raw, expected):
    # The spec example's unquoted email, in each position it could appear.
    assert salvage_json(raw)["Email"] == expected


def test_unquoted_value_may_contain_spaces():
    # A sender that forgets quotes forgets them for multi-word values too.
    payload = salvage_json('{"Source": Angi Quote Request, "Urgency": This Week, "FirstName": "Bob"}')
    assert payload == {"Source": "Angi Quote Request", "Urgency": "This Week", "FirstName": "Bob"}


def test_trailing_comma_is_tolerated():
    # The most common hand-made JSON mistake.
    assert salvage_json('{"a": "1", "b": "2", "c": "3",}') == {"a": "1", "b": "2", "c": "3"}


def test_truncated_body_gives_the_complete_pairs_and_drops_the_cut_one():
    # A request cut off mid-value still tells us who the consumer is; half an email is not an email.
    raw = '{"FirstName":"Bob","LastName":"Builder","PhoneNumber":"5554332646","Email":"bob@exam'
    assert salvage_json(raw) == {"FirstName": "Bob", "LastName": "Builder", "PhoneNumber": "5554332646"}


@pytest.mark.parametrize(
    "raw",
    [
        '{"a": "1" "b": "2"}',  # two pairs
        '{"a": "1" oops}',  # one pair
        '{"a": "1" "a": "2" "a": "3"}',  # three pairs, one key
        "{,,}",
    ],
)
def test_fewer_than_three_pairs_is_not_salvaged(raw):
    # When the body cannot be repaired as a whole and has to be read pair by pair, two stray
    # "key": value matches are not enough evidence that this was meant to be a lead.
    assert salvage_json(raw) is None
    payload, envelope, error = decode_body(raw)
    assert payload is None and envelope == ()
    assert error.startswith("request body is not valid JSON (")


def test_exactly_three_pairs_is_salvaged():
    # The threshold itself.
    assert salvage_json('{"a": "1", "b": "2", "c": "3",}') is not None
    assert decode_body('{"a": "1", "b": "2", "c": "3",}') == ({"a": "1", "b": "2", "c": "3"}, ("malformed-json",), None)


@pytest.mark.parametrize(
    "raw",
    [
        "Hello Angi, this is not a lead at all.",
        "Please call Bob at 555-433-2646: he asked twice, yesterday and today, about cleaning.",
        'He said "hello", then "goodbye", then "wait": nothing else.',
        "name: Bob, email: bob@example.com, phone: 5554332646",  # looks like data, but has no quoted keys
        "<html><body>502 Bad Gateway</body></html>",
        "FirstName,LastName,Email\nBob,Builder,bob@example.com",
    ],
)
def test_plain_prose_is_not_salvaged(raw):
    # Text that merely contains colons and commas must not be turned into a lead.
    assert salvage_json(raw) is None
    assert decode_body(raw)[0] is None


# --- repairs that keep the structure come before the pair-by-pair fallback -----------------------------


def test_trailing_comma_is_repaired_without_losing_nesting_types_or_lists():
    # One stray comma must not cost the nested objects, the number types or the interview
    # answers, all of which the pair-by-pair fallback would lose.
    raw = json.dumps(ANGI_LEADS_EXAMPLE)[:-1] + ",}"

    payload, envelope, error = decode_body(raw)

    assert payload == ANGI_LEADS_EXAMPLE
    assert (envelope, error) == (("malformed-json",), None)  # repaired, and still reported
    result = parse_angi_payload(payload, envelope=envelope)
    assert result.mode == "strict"
    assert result.lead == parse_angi_payload(ANGI_LEADS_EXAMPLE).lead
    assert len(result.lead.interview) == 2
    assert result.drift.unknown == ["<malformed-json>"]
    assert not result.is_clean


def test_trailing_comma_inside_a_nested_object_and_a_list_is_repaired():
    # The same slip anywhere in the body.
    raw = '{"FirstName": "Bob", "PostalAddress": {"City": "Indianapolis",}, "Tags": ["a", "b",],}'

    assert decode_body(raw) == (
        {"FirstName": "Bob", "PostalAddress": {"City": "Indianapolis"}, "Tags": ["a", "b"]},
        ("malformed-json",),
        None,
    )


@pytest.mark.parametrize("whitespace", ["\n", "\r\n", "\t"], ids=["newline", "crlf", "tab"])
def test_raw_line_break_or_tab_inside_a_string_is_repaired_and_kept(whitespace):
    # A sender that forgets to escape a line break in the consumer's message has still sent
    # every field. The text is kept as written, line break included.
    payload = {**ANGI_ADS_EXAMPLE, "Description": f"Kitchen first.{whitespace}Then the bathrooms."}
    raw = json.dumps(payload).replace(json.dumps(whitespace)[1:-1], whitespace)  # un-escape it
    with pytest.raises(json.JSONDecodeError):
        json.loads(raw)

    decoded, envelope, error = decode_body(raw)

    assert decoded == payload
    assert (envelope, error) == (("malformed-json",), None)
    result = parse_angi_payload(decoded, envelope=envelope)
    assert result.mode == "strict"
    assert result.lead.comments == f"Kitchen first.{whitespace}Then the bathrooms."
    assert result.lead.city == "Indianapolis"  # nesting intact


def test_repaired_body_is_processed_like_the_clean_one_and_reported_as_drift(client, ctx):
    # End to end: the lead is complete and needs no review; the malformed body is still alerted on.
    raw = json.dumps(leads_payload())[:-1] + ",}"

    body = post_lead(client, raw).json()

    assert body["result"] == "processed"
    assert body["parseMode"] == "strict"
    lead = db_one(ctx, "SELECT * FROM leads")
    assert len(json.loads(lead["interview_json"])) == 2
    assert lead["needs_review"] == 0
    alert = db_one(ctx, "SELECT * FROM alerts")
    assert alert["kind"] == "schema_drift"
    assert "<malformed-json>" in alert["title"]


def test_free_text_that_looks_like_fields_cannot_override_them_in_a_repairable_body():
    # In a body that only needed a structural repair, '"ALAccountId": ...' typed into the
    # description is just text; the pair-by-pair fallback would have had to refuse such a body.
    message = 'ignore "ALAccountId": "200001", "CorrelationId": "someone-elses-lead"'
    raw = json.dumps({**ANGI_ADS_EXAMPLE, "Description": message})[:-1] + ",}"

    payload, envelope, error = decode_body(raw)

    assert error is None
    assert payload["ALAccountId"] == ANGI_ADS_EXAMPLE["ALAccountId"]
    assert payload["CorrelationId"] == ANGI_ADS_EXAMPLE["CorrelationId"]
    assert payload["Description"] == message


def test_salvaged_angi_leads_body_still_gives_a_usable_lead():
    # The second feed with a missing comma, which only the pair-by-pair fallback can read:
    # contact details, ids, fee and flags must survive. (The interview answers do not: the
    # fallback cannot rebuild a list, so it leaves "question" / "answer" pairs out.)
    example = {**ANGI_LEADS_EXAMPLE, "interview": ANGI_LEADS_EXAMPLE["interview"][:1]}
    raw = json.dumps(example).replace(', "firstName"', ' "firstName"', 1)
    payload, envelope, error = decode_body(raw)
    assert (envelope, error) == (("malformed-json",), None)

    result = parse_angi_payload(payload, envelope=envelope)
    expected = parse_angi_payload(ANGI_LEADS_EXAMPLE).lead
    assert result.detected_format == "angi_leads"
    assert result.mode == "tolerant"
    lead = result.lead
    assert lead.email_normalized == expected.email_normalized
    assert lead.phone_normalized == expected.phone_normalized
    assert (lead.first_name, lead.last_name, lead.full_name) == ("Jane", "Doe", "Jane Doe")
    assert (lead.external_lead_id, lead.external_request_id, lead.external_account_id) == (
        "987654321",
        "123456789",
        "12345678",
    )
    assert lead.fee_cents == 2567
    assert lead.phone_is_masked is False
    assert lead.automated_contact_ok is True
    assert (lead.city, lead.state, lead.postal_code) == ("Denver", "CO", "80210")


def test_byte_order_mark_before_valid_json_is_ignored():
    # Some senders (notably .NET) prefix UTF-8 with a BOM; the JSON after it is perfectly valid.
    raw = "﻿" + json.dumps(ANGI_LEADS_EXAMPLE)
    assert decode_body(raw) == (ANGI_LEADS_EXAMPLE, (), None)


def test_two_concatenated_objects_are_never_merged_into_one_lead():
    # Two leads in one body (newline-delimited JSON) must not become one lead with mixed-up contact details.
    ann = {"CorrelationId": "lead-A", "FirstName": "Ann", "PhoneNumber": "5554332646", "ALAccountId": "123456"}
    ben = {"CorrelationId": "lead-B", "FirstName": "Ben", "Email": "ben@example.com", "ALAccountId": "123456"}
    payload, _, _ = decode_body(json.dumps(ann) + "\n" + json.dumps(ben))

    mixed = payload is not None and payload.get("FirstName") == "Ann" and payload.get("Email") == "ben@example.com"
    assert not mixed


def test_deeply_nested_body_is_an_error_not_an_exception():
    # 200 KB of "[" is inside the body size limit; it must be rejected like any other non-lead.
    raw = "[" * 100_000 + "]" * 100_000
    payload, envelope, error = decode_body(raw)
    assert payload is None and envelope == () and error


# --- robustness ---------------------------------------------------------------------------------------


def test_adversarial_text_is_handled_without_error():
    # Inputs built to stress the salvage pattern (unterminated strings, runs of backslashes, huge bare values).
    quarter_megabyte = 250_000
    bodies = [
        '"' + "a" * quarter_megabyte,
        '"a":"' + "\\" * (quarter_megabyte + 1),
        '"a":"' + "b" * quarter_megabyte,
        '"a": ' + "x" * quarter_megabyte,
        ('"' + "k" * 61) * 4000,
        '{"a":' * 500 + "1" + "}" * 500,
        "{" + ",".join(f'"k{n}": "v{n}"' for n in range(5000)) + ",}",
        "“" * quarter_megabyte,
        '{"a": ' + "9" * 5000 + ', "b": "x", "c": "y", "d": "z"}',  # an integer too long for Python to convert
        ":" * quarter_megabyte,
        '":' * 100_000,
    ]
    for raw in bodies:
        assert_decoded_consistently(decode_body(raw))


def test_mutated_bodies_never_raise_and_always_decode_consistently():
    # Random damage to real bodies: every outcome is "an object" or "an error", and the parser copes with the object.
    rng = random.Random(20261112)
    sources = [
        SPEC_EXAMPLE_TEXT,
        json.dumps(ANGI_ADS_EXAMPLE),
        json.dumps(ANGI_LEADS_EXAMPLE, indent=2),
        json.dumps([ANGI_ADS_EXAMPLE]),
    ]
    noise = list('{}[]",:\\ \n\t“”‘’–\x00') + ["😀", "null", "true", '"x":', ",,", "\\u00", "﻿"]
    outcomes = {"object": 0, "error": 0}
    for _ in range(3000):
        text = rng.choice(sources)
        for _ in range(rng.randint(1, 6)):
            position = rng.randrange(len(text) + 1)
            action = rng.choice(["delete", "insert", "truncate", "duplicate"])
            if action == "delete":
                text = text[:position] + text[position + rng.randint(1, 20) :]
            elif action == "insert":
                text = text[:position] + rng.choice(noise) + text[position:]
            elif action == "truncate" and position > 10:
                text = text[:position]
            elif action == "duplicate":
                text = text[:position] + text[position : position + 40] + text[position:]

        decoded = decode_body(text)

        assert_decoded_consistently(decoded)
        payload, envelope, _ = decoded
        outcomes["object" if payload is not None else "error"] += 1
        if payload is not None:
            result = parse_angi_payload(payload, envelope=envelope)
            assert result.mode in {"strict", "tolerant", "failed"}
            assert (result.lead is None) == (result.mode == "failed")
    assert outcomes["object"] > 300 and outcomes["error"] > 30, outcomes  # both paths were exercised


# --- the webhook's answer for each kind of body --------------------------------------------------------


def test_array_wrapped_lead_is_processed_and_the_wrapper_reported(client, ctx):
    # A batch of one is a lead; the unexpected array is surfaced as drift, not hidden.
    payload = ads_payload(Email="arrayed@example.com")
    response = post_lead(client, json.dumps([payload]))

    assert response.status_code == 200
    body = response.json()
    assert (body["status"], body["result"]) == ("success", "processed")
    assert body["parseMode"] == "strict"
    assert any("<array>" in warning for warning in body["warnings"])
    lead = db_one(ctx, "SELECT * FROM leads WHERE id = ?", (body["leadId"],))
    assert lead["email_normalized"] == "arrayed@example.com"
    assert lead["external_lead_id"] == payload["CorrelationId"]


@pytest.mark.parametrize(
    ("raw", "error_start"),
    [
        pytest.param(b"", "request body is empty", id="empty"),
        pytest.param(b"  \r\n ", "request body is empty", id="whitespace"),
        pytest.param(b"[]", "expected a JSON object, got array", id="empty-array"),
        pytest.param(
            json.dumps([ANGI_ADS_EXAMPLE, ANGI_ADS_EXAMPLE]).encode(),
            "expected a JSON object, got array",
            id="two-element-array",
        ),
        pytest.param(b'"a lead, honest"', "expected a JSON object, got string", id="string"),
        pytest.param(b"42", "expected a JSON object, got number", id="number"),
        pytest.param(b"null", "expected a JSON object, got null", id="null"),
        pytest.param(b"Hello Angi, this is not a lead at all.", "request body is not valid JSON", id="prose"),
        pytest.param(b'{"FirstName": "Bob", "LastName": ', "request body is not valid JSON", id="cut-off"),
    ],
)
def test_body_that_is_not_a_json_object_is_answered_with_400_and_kept(client, ctx, raw, error_start):
    # The one case answered with an error, so Angi's own retry / failure email kicks in; the bytes are kept for forensics.
    response = post_lead(client, raw)

    assert response.status_code == 400
    body = response.json()
    assert body["status"] == "error"
    assert body["result"] == "rejected"
    assert body["error"].startswith(error_start)
    event = db_one(ctx, "SELECT * FROM webhook_events WHERE id = ?", (body["eventId"],))
    assert event["status"] == "rejected"
    assert event["raw_body"] == raw.decode("utf-8")
    assert event["lead_id"] is None
    assert db_one(ctx, "SELECT COUNT(*) AS n FROM leads")["n"] == 0
    assert db_one(ctx, "SELECT COUNT(*) AS n FROM outbox_messages")["n"] == 0
