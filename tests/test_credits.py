"""Lead-credit claims (app/credits.py) and the /admin/api/claims endpoints.

Every claim in these tests is created the way production creates one: two leads from the
same consumer are posted to the webhook and duplicate detection records the claim.
"""

from __future__ import annotations

import csv
import io
from datetime import timedelta

import pytest

from app import credits
from app.util import iso
from tests.conftest import (
    ADMIN,
    SUMMIT_ADS_ACCOUNT,
    ads_payload,
    db_one,
    db_rows,
    leads_payload,
    post_lead,
)

EXPORT_URL = "/admin/api/claims/export.csv"


def send(client, payload) -> dict:
    response = post_lead(client, payload)
    assert response.status_code == 200, response.text
    return response.json()


def consumer(n: int) -> dict:
    """Contact details of the n-th distinct made-up homeowner (valid phone, valid email)."""
    return {"Email": f"homeowner{n}@example.com", "PhoneNumber": f"317555{n:04d}"}


def make_claim(client, clock, n: int = 1, *, gap: timedelta = timedelta(hours=1), **overrides) -> dict:
    """Post an original lead and, ``gap`` later, a duplicate of it. Returns the ids involved."""
    fields = {**consumer(n), **overrides}
    first_payload = ads_payload(**fields)
    first = send(client, first_payload)
    clock.advance(seconds=gap.total_seconds())
    second_payload = ads_payload(**fields)
    second = send(client, second_payload)
    assert second["duplicate"] is not None, second
    return {
        "claim_id": second["duplicate"]["creditClaimId"],
        "original_lead_id": first["leadId"],
        "duplicate_lead_id": second["leadId"],
        "original_angi_id": first_payload["CorrelationId"],
        "duplicate_angi_id": second_payload["CorrelationId"],
    }


# The standing note on every claim: Angi's condition is a call, and we only know about our email.
CALL_NOTE = (
    "Angi asks for a call attempt within 24h; this system records the email it sent, not calls: "
    "call the homeowner or have your call log ready"
)


def contact(ctx, clock) -> None:
    """Let the worker send whatever is due (this is the "contact attempt" Angi asks about)."""
    clock.advance(seconds=1)
    ctx.worker.deliver_due()


def list_claims(ctx, **filters) -> list[dict]:
    with ctx.db.read() as conn:
        return credits.list_claims(conn, now=ctx.now(), settings=ctx.settings, **filters)


def get_claim(ctx, claim_id: str) -> dict:
    with ctx.db.read() as conn:
        return credits.get_claim(conn, claim_id, ctx.now(), ctx.settings)


def set_status(ctx, claim_id: str, status: str, **kwargs):
    with ctx.db.transaction() as conn:
        return credits.set_status(conn, claim_id, status, ctx.now(), **kwargs)


def parse_csv(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text, newline="")))


def export_rows(ctx, **filters) -> list[dict[str, str]]:
    """The export as one dict per claim, keyed by column title."""
    rows = parse_csv(credits.export_csv(list_claims(ctx, **filters)))
    return [dict(zip(rows[0], row, strict=True)) for row in rows[1:]]


# --- list_claims -------------------------------------------------------------------------


def test_list_claims_is_empty_without_duplicates(client, ctx, clock):
    # Ordinary leads produce no claims.
    send(client, ads_payload(**consumer(1)))
    send(client, ads_payload(**consumer(2)))
    assert list_claims(ctx) == []


def test_list_claims_returns_the_claim_with_both_leads_joined_in(client, ctx, clock):
    # The worklist row carries what the operator needs without opening either lead.
    made = make_claim(client, clock)

    claims = list_claims(ctx)
    assert len(claims) == 1
    claim = claims[0]
    assert claim["id"] == made["claim_id"]
    assert claim["lead_id"] == made["duplicate_lead_id"]
    assert claim["original_lead_id"] == made["original_lead_id"]
    assert claim["tenant_name"] == "Sparkle Home Cleaning"
    assert claim["angi_account_id"] == "123456"
    assert claim["source_format"] == "angi_ads"
    assert claim["dup_angi_lead_id"] == made["duplicate_angi_id"]
    assert claim["orig_angi_lead_id"] == made["original_angi_id"]
    assert claim["dup_name"] == claim["orig_name"] == "Bob Builder"
    assert claim["dup_email"] == "homeowner1@example.com"
    assert claim["dup_task"] == claim["orig_task"] == "House Cleaning"
    assert claim["orig_fee_cents"] == claim["amount_cents"] == 2800
    assert claim["matched_on"] == ["phone", "email"]  # decoded from JSON
    assert isinstance(claim["evidence"], str)
    assert set(claim["eligibility"]) == {"eligible", "blockers", "notes", "days_left", "contacted_within_sla"}


def test_list_claims_without_a_clock_leaves_eligibility_unassessed(client, ctx, clock):
    # Eligibility depends on "now"; without it the claim is listed but not judged.
    make_claim(client, clock)
    with ctx.db.read() as conn:
        claims = credits.list_claims(conn)
    assert claims[0]["eligibility"] is None
    assert claims[0]["evidence"].startswith("Duplicate lead.")


def test_list_claims_filters_by_tenant(client, ctx, clock):
    # Each tenant sees only its own credits.
    sparkle = make_claim(client, clock, 1)
    summit = make_claim(client, clock, 2, ALAccountId=SUMMIT_ADS_ACCOUNT)

    assert [c["id"] for c in list_claims(ctx, tenant_id="tnt_sparkle")] == [sparkle["claim_id"]]
    assert [c["id"] for c in list_claims(ctx, tenant_id="tnt_summit")] == [summit["claim_id"]]
    assert list_claims(ctx, tenant_id="tnt_blueline") == []
    assert len(list_claims(ctx)) == 2


def test_list_claims_filters_by_status(client, ctx, clock):
    # The worklist can be narrowed to one stage.
    first = make_claim(client, clock, 1)
    second = make_claim(client, clock, 2)
    set_status(ctx, second["claim_id"], "submitted")

    assert [c["id"] for c in list_claims(ctx, status="detected")] == [first["claim_id"]]
    assert [c["id"] for c in list_claims(ctx, status="submitted")] == [second["claim_id"]]
    assert list_claims(ctx, status="credited") == []


def test_list_claims_combines_tenant_and_status_filters(client, ctx, clock):
    # Both filters apply together.
    make_claim(client, clock, 1)
    summit = make_claim(client, clock, 2, ALAccountId=SUMMIT_ADS_ACCOUNT)
    set_status(ctx, summit["claim_id"], "submitted")

    assert [c["id"] for c in list_claims(ctx, tenant_id="tnt_summit", status="submitted")] == [summit["claim_id"]]
    assert list_claims(ctx, tenant_id="tnt_sparkle", status="submitted") == []


def test_list_claims_is_ordered_by_deadline_soonest_first(client, ctx, clock):
    # The claim about to expire is always at the top of the worklist.
    oldest = make_claim(client, clock, 1)
    clock.advance(days=2)
    middle = make_claim(client, clock, 2)
    clock.advance(days=2)
    newest = make_claim(client, clock, 3)

    claims = list_claims(ctx)
    assert [c["id"] for c in claims] == [oldest["claim_id"], middle["claim_id"], newest["claim_id"]]
    deadlines = [c["request_deadline"] for c in claims]
    assert deadlines == sorted(deadlines)


