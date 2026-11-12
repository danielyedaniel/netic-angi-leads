"""The intake pipeline (app/ingest.py): idempotency, failure isolation, replay,
schema-drift bookkeeping, the follow-up cadence and the Angi Leads format end to end.

Most tests go through the real webhook; the concurrency tests call
``ctx.ingest.receive`` from several threads, which is what the web server does.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.context import build_context
from app.ingest import decode_body, idempotency_key
from app.main import create_app
from app.models import NormalizedLead
from app.tenants import SOURCE_ANGI, add_mapping
from app.util import iso, parse_iso
from tests.conftest import (
    ADMIN,
    API_KEY,
    START,
    ads_payload,
    db_one,
    db_rows,
    leads_payload,
    post_lead,
)

# --- helpers -----------------------------------------------------------------------------


@pytest.fixture
def make_client(settings, mailer, clock):
    """Build an application whose settings differ from the default fixture."""
    opened: list[TestClient] = []

    def build(**overrides):
        custom = Settings(_env_file=None, **{**settings.model_dump(), **overrides})
        ctx = build_context(custom, email_provider=mailer, clock=clock)
        client = TestClient(create_app(context=ctx))
        client.__enter__()
        opened.append(client)
        return ctx, client

    yield build
    for client in opened:
        client.__exit__(None, None, None)


def count(ctx, table: str, where: str = "1 = 1", params: tuple = ()) -> int:
    return db_one(ctx, f"SELECT COUNT(*) AS n FROM {table} WHERE {where}", params)["n"]


def lead_events(ctx, lead_id: str, type_: str) -> list[dict]:
    """The lead's lifecycle events of one type, oldest first, with ``data`` decoded."""
    rows = db_rows(ctx, "SELECT * FROM lead_events WHERE lead_id = ? AND type = ? ORDER BY id", (lead_id, type_))
    for row in rows:
        row["data"] = json.loads(row["data_json"]) if row["data_json"] else {}
    return rows


def outbox(ctx, lead_id: str) -> list[dict]:
    """The lead's queued messages: first touch (sequence 0) first, then follow-ups."""
    return db_rows(
        ctx, "SELECT * FROM outbox_messages WHERE lead_id = ? ORDER BY sequence, created_at, dedupe_key", (lead_id,)
    )


def get_lead(ctx, lead_id: str) -> dict:
    return db_one(ctx, "SELECT * FROM leads WHERE id = ?", (lead_id,))


def get_event(ctx, event_id: str) -> dict:
    return db_one(ctx, "SELECT * FROM webhook_events WHERE id = ?", (event_id,))


def tenant_zone(ctx, tenant_id: str) -> ZoneInfo:
    return ZoneInfo(db_one(ctx, "SELECT timezone FROM tenants WHERE id = ?", (tenant_id,))["timezone"])


def local(stamp: str, zone: ZoneInfo) -> datetime:
    return parse_iso(stamp).astimezone(zone)


def boom(*args, **kwargs):
    raise RuntimeError("boom: injected failure")


def tenant_url(ctx, tenant_id: str) -> str:
    key = db_one(ctx, "SELECT webhook_key FROM tenants WHERE id = ?", (tenant_id,))["webhook_key"]
    return f"/webhooks/angi/leads/{key}"


def open_alerts(ctx) -> dict[str, dict]:
    return {row["dedupe_key"]: row for row in db_rows(ctx, "SELECT * FROM alerts WHERE status = 'open'")}


# --- idempotency: the same Angi lead is stored once --------------------------------------


def test_same_payload_three_times_is_one_lead_and_one_first_touch(client, ctx, mailer, clock):
    # Angi retries must never create a second lead or a second intro email.
    payload = ads_payload()

    results = [post_lead(client, payload).json() for _ in range(3)]

    assert [r["result"] for r in results] == ["processed", "duplicate_delivery", "duplicate_delivery"]
    assert all(r["status"] == "success" for r in results)
    lead_id = results[0]["leadId"]
    assert [r["leadId"] for r in results] == [lead_id] * 3
    assert count(ctx, "leads") == 1
    assert count(ctx, "outbox_messages", "sequence = 0") == 1
    assert count(ctx, "outbox_messages", "template = 'followup'") == 2
    # Each redelivery is on the lead's timeline, pointing at the event that carried it.
    redelivered = lead_events(ctx, lead_id, "webhook.redelivered")
    assert [e["data"]["event_id"] for e in redelivered] == [results[1]["eventId"], results[2]["eventId"]]
    assert len(lead_events(ctx, lead_id, "lead.received")) == 1
    # All three requests are stored and linked to the one lead.
    events = db_rows(ctx, "SELECT * FROM webhook_events ORDER BY rowid")
    assert [e["status"] for e in events] == ["processed", "duplicate_delivery", "duplicate_delivery"]
    assert {e["lead_id"] for e in events} == {lead_id}

    clock.advance(seconds=1)
    ctx.worker.deliver_due()
    assert len(mailer.sent) == 1


def test_redelivery_with_changed_description_updates_the_lead_without_emailing(client, ctx, mailer, clock):
    # Same lead id, corrected content: update in place, log what changed, send nothing new.
    payload = ads_payload(Email="pat@example.com")
    first = post_lead(client, payload).json()
    clock.advance(seconds=1)
    ctx.worker.deliver_due()
    assert len(mailer.sent) == 1
    messages_before = count(ctx, "outbox_messages")
    clock.advance(minutes=5)

    response = post_lead(client, {**payload, "Description": "Changed my mind: a one-off deep clean."})

    body = response.json()
    assert response.status_code == 200
    assert body["result"] == "updated"
    assert body["leadId"] == first["leadId"]
    assert body["changed"] == ["comments"]
    assert body["email"] == {"status": "unchanged"}
    assert count(ctx, "leads") == 1
    lead = get_lead(ctx, first["leadId"])
    assert lead["comments"] == "Changed my mind: a one-off deep clean."
    assert lead["updated_at"] == iso(clock())
    assert lead["received_at"] == iso(START)  # the speed-to-lead clock is not restarted
    assert lead["status"] == "contacted"  # progress made so far is kept
    updated = lead_events(ctx, first["leadId"], "lead.updated")
    assert len(updated) == 1
    assert updated[0]["data"] == {"event_id": body["eventId"], "changed": ["comments"]}
    event = get_event(ctx, body["eventId"])
    assert event["status"] == "processed"
    assert event["status_reason"] == "updated_existing_lead"
    assert event["lead_id"] == first["leadId"]

    assert count(ctx, "outbox_messages") == messages_before
    clock.advance(seconds=1)
    ctx.worker.deliver_due()
    assert len(mailer.sent) == 1


def test_redelivery_with_changed_email_sends_one_first_touch_to_the_new_address(client, ctx, mailer, clock):
    # The intro went to the wrong address, so the corrected one gets it exactly once.
    payload = ads_payload(Email="old@example.com")
    lead_id = post_lead(client, payload).json()["leadId"]
    clock.advance(seconds=1)
    ctx.worker.deliver_due()
    assert [email.to for email in mailer.sent] == ["old@example.com"]
    corrected = {**payload, "Email": "new@example.com"}

    body = post_lead(client, corrected).json()

    assert body["result"] == "updated"
    assert body["changed"] == ["email", "email_normalized"]
    assert body["email"] == {"status": "queued", "to": "new@example.com"}
    lead = get_lead(ctx, lead_id)
    assert lead["email"] == "new@example.com"
    assert lead["email_normalized"] == "new@example.com"
    # Follow-ups that have not gone out yet now target the new address.
    follow_ups = [m for m in outbox(ctx, lead_id) if m["template"] == "followup"]
    assert len(follow_ups) == 2
    assert {m["to_address"] for m in follow_ups} == {"new@example.com"}
    assert {m["status"] for m in follow_ups} == {"pending"}

    clock.advance(seconds=1)
    ctx.worker.deliver_due()
    assert [email.to for email in mailer.sent] == ["old@example.com", "new@example.com"]

    # A further identical delivery changes nothing and sends nothing.
    again = post_lead(client, corrected).json()
    assert again["result"] == "duplicate_delivery"
    clock.advance(seconds=1)
    ctx.worker.deliver_due()
    assert len(mailer.sent) == 2
    assert count(ctx, "outbox_messages", "sequence = 0") == 2
    assert count(ctx, "leads") == 1


def test_email_corrected_before_the_intro_left_gets_exactly_one_intro(client, ctx, mailer, clock):
    # "Never a second intro": the corrected address must be introduced to once.
    payload = ads_payload(Email="old@example.com")
    post_lead(client, payload)
    post_lead(client, {**payload, "Email": "new@example.com"})

    clock.advance(seconds=1)
    ctx.worker.deliver_due()

    assert [email.to for email in mailer.sent] == ["new@example.com"]


def test_stale_redelivery_after_a_correction_does_not_reintroduce_the_old_address(client, ctx, mailer, clock):
    # An address that already received the first touch is never sent it again.
    original = ads_payload(Email="old@example.com")
    corrected = {**original, "Email": "new@example.com"}
    for payload in (original, corrected, original):  # the third is a late retry of the first
        post_lead(client, payload)
        clock.advance(seconds=1)
        ctx.worker.deliver_due()

    recipients = [email.to for email in mailer.sent]
    assert recipients.count("old@example.com") == 1


def test_correction_that_supplies_the_first_valid_email_triggers_the_first_touch(client, ctx, mailer, clock):
    # A lead that could not be emailed is contacted as soon as Angi sends a usable address.
    payload = ads_payload(Email="")
    first = post_lead(client, payload).json()
    assert first["email"]["status"] == "skipped"

    body = post_lead(client, {**payload, "Email": "found@example.com"}).json()

    assert body["result"] == "updated"
    # The same plan a lead gets that arrived with its address: the intro now, then the follow-ups.
    assert body["email"] == {
        "status": "queued",
        "template": "intro",
        "to": "found@example.com",
        "followUpsScheduled": 2,
    }
    assert [m["template"] for m in outbox(ctx, body["leadId"])] == ["intro", "followup", "followup"]
    clock.advance(seconds=1)
    ctx.worker.deliver_due()
    assert [email.to for email in mailer.sent] == ["found@example.com"]


def test_correction_that_supplies_the_first_valid_email_clears_needs_review(client, ctx):
    # The lead was flagged because nobody could be emailed. Once Angi sends a usable address
    # the flag is judged again from that delivery instead of staying set for good.
    payload = ads_payload(Email="not-an-address")
    lead_id = post_lead(client, payload).json()["leadId"]
    assert get_lead(ctx, lead_id)["needs_review"] == 1

    post_lead(client, {**payload, "Email": "found@example.com"})

    assert get_lead(ctx, lead_id)["needs_review"] == 0


