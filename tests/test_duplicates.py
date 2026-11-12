"""Duplicate-lead detection (app/duplicates.py), exercised through the webhook.

A "duplicate" here is the same consumer arriving as a NEW, separately billed lead for the
same tenant and Angi account inside Angi's credit window. It is not the same thing as Angi
redelivering one lead (same lead id), which idempotency handles and which must never
produce a claim.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from app.angi.parser import parse_angi_payload
from app.duplicates import find_duplicate
from app.tenants import SOURCE_ANGI, add_mapping
from app.util import iso, parse_iso
from tests.conftest import (
    ADMIN,
    BLUELINE_ACCOUNT,
    CANARY_ACCOUNT,
    SUMMIT_ADS_ACCOUNT,
    ads_payload,
    db_one,
    db_rows,
    leads_payload,
    post_lead,
)

BLUELINE_LEADS_ACCOUNT = 567890123  # spEntityId -> tnt_blueline (see app/seed.py)


def send(client, payload) -> dict:
    """Post a lead and return the webhook's JSON body; every stored lead is a success."""
    response = post_lead(client, payload)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "success"
    return body


def lead_row(ctx, lead_id: str) -> dict:
    return db_one(ctx, "SELECT * FROM leads WHERE id = ?", (lead_id,))


def claim_for(ctx, lead_id: str) -> dict | None:
    return db_one(ctx, "SELECT * FROM credit_claims WHERE lead_id = ?", (lead_id,))


def matched_on(ctx, lead_id: str) -> list[str]:
    return json.loads(lead_row(ctx, lead_id)["duplicate_matched_on"])


def templates(ctx, lead_id: str) -> list[str]:
    """What is queued for a lead: the first touch, then any follow-ups."""
    rows = db_rows(ctx, "SELECT template FROM outbox_messages WHERE lead_id = ? ORDER BY sequence", (lead_id,))
    return [row["template"] for row in rows]


def tenant_url(ctx, tenant_id: str) -> str:
    key = db_one(ctx, "SELECT webhook_key FROM tenants WHERE id = ?", (tenant_id,))["webhook_key"]
    return f"/webhooks/angi/leads/{key}"


def boom(*args, **kwargs):
    raise RuntimeError("boom: injected failure")


# --- what counts as the same consumer ---------------------------------------------------


def test_second_lead_from_same_consumer_is_flagged_as_duplicate_of_the_first(client, ctx, clock):
    # Same phone and email under a new CorrelationId = a second, separately billed lead.
    first = send(client, ads_payload())
    clock.advance(hours=2)
    second = send(client, ads_payload())

    assert first["duplicate"] is None
    assert lead_row(ctx, first["leadId"])["is_duplicate"] == 0
    duplicate = lead_row(ctx, second["leadId"])
    assert duplicate["is_duplicate"] == 1
    assert duplicate["duplicate_of_lead_id"] == first["leadId"]
    assert matched_on(ctx, second["leadId"]) == ["phone", "email"]
    assert db_one(ctx, "SELECT COUNT(*) AS n FROM leads")["n"] == 2


def test_duplicate_matched_on_phone_only(client, ctx, clock):
    # A different email does not hide a repeat caller: the phone alone is enough.
    first = send(client, ads_payload(Email="first@example.com"))
    clock.advance(minutes=5)
    second = send(client, ads_payload(Email="second@example.com"))

    assert lead_row(ctx, second["leadId"])["duplicate_of_lead_id"] == first["leadId"]
    assert matched_on(ctx, second["leadId"]) == ["phone"]


def test_duplicate_matched_on_email_only(client, ctx, clock):
    # A different phone does not hide a repeat requester: the email alone is enough.
    first = send(client, ads_payload(PhoneNumber="3175550101"))
    clock.advance(minutes=5)
    second = send(client, ads_payload(PhoneNumber="3175550102"))

    assert lead_row(ctx, second["leadId"])["duplicate_of_lead_id"] == first["leadId"]
    assert matched_on(ctx, second["leadId"]) == ["email"]


@pytest.mark.parametrize(
    "phone",
    [
        "(555) 433-2646",
        "555-433-2646",
        "555.433.2646",
        "+1 555 433 2646",
        "1-555-433-2646",
        " 555 433 2646 ",
        5554332646,  # sent as a JSON number
    ],
)
def test_phone_match_is_tolerant_of_formatting(client, ctx, clock, phone):
    # Phones are compared as E.164, so punctuation, a country code or a JSON number do not matter.
    first = send(client, ads_payload(PhoneNumber="5554332646", Email="first@example.com"))
    clock.advance(minutes=5)
    second = send(client, ads_payload(PhoneNumber=phone, Email="second@example.com"))

    assert lead_row(ctx, second["leadId"])["duplicate_of_lead_id"] == first["leadId"]
    assert matched_on(ctx, second["leadId"]) == ["phone"]


@pytest.mark.parametrize("email", ["BOB.BUILDER@GMAIL.COM", "  bob.builder@gmail.com  ", "Bob.Builder@Gmail.Com\n"])
def test_email_match_ignores_case_and_surrounding_whitespace(client, ctx, clock, email):
    # Emails are compared lower-cased and trimmed.
    first = send(client, ads_payload(PhoneNumber="3175550101"))
    clock.advance(minutes=5)
    second = send(client, ads_payload(PhoneNumber="3175550102", Email=email))

    assert lead_row(ctx, second["leadId"])["duplicate_of_lead_id"] == first["leadId"]
    assert matched_on(ctx, second["leadId"]) == ["email"]


def test_different_consumer_is_not_a_duplicate(client, ctx, clock):
    # Two different people asking the same business for the same service are two real leads.
    send(client, ads_payload(PhoneNumber="3175550101", Email="one@example.com"))
    clock.advance(minutes=5)
    second = send(client, ads_payload(PhoneNumber="3175550102", Email="two@example.com"))

    assert second["duplicate"] is None
    assert lead_row(ctx, second["leadId"])["is_duplicate"] == 0
    assert db_rows(ctx, "SELECT id FROM credit_claims") == []


def test_same_service_request_with_new_lead_id_matches_on_service_request(client, ctx, clock):
    # Angi Leads: one srOid sold to the same pro twice (new leadOid) is the same request.
    first = send(client, leads_payload(srOid=4242, primaryPhone="3035550101", email="a@example.com"))
    clock.advance(hours=1)
    second = send(
        client,
        leads_payload(srOid=4242, primaryPhone="3035550102", email="b@example.com", name="Someone Else"),
    )

    assert lead_row(ctx, second["leadId"])["duplicate_of_lead_id"] == first["leadId"]
    assert matched_on(ctx, second["leadId"]) == ["service_request"]
    assert second["duplicate"]["confidence"] == "high"