def test_list_claims_orders_by_deadline_not_by_when_the_claim_was_recorded(client, ctx, clock):
    # A duplicate released late from quarantine has an EARLIER deadline than claims recorded before it.
    # (Duplicates are matched within one Angi account, so both leads of the pair wait on the
    # same unmapped account and are released together.)
    first = send(client, ads_payload(**consumer(1), ALAccountId="555000"))
    clock.advance(hours=1)
    held = send(client, ads_payload(**consumer(1), ALAccountId="555000"))
    assert first["result"] == held["result"] == "quarantined"
    clock.advance(days=1)
    recorded_first = make_claim(client, clock, 2)
    clock.advance(days=1)
    mapped = client.post(
        "/admin/api/tenants/tnt_sparkle/lead-sources", headers=ADMIN, json={"external_account_id": "555000"}
    )
    assert mapped.status_code == 201

    claims = list_claims(ctx)
    assert len(claims) == 2
    assert claims[1]["id"] == recorded_first["claim_id"]
    assert claims[0]["created_at"] > claims[1]["created_at"]  # recorded later...
    assert claims[0]["request_deadline"] < claims[1]["request_deadline"]  # ...but due sooner


def test_claims_of_a_test_tenant_are_never_listed(client, ctx, clock):
    # The monitoring probe's tenant is not a paying customer: nothing of it belongs on the
    # worklist, in the totals or in the CSV. (Intake no longer flags the probe's own leads
    # as duplicates, so the claim is created for a normal tenant that is then marked as a
    # test tenant.)
    made = make_claim(client, clock)
    assert [claim["id"] for claim in list_claims(ctx)] == [made["claim_id"]]
    with ctx.db.transaction() as conn:  # there is no API to turn a tenant into a test tenant
        conn.execute("UPDATE tenants SET is_test = 1 WHERE id = 'tnt_sparkle'")

    assert list_claims(ctx) == []
    assert list_claims(ctx, tenant_id="tnt_sparkle") == []
    assert credits.summarize(list_claims(ctx))["open_count"] == 0
    assert export_rows(ctx) == []
    body = client.get("/admin/api/claims", headers=ADMIN).json()
    assert (body["claims"], body["summary"]["open_count"]) == ([], 0)


def test_get_claim_returns_none_for_an_unknown_id(client, ctx):
    # Looking up a claim that does not exist is not an error.
    with ctx.db.read() as conn:
        assert credits.get_claim(conn, "clm_does_not_exist") is None


# --- eligibility (assess) ----------------------------------------------------------------


def test_claim_is_not_eligible_before_any_contact_attempt(client, ctx, clock):
    # Angi only considers a credit if the pro tried to contact the homeowner.
    made = make_claim(client, clock)

    eligibility = get_claim(ctx, made["claim_id"])["eligibility"]
    assert eligibility["eligible"] is False
    assert eligibility["blockers"] == ["no contact attempt has been recorded for this lead yet"]
    assert eligibility["contacted_within_sla"] is False


def test_claim_is_eligible_once_the_email_went_out_within_24_hours(client, ctx, clock):
    # Our automatic email went out in time, so nothing we can check stands in the way. But
    # Angi's condition is a CALL attempt and we only recorded an email: the claim says so.
    made = make_claim(client, clock)
    contact(ctx, clock)

    eligibility = get_claim(ctx, made["claim_id"])["eligibility"]
    assert eligibility == {
        "eligible": True,
        "blockers": [],
        "notes": [CALL_NOTE],
        "days_left": 30.0,
        "contacted_within_sla": True,
    }


def test_every_claim_carries_the_call_note_whatever_its_state(client, ctx, clock, settings):
    # Not contacted, contacted in time, filed, credited: the reminder is always there, and it
    # quotes the configured number of hours.
    settings.credit_contact_sla_hours = 12
    note = CALL_NOTE.replace("24h", "12h")
    made = make_claim(client, clock)
    assert get_claim(ctx, made["claim_id"])["eligibility"]["notes"] == [note]
    assert get_claim(ctx, made["claim_id"])["eligibility"]["eligible"] is False  # no contact at all still blocks

    contact(ctx, clock)
    for status in ("detected", "submitted", "credited"):
        set_status(ctx, made["claim_id"], status)
        assert get_claim(ctx, made["claim_id"])["eligibility"]["notes"] == [note], status


def test_claim_is_blocked_when_first_contact_came_after_the_contact_sla(client, ctx, clock):
    # A first contact 25 hours after the lead arrived is too late for Angi's 24-hour condition.
    made = make_claim(client, clock)
    clock.advance(hours=25)
    ctx.worker.deliver_due()

    eligibility = get_claim(ctx, made["claim_id"])["eligibility"]
    assert eligibility["eligible"] is False
    assert eligibility["contacted_within_sla"] is False
    assert eligibility["blockers"] == ["the first contact attempt came more than 24h after the lead arrived"]


def test_contact_exactly_at_the_sla_still_counts(client, ctx, clock):
    # "Within 24 hours" includes the 24th hour itself.
    made = make_claim(client, clock)
    clock.advance(hours=24)
    ctx.worker.deliver_due()

    eligibility = get_claim(ctx, made["claim_id"])["eligibility"]
    assert eligibility["eligible"] is True
    assert eligibility["contacted_within_sla"] is True


def test_contact_sla_comes_from_the_setting(client, ctx, clock, settings):
    # credit_contact_sla_hours is configuration: with 1 hour, a contact after 2 hours blocks the claim.
    settings.credit_contact_sla_hours = 1
    made = make_claim(client, clock)
    clock.advance(hours=2)
    ctx.worker.deliver_due()

    eligibility = get_claim(ctx, made["claim_id"])["eligibility"]
    assert eligibility["blockers"] == ["the first contact attempt came more than 1h after the lead arrived"]


def test_detected_claim_is_blocked_once_the_request_deadline_has_passed(client, ctx, clock):
    # Angi accepts credit requests for 30 days; on day 31 an unfiled claim is no longer fileable.
    made = make_claim(client, clock)
    contact(ctx, clock)
    clock.advance(days=31)

    eligibility = get_claim(ctx, made["claim_id"])["eligibility"]
    assert eligibility["eligible"] is False
    assert "the deadline for requesting this credit has passed" in eligibility["blockers"]
    assert eligibility["days_left"] == -1.0


def test_submitted_claim_is_not_blocked_by_a_deadline_that_passed_afterwards(client, ctx, clock):
    # Filed in time: the deadline no longer matters while we wait for Angi's answer.
    made = make_claim(client, clock)
    contact(ctx, clock)
    set_status(ctx, made["claim_id"], "submitted")
    clock.advance(days=31)

    eligibility = get_claim(ctx, made["claim_id"])["eligibility"]
    assert eligibility["eligible"] is True
    assert eligibility["blockers"] == []
    assert eligibility["days_left"] == -1.0


def test_detected_claim_is_blocked_from_the_moment_the_deadline_passes(client, ctx, clock):
    # One hour past the deadline is past the deadline.
    made = make_claim(client, clock)
    contact(ctx, clock)
    clock.advance(days=30, hours=1)

    claim = get_claim(ctx, made["claim_id"])
    assert iso(clock()) > claim["request_deadline"]
    assert "the deadline for requesting this credit has passed" in claim["eligibility"]["blockers"]