def test_correction_with_an_email_keeps_needs_review_when_the_delivery_has_other_problems(client, ctx):
    # The corrected delivery itself still needs a look (a renamed field was found by alias).
    payload = ads_payload(Email="")
    lead_id = post_lead(client, payload).json()["leadId"]
    corrected = {**payload, "Email": "found@example.com"}
    corrected["Phone"] = corrected.pop("PhoneNumber")

    body = post_lead(client, corrected).json()

    assert body["result"] == "updated"
    assert get_lead(ctx, lead_id)["needs_review"] == 1


def test_correction_of_an_already_usable_email_leaves_needs_review_alone(client, ctx):
    # Only the arrival of the FIRST usable address re-opens the question; a later change of
    # address must not clear a flag that was set for another reason (here: a failed send).
    payload = ads_payload(Email="old@example.com")
    lead_id = post_lead(client, payload).json()["leadId"]
    with ctx.db.transaction() as conn:
        conn.execute("UPDATE leads SET needs_review = 1 WHERE id = ?", (lead_id,))

    post_lead(client, {**payload, "Email": "new@example.com"})

    assert get_lead(ctx, lead_id)["needs_review"] == 1


def test_sparser_redelivery_does_not_erase_stored_values(client, ctx):
    # A retry that lost fields (missing, null, empty, blank) must not wipe what we know.
    payload = ads_payload(Email="keep@example.com")
    lead_id = post_lead(client, payload).json()["leadId"]
    before = get_lead(ctx, lead_id)
    sparse = copy.deepcopy(payload)
    del sparse["Description"]
    sparse["Urgency"] = ""
    sparse["Email"] = None
    sparse["PhoneNumber"] = "   "
    sparse["Source"] = None
    sparse["PostalAddress"]["City"] = None
    sparse["PostalAddress"]["PostalCode"] = ""

    body = post_lead(client, sparse).json()

    assert body["result"] == "duplicate_delivery"
    assert body["leadId"] == lead_id
    after = get_lead(ctx, lead_id)
    for column in ("comments", "urgency", "email", "email_normalized", "phone", "phone_normalized",
                   "lead_source_detail", "city", "postal_code", "task_name", "market", "full_name"):
        assert after[column] == before[column], column
        assert after[column] is not None, column
    assert count(ctx, "leads") == 1
    assert {m["to_address"] for m in outbox(ctx, lead_id)} == {"keep@example.com"}


def test_sparser_redelivery_does_not_degrade_the_full_name(client, ctx):
    # Losing FirstName in a retry must not shorten the name we already stored.
    payload = ads_payload(FirstName="Bob", LastName="Builder")
    lead_id = post_lead(client, payload).json()["leadId"]
    sparse = copy.deepcopy(payload)
    del sparse["FirstName"]

    post_lead(client, sparse)

    lead = get_lead(ctx, lead_id)
    assert lead["first_name"] == "Bob"
    assert lead["full_name"] == "Bob Builder"


def test_same_correlation_id_under_another_account_is_a_separate_lead(client, ctx, mailer, clock):
    # Lead ids are only unique per Angi account: two businesses, two leads, two intros.
    correlation_id = "shared-correlation-id-0001"
    first = post_lead(
        client, ads_payload(CorrelationId=correlation_id, ALAccountId="123456", Email="a@example.com")
    ).json()
    second = post_lead(
        client, ads_payload(CorrelationId=correlation_id, ALAccountId="200002", Email="a@example.com")
    ).json()

    assert first["result"] == "processed"
    assert second["result"] == "processed"
    assert first["leadId"] != second["leadId"]
    assert first["tenant"]["id"] == "tnt_sparkle"
    assert second["tenant"]["id"] == "tnt_blueline"
    assert count(ctx, "leads") == 2
    # The same consumer contacting two different businesses is not a duplicate lead.
    assert count(ctx, "leads", "is_duplicate = 1") == 0
    assert count(ctx, "credit_claims") == 0

    clock.advance(seconds=1)
    ctx.worker.deliver_due()
    assert sorted(email.from_name for email in mailer.sent) == ["BlueLine Plumbing", "Sparkle Home Cleaning"]


def test_redelivery_that_lost_its_account_id_is_still_the_same_lead(client, ctx, mailer, clock):
    # Angi only "sometimes" sends spEntityId. The same leadOid without it is the same lead:
    # not a second lead, not a second email, and certainly not a credit claim.
    payload = leads_payload()
    first = post_lead(client, payload).json()
    without_account = {key: value for key, value in payload.items() if key != "spEntityId"}

    on_tenant_url = post_lead(client, without_account, headers={}, path=tenant_url(ctx, "tnt_summit")).json()
    # On the shared URL a NEW lead without an account id could not even be routed; a known one needs no routing.
    on_shared_url = post_lead(client, without_account).json()

    assert first["result"] == "processed"
    assert [on_tenant_url["result"], on_shared_url["result"]] == ["duplicate_delivery", "duplicate_delivery"]
    assert on_tenant_url["leadId"] == on_shared_url["leadId"] == first["leadId"]
    assert count(ctx, "leads") == 1
    assert count(ctx, "credit_claims") == 0
    clock.advance(seconds=1)
    ctx.worker.deliver_due()
    assert len(mailer.sent) == 1


def test_redelivery_that_gained_an_account_id_is_still_the_same_lead(client, ctx):
    # The other direction: first delivered without spEntityId (tenant URL), then with it.
    payload = leads_payload()
    without_account = {key: value for key, value in payload.items() if key != "spEntityId"}
    first = post_lead(client, without_account, headers={}, path=tenant_url(ctx, "tnt_summit")).json()

    second = post_lead(client, payload).json()

    assert first["result"] == "processed"
    assert second["result"] == "duplicate_delivery"
    assert second["leadId"] == first["leadId"]
    assert count(ctx, "leads") == 1
    assert count(ctx, "credit_claims") == 0
    assert count(ctx, "outbox_messages", "sequence = 0") == 1


def test_redelivery_parsed_as_another_format_is_still_the_same_lead(client, ctx):
    # A retry whose keys were renamed is no longer recognised as "Angi Ads", but it
    # carries the same lead id for the same account: how we parsed it must not matter.
    payload = ads_payload()
    first = post_lead(client, payload).json()
    renamed = {
        "lead_id": payload["CorrelationId"],
        "account_id": payload["ALAccountId"],
        "first_name": "Bob",
        "last_name": "Builder",
        "email": payload["Email"],
        "phone": payload["PhoneNumber"],
    }

    second = post_lead(client, renamed).json()

    assert first["format"] == "angi_ads"
    assert second["result"] == "duplicate_delivery"
    assert second["leadId"] == first["leadId"]
    assert get_event(ctx, second["eventId"])["detected_format"] == "unknown"
    assert count(ctx, "leads") == 1
    assert count(ctx, "credit_claims") == 0
    assert count(ctx, "outbox_messages", "sequence = 0") == 1


def test_lead_without_an_account_id_is_matched_by_a_later_delivery_from_any_account(client, ctx):
    # "A missing account id on either side matches any account": the stored lead is the one
    # that does not say which account it belongs to.
    payload = leads_payload()
    without_account = {key: value for key, value in payload.items() if key != "spEntityId"}
    first = post_lead(client, without_account, headers={}, path=tenant_url(ctx, "tnt_summit")).json()

    again = post_lead(client, {**payload, "spEntityId": 12345678}, headers={}, path=tenant_url(ctx, "tnt_summit")).json()

    assert again["result"] == "duplicate_delivery"
    assert again["leadId"] == first["leadId"]
    assert get_lead(ctx, first["leadId"])["idempotency_key"] == f"angi:-:{payload['leadOid']}"


def test_exact_account_match_is_preferred_when_one_lead_id_exists_under_two_accounts(client, ctx):
    # Two leads share an id under two accounts; a retry naming its account updates that one.
    correlation_id = "shared-correlation-id-0002"
    sparkle = post_lead(client, ads_payload(CorrelationId=correlation_id, ALAccountId="123456")).json()
    blueline_payload = ads_payload(CorrelationId=correlation_id, ALAccountId="200002")
    blueline = post_lead(client, blueline_payload).json()

    retry = post_lead(client, {**blueline_payload, "Urgency": "ASAP"}).json()

    assert retry["result"] == "updated"
    assert retry["leadId"] == blueline["leadId"]
    assert get_lead(ctx, sparkle["leadId"])["urgency"] == "This Week"
    assert count(ctx, "leads") == 2


def test_replay_never_creates_a_second_lead_even_if_the_ids_are_read_differently_now(client, ctx):
    # After a parser change the same stored event may yield another lead id than it did
    # when it created its lead. The event remembers its lead; a replay is still a no-op.
    first = post_lead(client, ads_payload()).json()
    with ctx.db.transaction() as conn:
        conn.execute(
            "UPDATE leads SET external_lead_id = 'read-differently-back-then', idempotency_key = 'angi:old-style-key' "
            "WHERE id = ?",
            (first["leadId"],),
        )

    outcome = ctx.ingest.process_event(first["eventId"])

    assert outcome.body["result"] == "processed"
    assert outcome.body["message"] == "Already processed."
    assert outcome.body["leadId"] == first["leadId"]
    assert count(ctx, "leads") == 1
    assert count(ctx, "outbox_messages", "sequence = 0") == 1


# --- a request that names its tenant can only touch that tenant's leads -------------------


def test_tenant_url_cannot_rewrite_another_tenants_lead(client, ctx, mailer, clock):
    # Whoever holds BlueLine's URL (no API key needed) sends Summit's lead id with their own
    # email address. Nothing may change, and nothing may be sent to that address.
    payload = leads_payload(email="homeowner@example.com")
    genuine = post_lead(client, payload).json()
    assert genuine["tenant"]["id"] == "tnt_summit"
    before = get_lead(ctx, genuine["leadId"])
    forged = {"leadOid": payload["leadOid"], "spEntityId": 12345678, "email": "attacker@example.net",
              "name": "Hannah Doe", "primaryPhone": "3035550123"}

    response = post_lead(client, forged, headers={}, path=tenant_url(ctx, "tnt_blueline"))

    body = response.json()
    assert response.status_code == 200  # stored, so a success for the sender
    assert (body["status"], body["result"], body["reason"]) == ("success", "quarantined", "tenant_conflict")
    assert "nothing was changed" in body["message"]
    assert "leadId" not in body
    assert get_lead(ctx, genuine["leadId"]) == before
    event = get_event(ctx, body["eventId"])
    assert (event["status"], event["status_reason"], event["lead_id"]) == ("quarantined", "tenant_conflict", None)
    alert = open_alerts(ctx)["tenant_conflict:tnt_blueline"]
    assert alert["severity"] == "critical"
    assert "BlueLine Plumbing" in alert["title"] and "Summit Heating & Air" in alert["title"]
    detail = json.loads(alert["detail_json"])
    assert detail["lead_id"] == genuine["leadId"]
    assert (detail["routed_tenant_id"], detail["lead_tenant_id"]) == ("tnt_blueline", "tnt_summit")
    assert count(ctx, "leads") == 1
    clock.advance(seconds=1)
    ctx.worker.deliver_due()
    assert [email.to for email in mailer.sent] == ["homeowner@example.com"]