def test_service_request_id_matches_whether_sent_as_number_or_string(client, ctx, clock):
    # Angi documents srOid as a number but ids arrive as strings too; both must compare equal.
    first = send(client, leads_payload(srOid=777001, primaryPhone="3035550101", email="a@example.com"))
    clock.advance(minutes=1)
    second = send(client, leads_payload(srOid="777001", primaryPhone="3035550102", email="b@example.com"))

    assert lead_row(ctx, second["leadId"])["duplicate_of_lead_id"] == first["leadId"]
    assert "service_request" in matched_on(ctx, second["leadId"])


def test_all_three_signals_are_reported_when_they_all_match(client, ctx, clock):
    # matched_on lists every signal that agreed, in a stable order, for the evidence pack.
    first = send(client, leads_payload(srOid=5151))
    clock.advance(minutes=1)
    second = send(client, leads_payload(srOid=5151))

    assert matched_on(ctx, second["leadId"]) == ["phone", "email", "service_request"]
    assert second["duplicate"]["matchedOn"] == ["phone", "email", "service_request"]
    assert lead_row(ctx, second["leadId"])["duplicate_of_lead_id"] == first["leadId"]


def test_same_consumer_for_a_different_tenant_is_not_a_duplicate(client, ctx, clock):
    # Each business paid for its own lead; a consumer contacting two businesses is not a credit case.
    sparkle = send(client, ads_payload())
    clock.advance(minutes=5)
    blueline = send(client, ads_payload(ALAccountId=BLUELINE_ACCOUNT))

    assert sparkle["tenant"]["id"] == "tnt_sparkle"
    assert blueline["tenant"]["id"] == "tnt_blueline"
    assert blueline["duplicate"] is None
    assert lead_row(ctx, blueline["leadId"])["is_duplicate"] == 0
    assert db_rows(ctx, "SELECT id FROM credit_claims") == []


def test_duplicate_points_at_the_same_tenants_lead_not_another_tenants(client, ctx, clock):
    # With leads for two tenants in between, the match is still the earlier lead of the SAME tenant.
    sparkle_first = send(client, ads_payload())
    clock.advance(minutes=5)
    send(client, ads_payload(ALAccountId=BLUELINE_ACCOUNT))
    clock.advance(minutes=5)
    sparkle_second = send(client, ads_payload())

    assert lead_row(ctx, sparkle_second["leadId"])["duplicate_of_lead_id"] == sparkle_first["leadId"]


def test_same_service_request_for_a_different_tenant_is_not_a_duplicate(client, ctx, clock):
    # Angi sells one service request to several pros; that is normal, not a duplicate.
    send(client, leads_payload(srOid=9090))
    clock.advance(minutes=5)
    other = send(client, leads_payload(srOid=9090, spEntityId=BLUELINE_LEADS_ACCOUNT))

    assert other["tenant"]["id"] == "tnt_blueline"
    assert other["duplicate"] is None


def test_same_consumer_through_two_angi_accounts_of_one_tenant_is_not_a_duplicate(client, ctx, clock):
    # A tenant with two Angi accounts (here one per feed) is charged by each account in its
    # own right: Angi's "charged twice" rule is per account, so there is nothing to claim.
    first = send(client, ads_payload(ALAccountId=SUMMIT_ADS_ACCOUNT, PhoneNumber="3035550123"))
    clock.advance(hours=3)
    second = send(client, leads_payload(email="someone.else@example.com"))  # primaryPhone 3035550123

    assert first["tenant"]["id"] == second["tenant"]["id"] == "tnt_summit"
    assert lead_row(ctx, first["leadId"])["external_account_id"] != lead_row(ctx, second["leadId"])["external_account_id"]
    assert second["duplicate"] is None
    assert lead_row(ctx, second["leadId"])["is_duplicate"] == 0
    assert db_rows(ctx, "SELECT id FROM credit_claims") == []


def test_same_service_request_bought_by_two_accounts_of_one_tenant_is_not_a_duplicate(client, ctx, clock):
    # The strongest signal there is (same srOid, phone and email) still is not a double
    # charge when two different accounts each bought the lead once.
    with ctx.db.transaction() as conn:
        add_mapping(
            conn, tenant_id="tnt_summit", source=SOURCE_ANGI, external_account_id="12345679",
            external_account_name="Summit Heating & Air (Boulder)", now=clock(),
        )
    send(client, leads_payload(srOid=8181))
    clock.advance(minutes=5)
    second = send(client, leads_payload(srOid=8181, spEntityId=12345679))

    assert second["tenant"]["id"] == "tnt_summit"
    assert second["duplicate"] is None
    assert db_rows(ctx, "SELECT id FROM credit_claims") == []


def test_lead_without_an_account_id_is_compared_with_every_account_of_the_tenant(client, ctx, clock):
    # Angi only "sometimes" sends the account id. A lead that does not say which account
    # bought it cannot be ruled out, so it is matched like before.
    first = send(client, leads_payload())  # spEntityId 12345678 -> tnt_summit
    clock.advance(hours=1)
    without_account = leads_payload()
    del without_account["spEntityId"]
    response = post_lead(client, without_account, headers={}, path=tenant_url(ctx, "tnt_summit"))

    second = response.json()
    assert second["result"] == "processed"
    assert lead_row(ctx, second["leadId"])["external_account_id"] is None
    assert lead_row(ctx, second["leadId"])["duplicate_of_lead_id"] == first["leadId"]


# --- the window --------------------------------------------------------------------------


def test_lead_just_inside_the_window_is_a_duplicate(client, ctx, clock):
    # Angi: "charged twice for the same homeowner within 45 days" - 44 days later still counts.
    first = send(client, ads_payload())
    clock.advance(days=44)
    second = send(client, ads_payload())

    assert lead_row(ctx, second["leadId"])["duplicate_of_lead_id"] == first["leadId"]
    assert claim_for(ctx, second["leadId"])["hours_apart"] == 44 * 24


def test_lead_outside_the_window_is_not_a_duplicate(client, ctx, clock):
    # 46 days later the same homeowner is a fresh, billable lead.
    send(client, ads_payload())
    clock.advance(days=46)
    second = send(client, ads_payload())

    assert second["duplicate"] is None
    assert lead_row(ctx, second["leadId"])["is_duplicate"] == 0
    assert claim_for(ctx, second["leadId"]) is None


def test_window_boundary_is_inclusive(client, ctx, clock):
    # Exactly 45 days apart is still "within 45 days"; one second more is not.
    first = send(client, ads_payload())
    clock.advance(days=45)
    on_boundary = send(client, ads_payload(Email="other1@example.com"))
    assert lead_row(ctx, on_boundary["leadId"])["duplicate_of_lead_id"] == first["leadId"]


def test_one_second_past_the_window_is_not_a_duplicate(client, ctx, clock):
    # The window is measured to the second from the earlier lead's arrival.
    send(client, ads_payload())
    clock.advance(days=45, seconds=1)
    late = send(client, ads_payload())
    assert late["duplicate"] is None