def test_claim_is_blocked_when_the_leads_are_further_apart_than_the_window(client, ctx, clock, settings):
    # If the window is tightened after detection, a pair now outside it stops being claimable.
    made = make_claim(client, clock, gap=timedelta(days=20))
    contact(ctx, clock)
    assert get_claim(ctx, made["claim_id"])["eligibility"]["eligible"] is True

    settings.duplicate_window_days = 10
    eligibility = get_claim(ctx, made["claim_id"])["eligibility"]
    assert eligibility["eligible"] is False
    assert eligibility["blockers"] == ["the two leads are more than 10 days apart"]


def test_several_blockers_are_all_reported(client, ctx, clock):
    # Never contacted AND past the deadline: the operator sees both reasons.
    made = make_claim(client, clock)
    clock.advance(days=40)

    blockers = get_claim(ctx, made["claim_id"])["eligibility"]["blockers"]
    assert blockers == [
        "the deadline for requesting this credit has passed",
        "no contact attempt has been recorded for this lead yet",
    ]


def test_unknown_fee_is_noted_but_does_not_block(client, ctx, clock):
    # Without a fee we cannot say how much to ask for; the claim itself is still valid.
    send(client, leads_payload(fee=None))
    clock.advance(hours=1)
    second = send(client, leads_payload(fee=None))
    contact(ctx, clock)

    claim = get_claim(ctx, second["duplicate"]["creditClaimId"])
    assert claim["amount_cents"] is None
    assert claim["eligibility"]["eligible"] is True
    assert claim["eligibility"]["notes"] == [
        CALL_NOTE,
        "the lead's fee is unknown; set a default fee on the Angi account mapping",
    ]


def test_zero_fee_is_noted_as_nothing_to_credit(client, ctx, clock):
    # A free lead (fee 0) cannot be credited; say so instead of listing $0.00 as recoverable.
    send(client, leads_payload(fee=0))
    clock.advance(hours=1)
    second = send(client, leads_payload(fee=0))
    contact(ctx, clock)

    claim = get_claim(ctx, second["duplicate"]["creditClaimId"])
    assert claim["amount_cents"] == 0
    assert claim["eligibility"]["notes"] == [CALL_NOTE, "this lead was not charged, so there is nothing to credit"]


def test_medium_confidence_is_noted(client, ctx, clock):
    # Same homeowner, different service: warn that Angi may see two separate projects.
    send(client, ads_payload(PhoneNumber="3175550101", Email="one@example.com", Category="House Cleaning"))
    clock.advance(hours=1)
    second = send(client, ads_payload(PhoneNumber="3175550101", Email="two@example.com", Category="Gutter Repair"))
    contact(ctx, clock)

    claim = get_claim(ctx, second["duplicate"]["creditClaimId"])
    assert claim["confidence"] == "medium"
    assert claim["eligibility"]["eligible"] is True
    assert claim["eligibility"]["notes"] == [
        CALL_NOTE,
        "same homeowner but a different service; Angi may treat it as a separate project",
    ]


def test_days_left_counts_down_to_the_request_deadline(client, ctx, clock):
    # days_left is the time remaining until duplicate arrival + 30 days, to one decimal.
    made = make_claim(client, clock)
    assert get_claim(ctx, made["claim_id"])["eligibility"]["days_left"] == 30.0
    clock.advance(hours=12)
    assert get_claim(ctx, made["claim_id"])["eligibility"]["days_left"] == 29.5
    clock.advance(days=20)
    assert get_claim(ctx, made["claim_id"])["eligibility"]["days_left"] == 9.5
    clock.advance(days=9, hours=12)
    assert get_claim(ctx, made["claim_id"])["eligibility"]["days_left"] == 0.0


def test_days_left_follows_the_credit_request_window_setting(client, ctx, clock, settings):
    # A shorter request window shortens the countdown.
    settings.credit_request_window_days = 7
    made = make_claim(client, clock)
    assert get_claim(ctx, made["claim_id"])["eligibility"]["days_left"] == 7.0


# --- summarize ---------------------------------------------------------------------------


def test_summarize_empty_list_is_all_zeros():
    # No claims: every figure is zero and nothing is expiring.
    summary = credits.summarize([])
    assert summary == {
        "by_status": {s: {"count": 0, "amount_cents": 0} for s in credits.CLAIM_STATUSES},
        "open_count": 0,
        "claimable_cents": 0,
        "ready_to_file_count": 0,
        "ready_to_file_cents": 0,
        "recovered_cents": 0,
        "expiring_soon": [],
    }


def test_summarize_counts_and_dollars_by_status(client, ctx, clock):
    # One claim in each stage; every bucket and total must add up. Fee is $28.00 each.
    make_claim(client, clock, 1)  # stays detected and, once contacted, is ready to file
    submitted = make_claim(client, clock, 2)
    credited_full = make_claim(client, clock, 3)
    credited_part = make_claim(client, clock, 4)
    denied = make_claim(client, clock, 5)
    dismissed = make_claim(client, clock, 6)
    contact(ctx, clock)
    make_claim(client, clock, 7)  # stays detected, but its email has not gone out yet
    set_status(ctx, submitted["claim_id"], "submitted")
    set_status(ctx, credited_full["claim_id"], "credited")
    set_status(ctx, credited_part["claim_id"], "credited", credited_cents=1500)
    set_status(ctx, denied["claim_id"], "denied")
    set_status(ctx, dismissed["claim_id"], "dismissed")

    summary = credits.summarize(list_claims(ctx))

    assert summary["by_status"] == {
        "detected": {"count": 2, "amount_cents": 5600},
        "submitted": {"count": 1, "amount_cents": 2800},
        "credited": {"count": 2, "amount_cents": 5600},
        "denied": {"count": 1, "amount_cents": 2800},
        "dismissed": {"count": 1, "amount_cents": 2800},
    }
    assert summary["open_count"] == 3  # detected + submitted
    assert summary["claimable_cents"] == 8400
    # Only the detected claim that already meets Angi's conditions is ready to file.
    assert summary["ready_to_file_count"] == 1
    assert summary["ready_to_file_cents"] == 2800
    assert summary["recovered_cents"] == 2800 + 1500
    assert summary["expiring_soon"] == []


def test_summarize_lists_ready_claims_expiring_within_seven_days(client, ctx, clock):
    # A fileable claim with a week or less left is called out with its remaining days.
    soon = make_claim(client, clock, 1)
    contact(ctx, clock)
    clock.advance(days=20)
    make_claim(client, clock, 2)
    contact(ctx, clock)
    clock.advance(days=4)  # first claim: ~6 days left; second: ~26 days left

    summary = credits.summarize(list_claims(ctx))
    assert summary["expiring_soon"] == [{"claim_id": soon["claim_id"], "days_left": 6.0}]
    assert summary["ready_to_file_count"] == 2


def test_summarize_expiring_soon_boundary_is_seven_days_inclusive(client, ctx, clock):
    # Exactly 7.0 days left is "expiring soon"; 7.5 is not yet.
    made = make_claim(client, clock, 1)
    contact(ctx, clock)
    clock.advance(days=22, hours=12)
    assert credits.summarize(list_claims(ctx))["expiring_soon"] == []
    clock.advance(hours=12)
    assert credits.summarize(list_claims(ctx))["expiring_soon"] == [
        {"claim_id": made["claim_id"], "days_left": 7.0}
    ]


