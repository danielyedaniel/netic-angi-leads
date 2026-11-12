"""Regression tests for the intake fixes of the final review round: quarantine alerts
that close too early or not at all, email after a corrected address, replay of an
applied update, the body salvage, the tenant URL with a trailing slash, the timeline of
a released lead, and follow-ups for the canary.

Every test here fails on the code as it was before that round. Where a rule has a
quiet side (an ordinary lead, an event that is still waiting) it is checked in the same
test as the side that used to be wrong, because on its own it would pass on both.
"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from pathlib import Path

import pytest

from app.angi.parser import parse_angi_payload
from app.ingest import decode_body, salvage_json
from app.mailer.providers import PermanentEmailError
from app.util import iso
from tests.conftest import (
    ADMIN,
    ANGI_LEADS_EXAMPLE,
    BLUELINE_ACCOUNT,
    CANARY_ACCOUNT,
    SPARKLE_ACCOUNT,
    START,
    SUMMIT_ADS_ACCOUNT,
    ads_payload,
    db_one,
    db_rows,
    leads_payload,
    post_lead,
)

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
LEFT, RIGHT = "“", "”"  # the curly quotes Angi's PDF prints where JSON needs straight ones
UNMAPPED_ACCOUNT = "555000"  # no tenant has this Angi account id
FIRST_TOUCH = ("intro", "repeat_request")
CONFLICT_ALERT = f"mapping_conflict:{SPARKLE_ACCOUNT}"


# --- helpers -----------------------------------------------------------------------------


def consumer(n: int) -> dict[str, str]:
    """Contact details no other lead in the test shares, so the lead is not a duplicate."""
    return {"Email": f"consumer{n}@example.com", "PhoneNumber": f"317555{1000 + n:04d}", "FirstName": f"Lead{n}"}


def count(ctx, table: str, where: str = "1 = 1", params: tuple = ()) -> int:
    return db_one(ctx, f"SELECT COUNT(*) AS n FROM {table} WHERE {where}", params)["n"]


def get_lead(ctx, lead_id: str) -> dict:
    return db_one(ctx, "SELECT * FROM leads WHERE id = ?", (lead_id,))


def get_event(ctx, event_id: str) -> dict:
    return db_one(ctx, "SELECT * FROM webhook_events WHERE id = ?", (event_id,))


def lead_events(ctx, lead_id: str, type_: str) -> list[dict]:
    """The lead's lifecycle events of one type, oldest first, with ``data`` decoded."""
    rows = db_rows(ctx, "SELECT * FROM lead_events WHERE lead_id = ? AND type = ? ORDER BY id", (lead_id, type_))
    for row in rows:
        row["data"] = json.loads(row["data_json"]) if row["data_json"] else {}
    return rows


def outbox(ctx, lead_id: str) -> list[dict]:
    """The lead's messages in the order they were queued (first touch before its follow-ups)."""
    return db_rows(
        ctx, "SELECT * FROM outbox_messages WHERE lead_id = ? ORDER BY created_at, sequence, id", (lead_id,)
    )


def alert_statuses(ctx, key: str) -> list[str]:
    """The status of every alert ever raised under this key, oldest first."""
    return [row["status"] for row in db_rows(ctx, "SELECT status FROM alerts WHERE dedupe_key = ? ORDER BY id", (key,))]


def tenant_url(ctx, tenant_id: str) -> str:
    key = db_one(ctx, "SELECT webhook_key FROM tenants WHERE id = ?", (tenant_id,))["webhook_key"]
    return f"/webhooks/angi/leads/{key}"


def deliver(ctx, clock) -> None:
    """Let a second pass, then run one pass of the email worker."""
    clock.advance(seconds=1)
    ctx.worker.deliver_due()


def assign(client, event_id: str, tenant_id: str) -> dict:
    """An operator says which tenant a quarantined event is for (the admin API's replay with a tenant)."""
    response = client.post(f"/admin/api/events/{event_id}/replay", json={"tenant_id": tenant_id}, headers=ADMIN)
    assert response.status_code == 200, response.text
    return response.json()


def discard(client, event_id: str) -> None:
    response = client.post(f"/admin/api/events/{event_id}/discard", headers=ADMIN)
    assert response.status_code == 200, response.text


def book_first_slot(client, lead: dict) -> None:
    """The homeowner opens the booking link and takes the first window offered."""
    page = client.get(f"/book/{lead['booking_token']}")
    slot = re.search(r'name="slot" value="([^"]+)"', page.text).group(1)
    response = client.post(f"/book/{lead['booking_token']}", data={"slot": slot}, follow_redirects=False)
    assert response.status_code == 303, response.text


def page_text(html: str) -> str:
    """What the operator reads on a page: the text without its tags, whitespace collapsed."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


# --- mapping-conflict-alert-closed-early ----------------------------------------------------


def conflicting_lead(client, ctx, n: int, account: str = SPARKLE_ACCOUNT) -> str:
    """A lead for another tenant's Angi account (Sparkle's unless said otherwise) posted to
    Summit's URL; returns the id of the quarantined event."""
    payload = ads_payload(ALAccountId=account, **consumer(n))
    body = post_lead(client, payload, headers={}, path=tenant_url(ctx, "tnt_summit")).json()
    assert (body["result"], body["reason"]) == ("quarantined", "mapping_conflict")
    return body["eventId"]