def test_window_length_comes_from_the_duplicate_window_days_setting(client, ctx, clock, settings):
    # The window is configuration, not a constant: with 10 days, day 9 matches and day 11 does not.
    settings.duplicate_window_days = 10
    first = send(client, ads_payload(Email="in@example.com", PhoneNumber="3175550101"))
    clock.advance(days=9)
    inside = send(client, ads_payload(Email="in@example.com", PhoneNumber="3175550101"))
    assert lead_row(ctx, inside["leadId"])["duplicate_of_lead_id"] == first["leadId"]

    send(client, ads_payload(Email="out@example.com", PhoneNumber="3175550202"))
    clock.advance(days=11)
    outside = send(client, ads_payload(Email="out@example.com", PhoneNumber="3175550202"))
    assert outside["duplicate"] is None


def test_chain_of_three_points_each_lead_at_the_most_recent_earlier_one(client, ctx, clock):
    # A, B, C from one consumer, all matching equally well: B duplicates A and C duplicates
    # B (of equally strong matches, the latest earlier lead).
    a = send(client, ads_payload())
    clock.advance(days=1)
    b = send(client, ads_payload())
    clock.advance(days=1)
    c = send(client, ads_payload())

    assert lead_row(ctx, a["leadId"])["is_duplicate"] == 0
    assert lead_row(ctx, b["leadId"])["duplicate_of_lead_id"] == a["leadId"]
    assert lead_row(ctx, c["leadId"])["duplicate_of_lead_id"] == b["leadId"]
    # Each duplicate is its own charge, so each gets its own claim.
    claims = db_rows(ctx, "SELECT lead_id, original_lead_id FROM credit_claims ORDER BY created_at")
    assert claims == [
        {"lead_id": b["leadId"], "original_lead_id": a["leadId"]},
        {"lead_id": c["leadId"], "original_lead_id": b["leadId"]},
    ]


def test_chain_keeps_matching_while_each_link_is_inside_the_window(client, ctx, clock):
    # C is 60 days after A (outside) but 30 days after B (inside): it duplicates B.
    send(client, ads_payload())
    clock.advance(days=30)
    b = send(client, ads_payload())
    clock.advance(days=30)
    c = send(client, ads_payload())

    assert lead_row(ctx, c["leadId"])["duplicate_of_lead_id"] == b["leadId"]
    assert claim_for(ctx, c["leadId"])["hours_apart"] == 30 * 24


# --- numbers that must never match -------------------------------------------------------


@pytest.mark.parametrize("phone", ["1234567890", "0000000000", "5551234", "not a phone", ""])
def test_invalid_phone_never_matches_another_invalid_phone(client, ctx, clock, phone):
    # Junk numbers normalise to nothing, so two leads with the same junk are not "the same person".
    first = send(client, ads_payload(PhoneNumber=phone, Email="one@example.com"))
    clock.advance(minutes=5)
    second = send(client, ads_payload(PhoneNumber=phone, Email="two@example.com"))

    assert lead_row(ctx, first["leadId"])["phone_normalized"] is None
    assert second["duplicate"] is None
    assert lead_row(ctx, second["leadId"])["is_duplicate"] == 0


def test_leads_without_any_contact_details_do_not_match_each_other(client, ctx, clock):
    # Nothing to compare on means no match (and no claim), never "both empty so equal".
    send(client, ads_payload(PhoneNumber="", Email=""))
    clock.advance(minutes=5)
    second = send(client, ads_payload(PhoneNumber="", Email=""))

    assert second["result"] == "processed"
    assert second["duplicate"] is None
    assert db_rows(ctx, "SELECT id FROM credit_claims") == []


def test_masked_phone_on_the_new_lead_never_matches(client, ctx, clock):
    # A masked number is a temporary Angi proxy that different consumers can share.
    send(client, leads_payload(primaryPhone="3035550150", email="one@example.com"))
    clock.advance(minutes=5)
    second = send(
        client,
        leads_payload(
            primaryPhone="3035550150", email="two@example.com", primaryPhoneDetails={"maskedNumber": True}
        ),
    )

    assert lead_row(ctx, second["leadId"])["phone_is_masked"] == 1
    assert second["duplicate"] is None


def test_masked_phone_on_the_earlier_lead_never_matches(client, ctx, clock):
    # The other direction: a real number later equal to an old proxy number is a coincidence.
    send(
        client,
        leads_payload(
            primaryPhone="3035550150", email="one@example.com", primaryPhoneDetails={"maskedNumber": True}
        ),
    )
    clock.advance(minutes=5)
    second = send(client, leads_payload(primaryPhone="3035550150", email="two@example.com"))

    assert second["duplicate"] is None


def test_two_masked_phones_never_match(client, ctx, clock):
    # Both sides masked: still no match on the proxy number.
    masked = {"maskedNumber": True}
    send(client, leads_payload(primaryPhone="3035550150", email="one@example.com", primaryPhoneDetails=masked))
    clock.advance(minutes=5)
    second = send(
        client, leads_payload(primaryPhone="3035550150", email="two@example.com", primaryPhoneDetails=masked)
    )

    assert second["duplicate"] is None


def test_masked_flag_sent_as_a_string_is_honoured(client, ctx, clock):
    # Angi's samples send this flag as "true" as well as true; "true" must also suppress the match.
    send(client, leads_payload(primaryPhone="3035550150", email="one@example.com"))
    clock.advance(minutes=5)
    second = send(
        client,
        leads_payload(
            primaryPhone="3035550150", email="two@example.com", primaryPhoneDetails={"maskedNumber": "true"}
        ),
    )

    assert second["duplicate"] is None


@pytest.mark.parametrize("masked_side", ["earlier", "later", "both"])
def test_masked_phone_does_not_stop_an_email_match(client, ctx, clock, masked_side):
    # The same email is still the same consumer; only the phone is ignored, and it is not listed.
    masked = {"maskedNumber": True}
    plain = {"maskedNumber": False}
    first = send(
        client,
        leads_payload(
            primaryPhone="3035550150",
            primaryPhoneDetails=masked if masked_side in ("earlier", "both") else plain,
        ),
    )
    clock.advance(minutes=5)
    second = send(
        client,
        leads_payload(
            primaryPhone="3035550150",
            primaryPhoneDetails=masked if masked_side in ("later", "both") else plain,
        ),
    )

    assert lead_row(ctx, second["leadId"])["duplicate_of_lead_id"] == first["leadId"]
    assert matched_on(ctx, second["leadId"]) == ["email"]


def test_new_leads_secondary_phone_matching_an_earlier_primary_phone_is_a_duplicate(client, ctx, clock):
    # People swap "mobile" and "home" between requests; either number identifies them.
    first = send(client, leads_payload(primaryPhone="3035550111", email="one@example.com"))
    clock.advance(minutes=5)
    second = send(
        client,
        leads_payload(primaryPhone="3035550222", secondaryPhone="(303) 555-0111", email="two@example.com"),
    )

    assert lead_row(ctx, second["leadId"])["duplicate_of_lead_id"] == first["leadId"]
    assert matched_on(ctx, second["leadId"]) == ["phone"]