def test_summarize_does_not_list_expired_or_already_submitted_claims_as_expiring(client, ctx, clock):
    # "Expiring soon" is a call to action: nothing to do for a filed claim or one that is too late.
    make_claim(client, clock, 1)  # never filed: expires
    filed = make_claim(client, clock, 2)
    contact(ctx, clock)
    set_status(ctx, filed["claim_id"], "submitted")
    clock.advance(days=32)

    summary = credits.summarize(list_claims(ctx))
    assert summary["expiring_soon"] == []
    assert summary["ready_to_file_count"] == 0
    assert summary["open_count"] == 2


def test_summarize_treats_an_unknown_fee_as_zero_dollars(client, ctx, clock):
    # A claim without an amount is counted but adds nothing to the dollar totals.
    send(client, leads_payload(fee=None))
    clock.advance(hours=1)
    send(client, leads_payload(fee=None))

    summary = credits.summarize(list_claims(ctx))
    assert summary["by_status"]["detected"] == {"count": 1, "amount_cents": 0}
    assert summary["open_count"] == 1
    assert summary["claimable_cents"] == 0


# --- set_status --------------------------------------------------------------------------


def test_new_claim_starts_as_detected_with_no_timestamps(client, ctx, clock):
    # Nothing has been filed or resolved yet.
    made = make_claim(client, clock)
    claim = get_claim(ctx, made["claim_id"])
    assert (claim["status"], claim["submitted_at"], claim["resolved_at"], claim["credited_cents"]) == (
        "detected", None, None, None,
    )


def test_submitted_sets_submitted_at(client, ctx, clock):
    # Filing the request stamps when it was filed and nothing else.
    made = make_claim(client, clock)
    clock.advance(hours=3)
    claim = set_status(ctx, made["claim_id"], "submitted")

    assert claim["status"] == "submitted"
    assert claim["submitted_at"] == iso(clock())
    assert claim["resolved_at"] is None
    assert claim["credited_cents"] is None


def test_credited_defaults_to_the_full_amount(client, ctx, clock):
    # Angi normally refunds the whole fee, so that is the default.
    made = make_claim(client, clock)
    set_status(ctx, made["claim_id"], "submitted")
    filed_at = iso(clock())
    clock.advance(days=3)
    claim = set_status(ctx, made["claim_id"], "credited")

    assert claim["status"] == "credited"
    assert claim["credited_cents"] == claim["amount_cents"] == 2800
    assert claim["resolved_at"] == iso(clock())
    assert claim["submitted_at"] == filed_at  # the original filing time is kept


def test_credited_accepts_a_custom_amount(client, ctx, clock):
    # A partial credit is recorded as what was actually received.
    made = make_claim(client, clock)
    claim = set_status(ctx, made["claim_id"], "credited", credited_cents=1234)
    assert claim["credited_cents"] == 1234
    assert claim["amount_cents"] == 2800


def test_credited_with_zero_keeps_zero(client, ctx, clock):
    # An explicit $0 is not replaced by the default.
    made = make_claim(client, clock)
    claim = set_status(ctx, made["claim_id"], "credited", credited_cents=0)
    assert claim["credited_cents"] == 0


def test_credited_with_an_unknown_fee_defaults_to_zero(client, ctx, clock):
    # No fee on record and no amount supplied: recovered is $0, not NULL.
    send(client, leads_payload(fee=None))
    clock.advance(hours=1)
    second = send(client, leads_payload(fee=None))
    claim = set_status(ctx, second["duplicate"]["creditClaimId"], "credited")
    assert claim["credited_cents"] == 0


def test_credited_straight_from_detected_also_records_a_submission_time(client, ctx, clock):
    # A credit implies a request was made, even if nobody recorded the "submitted" step.
    made = make_claim(client, clock)
    claim = set_status(ctx, made["claim_id"], "credited")
    assert claim["submitted_at"] == claim["resolved_at"] == iso(clock())


@pytest.mark.parametrize("status", ["denied", "dismissed"])
def test_denied_and_dismissed_set_resolved_at_and_clear_the_credited_amount(client, ctx, clock, status):
    # A claim that ends without money must not keep a credited amount from an earlier state.
    made = make_claim(client, clock)
    set_status(ctx, made["claim_id"], "credited", credited_cents=2800)
    clock.advance(days=1)
    claim = set_status(ctx, made["claim_id"], status)

    assert claim["status"] == status
    assert claim["resolved_at"] == iso(clock())
    assert claim["credited_cents"] is None


def test_dismissing_a_detected_claim_records_no_submission(client, ctx, clock):
    # Dismissed = we decided not to file; there is no submission time.
    made = make_claim(client, clock)
    claim = set_status(ctx, made["claim_id"], "dismissed")
    assert claim["submitted_at"] is None
    assert claim["resolved_at"] == iso(clock())


def test_marking_submitted_twice_keeps_the_first_submission_time(client, ctx, clock):
    # Clicking "submitted" again a week later must not move the date the request was filed.
    made = make_claim(client, clock)
    filed_at = set_status(ctx, made["claim_id"], "submitted")["submitted_at"]
    clock.advance(days=7)
    claim = set_status(ctx, made["claim_id"], "submitted")
    assert claim["submitted_at"] == filed_at
    assert claim["status"] == "submitted"


def test_moving_a_credited_claim_back_to_submitted_removes_the_credit(client, ctx, clock):
    # A credit recorded by mistake is undone: no recovered money, open again, filing date kept.
    made = make_claim(client, clock)
    filed_at = set_status(ctx, made["claim_id"], "submitted")["submitted_at"]
    set_status(ctx, made["claim_id"], "credited")
    claim = set_status(ctx, made["claim_id"], "submitted")

    assert (claim["status"], claim["credited_cents"], claim["resolved_at"]) == ("submitted", None, None)
    assert claim["submitted_at"] == filed_at
    assert credits.summarize(list_claims(ctx))["recovered_cents"] == 0


def test_reopening_a_claim_clears_its_resolution(client, ctx, clock):
    # Moving a denied claim back to "detected" makes it open again.
    made = make_claim(client, clock)
    set_status(ctx, made["claim_id"], "denied")
    claim = set_status(ctx, made["claim_id"], "detected")
    assert claim["status"] == "detected"
    assert claim["resolved_at"] is None
    assert claim["credited_cents"] is None


@pytest.mark.parametrize("earlier", ["submitted", "credited", "denied"])
def test_back_to_detected_means_nothing_was_filed(client, ctx, clock, earlier):
    # "Detected" is the state before any request went to Angi, so it keeps no filing date,
    # no resolution and no money from whatever the claim was marked as by mistake.
    made = make_claim(client, clock)
    set_status(ctx, made["claim_id"], earlier)
    clock.advance(days=1)
    claim = set_status(ctx, made["claim_id"], "detected")

    assert (claim["status"], claim["submitted_at"], claim["resolved_at"], claim["credited_cents"]) == (
        "detected", None, None, None,
    )