def test_mapping_conflict_alert_stays_open_when_an_ordinary_lead_of_that_account_arrives(client, ctx, clock):
    # The alert is about the lead that is still quarantined. The account's everyday traffic
    # on the shared URL always routed properly and says nothing about that lead.
    held = conflicting_lead(client, ctx, 1)
    clock.advance(minutes=5)

    ordinary = post_lead(client, ads_payload(**consumer(2))).json()

    assert (ordinary["result"], ordinary["tenant"]["id"]) == ("processed", "tnt_sparkle")
    assert alert_statuses(ctx, CONFLICT_ALERT) == ["open"]
    event = get_event(ctx, held)
    assert (event["status"], event["status_reason"]) == ("quarantined", "mapping_conflict")


def test_mapping_conflict_alert_closes_only_when_the_last_conflicting_lead_of_that_account_is_released(client, ctx):
    # One alert per account: releasing one of its two waiting leads leaves it open, and a
    # conflict of another account that is still waiting does not hold it open.
    first, second = conflicting_lead(client, ctx, 1), conflicting_lead(client, ctx, 2)
    conflicting_lead(client, ctx, 3, account=BLUELINE_ACCOUNT)

    assert assign(client, first, "tnt_sparkle")["result"] == "processed"
    assert alert_statuses(ctx, CONFLICT_ALERT) == ["open"]

    assert assign(client, second, "tnt_sparkle")["result"] == "processed"
    assert alert_statuses(ctx, CONFLICT_ALERT) == ["resolved"]
    assert alert_statuses(ctx, f"mapping_conflict:{BLUELINE_ACCOUNT}") == ["open"]


def test_mapping_conflict_alert_closes_with_the_next_ordinary_lead_once_the_conflicting_one_is_discarded(
    client, ctx, clock
):
    # Not open for good either: when nothing of that account waits on the conflict any more,
    # the next lead that routes properly ends it.
    held = conflicting_lead(client, ctx, 1)
    post_lead(client, ads_payload(**consumer(2)))
    assert alert_statuses(ctx, CONFLICT_ALERT) == ["open"]  # the conflicting lead still waits

    discard(client, held)
    clock.advance(minutes=5)
    post_lead(client, ads_payload(**consumer(3)))

    assert alert_statuses(ctx, CONFLICT_ALERT) == ["resolved"]


# --- address-flip-second-intro ------------------------------------------------------------


def flipped_back(client, ctx, clock) -> tuple[str, dict]:
    """A lead is introduced to at address A, Angi corrects it to B, and before the worker
    has sent B's introduction a late retry of the first version flips it back to A.
    Returns the lead id and the corrected (B) version of the payload."""
    version_a = ads_payload(Email="a@example.com")
    version_b = {**version_a, "Email": "b@example.com"}
    lead_id = post_lead(client, version_a).json()["leadId"]
    deliver(ctx, clock)  # A has its introduction
    clock.advance(minutes=1)
    assert post_lead(client, version_b).json()["email"] == {"status": "queued", "to": "b@example.com"}
    clock.advance(minutes=1)
    late_retry = post_lead(client, version_a).json()
    assert late_retry["email"] == {"status": "already_sent", "to": "a@example.com"}
    return lead_id, version_b


def test_address_flipped_back_before_the_corrected_intro_left_is_not_introduced_to_twice(client, ctx, mailer, clock):
    # One first email per address: the introduction still queued for the replaced address is
    # dropped, not re-addressed to the one that already has it. The follow-ups do move.
    lead_id, _ = flipped_back(client, ctx, clock)

    deliver(ctx, clock)

    assert [email.to for email in mailer.sent] == ["a@example.com"]
    messages = outbox(ctx, lead_id)
    assert [(m["to_address"], m["status"], m["last_error"]) for m in messages if m["template"] in FIRST_TOUCH] == [
        ("a@example.com", "sent", None),
        ("b@example.com", "cancelled", "address_corrected"),
    ]
    assert [(m["to_address"], m["status"]) for m in messages if m["template"] == "followup"] == [
        ("a@example.com", "pending")
    ] * 2


def test_address_corrected_again_after_a_flip_back_gets_its_one_introduction(client, ctx, mailer, clock):
    # The answer "queued" has to be true: the address that lost its queued introduction to
    # the flip gets one when Angi sends that address again.
    _, version_b = flipped_back(client, ctx, clock)
    deliver(ctx, clock)
    clock.advance(minutes=1)

    body = post_lead(client, version_b).json()

    assert body["email"] == {"status": "queued", "to": "b@example.com"}
    deliver(ctx, clock)
    assert [email.to for email in mailer.sent] == ["a@example.com", "b@example.com"]