def test_new_leads_primary_phone_matching_an_earlier_secondary_phone_is_a_duplicate(client, ctx, clock):
    # The same rule in the other direction.
    first = send(
        client, leads_payload(primaryPhone="3035550222", secondaryPhone="3035550111", email="one@example.com")
    )
    clock.advance(minutes=5)
    second = send(client, leads_payload(primaryPhone="3035550111", email="two@example.com"))

    assert lead_row(ctx, second["leadId"])["duplicate_of_lead_id"] == first["leadId"]
    assert matched_on(ctx, second["leadId"]) == ["phone"]


def test_masked_primary_does_not_block_a_secondary_phone_match(client, ctx, clock):
    # Only the masked primary is ignored; a real secondary number still identifies the consumer.
    first = send(client, leads_payload(primaryPhone="3035550111", email="one@example.com"))
    clock.advance(minutes=5)
    second = send(
        client,
        leads_payload(
            primaryPhone="3035550999",
            primaryPhoneDetails={"maskedNumber": True},
            secondaryPhone="3035550111",
            email="two@example.com",
        ),
    )

    assert lead_row(ctx, second["leadId"])["duplicate_of_lead_id"] == first["leadId"]
    assert matched_on(ctx, second["leadId"]) == ["phone"]


# --- confidence --------------------------------------------------------------------------


def test_confidence_is_high_when_phone_and_email_both_match_even_for_another_service(client, ctx, clock):
    # Two independent contact details agreeing is strong evidence regardless of the service.
    send(client, ads_payload(Category="Indianapolis – House Cleaning"))
    clock.advance(hours=1)
    second = send(client, ads_payload(Category="Indianapolis – Window Cleaning"))

    claim = claim_for(ctx, second["leadId"])
    assert second["duplicate"]["confidence"] == "high"
    assert claim["confidence"] == "high"
    assert claim["same_task"] == 0


def test_confidence_is_high_for_a_single_contact_match_on_the_same_service(client, ctx, clock):
    # One matching contact detail plus the same service requested is the classic double charge.
    send(client, ads_payload(Email="one@example.com"))
    clock.advance(hours=1)
    second = send(client, ads_payload(Email="two@example.com"))

    claim = claim_for(ctx, second["leadId"])
    assert json.loads(claim["matched_on"]) == ["phone"]
    assert claim["same_task"] == 1
    assert claim["confidence"] == "high"


def test_same_service_comparison_ignores_case_and_whitespace(client, ctx, clock):
    # "house cleaning" and "House Cleaning " are the same service.
    send(client, ads_payload(Email="one@example.com", Category="House Cleaning"))
    clock.advance(hours=1)
    second = send(client, ads_payload(Email="two@example.com", Category="  house CLEANING "))

    claim = claim_for(ctx, second["leadId"])
    assert claim["same_task"] == 1
    assert claim["confidence"] == "high"


def test_confidence_is_medium_for_a_single_contact_match_on_a_different_service(client, ctx, clock):
    # Same homeowner, different project: Angi may treat it as a separate lead, so say so.
    send(client, ads_payload(Email="one@example.com", Category="Indianapolis – House Cleaning"))
    clock.advance(hours=1)
    second = send(client, ads_payload(Email="two@example.com", Category="Indianapolis – Gutter Repair"))

    claim = claim_for(ctx, second["leadId"])
    assert json.loads(claim["matched_on"]) == ["phone"]
    assert claim["same_task"] == 0
    assert claim["confidence"] == "medium"
    assert second["duplicate"]["confidence"] == "medium"


def test_confidence_is_medium_for_an_email_only_match_on_a_different_service(client, ctx, clock):
    # The same rule when the single matching detail is the email.
    send(client, leads_payload(primaryPhone="3035550101", taskName="Central A/C - Repair"))
    clock.advance(hours=1)
    second = send(client, leads_payload(primaryPhone="3035550102", taskName="Furnace - Install"))

    assert matched_on(ctx, second["leadId"]) == ["email"]
    assert claim_for(ctx, second["leadId"])["confidence"] == "medium"


def test_confidence_is_high_for_the_same_service_request_even_with_a_different_service_name(client, ctx, clock):
    # The same srOid is the same request by definition, whatever the task is called.
    send(client, leads_payload(srOid=31337, primaryPhone="3035550101", email="a@example.com", taskName="A/C Repair"))
    clock.advance(hours=1)
    second = send(
        client,
        leads_payload(srOid=31337, primaryPhone="3035550102", email="b@example.com", taskName="Furnace Install"),
    )

    claim = claim_for(ctx, second["leadId"])
    assert json.loads(claim["matched_on"]) == ["service_request"]
    assert claim["same_task"] == 0
    assert claim["confidence"] == "high"


# --- which earlier lead, when several match --------------------------------------------------


def test_stronger_match_is_preferred_over_a_more_recent_weaker_one(client, ctx, clock):
    # The claim quotes its evidence, so it should quote the best there is: the older lead
    # that matches on phone AND email, not the newer one that only shares the phone.
    a = send(client, ads_payload(Email="pat@example.com"))
    clock.advance(days=5)
    send(client, ads_payload(Email="other@example.com", Category="Indianapolis – Window Cleaning"))
    clock.advance(days=5)
    c = send(client, ads_payload(Email="pat@example.com"))

    claim = claim_for(ctx, c["leadId"])
    assert claim["original_lead_id"] == a["leadId"]
    assert json.loads(claim["matched_on"]) == ["phone", "email"]
    assert claim["same_task"] == 1
    assert claim["confidence"] == "high"
    assert claim["hours_apart"] == 10 * 24


def test_same_service_request_is_preferred_over_matching_contact_details(client, ctx, clock):
    # The same srOid sold twice is the clearest double charge; it outranks a more recent
    # lead that merely has the same phone and email.
    a = send(client, leads_payload(srOid=7070, primaryPhone="3035550101", email="a@example.com"))
    clock.advance(hours=1)
    send(client, leads_payload(primaryPhone="3035550102", email="b@example.com"))
    clock.advance(hours=1)
    c = send(client, leads_payload(srOid=7070, primaryPhone="3035550102", email="b@example.com"))

    assert lead_row(ctx, c["leadId"])["duplicate_of_lead_id"] == a["leadId"]
    assert matched_on(ctx, c["leadId"]) == ["service_request"]


# --- the synthetic canary --------------------------------------------------------------------


def test_canary_runs_are_never_duplicates_and_always_get_the_intro(client, ctx, mailer, clock):
    # The monitoring probe posts the same consumer every few minutes on purpose. That is
    # not a double charge (no claim on the Credits page), and each run must exercise the
    # real intro email, which is the thing the canary exists to prove.
    runs = []
    for _ in range(3):
        runs.append(send(client, ads_payload(ALAccountId=CANARY_ACCOUNT, Email="canary@example.com")))
        clock.advance(minutes=5)

    assert all(run["tenant"]["id"] == "tnt_canary" for run in runs)
    assert all(run["duplicate"] is None for run in runs)
    assert all(run["email"]["template"] == "intro" for run in runs)
    assert db_one(ctx, "SELECT COUNT(*) AS n FROM leads WHERE is_duplicate = 1")["n"] == 0
    assert db_rows(ctx, "SELECT id FROM credit_claims") == []
    assert db_rows(ctx, "SELECT id FROM lead_events WHERE type = 'lead.duplicate_detected'") == []
    ctx.worker.deliver_due()
    assert [email.subject for email in mailer.sent] == [
        "Your House Cleaning request: pick a time with Synthetic Canary Co."
    ] * 3