def test_saving_credited_again_keeps_the_recorded_amount_and_date(client, ctx, clock):
    # Angi paid $20 of $28. Adding a note the next day must not turn that into the full fee
    # or move the day the credit was received.
    made = make_claim(client, clock)
    first = set_status(ctx, made["claim_id"], "credited", credited_cents=2000)
    clock.advance(days=1)
    again = set_status(ctx, made["claim_id"], "credited", notes="seen on the December statement")

    assert again["credited_cents"] == 2000
    assert again["resolved_at"] == first["resolved_at"]
    assert again["submitted_at"] == first["submitted_at"]
    assert again["notes"] == "seen on the December statement"


def test_a_new_amount_replaces_the_recorded_credit(client, ctx, clock):
    # Correcting the amount is still possible: an explicit value always wins.
    made = make_claim(client, clock)
    set_status(ctx, made["claim_id"], "credited", credited_cents=2000)
    assert set_status(ctx, made["claim_id"], "credited", credited_cents=2800)["credited_cents"] == 2800


def test_credited_after_another_status_defaults_to_the_full_fee_again(client, ctx, clock):
    # The kept amount belongs to a credit that is on record. Once the claim left "credited"
    # that record is gone, so crediting it again starts from the full fee.
    made = make_claim(client, clock)
    set_status(ctx, made["claim_id"], "credited", credited_cents=2000)
    set_status(ctx, made["claim_id"], "submitted")
    assert set_status(ctx, made["claim_id"], "credited")["credited_cents"] == 2800


@pytest.mark.parametrize("cents", [2801, 50000])
def test_a_credit_larger_than_the_fee_is_refused(client, ctx, clock, cents):
    # Angi cannot return more than it charged ($28.00 here).
    made = make_claim(client, clock)
    with pytest.raises(ValueError, match="cannot be larger than the fee"):
        set_status(ctx, made["claim_id"], "credited", credited_cents=cents)
    assert get_claim(ctx, made["claim_id"])["status"] == "detected"
    # The fee itself is fine.
    assert set_status(ctx, made["claim_id"], "credited", credited_cents=2800)["credited_cents"] == 2800


def test_a_credit_for_a_lead_with_an_unknown_fee_is_not_capped(client, ctx, clock):
    # With no fee on record there is nothing to compare the amount with.
    send(client, leads_payload(fee=None))
    clock.advance(hours=1)
    second = send(client, leads_payload(fee=None))
    claim = set_status(ctx, second["duplicate"]["creditClaimId"], "credited", credited_cents=2567)
    assert claim["credited_cents"] == 2567


def test_denied_after_submitted_keeps_the_filing_date(client, ctx, clock):
    # Angi said no: the request was still filed when it was filed.
    made = make_claim(client, clock)
    filed_at = set_status(ctx, made["claim_id"], "submitted")["submitted_at"]
    clock.advance(days=5)
    claim = set_status(ctx, made["claim_id"], "denied")
    assert (claim["submitted_at"], claim["resolved_at"], claim["credited_cents"]) == (filed_at, iso(clock()), None)


@pytest.mark.parametrize("status", ["", "approved", "CREDITED", "credited ", "paid"])
def test_invalid_status_is_a_value_error(client, ctx, clock, status):
    # Only the five documented statuses exist; anything else is rejected and changes nothing.
    made = make_claim(client, clock)
    with pytest.raises(ValueError, match="status must be one of"):
        set_status(ctx, made["claim_id"], status)
    assert get_claim(ctx, made["claim_id"])["status"] == "detected"


def test_unknown_claim_id_returns_none(client, ctx, clock):
    # Updating a claim that does not exist reports "not found" rather than raising.
    assert set_status(ctx, "clm_does_not_exist", "submitted") is None


def test_notes_are_kept_when_not_supplied_and_replaced_when_supplied(client, ctx, clock):
    # A later status change without notes must not wipe the case reference typed earlier.
    made = make_claim(client, clock)
    assert set_status(ctx, made["claim_id"], "submitted", notes="Angi case #12345")["notes"] == "Angi case #12345"
    assert set_status(ctx, made["claim_id"], "credited")["notes"] == "Angi case #12345"
    assert set_status(ctx, made["claim_id"], "credited", notes="credited on the Dec invoice")["notes"] == (
        "credited on the Dec invoice"
    )


def test_set_status_does_not_touch_other_claims(client, ctx, clock):
    # A status change is scoped to one claim.
    first = make_claim(client, clock, 1)
    second = make_claim(client, clock, 2)
    set_status(ctx, first["claim_id"], "credited")
    assert get_claim(ctx, second["claim_id"])["status"] == "detected"


# --- evidence text -----------------------------------------------------------------------


def test_evidence_names_both_lead_ids_the_dates_what_matched_and_the_amount(client, ctx, clock):
    # The paragraph must stand on its own when pasted into Angi's credit request form.
    made = make_claim(client, clock, gap=timedelta(minutes=90))
    evidence = get_claim(ctx, made["claim_id"])["evidence"]

    assert evidence.startswith("Duplicate lead.")
    assert f"Lead {made['duplicate_angi_id']} received 2026-11-12 16:30 UTC" in evidence
    assert f"as lead {made['original_angi_id']} received 2026-11-12 15:00 UTC" in evidence
    assert "1.5 hours later" in evidence
    assert "for the same service" in evidence
    assert "(matching phone and email)" in evidence
    assert "Homeowner: Bob Builder, 3175550001, homeowner1@example.com." in evidence
    assert "Requesting a credit of $28.00." in evidence
    assert "the homeowner on" not in evidence  # no email has gone out yet


def test_evidence_mentions_the_contact_attempt_once_made(client, ctx, clock):
    # Angi asks whether the pro tried to reach the homeowner; state when we did, and say
    # what we actually did (an email), so the text pasted into Angi's form is true.
    made = make_claim(client, clock)
    contact(ctx, clock)
    evidence = get_claim(ctx, made["claim_id"])["evidence"]
    assert "We emailed the homeowner on 2026-11-12 16:00 UTC." in evidence
    assert "contacted" not in evidence and "called" not in evidence


def test_evidence_expresses_long_gaps_in_days(client, ctx, clock):
    # Two days or more apart reads better in days than in hours.
    made = make_claim(client, clock, gap=timedelta(days=3))
    evidence = get_claim(ctx, made["claim_id"])["evidence"]
    assert "3.0 days later" in evidence
    assert "hours later" not in evidence


def test_evidence_for_a_different_service_and_single_match(client, ctx, clock):
    # No "same service" claim when the services differ, and only what really matched is named.
    send(client, ads_payload(PhoneNumber="3175550101", Email="one@example.com", Category="House Cleaning"))
    clock.advance(hours=1)
    second = send(client, ads_payload(PhoneNumber="3175550101", Email="two@example.com", Category="Gutter Repair"))

    evidence = get_claim(ctx, second["duplicate"]["creditClaimId"])["evidence"]
    assert "for the same service" not in evidence
    assert "(matching phone)" in evidence


def test_evidence_omits_the_amount_when_the_fee_is_unknown_or_zero(client, ctx, clock):
    # Never ask Angi for "$0.00" or for an amount we do not know.
    send(client, leads_payload(fee=0, srOid=808080))
    clock.advance(hours=1)
    second = send(client, leads_payload(fee=0, srOid=808080))

    evidence = get_claim(ctx, second["duplicate"]["creditClaimId"])["evidence"]
    assert "Requesting a credit" not in evidence
    assert "(matching phone and email and service_request)" in evidence