def test_address_whose_queued_intro_went_to_a_later_correction_is_introduced_to_when_it_returns(
    client, ctx, mailer, clock
):
    # A, then B, then C before the worker ran: B's queued introduction is sent to C. B was
    # never written to, so when it comes back it gets its own, whatever key that row holds.
    version_a = ads_payload(Email="a@example.com")
    post_lead(client, version_a)
    deliver(ctx, clock)
    for address in ("b@example.com", "c@example.com"):
        clock.advance(minutes=1)
        post_lead(client, {**version_a, "Email": address})
    deliver(ctx, clock)
    assert [email.to for email in mailer.sent] == ["a@example.com", "c@example.com"]
    clock.advance(minutes=1)

    body = post_lead(client, {**version_a, "Email": "b@example.com"}).json()

    assert body["email"] == {"status": "queued", "to": "b@example.com"}
    deliver(ctx, clock)
    assert [email.to for email in mailer.sent] == ["a@example.com", "c@example.com", "b@example.com"]


# --- api-replay-ignores-event-state (b): replay of an update that was already applied -------


def applied_update(client, clock) -> tuple[dict, dict]:
    """A lead, then a corrected delivery of it (another Urgency) that was applied.
    Returns the two webhook answers."""
    payload = ads_payload(**consumer(1))
    first = post_lead(client, payload).json()
    clock.advance(minutes=5)
    update = post_lead(client, {**payload, "Urgency": "Today"}).json()
    assert (update["result"], update["changed"]) == ("updated", ["urgency"])
    clock.advance(minutes=5)
    return first, update


def test_replaying_an_event_that_already_updated_the_lead_keeps_it_on_record_as_that_update(client, ctx, clock):
    # A replay of an applied correction is a no-op, and the row still says which request
    # changed the lead instead of being relabelled a duplicate delivery.
    first, update = applied_update(client, clock)

    response = client.post(f"/admin/api/events/{update['eventId']}/replay", headers=ADMIN)

    body = response.json()
    assert response.status_code == 200
    assert (body["result"], body["message"], body["leadId"]) == ("processed", "Already processed.", first["leadId"])
    event = get_event(ctx, update["eventId"])
    assert (event["status"], event["status_reason"]) == ("processed", "updated_existing_lead")
    assert (event["lead_id"], event["attempts"]) == (first["leadId"], 2)  # the pass itself is on record
    assert get_lead(ctx, first["leadId"])["urgency"] == "Today"


def test_replaying_an_event_that_already_updated_the_lead_adds_nothing_to_its_timeline(client, ctx, clock):
    # Angi did not deliver anything again, so the timeline must not say it did.
    first, update = applied_update(client, clock)
    timeline = "SELECT id, type FROM lead_events WHERE lead_id = ? ORDER BY id"
    before = db_rows(ctx, timeline, (first["leadId"],))

    client.post(f"/admin/api/events/{update['eventId']}/replay", headers=ADMIN)

    assert db_rows(ctx, timeline, (first["leadId"],)) == before
    assert lead_events(ctx, first["leadId"], "webhook.redelivered") == []


# --- quarantine-alerts-stale-after-discard-or-duplicate -------------------------------------


def without_account(**overrides) -> dict:
    """An Angi Ads lead whose payload names no Angi account: nothing says which tenant it is for."""
    payload = ads_payload(**overrides)
    del payload["ALAccountId"]
    return payload


def hold_without_account(client, payload: dict) -> str:
    body = post_lead(client, payload).json()
    assert (body["result"], body["reason"]) == ("quarantined", "no_account_id")
    return body["eventId"]


def heartbeat(client, sequence: int) -> str:
    """A request that is no lead at all (what samples/unrecognised.json is); returns its event id."""
    body = post_lead(client, {"event": "heartbeat", "sequence": sequence, "environment": "production"}).json()
    assert (body["result"], body["reason"]) == ("quarantined", "unrecognised_payload")
    return body["eventId"]


def test_no_account_alert_closes_when_the_last_waiting_delivery_turns_out_to_be_a_duplicate(client, ctx, clock):
    # Angi delivered the same lead twice. Once the first is placed, the second leaves the
    # quarantine as a duplicate delivery and nothing is waiting for an operator any more.
    payload = without_account(**consumer(1))
    first = hold_without_account(client, payload)
    clock.advance(minutes=15)
    second = hold_without_account(client, payload)
    assert assign(client, first, "tnt_sparkle")["result"] == "processed"
    assert alert_statuses(ctx, "no_account_id") == ["open"]  # the second delivery still waits

    replayed = client.post("/admin/api/events/replay-quarantined", headers=ADMIN).json()

    assert replayed["results"] == [{"eventId": second, "result": "duplicate_delivery"}]
    assert count(ctx, "webhook_events", "status = 'quarantined'") == 0
    assert alert_statuses(ctx, "no_account_id") == ["resolved"]


def test_no_account_alert_closes_when_the_last_waiting_event_is_discarded(client, ctx):
    # Discarding is the other way out of the queue: one of two leaves the alert open, the
    # last one closes it. What waits for another reason neither holds it open nor closes with it.
    first = hold_without_account(client, without_account(**consumer(1)))
    second = hold_without_account(client, without_account(**consumer(2)))
    heartbeat(client, 1)

    discard(client, first)
    assert alert_statuses(ctx, "no_account_id") == ["open"]

    discard(client, second)
    assert alert_statuses(ctx, "no_account_id") == ["resolved"]
    assert alert_statuses(ctx, "unrecognised_payload") == ["open"]