def test_tenant_url_cannot_rewrite_another_tenants_lead_by_leaving_the_account_id_out(client, ctx):
    # The lead id alone finds the lead (a missing account id matches any account), so the
    # check is on who the request is addressed to, not on what the payload claims.
    payload = leads_payload()
    genuine = post_lead(client, payload).json()
    before = get_lead(ctx, genuine["leadId"])
    forged = {"leadOid": payload["leadOid"], "email": "attacker@example.net", "primaryPhone": "3035550123"}

    body = post_lead(client, forged, headers={}, path=tenant_url(ctx, "tnt_blueline")).json()

    assert (body["result"], body["reason"]) == ("quarantined", "tenant_conflict")
    assert get_lead(ctx, genuine["leadId"]) == before


def test_crm_key_naming_another_tenant_cannot_update_a_lead(client, ctx):
    # The same rule for the other way a request names its tenant.
    payload = leads_payload()
    genuine = post_lead(client, payload).json()
    before = get_lead(ctx, genuine["leadId"])

    body = post_lead(client, {**payload, "crmKey": "tnt_blueline", "comments": "changed"}).json()

    assert (body["result"], body["reason"]) == ("quarantined", "tenant_conflict")
    assert get_lead(ctx, genuine["leadId"]) == before


def test_correction_through_the_leads_own_tenant_url_is_still_applied(client, ctx):
    # The rule only stops cross-tenant writes; the tenant's own URL works as before.
    payload = leads_payload()
    genuine = post_lead(client, payload).json()

    body = post_lead(
        client, {**payload, "comments": "Please come after 5pm."}, headers={}, path=tenant_url(ctx, "tnt_summit")
    ).json()

    assert body["result"] == "updated"
    assert body["leadId"] == genuine["leadId"]
    assert get_lead(ctx, genuine["leadId"])["comments"] == "Please come after 5pm."
    assert count(ctx, "alerts", "kind = 'tenant_conflict'") == 0


def test_replaying_a_tenant_conflict_changes_nothing_and_counts_once(client, ctx):
    # The event stays quarantined however often it is replayed, and the alert keeps
    # counting requests, not replays.
    payload = leads_payload()
    genuine = post_lead(client, payload).json()
    before = get_lead(ctx, genuine["leadId"])
    held = post_lead(client, {**payload, "email": "other@example.net"}, headers={}, path=tenant_url(ctx, "tnt_blueline")).json()

    results = [ctx.ingest.process_event(held["eventId"]).body["result"] for _ in range(2)]
    # An operator saying "it is BlueLine's" does not make it so: the lead is Summit's.
    assigned = ctx.ingest.process_event(held["eventId"], assign_tenant_id="tnt_blueline")

    assert results == ["quarantined", "quarantined"]
    assert (assigned.body["result"], assigned.body["reason"]) == ("quarantined", "tenant_conflict")
    assert get_lead(ctx, genuine["leadId"]) == before
    assert open_alerts(ctx)["tenant_conflict:tnt_blueline"]["count"] == 1


def test_payload_without_correlation_id_is_deduplicated_by_body_hash(client, ctx):
    # With no lead id, an identical body is still recognised as Angi's retry.
    payload = ads_payload()
    del payload["CorrelationId"]
    raw = json.dumps(payload).encode("utf-8")

    first = post_lead(client, raw).json()
    second = post_lead(client, raw).json()

    assert first["result"] == "processed"
    assert second["result"] == "duplicate_delivery"
    assert second["leadId"] == first["leadId"]
    assert count(ctx, "leads") == 1
    lead = get_lead(ctx, first["leadId"])
    assert lead["external_lead_id"] is None
    assert lead["idempotency_key"] == f"angi:body:{hashlib.sha256(raw).hexdigest()}"
    assert lead["needs_review"] == 1  # a lead with no id deserves a human look

    # A different body is a different lead.
    other = json.dumps({**payload, "Description": "A completely different request."}).encode("utf-8")
    third = post_lead(client, other).json()
    assert third["result"] == "processed"
    assert third["leadId"] != first["leadId"]
    assert count(ctx, "leads") == 2
    assert count(ctx, "outbox_messages", "sequence = 0") == 2


def test_redelivery_is_recognised_when_ids_change_json_type_or_gain_spaces(client, ctx):
    # "123456", 123456 and " 123456 " are the same account; the lead stays one lead.
    payload = ads_payload(ALAccountId="123456")
    first = post_lead(client, payload).json()

    as_number = post_lead(client, {**payload, "ALAccountId": 123456}).json()
    with_spaces = post_lead(client, {**payload, "ALAccountId": " 123456 "}).json()
    padded_id = post_lead(client, {**payload, "CorrelationId": f"  {payload['CorrelationId']} "}).json()

    assert as_number["result"] == "duplicate_delivery"
    assert with_spaces["result"] == "duplicate_delivery"
    assert padded_id["result"] == "duplicate_delivery"
    assert {as_number["leadId"], with_spaces["leadId"], padded_id["leadId"]} == {first["leadId"]}
    assert count(ctx, "leads") == 1


def test_angi_leads_redelivery_with_string_lead_oid_is_the_same_lead(client, ctx):
    # leadOid is documented as a number but a string form must not create a second lead.
    payload = leads_payload()
    first = post_lead(client, payload).json()

    second = post_lead(client, {**payload, "leadOid": str(payload["leadOid"])}).json()

    assert second["result"] == "duplicate_delivery"
    assert second["leadId"] == first["leadId"]
    assert count(ctx, "leads") == 1


def test_eight_simultaneous_deliveries_of_one_lead_create_exactly_one_lead(client, ctx):
    # Concurrent retries are serialised by the database: one lead, one intro, no errors.
    raw = json.dumps(ads_payload()).encode("utf-8")
    barrier = threading.Barrier(8)

    def deliver(_: int):
        barrier.wait(timeout=30)
        return ctx.ingest.receive(raw_body=raw, headers={"content-type": "application/json"})

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(deliver, range(8)))  # re-raises anything a thread raised

    assert all(o.http_status == 200 and o.body["status"] == "success" for o in outcomes)
    results = sorted(o.body["result"] for o in outcomes)
    assert results == ["duplicate_delivery"] * 7 + ["processed"]
    assert len({o.body["leadId"] for o in outcomes}) == 1
    assert count(ctx, "leads") == 1
    assert count(ctx, "outbox_messages", "sequence = 0") == 1
    assert count(ctx, "outbox_messages", "template = 'followup'") == 2
    assert count(ctx, "webhook_events") == 8
    assert count(ctx, "webhook_events", "status = 'processed'") == 1
    assert count(ctx, "webhook_events", "status = 'duplicate_delivery'") == 7


def test_eight_simultaneous_different_leads_are_all_stored(client, ctx):
    # A burst of distinct leads loses none of them.
    bodies = [
        json.dumps(ads_payload(Email=f"burst{i}@example.com", PhoneNumber=f"317555{2000 + i}")).encode("utf-8")
        for i in range(8)
    ]
    barrier = threading.Barrier(8)

    def deliver(raw: bytes):
        barrier.wait(timeout=30)
        return ctx.ingest.receive(raw_body=raw, headers={"content-type": "application/json"})

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(deliver, bodies))

    assert [o.body["result"] for o in outcomes] == ["processed"] * 8
    assert len({o.body["leadId"] for o in outcomes}) == 8
    assert count(ctx, "leads") == 8
    assert count(ctx, "leads", "is_duplicate = 1") == 0
    assert count(ctx, "outbox_messages", "sequence = 0") == 8
    assert count(ctx, "webhook_events", "status = 'processed'") == 8


@pytest.mark.parametrize(
    "lead, sha, expected",
    [
        (
            NormalizedLead(source_format="angi_ads", external_lead_id="abc", external_account_id="123456"),
            "f00d",
            "angi:123456:abc",
        ),
        # The detected format is not part of the key: the same lead parsed differently is the same lead.
        (
            NormalizedLead(source_format="unknown", external_lead_id="abc", external_account_id="123456"),
            "f00d",
            "angi:123456:abc",
        ),
        (NormalizedLead(source_format="angi_leads", external_lead_id="987"), "f00d", "angi:-:987"),
        (NormalizedLead(source_format="angi_ads", external_account_id="123456"), "f00d", "angi:body:f00d"),
        (NormalizedLead(source_format="unknown", external_lead_id=""), "f00d", "angi:body:f00d"),
    ],
)
def test_idempotency_key_shapes(lead, sha, expected):
    # Lead id scoped to the Angi account; body hash when there is no lead id.
    assert idempotency_key(lead, sha) == expected


@pytest.mark.parametrize(
    "raw, payload, envelope, error_prefix",
    [
        ('{"a": 1}', {"a": 1}, (), None),
        ('[{"a": 1}]', {"a": 1}, ("array",), None),
        ("", None, (), "request body is empty"),
        (" \n ", None, (), "request body is empty"),
        ("[]", None, (), "expected a JSON object, got array"),
        ('[{"a": 1}, {"b": 2}]', None, (), "expected a JSON object, got array"),
        ('"text"', None, (), "expected a JSON object, got string"),
        ("7", None, (), "expected a JSON object, got number"),
        ("null", None, (), "expected a JSON object, got null"),
        ("garbage", None, (), "request body is not valid JSON"),
    ],
)
def test_decode_body(raw, payload, envelope, error_prefix):
    # Exactly one JSON object (optionally wrapped in a one-element array) is usable.
    decoded, found_envelope, error = decode_body(raw)

    assert decoded == payload
    assert found_envelope == envelope
    if error_prefix is None:
        assert error is None
    else:
        assert error.startswith(error_prefix)


# --- failure isolation: a stored request is never lost -----------------------------------