# --- consequences of a duplicate -----------------------------------------------------------


def test_duplicate_gets_the_repeat_request_email_and_no_follow_ups(client, ctx, mailer, clock):
    # The consumer already has our intro and nudges; a repeat request is acknowledged once.
    first = send(client, ads_payload())
    clock.advance(minutes=10)
    second = send(client, ads_payload())

    queued = db_rows(
        ctx, "SELECT template, sequence FROM outbox_messages WHERE lead_id = ? ORDER BY sequence", (second["leadId"],)
    )
    assert queued == [{"template": "repeat_request", "sequence": 0}]
    original = db_rows(
        ctx, "SELECT template FROM outbox_messages WHERE lead_id = ? ORDER BY sequence", (first["leadId"],)
    )
    assert [m["template"] for m in original] == ["intro", "followup", "followup"]
    assert second["email"] == {
        "status": "queued",
        "template": "repeat_request",
        "to": "bob.builder@gmail.com",
        "followUpsScheduled": 0,
    }

    clock.advance(seconds=1)
    ctx.worker.deliver_due()
    subjects = [email.subject for email in mailer.sent]
    assert len(subjects) == 2
    assert sum(1 for s in subjects if s.startswith("We got your new request")) == 1
    assert sum(1 for s in subjects if "pick a time" in s) == 1
    sent = db_one(ctx, "SELECT template, status FROM outbox_messages WHERE lead_id = ?", (second["leadId"],))
    assert sent == {"template": "repeat_request", "status": "sent"}


def test_duplicate_still_gets_the_acknowledgement_after_the_intro_was_sent(client, ctx, mailer, clock):
    # The usual case: the first email went out long before the repeat request arrived.
    send(client, ads_payload())
    clock.advance(seconds=1)
    ctx.worker.deliver_due()
    clock.advance(days=3)
    second = send(client, ads_payload())

    assert second["email"]["template"] == "repeat_request"
    assert templates(ctx, second["leadId"]) == ["repeat_request"]


def test_duplicate_with_an_address_we_never_wrote_to_gets_the_intro_and_follow_ups(client, ctx, mailer, clock):
    # Two people who each try Angi's spec example with their own email share its sample
    # phone number. The second is flagged (same phone) and has a claim, but "we got your
    # new request" would make no sense to someone who never received our first email.
    first = send(client, ads_payload(Email="presenter@example.com"))
    clock.advance(minutes=20)
    second = send(client, ads_payload(Email="reviewer@example.com"))

    assert second["duplicate"]["ofLeadId"] == first["leadId"]
    assert second["duplicate"]["matchedOn"] == ["phone"]
    assert claim_for(ctx, second["leadId"]) is not None
    assert second["email"] == {
        "status": "queued",
        "template": "intro",
        "to": "reviewer@example.com",
        "followUpsScheduled": 2,
    }
    assert templates(ctx, second["leadId"]) == ["intro", "followup", "followup"]
    clock.advance(seconds=1)
    ctx.worker.deliver_due()
    assert sorted(email.to for email in mailer.sent) == ["presenter@example.com", "reviewer@example.com"]
    assert all("pick a time" in email.subject for email in mailer.sent)


def test_duplicate_of_a_lead_that_could_not_be_emailed_gets_the_intro(client, ctx, clock):
    # The earlier lead had no usable address, so this is the first email the consumer gets.
    send(client, ads_payload(Email="not-an-email"))
    clock.advance(days=3)
    second = send(client, ads_payload(Email="pat@example.com"))

    assert second["duplicate"]["matchedOn"] == ["phone"]
    assert second["email"]["template"] == "intro"
    assert templates(ctx, second["leadId"]) == ["intro", "followup", "followup"]


def test_duplicate_gets_the_intro_when_the_first_email_was_given_up_on(client, ctx, clock):
    # An intro that failed for good reached nobody, so it does not count as "introduced".
    first = send(client, ads_payload())
    with ctx.db.transaction() as conn:
        conn.execute("UPDATE outbox_messages SET status = 'failed' WHERE lead_id = ?", (first["leadId"],))
    clock.advance(hours=1)
    second = send(client, ads_payload())

    assert second["duplicate"]["ofLeadId"] == first["leadId"]
    assert second["email"]["template"] == "intro"


def test_acknowledgement_only_counts_first_emails_from_the_same_tenant(client, ctx, clock):
    # Another business having written to this address says nothing about ours.
    send(client, ads_payload(ALAccountId=BLUELINE_ACCOUNT, Email="pat@example.com", PhoneNumber="3175550101"))
    first = send(client, ads_payload(Email="other@example.com"))
    clock.advance(hours=1)
    second = send(client, ads_payload(Email="pat@example.com"))  # same phone as `first`, new address for Sparkle

    assert second["duplicate"]["ofLeadId"] == first["leadId"]
    assert second["email"]["template"] == "intro"


def test_duplicate_creates_a_credit_claim_with_the_evidence(client, ctx, clock):
    # One claim per duplicate, carrying everything needed to ask Angi for the fee back.
    first = send(client, ads_payload())
    clock.advance(minutes=90)
    second = send(client, ads_payload())

    duplicate = lead_row(ctx, second["leadId"])
    claims = db_rows(ctx, "SELECT * FROM credit_claims")
    assert len(claims) == 1
    claim = claims[0]
    assert claim["id"] == second["duplicate"]["creditClaimId"]
    assert claim["tenant_id"] == "tnt_sparkle"
    assert claim["lead_id"] == second["leadId"]
    assert claim["original_lead_id"] == first["leadId"]
    assert claim["reason"] == "duplicate"
    assert claim["status"] == "detected"
    assert claim["amount_cents"] == duplicate["fee_cents"] == 2800  # the account's default fee
    assert claim["hours_apart"] == 1.5
    assert claim["same_task"] == 1
    assert claim["confidence"] == "high"
    assert json.loads(claim["matched_on"]) == ["phone", "email"]
    assert claim["request_deadline"] == iso(parse_iso(duplicate["received_at"]) + timedelta(days=30))
    assert claim["submitted_at"] is None
    assert claim["resolved_at"] is None
    assert claim["credited_cents"] is None
    assert claim["created_at"] == iso(clock())


def test_claim_deadline_follows_the_credit_request_window_setting(client, ctx, clock, settings):
    # The deadline is the duplicate's arrival plus credit_request_window_days.
    settings.credit_request_window_days = 10
    send(client, ads_payload())
    clock.advance(days=3)
    second = send(client, ads_payload())

    claim = claim_for(ctx, second["leadId"])
    assert claim["request_deadline"] == iso(clock() + timedelta(days=10))