def test_no_account_alert_closes_when_the_last_waiting_event_ends_as_a_tenant_conflict(client, ctx, clock):
    # The event waits for something else now (its lead id belongs to another tenant), which
    # has an alert of its own; "an operator has to assign it" is true of nothing any more.
    payload = ads_payload(**consumer(1))
    held = hold_without_account(client, {key: value for key, value in payload.items() if key != "ALAccountId"})
    clock.advance(minutes=5)
    assert post_lead(client, payload).json()["tenant"]["id"] == "tnt_sparkle"  # the same lead id, with its account

    outcome = assign(client, held, "tnt_blueline")

    assert (outcome["result"], outcome["reason"]) == ("quarantined", "tenant_conflict")
    assert alert_statuses(ctx, "no_account_id") == ["resolved"]
    assert alert_statuses(ctx, "tenant_conflict:tnt_blueline") == ["open"]


def test_unrecognised_payload_alert_closes_when_the_last_such_event_is_discarded(client, ctx):
    # A heartbeat will never be a lead. When the last one has been discarded, the critical
    # alert about payloads nobody could read is over; while one is left it is not.
    first, second = heartbeat(client, 1), heartbeat(client, 2)

    discard(client, first)
    assert alert_statuses(ctx, "unrecognised_payload") == ["open"]

    discard(client, second)
    assert alert_statuses(ctx, "unrecognised_payload") == ["resolved"]


def test_discard_on_the_monitoring_page_closes_the_alert_of_the_last_unreadable_request(client, ctx):
    # The demo's step: send the heartbeat, press Discard. The dashboard form follows the same rule.
    held = heartbeat(client, 1)

    response = client.post(f"/dashboard/events/{held}/discard", headers=ADMIN, follow_redirects=False)

    assert response.status_code == 303
    assert get_event(ctx, held)["status"] == "discarded"
    assert alert_statuses(ctx, "unrecognised_payload") == ["resolved"]


def test_replay_that_leaves_an_event_quarantined_keeps_its_alert_open_until_it_is_discarded(client, ctx):
    # Only leaving the quarantine ends the alert: a replay that changes nothing does not.
    held = heartbeat(client, 1)

    replayed = client.post(f"/admin/api/events/{held}/replay", headers=ADMIN).json()
    assert (replayed["result"], replayed["reason"]) == ("quarantined", "unrecognised_payload")
    assert alert_statuses(ctx, "unrecognised_payload") == ["open"]

    discard(client, held)
    assert alert_statuses(ctx, "unrecognised_payload") == ["resolved"]


def test_unrecognised_payload_after_its_alert_was_closed_opens_a_new_alert(client, ctx, clock):
    # The next unreadable request is news, not one more occurrence folded into an alert
    # that stayed open with nothing behind it.
    discard(client, heartbeat(client, 1))
    clock.advance(hours=1)

    heartbeat(client, 2)

    alerts = db_rows(ctx, "SELECT status, count FROM alerts WHERE dedupe_key = 'unrecognised_payload' ORDER BY id")
    assert alerts == [{"status": "resolved", "count": 1}, {"status": "open", "count": 1}]


# --- no-followups-after-corrected-bad-address -----------------------------------------------


def bounced_lead(client, ctx, clock, mailer, **overrides) -> tuple[str, dict]:
    """A lead whose first address the mail server refuses for good, twenty minutes ago.
    Returns the lead id and its payload."""
    payload = ads_payload(**{"Email": "mistyped@example.com", **overrides})
    lead_id = post_lead(client, payload).json()["leadId"]
    mailer.fail_with = PermanentEmailError("recipient refused: no such mailbox")
    mailer.fail_times = 1
    deliver(ctx, clock)
    assert [m["status"] for m in outbox(ctx, lead_id) if m["template"] == "intro"] == ["failed"]
    assert count(ctx, "outbox_messages", "lead_id = ? AND status = 'pending'", (lead_id,)) == 0
    clock.advance(minutes=20)
    return lead_id, payload


def test_corrected_address_after_a_hard_bounce_gets_the_intro_and_the_follow_up_cadence(client, ctx, mailer, clock):
    # The lead was never reached and its follow-ups were cancelled with the bounced intro, so
    # the corrected address starts the normal cadence, counted from the correction.
    lead_id, payload = bounced_lead(client, ctx, clock, mailer)

    body = post_lead(client, {**payload, "Email": "right@example.com"}).json()

    assert body["result"] == "updated"
    assert body["email"] == {"status": "queued", "to": "right@example.com", "followUpsScheduled": 2}
    queued = [m for m in outbox(ctx, lead_id) if m["status"] == "pending"]
    assert [(m["template"], m["sequence"], m["to_address"]) for m in queued] == [
        ("intro", 0, "right@example.com"),
        ("followup", 1, "right@example.com"),
        ("followup", 2, "right@example.com"),
    ]
    now = clock()
    assert [m["send_after"] for m in queued] == [iso(now), iso(now + timedelta(hours=1)), iso(now + timedelta(days=1))]