def test_processing_failure_is_acknowledged_rolled_back_and_retried_later(client, ctx, mailer, clock, monkeypatch):
    # A bug after "stored" still answers Angi with success, leaves no half-written rows,
    # and the retry creates the lead with its ORIGINAL arrival time.
    monkeypatch.setattr("app.ingest.find_duplicate", boom)
    payload = ads_payload(Email="resilient@example.com")

    response = post_lead(client, payload)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["result"] == "accepted"
    event = get_event(ctx, body["eventId"])
    assert event["status"] == "failed"
    assert event["status_reason"] == "RuntimeError: boom: injected failure"
    assert event["attempts"] == 1
    assert json.loads(event["raw_body"]) == payload  # the request itself is safe on disk
    alerts = db_rows(ctx, "SELECT * FROM alerts")
    assert len(alerts) == 1
    assert alerts[0]["kind"] == "processing_error"
    assert alerts[0]["severity"] == "critical"
    assert alerts[0]["status"] == "open"
    assert json.loads(alerts[0]["detail_json"])["event_id"] == body["eventId"]
    # The transaction rolled back: nothing half-written.
    for table in ("leads", "lead_events", "outbox_messages", "credit_claims", "payload_shapes"):
        assert count(ctx, table) == 0, table

    monkeypatch.undo()  # the bug is fixed
    clock.advance(minutes=7)
    assert ctx.ingest.retry_unfinished(older_than_seconds=0) == 1

    event = get_event(ctx, body["eventId"])
    assert event["status"] == "processed"
    assert event["attempts"] == 2
    lead = db_one(ctx, "SELECT * FROM leads")
    assert event["lead_id"] == lead["id"]
    assert lead["received_at"] == iso(START)
    assert lead["created_at"] == iso(START + timedelta(minutes=7))
    assert lead["first_event_id"] == body["eventId"]
    first_touch = [m for m in outbox(ctx, lead["id"]) if m["sequence"] == 0]
    assert len(first_touch) == 1
    assert first_touch[0]["template"] == "intro"
    assert first_touch[0]["status"] == "pending"
    assert first_touch[0]["send_after"] == iso(clock())

    clock.advance(seconds=1)
    ctx.worker.deliver_due()
    assert [email.to for email in mailer.sent] == ["resilient@example.com"]
    # Speed-to-lead is measured from when Angi delivered, not from the retry.
    sent = lead_events(ctx, lead["id"], "email.sent")
    assert sent[0]["data"]["seconds_since_received"] == 7 * 60 + 1

    # Nothing left to retry.
    assert ctx.ingest.retry_unfinished(older_than_seconds=0) == 0


@pytest.mark.parametrize(
    "failing_step",
    [
        "app.ingest.parse_angi_payload",  # before the transaction
        "app.ingest.resolve_tenant",  # after the payload shape was recorded
        "app.ingest.find_duplicate",
        "app.ingest.add_lead_event",  # after the lead row was inserted
        "app.outbox.enqueue",  # after the lead and its timeline were inserted
    ],
)
def test_failure_at_any_step_leaves_nothing_behind_and_retry_completes_it(
    client, ctx, clock, monkeypatch, failing_step
):
    # Wherever processing breaks, the outcome is the same: accepted, clean rollback, retryable.
    monkeypatch.setattr(failing_step, boom)

    response = post_lead(client, ads_payload())

    assert response.status_code == 200
    assert response.json()["status"] == "success"
    assert response.json()["result"] == "accepted"
    event = db_one(ctx, "SELECT * FROM webhook_events")
    assert event["status"] == "failed"
    assert event["status_reason"].startswith("RuntimeError: boom")
    for table in ("leads", "lead_events", "outbox_messages", "credit_claims", "payload_shapes"):
        assert count(ctx, table) == 0, table
    alert = db_one(ctx, "SELECT * FROM alerts WHERE kind = 'processing_error'")
    assert alert["severity"] == "critical" and alert["status"] == "open" and alert["count"] == 1

    monkeypatch.undo()
    clock.advance(seconds=1)
    assert ctx.ingest.retry_unfinished(older_than_seconds=0) == 1
    assert db_one(ctx, "SELECT status FROM webhook_events")["status"] == "processed"
    assert count(ctx, "leads") == 1
    assert count(ctx, "outbox_messages", "sequence = 0") == 1
    assert count(ctx, "outbox_messages", "template = 'followup'") == 2


def test_failure_on_a_tenant_url_also_rolls_back_the_learned_mapping(client, ctx, monkeypatch):
    # The account mapping learned from a tenant URL is part of the same transaction.
    key = db_one(ctx, "SELECT webhook_key FROM tenants WHERE id = 'tnt_summit'")["webhook_key"]
    mappings_before = count(ctx, "tenant_lead_sources")
    monkeypatch.setattr("app.ingest.find_duplicate", boom)

    response = post_lead(client, ads_payload(ALAccountId="808080"), headers={}, path=f"/webhooks/angi/leads/{key}")

    assert response.json()["result"] == "accepted"
    assert count(ctx, "tenant_lead_sources") == mappings_before
    assert count(ctx, "leads") == 0


def test_event_that_keeps_failing_is_retried_up_to_max_attempts_then_left_alone(client, ctx, clock, monkeypatch):
    # A poison event must not be retried forever; after 5 attempts it waits for a human.
    monkeypatch.setattr("app.ingest.find_duplicate", boom)
    event_id = post_lead(client, ads_payload()).json()["eventId"]
    assert get_event(ctx, event_id)["attempts"] == 1

    for expected_attempts in (2, 3, 4, 5):
        clock.advance(seconds=1)
        assert ctx.ingest.retry_unfinished(older_than_seconds=0) == 1
        assert get_event(ctx, event_id)["attempts"] == expected_attempts

    clock.advance(seconds=1)
    assert ctx.ingest.retry_unfinished(older_than_seconds=0) == 0
    event = get_event(ctx, event_id)
    assert event["status"] == "failed"
    assert event["attempts"] == 5
    assert count(ctx, "leads") == 0
    # Still one alert. Its counter is leads affected, so five attempts at one lead count once.
    assert db_one(ctx, "SELECT count FROM alerts WHERE kind = 'processing_error'")["count"] == 1
    assert count(ctx, "alerts", "kind = 'processing_error'") == 1

    # Once the bug is fixed an operator can still replay it by hand.
    monkeypatch.undo()
    replay = client.post(f"/admin/api/events/{event_id}/replay", headers=ADMIN)
    assert replay.status_code == 200
    assert replay.json()["result"] == "processed"
    assert count(ctx, "leads") == 1
    assert db_one(ctx, "SELECT received_at FROM leads")["received_at"] == iso(START)


def test_retry_unfinished_honours_a_custom_max_attempts(client, ctx, clock, monkeypatch):
    # The attempt budget is a parameter, not a constant.
    monkeypatch.setattr("app.ingest.find_duplicate", boom)
    post_lead(client, ads_payload())

    clock.advance(seconds=1)
    assert ctx.ingest.retry_unfinished(older_than_seconds=0, max_attempts=2) == 1
    clock.advance(seconds=1)
    assert ctx.ingest.retry_unfinished(older_than_seconds=0, max_attempts=2) == 0
    assert db_one(ctx, "SELECT attempts FROM webhook_events")["attempts"] == 2


def test_retry_unfinished_waits_until_the_event_is_older_than_the_cutoff(client, ctx, clock, monkeypatch):
    # A request that may still be in flight is not grabbed by the retry loop.
    monkeypatch.setattr("app.ingest.find_duplicate", boom)
    post_lead(client, ads_payload())
    monkeypatch.undo()

    clock.advance(seconds=10)
    assert ctx.ingest.retry_unfinished(older_than_seconds=30) == 0
    assert count(ctx, "leads") == 0

    clock.advance(seconds=21)
    assert ctx.ingest.retry_unfinished(older_than_seconds=30) == 1
    assert count(ctx, "leads") == 1


def test_retry_unfinished_ignores_finished_events(client, ctx, clock):
    # Processed, duplicate, quarantined and rejected events are not "unfinished".
    payload = ads_payload()
    post_lead(client, payload)
    post_lead(client, payload)
    post_lead(client, ads_payload(ALAccountId="424242"))
    post_lead(client, b"not json")
    before = db_rows(ctx, "SELECT id, status, attempts FROM webhook_events ORDER BY id")

    clock.advance(hours=1)
    assert ctx.ingest.retry_unfinished(older_than_seconds=0) == 0

    assert db_rows(ctx, "SELECT id, status, attempts FROM webhook_events ORDER BY id") == before


def test_event_left_in_received_by_a_crash_is_picked_up_by_maintenance(client, ctx, mailer, clock):
    # A crash between "stored" and "processed" loses nothing: maintenance finishes the job.
    raw = json.dumps(ads_payload(Email="crash@example.com"))
    with ctx.db.transaction() as conn:
        conn.execute(
            """INSERT INTO webhook_events (id, source, received_at, headers_json, raw_body, body_sha256, status)
               VALUES ('evt_crashed', 'angi', ?, '{}', ?, ?, 'received')""",
            (iso(clock()), raw, hashlib.sha256(raw.encode("utf-8")).hexdigest()),
        )

    # Too fresh: it might still be in flight on another thread.
    clock.advance(seconds=10)
    ctx.worker.run_maintenance()
    assert get_event(ctx, "evt_crashed")["status"] == "received"
    assert count(ctx, "leads") == 0

    clock.advance(seconds=21)
    ctx.worker.run_maintenance()
    event = get_event(ctx, "evt_crashed")
    assert event["status"] == "processed"
    assert event["attempts"] == 1
    lead = db_one(ctx, "SELECT * FROM leads")
    assert lead["received_at"] == iso(START)
    assert lead["first_event_id"] == "evt_crashed"
    assert lead["tenant_id"] == "tnt_sparkle"

    ctx.worker.deliver_due()
    assert [email.to for email in mailer.sent] == ["crash@example.com"]


def test_request_whose_processing_never_ran_is_recovered_by_maintenance(client, ctx, clock, monkeypatch):
    # Same guarantee through the real webhook: the process dies right after step 1.
    def killed(event_id: str):
        raise RuntimeError("process killed")

    monkeypatch.setattr(ctx.ingest, "process_event", killed)
    with pytest.raises(RuntimeError, match="process killed"):
        post_lead(client, ads_payload(Email="survivor@example.com"))
    monkeypatch.undo()

    event = db_one(ctx, "SELECT * FROM webhook_events")
    assert event["status"] == "received"
    assert event["attempts"] == 0
    assert count(ctx, "leads") == 0

    clock.advance(seconds=31)
    ctx.worker.run_maintenance()

    assert db_one(ctx, "SELECT status FROM webhook_events")["status"] == "processed"
    lead = db_one(ctx, "SELECT * FROM leads")
    assert lead["email_normalized"] == "survivor@example.com"
    assert lead["received_at"] == iso(START)


def test_lone_surrogate_in_a_text_field_does_not_cost_the_lead(client, ctx):
    # One broken character in a free-text field must not make the whole lead unprocessable.
    raw = json.dumps(ads_payload(Description="broken emoji here")).replace("broken emoji", "broken \\ud83d emoji")

    response = post_lead(client, raw)

    assert response.status_code == 200
    assert response.json()["result"] == "processed"
    assert count(ctx, "leads") == 1
    assert count(ctx, "webhook_events", "status = 'failed'") == 0


def test_absurd_fee_does_not_cost_the_lead(client, ctx):
    # A nonsensical price is a data-quality issue on one field, not a reason to drop the lead.
    response = post_lead(client, leads_payload(fee=1e30))

    assert response.status_code == 200
    assert response.json()["result"] == "processed"
    assert count(ctx, "leads") == 1
    assert count(ctx, "webhook_events", "status = 'failed'") == 0


# --- replay ------------------------------------------------------------------------------


