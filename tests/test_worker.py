"""The outbox worker: what gets sent, when, what is recorded, and what happens when
sending fails. Covers app/worker.py, app/outbox.py and app/cadence.py.

The worker thread is not running (except in the one lifecycle test); each test moves
the fake clock and calls ``ctx.worker.deliver_due()`` itself.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app import outbox
from app.cadence import defer_past_quiet_hours
from app.mailer.providers import PermanentEmailError
from app.mailer.render import RenderedEmail
from app.util import iso, parse_iso
from tests.conftest import ADMIN, START, SUMMIT_ADS_ACCOUNT, ads_payload, db_one, db_rows, post_lead

EMAIL_ALERT_KEY = "email_send_error"
INTRO_SUBJECT = "Your House Cleaning request: pick a time with Sparkle Home Cleaning"
FOLLOWUP_SUBJECT = "Still need help with your House Cleaning? - Sparkle Home Cleaning"


# --- helpers ---------------------------------------------------------------------------


def consumer(n: int) -> dict[str, str]:
    """Contact details no other lead in the test shares, so the lead is not a duplicate."""
    return {"Email": f"consumer{n}@example.com", "PhoneNumber": f"317555{1000 + n:04d}", "FirstName": f"Lead{n}"}


def new_lead(client, ctx, **overrides) -> dict:
    """Post an Angi Ads lead (Sparkle unless ALAccountId is overridden); return its row."""
    response = post_lead(client, ads_payload(**overrides))
    assert response.status_code == 200, response.text
    assert response.json()["result"] == "processed", response.text
    return lead_row(ctx, response.json()["leadId"])


def lead_row(ctx, lead_id: str) -> dict:
    return db_one(ctx, "SELECT * FROM leads WHERE id = ?", (lead_id,))


def messages(ctx, lead_id: str, template: str | None = None) -> list[dict]:
    rows = db_rows(
        ctx, "SELECT * FROM outbox_messages WHERE lead_id = ? ORDER BY sequence, created_at, id", (lead_id,)
    )
    return [row for row in rows if template is None or row["template"] == template]


def message(ctx, lead_id: str, template: str, sequence: int = 0) -> dict:
    found = [m for m in messages(ctx, lead_id, template) if m["sequence"] == sequence]
    assert len(found) == 1, f"expected one {template}#{sequence} message, found {len(found)}"
    return found[0]


def lead_events(ctx, lead_id: str, event_type: str) -> list[dict]:
    rows = db_rows(
        ctx, "SELECT data_json FROM lead_events WHERE lead_id = ? AND type = ? ORDER BY id", (lead_id, event_type)
    )
    return [json.loads(row["data_json"] or "{}") for row in rows]


def email_alerts(ctx) -> list[dict]:
    return db_rows(ctx, "SELECT * FROM alerts WHERE dedupe_key = ? ORDER BY id", (EMAIL_ALERT_KEY,))


def book_first_open_slot(client, lead: dict) -> str:
    page = client.get(f"/book/{lead['booking_token']}")
    slot = re.search(r'name="slot" value="([^"]+)"', page.text).group(1)
    response = client.post(f"/book/{lead['booking_token']}", data={"slot": slot}, follow_redirects=False)
    assert response.status_code == 303, response.text
    return slot


def sql(ctx, statement: str, params: tuple = ()) -> None:
    """A direct write, for states the HTTP API has no endpoint for (e.g. deactivating a tenant)."""
    with ctx.db.transaction() as conn:
        conn.execute(statement, params)


def run_until(ctx, clock, moment: datetime) -> None:
    """Let the worker do what it would do in real time up to ``moment``: jump the clock
    to each time a message falls due and run one delivery pass there."""
    while (due := outbox.next_due_at(ctx.db)) and parse_iso(due) <= moment:
        clock.set(max(clock(), parse_iso(due)))
        ctx.worker.deliver_due()
    clock.set(moment)


def database_locked():
    """Stands in for ``Database.transaction`` while the database cannot be written."""
    raise sqlite3.OperationalError("database is locked")


# --- what is due -----------------------------------------------------------------------


def test_intro_waits_for_the_worker_and_is_sent_exactly_once(client, ctx, mailer):
    # Rule: the webhook only queues the intro; the worker sends it, once.
    lead = new_lead(client, ctx, **consumer(1))
    assert mailer.sent == []
    assert message(ctx, lead["id"], "intro")["status"] == "pending"

    assert ctx.worker.deliver_due() == 1
    assert ctx.worker.deliver_due() == 0

    assert [email.subject for email in mailer.sent] == [INTRO_SUBJECT]
    assert mailer.sent[0].to == "consumer1@example.com"


def test_follow_up_becomes_due_only_when_the_clock_reaches_its_send_after(client, ctx, mailer, clock):
    # Rule: follow-ups go out 60 minutes and 24 hours after the lead, not a second earlier.
    lead = new_lead(client, ctx, **consumer(1))
    ctx.worker.deliver_due()
    assert [m["send_after"] for m in messages(ctx, lead["id"], "followup")] == [
        iso(START + timedelta(minutes=60)),
        iso(START + timedelta(minutes=1440)),
    ]

    clock.advance(minutes=59, seconds=59)
    assert ctx.worker.deliver_due() == 0
    assert len(mailer.sent) == 1

    clock.advance(seconds=1)
    assert ctx.worker.deliver_due() == 1
    assert [email.subject for email in mailer.sent] == [INTRO_SUBJECT, FOLLOWUP_SUBJECT]
    assert message(ctx, lead["id"], "followup", 1)["status"] == "sent"
    assert message(ctx, lead["id"], "followup", 2)["status"] == "pending"

    clock.advance(hours=22, minutes=59, seconds=59)
    assert ctx.worker.deliver_due() == 0
    clock.advance(seconds=1)
    assert ctx.worker.deliver_due() == 1
    assert message(ctx, lead["id"], "followup", 2)["status"] == "sent"
    assert len(mailer.sent) == 3


def test_first_touch_is_claimed_before_a_follow_up_that_has_been_due_for_longer(client, ctx, clock):
    # Rule: a brand-new lead's first email jumps the queue ahead of older follow-ups.
    old = new_lead(client, ctx, **consumer(1))
    ctx.worker.deliver_due()
    clock.advance(minutes=61)  # the old lead's follow-up fell due a minute ago
    new = new_lead(client, ctx, **consumer(2))

    first = outbox.claim_next(ctx.db, clock())
    second = outbox.claim_next(ctx.db, clock())

    assert (first["lead_id"], first["template"], first["sequence"]) == (new["id"], "intro", 0)
    assert (second["lead_id"], second["template"], second["sequence"]) == (old["id"], "followup", 1)
    assert second["send_after"] < first["send_after"]
    assert first["status"] == second["status"] == "sending"
    assert outbox.claim_next(ctx.db, clock()) is None


def test_a_backlog_of_follow_ups_does_not_delay_a_new_leads_intro(client, ctx, mailer, clock):
    # Rule: with more overdue follow-ups than one batch holds, the new intro still goes first.
    for n in range(1, 7):
        new_lead(client, ctx, **consumer(n))
    assert ctx.worker.deliver_due() == 6
    clock.advance(minutes=61)
    new_lead(client, ctx, **consumer(7))

    assert ctx.worker.deliver_due() == 7

    this_pass = mailer.sent[6:]
    assert this_pass[0].to == "consumer7@example.com"
    assert this_pass[0].subject == INTRO_SUBJECT
    assert sorted(email.to for email in this_pass[1:]) == [f"consumer{n}@example.com" for n in range(1, 7)]
    assert {email.subject for email in this_pass[1:]} == {FOLLOWUP_SUBJECT}


def test_first_response_goes_out_at_night_but_follow_ups_wait_for_morning(client, ctx, mailer, clock):
    # Rule: the intro is sent at any hour; a follow-up landing in quiet hours waits for 8 AM local.
    clock.set(datetime(2026, 11, 13, 0, 30, tzinfo=UTC))  # 19:30 on Nov 12 in Indianapolis
    lead = new_lead(client, ctx, **consumer(1))

    assert ctx.worker.deliver_due() == 1
    assert len(mailer.sent) == 1
    # +60 min = 20:30 local -> held until 08:00 local (13:00 UTC); +24 h = 19:30 local -> unchanged.
    assert message(ctx, lead["id"], "followup", 1)["send_after"] == "2026-11-13T13:00:00.000Z"
    assert message(ctx, lead["id"], "followup", 2)["send_after"] == "2026-11-14T00:30:00.000Z"


# --- failures and retries --------------------------------------------------------------


def test_transient_failure_is_retried_and_the_alert_resolves_when_sending_works_again(client, ctx, mailer, clock):
    # Rule: a provider outage delays the email (5s, then 30s) instead of losing it, and alerts.
    mailer.fail_with = RuntimeError("smtp down")
    mailer.fail_times = 2
    lead = new_lead(client, ctx, **consumer(1))

    assert ctx.worker.deliver_due() == 1
    intro = message(ctx, lead["id"], "intro")
    assert (intro["status"], intro["attempts"]) == ("pending", 1)
    assert intro["send_after"] == iso(START + timedelta(seconds=5))
    assert intro["last_error"] == "RuntimeError: smtp down"
    assert intro["locked_at"] is None
    retries = lead_events(ctx, lead["id"], "email.retry_scheduled")
    assert len(retries) == 1
    assert retries[0]["attempt"] == 1
    assert retries[0]["retry_at"] == iso(START + timedelta(seconds=5))
    assert retries[0]["template"] == "intro"
    assert "smtp down" in retries[0]["error"]
    alerts = email_alerts(ctx)
    assert [(a["status"], a["severity"], a["kind"], a["count"]) for a in alerts] == [
        ("open", "warning", "email_send_error", 1)
    ]

    clock.advance(seconds=4)
    assert ctx.worker.deliver_due() == 0  # not yet
    clock.advance(seconds=1)
    assert ctx.worker.deliver_due() == 1  # second attempt also fails
    intro = message(ctx, lead["id"], "intro")
    assert (intro["status"], intro["attempts"]) == ("pending", 2)
    assert intro["send_after"] == iso(clock() + timedelta(seconds=30))
    assert [(a["status"], a["count"]) for a in email_alerts(ctx)] == [("open", 2)]
    assert mailer.sent == []

    clock.advance(seconds=30)
    assert ctx.worker.deliver_due() == 1  # third attempt succeeds
    intro = message(ctx, lead["id"], "intro")
    assert (intro["status"], intro["attempts"], intro["last_error"]) == ("sent", 3, None)
    assert len(mailer.sent) == 1
    alerts = email_alerts(ctx)
    assert [(a["status"], a["resolved_at"]) for a in alerts] == [("resolved", iso(clock()))]
    assert lead_events(ctx, lead["id"], "email.sent")[0]["attempts"] == 3
    assert lead_events(ctx, lead["id"], "email.failed") == []
    assert lead_row(ctx, lead["id"])["needs_review"] == 0


def test_first_response_backs_off_then_is_retried_every_30_minutes_for_a_day(client, ctx, mailer, clock):
    # Rule: retries wait 5s, 30s, 120s, 600s; after that a first response is not given up
    # but retried every 30 minutes until the lead is 24 hours old. Only then is it failed.
    mailer.fail_with = RuntimeError("smtp down")
    mailer.fail_times = -1  # never recovers
    lead = new_lead(client, ctx, **consumer(1))
    assert lead["needs_review"] == 0

    for attempt, delay in enumerate((5, 30, 120, 600), start=1):
        assert ctx.worker.deliver_due() == 1
        intro = message(ctx, lead["id"], "intro")
        assert (intro["status"], intro["attempts"]) == ("pending", attempt)
        assert parse_iso(intro["send_after"]) == clock() + timedelta(seconds=delay)
        assert email_alerts(ctx)[0]["severity"] == "warning"
        clock.advance(seconds=delay - 1)
        assert ctx.worker.deliver_due() == 0
        clock.advance(seconds=1)

    assert ctx.worker.deliver_due() == 1  # attempt 5: the quick attempts are used up
    intro = message(ctx, lead["id"], "intro")
    assert (intro["status"], intro["attempts"]) == ("pending", 5)  # ...but it is not given up
    assert parse_iso(intro["send_after"]) == clock() + timedelta(minutes=30)
    assert lead_events(ctx, lead["id"], "email.failed") == []
    assert lead_row(ctx, lead["id"])["needs_review"] == 0
    # The response-time promise is long broken, so the alert is critical from here on.
    alerts = email_alerts(ctx)
    assert [(a["status"], a["severity"], a["count"]) for a in alerts] == [("open", "critical", 5)]
    assert json.loads(alerts[0]["detail_json"])["gave_up"] is False

    clock.advance(minutes=29, seconds=59)
    assert ctx.worker.deliver_due() == 0
    clock.advance(seconds=1)
    assert ctx.worker.deliver_due() == 1  # attempt 6, half an hour later
    intro = message(ctx, lead["id"], "intro")
    assert (intro["status"], intro["attempts"]) == ("pending", 6)
    assert parse_iso(intro["send_after"]) == clock() + timedelta(minutes=30)

    # Attempts so far at 0:00, 0:05, 0:35, 2:35, 12:35 and 42:35 (min:sec), then every
    # 30 minutes. The first attempt that fails 24 hours or more after the lead arrived
    # (at 24 h 12 min 35 s) is the last.
    run_until(ctx, clock, START + timedelta(hours=24, minutes=12, seconds=34))
    intro = message(ctx, lead["id"], "intro")
    assert (intro["status"], intro["attempts"]) == ("pending", 52)
    assert lead_events(ctx, lead["id"], "email.failed") == []

    run_until(ctx, clock, START + timedelta(hours=24, minutes=12, seconds=35))
    intro = message(ctx, lead["id"], "intro")
    assert (intro["status"], intro["attempts"]) == ("failed", 53)
    assert intro["last_error"] == "RuntimeError: smtp down"
    assert intro["locked_at"] is None
    assert lead_row(ctx, lead["id"])["needs_review"] == 1
    assert len(lead_events(ctx, lead["id"], "email.retry_scheduled")) == 52
    failed = lead_events(ctx, lead["id"], "email.failed")
    assert [(e["permanent"], e["attempts"], e["template"]) for e in failed] == [(False, 53, "intro")]
    alerts = email_alerts(ctx)
    assert [(a["status"], a["severity"]) for a in alerts] == [("open", "critical")]
    assert json.loads(alerts[0]["detail_json"])["gave_up"] is True

    # A failed message is never picked up again, and the nudges went with it.
    assert [m["status"] for m in messages(ctx, lead["id"], "followup")] == ["cancelled", "cancelled"]
    mailer.fail_with = None
    run_until(ctx, clock, START + timedelta(days=3))
    assert mailer.sent == []
    assert lead_row(ctx, lead["id"])["first_contacted_at"] is None


def test_first_response_goes_out_when_a_long_mail_outage_ends_and_the_follow_up_waits_for_it(
    client, ctx, mailer, clock
):
    # Rule: a mail outage longer than the quick retries delays the intro, it does not lose
    # it; and "just checking in" is never the first thing the consumer hears from us.
    mailer.fail_with = RuntimeError("smtp down")
    mailer.fail_times = -1
    lead = new_lead(client, ctx, **consumer(1))

    run_until(ctx, clock, START + timedelta(minutes=70))  # follow-up 1 fell due at +60 min
    assert mailer.sent == []
    assert message(ctx, lead["id"], "intro")["status"] == "pending"
    waiting = message(ctx, lead["id"], "followup", 1)
    assert (waiting["status"], waiting["attempts"]) == ("pending", 0)  # postponed, not attempted
    assert parse_iso(waiting["send_after"]) > clock()

    mailer.fail_with = None  # the credentials are fixed
    run_until(ctx, clock, START + timedelta(hours=3))

    assert [email.subject for email in mailer.sent] == [INTRO_SUBJECT, FOLLOWUP_SUBJECT]
    intro, follow_up = message(ctx, lead["id"], "intro"), message(ctx, lead["id"], "followup", 1)
    assert intro["sent_at"] == iso(START + timedelta(minutes=72, seconds=35))  # the next 30-minute retry
    # The follow-up was put off twice by 30 minutes: at +60 (intro not out yet) and at +90
    # (the intro had gone out 17 minutes earlier).
    assert follow_up["sent_at"] == iso(START + timedelta(minutes=120))
    assert lead_row(ctx, lead["id"])["first_contacted_at"] == intro["sent_at"]
    assert lead_row(ctx, lead["id"])["needs_review"] == 0
    assert [a["status"] for a in email_alerts(ctx)] == ["resolved"]
    assert lead_events(ctx, lead["id"], "email.cancelled") == []


def test_a_repeat_request_acknowledgement_is_a_first_response_too(client, ctx, mailer, clock):
    # Rule: for a duplicate lead the short acknowledgement is the first response, so it gets
    # the same staying power as an intro.
    new_lead(client, ctx, Email="pat@example.com")
    ctx.worker.deliver_due()
    clock.advance(minutes=5)
    again = new_lead(client, ctx, Email="pat@example.com")
    mailer.fail_with = RuntimeError("smtp down")
    mailer.fail_times = -1

    run_until(ctx, clock, clock() + timedelta(minutes=20))  # past the five quick attempts

    repeat = message(ctx, again["id"], "repeat_request")
    assert (repeat["status"], repeat["attempts"]) == ("pending", 5)
    assert parse_iso(repeat["send_after"]) - clock() <= timedelta(minutes=30)

    mailer.fail_with = None
    run_until(ctx, clock, clock() + timedelta(minutes=30))
    assert message(ctx, again["id"], "repeat_request")["status"] == "sent"


def test_follow_up_is_given_up_after_max_attempts(client, ctx, mailer, clock):
    # Rule: only a first response is retried for a day; a nudge is dropped after 5 attempts.
    lead = new_lead(client, ctx, **consumer(1))
    ctx.worker.deliver_due()
    clock.advance(minutes=60)
    mailer.fail_with = RuntimeError("smtp down")
    mailer.fail_times = -1

    run_until(ctx, clock, START + timedelta(hours=3))

    follow_up = message(ctx, lead["id"], "followup", 1)
    assert (follow_up["status"], follow_up["attempts"]) == ("failed", 5)
    # 5 s + 30 s + 120 s + 600 s after it fell due.
    failed_at = db_one(ctx, "SELECT occurred_at FROM lead_events WHERE lead_id = ? AND type = 'email.failed'", (lead["id"],))
    assert failed_at["occurred_at"] == iso(START + timedelta(minutes=60, seconds=755))
    alerts = email_alerts(ctx)
    assert [(a["status"], a["severity"]) for a in alerts] == [("open", "critical")]
    assert json.loads(alerts[0]["detail_json"])["gave_up"] is True
    assert message(ctx, lead["id"], "followup", 2)["status"] == "pending"  # the next nudge is unaffected
    assert len(mailer.sent) == 1


def test_booking_confirmation_is_given_up_after_max_attempts(client, ctx, mailer, clock):
    # Rule: a confirmation is sequence 0 like an intro, but it is not a first response.
    lead = new_lead(client, ctx, **consumer(1))
    ctx.worker.deliver_due()
    book_first_open_slot(client, lead)
    mailer.fail_with = RuntimeError("smtp down")
    mailer.fail_times = -1

    run_until(ctx, clock, START + timedelta(hours=3))

    confirmation = message(ctx, lead["id"], "booking_confirmation")
    assert (confirmation["status"], confirmation["attempts"]) == ("failed", 5)
    assert lead_row(ctx, lead["id"])["needs_review"] == 1


def test_operator_retry_after_the_day_is_over_gets_the_quick_attempts_only(client, ctx, mailer, clock):
    # Rule: the 24 hours count from when the lead arrived; a later manual retry does not restart them.
    mailer.fail_with = RuntimeError("smtp down")
    mailer.fail_times = -1
    lead = new_lead(client, ctx, **consumer(1))
    run_until(ctx, clock, START + timedelta(hours=25))
    intro = message(ctx, lead["id"], "intro")
    assert intro["status"] == "failed"

    with ctx.db.transaction() as conn:
        assert outbox.requeue_failed(conn, intro["id"], clock()) is True
    run_until(ctx, clock, START + timedelta(hours=27))

    intro = message(ctx, lead["id"], "intro")
    assert (intro["status"], intro["attempts"]) == ("failed", 5)


def test_permanent_error_fails_on_the_first_attempt_and_opens_no_alert(client, ctx, mailer, clock):
    # Rule: a rejected mailbox cannot be fixed by retrying and is not an outage on our side.
    mailer.fail_with = PermanentEmailError("recipient refused: no such mailbox")
    mailer.fail_times = -1
    lead = new_lead(client, ctx, **consumer(1))

    assert ctx.worker.deliver_due() == 1

    intro = message(ctx, lead["id"], "intro")
    assert (intro["status"], intro["attempts"]) == ("failed", 1)
    assert intro["last_error"] == "recipient refused: no such mailbox"
    assert lead_row(ctx, lead["id"])["needs_review"] == 1
    assert lead_events(ctx, lead["id"], "email.retry_scheduled") == []
    failed = lead_events(ctx, lead["id"], "email.failed")
    assert [(e["permanent"], e["attempts"]) for e in failed] == [(True, 1)]
    assert email_alerts(ctx) == []
    assert lead_row(ctx, lead["id"])["first_contacted_at"] is None

    clock.advance(seconds=600)
    assert ctx.worker.deliver_due() == 0


def test_follow_ups_are_cancelled_when_the_first_email_failed_for_good(client, ctx, mailer, clock):
    # Rule: the introduction never arrived, so no "just checking in" is sent to that address.
    mailer.fail_with = PermanentEmailError("recipient refused: no such mailbox")
    mailer.fail_times = 1
    lead = new_lead(client, ctx, **consumer(1))

    assert ctx.worker.deliver_due() == 1

    assert [(m["status"], m["last_error"]) for m in messages(ctx, lead["id"], "followup")] == [
        ("cancelled", "first_touch_failed"),
        ("cancelled", "first_touch_failed"),
    ]
    assert {"reason": "first_touch_failed", "count": 2, "templates": ["followup"]} in lead_events(
        ctx, lead["id"], "email.cancelled"
    )
    run_until(ctx, clock, START + timedelta(hours=25))
    assert mailer.sent == []


def test_follow_up_already_claimed_when_the_first_email_fails_is_cancelled_too(client, ctx, mailer, clock):
    # Rule: after downtime the intro and an overdue follow-up are picked up together; if the
    # intro is then refused, the follow-up in the same batch must not go out on its own.
    lead = new_lead(client, ctx, **consumer(1))
    clock.advance(hours=2)  # the worker was down: the intro and follow-up 1 are both due
    mailer.fail_with = PermanentEmailError("recipient refused: no such mailbox")
    mailer.fail_times = 1

    assert ctx.worker.deliver_due() == 2

    assert message(ctx, lead["id"], "intro")["status"] == "failed"
    assert_cancelled(ctx, lead["id"], "followup", 1, "first_touch_not_delivered")
    assert message(ctx, lead["id"], "followup", 2)["last_error"] == "first_touch_failed"
    assert mailer.sent == []


def test_follow_up_waits_while_the_first_email_is_still_being_sent(client, ctx, mailer, clock):
    # Rule: a first email that is claimed but not finished counts as "not out yet".
    lead = new_lead(client, ctx, **consumer(1))
    outbox.claim_next(ctx.db, clock())  # the intro is mid-send (or was cut off by a crash)
    clock.advance(minutes=60)

    assert ctx.worker.deliver_due() == 1

    waiting = message(ctx, lead["id"], "followup", 1)
    assert (waiting["status"], waiting["attempts"]) == ("pending", 0)
    assert waiting["send_after"] == iso(START + timedelta(minutes=90))
    assert mailer.sent == []


def test_follow_up_without_any_first_email_is_cancelled(ctx, quiet_lead, mailer, clock):
    # Rule: no first email was ever queued for this lead, so a nudge would be the first contact.
    enqueue(ctx, quiet_lead, template="followup", dedupe_key="f1", send_after=START, sequence=1)

    assert ctx.worker.deliver_due() == 1

    assert_cancelled(ctx, quiet_lead["id"], "followup", 1, "first_touch_not_delivered")
    assert mailer.sent == []


def test_follow_up_is_cancelled_when_the_first_email_was_cancelled(client, ctx, mailer, clock):
    # Rule: same when the intro was called off (here: the business was switched off for a while).
    lead = new_lead(client, ctx, **consumer(1))
    sql(ctx, "UPDATE tenants SET is_active = 0 WHERE id = 'tnt_sparkle'")
    ctx.worker.deliver_due()
    assert message(ctx, lead["id"], "intro")["status"] == "cancelled"
    sql(ctx, "UPDATE tenants SET is_active = 1 WHERE id = 'tnt_sparkle'")

    clock.advance(minutes=60)
    assert ctx.worker.deliver_due() == 1

    assert_cancelled(ctx, lead["id"], "followup", 1, "first_touch_not_delivered")
    assert mailer.sent == []


def test_a_provider_that_really_sends_is_never_given_an_example_com_address(client, ctx, mailer, monkeypatch):
    # Rule: demo and documentation leads use example.com; real mail there only bounces.
    monkeypatch.setattr(mailer, "delivers_externally", True, raising=False)  # as SMTP and Resend declare
    demo = new_lead(client, ctx, **consumer(1))
    real = new_lead(client, ctx, Email="pat.lee@gmail.com", PhoneNumber="3175552000", FirstName="Pat")

    assert ctx.worker.deliver_due() == 2

    intro = message(ctx, demo["id"], "intro")
    assert (intro["status"], intro["attempts"]) == ("failed", 1)
    assert intro["last_error"] == "address is on a reserved example domain; nothing was sent"
    assert [m["status"] for m in messages(ctx, demo["id"], "followup")] == ["cancelled", "cancelled"]
    assert email_alerts(ctx) == []  # not an outage
    assert [email.to for email in mailer.sent] == ["pat.lee@gmail.com"]
    assert message(ctx, real["id"], "intro")["status"] == "sent"


def test_permanent_failure_of_one_lead_does_not_resolve_an_open_outage_alert(client, ctx, mailer):
    # Rule: only a successful send proves the provider works again.
    mailer.fail_with = RuntimeError("smtp down")
    mailer.fail_times = 1
    new_lead(client, ctx, **consumer(1))
    ctx.worker.deliver_due()
    assert [a["status"] for a in email_alerts(ctx)] == ["open"]

    mailer.fail_with = PermanentEmailError("no such mailbox")
    mailer.fail_times = 1
    new_lead(client, ctx, **consumer(2))
    ctx.worker.deliver_due()

    assert [a["status"] for a in email_alerts(ctx)] == ["open"]


def test_a_template_bug_takes_the_retry_path_instead_of_killing_delivery(client, ctx, mailer, clock, monkeypatch):
    # Rule: an exception while rendering is a failed attempt for that message, nothing more.
    def broken_render(*args, **kwargs):
        raise ZeroDivisionError("template bug")

    lead = new_lead(client, ctx, **consumer(1))
    with monkeypatch.context() as patch:
        patch.setattr(ctx.renderer, "render", broken_render)
        assert ctx.worker.deliver_due() == 1  # returns normally

    intro = message(ctx, lead["id"], "intro")
    assert (intro["status"], intro["attempts"]) == ("pending", 1)
    assert intro["last_error"] == "ZeroDivisionError: template bug"
    assert intro["send_after"] == iso(START + timedelta(seconds=5))
    assert len(lead_events(ctx, lead["id"], "email.retry_scheduled")) == 1
    assert mailer.sent == []

    # Once the template is fixed the same message goes out.
    clock.advance(seconds=5)
    assert ctx.worker.deliver_due() == 1
    assert message(ctx, lead["id"], "intro")["status"] == "sent"
    assert len(mailer.sent) == 1


def test_one_failing_message_does_not_stop_the_others_in_the_batch(client, ctx, mailer):
    # Rule: leads are independent; one bad send must not block the rest.
    real_send = mailer.send

    def fail_for_one(email):
        if email.to == "consumer2@example.com":
            raise RuntimeError("connection reset")
        return real_send(email)

    mailer.send = fail_for_one
    leads = [new_lead(client, ctx, **consumer(n)) for n in (1, 2, 3)]

    assert ctx.worker.deliver_due() == 3

    assert sorted(email.to for email in mailer.sent) == ["consumer1@example.com", "consumer3@example.com"]
    assert [message(ctx, lead["id"], "intro")["status"] for lead in leads] == ["sent", "pending", "sent"]


def test_message_stuck_in_sending_is_requeued_after_the_grace_period_and_delivered(client, ctx, mailer, clock):
    # Rule: a crash mid-send must not lose the email; it is retried after STUCK_AFTER_SECONDS.
    lead = new_lead(client, ctx, **consumer(1))
    claimed = outbox.claim_next(ctx.db, clock())  # ...and the process dies here
    assert claimed["status"] == "sending"
    assert message(ctx, lead["id"], "intro")["locked_at"] == iso(START)

    # Recently locked: assumed to be in flight, left alone.
    clock.advance(seconds=outbox.STUCK_AFTER_SECONDS)
    ctx.worker.run_maintenance()
    assert message(ctx, lead["id"], "intro")["status"] == "sending"
    assert ctx.worker.deliver_due() == 0
    assert mailer.sent == []

    clock.advance(seconds=1)
    ctx.worker.run_maintenance()
    requeued = message(ctx, lead["id"], "intro")
    assert (requeued["status"], requeued["locked_at"]) == ("pending", None)

    assert ctx.worker.deliver_due() == 1
    sent = message(ctx, lead["id"], "intro")
    assert (sent["status"], sent["attempts"]) == ("sent", 2)
    assert [email.to for email in mailer.sent] == ["consumer1@example.com"]


# --- the mail server said yes, but we could not write it down ------------------------------


def test_a_failure_to_record_a_send_is_retried_without_sending_again(client, ctx, mailer, clock, monkeypatch):
    # Rule: once the mail server accepted the message, a database hiccup is not a send failure.
    lead = new_lead(client, ctx, **consumer(1))
    real_record = ctx.worker._record_sent
    tries = []

    def locked_once(*args):
        tries.append(1)
        if len(tries) == 1:
            raise sqlite3.OperationalError("database is locked")
        return real_record(*args)

    monkeypatch.setattr(ctx.worker, "_record_sent", locked_once)

    assert ctx.worker.deliver_due() == 1

    assert len(tries) == 2
    assert len(mailer.sent) == 1
    intro = message(ctx, lead["id"], "intro")
    assert (intro["status"], intro["attempts"], intro["last_error"]) == ("sent", 1, None)
    assert intro["body_text"] == mailer.sent[0].text
    assert lead_row(ctx, lead["id"])["first_contacted_at"] == iso(clock())
    assert email_alerts(ctx) == []  # no "Outbound email is failing"
    assert lead_events(ctx, lead["id"], "email.retry_scheduled") == []
    clock.advance(seconds=5)
    assert ctx.worker.deliver_due() == 0  # and no second send five seconds later


def test_a_send_that_can_never_be_recorded_stays_in_sending_and_is_resent_once_the_database_is_back(
    client, ctx, mailer, clock
):
    # Rule: the documented at-least-once case. The row is left 'sending' (no alert, no
    # quick retry); after the grace period it is sent again and recorded.
    lead = new_lead(client, ctx, **consumer(1))
    real_send = mailer.send

    with pytest.MonkeyPatch.context() as outage:

        def accept_then_lose_the_database(email):
            result = real_send(email)
            outage.setattr(ctx.db, "transaction", database_locked)
            return result

        outage.setattr(mailer, "send", accept_then_lose_the_database)
        assert ctx.worker.deliver(outbox.claim_next(ctx.db, clock())) == "sent"

    assert len(mailer.sent) == 1
    intro = message(ctx, lead["id"], "intro")
    assert (intro["status"], intro["attempts"], intro["last_error"]) == ("sending", 1, None)
    assert email_alerts(ctx) == []
    assert lead_events(ctx, lead["id"], "email.retry_scheduled") == []
    clock.advance(seconds=5)
    assert ctx.worker.deliver_due() == 0

    clock.advance(seconds=outbox.STUCK_AFTER_SECONDS)
    ctx.worker.run_maintenance()
    assert ctx.worker.deliver_due() == 1
    assert len(mailer.sent) == 2  # the same email, a second time
    intro = message(ctx, lead["id"], "intro")
    assert (intro["status"], intro["attempts"]) == ("sent", 2)


def test_a_send_whose_details_cannot_be_stored_is_still_marked_sent_and_never_repeated(
    client, ctx, mailer, clock, monkeypatch
):
    # Rule: if only the audit detail is the problem, the row is marked sent on its own;
    # otherwise the same email would go out again every couple of minutes, forever.
    lead = new_lead(client, ctx, **consumer(1))

    def unstorable(*args):
        raise ValueError("a value the database cannot store")

    monkeypatch.setattr(ctx.worker, "_record_sent", unstorable)

    assert ctx.worker.deliver_due() == 1

    intro = message(ctx, lead["id"], "intro")
    assert (intro["status"], intro["sent_at"], intro["locked_at"]) == ("sent", iso(clock()), None)
    assert intro["last_error"] == "sent, but the details could not be recorded"
    assert email_alerts(ctx) == []
    clock.advance(seconds=outbox.STUCK_AFTER_SECONDS + 1)
    ctx.worker.run_maintenance()
    assert ctx.worker.deliver_due() == 0
    assert len(mailer.sent) == 1


def test_content_the_database_could_not_store_is_cleaned_before_it_is_sent(client, ctx, mailer, monkeypatch):
    # Rule: half an emoji or a control character in an email must not make "record the send" fail.
    lead = new_lead(client, ctx, **consumer(1))
    broken = RenderedEmail(subject="Thanks \ud83d", text="Thanks \ud83d\x00\n", html="<p>Thanks \ud83d\x00</p>\n")
    monkeypatch.setattr(ctx.renderer, "render", lambda *args, **kwargs: broken)

    assert ctx.worker.deliver_due() == 1

    (email,) = mailer.sent
    assert (email.subject, email.text, email.html) == ("Thanks \ufffd", "Thanks \ufffd\n", "<p>Thanks \ufffd</p>\n")
    intro = message(ctx, lead["id"], "intro")
    assert (intro["status"], intro["attempts"]) == ("sent", 1)
    assert (intro["subject"], intro["body_text"]) == (email.subject, email.text)
    assert email_alerts(ctx) == []


def test_delivery_survives_a_database_that_cannot_record_the_failure(client, ctx, mailer, clock):
    # Rule: deliver() never raises. If neither the send nor the note about it works, the row
    # stays 'sending' and comes back through the usual recovery; nothing is lost.
    lead = new_lead(client, ctx, **consumer(1))
    mailer.fail_with = RuntimeError("smtp down")
    mailer.fail_times = 1
    claimed = outbox.claim_next(ctx.db, clock())

    with pytest.MonkeyPatch.context() as outage:
        outage.setattr(ctx.db, "transaction", database_locked)
        assert ctx.worker.deliver(claimed) == "retry"

    assert message(ctx, lead["id"], "intro")["status"] == "sending"
    clock.advance(seconds=outbox.STUCK_AFTER_SECONDS + 1)
    ctx.worker.run_maintenance()
    assert ctx.worker.deliver_due() == 1
    assert [email.to for email in mailer.sent] == ["consumer1@example.com"]


def test_messages_already_claimed_are_sent_even_if_claiming_the_next_one_fails(client, ctx, mailer, monkeypatch):
    # Rule: a claimed message is never left behind in 'sending' because the batch could not be filled.
    leads = [new_lead(client, ctx, **consumer(n)) for n in (1, 2)]
    real_claim = outbox.claim_next
    calls = []

    def locked_on_the_second_claim(db, now):
        calls.append(1)
        if len(calls) == 2:
            raise sqlite3.OperationalError("database is locked")
        return real_claim(db, now)

    monkeypatch.setattr(outbox, "claim_next", locked_on_the_second_claim)

    assert ctx.worker.deliver_due() == 2

    assert [message(ctx, lead["id"], "intro")["status"] for lead in leads] == ["sent", "sent"]
    assert len(mailer.sent) == 2


# --- send-time cancellations -----------------------------------------------------------


def assert_cancelled(ctx, lead_id: str, template: str, sequence: int, reason: str) -> None:
    row = message(ctx, lead_id, template, sequence)
    assert (row["status"], row["last_error"], row["locked_at"]) == ("cancelled", reason, None)
    assert row["sent_at"] is None
    cancelled = [e for e in lead_events(ctx, lead_id, "email.cancelled") if e["reason"] == reason]
    assert {"reason": reason, "count": 1, "templates": [template]} in cancelled


def test_follow_up_is_cancelled_at_send_time_when_the_address_has_unsubscribed(client, ctx, mailer, clock):
    # Rule: an unsubscribe through one request stops email for the consumer's other requests too.
    first = new_lead(client, ctx, Email="pat@example.com")
    again = new_lead(client, ctx, Email="pat@example.com")  # same consumer -> repeat request
    assert ctx.worker.deliver_due() == 2
    assert client.post(f"/unsubscribe/{again['booking_token']}").status_code == 200
    # The first lead's follow-ups were not touched by that unsubscribe...
    assert [m["status"] for m in messages(ctx, first["id"], "followup")] == ["pending", "pending"]

    clock.advance(minutes=60)
    assert ctx.worker.deliver_due() == 1

    # ...so the check at send time is what stops them.
    assert_cancelled(ctx, first["id"], "followup", 1, "unsubscribed")
    assert len(mailer.sent) == 2


def test_intro_is_cancelled_at_send_time_when_the_address_was_suppressed_after_queueing(client, ctx, mailer):
    # Rule: suppression is checked when sending, not only when queueing.
    lead = new_lead(client, ctx, Email="pat@example.com")
    sql(
        ctx,
        "INSERT INTO email_suppressions (tenant_id, email_normalized, reason, created_at) VALUES (?, ?, ?, ?)",
        ("tnt_sparkle", "pat@example.com", "complaint", iso(START)),
    )

    assert ctx.worker.deliver_due() == 1

    assert_cancelled(ctx, lead["id"], "intro", 0, "unsubscribed")
    assert mailer.sent == []
    assert lead_row(ctx, lead["id"])["status"] == "new"


def test_claimed_follow_up_is_cancelled_when_the_lead_books_in_the_meantime(client, ctx, mailer, clock):
    # Rule: nobody is nudged to book after they have booked.
    lead = new_lead(client, ctx, **consumer(1))
    ctx.worker.deliver_due()
    clock.advance(minutes=60)
    claimed = outbox.claim_next(ctx.db, clock())  # the worker has picked up follow-up 1...
    assert (claimed["template"], claimed["sequence"]) == ("followup", 1)
    book_first_open_slot(client, lead)  # ...and the lead books before it is sent

    assert ctx.worker.deliver(claimed) == "cancelled"

    assert_cancelled(ctx, lead["id"], "followup", 1, "no_longer_needed")
    assert FOLLOWUP_SUBJECT not in [email.subject for email in mailer.sent]


def test_claimed_follow_up_is_cancelled_when_an_outcome_was_recorded(client, ctx, mailer, clock):
    # Rule: a closed lead (won or lost) gets no more nudges.
    lead = new_lead(client, ctx, **consumer(1))
    ctx.worker.deliver_due()
    clock.advance(minutes=60)
    claimed = outbox.claim_next(ctx.db, clock())
    response = client.post(f"/admin/api/leads/{lead['id']}/outcome", json={"outcome": "lost"}, headers=ADMIN)
    assert response.status_code == 200, response.text

    assert ctx.worker.deliver(claimed) == "cancelled"

    assert_cancelled(ctx, lead["id"], "followup", 1, "no_longer_needed")
    assert len(mailer.sent) == 1  # only the intro


def test_messages_are_cancelled_when_the_tenant_was_deactivated(client, ctx, mailer):
    # Rule: we never email on behalf of a business that has been switched off.
    lead = new_lead(client, ctx, **consumer(1))
    sql(ctx, "UPDATE tenants SET is_active = 0 WHERE id = 'tnt_sparkle'")

    assert ctx.worker.deliver_due() == 1

    assert_cancelled(ctx, lead["id"], "intro", 0, "tenant_inactive")
    assert mailer.sent == []


def test_booking_confirmation_is_cancelled_when_its_appointment_was_cancelled(client, ctx, mailer):
    # Rule: do not confirm a visit that no longer exists.
    lead = new_lead(client, ctx, **consumer(1))
    ctx.worker.deliver_due()
    book_first_open_slot(client, lead)
    sql(ctx, "UPDATE appointments SET status = 'cancelled' WHERE lead_id = ?", (lead["id"],))

    assert ctx.worker.deliver_due() == 1

    assert_cancelled(ctx, lead["id"], "booking_confirmation", 0, "appointment_cancelled")
    assert [email.subject for email in mailer.sent] == [INTRO_SUBJECT]


def test_rebooking_before_the_worker_runs_sends_a_single_confirmation(client, ctx, mailer):
    # Rule: the confirmation for an appointment that was replaced must not go out.
    lead = new_lead(client, ctx, **consumer(1))
    ctx.worker.deliver_due()
    page = client.get(f"/book/{lead['booking_token']}")
    first, second = re.findall(r'name="slot" value="([^"]+)"', page.text)[:2]
    for slot in (first, second):  # changes their mind before the worker's next pass
        assert client.post(f"/book/{lead['booking_token']}", data={"slot": slot}, follow_redirects=False).status_code == 303

    ctx.worker.deliver_due()

    confirmations = [email for email in mailer.sent if email.subject.startswith("You're booked")]
    assert len(confirmations) == 1


def test_follow_up_is_cancelled_when_the_consumer_booked_through_a_different_lead(client, ctx, mailer, clock):
    # Rule: one person, two requests: booking either one ends the nudges for both.
    first = new_lead(client, ctx, Email="pat@example.com")
    ctx.worker.deliver_due()
    clock.advance(minutes=10)
    again = new_lead(client, ctx, Email="pat@example.com")  # repeat request, no cadence of its own
    assert messages(ctx, again["id"], "followup") == []
    ctx.worker.deliver_due()
    book_first_open_slot(client, again)
    ctx.worker.deliver_due()  # booking confirmation for the second lead
    assert [m["status"] for m in messages(ctx, first["id"], "followup")] == ["pending", "pending"]

    clock.advance(minutes=50)  # the first lead's follow-up 1 is due
    assert ctx.worker.deliver_due() == 1

    assert_cancelled(ctx, first["id"], "followup", 1, "no_longer_needed")
    assert FOLLOWUP_SUBJECT not in [email.subject for email in mailer.sent]
    assert lead_row(ctx, first["id"])["booked_at"] is None  # it was the other lead that booked


def test_booking_with_another_tenant_does_not_cancel_this_tenants_follow_up(client, ctx, mailer, clock):
    # Rule: tenants are isolated; a booking with business B says nothing about business A.
    sparkle = new_lead(client, ctx, Email="pat@example.com")
    summit = new_lead(client, ctx, Email="pat@example.com", ALAccountId=SUMMIT_ADS_ACCOUNT)
    assert summit["tenant_id"] == "tnt_summit"
    ctx.worker.deliver_due()
    book_first_open_slot(client, summit)
    ctx.worker.deliver_due()

    clock.advance(minutes=60)
    assert ctx.worker.deliver_due() == 1

    assert message(ctx, sparkle["id"], "followup", 1)["status"] == "sent"
    assert mailer.sent[-1].subject == FOLLOWUP_SUBJECT
    assert mailer.sent[-1].from_name == "Sparkle Home Cleaning"


def test_returning_customer_who_booked_months_ago_still_gets_follow_ups(client, ctx, mailer, clock):
    # Rule: only a booking for the current request ends its nudges; last season's job does not.
    old = new_lead(client, ctx, Email="pat@example.com")
    ctx.worker.deliver_due()
    book_first_open_slot(client, old)
    ctx.worker.deliver_due()
    clock.advance(days=50)  # past the 45-day duplicate window: this is a brand-new request
    new = new_lead(client, ctx, Email="pat@example.com")
    assert new["is_duplicate"] == 0
    assert [m["status"] for m in messages(ctx, new["id"], "followup")] == ["pending", "pending"]
    ctx.worker.deliver_due()  # its intro

    clock.advance(minutes=60)
    ctx.worker.deliver_due()

    assert message(ctx, new["id"], "followup", 1)["status"] == "sent"
    assert mailer.sent[-1].subject == FOLLOWUP_SUBJECT


# --- minimum gap between emails --------------------------------------------------------


def test_follow_up_is_postponed_when_the_address_was_emailed_less_than_30_minutes_ago(client, ctx, mailer, clock):
    # Rule: no nudge within 30 minutes of another email from the same business.
    first = new_lead(client, ctx, Email="pat@example.com")
    ctx.worker.deliver_due()
    clock.advance(minutes=45)
    new_lead(client, ctx, Email="pat@example.com")  # repeat request, answered at +45 min
    assert ctx.worker.deliver_due() == 1
    assert len(mailer.sent) == 2

    clock.advance(minutes=15)  # +60 min: follow-up 1 is due, 15 minutes after the last email
    assert ctx.worker.deliver_due() == 1
    postponed = message(ctx, first["id"], "followup", 1)
    assert (postponed["status"], postponed["attempts"], postponed["locked_at"]) == ("pending", 0, None)
    assert postponed["send_after"] == iso(START + timedelta(minutes=90))
    assert len(mailer.sent) == 2

    clock.advance(minutes=29, seconds=59)
    assert ctx.worker.deliver_due() == 0
    clock.advance(seconds=1)  # +90 min: the gap has passed
    assert ctx.worker.deliver_due() == 1
    sent = message(ctx, first["id"], "followup", 1)
    assert (sent["status"], sent["attempts"]) == ("sent", 1)
    assert [email.subject for email in mailer.sent][-1] == FOLLOWUP_SUBJECT


def test_follow_up_claimed_in_the_same_batch_as_a_repeat_request_waits(client, ctx, mailer, clock):
    # Rule: the first response goes out first and the follow-up to the same person then waits.
    first = new_lead(client, ctx, Email="pat@example.com")
    ctx.worker.deliver_due()
    clock.advance(minutes=60)  # follow-up 1 is due...
    again = new_lead(client, ctx, Email="pat@example.com")  # ...just as a repeat request arrives

    assert ctx.worker.deliver_due() == 2

    assert message(ctx, again["id"], "repeat_request")["status"] == "sent"
    postponed = message(ctx, first["id"], "followup", 1)
    assert (postponed["status"], postponed["attempts"]) == ("pending", 0)
    assert postponed["send_after"] == iso(START + timedelta(minutes=90))
    assert [email.subject for email in mailer.sent] == [
        INTRO_SUBJECT,
        "We got your new request - Sparkle Home Cleaning",
    ]

    clock.advance(minutes=30)
    assert ctx.worker.deliver_due() == 1
    assert mailer.sent[-1].subject == FOLLOWUP_SUBJECT


def test_an_email_from_another_tenant_does_not_postpone_the_follow_up(client, ctx, mailer, clock):
    # Rule: the 30-minute gap is per business; other tenants' email is not ours to count.
    sparkle = new_lead(client, ctx, Email="pat@example.com")
    ctx.worker.deliver_due()
    clock.advance(minutes=45)
    new_lead(client, ctx, Email="pat@example.com", ALAccountId=SUMMIT_ADS_ACCOUNT)
    ctx.worker.deliver_due()  # Summit's intro goes out at +45 min

    clock.advance(minutes=15)
    assert ctx.worker.deliver_due() == 1

    assert message(ctx, sparkle["id"], "followup", 1)["status"] == "sent"
    assert mailer.sent[-1].subject == FOLLOWUP_SUBJECT


def test_two_overdue_follow_ups_for_one_lead_are_not_sent_together(client, ctx, mailer, clock):
    # Rule: after downtime the consumer gets one nudge, then the next one at least 30 minutes later.
    lead = new_lead(client, ctx, **consumer(1))
    ctx.worker.deliver_due()
    clock.advance(hours=30)  # the worker was down: follow-ups 1 and 2 are both overdue

    # Hold each send until the other thread is sending too (or 5s pass), so the result
    # does not depend on thread timing. Once the bug is fixed this wrapper can go.
    together = threading.Barrier(2)
    real_send = mailer.send

    def send_when_both_are_ready(email):
        try:
            together.wait(timeout=5)
        except threading.BrokenBarrierError:
            pass
        return real_send(email)

    mailer.send = send_when_both_are_ready
    ctx.worker.deliver_due()

    follow_ups = [email for email in mailer.sent if email.subject == FOLLOWUP_SUBJECT]
    assert len(follow_ups) == 1
    assert len([m for m in messages(ctx, lead["id"], "followup") if m["status"] == "sent"]) == 1


def test_follow_up_postponed_into_quiet_hours_waits_for_the_morning(client, ctx, mailer, clock):
    # Rule: no "just checking in" between 8 PM and 8 AM local, however the send time came about.
    clock.set(datetime(2026, 11, 12, 23, 50, tzinfo=UTC))  # 18:50 in Indianapolis
    first = new_lead(client, ctx, Email="pat@example.com")
    ctx.worker.deliver_due()
    assert message(ctx, first["id"], "followup", 1)["send_after"] == "2026-11-13T00:50:00.000Z"  # 19:50 local
    clock.advance(minutes=50)  # 19:40: a repeat request is answered
    new_lead(client, ctx, Email="pat@example.com")
    ctx.worker.deliver_due()
    clock.advance(minutes=10)  # 19:50: follow-up 1 is due but the address was emailed 10 minutes ago
    ctx.worker.deliver_due()
    assert message(ctx, first["id"], "followup", 1)["status"] == "pending"

    clock.advance(minutes=30)  # 20:20 local: inside quiet hours
    ctx.worker.deliver_due()

    assert FOLLOWUP_SUBJECT not in [email.subject for email in mailer.sent]
    assert message(ctx, first["id"], "followup", 1)["status"] == "pending"


# --- what the email looks like on the wire ---------------------------------------------


def test_redirect_all_sends_to_the_safe_address_but_audits_the_real_subject(client, ctx, mailer, settings):
    # Rule: with EMAIL_REDIRECT_ALL_TO set no consumer is emailed; the subject says who it was for.
    settings.email_redirect_all_to = "dev@example.test"
    lead = new_lead(client, ctx, Email="pat@example.com")

    ctx.worker.deliver_due()

    assert len(mailer.sent) == 1
    assert mailer.sent[0].to == "dev@example.test"
    assert mailer.sent[0].subject == f"[for pat@example.com] {INTRO_SUBJECT}"
    stored = message(ctx, lead["id"], "intro")
    assert stored["subject"] == INTRO_SUBJECT
    assert stored["to_address"] == "pat@example.com"
    assert lead_events(ctx, lead["id"], "email.sent")[0]["to"] == "dev@example.test"


@pytest.mark.parametrize(
    ("tenant_reply_to", "global_reply_to", "expected"),
    [
        ("office@sparkle.example", "global@example.test", "office@sparkle.example"),
        (None, "global@example.test", "global@example.test"),
        ("", "global@example.test", "global@example.test"),
        ("office@sparkle.example", "", "office@sparkle.example"),
        (None, "", None),
    ],
)
def test_reply_to_prefers_the_tenant_then_the_global_setting(
    client, ctx, mailer, settings, tenant_reply_to, global_reply_to, expected
):
    # Rule: replies go to the business if it has an address, else to the configured default.
    settings.email_reply_to = global_reply_to
    sql(ctx, "UPDATE tenants SET reply_to_email = ? WHERE id = 'tnt_sparkle'", (tenant_reply_to,))
    new_lead(client, ctx, **consumer(1))

    ctx.worker.deliver_due()

    assert mailer.sent[0].reply_to == expected


def test_marketing_templates_carry_one_click_unsubscribe_headers_and_confirmations_do_not(client, ctx, mailer, clock):
    # Rule: intro, repeat request and follow-up can be unsubscribed from; a confirmation is transactional.
    lead = new_lead(client, ctx, Email="pat@example.com")
    ctx.worker.deliver_due()
    clock.advance(minutes=60)
    ctx.worker.deliver_due()  # follow-up 1
    clock.advance(minutes=60)
    repeat = new_lead(client, ctx, Email="pat@example.com")
    ctx.worker.deliver_due()  # repeat request
    book_first_open_slot(client, lead)
    ctx.worker.deliver_due()  # booking confirmation

    by_template = {}
    for row in db_rows(ctx, "SELECT lead_id, template, subject FROM outbox_messages WHERE status = 'sent'"):
        email = next(e for e in mailer.sent if e.subject == row["subject"])
        by_template[row["template"]] = (email, row["lead_id"])
    assert set(by_template) == {"intro", "followup", "repeat_request", "booking_confirmation"}

    tokens = {lead["id"]: lead["booking_token"], repeat["id"]: repeat["booking_token"]}
    for template in ("intro", "followup", "repeat_request"):
        email, lead_id = by_template[template]
        assert email.headers == {
            "List-Unsubscribe": f"<https://leads.example.test/unsubscribe/{tokens[lead_id]}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        }
    confirmation, _ = by_template["booking_confirmation"]
    assert "List-Unsubscribe" not in confirmation.headers
    assert "List-Unsubscribe-Post" not in confirmation.headers


def test_email_is_sent_in_the_tenants_name_from_the_configured_address(client, ctx, mailer):
    # Rule: the consumer sees the business they contacted; the envelope uses our sending address.
    new_lead(client, ctx, **consumer(1))
    new_lead(client, ctx, **consumer(2), ALAccountId=SUMMIT_ADS_ACCOUNT)

    ctx.worker.deliver_due()

    senders = {email.to: (email.from_name, email.from_address) for email in mailer.sent}
    assert senders == {
        "consumer1@example.com": ("Sparkle Home Cleaning", "leads@example.test"),
        "consumer2@example.com": ("Summit Heating & Air", "leads@example.test"),
    }


def test_slots_listed_in_an_email_are_the_ones_still_open_when_it_is_sent(client, ctx, mailer):
    # Rule: availability is read at send time, so an email never offers a window already taken.
    first = new_lead(client, ctx, **consumer(1))
    ctx.worker.deliver_due()
    assert "  - Thu, Nov 12, 12:00 - 2:00 PM\n" in mailer.sent[0].text
    book_first_open_slot(client, first)  # takes Thursday 12:00
    new_lead(client, ctx, **consumer(2))

    ctx.worker.deliver_due()

    intro = next(email for email in mailer.sent if email.to == "consumer2@example.com")
    assert "Thu, Nov 12, 12:00 - 2:00 PM" not in intro.text and "Thu, Nov 12, 12:00 - 2:00 PM" not in intro.html
    assert "  - Thu, Nov 12, 2:00 - 4:00 PM\n" in intro.text


# --- bookkeeping after a send ----------------------------------------------------------


def test_sent_message_keeps_an_audit_copy_of_what_went_out(client, ctx, mailer, clock):
    # Rule: we can always show exactly what was sent, when, and through which provider.
    lead = new_lead(client, ctx, **consumer(1))
    clock.advance(seconds=2)

    ctx.worker.deliver_due()

    row = message(ctx, lead["id"], "intro")
    email = mailer.sent[0]
    assert row["status"] == "sent"
    assert row["sent_at"] == iso(START + timedelta(seconds=2))
    assert row["subject"] == email.subject == INTRO_SUBJECT
    assert row["body_text"] == email.text
    assert row["provider"] == "memory"
    assert row["provider_message_id"] == "<memory-1@test>"
    assert (row["attempts"], row["last_error"], row["locked_at"]) == (1, None, None)


def test_first_contacted_at_is_set_by_the_first_email_and_never_moved(client, ctx, clock):
    # Rule: speed-to-lead is measured to the FIRST email; follow-ups must not overwrite it.
    lead = new_lead(client, ctx, **consumer(1))
    assert lead["first_contacted_at"] is None
    clock.advance(seconds=7)
    ctx.worker.deliver_due()
    first_contact = lead_row(ctx, lead["id"])["first_contacted_at"]
    assert first_contact == iso(START + timedelta(seconds=7))

    clock.advance(minutes=60)
    assert ctx.worker.deliver_due() == 1  # follow-up 1

    assert lead_row(ctx, lead["id"])["first_contacted_at"] == first_contact


def test_status_moves_from_new_to_contacted_and_is_never_downgraded(client, ctx, clock):
    # Rule: sending an email marks a new lead contacted, and leaves later stages alone.
    lead = new_lead(client, ctx, **consumer(1))
    assert lead["status"] == "new"
    ctx.worker.deliver_due()
    assert lead_row(ctx, lead["id"])["status"] == "contacted"

    book_first_open_slot(client, lead)
    assert lead_row(ctx, lead["id"])["status"] == "booked"
    assert ctx.worker.deliver_due() == 1  # the confirmation is an email too

    assert lead_row(ctx, lead["id"])["status"] == "booked"


def test_a_late_intro_does_not_pull_an_engaged_lead_back_to_contacted(client, ctx, mailer, clock):
    # Rule: status only moves forward, even when the intro is delivered after the link was opened.
    mailer.fail_with = RuntimeError("smtp down")
    mailer.fail_times = 1
    lead = new_lead(client, ctx, **consumer(1))
    ctx.worker.deliver_due()  # fails once; the intro is retried in 5s
    assert client.get(f"/book/{lead['booking_token']}").status_code == 200
    assert lead_row(ctx, lead["id"])["status"] == "engaged"

    clock.advance(seconds=5)
    assert ctx.worker.deliver_due() == 1

    row = lead_row(ctx, lead["id"])
    assert row["status"] == "engaged"
    assert row["first_contacted_at"] == iso(START + timedelta(seconds=5))


def test_email_sent_event_records_speed_to_lead_for_first_touch_messages_only(client, ctx, clock):
    # Rule: seconds_since_received is the time from the webhook to the first response.
    lead = new_lead(client, ctx, **consumer(1))
    clock.advance(seconds=7)
    ctx.worker.deliver_due()
    clock.advance(minutes=60)
    ctx.worker.deliver_due()  # follow-up 1

    intro_event, followup_event = lead_events(ctx, lead["id"], "email.sent")
    assert intro_event["template"] == "intro"
    assert intro_event["seconds_since_received"] == 7.0
    assert intro_event["to"] == "consumer1@example.com"
    assert intro_event["provider"] == "memory"
    assert intro_event["attempts"] == 1
    assert intro_event["message_id"] == message(ctx, lead["id"], "intro")["id"]
    assert followup_event["template"] == "followup"
    assert "seconds_since_received" not in followup_event


def test_repeat_request_also_counts_as_a_first_touch_for_speed_to_lead(client, ctx, clock):
    # Rule: a duplicate lead's acknowledgement is its first response and is timed like one.
    new_lead(client, ctx, Email="pat@example.com")
    ctx.worker.deliver_due()
    clock.advance(minutes=5)
    again = new_lead(client, ctx, Email="pat@example.com")
    clock.advance(seconds=3)
    ctx.worker.deliver_due()

    (event,) = lead_events(ctx, again["id"], "email.sent")
    assert event["template"] == "repeat_request"
    assert event["seconds_since_received"] == 3.0
    assert lead_row(ctx, again["id"])["status"] == "contacted"


def test_a_burst_of_25_leads_gets_25_intros_with_no_duplicates(client, ctx, mailer):
    # Rule: under a burst every lead is answered exactly once (parallel sends must not collide).
    leads = [new_lead(client, ctx, **consumer(n)) for n in range(25)]

    assert ctx.worker.deliver_due() == 25

    recipients = [email.to for email in mailer.sent]
    assert len(recipients) == 25
    assert sorted(recipients) == sorted(f"consumer{n}@example.com" for n in range(25))
    assert {email.subject for email in mailer.sent} == {INTRO_SUBJECT}
    for lead in leads:
        intro = message(ctx, lead["id"], "intro")
        assert (intro["status"], intro["attempts"]) == ("sent", 1)
        row = lead_row(ctx, lead["id"])
        assert row["status"] == "contacted"
        assert row["first_contacted_at"] is not None
        assert len(lead_events(ctx, lead["id"], "email.sent")) == 1
        # Each email carries its own lead's booking link.
        email = next(e for e in mailer.sent if e.to == lead["email_normalized"])
        assert f"/book/{lead['booking_token']}" in email.text
    assert ctx.worker.deliver_due() == 0


# --- outbox primitives -----------------------------------------------------------------


@pytest.fixture
def quiet_lead(client, ctx) -> dict:
    """A stored lead with no usable email, so it starts with an empty outbox."""
    lead = new_lead(client, ctx, Email="not-an-email")
    assert messages(ctx, lead["id"]) == []
    return lead


def enqueue(ctx, lead: dict, *, template: str, dedupe_key: str, send_after: datetime, sequence: int = 0):
    with ctx.db.transaction() as conn:
        return outbox.enqueue(
            conn,
            lead_id=lead["id"],
            tenant_id=lead["tenant_id"],
            template=template,
            to_address="pat@example.com",
            dedupe_key=dedupe_key,
            send_after=send_after,
            now=START,
            sequence=sequence,
        )


def test_enqueue_is_idempotent_on_the_dedupe_key(ctx, quiet_lead):
    # Rule: replaying a webhook can never queue a second copy of the same email.
    first = enqueue(ctx, quiet_lead, template="intro", dedupe_key="k1", send_after=START)
    second = enqueue(ctx, quiet_lead, template="intro", dedupe_key="k1", send_after=START + timedelta(hours=1))

    assert first is not None and first.startswith("msg_")
    assert second is None
    rows = messages(ctx, quiet_lead["id"])
    assert [(r["id"], r["status"], r["send_after"], r["attempts"]) for r in rows] == [(first, "pending", iso(START), 0)]
    queued = lead_events(ctx, quiet_lead["id"], "email.queued")
    assert queued == [{"message_id": first, "template": "intro", "send_after": iso(START)}]


def test_cancel_pending_without_a_filter_cancels_everything_not_yet_sent(ctx, quiet_lead, clock):
    # Rule: an unsubscribe stops every queued email for the lead, but cannot recall a sent one.
    enqueue(ctx, quiet_lead, template="intro", dedupe_key="intro", send_after=START)
    ctx.worker.deliver_due()
    enqueue(ctx, quiet_lead, template="followup", dedupe_key="f1", send_after=START + timedelta(hours=1), sequence=1)
    enqueue(ctx, quiet_lead, template="booking_confirmation", dedupe_key="c1", send_after=START)

    with ctx.db.transaction() as conn:
        cancelled = outbox.cancel_pending(conn, quiet_lead["id"], clock(), reason="unsubscribed")

    assert cancelled == 2
    statuses = {m["template"]: (m["status"], m["last_error"]) for m in messages(ctx, quiet_lead["id"])}
    assert statuses == {
        "intro": ("sent", None),
        "followup": ("cancelled", "unsubscribed"),
        "booking_confirmation": ("cancelled", "unsubscribed"),
    }
    assert lead_events(ctx, quiet_lead["id"], "email.cancelled") == [
        {"reason": "unsubscribed", "count": 2, "templates": ["booking_confirmation", "followup"]}
    ]


def test_cancel_pending_with_a_template_filter_leaves_other_templates_alone(ctx, quiet_lead, clock):
    # Rule: booking cancels the nudges but not the confirmation.
    enqueue(ctx, quiet_lead, template="followup", dedupe_key="f1", send_after=START + timedelta(hours=1), sequence=1)
    enqueue(ctx, quiet_lead, template="followup", dedupe_key="f2", send_after=START + timedelta(hours=24), sequence=2)
    enqueue(ctx, quiet_lead, template="booking_confirmation", dedupe_key="c1", send_after=START)

    with ctx.db.transaction() as conn:
        cancelled = outbox.cancel_pending(conn, quiet_lead["id"], clock(), reason="booked", templates=("followup",))

    assert cancelled == 2
    assert [(m["template"], m["status"]) for m in messages(ctx, quiet_lead["id"])] == [
        ("booking_confirmation", "pending"),
        ("followup", "cancelled"),
        ("followup", "cancelled"),
    ]
    assert lead_events(ctx, quiet_lead["id"], "email.cancelled") == [
        {"reason": "booked", "count": 2, "templates": ["followup"]}
    ]


def test_cancel_pending_with_nothing_to_cancel_records_nothing(ctx, quiet_lead, clock):
    # Rule: the timeline only shows cancellations that actually happened.
    with ctx.db.transaction() as conn:
        assert outbox.cancel_pending(conn, quiet_lead["id"], clock(), reason="booked") == 0
        assert outbox.cancel_pending(conn, quiet_lead["id"], clock(), reason="booked", templates=("followup",)) == 0

    assert lead_events(ctx, quiet_lead["id"], "email.cancelled") == []


@pytest.mark.parametrize(
    ("attempts", "seconds"),
    [(1, 5), (2, 30), (3, 120), (4, 600), (5, 1800), (6, 1800), (50, 1800), (0, 5), (-3, 5)],
)
def test_retry_delay_table(attempts, seconds):
    # Rule: quick first retry (speed to lead), then back off; out-of-range attempts are clamped.
    assert outbox.retry_delay(attempts) == timedelta(seconds=seconds)


def test_next_due_at_is_the_earliest_pending_send_time(ctx, quiet_lead):
    # Rule: only messages still waiting count; sent and cancelled ones do not.
    assert outbox.next_due_at(ctx.db) is None
    enqueue(ctx, quiet_lead, template="intro", dedupe_key="intro", send_after=START)
    enqueue(ctx, quiet_lead, template="followup", dedupe_key="f1", send_after=START + timedelta(hours=1), sequence=1)
    enqueue(ctx, quiet_lead, template="followup", dedupe_key="f2", send_after=START + timedelta(hours=24), sequence=2)
    assert outbox.next_due_at(ctx.db) == iso(START)

    ctx.worker.deliver_due()  # the intro is sent
    assert outbox.next_due_at(ctx.db) == iso(START + timedelta(hours=1))

    with ctx.db.transaction() as conn:
        outbox.cancel_pending(conn, quiet_lead["id"], START, reason="booked")
    assert outbox.next_due_at(ctx.db) is None


def test_claim_next_takes_each_message_once_and_counts_the_attempt(ctx, quiet_lead, clock):
    # Rule: a claimed message is locked ("sending") so no second worker pass can send it too.
    enqueue(ctx, quiet_lead, template="intro", dedupe_key="intro", send_after=START + timedelta(seconds=1))
    assert outbox.claim_next(ctx.db, clock()) is None  # not due for another second

    clock.advance(seconds=1)
    claimed = outbox.claim_next(ctx.db, clock())

    assert (claimed["status"], claimed["attempts"]) == ("sending", 1)
    stored = message(ctx, quiet_lead["id"], "intro")
    assert (stored["status"], stored["attempts"], stored["locked_at"]) == ("sending", 1, iso(clock()))
    assert outbox.claim_next(ctx.db, clock()) is None


def test_requeue_stuck_reports_how_many_messages_it_released(ctx, quiet_lead, clock):
    # Rule: only messages locked for longer than the grace period are released.
    enqueue(ctx, quiet_lead, template="intro", dedupe_key="a", send_after=START)
    outbox.claim_next(ctx.db, clock())
    clock.advance(seconds=100)
    enqueue(ctx, quiet_lead, template="booking_confirmation", dedupe_key="b", send_after=START)
    outbox.claim_next(ctx.db, clock())  # locked 100s after the first one

    clock.advance(seconds=30)  # first locked 130s ago, second 30s ago
    assert outbox.requeue_stuck(ctx.db, clock()) == 1

    statuses = {m["template"]: m["status"] for m in messages(ctx, quiet_lead["id"])}
    assert statuses == {"intro": "pending", "booking_confirmation": "sending"}


def test_requeue_stuck_at_start_up_releases_every_claimed_message_whatever_its_age(ctx, quiet_lead, clock):
    # Rule: when the worker starts nothing can be in flight, so every 'sending' row is orphaned.
    enqueue(ctx, quiet_lead, template="intro", dedupe_key="a", send_after=START)
    enqueue(ctx, quiet_lead, template="booking_confirmation", dedupe_key="b", send_after=START)
    outbox.claim_next(ctx.db, clock())
    outbox.claim_next(ctx.db, clock())  # both claimed this very second
    assert outbox.requeue_stuck(ctx.db, clock()) == 0  # the periodic pass leaves them alone

    assert outbox.requeue_stuck(ctx.db, clock(), all_sending=True) == 2

    rows = messages(ctx, quiet_lead["id"])
    assert [(m["status"], m["locked_at"], m["attempts"]) for m in rows] == [("pending", None, 1)] * 2


def test_operator_retry_sends_a_failed_intro(client, ctx, mailer, clock):
    # Rule: after the cause is fixed (say, the mail password) an operator can retry a message we gave up on.
    mailer.fail_with = PermanentEmailError("recipient refused: mailbox full")
    mailer.fail_times = 1
    lead = new_lead(client, ctx, **consumer(1))
    ctx.worker.deliver_due()
    failed = message(ctx, lead["id"], "intro")
    assert (failed["status"], failed["attempts"]) == ("failed", 1)
    clock.advance(hours=2)

    with ctx.db.transaction() as conn:
        assert outbox.requeue_failed(conn, failed["id"], clock()) is True

    queued = message(ctx, lead["id"], "intro")
    assert (queued["status"], queued["attempts"], queued["last_error"], queued["locked_at"]) == ("pending", 0, None, None)
    assert queued["send_after"] == iso(clock())
    assert queued["id"] == failed["id"]  # the same message, not a copy
    # The timeline shows that it was queued again, and by whom.
    assert lead_events(ctx, lead["id"], "email.queued")[-1] == {
        "message_id": failed["id"],
        "template": "intro",
        "send_after": iso(clock()),
        "requeued_by_operator": True,
    }

    assert ctx.worker.deliver_due() == 1

    sent = message(ctx, lead["id"], "intro")
    assert (sent["status"], sent["attempts"], sent["sent_at"]) == ("sent", 1, iso(clock()))
    assert [email.subject for email in mailer.sent] == [INTRO_SUBJECT]
    assert lead_row(ctx, lead["id"])["first_contacted_at"] == iso(clock())
    # The nudges that were called off with it stay off: they would arrive on top of a late intro.
    assert [m["status"] for m in messages(ctx, lead["id"], "followup")] == ["cancelled", "cancelled"]


def test_operator_retry_leaves_a_message_that_has_not_failed_alone(client, ctx, mailer, clock):
    # Rule: only a failed message can be retried; pending, sent and cancelled ones are not touched.
    lead = new_lead(client, ctx, **consumer(1))
    ctx.worker.deliver_due()  # intro sent, follow-ups pending
    with ctx.db.transaction() as conn:
        outbox.cancel_pending(conn, lead["id"], clock(), reason="booked", templates=("followup",))
    sql(ctx, "UPDATE outbox_messages SET status = 'pending' WHERE lead_id = ? AND sequence = 1", (lead["id"],))
    before = messages(ctx, lead["id"])
    assert [m["status"] for m in before] == ["sent", "pending", "cancelled"]
    queued_before = lead_events(ctx, lead["id"], "email.queued")
    clock.advance(minutes=5)

    with ctx.db.transaction() as conn:
        results = [outbox.requeue_failed(conn, m["id"], clock()) for m in before]
        unknown = outbox.requeue_failed(conn, "msg_does_not_exist", clock())

    assert results == [False, False, False]
    assert unknown is False
    assert messages(ctx, lead["id"]) == before
    assert lead_events(ctx, lead["id"], "email.queued") == queued_before
    assert ctx.worker.deliver_due() == 0
    assert len(mailer.sent) == 1


# --- the real background thread --------------------------------------------------------


def signal_on_send(mailer, monkeypatch) -> threading.Event:
    """An event that is set as soon as the mailer has accepted an email (no sleeping needed)."""
    sent = threading.Event()
    real_send = mailer.send

    def send_and_signal(email):
        result = real_send(email)
        sent.set()
        return result

    monkeypatch.setattr(mailer, "send", send_and_signal)
    return sent


def test_background_thread_sends_a_new_leads_intro_and_stops_cleanly(client, ctx, mailer, monkeypatch):
    # Rule: storing a lead wakes the worker thread, which sends the intro without being polled.
    intro_sent = signal_on_send(mailer, monkeypatch)

    def worker_threads() -> int:
        return sum(1 for thread in threading.enumerate() if thread.name == "background-worker")

    before = worker_threads()
    assert ctx.worker.is_running is False
    ctx.worker.start()
    try:
        assert ctx.worker.is_running is True
        ctx.worker.start()  # a second start must not spawn a second thread
        assert worker_threads() == before + 1

        outcome = ctx.ingest.receive(
            raw_body=json.dumps(ads_payload(Email="pat@example.com")).encode(),
            headers={"content-type": "application/json"},
        )
        assert outcome.body["result"] == "processed"
        assert intro_sent.wait(timeout=30), "the worker thread did not send the intro"
    finally:
        ctx.worker.stop()  # waits for the pass in progress to finish

    assert ctx.worker.is_running is False
    assert worker_threads() == before
    assert [email.to for email in mailer.sent] == ["pat@example.com"]
    intro = message(ctx, outcome.body["leadId"], "intro")
    assert intro["status"] == "sent"
    assert lead_row(ctx, outcome.body["leadId"])["status"] == "contacted"


def test_email_queued_while_the_worker_was_stopped_goes_out_when_it_starts_again(client, ctx, mailer, monkeypatch):
    # Rule: the queue lives in the database, so stopping and restarting the worker loses nothing.
    intro_sent = signal_on_send(mailer, monkeypatch)
    ctx.worker.start()
    ctx.worker.stop()
    assert ctx.worker.is_running is False
    lead = new_lead(client, ctx, **consumer(1))  # arrives while the worker is down
    assert mailer.sent == []

    ctx.worker.start()
    try:
        assert ctx.worker.is_running is True
        assert intro_sent.wait(timeout=30), "the restarted worker did not send the queued intro"
    finally:
        ctx.worker.stop()

    assert ctx.worker.is_running is False
    assert message(ctx, lead["id"], "intro")["status"] == "sent"


def test_message_cut_off_mid_send_goes_out_as_soon_as_the_worker_starts(client, ctx, mailer, monkeypatch):
    # Rule: on start-up nothing is in flight, so a message left 'sending' by the last run is
    # sent at once instead of after the two-minute grace period (it may be a first response).
    lead = new_lead(client, ctx, **consumer(1))
    assert outbox.claim_next(ctx.db, ctx.now())["status"] == "sending"  # ...and the process died here
    intro_sent = signal_on_send(mailer, monkeypatch)

    ctx.worker.start()
    try:
        assert intro_sent.wait(timeout=30), "the restarted worker did not send the interrupted intro"
    finally:
        ctx.worker.stop()

    # The fake clock never moved, so the grace period cannot be what released it.
    intro = message(ctx, lead["id"], "intro")
    assert (intro["status"], intro["attempts"]) == ("sent", 2)
    assert [email.to for email in mailer.sent] == ["consumer1@example.com"]


def test_worker_thread_keeps_delivering_after_a_maintenance_pass_blows_up(client, ctx, mailer, monkeypatch):
    # Rule: a broken background job (monitoring, retries) must never stop first responses.
    maintenance_failed = threading.Event()

    def broken_maintenance():
        maintenance_failed.set()
        raise RuntimeError("monitoring exploded")

    monkeypatch.setattr(ctx.worker, "run_maintenance", broken_maintenance)
    intro_sent = signal_on_send(mailer, monkeypatch)

    ctx.worker.start()
    try:
        assert maintenance_failed.wait(timeout=30), "the worker never ran its maintenance pass"
        lead = new_lead(client, ctx, **consumer(1))  # arrives after the loop has already hit the error
        assert intro_sent.wait(timeout=30), "the worker stopped delivering after the error"
        assert ctx.worker.is_running is True
    finally:
        ctx.worker.stop()

    assert message(ctx, lead["id"], "intro")["status"] == "sent"


def test_worker_can_be_stopped_before_it_was_ever_started(ctx):
    # Rule: shutdown is safe whether or not the worker ran (the app always calls stop()).
    ctx.worker.stop()
    assert ctx.worker.is_running is False


# --- quiet hours -----------------------------------------------------------------------

INDIANAPOLIS = ZoneInfo("America/Indiana/Indianapolis")
DENVER = ZoneInfo("America/Denver")


def local(zone: ZoneInfo, year: int, month: int, day: int, hour: int, minute: int = 0, **kwargs) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=zone, **kwargs)


@pytest.mark.parametrize(
    ("when", "expected"),
    [
        (local(INDIANAPOLIS, 2026, 11, 12, 7, 59), local(INDIANAPOLIS, 2026, 11, 12, 8, 0)),
        (local(INDIANAPOLIS, 2026, 11, 12, 0, 0), local(INDIANAPOLIS, 2026, 11, 12, 8, 0)),
        (local(INDIANAPOLIS, 2026, 11, 12, 8, 0), None),  # None = unchanged
        (local(INDIANAPOLIS, 2026, 11, 12, 13, 15), None),
        (local(INDIANAPOLIS, 2026, 11, 12, 19, 59), None),
        (local(INDIANAPOLIS, 2026, 11, 12, 19, 59, second=59, microsecond=999999), None),
        (local(INDIANAPOLIS, 2026, 11, 12, 20, 0), local(INDIANAPOLIS, 2026, 11, 13, 8, 0)),
        (local(INDIANAPOLIS, 2026, 11, 12, 23, 30), local(INDIANAPOLIS, 2026, 11, 13, 8, 0)),
        (local(INDIANAPOLIS, 2026, 12, 31, 22, 0), local(INDIANAPOLIS, 2027, 1, 1, 8, 0)),
    ],
)
def test_defer_past_quiet_hours_boundaries_in_the_tenants_timezone(when, expected):
    # Rule: daytime is 08:00-19:59 local; anything else waits for the next 8 AM local.
    tenant = {"timezone": "America/Indiana/Indianapolis"}
    as_utc = when.astimezone(UTC)  # the app always passes UTC instants

    result = defer_past_quiet_hours(tenant, as_utc)

    assert result == (expected or when)
    assert result.utcoffset() == timedelta(0)  # always handed back in UTC


@pytest.mark.parametrize(
    ("when", "expected_utc"),
    [
        # Spring forward, Sunday 2026-03-08 02:00 MST -> 03:00 MDT.
        (local(DENVER, 2026, 3, 7, 21, 0), datetime(2026, 3, 8, 14, 0, tzinfo=UTC)),  # 8 AM MDT
        (local(DENVER, 2026, 3, 8, 1, 30), datetime(2026, 3, 8, 14, 0, tzinfo=UTC)),
        (local(DENVER, 2026, 3, 8, 3, 30), datetime(2026, 3, 8, 14, 0, tzinfo=UTC)),
        (local(DENVER, 2026, 3, 6, 21, 0), datetime(2026, 3, 7, 15, 0, tzinfo=UTC)),  # 8 AM MST, day before
        # Fall back, Sunday 2026-11-01 02:00 MDT -> 01:00 MST.
        (local(DENVER, 2026, 10, 31, 22, 0), datetime(2026, 11, 1, 15, 0, tzinfo=UTC)),  # 8 AM MST
        (local(DENVER, 2026, 11, 1, 1, 30, fold=0), datetime(2026, 11, 1, 15, 0, tzinfo=UTC)),
        (local(DENVER, 2026, 11, 1, 1, 30, fold=1), datetime(2026, 11, 1, 15, 0, tzinfo=UTC)),
        (local(DENVER, 2026, 10, 30, 22, 0), datetime(2026, 10, 31, 14, 0, tzinfo=UTC)),  # 8 AM MDT, day before
    ],
)
def test_defer_past_quiet_hours_lands_on_8am_wall_clock_across_dst_changes(when, expected_utc):
    # Rule: "8 AM" means 8 AM on the tenant's wall clock, also on the day the clocks change.
    result = defer_past_quiet_hours({"timezone": "America/Denver"}, when.astimezone(UTC))

    assert result == expected_utc
    assert (result.astimezone(DENVER).hour, result.astimezone(DENVER).minute) == (8, 0)


def test_defer_past_quiet_hours_uses_each_tenants_own_timezone():
    # Rule: the same instant can be daytime for one tenant and night for another.
    instant = datetime(2026, 11, 13, 2, 30, tzinfo=UTC)  # 21:30 Indianapolis, 19:30 Denver

    assert defer_past_quiet_hours({"timezone": "America/Denver"}, instant) == instant
    assert defer_past_quiet_hours({"timezone": "America/Indiana/Indianapolis"}, instant) == datetime(
        2026, 11, 13, 13, 0, tzinfo=UTC
    )