def test_follow_ups_for_a_corrected_address_are_sent_after_its_introduction(client, ctx, mailer, clock):
    # Queued is not enough: the nudges have to go out, an hour and a day after the introduction.
    lead_id, payload = bounced_lead(client, ctx, clock, mailer)
    post_lead(client, {**payload, "Email": "right@example.com"})

    deliver(ctx, clock)
    assert len(mailer.sent) == 1  # the introduction; the follow-ups wait their turn
    clock.advance(minutes=61)
    ctx.worker.deliver_due()
    clock.advance(days=1)
    ctx.worker.deliver_due()

    assert [email.to for email in mailer.sent] == ["right@example.com"] * 3
    to_the_corrected_address = [m for m in outbox(ctx, lead_id) if m["to_address"] == "right@example.com"]
    assert [(m["template"], m["sequence"], m["status"]) for m in to_the_corrected_address] == [
        ("intro", 0, "sent"),
        ("followup", 1, "sent"),
        ("followup", 2, "sent"),
    ]


def test_corrected_address_after_an_opted_out_first_address_gets_the_follow_up_cadence(client, ctx, mailer, clock):
    # The other way a first address can be unusable: it had unsubscribed from this tenant, so
    # nothing was queued at all. The corrected address gets the introduction and the follow-ups.
    earlier = post_lead(client, ads_payload(Email="optedout@example.com", PhoneNumber="3175550150")).json()
    deliver(ctx, clock)
    assert client.post(f"/unsubscribe/{get_lead(ctx, earlier['leadId'])['booking_token']}").status_code == 200
    clock.advance(days=3)
    payload = ads_payload(Email="optedout@example.com", PhoneNumber="3175550151", FirstName="Nora")
    first = post_lead(client, payload).json()
    assert first["email"] == {"status": "skipped", "reason": "unsubscribed"}
    clock.advance(minutes=10)

    body = post_lead(client, {**payload, "Email": "nora@example.com"}).json()

    assert body["email"] == {"status": "queued", "to": "nora@example.com", "followUpsScheduled": 2}
    assert [(m["template"], m["to_address"], m["status"]) for m in outbox(ctx, first["leadId"])] == [
        ("intro", "nora@example.com", "pending"),
        ("followup", "nora@example.com", "pending"),
        ("followup", "nora@example.com", "pending"),
    ]


def test_second_correction_before_the_new_intro_left_does_not_double_the_follow_ups(client, ctx, mailer, clock):
    # The cadence is queued once per lead: a further correction re-addresses what is queued.
    lead_id, payload = bounced_lead(client, ctx, clock, mailer)
    post_lead(client, {**payload, "Email": "right@example.com"})
    clock.advance(minutes=1)

    body = post_lead(client, {**payload, "Email": "really.right@example.com"}).json()

    assert body["email"] == {"status": "queued", "to": "really.right@example.com"}
    queued = [m for m in outbox(ctx, lead_id) if m["status"] == "pending"]
    assert [(m["template"], m["to_address"]) for m in queued] == [
        ("intro", "really.right@example.com"),
        ("followup", "really.right@example.com"),
        ("followup", "really.right@example.com"),
    ]


# --- salvage-rejects-angi-leads-interview ---------------------------------------------------


@pytest.mark.parametrize(
    ("valid", "broken"),
    [
        pytest.param(', "firstName"', ' "firstName"', id="dropped-comma"),
        pytest.param('"janedoe@example.com"', "janedoe@example.com", id="unquoted-email"),
    ],
)
def test_broken_angi_leads_body_with_several_interview_answers_is_salvaged_without_the_interview(valid, broken):
    # The last resort cannot rebuild the interview list, so it leaves it out. It must not
    # take the repeated "question" key for two leads glued together and refuse the body.
    raw = json.dumps(ANGI_LEADS_EXAMPLE).replace(valid, broken, 1)
    assert len(ANGI_LEADS_EXAMPLE["interview"]) == 2
    with pytest.raises(ValueError):
        json.loads(raw)

    payload, envelope, error = decode_body(raw)

    assert (envelope, error) == (("malformed-json",), None)
    assert "question" not in payload and "answer" not in payload
    lead = parse_angi_payload(payload, envelope=envelope).lead
    expected = parse_angi_payload(ANGI_LEADS_EXAMPLE).lead
    assert lead.interview == []
    assert (lead.full_name, lead.email_normalized, lead.phone_normalized) == (
        expected.full_name,
        expected.email_normalized,
        expected.phone_normalized,
    )
    assert (lead.external_lead_id, lead.external_request_id, lead.external_account_id) == (
        "987654321",
        "123456789",
        "12345678",
    )


def test_broken_angi_leads_body_with_several_interview_answers_becomes_a_lead_not_a_400(client, ctx, mailer, clock):
    # A 400 makes Angi send the same body three more times and the lead is never created.
    payload = leads_payload(email="jane@example.com")
    raw = json.dumps(payload).replace(', "firstName"', ' "firstName"', 1)

    response = post_lead(client, raw)

    body = response.json()
    assert response.status_code == 200
    assert (body["result"], body["tenant"]["id"], body["parseMode"]) == ("processed", "tnt_summit", "tolerant")
    lead = get_lead(ctx, body["leadId"])
    assert (lead["email_normalized"], lead["external_lead_id"]) == ("jane@example.com", str(payload["leadOid"]))
    assert lead["interview_json"] is None  # the one thing that could not be recovered
    deliver(ctx, clock)
    assert [email.to for email in mailer.sent] == ["jane@example.com"]