def test_replaying_an_already_processed_event_is_a_noop(client, ctx, mailer, clock):
    # Replay is idempotent: no second lead, no second email, result still "processed".
    first = post_lead(client, ads_payload()).json()
    clock.advance(seconds=1)
    ctx.worker.deliver_due()
    assert len(mailer.sent) == 1
    messages_before = db_rows(ctx, "SELECT id, status, to_address, send_after FROM outbox_messages ORDER BY id")
    clock.advance(minutes=10)

    response = client.post(f"/admin/api/events/{first['eventId']}/replay", headers=ADMIN)

    assert response.status_code == 200
    body = response.json()
    assert body["httpStatus"] == 200
    assert body["status"] == "success"
    assert body["result"] == "processed"
    assert body["leadId"] == first["leadId"]
    assert count(ctx, "leads") == 1
    assert db_rows(ctx, "SELECT id, status, to_address, send_after FROM outbox_messages ORDER BY id") == messages_before
    event = get_event(ctx, first["eventId"])
    assert event["status"] == "processed"
    assert event["lead_id"] == first["leadId"]
    assert event["attempts"] == 2
    lead = get_lead(ctx, first["leadId"])
    assert lead["received_at"] == iso(START)
    assert lead["status"] == "contacted"
    assert len(lead_events(ctx, first["leadId"], "lead.received")) == 1
    assert lead_events(ctx, first["leadId"], "webhook.redelivered") == []

    ctx.worker.deliver_due()
    assert len(mailer.sent) == 1


def test_replay_requires_the_admin_token_and_an_existing_event(client, ctx):
    # Replay is an operator action.
    event_id = post_lead(client, ads_payload()).json()["eventId"]

    assert client.post(f"/admin/api/events/{event_id}/replay").status_code == 401
    assert client.post("/admin/api/events/evt_does_not_exist/replay", headers=ADMIN).status_code == 404
    assert get_event(ctx, event_id)["attempts"] == 1


def test_replaying_a_duplicate_delivery_event_creates_nothing(client, ctx):
    # Replaying the retry of a lead is as harmless as the retry was.
    payload = ads_payload()
    post_lead(client, payload)
    second = post_lead(client, payload).json()

    body = client.post(f"/admin/api/events/{second['eventId']}/replay", headers=ADMIN).json()

    assert body["result"] == "duplicate_delivery"
    assert count(ctx, "leads") == 1
    assert count(ctx, "outbox_messages", "sequence = 0") == 1


def test_replaying_a_rejected_event_stays_rejected(client, ctx):
    # A body that was never JSON does not become a lead by being replayed.
    event_id = post_lead(client, b"still not json").json()["eventId"]

    body = client.post(f"/admin/api/events/{event_id}/replay", headers=ADMIN).json()

    assert body["httpStatus"] == 400
    assert body["status"] == "error"
    assert get_event(ctx, event_id)["status"] == "rejected"
    assert count(ctx, "leads") == 0


def test_replay_quarantined_processes_newly_mapped_leads_and_leaves_unrecognised_ones(client, ctx, mailer, clock):
    # After the mapping is added, the waiting lead flows through; junk stays for a human.
    unmapped = post_lead(client, ads_payload(ALAccountId="555000", Email="later@example.com")).json()
    junk = post_lead(client, {"foo": "bar"}).json()
    assert unmapped["result"] == "quarantined" and junk["result"] == "quarantined"
    with ctx.db.transaction() as conn:
        add_mapping(
            conn, tenant_id="tnt_blueline", source=SOURCE_ANGI, external_account_id="555000",
            external_account_name="BlueLine North", now=clock(),
        )
    clock.advance(minutes=30)

    results = ctx.ingest.replay_quarantined()

    assert {r["eventId"]: r["result"] for r in results} == {
        unmapped["eventId"]: "processed",
        junk["eventId"]: "quarantined",
    }
    assert get_event(ctx, unmapped["eventId"])["status"] == "processed"
    assert get_event(ctx, junk["eventId"])["status"] == "quarantined"
    assert get_event(ctx, junk["eventId"])["status_reason"] == "unrecognised_payload"
    lead = db_one(ctx, "SELECT * FROM leads")
    assert lead["tenant_id"] == "tnt_blueline"
    assert lead["received_at"] == iso(START)
    assert count(ctx, "leads") == 1

    ctx.worker.deliver_due()
    assert [email.to for email in mailer.sent] == ["later@example.com"]


def test_replay_quarantined_can_be_limited_to_one_reason(client, ctx):
    # The admin endpoint replays only what the given fix could have unblocked.
    unmapped = post_lead(client, ads_payload(ALAccountId="555000")).json()
    junk = post_lead(client, {"foo": "bar"}).json()

    response = client.post("/admin/api/events/replay-quarantined", params={"reason": "unmapped_account"}, headers=ADMIN)

    assert response.status_code == 200
    assert response.json() == {
        "replayed": 1,
        "results": [{"eventId": unmapped["eventId"], "result": "quarantined"}],
    }
    assert get_event(ctx, unmapped["eventId"])["attempts"] == 2
    assert get_event(ctx, junk["eventId"])["attempts"] == 1


def test_two_quarantined_deliveries_of_one_lead_replay_into_one_lead(client, ctx, clock):
    # Idempotency also holds for leads that were waiting in quarantine.
    payload = ads_payload(ALAccountId="555000")
    first = post_lead(client, payload).json()
    clock.advance(minutes=15)
    second = post_lead(client, payload).json()
    with ctx.db.transaction() as conn:
        add_mapping(
            conn, tenant_id="tnt_blueline", source=SOURCE_ANGI, external_account_id="555000",
            external_account_name=None, now=clock(),
        )
    clock.advance(minutes=15)

    results = ctx.ingest.replay_quarantined("unmapped_account")

    assert results == [
        {"eventId": first["eventId"], "result": "processed"},
        {"eventId": second["eventId"], "result": "duplicate_delivery"},
    ]
    assert count(ctx, "leads") == 1
    lead = db_one(ctx, "SELECT * FROM leads")
    assert lead["first_event_id"] == first["eventId"]
    assert lead["received_at"] == iso(START)
    assert count(ctx, "outbox_messages", "sequence = 0") == 1

    # Running the replay again finds nothing left to do.
    assert ctx.ingest.replay_quarantined() == []


def test_replaying_an_older_delivery_does_not_overwrite_a_newer_correction(client, ctx, clock):
    # The most recently received version of a lead wins, whatever order events are processed in.
    payload = ads_payload(ALAccountId="555000", Description="First version")
    old = post_lead(client, payload).json()
    assert old["result"] == "quarantined"  # account not mapped yet
    clock.advance(minutes=10)
    summit_url = "/webhooks/angi/leads/" + db_one(ctx, "SELECT webhook_key FROM tenants WHERE id = 'tnt_summit'")["webhook_key"]
    new = post_lead(client, {**payload, "Description": "Corrected version"}, headers={}, path=summit_url).json()
    assert new["result"] == "processed"  # the tenant URL routes it (and learns the account)
    clock.advance(minutes=10)

    ctx.ingest.replay_quarantined()  # what adding a mapping does automatically

    assert get_lead(ctx, new["leadId"])["comments"] == "Corrected version"


# --- re-processing an event does not count it again ---------------------------------------


def test_replays_do_not_inflate_alert_counters_or_shape_counts(client, ctx):
    # The counters say how many leads were affected. One quarantined lead replayed three
    # times (which is what adding unrelated mappings used to do) is still one lead.
    held = post_lead(client, ads_payload(ALAccountId="777777", LoyaltyTier="gold")).json()
    assert held["result"] == "quarantined"

    for _ in range(3):
        assert ctx.ingest.replay_quarantined() == [{"eventId": held["eventId"], "result": "quarantined"}]

    alerts = open_alerts(ctx)
    assert alerts["unmapped_account:777777"]["count"] == 1
    assert [a["count"] for a in alerts.values() if a["kind"] == "schema_drift"] == [1]
    assert db_one(ctx, "SELECT seen_count FROM payload_shapes")["seen_count"] == 1
    assert get_event(ctx, held["eventId"])["attempts"] == 4  # the passes themselves are still on record


def test_second_lead_of_the_same_kind_still_counts(client, ctx):
    # A new request is a new occurrence; only re-runs of the same request are not.
    first = post_lead(client, ads_payload(ALAccountId="777777", LoyaltyTier="gold")).json()
    ctx.ingest.process_event(first["eventId"])
    post_lead(client, ads_payload(ALAccountId="777777", LoyaltyTier="silver"))

    assert open_alerts(ctx)["unmapped_account:777777"]["count"] == 2
    assert db_one(ctx, "SELECT seen_count FROM payload_shapes")["seen_count"] == 2


def test_replaying_unrecognised_and_rejected_events_counts_them_once(client, ctx):
    # The same rule for the two kinds of request that never become a lead.
    junk = post_lead(client, {"foo": "bar"}).json()
    garbage = post_lead(client, b"not json at all").json()

    for event_id in (junk["eventId"], garbage["eventId"]):
        ctx.ingest.process_event(event_id)
        ctx.ingest.process_event(event_id)

    alerts = open_alerts(ctx)
    assert alerts["unrecognised_payload"]["count"] == 1
    assert alerts["invalid_payload"]["count"] == 1


def test_replay_reopens_an_alert_that_was_resolved_while_the_problem_remains(client, ctx, clock):
    # Not counting is not the same as not alerting: a replay that still cannot route the
    # lead puts the problem back in front of the operator.
    held = post_lead(client, ads_payload(ALAccountId="777777")).json()
    with ctx.db.transaction() as conn:
        conn.execute("UPDATE alerts SET status = 'resolved'")

    ctx.ingest.process_event(held["eventId"])

    assert open_alerts(ctx)["unmapped_account:777777"]["count"] == 1


# --- process_event never raises ------------------------------------------------------------


def test_request_is_acknowledged_even_when_the_failure_itself_cannot_be_recorded(client, ctx, mailer, clock, monkeypatch):
    # The database stays locked after the request was stored: processing fails, and so does
    # writing down that it failed. The sender still gets its success (the request is on
    # disk), and the event simply stays "received" for the retry loop.
    real_transaction = ctx.db.transaction
    writes = {"count": 0}

    @contextmanager
    def locked_after_the_request_is_stored():
        writes["count"] += 1
        if writes["count"] > 1:
            raise sqlite3.OperationalError("database is locked")
        with real_transaction() as conn:
            yield conn

    monkeypatch.setattr(ctx.db, "transaction", locked_after_the_request_is_stored)

    response = post_lead(client, ads_payload(Email="locked@example.com"))

    assert response.status_code == 200
    body = response.json()
    assert (body["status"], body["result"]) == ("success", "accepted")
    assert writes["count"] == 3  # store, process, record the failure
    event = get_event(ctx, body["eventId"])
    assert (event["status"], event["attempts"]) == ("received", 0)
    assert count(ctx, "leads") == 0

    monkeypatch.undo()  # the lock is gone
    clock.advance(seconds=31)
    ctx.worker.run_maintenance()
    assert get_event(ctx, body["eventId"])["status"] == "processed"
    ctx.worker.deliver_due()
    assert [email.to for email in mailer.sent] == ["locked@example.com"]