def test_claim_amount_is_the_fee_carried_in_an_angi_leads_payload(client, ctx, clock):
    # Angi Leads states the price of each lead; the claim is for the duplicate's own fee.
    send(client, leads_payload(fee=25.67))
    clock.advance(hours=1)
    second = send(client, leads_payload(fee=31.05))

    assert claim_for(ctx, second["leadId"])["amount_cents"] == 3105


def test_claim_amount_is_unknown_when_no_fee_is_available(client, ctx, clock):
    # No fee in the payload and no default on the mapping: the amount is NULL, never a guess.
    send(client, leads_payload(fee=None))
    clock.advance(hours=1)
    second = send(client, leads_payload(fee=None))

    assert lead_row(ctx, second["leadId"])["fee_cents"] is None
    assert claim_for(ctx, second["leadId"])["amount_cents"] is None


def test_duplicate_without_an_email_still_gets_a_claim(client, ctx, clock):
    # The credit does not depend on us being able to email the consumer.
    first = send(client, ads_payload())
    clock.advance(hours=1)
    second = send(client, ads_payload(Email=""))

    assert second["email"] == {"status": "skipped", "reason": "no_valid_email"}
    assert claim_for(ctx, second["leadId"])["original_lead_id"] == first["leadId"]
    assert db_rows(ctx, "SELECT id FROM outbox_messages WHERE lead_id = ?", (second["leadId"],)) == []


def test_duplicate_detected_event_is_recorded_on_the_lead(client, ctx, clock):
    # The lead's timeline shows why it was flagged and which claim it produced.
    first = send(client, ads_payload())
    clock.advance(minutes=30)
    second = send(client, ads_payload())

    events = db_rows(
        ctx, "SELECT * FROM lead_events WHERE lead_id = ? AND type = 'lead.duplicate_detected'", (second["leadId"],)
    )
    assert len(events) == 1
    assert events[0]["tenant_id"] == "tnt_sparkle"
    assert json.loads(events[0]["data_json"]) == {
        "original_lead_id": first["leadId"],
        "matched_on": ["phone", "email"],
        "confidence": "high",
        "hours_apart": 0.5,
        "claim_id": claim_for(ctx, second["leadId"])["id"],
    }
    # The original lead's timeline is untouched.
    assert db_rows(
        ctx, "SELECT id FROM lead_events WHERE lead_id = ? AND type = 'lead.duplicate_detected'", (first["leadId"],)
    ) == []


def test_webhook_response_reports_the_duplicate(client, ctx, clock):
    # Still a successful delivery for Angi, with the duplicate described for our own logs.
    first = send(client, ads_payload())
    clock.advance(minutes=30)
    response = post_lead(client, ads_payload())

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["result"] == "processed"
    assert body["duplicate"] == {
        "ofLeadId": first["leadId"],
        "matchedOn": ["phone", "email"],
        "confidence": "high",
        "creditClaimId": claim_for(ctx, body["leadId"])["id"],
    }


def test_lead_detail_links_a_duplicate_and_its_original_both_ways(client, ctx, clock):
    # The operator can navigate from the duplicate to the original and back.
    first = send(client, ads_payload())
    clock.advance(minutes=30)
    second = send(client, ads_payload())

    duplicate = client.get(f"/admin/api/leads/{second['leadId']}", headers=ADMIN).json()
    assert duplicate["original"]["id"] == first["leadId"]
    assert duplicate["claim"]["original_lead_id"] == first["leadId"]
    assert duplicate["lead"]["duplicate_matched_on"] == ["phone", "email"]
    original = client.get(f"/admin/api/leads/{first['leadId']}", headers=ADMIN).json()
    assert [d["id"] for d in original["duplicates"]] == [second["leadId"]]
    assert original["claim"] is None


# --- redelivery is not a duplicate ---------------------------------------------------------


def test_exact_redelivery_creates_no_claim_and_is_not_flagged(client, ctx, mailer, clock):
    # Angi retrying the same lead id was charged once: no second lead, no claim, no second email.
    payload = ads_payload()
    first = send(client, payload)
    clock.advance(minutes=15)
    again = send(client, payload)

    assert again["result"] == "duplicate_delivery"
    assert again["leadId"] == first["leadId"]
    assert db_one(ctx, "SELECT COUNT(*) AS n FROM leads")["n"] == 1
    assert lead_row(ctx, first["leadId"])["is_duplicate"] == 0
    assert db_rows(ctx, "SELECT id FROM credit_claims") == []
    assert db_rows(ctx, "SELECT id FROM lead_events WHERE type = 'lead.duplicate_detected'") == []
    ctx.worker.deliver_due()
    assert len(mailer.sent) == 1


def test_three_redeliveries_fifteen_minutes_apart_never_create_a_claim(client, ctx, clock):
    # Angi's retry schedule (3 times, 15 minutes apart) must stay claim-free.
    payload = leads_payload()
    send(client, payload)
    for _ in range(3):
        clock.advance(minutes=15)
        assert send(client, payload)["result"] == "duplicate_delivery"

    assert db_one(ctx, "SELECT COUNT(*) AS n FROM leads")["n"] == 1
    assert db_rows(ctx, "SELECT id FROM credit_claims") == []


def test_corrected_redelivery_of_the_same_lead_id_creates_no_claim(client, ctx, clock):
    # Same lead id with changed content is an update to one lead, not a second charge.
    payload = ads_payload()
    first = send(client, payload)
    clock.advance(minutes=15)
    updated = send(client, {**payload, "Urgency": "ASAP"})

    assert updated["result"] == "updated"
    assert updated["leadId"] == first["leadId"]
    assert lead_row(ctx, first["leadId"])["is_duplicate"] == 0
    assert db_rows(ctx, "SELECT id FROM credit_claims") == []


def test_redelivery_of_a_duplicate_lead_keeps_exactly_one_claim(client, ctx, clock):
    # Retrying the duplicate itself must not double the claim (or the amount we report).
    send(client, ads_payload())
    clock.advance(hours=1)
    payload = ads_payload()
    second = send(client, payload)
    clock.advance(minutes=15)
    again = send(client, payload)

    assert again["result"] == "duplicate_delivery"
    claims = db_rows(ctx, "SELECT lead_id FROM credit_claims")
    assert claims == [{"lead_id": second["leadId"]}]
    assert db_one(ctx, "SELECT COUNT(*) AS n FROM leads")["n"] == 2


def test_redelivery_of_the_original_after_its_duplicate_arrived_does_not_flag_the_original(client, ctx, clock):
    # The original stays the original even if Angi retries it after the duplicate exists.
    payload = ads_payload()
    first = send(client, payload)
    clock.advance(hours=1)
    send(client, ads_payload())
    clock.advance(minutes=15)
    send(client, payload)

    assert lead_row(ctx, first["leadId"])["is_duplicate"] == 0
    assert db_one(ctx, "SELECT COUNT(*) AS n FROM credit_claims")["n"] == 1