def test_evidence_text_survives_missing_fields():
    # Evidence is built defensively: a bare claim still yields a sentence instead of raising.
    text = credits.evidence_text({"matched_on": []})
    assert text == (
        "Duplicate lead. Lead (no id) received (unknown date) is the same homeowner as lead (no id) "
        "received (unknown date) (matching contact details)."
    )


# --- CSV export ---------------------------------------------------------------------------


def test_csv_header_row_is_the_documented_columns(client, ctx, clock):
    # The first row is exactly the column titles declared in CSV_COLUMNS, in order.
    header = parse_csv(credits.export_csv([]))[0]
    assert header == [title for title, _ in credits.CSV_COLUMNS]
    assert len(header) == len(set(header)) == 29
    # The columns Angi's credit form needs must be there under these names.
    for title in (
        "Claim ID",
        "Claim status",
        "Business",
        "Angi account ID",
        "Passes the checks we can verify",
        "Request credit by (UTC)",
        "Matched on",
        "Duplicate lead ID (Angi)",
        "Duplicate received (UTC)",
        "First contact attempt (UTC)",
        "Customer name",
        "Customer phone",
        "Customer email",
        "Fee charged for duplicate",
        "Original lead ID (Angi)",
        "Original received (UTC)",
        "Credit request text",
    ):
        assert title in header


def test_csv_of_no_claims_is_just_the_header(client, ctx):
    # An empty export is still a valid one-line CSV.
    text = credits.export_csv([])
    assert parse_csv(text) == [[title for title, _ in credits.CSV_COLUMNS]]
    assert text.count("\n") == 1


def test_csv_has_one_row_per_claim(client, ctx, clock):
    # Three duplicates, three data rows, in worklist (deadline) order.
    made = [make_claim(client, clock, n) for n in (1, 2, 3)]
    rows = export_rows(ctx)
    assert [row["Claim ID"] for row in rows] == [m["claim_id"] for m in made]


def test_csv_row_values(client, ctx, clock):
    # Spot-check every kind of cell: ids, yes/no flags, money, timestamps, address, text.
    made = make_claim(client, clock, gap=timedelta(minutes=90))
    contact(ctx, clock)
    row = export_rows(ctx)[0]
    duplicate = db_one(ctx, "SELECT * FROM leads WHERE id = ?", (made["duplicate_lead_id"],))

    assert row["Claim ID"] == made["claim_id"]
    assert row["Claim status"] == "detected"
    assert row["Business"] == "Sparkle Home Cleaning"
    assert row["Angi account ID"] == "123456"
    assert row["Credit reason"] == "duplicate"
    assert row["Passes the checks we can verify"] == "yes"  # not "meets Angi's conditions": see the note
    assert row["Notes"] == CALL_NOTE
    assert row["Request credit by (UTC)"] == "2026-12-12T16:30:00.000Z"
    assert row["Confidence"] == "high"
    assert row["Matched on"] == "phone, email"
    assert row["Same service requested"] == "yes"
    assert row["Hours between leads"] == "1.5"
    assert row["Duplicate lead ID (Angi)"] == made["duplicate_angi_id"]
    assert row["Duplicate received (UTC)"] == "2026-11-12T16:30:00.000Z"
    assert row["First contact attempt (UTC)"] == duplicate["first_contacted_at"] == "2026-11-12T16:30:01.000Z"
    assert row["Customer name"] == "Bob Builder"
    assert row["Customer phone"] == "3175550001"
    assert row["Customer email"] == "homeowner1@example.com"
    assert row["Service"] == "House Cleaning"
    assert row["Address"] == "123 Main St., Indianapolis, IN, 46203"
    assert row["Fee charged for duplicate"] == "$28.00"
    assert row["Original lead ID (Angi)"] == made["original_angi_id"]
    assert row["Original received (UTC)"] == "2026-11-12T15:00:00.000Z"
    assert row["Original customer name"] == "Bob Builder"
    assert row["Original service"] == "House Cleaning"
    assert row["Original fee"] == "$28.00"
    assert row["Credit request text"] == get_claim(ctx, made["claim_id"])["evidence"]


def test_csv_explains_why_a_claim_does_not_meet_the_conditions(client, ctx, clock):
    # An ineligible claim says "no" and lists the reasons in the Notes column.
    make_claim(client, clock)
    row = export_rows(ctx)[0]
    assert row["Passes the checks we can verify"] == "no"
    assert row["Notes"] == f"no contact attempt has been recorded for this lead yet; {CALL_NOTE}"
    assert row["First contact attempt (UTC)"] == ""


def test_csv_angi_leads_claim_includes_service_request_ids_and_payload_fee(client, ctx, clock):
    # For the Angi Leads feed both srOids and the fee stated in the payload are exported.
    first = leads_payload(fee=25.67)
    send(client, first)
    clock.advance(hours=1)
    second = leads_payload(fee=31.5)
    send(client, second)

    row = export_rows(ctx)[0]
    assert row["Duplicate lead ID (Angi)"] == str(second["leadOid"])
    assert row["Duplicate service request ID (Angi)"] == str(second["srOid"])
    assert row["Original lead ID (Angi)"] == str(first["leadOid"])
    assert row["Original service request ID (Angi)"] == str(first["srOid"])
    assert row["Fee charged for duplicate"] == "$31.50"
    assert row["Original fee"] == "$25.67"
    assert row["Angi account ID"] == "12345678"


def test_csv_leaves_money_cells_empty_when_the_fee_is_unknown(client, ctx, clock):
    # Unknown is exported as an empty cell, never as "$0.00" or "None".
    send(client, leads_payload(fee=None))
    clock.advance(hours=1)
    send(client, leads_payload(fee=None))

    row = export_rows(ctx)[0]
    assert row["Fee charged for duplicate"] == ""
    assert row["Original fee"] == ""
    assert "None" not in row.values()


def test_csv_values_round_trip_with_commas_quotes_and_newlines(client, ctx, clock):
    # Lead data is free text; the file must still parse into the same cells with the csv module.
    awkward = {
        "FirstName": 'Bob "The Builder", Jr.',
        "LastName": "O'Neil\nSecond line",
        "PostalAddress": {
            "AddressFirstLine": '12 "Rear" Unit, Main St.',
            "AddressSecondLine": "",
            "City": "Indianapolis",
            "State": "IN",
            "PostalCode": "46203",
        },
        "Category": 'Cleaning, "deep" kind',
    }
    made_plain = make_claim(client, clock, 1)
    made_awkward = make_claim(client, clock, 2, **awkward)

    text = credits.export_csv(list_claims(ctx))
    rows = parse_csv(text)
    assert len(rows) == 3  # header + 2 claims, although the text has more physical lines
    assert all(len(row) == len(credits.CSV_COLUMNS) for row in rows)
    by_id = {row["Claim ID"]: row for row in export_rows(ctx)}
    row = by_id[made_awkward["claim_id"]]
    stored = db_one(ctx, "SELECT * FROM leads WHERE id = ?", (made_awkward["duplicate_lead_id"],))
    assert stored["full_name"] == 'Bob "The Builder", Jr. O\'Neil\nSecond line'
    assert row["Customer name"] == stored["full_name"]
    assert row["Original customer name"] == stored["full_name"]
    assert row["Service"] == 'Cleaning, "deep" kind'
    assert row["Address"] == '12 "Rear" Unit, Main St., Indianapolis, IN, 46203'
    assert row["Credit request text"] == get_claim(ctx, made_awkward["claim_id"])["evidence"]
    assert by_id[made_plain["claim_id"]]["Customer name"] == "Bob Builder"