def test_process_event_does_not_raise_for_an_event_that_does_not_exist(ctx):
    # Even the most basic failure is an answer, not an exception.
    outcome = ctx.ingest.process_event("evt_does_not_exist")

    assert outcome.http_status == 200
    assert outcome.body["result"] == "accepted"


# --- a learned mapping releases the leads that were waiting for it -------------------------


def test_learned_mapping_releases_the_leads_quarantined_for_that_account(client, ctx, mailer, clock):
    # A lead for an unknown account waits in quarantine. When the tenant's own URL then
    # teaches us whose account that is, the waiting lead must not keep waiting for an
    # operator: it is exactly the mapping it was waiting for.
    waiting = post_lead(client, ads_payload(ALAccountId="777001", Email="first@example.com", PhoneNumber="3175550101")).json()
    other = post_lead(client, ads_payload(ALAccountId="777002")).json()
    assert waiting["result"] == other["result"] == "quarantined"
    clock.advance(minutes=20)

    taught = post_lead(
        client,
        ads_payload(ALAccountId="777001", Email="second@example.com", PhoneNumber="3175550102"),
        headers={},
        path=tenant_url(ctx, "tnt_blueline"),
    ).json()

    assert taught["result"] == "processed"
    assert taught["released"] == [{"eventId": waiting["eventId"], "result": "processed"}]
    released = get_event(ctx, waiting["eventId"])
    assert (released["status"], released["tenant_id"]) == ("processed", "tnt_blueline")
    lead = get_lead(ctx, released["lead_id"])
    assert lead["routed_by"] == "account_id"
    assert lead["received_at"] == iso(START)  # its own arrival time, not the release time
    # The alert for that account is over; the other account is still waiting.
    alerts = open_alerts(ctx)
    assert "unmapped_account:777001" not in alerts
    assert "unmapped_account:777002" in alerts
    assert get_event(ctx, other["eventId"])["status"] == "quarantined"
    assert get_event(ctx, other["eventId"])["attempts"] == 1
    clock.advance(seconds=1)
    ctx.worker.deliver_due()
    assert sorted(email.to for email in mailer.sent) == ["first@example.com", "second@example.com"]
    assert {email.from_name for email in mailer.sent} == {"BlueLine Plumbing"}


def test_lead_that_learns_nothing_new_reports_no_released_leads(client, ctx):
    # The everyday case: nothing was waiting, and the answer does not mention it.
    body = post_lead(client, ads_payload(), headers={}, path=tenant_url(ctx, "tnt_sparkle")).json()

    assert body["result"] == "processed"
    assert "released" not in body


def test_released_lead_is_paired_with_the_lead_that_released_it(client, ctx, clock):
    # Same consumer both times: the lead that arrived first is the original, the one that
    # taught us the mapping (and was processed first) is the duplicate with the claim.
    waiting = post_lead(client, ads_payload(ALAccountId="777001")).json()
    clock.advance(minutes=30)

    taught = post_lead(client, ads_payload(ALAccountId="777001"), headers={}, path=tenant_url(ctx, "tnt_blueline")).json()

    original = get_lead(ctx, get_event(ctx, waiting["eventId"])["lead_id"])
    duplicate = get_lead(ctx, taught["leadId"])
    assert original["is_duplicate"] == 0
    assert duplicate["duplicate_of_lead_id"] == original["id"]
    claims = db_rows(ctx, "SELECT lead_id, original_lead_id, hours_apart FROM credit_claims")
    assert claims == [{"lead_id": taught["leadId"], "original_lead_id": original["id"], "hours_apart": 0.5}]


# --- a payload that names no account -------------------------------------------------------


def test_payload_without_an_account_id_is_quarantined_with_a_message_that_can_be_kept(client, ctx):
    # No account id and no tenant URL / crmKey: no mapping could ever release this lead, so
    # the answer must not promise that one will.
    payload = ads_payload()
    del payload["ALAccountId"]

    body = post_lead(client, payload).json()

    assert (body["status"], body["result"], body["reason"]) == ("success", "quarantined", "no_account_id")
    assert "operator has to assign" in body["message"]
    assert "automatically" not in body["message"]
    assert body["angiAccountId"] is None
    event = get_event(ctx, body["eventId"])
    assert (event["status"], event["status_reason"]) == ("quarantined", "no_account_id")
    alert = open_alerts(ctx)["no_account_id"]
    assert (alert["kind"], alert["severity"]) == ("no_account_id", "critical")
    assert "assign" in alert["title"]
    # It is not listed as an account waiting to be mapped.
    assert count(ctx, "alerts", "kind = 'unmapped_account'") == 0


def test_adding_a_mapping_does_not_churn_through_leads_without_an_account_id(client, ctx):
    # Mapping some account cannot help a lead that names none, so it is not re-run.
    payload = ads_payload()
    del payload["ALAccountId"]
    held = post_lead(client, payload).json()

    response = client.post(
        "/admin/api/tenants/tnt_sparkle/lead-sources", json={"external_account_id": "777003"}, headers=ADMIN
    )

    assert response.json()["replayed"] == []
    assert get_event(ctx, held["eventId"])["attempts"] == 1
    assert open_alerts(ctx)["no_account_id"]["count"] == 1


def test_operator_can_assign_a_lead_without_an_account_id_to_a_tenant(client, ctx, mailer, clock):
    # The way out of that quarantine: a person says which business the lead is for.
    payload = ads_payload(Email="assigned@example.com")
    del payload["ALAccountId"]
    held = post_lead(client, payload).json()
    clock.advance(minutes=45)

    outcome = ctx.ingest.process_event(held["eventId"], assign_tenant_id="tnt_sparkle")

    assert outcome.http_status == 200
    assert outcome.body["result"] == "processed"
    assert outcome.body["tenant"] == {"id": "tnt_sparkle", "name": "Sparkle Home Cleaning", "routedBy": "operator"}
    assert "assignment" not in outcome.body
    lead = get_lead(ctx, outcome.body["leadId"])
    assert (lead["tenant_id"], lead["routed_by"]) == ("tnt_sparkle", "operator")
    assert lead["received_at"] == iso(START)
    event = get_event(ctx, held["eventId"])
    assert (event["status"], event["operator_tenant_id"], event["tenant_id"]) == ("processed", "tnt_sparkle", "tnt_sparkle")
    assert lead_events(ctx, lead["id"], "lead.received")[0]["data"]["routed_by"] == "operator"
    assert count(ctx, "tenant_lead_sources", "learned = 1") == 0  # no account id, nothing to learn
    assert "no_account_id" not in open_alerts(ctx)  # nothing is waiting to be assigned any more
    clock.advance(seconds=1)
    ctx.worker.deliver_due()
    assert [(email.to, email.from_name) for email in mailer.sent] == [("assigned@example.com", "Sparkle Home Cleaning")]


def test_no_account_alert_stays_open_while_another_lead_is_still_waiting(client, ctx):
    # One alert for the whole queue: it closes when the last waiting lead has been placed.
    held = []
    for email in ("one@example.com", "two@example.com"):
        payload = ads_payload(Email=email, PhoneNumber="317555010" + str(len(held)))
        del payload["ALAccountId"]
        held.append(post_lead(client, payload).json()["eventId"])

    ctx.ingest.process_event(held[0], assign_tenant_id="tnt_sparkle")
    assert open_alerts(ctx)["no_account_id"]["count"] == 2

    ctx.ingest.process_event(held[1], assign_tenant_id="tnt_sparkle")
    assert "no_account_id" not in open_alerts(ctx)


@pytest.mark.parametrize("tenant_id", ["tnt_does_not_exist", "tnt_summit"], ids=["unknown", "deactivated"])
def test_assigning_an_unknown_or_deactivated_tenant_leaves_the_event_quarantined(client, ctx, tenant_id):
    # A wrong id must not raise, must not create anything, and must not stick to the
    # event (a later, correct assignment still works).
    with ctx.db.transaction() as conn:
        conn.execute("UPDATE tenants SET is_active = 0 WHERE id = 'tnt_summit'")
    payload = ads_payload()
    del payload["ALAccountId"]
    held = post_lead(client, payload).json()

    outcome = ctx.ingest.process_event(held["eventId"], assign_tenant_id=tenant_id)

    assert outcome.http_status == 200
    assert (outcome.body["result"], outcome.body["reason"]) == ("quarantined", "no_account_id")
    assert outcome.body["assignment"] == "not applied: that tenant does not exist or is deactivated"
    event = get_event(ctx, held["eventId"])
    assert (event["status"], event["operator_tenant_id"]) == ("quarantined", None)
    assert count(ctx, "leads") == 0

    assert ctx.ingest.process_event(held["eventId"], assign_tenant_id="tnt_sparkle").body["result"] == "processed"


def test_assigning_a_tenant_to_an_event_that_is_not_quarantined_changes_nothing(client, ctx):
    # Only a waiting event can be placed. A lead that already exists is not moved.
    first = post_lead(client, ads_payload()).json()

    outcome = ctx.ingest.process_event(first["eventId"], assign_tenant_id="tnt_blueline")

    assert outcome.body["result"] == "processed"
    assert outcome.body["leadId"] == first["leadId"]
    assert outcome.body["assignment"] == "not applied: only a quarantined event can be assigned to a tenant"
    assert get_lead(ctx, first["leadId"])["tenant_id"] == "tnt_sparkle"
    assert get_event(ctx, first["eventId"])["operator_tenant_id"] is None
    assert count(ctx, "leads") == 1


def test_operator_assignment_of_a_lead_for_an_unmapped_account_teaches_the_mapping(client, ctx):
    # An operator's decision is explicit routing like a tenant URL: the account is
    # remembered, and the other leads waiting for it are released with it.
    first = post_lead(client, ads_payload(ALAccountId="777001", Email="one@example.com", PhoneNumber="3175550101")).json()
    second = post_lead(client, ads_payload(ALAccountId="777001", Email="two@example.com", PhoneNumber="3175550102")).json()

    outcome = ctx.ingest.process_event(first["eventId"], assign_tenant_id="tnt_blueline")

    assert outcome.body["result"] == "processed"
    assert outcome.body["released"] == [{"eventId": second["eventId"], "result": "processed"}]
    mapping = db_one(ctx, "SELECT * FROM tenant_lead_sources WHERE external_account_id = '777001'")
    assert (mapping["tenant_id"], mapping["learned"]) == ("tnt_blueline", 1)
    assert count(ctx, "leads", "tenant_id = 'tnt_blueline'") == 2


@pytest.mark.parametrize(
    ("reason", "fragment"),
    [
        ("unmapped_account", "processed automatically once the mapping is added"),
        ("tenant_inactive", "deactivated"),
    ],
)
def test_each_quarantine_reason_has_its_own_honest_message(client, ctx, reason, fragment):
    # "Will be processed automatically once the mapping is added" is only true for one of them.
    if reason == "tenant_inactive":
        with ctx.db.transaction() as conn:
            conn.execute("UPDATE tenants SET is_active = 0 WHERE id = 'tnt_sparkle'")
        body = post_lead(client, ads_payload()).json()
    else:
        body = post_lead(client, ads_payload(ALAccountId="777001")).json()

    assert (body["result"], body["reason"]) == ("quarantined", reason)
    assert fragment in body["message"]
    assert ("mapping is added" in body["message"]) == (reason == "unmapped_account")