# --- salvage-mixed-quotes-rejected ----------------------------------------------------------


def printed_spec_example() -> str:
    """Angi's example as its PDF prints it: curly quotes, an unquoted email, a missing comma."""
    return (SAMPLES / "malformed_spec_example.txt").read_text(encoding="utf-8")


def with_the_email_line_retyped(text: str) -> str:
    """The same text with the one obviously broken line fixed by hand, in straight quotes."""
    as_printed = f"{LEFT}Email{RIGHT}:bob.builder@example.com,"
    assert as_printed in text and '"' not in text
    return text.replace(as_printed, f'{LEFT}Email{RIGHT}:"bob.builder@example.com",')


def test_spec_example_with_one_line_retyped_in_straight_quotes_is_salvaged_like_the_printed_one():
    # Curly and straight delimiters in one body: when nothing is readable as it stands,
    # every one of them is read as a quote.
    printed = printed_spec_example()

    payload = salvage_json(with_the_email_line_retyped(printed))

    assert payload is not None
    assert payload == salvage_json(printed)
    assert (payload["Email"], payload["City"], payload["ALAccountId"]) == (
        "bob.builder@example.com",
        "Indianapolis",
        "123456",
    )


def test_mixed_quote_body_that_is_valid_once_its_quotes_are_straight_keeps_its_nesting():
    # With the missing comma typed in as well, the quotes are all that is wrong: the body is
    # repaired as a whole and the address block stays an object.
    missing_comma = f"please.{RIGHT}\n{LEFT}Category"
    text = with_the_email_line_retyped(printed_spec_example())
    assert missing_comma in text

    payload, envelope, error = decode_body(text.replace(missing_comma, f"please.{RIGHT},\n{LEFT}Category"))

    assert (envelope, error) == (("malformed-json",), None)
    assert payload["PostalAddress"] == {
        "AddressFirstLine": "123 Main St.",
        "AddressSecondLine": "",
        "City": "Indianapolis",
        "State": "IN",
        "PostalCode": "46203",
    }
    assert payload["Email"] == "bob.builder@example.com"


def test_spec_example_with_one_line_retyped_becomes_a_lead_not_a_400(client, ctx):
    # End to end: the half-corrected paste of Angi's own example is a lead like the other two.
    response = post_lead(client, with_the_email_line_retyped(printed_spec_example()).encode("utf-8"))

    body = response.json()
    assert response.status_code == 200
    assert (body["result"], body["tenant"]["id"]) == ("processed", "tnt_sparkle")
    lead = get_lead(ctx, body["leadId"])
    assert (lead["email_normalized"], lead["city"], lead["postal_code"]) == (
        "bob.builder@example.com",
        "Indianapolis",
        "46203",
    )
    assert lead["comments"] == "I'm Looking for recurring house cleaning services please."


# --- tenant-url-trailing-slash ----------------------------------------------------------------


def test_trailing_slash_on_a_tenant_url_is_the_same_endpoint(client, ctx):
    # A sender configured with ".../leads/<key>/" must not lose its leads to a redirect
    # (a POST is not reliably re-sent after one). The URL itself is the credential.
    payload = ads_payload(ALAccountId=SUMMIT_ADS_ACCOUNT, **consumer(1))

    response = client.post(tenant_url(ctx, "tnt_summit") + "/", json=payload, follow_redirects=False)

    assert response.status_code == 200
    body = response.json()
    assert (body["result"], body["tenant"]["id"], body["tenant"]["routedBy"]) == ("processed", "tnt_summit", "url_key")
    assert get_event(ctx, body["eventId"])["url_tenant_id"] == "tnt_summit"
    assert count(ctx, "leads") == 1


def test_trailing_slash_on_an_unknown_tenant_url_is_a_401_and_stores_nothing(client, ctx):
    # The slash opens no door: a key nobody was given is refused exactly like without it.
    response = client.post("/webhooks/angi/leads/not-a-key-we-issued/", json=ads_payload(), follow_redirects=False)

    assert response.status_code == 401
    assert response.json()["result"] == "unauthorized"
    assert count(ctx, "webhook_events") == 0


# --- released-lead-timeline-received-time -----------------------------------------------------


def released_by_mapping(client, ctx, clock) -> str:
    """A lead for an unmapped account waits two hours until the mapping is added; returns its id."""
    held = post_lead(client, ads_payload(ALAccountId=UNMAPPED_ACCOUNT, **consumer(1))).json()
    assert held["result"] == "quarantined"
    clock.advance(hours=2)
    response = client.post(
        "/admin/api/tenants/tnt_blueline/lead-sources", json={"external_account_id": UNMAPPED_ACCOUNT}, headers=ADMIN
    )
    assert response.json()["replayed"] == [{"eventId": held["eventId"], "result": "processed"}]
    return get_event(ctx, held["eventId"])["lead_id"]