@pytest.mark.parametrize("prefix", ["=", "+", "-", "@"])
def test_csv_neutralises_formulas_in_the_customer_name(client, ctx, clock, prefix):
    # A name starting with a formula character must open in Excel as text, not as a formula.
    name = f'{prefix}HYPERLINK("http://evil.test","click")'
    make_claim(client, clock, FirstName=name, LastName="")

    row = export_rows(ctx)[0]
    assert row["Customer name"] == "'" + name
    assert row["Original customer name"] == "'" + name


@pytest.mark.parametrize("prefix", ["=", "+", "-", "@"])
def test_csv_neutralises_formulas_in_the_service_description(client, ctx, clock, prefix):
    # The requested service is consumer-influenced text too.
    task = f"{prefix}cmd|' /C calc'!A0"
    make_claim(client, clock, Category=task)

    row = export_rows(ctx)[0]
    assert row["Service"] == "'" + task
    assert row["Original service"] == "'" + task


def test_csv_neutralises_formulas_in_every_free_text_contact_cell(client, ctx, clock):
    # Phone, raw email and address are stored as received, so they need the same treatment.
    address = {
        "AddressFirstLine": "=1+1",
        "AddressSecondLine": "",
        "City": "Indianapolis",
        "State": "IN",
        "PostalCode": "46203",
    }
    first = send(client, ads_payload(PhoneNumber="+1 317 555 0142", Email="=SUM(A1:A9)", PostalAddress=address))
    clock.advance(hours=1)
    second = send(client, ads_payload(PhoneNumber="+1 317 555 0142", Email="=SUM(A1:A9)", PostalAddress=address))
    assert second["duplicate"]["ofLeadId"] == first["leadId"]

    row = export_rows(ctx)[0]
    assert row["Customer phone"] == "'+1 317 555 0142"
    assert row["Customer email"] == "'=SUM(A1:A9)"
    assert row["Address"] == "'=1+1, Indianapolis, IN, 46203"


def test_csv_no_cell_starts_with_a_formula_character(client, ctx, clock):
    # Belt and braces: whatever the column, no exported cell may begin with = + - or @.
    make_claim(client, clock, 1, FirstName="=A1", LastName="+B2", Category="@SUM(1)")
    make_claim(client, clock, 2, FirstName="-2+3", LastName="@x")
    for row in parse_csv(credits.export_csv(list_claims(ctx)))[1:]:
        for cell in row:
            assert cell[:1] not in ("=", "+", "-", "@", "\t", "\r"), cell


def test_csv_cell_helper_only_prefixes_dangerous_values():
    # Ordinary text, numbers and empty values are exported unchanged.
    assert credits._cell("Bob Builder") == "Bob Builder"
    assert credits._cell(None) == ""
    assert credits._cell(1.5) == "1.5"
    assert credits._cell("a=b") == "a=b"
    assert credits._cell("=1+1") == "'=1+1"
    assert credits._cell("\t=1+1") == "'\t=1+1"
    assert credits._cell(-3) == "'-3"


# --- HTTP: /admin/api/claims ---------------------------------------------------------------


def test_claims_endpoint_returns_summary_and_claims(client, ctx, clock):
    # The API returns the worklist with eligibility plus the totals shown above it.
    made = make_claim(client, clock)
    contact(ctx, clock)

    response = client.get("/admin/api/claims", headers=ADMIN)
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"summary", "claims"}
    assert [c["id"] for c in body["claims"]] == [made["claim_id"]]
    assert body["claims"][0]["eligibility"]["eligible"] is True
    assert body["claims"][0]["matched_on"] == ["phone", "email"]
    assert body["summary"]["open_count"] == 1
    assert body["summary"]["ready_to_file_cents"] == 2800


def test_claims_endpoint_with_no_claims(client):
    # An empty worklist is a normal answer, not an error.
    body = client.get("/admin/api/claims", headers=ADMIN).json()
    assert body["claims"] == []
    assert body["summary"]["open_count"] == 0


def test_claims_endpoint_honours_tenant_and_status_filters(client, ctx, clock):
    # ?tenant_id= and ?status= narrow both the list and the summary.
    sparkle = make_claim(client, clock, 1)
    summit = make_claim(client, clock, 2, ALAccountId=SUMMIT_ADS_ACCOUNT)
    set_status(ctx, summit["claim_id"], "submitted")

    by_tenant = client.get("/admin/api/claims", params={"tenant_id": "tnt_summit"}, headers=ADMIN).json()
    assert [c["id"] for c in by_tenant["claims"]] == [summit["claim_id"]]
    assert by_tenant["summary"]["by_status"]["submitted"] == {"count": 1, "amount_cents": 4500}
    assert by_tenant["summary"]["by_status"]["detected"]["count"] == 0

    by_status = client.get("/admin/api/claims", params={"status": "detected"}, headers=ADMIN).json()
    assert [c["id"] for c in by_status["claims"]] == [sparkle["claim_id"]]

    both = client.get(
        "/admin/api/claims", params={"tenant_id": "tnt_sparkle", "status": "submitted"}, headers=ADMIN
    ).json()
    assert both["claims"] == []


def test_claim_status_endpoint_marks_a_claim_submitted(client, ctx, clock):
    # The operator records that the request was filed.
    made = make_claim(client, clock)
    response = client.post(
        f"/admin/api/claims/{made['claim_id']}/status",
        headers=ADMIN,
        json={"status": "submitted", "notes": "filed in the pro portal"},
    )

    assert response.status_code == 200
    claim = response.json()["claim"]
    assert claim["status"] == "submitted"
    assert claim["submitted_at"] == iso(clock())
    assert claim["notes"] == "filed in the pro portal"
    stored = db_one(ctx, "SELECT status, notes FROM credit_claims WHERE id = ?", (made["claim_id"],))
    assert stored == {"status": "submitted", "notes": "filed in the pro portal"}


def test_claim_status_endpoint_takes_the_credited_amount_in_dollars(client, ctx, clock):
    # Dollars in the API, integer cents in the database.
    made = make_claim(client, clock)
    response = client.post(
        f"/admin/api/claims/{made['claim_id']}/status", headers=ADMIN, json={"status": "credited", "credited": 12.34}
    )
    assert response.status_code == 200
    assert response.json()["claim"]["credited_cents"] == 1234
    assert db_one(ctx, "SELECT credited_cents FROM credit_claims WHERE id = ?", (made["claim_id"],)) == {
        "credited_cents": 1234
    }


def test_claim_status_endpoint_defaults_the_credit_to_the_full_fee(client, ctx, clock):
    # No amount given: assume Angi refunded the whole fee.
    made = make_claim(client, clock)
    response = client.post(
        f"/admin/api/claims/{made['claim_id']}/status", headers=ADMIN, json={"status": "credited"}
    )
    assert response.json()["claim"]["credited_cents"] == 2800
    summary = client.get("/admin/api/claims", headers=ADMIN).json()["summary"]
    assert summary["recovered_cents"] == 2800
    assert summary["open_count"] == 0