# --- one log line per processed event ------------------------------------------------------


def test_each_processed_event_writes_one_info_line_with_ids_only(client, ctx, caplog):
    # Enough to follow a lead through the logs - and nothing that identifies a person or
    # opens a door: no name, email, phone, API key or booking token.
    payload = leads_payload(email="private.person@example.com", primaryPhone="3035550177", name="Private Person")
    with caplog.at_level(logging.INFO, logger="app.ingest"):
        processed = post_lead(client, payload).json()
        again = post_lead(client, payload).json()
        held = post_lead(client, ads_payload(ALAccountId="777001")).json()
        rejected = post_lead(client, b"not json").json()

    lines = [record.getMessage() for record in caplog.records if record.name == "app.ingest" and record.levelno == logging.INFO]
    assert len(lines) == 4
    lead_id = processed["leadId"]
    assert lines[0].startswith(
        f"webhook event {processed['eventId']}: result=processed lead={lead_id} tenant=tnt_summit "
        "format=angi_leads parse_mode=strict ms="
    )
    assert lines[1].startswith(
        f"webhook event {again['eventId']}: result=duplicate_delivery lead={lead_id} tenant=tnt_summit "
        "format=angi_leads parse_mode=strict ms="
    )
    assert lines[2].startswith(
        f"webhook event {held['eventId']}: result=quarantined lead=None tenant=None format=angi_ads parse_mode=strict ms="
    )
    assert lines[3].startswith(
        f"webhook event {rejected['eventId']}: result=rejected lead=None tenant=None format=None parse_mode=None ms="
    )
    assert all(float(line.rsplit("ms=", 1)[1]) >= 0 for line in lines)
    booking_token = get_lead(ctx, lead_id)["booking_token"]
    for secret in ("private.person", "example.com", "3035550177", "Private", "Person", "Summit", API_KEY, booking_token):
        assert all(secret not in line for line in lines), secret


def test_failed_pass_also_writes_its_info_line(client, ctx, caplog, monkeypatch):
    # "accepted" (stored, processing failed) is an outcome too, and is logged like the others.
    monkeypatch.setattr("app.ingest.find_duplicate", boom)
    with caplog.at_level(logging.INFO, logger="app.ingest"):
        body = post_lead(client, ads_payload()).json()

    lines = [record.getMessage() for record in caplog.records if record.levelno == logging.INFO]
    assert len(lines) == 1
    assert lines[0].startswith(
        f"webhook event {body['eventId']}: result=accepted lead=None tenant=None format=None parse_mode=None ms="
    )


# --- schema-drift bookkeeping ------------------------------------------------------------


def test_clean_payload_records_the_clean_shape_and_opens_no_alert(client, ctx):
    # The documented format is the baseline: counted, never alerted on.
    post_lead(client, ads_payload())
    post_lead(client, ads_payload())
    post_lead(client, leads_payload())

    shapes = {row["signature"]: row for row in db_rows(ctx, "SELECT * FROM payload_shapes")}
    assert set(shapes) == {"angi_ads:clean", "angi_leads:clean"}
    assert shapes["angi_ads:clean"]["is_clean"] == 1
    assert shapes["angi_ads:clean"]["seen_count"] == 2
    assert shapes["angi_ads:clean"]["detected_format"] == "angi_ads"
    assert shapes["angi_ads:clean"]["parse_mode"] == "strict"
    assert shapes["angi_leads:clean"]["is_clean"] == 1
    assert shapes["angi_leads:clean"]["seen_count"] == 1
    assert count(ctx, "alerts") == 0


def test_new_field_opens_one_info_alert_and_repeats_only_bump_its_count(client, ctx):
    # A format change raises one alert with a counter, not one alert per lead.
    first = post_lead(client, ads_payload(LoyaltyTier="gold")).json()

    alerts = db_rows(ctx, "SELECT * FROM alerts")
    assert len(alerts) == 1
    assert alerts[0]["kind"] == "schema_drift"
    assert alerts[0]["severity"] == "info"
    assert alerts[0]["count"] == 1
    assert "LoyaltyTier" in alerts[0]["title"]
    assert first["result"] == "processed"
    assert first["parseMode"] == "strict"  # nothing broke: the lead parsed normally

    post_lead(client, ads_payload(LoyaltyTier="silver"))

    alerts = db_rows(ctx, "SELECT * FROM alerts")
    assert len(alerts) == 1
    assert alerts[0]["count"] == 2
    shape = db_one(ctx, "SELECT * FROM payload_shapes WHERE is_clean = 0")
    assert shape["seen_count"] == 2
    assert shape["sample_event_id"] == first["eventId"]
    assert json.loads(shape["detail_json"])["unknown"] == ["LoyaltyTier"]
    assert count(ctx, "leads") == 2
    assert count(ctx, "leads", "needs_review = 1") == 0


def test_missing_documented_field_is_a_warning_and_the_lead_is_still_created(client, ctx):
    # A documented field disappearing means the mapping no longer fits: warning, not info.
    payload = ads_payload()
    del payload["Urgency"]

    body = post_lead(client, payload).json()

    assert body["result"] == "processed"
    assert body["parseMode"] == "tolerant"
    alerts = db_rows(ctx, "SELECT * FROM alerts")
    assert len(alerts) == 1
    assert alerts[0]["kind"] == "schema_drift"
    assert alerts[0]["severity"] == "warning"
    assert "Urgency" in alerts[0]["title"]
    assert json.loads(alerts[0]["detail_json"])["missing"] == ["Urgency"]
    assert count(ctx, "leads") == 1


def test_different_deviations_open_different_alerts(client, ctx):
    # Each kind of drift is tracked separately.
    post_lead(client, ads_payload(LoyaltyTier="gold"))
    post_lead(client, ads_payload(ReferralCode="R-1"))
    missing = ads_payload()
    del missing["Urgency"]
    post_lead(client, missing)

    alerts = db_rows(ctx, "SELECT * FROM alerts WHERE kind = 'schema_drift'")
    assert len(alerts) == 3
    assert len({a["dedupe_key"] for a in alerts}) == 3
    assert sorted(a["severity"] for a in alerts) == ["info", "info", "warning"]
    assert count(ctx, "payload_shapes", "is_clean = 0") == 3


def test_unknown_format_with_an_email_becomes_a_lead_flagged_for_review(client, ctx, mailer):
    # A format we have never seen still gets answered if we can find the contact details.
    payload = {
        "customer_name": "Dana Fox",
        "customer_email": "dana@example.com",
        "account_id": "123456",
        "message": "Need a quote for weekly cleaning.",
    }

    body = post_lead(client, payload).json()

    assert body["result"] == "processed"
    assert body["format"] == "unknown"
    assert body["parseMode"] == "tolerant"
    lead = get_lead(ctx, body["leadId"])
    assert lead["email_normalized"] == "dana@example.com"
    assert lead["full_name"] == "Dana Fox"
    assert lead["tenant_id"] == "tnt_sparkle"
    assert lead["parse_mode"] == "tolerant"
    assert lead["needs_review"] == 1
    event = get_event(ctx, body["eventId"])
    assert event["detected_format"] == "unknown"
    assert event["parse_mode"] == "tolerant"
    alerts = db_rows(ctx, "SELECT * FROM alerts")
    assert len(alerts) == 1
    assert alerts[0]["kind"] == "schema_drift"
    assert alerts[0]["severity"] == "critical"

    ctx.worker.deliver_due()
    assert [email.to for email in mailer.sent] == ["dana@example.com"]


def test_unrecognised_payload_without_contact_details_is_quarantined_with_a_critical_alert(client, ctx):
    # Nothing to contact and no known format: keep the request, create no lead, page someone.
    body = post_lead(client, {"foo": "bar", "ALAccountId": "123456"}).json()

    assert body["result"] == "quarantined"
    assert body["reason"] == "unrecognised_payload"
    assert count(ctx, "leads") == 0
    assert count(ctx, "outbox_messages") == 0
    event = get_event(ctx, body["eventId"])
    assert event["status"] == "quarantined"
    assert event["parse_mode"] == "failed"
    alerts = db_rows(ctx, "SELECT * FROM alerts")
    assert len(alerts) == 1
    assert alerts[0]["kind"] == "unrecognised_payload"
    assert alerts[0]["severity"] == "critical"
    shape = db_one(ctx, "SELECT * FROM payload_shapes")
    assert shape["parse_mode"] == "failed"
    assert shape["is_clean"] == 0


# --- cadence queued at ingest ------------------------------------------------------------


def test_follow_ups_are_queued_at_60_and_1440_minutes(client, ctx, clock):
    # 10:00 local arrival: intro now, nudges after an hour and a day (neither deferred).
    body = post_lead(client, ads_payload(Email="pat@example.com")).json()

    assert body["email"] == {
        "status": "queued", "template": "intro", "to": "pat@example.com", "followUpsScheduled": 2,
    }
    intro, first, second = outbox(ctx, body["leadId"])
    assert (intro["template"], intro["sequence"], intro["send_after"]) == ("intro", 0, iso(START))
    assert (first["template"], first["sequence"]) == ("followup", 1)
    assert first["send_after"] == iso(START + timedelta(minutes=60))
    assert (second["template"], second["sequence"]) == ("followup", 2)
    assert second["send_after"] == iso(START + timedelta(minutes=1440))
    assert {m["to_address"] for m in (intro, first, second)} == {"pat@example.com"}
    assert {m["status"] for m in (intro, first, second)} == {"pending"}
    assert local(first["send_after"], tenant_zone(ctx, "tnt_sparkle")).hour == 11


def test_empty_follow_up_setting_queues_only_the_intro(make_client):
    # FOLLOWUP_DELAYS_MINUTES="" switches the nudges off.
    ctx, client = make_client(followup_delays_minutes="")

    body = post_lead(client, ads_payload()).json()

    assert body["email"]["followUpsScheduled"] == 0
    messages = outbox(ctx, body["leadId"])
    assert [m["template"] for m in messages] == ["intro"]


def test_garbage_in_the_follow_up_setting_is_ignored(make_client, clock):
    # Only positive whole minutes count; everything else is dropped, not fatal.
    ctx, client = make_client(followup_delays_minutes="abc, -5, 0, 1.5, 90x, , 30")

    body = post_lead(client, ads_payload()).json()

    assert body["result"] == "processed"
    assert body["email"]["followUpsScheduled"] == 1
    intro, follow_up = outbox(ctx, body["leadId"])
    assert intro["template"] == "intro"
    assert follow_up["send_after"] == iso(clock() + timedelta(minutes=30))