def test_replaying_a_processed_event_does_not_create_a_duplicate(client, ctx, clock):
    # Operator replay of an event that already produced its lead is a no-op.
    first = send(client, ads_payload())
    clock.advance(hours=1)
    replay = client.post(f"/admin/api/events/{first['eventId']}/replay", headers=ADMIN)

    assert replay.status_code == 200
    assert replay.json()["leadId"] == first["leadId"]
    assert db_one(ctx, "SELECT COUNT(*) AS n FROM leads")["n"] == 1
    assert lead_row(ctx, first["leadId"])["is_duplicate"] == 0
    assert db_rows(ctx, "SELECT id FROM credit_claims") == []


def test_quarantined_leads_are_checked_for_duplicates_when_they_are_replayed(client, ctx, clock):
    # Two leads from one homeowner held for the same missing mapping are compared with each
    # other once released, exactly as if they had been processed on arrival.
    first = send(client, ads_payload(ALAccountId="555000"))
    clock.advance(hours=2)
    second = send(client, ads_payload(ALAccountId="555000"))
    assert first["result"] == second["result"] == "quarantined"
    clock.advance(hours=1)
    mapped = client.post(
        "/admin/api/tenants/tnt_sparkle/lead-sources", headers=ADMIN, json={"external_account_id": "555000"}
    )
    assert mapped.status_code == 201

    original = db_one(ctx, "SELECT * FROM leads WHERE first_event_id = ?", (first["eventId"],))
    released = db_one(ctx, "SELECT * FROM leads WHERE first_event_id = ?", (second["eventId"],))
    assert original["is_duplicate"] == 0
    assert released["duplicate_of_lead_id"] == original["id"]
    # The gap is measured between the two arrivals, not to the moment of the replay.
    assert claim_for(ctx, released["id"])["hours_apart"] == 2.0


def test_lead_released_from_quarantine_is_compared_with_what_arrived_before_it(client, ctx, clock):
    # A lead held because its tenant was switched off is still compared with the leads
    # that arrived BEFORE it once the tenant is back and the event is replayed.
    first = send(client, ads_payload())
    clock.advance(hours=2)
    with ctx.db.transaction() as conn:
        conn.execute("UPDATE tenants SET is_active = 0 WHERE id = 'tnt_sparkle'")
    held = send(client, ads_payload())
    assert (held["result"], held["reason"]) == ("quarantined", "tenant_inactive")
    clock.advance(hours=1)
    with ctx.db.transaction() as conn:
        conn.execute("UPDATE tenants SET is_active = 1 WHERE id = 'tnt_sparkle'")

    assert ctx.ingest.replay_quarantined() == [{"eventId": held["eventId"], "result": "processed"}]

    released = db_one(ctx, "SELECT * FROM leads WHERE first_event_id = ?", (held["eventId"],))
    assert released["duplicate_of_lead_id"] == first["leadId"]
    assert claim_for(ctx, released["id"])["hours_apart"] == 2.0


def test_duplicate_is_detected_when_the_earlier_lead_is_processed_after_the_later_one(client, ctx, clock):
    # Two charges for one homeowner must yield a claim whatever order the two events were processed in.
    held = send(client, ads_payload(ALAccountId="555000"))  # arrives first, quarantined: unmapped account
    assert held["result"] == "quarantined"
    clock.advance(minutes=30)
    # Arrives second on the tenant's own URL without an account id, processed straight away.
    payload = ads_payload()
    del payload["ALAccountId"]
    later = post_lead(client, payload, headers={}, path=tenant_url(ctx, "tnt_sparkle")).json()
    assert later["result"] == "processed"
    assert later["duplicate"] is None  # the earlier lead does not exist yet
    clock.advance(minutes=30)
    client.post("/admin/api/tenants/tnt_sparkle/lead-sources", headers=ADMIN, json={"external_account_id": "555000"})

    assert db_one(ctx, "SELECT COUNT(*) AS n FROM leads")["n"] == 2
    # Exactly one of the two charges is the duplicate - the one that ARRIVED second - and it has a claim.
    earlier = db_one(ctx, "SELECT * FROM leads WHERE first_event_id = ?", (held["eventId"],))
    flagged = lead_row(ctx, later["leadId"])
    assert earlier["is_duplicate"] == 0
    assert flagged["is_duplicate"] == 1
    assert flagged["duplicate_of_lead_id"] == earlier["id"]
    assert matched_on(ctx, later["leadId"]) == ["phone", "email"]
    claims = db_rows(ctx, "SELECT * FROM credit_claims")
    assert len(claims) == 1
    assert claims[0]["lead_id"] == later["leadId"]
    assert claims[0]["original_lead_id"] == earlier["id"]
    assert claims[0]["hours_apart"] == 0.5  # between the two arrivals
    assert claims[0]["amount_cents"] == flagged["fee_cents"]
    assert claims[0]["request_deadline"] == iso(parse_iso(flagged["received_at"]) + timedelta(days=30))
    # The finding is on the flagged lead's timeline, like any other duplicate.
    events = db_rows(
        ctx, "SELECT data_json FROM lead_events WHERE lead_id = ? AND type = 'lead.duplicate_detected'", (later["leadId"],)
    )
    assert len(events) == 1
    assert json.loads(events[0]["data_json"])["claim_id"] == claims[0]["id"]
    # The consumer already has the intro (sent for the lead that was processed first), so
    # the late one is acknowledged instead of introducing the business a second time.
    assert templates(ctx, later["leadId"]) == ["intro", "followup", "followup"]
    assert templates(ctx, earlier["id"]) == ["repeat_request"]


def test_duplicate_is_detected_when_the_earlier_lead_failed_and_was_retried_later(client, ctx, clock, monkeypatch):
    # The other way to be processed late: the first lead hit a bug and was retried after
    # the second one had already been stored.
    monkeypatch.setattr("app.ingest.find_duplicate", boom)
    failed = send(client, ads_payload())
    assert failed["result"] == "accepted"
    monkeypatch.undo()  # the bug is fixed
    clock.advance(hours=1)
    later = send(client, ads_payload())
    assert later["duplicate"] is None
    clock.advance(minutes=1)

    assert ctx.ingest.retry_unfinished(older_than_seconds=0) == 1

    earlier = db_one(ctx, "SELECT * FROM leads WHERE first_event_id = ?", (failed["eventId"],))
    assert earlier["is_duplicate"] == 0
    assert lead_row(ctx, later["leadId"])["duplicate_of_lead_id"] == earlier["id"]
    claims = db_rows(ctx, "SELECT lead_id, original_lead_id, hours_apart FROM credit_claims")
    assert claims == [{"lead_id": later["leadId"], "original_lead_id": earlier["id"], "hours_apart": 1.0}]