def test_lead_released_by_a_new_mapping_records_since_when_it_waited_in_quarantine(client, ctx, clock):
    # The "received" entry is written when the lead is created, hours after the request
    # arrived, so it names the arrival. A lead that never waited carries no such note.
    lead_id = released_by_mapping(client, ctx, clock)

    (received,) = lead_events(ctx, lead_id, "lead.received")
    assert received["occurred_at"] == iso(clock())
    assert received["data"].get("waited_in_quarantine_since") == iso(START)
    assert get_lead(ctx, lead_id)["received_at"] == iso(START)
    ordinary = post_lead(client, ads_payload(**consumer(2))).json()
    (received,) = lead_events(ctx, ordinary["leadId"], "lead.received")
    assert received["data"].get("waited_in_quarantine_since") is None


def test_lead_released_by_an_operator_assignment_records_since_when_it_waited_in_quarantine(client, ctx, clock):
    # The same for the other way out of quarantine: a person placing a lead that names no account.
    held = hold_without_account(client, without_account(**consumer(1)))
    clock.advance(minutes=45)

    assert assign(client, held, "tnt_sparkle")["result"] == "processed"

    (received,) = lead_events(ctx, get_event(ctx, held)["lead_id"], "lead.received")
    assert (received["occurred_at"], received["data"].get("waited_in_quarantine_since")) == (iso(clock()), iso(START))


def test_released_leads_page_says_since_when_it_waited_next_to_its_received_entry(client, ctx, clock):
    # Funnel shows when the request arrived, the timeline when the lead was created. The
    # timeline entry explains the gap; an ordinary lead's page says nothing about quarantine.
    lead_id = released_by_mapping(client, ctx, clock)

    page = page_text(client.get(f"/dashboard/leads/{lead_id}", headers=ADMIN).text)

    assert "Received 2026-11-12 15:00:00 UTC" in page
    assert "2026-11-12 17:00:00 Lead received from Angi" in page
    assert "waited in quarantine since: 2026-11-12 15:00:00 UTC" in page
    ordinary = post_lead(client, ads_payload(**consumer(2))).json()
    assert "quarantine" not in page_text(client.get(f"/dashboard/leads/{ordinary['leadId']}", headers=ADMIN).text)


# --- malformed-warnings-do-not-say-what-was-salvaged ------------------------------------------


def test_salvaged_spec_example_is_told_first_that_its_body_was_not_valid_json(client):
    # Without that sentence the answer only lists the address fields as "missing", as if the
    # sender had left them out. The lists it points at follow it; a well-formed lead gets none.
    warnings = post_lead(client, (SAMPLES / "malformed_spec_example.txt").read_bytes()).json()["warnings"]

    assert warnings[0].startswith("the body was not valid JSON (<malformed-json>)")
    assert "salvaged one by one" in warnings[0] and "nesting is lost" in warnings[0]
    assert [warning.split(":")[0] for warning in warnings[1:]] == [
        "payload is missing documented fields",
        "fields recovered by alias",
        "fields not in the documented format",
    ]
    assert post_lead(client, ads_payload(**consumer(1))).json()["warnings"] == []


# --- correction-reintroduces-booked-or-closed-lead --------------------------------------------


def introduced_lead(client, ctx, clock, mailer) -> tuple[dict, dict]:
    """A lead that had its introduction at old@example.com ten minutes ago: (its row, its payload)."""
    payload = ads_payload(Email="old@example.com")
    lead_id = post_lead(client, payload).json()["leadId"]
    deliver(ctx, clock)
    assert [email.to for email in mailer.sent] == ["old@example.com"]
    clock.advance(minutes=10)
    return get_lead(ctx, lead_id), payload


def correct_the_address(client, payload: dict) -> dict:
    """Angi redelivers the lead with another Email; returns what the answer says about email."""
    body = post_lead(client, {**payload, "Email": "new@example.com"}).json()
    assert (body["result"], body["changed"]) == ("updated", ["email", "email_normalized"])
    return body["email"]


def test_corrected_address_on_a_booked_lead_gets_no_new_introduction(client, ctx, mailer, clock):
    # "Pick a time" makes no sense to someone who has picked one. The correction itself is stored.
    lead, payload = introduced_lead(client, ctx, clock, mailer)
    book_first_slot(client, lead)
    deliver(ctx, clock)  # the confirmation goes to the address the lead had when it booked
    sent_before = [email.to for email in mailer.sent]
    clock.advance(minutes=10)

    plan = correct_the_address(client, payload)

    assert plan == {"status": "unchanged"}
    assert get_lead(ctx, lead["id"])["email_normalized"] == "new@example.com"
    deliver(ctx, clock)
    assert [email.to for email in mailer.sent] == sent_before == ["old@example.com"] * 2