def test_follow_up_delays_are_applied_in_ascending_order(make_client, clock):
    # "1440,60" means the same cadence as "60,1440".
    ctx, client = make_client(followup_delays_minutes="1440,60")

    body = post_lead(client, ads_payload()).json()

    _, first, second = outbox(ctx, body["leadId"])
    assert (first["sequence"], first["send_after"]) == (1, iso(clock() + timedelta(minutes=60)))
    assert (second["sequence"], second["send_after"]) == (2, iso(clock() + timedelta(minutes=1440)))


def test_lead_at_2am_local_gets_the_intro_now_and_follow_ups_at_8am_local(client, ctx, clock):
    # The first response goes out at any hour; a 3 AM "checking in" waits for the morning.
    zone = tenant_zone(ctx, "tnt_sparkle")
    clock.set(datetime(2026, 11, 16, 2, 0, tzinfo=zone).astimezone(UTC))

    body = post_lead(client, ads_payload()).json()

    intro, first, second = outbox(ctx, body["leadId"])
    assert intro["send_after"] == iso(clock())
    assert local(first["send_after"], zone) == datetime(2026, 11, 16, 8, 0, tzinfo=zone)
    # +1440 min lands at 02:00 the next night, so it waits for the next morning too.
    assert local(second["send_after"], zone) == datetime(2026, 11, 17, 8, 0, tzinfo=zone)


def test_lead_at_1930_local_has_its_first_follow_up_deferred_to_8am_next_day(client, ctx, clock):
    # 19:30 + 60 min = 20:30, inside quiet hours: hold until 08:00 tomorrow.
    zone = tenant_zone(ctx, "tnt_sparkle")
    clock.set(datetime(2026, 11, 16, 19, 30, tzinfo=zone).astimezone(UTC))

    body = post_lead(client, ads_payload()).json()

    intro, first, second = outbox(ctx, body["leadId"])
    assert intro["send_after"] == iso(clock())
    assert local(first["send_after"], zone) == datetime(2026, 11, 17, 8, 0, tzinfo=zone)
    # +1440 min = 19:30 tomorrow, which is daytime: not moved.
    assert local(second["send_after"], zone) == datetime(2026, 11, 17, 19, 30, tzinfo=zone)


@pytest.mark.parametrize(
    "arrival, expected_first_follow_up",
    [
        ((16, 10, 0), (16, 11, 0)),  # daytime: untouched
        ((16, 18, 59), (16, 19, 59)),  # last minute before quiet hours
        ((16, 19, 0), (17, 8, 0)),  # 20:00 sharp is already quiet
        ((16, 23, 30), (17, 8, 0)),  # crosses midnight
        ((16, 0, 0), (16, 8, 0)),
        ((16, 6, 59), (16, 8, 0)),  # 07:59 is still quiet
        ((16, 7, 30), (16, 8, 30)),  # 08:30 is daytime
    ],
)
def test_quiet_hours_boundaries_for_the_first_follow_up(client, ctx, clock, arrival, expected_first_follow_up):
    # Quiet hours are 20:00-08:00 in the tenant's own timezone.
    zone = tenant_zone(ctx, "tnt_sparkle")
    day, hour, minute = arrival
    clock.set(datetime(2026, 11, day, hour, minute, tzinfo=zone).astimezone(UTC))

    body = post_lead(client, ads_payload()).json()

    first = outbox(ctx, body["leadId"])[1]
    day, hour, minute = expected_first_follow_up
    assert local(first["send_after"], zone) == datetime(2026, 11, day, hour, minute, tzinfo=zone)


def test_quiet_hours_use_each_tenants_own_timezone(client, ctx, clock):
    # The same instant is evening in Indianapolis (deferred) and afternoon in Denver (not).
    clock.set(datetime(2026, 11, 17, 1, 30, tzinfo=UTC))  # 20:30 in Indianapolis, 18:30 in Denver
    indy, denver = tenant_zone(ctx, "tnt_sparkle"), tenant_zone(ctx, "tnt_summit")

    sparkle = post_lead(client, ads_payload(ALAccountId="123456", Email="a@example.com")).json()
    summit = post_lead(client, ads_payload(ALAccountId="200001", Email="b@example.com")).json()

    sparkle_first = outbox(ctx, sparkle["leadId"])[1]
    summit_first = outbox(ctx, summit["leadId"])[1]
    assert local(sparkle_first["send_after"], indy) == datetime(2026, 11, 17, 8, 0, tzinfo=indy)
    assert local(summit_first["send_after"], denver) == datetime(2026, 11, 16, 19, 30, tzinfo=denver)
    assert summit_first["send_after"] == iso(clock() + timedelta(minutes=60))


@pytest.mark.parametrize("email", ["", "   ", "not-an-email", "bob@@example..com", None])
def test_lead_without_a_valid_email_is_stored_but_nothing_is_queued(client, ctx, mailer, email):
    # No usable address: keep the lead, flag it for a human, do not invent an email.
    body = post_lead(client, ads_payload(Email=email)).json()

    assert body["status"] == "success"
    assert body["result"] == "processed"
    assert body["email"] == {"status": "skipped", "reason": "no_valid_email"}
    lead = get_lead(ctx, body["leadId"])
    assert lead["email_normalized"] is None
    assert lead["needs_review"] == 1
    assert lead["phone_normalized"] == "+15554332646"  # the rest of the lead is intact
    assert count(ctx, "outbox_messages") == 0
    skipped = lead_events(ctx, body["leadId"], "email.skipped")
    assert len(skipped) == 1
    assert skipped[0]["data"] == {"reason": "no_valid_email"}

    ctx.worker.deliver_due()
    assert mailer.sent == []


def test_unsubscribed_address_is_not_emailed_for_that_tenant_only(client, ctx, mailer):
    # An opt-out is honoured at ingest, and is scoped to the business it was given to.
    with ctx.db.transaction() as conn:
        conn.execute(
            "INSERT INTO email_suppressions (tenant_id, email_normalized, reason, created_at) VALUES (?, ?, ?, ?)",
            ("tnt_sparkle", "optout@example.com", "unsubscribed", iso(START)),
        )

    sparkle = post_lead(client, ads_payload(ALAccountId="123456", Email="OptOut@Example.com")).json()
    summit = post_lead(client, ads_payload(ALAccountId="200001", Email="OptOut@Example.com")).json()

    assert sparkle["result"] == "processed"
    assert sparkle["email"] == {"status": "skipped", "reason": "unsubscribed"}
    assert outbox(ctx, sparkle["leadId"]) == []
    assert lead_events(ctx, sparkle["leadId"], "email.skipped")[0]["data"] == {"reason": "unsubscribed"}
    assert summit["email"]["status"] == "queued"

    ctx.worker.deliver_due()
    assert [email.from_name for email in mailer.sent] == ["Summit Heating & Air"]


# --- Angi Leads format, end to end -------------------------------------------------------


def test_angi_leads_payload_is_routed_and_stored_with_fee_and_interview(client, ctx, mailer):
    # The second feed (camelCase, numeric ids) lands in the same canonical lead shape.
    payload = leads_payload()

    response = post_lead(client, payload)

    assert response.status_code == 200
    body = response.json()
    assert body["result"] == "processed"
    assert body["format"] == "angi_leads"
    assert body["parseMode"] == "strict"
    assert body["tenant"] == {"id": "tnt_summit", "name": "Summit Heating & Air", "routedBy": "account_id"}
    assert body["warnings"] == []
    lead = get_lead(ctx, body["leadId"])
    assert lead["tenant_id"] == "tnt_summit"
    assert lead["source_format"] == "angi_leads"
    assert lead["external_lead_id"] == str(payload["leadOid"])
    assert lead["external_request_id"] == str(payload["srOid"])
    assert lead["external_account_id"] == "12345678"
    assert lead["external_account_name"] == "Summit Heating & Air"
    assert lead["idempotency_key"] == f"angi:12345678:{payload['leadOid']}"
    assert lead["fee_cents"] == 2567
    assert lead["fee_source"] == "payload"
    assert json.loads(lead["interview_json"]) == payload["interview"]
    assert lead["phone_is_masked"] == 0
    assert lead["automated_contact_ok"] == 1
    assert lead["full_name"] == "Jane Doe"
    assert lead["email_normalized"] == "janedoe@example.com"
    assert lead["phone_normalized"] == "+13035550123"
    assert (lead["city"], lead["state"], lead["postal_code"]) == ("Denver", "CO", "80210")
    assert lead["task_name"] == "Central A/C - Repair"
    assert lead["match_type"] == "Lead"
    assert lead["lead_source_detail"] == "HomeAdvisor"
    assert lead["trusted_form_url"] == "https://cert.trustedform.com/abc123"
    assert lead["needs_review"] == 0
    assert lead["is_duplicate"] == 0

    ctx.worker.deliver_due()
    assert [(email.to, email.from_name) for email in mailer.sent] == [("janedoe@example.com", "Summit Heating & Air")]


def test_same_service_request_sold_twice_is_flagged_as_a_duplicate(client, ctx, clock):
    # One srOid billed as two leads is a duplicate even if the contact details differ.
    first_payload = leads_payload()
    first = post_lead(client, first_payload).json()
    clock.advance(hours=3)
    second_payload = leads_payload(
        srOid=first_payload["srOid"],
        name="Sam Roe",
        firstName="Sam",
        lastName="Roe",
        email="sam.roe@example.com",
        primaryPhone="7205550199",
    )
    assert second_payload["leadOid"] != first_payload["leadOid"]

    second = post_lead(client, second_payload).json()

    assert second["result"] == "processed"
    assert second["leadId"] != first["leadId"]
    assert second["duplicate"]["ofLeadId"] == first["leadId"]
    assert second["duplicate"]["matchedOn"] == ["service_request"]
    assert second["duplicate"]["confidence"] == "high"
    lead = get_lead(ctx, second["leadId"])
    assert lead["is_duplicate"] == 1
    assert lead["duplicate_of_lead_id"] == first["leadId"]
    assert json.loads(lead["duplicate_matched_on"]) == ["service_request"]
    claim = db_one(ctx, "SELECT * FROM credit_claims")
    assert claim["id"] == second["duplicate"]["creditClaimId"]
    assert claim["lead_id"] == second["leadId"]
    assert claim["original_lead_id"] == first["leadId"]
    assert claim["amount_cents"] == 2567
    assert claim["hours_apart"] == 3.0
    detected = lead_events(ctx, second["leadId"], "lead.duplicate_detected")
    assert detected[0]["data"]["matched_on"] == ["service_request"]
    # It is a duplicate for the credit claim, but sam.roe@example.com has never heard from
    # this business: that address gets the normal introduction and follow-ups, not
    # "we got your new request".
    assert second["email"]["template"] == "intro"
    assert second["email"]["followUpsScheduled"] == 2
    assert [m["template"] for m in outbox(ctx, second["leadId"])] == ["intro", "followup", "followup"]
    assert get_lead(ctx, first["leadId"])["is_duplicate"] == 0