def test_late_processed_lead_does_not_flag_a_later_lead_of_another_consumer_or_tenant(client, ctx, clock, monkeypatch):
    # Only the same consumer, for the same tenant, is paired up afterwards.
    monkeypatch.setattr("app.ingest.find_duplicate", boom)
    send(client, ads_payload())
    monkeypatch.undo()
    clock.advance(hours=1)
    stranger = send(client, ads_payload(PhoneNumber="3175550199", Email="stranger@example.com"))
    other_tenant = send(client, ads_payload(ALAccountId=BLUELINE_ACCOUNT))
    clock.advance(minutes=1)

    assert ctx.ingest.retry_unfinished(older_than_seconds=0) == 1

    assert lead_row(ctx, stranger["leadId"])["is_duplicate"] == 0
    assert lead_row(ctx, other_tenant["leadId"])["is_duplicate"] == 0
    assert db_rows(ctx, "SELECT id FROM credit_claims") == []


def test_lead_processed_on_arrival_never_flags_an_earlier_lead(client, ctx, clock):
    # The ordinary order is untouched: the second arrival is the duplicate, never the first.
    first = send(client, ads_payload())
    clock.advance(hours=1)
    second = send(client, ads_payload())

    assert lead_row(ctx, first["leadId"])["is_duplicate"] == 0
    assert lead_row(ctx, second["leadId"])["is_duplicate"] == 1
    assert db_one(ctx, "SELECT COUNT(*) AS n FROM credit_claims")["n"] == 1


# --- find_duplicate directly ---------------------------------------------------------------


def normalized(payload: dict):
    """The canonical lead the parser builds from a payload (what ingest hands to find_duplicate)."""
    return parse_angi_payload(payload).lead


def test_find_duplicate_returns_the_earlier_lead_with_match_details(client, ctx, clock):
    # The match carries the original row and everything the claim needs.
    first = send(client, ads_payload())
    clock.advance(hours=5, minutes=20)

    with ctx.db.read() as conn:
        match = find_duplicate(
            conn, tenant_id="tnt_sparkle", lead=normalized(ads_payload()), received_at=iso(clock()), window_days=45
        )

    assert match is not None
    assert match.original["id"] == first["leadId"]
    assert match.matched_on == ["phone", "email"]
    assert match.same_task is True
    assert match.confidence == "high"
    assert match.hours_apart == 5.33  # rounded to two decimals


def test_find_duplicate_returns_none_for_an_unknown_consumer(client, ctx, clock):
    # No shared phone, email or request id: nothing to report.
    send(client, ads_payload())
    clock.advance(hours=1)
    stranger = normalized(ads_payload(PhoneNumber="3175550199", Email="stranger@example.com"))

    with ctx.db.read() as conn:
        assert find_duplicate(
            conn, tenant_id="tnt_sparkle", lead=stranger, received_at=iso(clock()), window_days=45
        ) is None


def test_find_duplicate_returns_none_when_the_lead_has_nothing_to_match_on(client, ctx, clock):
    # A lead with no usable phone, email or request id can never be matched.
    send(client, ads_payload(PhoneNumber="", Email=""))
    clock.advance(hours=1)
    blank = normalized(ads_payload(PhoneNumber="", Email=""))

    with ctx.db.read() as conn:
        assert find_duplicate(
            conn, tenant_id="tnt_sparkle", lead=blank, received_at=iso(clock()), window_days=45
        ) is None


def test_find_duplicate_is_scoped_to_the_tenant(client, ctx, clock):
    # The same consumer is only looked for among the given tenant's leads.
    send(client, ads_payload())
    clock.advance(hours=1)

    with ctx.db.read() as conn:
        other = find_duplicate(
            conn, tenant_id="tnt_blueline", lead=normalized(ads_payload()), received_at=iso(clock()), window_days=45
        )
    assert other is None


def test_find_duplicate_exclude_lead_id_skips_that_lead(client, ctx, clock):
    # Re-checking a stored lead must not match the lead against itself.
    only = send(client, ads_payload())
    clock.advance(hours=1)

    with ctx.db.read() as conn:
        match = find_duplicate(
            conn,
            tenant_id="tnt_sparkle",
            lead=normalized(ads_payload()),
            received_at=iso(clock()),
            window_days=45,
            exclude_lead_id=only["leadId"],
        )
    assert match is None


def test_find_duplicate_exclude_lead_id_falls_back_to_the_next_most_recent(client, ctx, clock):
    # Excluding the newest match exposes the one before it.
    a = send(client, ads_payload())
    clock.advance(days=1)
    b = send(client, ads_payload())
    clock.advance(days=1)
    probe = normalized(ads_payload())

    with ctx.db.read() as conn:
        newest = find_duplicate(conn, tenant_id="tnt_sparkle", lead=probe, received_at=iso(clock()), window_days=45)
        previous = find_duplicate(
            conn,
            tenant_id="tnt_sparkle",
            lead=probe,
            received_at=iso(clock()),
            window_days=45,
            exclude_lead_id=b["leadId"],
        )
    assert newest.original["id"] == b["leadId"]
    assert previous.original["id"] == a["leadId"]
    assert previous.hours_apart == 48.0


def test_find_duplicate_only_looks_at_leads_received_at_or_before_the_given_time(client, ctx, clock):
    # A lead that arrived AFTER the one being checked is not "the earlier lead".
    before = iso(clock() - timedelta(minutes=1))
    stored = send(client, ads_payload())

    with ctx.db.read() as conn:
        earlier = find_duplicate(
            conn, tenant_id="tnt_sparkle", lead=normalized(ads_payload()), received_at=before, window_days=45
        )
        same_instant = find_duplicate(
            conn, tenant_id="tnt_sparkle", lead=normalized(ads_payload()), received_at=iso(clock()), window_days=45
        )
    assert earlier is None
    assert same_instant.original["id"] == stored["leadId"]
    assert same_instant.hours_apart == 0.0


def test_find_duplicate_respects_the_window_days_argument(client, ctx, clock):
    # The same pair is a match with a 45-day window and not with a 5-day one.
    send(client, ads_payload())
    clock.advance(days=6)
    probe = normalized(ads_payload())

    with ctx.db.read() as conn:
        wide = find_duplicate(conn, tenant_id="tnt_sparkle", lead=probe, received_at=iso(clock()), window_days=45)
        narrow = find_duplicate(conn, tenant_id="tnt_sparkle", lead=probe, received_at=iso(clock()), window_days=5)
    assert wide is not None
    assert narrow is None


def test_find_duplicate_does_not_match_a_service_request_id_across_feeds(client, ctx, clock):
    # srOid only means something inside the Angi Leads feed; an equal id elsewhere is a coincidence.
    send(client, leads_payload(srOid=606060, primaryPhone="3035550101", email="a@example.com"))
    clock.advance(hours=1)
    probe = normalized(ads_payload(PhoneNumber="3035550102", Email="b@example.com"))
    probe.external_request_id = "606060"  # same id, but this lead is in the Angi Ads format

    with ctx.db.read() as conn:
        assert find_duplicate(
            conn, tenant_id="tnt_summit", lead=probe, received_at=iso(clock()), window_days=45
        ) is None