@pytest.mark.parametrize("outcome", ["won", "lost"])
def test_corrected_address_on_a_closed_lead_gets_no_new_introduction(client, ctx, mailer, clock, outcome):
    # The job is over either way: nobody is asked to pick a time for it.
    lead, payload = introduced_lead(client, ctx, clock, mailer)
    recorded = client.post(f"/admin/api/leads/{lead['id']}/outcome", json={"outcome": outcome}, headers=ADMIN)
    assert recorded.status_code == 200
    clock.advance(minutes=10)

    plan = correct_the_address(client, payload)

    assert plan == {"status": "unchanged"}
    deliver(ctx, clock)
    assert [email.to for email in mailer.sent] == ["old@example.com"]


def test_corrected_address_on_a_lead_that_unsubscribed_through_its_link_gets_no_new_introduction(
    client, ctx, mailer, clock
):
    # The person asked for no more email about this request. A new address for the same
    # request does not undo that, although the opt-out on record only names the old one.
    lead, payload = introduced_lead(client, ctx, clock, mailer)
    assert client.post(f"/unsubscribe/{lead['booking_token']}").status_code == 200
    clock.advance(minutes=10)

    plan = correct_the_address(client, payload)

    assert plan == {"status": "unchanged"}
    deliver(ctx, clock)
    assert [email.to for email in mailer.sent] == ["old@example.com"]


def test_unsent_booking_confirmation_follows_the_corrected_address_and_no_introduction_is_added(
    client, ctx, mailer, clock
):
    # What is still queued goes to the corrected address, here the confirmation of the
    # booking they made, and that is all that goes there.
    lead, payload = introduced_lead(client, ctx, clock, mailer)
    book_first_slot(client, lead)  # the confirmation is queued; the worker has not run yet

    plan = correct_the_address(client, payload)

    assert plan == {"status": "unchanged"}
    deliver(ctx, clock)
    assert [email.to for email in mailer.sent] == ["old@example.com", "new@example.com"]
    assert mailer.sent[-1].subject.startswith("You're booked")


# --- canary-followups -------------------------------------------------------------------------


def canary_payload(**overrides) -> dict:
    return ads_payload(**{"ALAccountId": CANARY_ACCOUNT, "Email": "canary@example.com", **overrides})


def test_canary_lead_gets_the_introduction_and_no_follow_ups(client, ctx):
    # A test tenant's lead is the monitoring probe: it proves the first response and gets
    # no nudges. A real tenant's lead still gets both of its follow-ups.
    probe = post_lead(client, canary_payload()).json()

    assert probe["tenant"]["id"] == "tnt_canary"
    assert probe["email"] == {
        "status": "queued",
        "template": "intro",
        "to": "canary@example.com",
        "followUpsScheduled": 0,
    }
    assert [m["template"] for m in outbox(ctx, probe["leadId"])] == ["intro"]
    real = post_lead(client, ads_payload(**consumer(1))).json()
    assert real["email"]["followUpsScheduled"] == 2
    assert [m["template"] for m in outbox(ctx, real["leadId"])] == ["intro", "followup", "followup"]


def test_repeated_canary_runs_send_one_email_each_and_leave_nothing_pending(client, ctx, mailer, clock):
    # One email per run: no "just checking in" an hour and a day later, and no rows that
    # wait for ever because the same address was written to minutes ago.
    for _ in range(4):
        post_lead(client, canary_payload())
        deliver(ctx, clock)
        clock.advance(minutes=5)

    clock.advance(days=2)
    handled = ctx.worker.deliver_due()

    assert handled == 0
    assert [email.to for email in mailer.sent] == ["canary@example.com"] * 4
    assert db_rows(ctx, "SELECT template, status, COUNT(*) AS n FROM outbox_messages GROUP BY template, status") == [
        {"template": "intro", "status": "sent", "n": 4}
    ]


def test_canary_lead_that_gets_its_first_usable_address_by_correction_gets_no_follow_ups(client, ctx):
    # The same rule on the other path that starts a cadence: a correction that supplies
    # the first address a lead could be emailed at.
    payload = canary_payload(Email="")
    first = post_lead(client, payload).json()
    assert first["email"] == {"status": "skipped", "reason": "no_valid_email"}

    body = post_lead(client, {**payload, "Email": "canary@example.com"}).json()

    assert body["email"] == {
        "status": "queued",
        "template": "intro",
        "to": "canary@example.com",
        "followUpsScheduled": 0,
    }
    assert [m["template"] for m in outbox(ctx, first["leadId"])] == ["intro"]


def test_canary_lead_whose_address_is_corrected_after_a_bounce_gets_no_follow_ups(client, ctx, mailer, clock):
    # A real tenant's lead gets its cadence at the corrected address after a hard bounce;
    # a test tenant's lead gets the introduction there and still no follow-up, ever.
    lead_id, payload = bounced_lead(client, ctx, clock, mailer, ALAccountId=CANARY_ACCOUNT)

    body = post_lead(client, {**payload, "Email": "canary@example.com"}).json()

    assert body["email"] == {"status": "queued", "to": "canary@example.com"}
    assert [(m["template"], m["to_address"], m["status"]) for m in outbox(ctx, lead_id)] == [
        ("intro", "mistyped@example.com", "failed"),
        ("intro", "canary@example.com", "pending"),
    ]
    deliver(ctx, clock)
    clock.advance(days=2)
    ctx.worker.deliver_due()
    assert [email.to for email in mailer.sent] == ["canary@example.com"]
