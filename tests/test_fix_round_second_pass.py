"""Regression tests for what the review of the fix round itself turned up: places where
two fixes met, or where a fix was true for one path and not for its neighbour.

Every test here fails on the code as it was before these corrections, except the two
whose comment begins "The quiet side": those guard the other direction of a rule.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.config import Settings
from app.mailer.providers import OutboundEmail, PermanentEmailError, ResendEmailProvider
from app.repo import EV_EMAIL_FAILED, EV_EMAIL_SKIPPED
from tests.conftest import ADMIN, ads_payload, db_one, db_rows, leads_payload, post_lead

EMAIL_ALERT_KEY = "email_send_error"
PAIR_BY_PAIR = "salvaged one by one"
AS_A_WHOLE = "repaired as a whole"


# --- helpers ---------------------------------------------------------------------------


def consumer(n: int) -> dict[str, str]:
    """Contact details no other lead in the test shares, so the lead is not a duplicate."""
    return {"Email": f"consumer{n}@example.com", "PhoneNumber": f"317555{2000 + n:04d}", "FirstName": f"Lead{n}"}


def lead_row(ctx, lead_id: str) -> dict:
    return db_one(ctx, "SELECT * FROM leads WHERE id = ?", (lead_id,))


def messages(ctx, lead_id: str, template: str | None = None) -> list[dict]:
    rows = db_rows(ctx, "SELECT * FROM outbox_messages WHERE lead_id = ? ORDER BY sequence, created_at, id", (lead_id,))
    return [row for row in rows if template is None or row["template"] == template]


def lead_events(ctx, lead_id: str, event_type: str) -> list[dict]:
    rows = db_rows(
        ctx, "SELECT data_json FROM lead_events WHERE lead_id = ? AND type = ? ORDER BY id", (lead_id, event_type)
    )
    return [json.loads(row["data_json"] or "{}") for row in rows]


def alert_statuses(ctx, key: str) -> list[str]:
    return [row["status"] for row in db_rows(ctx, "SELECT status FROM alerts WHERE dedupe_key = ? ORDER BY id", (key,))]


def unsubscribe(client, lead: dict) -> None:
    response = client.post(f"/unsubscribe/{lead['booking_token']}")
    assert response.status_code == 200, response.text


# --- the warning about a body that was not valid JSON ----------------------------------


def test_body_repaired_as_a_whole_is_not_told_that_its_nesting_was_lost(client):
    # Rule: a comma before the closing brace is repaired without losing anything (strict
    # parse, interview and nested objects intact). The answer must say that, not that the
    # pairs were salvaged one by one and nested fields are listed as missing.
    body = post_lead(client, json.dumps(leads_payload())[:-1] + ",}").json()

    assert (body["result"], body["parseMode"]) == ("processed", "strict")
    (note,) = [warning for warning in body["warnings"] if warning.startswith("the body was not valid JSON")]
    assert AS_A_WHOLE in note and "nesting is intact" in note
    assert not any(PAIR_BY_PAIR in warning for warning in body["warnings"])
    assert not any(warning.startswith(("payload is missing", "fields recovered")) for warning in body["warnings"])


def test_body_read_pair_by_pair_says_that_nesting_and_interview_answers_are_gone(client):
    # The other side of the rule: a missing comma can only be read pair by pair, and then
    # the warning explains why nested fields show up as missing or recovered by alias.
    raw = json.dumps(ads_payload(**consumer(1))).replace(', "LastName"', ' "LastName"', 1)

    body = post_lead(client, raw).json()

    assert body["result"] == "processed"
    (note,) = [warning for warning in body["warnings"] if warning.startswith("the body was not valid JSON")]
    assert PAIR_BY_PAIR in note and "interview answers are not kept" in note
    assert AS_A_WHOLE not in note


# --- alerts about leads waiting for an operator ----------------------------------------


def test_no_account_alert_closes_when_the_assigned_event_is_processed_by_the_retry_after_a_crash(
    client, ctx, clock, monkeypatch
):
    # Rule: the alert says "an operator has to assign it". Once the operator has, it is
    # over, also when the pass that should have created the lead crashed and the retry
    # loop finished the job (the event is "failed" in between, not "quarantined").
    payload = ads_payload(**consumer(2))
    del payload["ALAccountId"]
    held = post_lead(client, payload).json()["eventId"]
    assert alert_statuses(ctx, "no_account_id") == ["open"]
    real_insert = ctx.ingest._insert_lead

    def crash_once(*args, **kwargs):
        monkeypatch.setattr(ctx.ingest, "_insert_lead", real_insert)
        raise RuntimeError("boom: injected failure")

    monkeypatch.setattr(ctx.ingest, "_insert_lead", crash_once)
    clock.advance(minutes=10)
    assigned = client.post(f"/admin/api/events/{held}/replay", json={"tenant_id": "tnt_sparkle"}, headers=ADMIN).json()
    assert assigned["result"] == "accepted"  # stored; processing is retried
    clock.advance(seconds=45)

    assert ctx.ingest.retry_unfinished() == 1

    event = db_one(ctx, "SELECT status, tenant_id FROM webhook_events WHERE id = ?", (held,))
    assert (event["status"], event["tenant_id"]) == ("processed", "tnt_sparkle")
    assert alert_statuses(ctx, "no_account_id") == ["resolved"]


def test_no_account_alert_stays_open_when_another_such_event_still_waits_after_the_crash(
    client, ctx, clock, monkeypatch
):
    # The quiet side: one event crashes on its way out of the quarantine, another one is
    # still waiting to be assigned, so the alert is not over.
    first, second = ads_payload(**consumer(3)), ads_payload(**consumer(4))
    del first["ALAccountId"], second["ALAccountId"]
    held = post_lead(client, first).json()["eventId"]
    post_lead(client, second)

    def crash(*args, **kwargs):
        raise RuntimeError("boom: injected failure")

    monkeypatch.setattr(ctx.ingest, "_insert_lead", crash)
    client.post(f"/admin/api/events/{held}/replay", json={"tenant_id": "tnt_sparkle"}, headers=ADMIN)

    assert alert_statuses(ctx, "no_account_id") == ["open"]


# --- a corrected address that will not be emailed --------------------------------------


def test_correction_to_an_address_that_opted_out_puts_the_unreached_lead_in_front_of_a_person(client, ctx):
    # Rule: a lead nobody will hear from by email needs a person. A new lead whose address
    # has opted out is flagged; a lead corrected to such an address before its first email
    # went out must be flagged too.
    earlier = lead_row(ctx, post_lead(client, ads_payload(**consumer(5))).json()["leadId"])
    ctx.worker.deliver_due()
    unsubscribe(client, earlier)  # consumer5@example.com no longer wants this tenant's email

    payload = ads_payload(**consumer(6))
    lead_id = post_lead(client, payload).json()["leadId"]  # its first email is still queued
    assert lead_row(ctx, lead_id)["needs_review"] == 0

    corrected = post_lead(client, {**payload, "Email": "consumer5@example.com"}).json()

    assert (corrected["result"], corrected["email"]) == ("updated", {"status": "unchanged"})
    assert lead_row(ctx, lead_id)["needs_review"] == 1
    ctx.worker.deliver_due()
    assert [m["status"] for m in messages(ctx, lead_id, "intro")] == ["cancelled"]  # nothing goes to that address


def test_correction_on_a_lead_that_opted_out_is_recorded_as_skipped_and_flagged(client, ctx, mailer):
    # Rule: the opt-out came through this lead's own link, so the corrected address gets
    # no introduction. That decision is on the timeline, and the lead is flagged: nobody
    # at the corrected address will hear from us either.
    payload = ads_payload(**consumer(7))
    lead = lead_row(ctx, post_lead(client, payload).json()["leadId"])
    ctx.worker.deliver_due()
    unsubscribe(client, lead)
    sent_before = len(mailer.sent)
    assert lead_row(ctx, lead["id"])["needs_review"] == 0

    corrected = post_lead(client, {**payload, "Email": "consumer7.fixed@example.com"}).json()
    ctx.worker.deliver_due()

    assert (corrected["result"], corrected["email"]) == ("updated", {"status": "unchanged"})
    assert len(mailer.sent) == sent_before  # no new introduction
    assert lead_events(ctx, lead["id"], EV_EMAIL_SKIPPED) == [{"reason": "unsubscribed"}]
    assert lead_row(ctx, lead["id"])["needs_review"] == 1


# --- retry of an email to an example address, through the admin API ---------------------


def test_admin_api_refuses_to_retry_an_email_to_an_example_address_as_the_dashboard_does(
    client, ctx, mailer, monkeypatch
):
    # Rule: the API and the dashboard do the same things. With a provider that really
    # sends, the worker refuses an example address again at once, so "queued" would be a
    # promise that cannot be kept: the API says why instead, and nothing is queued.
    monkeypatch.setattr(mailer, "delivers_externally", True, raising=False)  # as SMTP and Resend declare
    lead_id = post_lead(client, ads_payload(**consumer(8))).json()["leadId"]
    ctx.worker.deliver_due()
    (intro,) = messages(ctx, lead_id, "intro")
    assert intro["status"] == "failed"

    response = client.post(f"/admin/api/messages/{intro['id']}/retry", headers=ADMIN)

    assert response.status_code == 409
    assert "only ever an example" in response.json()["detail"]
    assert [m["status"] for m in messages(ctx, lead_id, "intro")] == ["failed"]
    assert len(lead_events(ctx, lead_id, EV_EMAIL_FAILED)) == 1  # no second failure on the timeline


def test_admin_api_still_retries_a_failed_email_to_an_ordinary_address(client, ctx, mailer, monkeypatch):
    # The quiet side: an address that can be mailed is queued again and sent.
    monkeypatch.setattr(mailer, "delivers_externally", True, raising=False)
    real_send = mailer.send

    def refuse_once(email):
        monkeypatch.setattr(mailer, "send", real_send)
        raise PermanentEmailError("recipient refused: 550 mailbox unavailable")

    monkeypatch.setattr(mailer, "send", refuse_once)
    lead_id = post_lead(client, ads_payload(**{**consumer(9), "Email": "pat@customer-mail.dev"})).json()["leadId"]
    ctx.worker.deliver_due()
    (intro,) = messages(ctx, lead_id, "intro")
    assert intro["status"] == "failed"

    response = client.post(f"/admin/api/messages/{intro['id']}/retry", headers=ADMIN)
    ctx.worker.deliver_due()

    assert response.status_code == 200
    assert [m["status"] for m in messages(ctx, lead_id, "intro")] == ["sent"]


# --- a follow-up that is held back is not "failing" ------------------------------------


def test_follow_up_held_back_after_a_failed_attempt_does_not_keep_the_email_alert_open(client, ctx, clock, mailer):
    # Rule: the "email is failing" alert stays open while a message is between retries. A
    # follow-up that failed once and is then deliberately postponed (the same address was
    # emailed a moment ago) is waiting, not failing: the next successful send closes the
    # alert instead of leaving it open until the follow-up finally goes out.
    payload = ads_payload(**consumer(10))
    lead_id = post_lead(client, payload).json()["leadId"]
    ctx.worker.deliver_due()  # the introduction

    refused = {"consumer10@example.com"}
    real_send = mailer.send

    def send(email):
        if email.to in refused:
            raise RuntimeError("452 4.2.2 mailbox over quota")
        return real_send(email)

    mailer.send = send
    clock.advance(minutes=60)
    ctx.worker.deliver_due()  # follow-up 1 falls due and fails
    (first_nudge,) = [m for m in messages(ctx, lead_id, "followup") if m["sequence"] == 1]
    assert (first_nudge["status"], bool(first_nudge["last_error"])) == ("pending", True)
    assert alert_statuses(ctx, EMAIL_ALERT_KEY) == ["open"]

    # The mailbox has room again and the same person files a second request: its
    # acknowledgement goes out, so the nudge due seconds later is held for half an hour.
    refused.clear()
    clock.advance(seconds=2)
    repeat = post_lead(client, {**payload, "CorrelationId": "second-request-of-consumer-10"}).json()
    ctx.worker.deliver_due()
    assert repeat["email"]["template"] == "repeat_request"
    clock.advance(seconds=10)
    ctx.worker.deliver_due()
    (first_nudge,) = [m for m in messages(ctx, lead_id, "followup") if m["sequence"] == 1]
    assert first_nudge["status"] == "pending" and first_nudge["send_after"] > clock().isoformat()[:19]
    assert first_nudge["last_error"] is None  # held back, not failing

    # Somebody else's lead is answered: sending works, and nothing is between retries.
    clock.advance(seconds=30)
    post_lead(client, ads_payload(**consumer(11)))
    ctx.worker.deliver_due()

    assert alert_statuses(ctx, EMAIL_ALERT_KEY) == ["resolved"]


# --- the subject line through the Resend API --------------------------------------------


def test_resend_subject_never_carries_an_encoded_word(monkeypatch):
    # Rule: as over SMTP, "=?" in a subject (it contains words the lead supplied) must not
    # reach a mail system as the start of an RFC 2047 encoded word.
    requests: list[dict] = []

    def post(url, **kwargs):
        requests.append(kwargs)
        return httpx.Response(200, json={"id": "re_123"}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", post)
    provider = ResendEmailProvider(Settings(_env_file=None, resend_api_key="re_test_key", smtp_timeout_seconds=15.0))

    provider.send(
        OutboundEmail(
            to="pat@example.com",
            subject="Your =?utf-8?q?evil=2Ecom/claim?= request",
            text="Hi Pat",
            html="<p>Hi Pat</p>",
            from_name="Sparkle Home Cleaning",
            from_address="mailer@example.test",
        )
    )

    (request,) = requests
    assert request["json"]["subject"] == "Your = ?utf-8?q?evil=2Ecom/claim?= request"


# --- speed to lead, to the millisecond --------------------------------------------------


@pytest.mark.parametrize(("milliseconds", "shown"), [(6, "6 ms"), (95, "95 ms")])
def test_overview_shows_a_median_speed_to_lead_of_milliseconds_as_milliseconds(
    client, ctx, clock, milliseconds, shown
):
    # Rule: the console provider answers in a few milliseconds. Rounded to a tenth of a
    # second the Overview would say "0.0 s" above a lead whose own page says "6 ms".
    post_lead(client, ads_payload(**consumer(12)))
    clock.advance(milliseconds=milliseconds)
    ctx.worker.deliver_due()

    summary = client.get("/admin/api/analytics/summary", headers=ADMIN).json()
    page = client.get("/dashboard", headers=ADMIN).text

    assert summary["speed"]["speed_to_lead_p50_seconds"] == pytest.approx(milliseconds / 1000)
    tile = page.split("Median speed to lead", 1)[1].split("</div>", 2)[1]
    assert shown in tile and "0.0 s" not in tile