@pytest.mark.parametrize("body", [{"status": "approved"}, {"status": ""}, {"status": "Credited"}])
def test_claim_status_endpoint_rejects_an_invalid_status_with_422(client, ctx, clock, body):
    # A typo in the status must not silently park the claim in an unknown state.
    made = make_claim(client, clock)
    response = client.post(f"/admin/api/claims/{made['claim_id']}/status", headers=ADMIN, json=body)
    assert response.status_code == 422
    assert db_one(ctx, "SELECT status FROM credit_claims WHERE id = ?", (made["claim_id"],)) == {"status": "detected"}


@pytest.mark.parametrize("body", [{}, {"credited": 5}, {"status": "credited", "credited": -1}, {"status": None}])
def test_claim_status_endpoint_rejects_a_malformed_body_with_422(client, ctx, clock, body):
    # Missing status or a negative amount is a client error.
    made = make_claim(client, clock)
    response = client.post(f"/admin/api/claims/{made['claim_id']}/status", headers=ADMIN, json=body)
    assert response.status_code == 422
    assert db_one(ctx, "SELECT status FROM credit_claims WHERE id = ?", (made["claim_id"],)) == {"status": "detected"}


def test_claim_status_endpoint_returns_404_for_an_unknown_claim(client):
    # A well-formed request for a claim that does not exist is "not found".
    response = client.post("/admin/api/claims/clm_missing/status", headers=ADMIN, json={"status": "submitted"})
    assert response.status_code == 404


def test_claim_status_endpoint_requires_the_admin_token(client, ctx, clock):
    # Changing a claim is an operator action.
    made = make_claim(client, clock)
    response = client.post(f"/admin/api/claims/{made['claim_id']}/status", json={"status": "credited"})
    assert response.status_code == 401
    assert db_one(ctx, "SELECT status FROM credit_claims WHERE id = ?", (made["claim_id"],)) == {"status": "detected"}


@pytest.mark.parametrize("amount", ["1e400", "NaN", "Infinity", "-Infinity", "1e300", "1000000001"])
def test_claim_status_endpoint_rejects_an_out_of_range_amount_with_422(client, ctx, clock, amount):
    # 1e400 parses as infinity and Python's JSON reader accepts NaN; a nonsense amount is a
    # client error, not a server crash, and the claim is left alone.
    made = make_claim(client, clock)
    response = client.post(
        f"/admin/api/claims/{made['claim_id']}/status",
        headers={**ADMIN, "Content-Type": "application/json"},
        content=f'{{"status": "credited", "credited": {amount}}}',
    )
    assert response.status_code == 422
    assert db_one(ctx, "SELECT status FROM credit_claims WHERE id = ?", (made["claim_id"],)) == {"status": "detected"}


def test_claim_status_endpoint_rejects_a_credit_larger_than_the_fee_with_422(client, ctx, clock):
    # $500 "credited" on a $28 lead is a typo; accepting it would make net spend negative.
    made = make_claim(client, clock)
    response = client.post(
        f"/admin/api/claims/{made['claim_id']}/status", headers=ADMIN, json={"status": "credited", "credited": 500}
    )
    assert response.status_code == 422
    assert response.json() == {"detail": "the credit cannot be larger than the fee paid for the lead"}
    stored = db_one(ctx, "SELECT status, credited_cents FROM credit_claims WHERE id = ?", (made["claim_id"],))
    assert stored == {"status": "detected", "credited_cents": None}


# --- HTTP: the CSV export ------------------------------------------------------------------


def test_export_endpoint_returns_a_csv_attachment(client, ctx, clock):
    # The browser must download a .csv file rather than render text.
    made = make_claim(client, clock)
    response = client.get(EXPORT_URL, headers=ADMIN)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment")
    assert 'filename="angi-duplicate-lead-credits.csv"' in disposition
    rows = parse_csv(response.text)
    assert rows[0] == [title for title, _ in credits.CSV_COLUMNS]
    assert len(rows) == 2
    assert rows[1][0] == made["claim_id"]


def test_export_endpoint_matches_the_library_export(client, ctx, clock):
    # The endpoint adds nothing and loses nothing compared with credits.export_csv.
    make_claim(client, clock, 1)
    make_claim(client, clock, 2, FirstName='Ann "A", B', LastName="=x")
    response = client.get(EXPORT_URL, headers=ADMIN)
    assert parse_csv(response.text) == parse_csv(credits.export_csv(list_claims(ctx)))


def test_export_endpoint_honours_tenant_and_status_filters(client, ctx, clock):
    # A tenant's export contains only that tenant's claims; ?status= narrows it further.
    sparkle = make_claim(client, clock, 1)
    summit = make_claim(client, clock, 2, ALAccountId=SUMMIT_ADS_ACCOUNT)
    set_status(ctx, summit["claim_id"], "credited")

    def claim_ids(**params) -> list[str]:
        response = client.get(EXPORT_URL, params=params, headers=ADMIN)
        assert response.status_code == 200
        return [row[0] for row in parse_csv(response.text)[1:]]

    assert claim_ids() == [sparkle["claim_id"], summit["claim_id"]]
    assert claim_ids(tenant_id="tnt_summit") == [summit["claim_id"]]
    assert claim_ids(tenant_id="tnt_sparkle") == [sparkle["claim_id"]]
    assert claim_ids(status="credited") == [summit["claim_id"]]
    assert claim_ids(status="detected") == [sparkle["claim_id"]]
    assert claim_ids(tenant_id="tnt_sparkle", status="credited") == []


def test_export_endpoint_with_no_matching_claims_is_just_the_header(client, ctx, clock):
    # An empty result is still a well-formed CSV download.
    make_claim(client, clock)
    for params in ({"status": "credited"}, {"tenant_id": "tnt_blueline"}):
        response = client.get(EXPORT_URL, params=params, headers=ADMIN)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")
        assert parse_csv(response.text) == [[title for title, _ in credits.CSV_COLUMNS]]


def test_export_endpoint_on_an_empty_database_is_just_the_header(client):
    # Before any duplicate exists the export is the header row alone.
    response = client.get(EXPORT_URL, headers=ADMIN)
    assert response.status_code == 200
    assert response.text.strip().splitlines() == [",".join(title for title, _ in credits.CSV_COLUMNS)]


def test_export_endpoint_requires_the_admin_token(client, ctx, clock):
    # The export is full of consumer contact details.
    make_claim(client, clock)
    response = client.get(EXPORT_URL)
    assert response.status_code == 401
    assert "homeowner1@example.com" not in response.text


def test_claims_for_every_tenant_are_recorded_separately(client, ctx, clock):
    # The same consumer duplicated at two businesses gives each business its own claim.
    make_claim(client, clock, 1)
    make_claim(client, clock, 1, ALAccountId=SUMMIT_ADS_ACCOUNT)
    tenants = [row["tenant_id"] for row in db_rows(ctx, "SELECT tenant_id FROM credit_claims ORDER BY tenant_id")]
    assert tenants == ["tnt_sparkle", "tnt_summit"]
