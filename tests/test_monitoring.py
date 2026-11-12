"""Health checks (app/monitoring.py), alerts and their notification channels (app/alerts.py).

Each check is driven to "ok" and to "failing" with data created through the real pipeline:
leads posted to the webhook, emails delivered (or not) by the worker, time moved with the
fake clock. Failures are injected only at true edges (the mail provider, Slack's HTTP call)
or to simulate a crash between "stored" and "processed".
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest

from app import monitoring, outbox, queries
from app.alerts import (
    list_alerts,
    raise_alert,
    resolve_alert,
    resolve_alert_by_id,
    resolve_quiet_event_alerts,
)
from app.mailer.providers import PermanentEmailError, build_mime
from app.monitoring import expected_leads_between
from app.util import iso
from tests.conftest import (
    ADMIN,
    BASE_URL,
    BLUELINE_ACCOUNT,
    CANARY_ACCOUNT,
    SPARKLE_ACCOUNT,
    START,
    ads_payload,
    db_one,
    db_rows,
    post_lead,
)

SLACK_URL = "https://hooks.slack.test/services/T000/B000/XXXX"
UNMAPPED_ACCOUNT = "000111"
INDIANAPOLIS = ZoneInfo("America/Indiana/Indianapolis")
SPARKLE_SILENCE = "lead_silence:sparkle-home-cleaning"


def send(client, payload) -> dict:
    response = post_lead(client, payload)
    assert response.status_code == 200, response.text
    return response.json()


def consumer(n: int) -> dict:
    return {"Email": f"homeowner{n}@example.com", "PhoneNumber": f"317555{n:04d}"}


def checks(ctx) -> dict[str, monitoring.Check]:
    """Every health check evaluated at the fake clock's "now", by name (read-only)."""
    with ctx.db.read() as conn:
        return {check.name: check for check in monitoring.evaluate(conn, ctx.settings, ctx.now())}


def quarantine_one(client, n: int = 1, account: str = UNMAPPED_ACCOUNT) -> dict:
    """Post a lead for an Angi account no tenant is mapped to."""
    body = send(client, ads_payload(**consumer(n), ALAccountId=account))
    assert body["result"] == "quarantined"
    return body


def alerts(ctx, key: str | None = None) -> list[dict]:
    if key is None:
        return db_rows(ctx, "SELECT * FROM alerts ORDER BY id")
    return db_rows(ctx, "SELECT * FROM alerts WHERE dedupe_key = ? ORDER BY id", (key,))


def raise_one(ctx, key: str = "test:problem", severity: str = "warning", title: str = "Something is wrong", **kwargs):
    with ctx.db.transaction() as conn:
        return raise_alert(
            conn, kind="test", key=key, severity=severity, title=title, detail=kwargs.pop("detail", {"n": 1}),
            now=ctx.now(), **kwargs,
        )


class FakeSlack:
    """Stands in for httpx.post: records every call and can be told to fail."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.raise_on_call: Exception | None = None
        self.status_code = 200

    def __call__(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if self.raise_on_call is not None:
            raise self.raise_on_call
        return httpx.Response(self.status_code, request=httpx.Request("POST", url))

    @property
    def texts(self) -> list[str]:
        return [call["json"]["text"] for call in self.calls]


@pytest.fixture
def slack(monkeypatch, settings) -> FakeSlack:
    """Slack configured, with the outbound HTTP call captured instead of sent."""
    fake = FakeSlack()
    monkeypatch.setattr(httpx, "post", fake)
    settings.alert_slack_webhook_url = SLACK_URL
    return fake


# --- a healthy system --------------------------------------------------------------------


def test_fresh_system_passes_every_check(client, ctx):
    # Nothing received yet is not a problem: every check is ok and the overall status is ok.
    result = checks(ctx)
    assert set(result) == {
        "ingest_errors",
        "unprocessed",
        "quarantine",
        "email_backlog",
        "email_failures",
        "lead_silence:sparkle-home-cleaning",
        "lead_silence:summit-heating-air",
        "lead_silence:blueline-plumbing",
        "canary",
    }
    assert {check.status for check in result.values()} == {"ok"}
    assert monitoring.overall_status(list(result.values())) == "ok"


def test_processed_and_emailed_lead_keeps_every_check_ok(client, ctx, clock):
    # The happy path leaves nothing for monitoring to complain about.
    send(client, ads_payload())
    clock.advance(seconds=1)
    ctx.worker.deliver_due()
    clock.advance(minutes=10)
    assert {check.status for check in checks(ctx).values()} == {"ok"}


def test_overall_status_is_the_worst_check():
    # One critical outweighs any number of warnings; warnings outweigh ok.
    ok = monitoring.Check("a", "ok", "fine")
    warning = monitoring.Check("b", "warning", "hmm")
    critical = monitoring.Check("c", "critical", "bad")
    assert monitoring.overall_status([]) == "ok"
    assert monitoring.overall_status([ok, ok]) == "ok"
    assert monitoring.overall_status([ok, warning]) == "warning"
    assert monitoring.overall_status([warning, critical, ok]) == "critical"


def test_check_as_dict_has_the_four_documented_fields():
    # The API and dashboard read checks as plain dicts.
    check = monitoring.Check("quarantine", "warning", "1 waiting", {"count": 1})
    assert check.as_dict() == {"name": "quarantine", "status": "warning", "summary": "1 waiting", "detail": {"count": 1}}


# --- ingest_errors -----------------------------------------------------------------------


def test_ingest_errors_ok_below_the_minimum_event_count_even_if_all_failed(client, ctx):
    # Two bad events out of two is 100%, but too few to call it a format change.
    quarantine_one(client, 1)
    quarantine_one(client, 2)

    check = checks(ctx)["ingest_errors"]
    assert check.status == "ok"
    assert check.detail == {"window_minutes": 60, "total": 2, "not_processed": 2, "by_status": {"quarantined": 2}}


def test_ingest_errors_critical_at_the_minimum_event_count(client, ctx):
    # Three events, none turned into a lead: that is what an upstream format change looks like.
    for n in (1, 2, 3):
        quarantine_one(client, n)

    check = checks(ctx)["ingest_errors"]
    assert check.status == "critical"
    assert check.detail == {"window_minutes": 60, "total": 3, "not_processed": 3, "by_status": {"quarantined": 3}}
    assert "3 of 3" in check.summary
    assert "100%" in check.summary


def test_ingest_errors_threshold_is_a_share_of_recent_events(client, ctx):
    # Default threshold 20%: 1 bad of 6 (17%) is ok, 1 bad of 5 (20%) is critical (inclusive).
    quarantine_one(client, 1)
    for n in (2, 3, 4, 5):
        send(client, ads_payload(**consumer(n)))
    at_threshold = checks(ctx)["ingest_errors"]
    assert at_threshold.status == "critical"
    assert at_threshold.detail["total"] == 5
    assert at_threshold.detail["not_processed"] == 1
    assert at_threshold.detail["by_status"] == {"processed": 4, "quarantined": 1}

    send(client, ads_payload(**consumer(6)))
    assert checks(ctx)["ingest_errors"].status == "ok"


def test_ingest_errors_counts_rejected_and_failed_events_too(client, ctx, monkeypatch):
    # "Could not be turned into a lead" covers junk bodies and our own processing errors as well.
    assert post_lead(client, "this is not json").status_code == 400  # stored as rejected
    quarantine_one(client, 1)

    def explode(*args, **kwargs):
        raise RuntimeError("injected processing failure")

    monkeypatch.setattr("app.ingest.find_duplicate", explode)
    accepted = send(client, ads_payload(**consumer(2)))
    assert accepted["result"] == "accepted"  # stored, processing failed on our side

    check = checks(ctx)["ingest_errors"]
    assert check.status == "critical"
    assert check.detail["by_status"] == {"rejected": 1, "quarantined": 1, "failed": 1}
    assert check.detail["not_processed"] == 3


def test_ingest_errors_does_not_count_redeliveries_as_errors(client, ctx, clock):
    # Angi retrying a lead we already have is healthy traffic.
    payload = ads_payload()
    send(client, payload)
    for _ in range(5):
        clock.advance(minutes=1)
        assert send(client, payload)["result"] == "duplicate_delivery"

    check = checks(ctx)["ingest_errors"]
    assert check.status == "ok"
    assert check.detail["not_processed"] == 0
    assert check.detail["by_status"] == {"processed": 1, "duplicate_delivery": 5}


def test_ingest_errors_ignores_events_older_than_the_window(client, ctx, clock):
    # A burst of failures an hour ago is history, not a current incident.
    for n in (1, 2, 3):
        quarantine_one(client, n)
    clock.advance(minutes=59)
    assert checks(ctx)["ingest_errors"].status == "critical"

    clock.advance(minutes=2)
    check = checks(ctx)["ingest_errors"]
    assert check.status == "ok"
    assert check.detail == {"window_minutes": 60, "total": 0, "not_processed": 0, "by_status": {}}


def test_ingest_errors_thresholds_come_from_settings(client, ctx, settings):
    # Minimum events, error share and window are all configuration.
    quarantine_one(client, 1)
    send(client, ads_payload(**consumer(2)))
    assert checks(ctx)["ingest_errors"].status == "ok"  # 2 events < 3

    settings.error_rate_min_events = 2
    assert checks(ctx)["ingest_errors"].status == "critical"  # 50% >= 20%
    settings.error_rate_threshold = 0.75
    assert checks(ctx)["ingest_errors"].status == "ok"  # 50% < 75%
    settings.error_rate_window_minutes = 15
    assert checks(ctx)["ingest_errors"].detail["window_minutes"] == 15


# --- unprocessed -------------------------------------------------------------------------


def test_unprocessed_event_stuck_in_failed_is_critical_after_the_grace_period(client, ctx, clock, monkeypatch):
    # A stored event that we failed to process is a lead nobody has answered.
    def explode(*args, **kwargs):
        raise RuntimeError("injected processing failure")

    monkeypatch.setattr("app.ingest.find_duplicate", explode)
    body = send(client, ads_payload())
    assert body["result"] == "accepted"
    assert db_one(ctx, "SELECT status FROM webhook_events WHERE id = ?", (body["eventId"],)) == {"status": "failed"}

    clock.advance(seconds=monitoring.UNPROCESSED_GRACE_SECONDS)
    assert checks(ctx)["unprocessed"].status == "ok"  # still inside the grace period

    clock.advance(seconds=1)
    check = checks(ctx)["unprocessed"]
    assert check.status == "critical"
    assert check.detail == {"count": 1, "oldest": iso(START)}
    assert "1 stored webhook event(s) have not been processed" in check.summary


def test_unprocessed_is_ok_again_once_the_event_is_processed(client, ctx, clock, monkeypatch):
    # The worker's maintenance pass retries the event; when it succeeds the check clears.
    def explode(*args, **kwargs):
        raise RuntimeError("injected processing failure")

    monkeypatch.setattr("app.ingest.find_duplicate", explode)
    body = send(client, ads_payload())
    clock.advance(minutes=5)
    assert checks(ctx)["unprocessed"].status == "critical"

    monkeypatch.undo()  # the "bug" is fixed
    ctx.worker.run_maintenance()

    event = db_one(ctx, "SELECT status, lead_id, attempts FROM webhook_events WHERE id = ?", (body["eventId"],))
    assert event["status"] == "processed"
    assert event["lead_id"] is not None
    assert event["attempts"] == 2
    assert checks(ctx)["unprocessed"].status == "ok"
    assert db_one(ctx, "SELECT COUNT(*) AS n FROM leads")["n"] == 1


def test_unprocessed_event_stuck_in_received_is_critical(client, ctx, clock, monkeypatch):
    # A crash between "stored" and "processed" leaves the event in 'received'; that must be seen.
    def crash(event_id):
        raise RuntimeError("process died right after storing the request")

    monkeypatch.setattr(ctx.ingest, "process_event", crash)
    with pytest.raises(RuntimeError):
        ctx.ingest.receive(raw_body=json.dumps(ads_payload()).encode(), headers={"content-type": "application/json"})
    assert db_rows(ctx, "SELECT status, attempts FROM webhook_events") == [{"status": "received", "attempts": 0}]

    assert checks(ctx)["unprocessed"].status == "ok"  # just arrived
    clock.advance(seconds=monitoring.UNPROCESSED_GRACE_SECONDS + 1)
    check = checks(ctx)["unprocessed"]
    assert check.status == "critical"
    assert check.detail["count"] == 1

    monkeypatch.undo()  # the process is back
    ctx.worker.run_maintenance()
    assert db_rows(ctx, "SELECT status FROM webhook_events") == [{"status": "processed"}]
    assert checks(ctx)["unprocessed"].status == "ok"
    assert db_one(ctx, "SELECT COUNT(*) AS n FROM leads")["n"] == 1


def test_unprocessed_reports_the_oldest_stuck_event(client, ctx, clock, monkeypatch):
    # With several stuck events, "oldest" says how long the first lead has been waiting.
    def explode(*args, **kwargs):
        raise RuntimeError("injected processing failure")

    monkeypatch.setattr("app.ingest.find_duplicate", explode)
    send(client, ads_payload(**consumer(1)))
    clock.advance(minutes=3)
    send(client, ads_payload(**consumer(2)))
    clock.advance(minutes=3)

    check = checks(ctx)["unprocessed"]
    assert check.status == "critical"
    assert check.detail == {"count": 2, "oldest": iso(START)}


def test_unprocessed_stays_critical_when_retries_are_exhausted(client, ctx, clock, monkeypatch):
    # After the automatic retries give up, the event must keep alerting until a human replays it.
    def explode(*args, **kwargs):
        raise RuntimeError("injected processing failure")

    monkeypatch.setattr("app.ingest.find_duplicate", explode)
    body = send(client, ads_payload())
    for _ in range(8):  # more maintenance passes than the retry budget
        clock.advance(minutes=1)
        ctx.worker.run_maintenance()
    assert db_one(ctx, "SELECT attempts FROM webhook_events WHERE id = ?", (body["eventId"],)) == {"attempts": 5}
    assert checks(ctx)["unprocessed"].status == "critical"

    monkeypatch.undo()
    clock.advance(minutes=1)
    ctx.worker.run_maintenance()  # no longer retried automatically
    assert checks(ctx)["unprocessed"].status == "critical"
    replay = client.post(f"/admin/api/events/{body['eventId']}/replay", headers=ADMIN)
    assert replay.json()["result"] == "processed"
    assert checks(ctx)["unprocessed"].status == "ok"


# --- quarantine --------------------------------------------------------------------------


def test_quarantine_ok_when_empty(client, ctx):
    # Nothing waiting on a human.
    send(client, ads_payload())
    check = checks(ctx)["quarantine"]
    assert check.status == "ok"
    assert check.summary == "nothing in quarantine"


def test_quarantine_warning_with_a_breakdown_by_reason(client, ctx):
    # Leads waiting on a human are listed by why they are waiting.
    quarantine_one(client, 1)
    quarantine_one(client, 2)
    unknown = send(client, {"foo": "bar", "baz": 1})  # no known format, no contact details
    assert (unknown["result"], unknown["reason"]) == ("quarantined", "unrecognised_payload")

    check = checks(ctx)["quarantine"]
    assert check.status == "warning"
    assert check.detail == {"count": 3, "by_reason": {"unmapped_account": 2, "unrecognised_payload": 1}}
    assert check.summary == (
        "3 lead(s) are quarantined and waiting for action (2 unmapped_account, 1 unrecognised_payload)"
    )


def test_quarantine_reports_a_deactivated_tenant_as_its_own_reason(client, ctx):
    # A lead for a tenant that was switched off is held, and the reason says so.
    with ctx.db.transaction() as conn:  # there is no API to deactivate a tenant; flip the flag directly
        conn.execute("UPDATE tenants SET is_active = 0 WHERE id = 'tnt_blueline'")
    body = send(client, ads_payload(ALAccountId=BLUELINE_ACCOUNT))
    assert (body["result"], body["reason"]) == ("quarantined", "tenant_inactive")

    assert checks(ctx)["quarantine"].detail == {"count": 1, "by_reason": {"tenant_inactive": 1}}


def test_quarantine_clears_when_the_missing_mapping_is_added(client, ctx):
    # Adding the mapping replays the held leads; the check passes again by itself.
    quarantine_one(client, 1)
    quarantine_one(client, 2)
    assert checks(ctx)["quarantine"].status == "warning"

    mapped = client.post(
        "/admin/api/tenants/tnt_sparkle/lead-sources", headers=ADMIN, json={"external_account_id": UNMAPPED_ACCOUNT}
    )
    assert mapped.status_code == 201
    assert len(mapped.json()["replayed"]) == 2

    assert checks(ctx)["quarantine"].status == "ok"
    assert db_one(ctx, "SELECT COUNT(*) AS n FROM leads")["n"] == 2


def test_quarantine_clears_when_the_operator_discards_what_can_never_become_a_lead(client, ctx, clock):
    # A junk body stays quarantined however often it is replayed. Discarding it is the way
    # out: it no longer waits for anyone, so the check (and its alert) can go green.
    junk = send(client, {"hello": "world"})
    assert junk["result"] == "quarantined"
    monitoring.run_checks(ctx.db, ctx.settings, ctx.now())
    assert [a["status"] for a in alerts(ctx, "check:quarantine")] == ["open"]

    with ctx.db.transaction() as conn:
        assert queries.discard_event(conn, junk["eventId"], ctx.now()) is True
    clock.advance(minutes=1)
    monitoring.run_checks(ctx.db, ctx.settings, ctx.now())

    check = checks(ctx)["quarantine"]
    assert (check.status, check.summary) == ("ok", "nothing in quarantine")
    assert [a["status"] for a in alerts(ctx, "check:quarantine")] == ["resolved"]
    assert db_one(ctx, "SELECT status FROM webhook_events WHERE id = ?", (junk["eventId"],)) == {"status": "discarded"}


# --- email_backlog -----------------------------------------------------------------------


def test_email_backlog_critical_when_a_first_touch_is_unsent_past_the_sla(client, ctx, clock):
    # The intro must leave within the speed-to-lead SLA (120 s); if the worker is down, alert.
    send(client, ads_payload())
    clock.advance(seconds=120)
    assert checks(ctx)["email_backlog"].status == "ok"  # exactly at the SLA is not yet late

    clock.advance(seconds=1)
    check = checks(ctx)["email_backlog"]
    assert check.status == "critical"
    assert check.detail == {"count": 1, "oldest_due": iso(START), "sla_seconds": 120.0}  # follow-ups not counted
    assert "1 email(s) have been waiting longer than the 120s speed-to-lead SLA" in check.summary


def test_email_backlog_ok_once_the_email_is_sent(client, ctx, mailer, clock):
    # Late is still better than never: once delivered, the check clears.
    send(client, ads_payload())
    clock.advance(minutes=10)
    assert checks(ctx)["email_backlog"].status == "critical"

    ctx.worker.deliver_due()
    assert len(mailer.sent) == 1
    assert checks(ctx)["email_backlog"].status == "ok"


def test_email_backlog_counts_a_first_touch_waiting_between_retries(client, ctx, mailer, clock):
    # While the provider is failing the message is "pending" with a FUTURE send_after;
    # the consumer has still been waiting since the lead arrived.
    send(client, ads_payload())
    mailer.fail_with = RuntimeError("smtp is down")
    mailer.fail_times = -1
    for wait in (1, 9, 35):  # attempts at +1 s, +10 s, +45 s; backoff 5 s, 30 s, then 120 s
        clock.advance(seconds=wait)
        ctx.worker.deliver_due()
    clock.advance(seconds=80)  # +125 s: past the SLA, next retry not due until +165 s

    message = db_one(ctx, "SELECT status, attempts, send_after FROM outbox_messages WHERE template = 'intro'")
    assert message["status"] == "pending"
    assert message["attempts"] == 3
    assert message["send_after"] > iso(clock())  # not due yet
    check = checks(ctx)["email_backlog"]
    assert check.status == "critical"
    assert check.detail["count"] == 1
    assert check.detail["oldest_due"] == iso(START)

    mailer.fail_with = None  # the provider recovers
    clock.advance(seconds=60)
    ctx.worker.deliver_due()
    assert len(mailer.sent) == 1
    assert checks(ctx)["email_backlog"].status == "ok"


def test_email_backlog_ignores_a_follow_up_that_is_not_due_yet(client, ctx, mailer, clock):
    # A nudge scheduled for later is not "late" just because it was queued long ago.
    send(client, ads_payload())
    clock.advance(seconds=1)
    ctx.worker.deliver_due()
    clock.advance(minutes=50)  # follow-up 1 is due at +60 min, follow-up 2 at +24 h

    pending = db_rows(ctx, "SELECT template FROM outbox_messages WHERE status = 'pending'")
    assert pending == [{"template": "followup"}, {"template": "followup"}]
    assert checks(ctx)["email_backlog"].status == "ok"


def test_email_backlog_counts_a_follow_up_only_from_when_it_fell_due(client, ctx, clock):
    # An overdue nudge does count, measured from its due time rather than from when it was queued.
    send(client, ads_payload())
    clock.advance(seconds=1)
    ctx.worker.deliver_due()
    clock.advance(minutes=60, seconds=119)  # follow-up 1 has been due for 120 s
    assert checks(ctx)["email_backlog"].status == "ok"

    clock.advance(seconds=2)
    check = checks(ctx)["email_backlog"]
    assert check.status == "critical"
    assert check.detail["count"] == 1
    assert check.detail["oldest_due"] == iso(START + timedelta(minutes=60))


def test_email_backlog_counts_a_message_stuck_in_sending(client, ctx, mailer, clock):
    # A worker that died mid-send leaves the row in 'sending'; the consumer is still waiting.
    send(client, ads_payload())
    claimed = outbox.claim_next(ctx.db, ctx.now())  # the worker takes the message... and crashes
    assert claimed["template"] == "intro"
    clock.advance(seconds=121)
    assert checks(ctx)["email_backlog"].status == "critical"

    ctx.worker.run_maintenance()  # puts the stuck message back in the queue
    ctx.worker.deliver_due()
    assert len(mailer.sent) == 1
    assert checks(ctx)["email_backlog"].status == "ok"


def test_email_backlog_sla_comes_from_settings(client, ctx, clock, settings):
    # email_sla_seconds is configuration.
    settings.email_sla_seconds = 30
    send(client, ads_payload())
    clock.advance(seconds=31)
    check = checks(ctx)["email_backlog"]
    assert check.status == "critical"
    assert check.detail["sla_seconds"] == 30


def test_email_backlog_ignores_a_lead_with_no_email_to_send(client, ctx, clock):
    # No address means nothing was queued, so nothing can be late.
    send(client, ads_payload(Email=""))
    clock.advance(minutes=30)
    assert checks(ctx)["email_backlog"].status == "ok"


# --- email_failures ----------------------------------------------------------------------


def test_email_failures_ok_when_nothing_failed(client, ctx, clock):
    # Delivered mail is not a failure.
    send(client, ads_payload())
    clock.advance(seconds=1)
    ctx.worker.deliver_due()
    assert checks(ctx)["email_failures"].status == "ok"


def test_email_failures_warning_when_a_message_was_given_up_on(client, ctx, mailer, clock):
    # A permanently rejected address means that lead got no response from us.
    send(client, ads_payload(**consumer(1)))
    send(client, ads_payload(**consumer(2)))
    mailer.fail_with = PermanentEmailError("550 mailbox does not exist")
    mailer.fail_times = -1
    clock.advance(seconds=1)
    ctx.worker.deliver_due()

    assert db_one(ctx, "SELECT COUNT(*) AS n FROM outbox_messages WHERE status = 'failed'")["n"] == 2
    check = checks(ctx)["email_failures"]
    assert check.status == "warning"  # a bad address is the lead's problem, not an outage of ours
    assert check.detail == {"count": 2, "first_responses_abandoned": 0}
    assert check.summary == "2 email(s) could not be delivered in the last 24h"
    # Failed messages are not "waiting", so they do not also show up as a backlog.
    clock.advance(minutes=10)
    assert checks(ctx)["email_backlog"].status == "ok"


def fail_until_given_up(ctx, clock, template: str = "intro") -> None:
    """Let the worker keep trying, an hour at a time, until it gives up on the message.
    How many attempts that takes is the worker's business, not this check's."""
    for _ in range(72):
        clock.advance(hours=1)
        ctx.worker.deliver_due()
        message = db_one(ctx, "SELECT status FROM outbox_messages WHERE template = ?", (template,))
        if message["status"] == "failed":
            return
    raise AssertionError(f"the worker never gave up on the {template} message")


def test_email_failures_critical_when_a_first_response_is_abandoned_because_sending_is_broken(
    client, ctx, mailer, clock, settings
):
    # The provider stayed down until we stopped trying: a consumer who asked for help never
    # heard from us. That is as serious as a late first response, and it is our fault.
    settings.followup_delays_minutes = ""  # one message per lead keeps the count simple
    send(client, ads_payload())
    mailer.fail_with = RuntimeError("smtp is down")
    mailer.fail_times = -1
    fail_until_given_up(ctx, clock)

    check = checks(ctx)["email_failures"]
    assert check.status == "critical"
    assert check.detail == {"count": 1, "first_responses_abandoned": 1}
    assert "1 first response(s) were abandoned because sending failed on our side" in check.summary
    # The message is no longer "waiting", so without this the backlog check going green
    # would have been the only signal.
    assert checks(ctx)["email_backlog"].status == "ok"


def test_email_failures_clears_once_an_operator_retry_delivers_the_message(client, ctx, mailer, clock, settings):
    # A check must be clearable by fixing the cause: retry after the provider is back, and it is ok.
    settings.followup_delays_minutes = ""
    send(client, ads_payload())
    mailer.fail_with = RuntimeError("smtp is down")
    mailer.fail_times = -1
    fail_until_given_up(ctx, clock)
    assert checks(ctx)["email_failures"].status == "critical"

    mailer.fail_with = None  # credentials fixed
    message = db_one(ctx, "SELECT id FROM outbox_messages WHERE template = 'intro'")
    with ctx.db.transaction() as conn:
        assert outbox.requeue_failed(conn, message["id"], ctx.now()) is True
    ctx.worker.deliver_due()

    assert len(mailer.sent) == 1
    assert checks(ctx)["email_failures"].status == "ok"


def test_email_failures_clears_after_24_hours(client, ctx, mailer, clock):
    # The check looks at the last day only.
    send(client, ads_payload(Email="gone@example.com"))
    mailer.fail_with = PermanentEmailError("550 mailbox does not exist")
    mailer.fail_times = 1
    clock.advance(seconds=1)
    ctx.worker.deliver_due()
    assert checks(ctx)["email_failures"].status == "warning"

    clock.advance(hours=24, seconds=1)
    assert checks(ctx)["email_failures"].status == "ok"


def test_email_failures_sees_a_follow_up_that_fails_a_day_after_it_was_queued(client, ctx, mailer, clock):
    # A message we gave up on ONE MINUTE ago happened "in the last 24h", whenever it was queued.
    send(client, ads_payload())
    clock.advance(seconds=1)
    ctx.worker.deliver_due()  # intro
    clock.advance(minutes=61)
    ctx.worker.deliver_due()  # follow-up 1
    clock.advance(hours=23, minutes=5)  # follow-up 2 (queued with the lead, due at +24 h) is now due
    mailer.fail_with = PermanentEmailError("550 mailbox does not exist")
    mailer.fail_times = -1
    ctx.worker.deliver_due()

    failed = db_rows(ctx, "SELECT template, sequence FROM outbox_messages WHERE status = 'failed'")
    assert failed == [{"template": "followup", "sequence": 2}]
    clock.advance(minutes=1)
    check = checks(ctx)["email_failures"]
    assert check.status == "warning"  # a follow-up, and a bad address: not a first response we abandoned
    assert check.detail == {"count": 1, "first_responses_abandoned": 0}
    # The window runs from the moment we gave up, so it is still reported 23 hours later...
    clock.advance(hours=23)
    assert checks(ctx)["email_failures"].status == "warning"
    # ...and no longer after 24.
    clock.advance(hours=1)
    assert checks(ctx)["email_failures"].status == "ok"


# --- lead_silence: fixed threshold ---------------------------------------------------------


def test_fixed_silence_check_is_absent_when_disabled(client, ctx, clock):
    # silence_alert_minutes = 0 (the default) switches the fixed threshold off entirely.
    send(client, ads_payload())
    clock.advance(days=3)
    assert "lead_silence" not in checks(ctx)


def test_fixed_silence_check_ok_before_any_traffic(client, ctx, clock, settings):
    # A new install that has never received a lead is not "silent".
    settings.silence_alert_minutes = 30
    clock.advance(days=1)
    check = checks(ctx)["lead_silence"]
    assert check.status == "ok"
    assert check.summary == "no Angi traffic received yet"


def test_fixed_silence_check_warns_after_the_configured_minutes(client, ctx, clock, settings):
    # No webhook at all for longer than the limit raises the dead man's switch.
    settings.silence_alert_minutes = 30
    send(client, ads_payload())
    clock.advance(minutes=30)
    at_limit = checks(ctx)["lead_silence"]
    assert at_limit.status == "ok"  # exactly at the limit is not over it

    clock.advance(minutes=1)
    check = checks(ctx)["lead_silence"]
    assert check.status == "warning"
    assert check.detail == {"last_event_at": iso(START), "minutes_since": 31.0, "limit_minutes": 30}
    assert check.summary == "last Angi webhook was 31 min ago (limit 30 min)"


def test_fixed_silence_check_resets_on_any_new_webhook(client, ctx, clock, settings):
    # Any stored request, even a quarantined one, proves Angi is still sending.
    settings.silence_alert_minutes = 30
    send(client, ads_payload())
    clock.advance(minutes=45)
    assert checks(ctx)["lead_silence"].status == "warning"

    quarantine_one(client, 2)
    assert checks(ctx)["lead_silence"].status == "ok"


def test_fixed_silence_check_is_not_reset_by_the_canary(client, ctx, clock, settings):
    # The probe posts every few minutes through our own URL; it says nothing about Angi.
    settings.silence_alert_minutes = 30
    send(client, ads_payload())
    clock.advance(minutes=45)

    assert send(client, ads_payload(**consumer(2), ALAccountId=CANARY_ACCOUNT))["tenant"]["id"] == "tnt_canary"
    check = checks(ctx)["lead_silence"]
    assert check.status == "warning"
    assert check.detail["last_event_at"] == iso(START)


def test_fixed_silence_check_is_not_reset_by_a_rejected_body(client, ctx, clock, settings):
    # Anyone can post junk at the URL; a body we rejected is not a lead from Angi.
    settings.silence_alert_minutes = 30
    send(client, ads_payload())
    clock.advance(minutes=45)

    assert post_lead(client, "this is not json").status_code == 400
    assert checks(ctx)["lead_silence"].status == "warning"


def test_fixed_silence_check_ignores_an_event_the_operator_discarded(client, ctx, clock, settings):
    # A quarantined request counts as traffic until an operator says it was junk.
    settings.silence_alert_minutes = 30
    send(client, ads_payload())
    clock.advance(minutes=45)
    junk = send(client, {"foo": "bar", "baz": 1})
    assert checks(ctx)["lead_silence"].status == "ok"

    with ctx.db.transaction() as conn:
        assert queries.discard_event(conn, junk["eventId"], ctx.now()) is True
    assert checks(ctx)["lead_silence"].status == "warning"


def test_fixed_silence_before_any_real_traffic_is_ok_even_with_canary_runs(client, ctx, clock, settings):
    # Only probe traffic so far: there is no real lead to be "silent" since.
    settings.silence_alert_minutes = 30
    send(client, ads_payload(ALAccountId=CANARY_ACCOUNT))
    clock.advance(hours=2)
    assert checks(ctx)["lead_silence"].summary == "no Angi traffic received yet"


# --- lead_silence: learned per-tenant rate -------------------------------------------------


def local(day: int, hour: int, minute: int = 0) -> datetime:
    """``day`` days after Thu 2026-11-12 at the given Indianapolis wall-clock time, as UTC.
    (No daylight-saving change falls inside the weeks these tests use.)"""
    moment = datetime(2026, 11, 12, hour, minute, tzinfo=INDIANAPOLIS) + timedelta(days=day)
    return moment.astimezone(UTC)


def build_history(client, clock, *, account: str = SPARKLE_ACCOUNT, days=range(1, 15), hours=range(9, 18), first_n=0):
    """One lead per listed hour on each listed day, posted through the real webhook with the
    clock set to that moment (fast enough that no direct SQL is needed). Every lead is a
    distinct consumer so none is a duplicate. Returns how many leads were created."""
    n = first_n
    for day in days:
        for hour in hours:
            clock.set(local(day, hour))
            n += 1
            body = send(client, ads_payload(**consumer(n), ALAccountId=account))
            assert body["result"] == "processed" and body["duplicate"] is None
    return n - first_n


def test_rate_based_silence_does_not_alert_during_the_following_night(client, ctx, clock):
    # 14 days of leads between 09:00 and 17:00: a quiet night is normal, not an outage.
    assert build_history(client, clock) == 126

    clock.set(local(15, 3))  # 03:00 the night after the last lead
    check = checks(ctx)[SPARKLE_SILENCE]
    assert check.status == "ok"
    assert check.detail["hours_quiet"] == 10.0
    assert check.detail["expected_leads"] <= 1.1  # only the tail of the 17:00 hour
    assert check.detail["tenant_id"] == "tnt_sparkle"

    clock.set(local(15, 8, 59))  # still before opening time
    assert checks(ctx)[SPARKLE_SILENCE].status == "ok"


def test_rate_based_silence_warns_by_mid_afternoon_of_a_day_without_leads(client, ctx, clock):
    # By 16:00 about seven leads should have arrived; none did, so Angi has probably stopped sending.
    build_history(client, clock)

    clock.set(local(15, 12))
    assert checks(ctx)[SPARKLE_SILENCE].status == "ok"  # lunchtime: quiet, but not yet beyond chance

    clock.set(local(15, 16))
    check = checks(ctx)[SPARKLE_SILENCE]
    assert check.status == "warning"
    assert "Sparkle Home Cleaning" in check.summary
    assert check.summary.startswith("No Angi leads for Sparkle Home Cleaning in 23.0h")
    assert check.detail["tenant_id"] == "tnt_sparkle"
    assert check.detail["expected_leads"] >= 7
    assert check.detail["hours_quiet"] == 23.0
    assert check.detail["last_lead_at"] == iso(local(14, 17))
    assert check.detail["probability_by_chance"] < monitoring.SILENCE_P_THRESHOLD
    assert 8.0 <= check.detail["baseline_leads_per_day"] <= 9.0
    # The other tenants have no history and stay quiet.
    assert checks(ctx)["lead_silence:summit-heating-air"].status == "ok"
    assert checks(ctx)["lead_silence:blueline-plumbing"].status == "ok"


def test_rate_based_silence_clears_as_soon_as_a_lead_arrives(client, ctx, clock):
    # One new lead resets the gap.
    build_history(client, clock)
    clock.set(local(15, 16))
    assert checks(ctx)[SPARKLE_SILENCE].status == "warning"

    send(client, ads_payload(**consumer(5000)))
    assert checks(ctx)[SPARKLE_SILENCE].status == "ok"


def test_tenant_with_fewer_leads_than_the_minimum_history_never_alerts(client, ctx, clock):
    # 19 leads is one short of a trustworthy baseline: no alert however long the silence.
    assert monitoring.SILENCE_MIN_HISTORY == 20
    created = build_history(client, clock, days=range(1, 8), hours=(10, 13))  # 14 leads over a week
    for n, hour in enumerate((14, 15, 16, 17, 18)):
        clock.set(local(7, hour))
        send(client, ads_payload(**consumer(900 + n)))
    assert created + 5 == 19

    for when in (local(8, 16), local(10, 16), local(16, 16), local(40, 16)):
        clock.set(when)
        check = checks(ctx)[SPARKLE_SILENCE]
        assert check.status == "ok"
        assert check.summary == "Sparkle Home Cleaning: not enough history for a baseline (19 leads over 7 days)"


def test_tenant_with_exactly_the_minimum_history_does_alert(client, ctx, clock):
    # 20 leads over 7 days is the smallest baseline we trust. Three leads a day (two on the
    # last day): by Saturday evening about 7.6 were due since Thursday 13:00 and none came.
    assert monitoring.SILENCE_MIN_DAYS == 7
    build_history(client, clock, days=range(1, 7), hours=(10, 13, 16))  # 18 leads, Fri-Wed
    for n, hour in ((900, 10), (901, 13)):
        clock.set(local(7, hour))  # Thursday
        send(client, ads_payload(**consumer(n)))

    clock.set(local(8, 17))  # Friday evening: 4.6 expected, could still be chance
    assert checks(ctx)[SPARKLE_SILENCE].status == "ok"
    clock.set(local(9, 17))  # Saturday evening
    check = checks(ctx)[SPARKLE_SILENCE]
    assert check.status == "warning"
    assert check.detail["expected_leads"] == 7.6
    assert check.detail["baseline_days"] == 7
    assert check.summary.endswith("about 8 were expected from the 7 days of history before that")


def test_plenty_of_leads_over_fewer_than_seven_days_is_not_a_baseline(client, ctx, clock):
    # 24 leads, but only six days of them: one short of a week, so the rhythm is not trusted yet.
    assert build_history(client, clock, days=range(1, 7), hours=(10, 12, 14, 16)) == 24

    for when in (local(7, 16), local(12, 16), local(30, 16)):
        clock.set(when)
        check = checks(ctx)[SPARKLE_SILENCE]
        assert check.status == "ok"
        assert check.summary == "Sparkle Home Cleaning: not enough history for a baseline (24 leads over 6 days)"


def test_a_burst_on_the_first_day_does_not_raise_a_silence_alert(client, ctx, clock):
    # Go-live, a load test, or rehearsing the demo: 25 leads in 25 minutes, then nothing.
    # Measured as "25 leads a day" that would look like an outage twenty minutes later.
    for n in range(25):
        clock.set(local(1, 10, n))
        send(client, ads_payload(**consumer(n + 1)))

    for when in (local(1, 10, 45), local(1, 20), local(2, 16), local(5, 16)):
        clock.set(when)
        check = checks(ctx)[SPARKLE_SILENCE]
        assert check.status == "ok"
        assert check.summary == "Sparkle Home Cleaning: not enough history for a baseline (25 leads over 1 days)"


def test_the_baseline_is_the_fourteen_days_before_the_last_lead(client, ctx, clock):
    # Three busy days, then one lead 17 days later. The 14 days before that last lead hold
    # only the lead itself, so the old burst is not used as "what normal looks like".
    build_history(client, clock, days=range(1, 4))  # 27 leads
    clock.set(local(20, 10))
    send(client, ads_payload(**consumer(900)))

    clock.set(local(30, 16))
    check = checks(ctx)[SPARKLE_SILENCE]
    assert check.status == "ok"
    assert check.summary == "Sparkle Home Cleaning: not enough history for a baseline (1 leads over 1 days)"


def test_silence_warning_does_not_clear_itself_while_the_outage_lasts(client, ctx, clock):
    # The baseline ends at the last lead, so the outage cannot dilute it: no lead, no all-clear.
    build_history(client, clock)
    clock.set(local(15, 16))
    first = checks(ctx)[SPARKLE_SILENCE]
    assert first.status == "warning"

    expected_so_far = first.detail["expected_leads"]
    for day in (17, 22, 29, 30, 45, 90):  # day 29 is when a window ending "now" would have emptied
        clock.set(local(day, 16))
        check = checks(ctx)[SPARKLE_SILENCE]
        assert check.status == "warning", day
        assert check.detail["expected_leads"] > expected_so_far
        assert check.detail["baseline_leads_per_day"] == 9.0  # unchanged by the silence
        assert check.detail["last_lead_at"] == iso(local(14, 17))
        expected_so_far = check.detail["expected_leads"]


def test_silence_alert_stays_open_through_a_long_outage_and_closes_on_the_next_lead(client, ctx, clock):
    # One alert row for the whole outage; it is the arriving lead that closes it.
    build_history(client, clock)
    key = f"check:{SPARKLE_SILENCE}"
    for day in range(15, 46, 3):
        clock.set(local(day, 16))
        monitoring.run_checks(ctx.db, ctx.settings, ctx.now())
        assert [a["status"] for a in alerts(ctx, key)] == ["open"], day

    send(client, ads_payload(**consumer(5000)))
    monitoring.run_checks(ctx.db, ctx.settings, ctx.now())
    assert [a["status"] for a in alerts(ctx, key)] == ["resolved"]


def test_a_tenant_with_two_leads_a_day_is_flagged_after_a_few_days_of_silence(client, ctx, clock):
    # A small business matters too. Two leads a day used to be too few to ever alert.
    assert build_history(client, clock, hours=(10, 15)) == 28

    clock.set(local(17, 12))  # the third silent day, noon: 6 expected, still within chance
    assert checks(ctx)[SPARKLE_SILENCE].status == "ok"
    clock.set(local(17, 16))  # that afternoon: 7 expected and none arrived
    check = checks(ctx)[SPARKLE_SILENCE]
    assert check.status == "warning"
    assert check.detail["expected_leads"] == 7.0
    assert check.detail["baseline_leads_per_day"] == 2.0
    clock.set(local(40, 16))
    assert checks(ctx)[SPARKLE_SILENCE].status == "warning"


def weekdays_only(days) -> list[int]:
    return [day for day in days if local(day, 12).astimezone(INDIANAPOLIS).weekday() < 5]


def test_a_weekday_business_is_not_silent_at_the_weekend(client, ctx, clock):
    # Eight leads every weekday, none on Saturday or Sunday, for two weeks ending Friday 27th.
    # A single rate for all days would expect about 7 leads by Saturday evening.
    working_days = weekdays_only(range(1, 16))
    assert len(working_days) == 11
    build_history(client, clock, days=working_days, hours=range(9, 17))

    for when in (local(16, 17), local(17, 12), local(18, 8)):  # Saturday, Sunday, Monday 08:00
        clock.set(when)
        check = checks(ctx)[SPARKLE_SILENCE]
        assert check.status == "ok", when
        assert check.detail["expected_leads"] <= 1.0  # only the rest of Friday's 16:00 hour


def test_a_weekday_business_is_flagged_when_monday_brings_nothing(client, ctx, clock):
    # The weekday rate still applies on weekdays: a Monday without leads is an outage.
    build_history(client, clock, days=weekdays_only(range(1, 16)), hours=range(9, 17))

    clock.set(local(18, 12))  # Monday noon: three leads due, could be chance
    assert checks(ctx)[SPARKLE_SILENCE].status == "ok"
    clock.set(local(18, 17, 30))  # Monday evening: a whole working day without one
    check = checks(ctx)[SPARKLE_SILENCE]
    assert check.status == "warning"
    assert check.detail["expected_leads"] >= 8


def test_canary_and_inactive_tenants_are_skipped_by_the_silence_check(client, ctx, clock):
    # The probe tenant is synthetic and a deactivated tenant is expected to be silent.
    for index, account in enumerate((SPARKLE_ACCOUNT, BLUELINE_ACCOUNT, CANARY_ACCOUNT)):
        created = build_history(
            client, clock, account=account, days=range(1, 8), hours=(10, 12, 14, 16), first_n=1000 * index
        )
        assert created == 28
    with ctx.db.transaction() as conn:  # there is no API to deactivate a tenant; flip the flag directly
        conn.execute("UPDATE tenants SET is_active = 0 WHERE id = 'tnt_blueline'")

    clock.set(local(12, 16))  # five silent days for everyone
    result = checks(ctx)
    silence_checks = {name for name in result if name.startswith("lead_silence")}
    assert silence_checks == {SPARKLE_SILENCE, "lead_silence:summit-heating-air"}
    assert result[SPARKLE_SILENCE].status == "warning"  # same history, not skipped: it does alert


def test_run_checks_opens_and_resolves_the_per_tenant_silence_alert(client, ctx, clock):
    # The silence warning becomes an alert keyed by tenant, and closes itself when leads resume.
    build_history(client, clock)
    clock.set(local(15, 16))
    monitoring.run_checks(ctx.db, ctx.settings, ctx.now())

    key = f"check:{SPARKLE_SILENCE}"
    opened = alerts(ctx, key)
    assert len(opened) == 1
    assert opened[0]["kind"] == "lead_silence"
    assert opened[0]["severity"] == "warning"
    assert opened[0]["status"] == "open"
    assert "Sparkle Home Cleaning" in opened[0]["title"]
    assert json.loads(opened[0]["detail_json"])["tenant_id"] == "tnt_sparkle"

    send(client, ads_payload(**consumer(5000)))
    monitoring.run_checks(ctx.db, ctx.settings, ctx.now())
    assert [a["status"] for a in alerts(ctx, key)] == ["resolved"]


def test_silence_alert_is_resolved_when_its_tenant_is_deactivated(client, ctx, clock):
    # A switched-off tenant is supposed to be silent; its silence alert must not stay open forever.
    build_history(client, clock, days=range(1, 8), hours=(10, 12, 14, 16))
    clock.set(local(12, 16))
    monitoring.run_checks(ctx.db, ctx.settings, ctx.now())
    key = f"check:{SPARKLE_SILENCE}"
    assert [a["status"] for a in alerts(ctx, key)] == ["open"]

    with ctx.db.transaction() as conn:  # there is no API to deactivate a tenant; flip the flag directly
        conn.execute("UPDATE tenants SET is_active = 0 WHERE id = 'tnt_sparkle'")
    clock.advance(hours=1)
    monitoring.run_checks(ctx.db, ctx.settings, ctx.now())
    assert [a["status"] for a in alerts(ctx, key)] == ["resolved"]
    assert alerts(ctx, key)[0]["resolved_at"] == iso(clock())


def test_fixed_silence_alert_is_resolved_when_the_threshold_is_switched_off(client, ctx, clock, settings):
    # With SILENCE_ALERT_MINUTES back at 0 the check no longer exists; its alert goes with it.
    settings.silence_alert_minutes = 30
    send(client, ads_payload())
    clock.advance(minutes=45)
    monitoring.run_checks(ctx.db, ctx.settings, ctx.now())
    assert [a["status"] for a in alerts(ctx, "check:lead_silence")] == ["open"]

    settings.silence_alert_minutes = 0
    clock.advance(minutes=1)
    monitoring.run_checks(ctx.db, ctx.settings, ctx.now())
    assert [a["status"] for a in alerts(ctx, "check:lead_silence")] == ["resolved"]


# --- expected_leads_between ----------------------------------------------------------------


def utc(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=UTC)


HOUR_AS_RATE = [float(hour) for hour in range(24)]  # hour h expects h leads per hour: easy to add up


def test_expected_leads_over_whole_hours():
    # 10:00-13:00 covers hours 10, 11 and 12 in full.
    assert expected_leads_between(HOUR_AS_RATE, utc(12, 10), utc(12, 13), UTC) == pytest.approx(10 + 11 + 12)


def test_expected_leads_over_a_single_whole_hour():
    # One full hour at that hour's rate.
    assert expected_leads_between(HOUR_AS_RATE, utc(12, 9), utc(12, 10), UTC) == pytest.approx(9.0)


def test_expected_leads_over_partial_hours():
    # 10:15-12:30 = 45 min of hour 10, all of hour 11, 30 min of hour 12.
    expected = 10 * 0.75 + 11 + 12 * 0.5
    assert expected_leads_between(HOUR_AS_RATE, utc(12, 10, 15), utc(12, 12, 30), UTC) == pytest.approx(expected)


def test_expected_leads_inside_one_hour():
    # 20 minutes of hour 14.
    assert expected_leads_between(HOUR_AS_RATE, utc(12, 14, 10), utc(12, 14, 30), UTC) == pytest.approx(14 / 3)


def test_expected_leads_across_midnight():
    # 22:30-01:30 = half of hour 22, all of hour 23, all of hour 0, half of hour 1.
    expected = 22 * 0.5 + 23 + 0 + 1 * 0.5
    assert expected_leads_between(HOUR_AS_RATE, utc(12, 22, 30), utc(13, 1, 30), UTC) == pytest.approx(expected)


def test_expected_leads_over_a_full_day_is_the_sum_of_the_hourly_rates():
    # Any 24-hour span sees each hour of the day exactly once.
    assert expected_leads_between(HOUR_AS_RATE, utc(12, 7, 20), utc(13, 7, 20), UTC) == pytest.approx(sum(range(24)))


def test_expected_leads_for_an_empty_or_reversed_span_is_zero():
    # No time elapsed (or an end before the start) expects nothing and must not loop forever.
    assert expected_leads_between(HOUR_AS_RATE, utc(12, 10), utc(12, 10), UTC) == 0.0
    assert expected_leads_between(HOUR_AS_RATE, utc(12, 10), utc(12, 8), UTC) == 0.0


def test_expected_leads_uses_the_hour_of_day_in_the_given_zone():
    # 15:00-16:00 UTC is 10:00-11:00 in Indianapolis (UTC-5): the rate for local hour 10 applies.
    rate = [0.0] * 24
    rate[10] = 4.0
    assert expected_leads_between(rate, utc(12, 15), utc(12, 16), INDIANAPOLIS) == pytest.approx(4.0)
    assert expected_leads_between(rate, utc(12, 15), utc(12, 16), UTC) == 0.0


def test_expected_leads_in_a_half_hour_offset_zone():
    # Asia/Kolkata is UTC+5:30: 04:00-06:00 UTC is 09:30-11:30 local, containing all of local hour 10.
    rate = [0.0] * 24
    rate[10] = 6.0
    kolkata = ZoneInfo("Asia/Kolkata")
    assert expected_leads_between(rate, utc(12, 4), utc(12, 6), kolkata) == pytest.approx(6.0)


def test_expected_leads_on_daylight_saving_change_days():
    # The autumn day has 25 local hours and the spring day 23; a flat rate must reflect that.
    new_york = ZoneInfo("America/New_York")
    flat = [1.0] * 24
    autumn = expected_leads_between(
        flat, datetime(2026, 11, 1, 4, tzinfo=UTC), datetime(2026, 11, 2, 5, tzinfo=UTC), new_york
    )
    spring = expected_leads_between(
        flat, datetime(2026, 3, 8, 5, tzinfo=UTC), datetime(2026, 3, 9, 4, tzinfo=UTC), new_york
    )
    assert autumn == pytest.approx(25.0)
    assert spring == pytest.approx(23.0)


def test_expected_leads_uses_the_weekend_rate_on_saturday_and_sunday():
    # Friday 13 November 22:00 to Monday 16 November 02:00 (UTC): two hours of Friday, the
    # whole weekend, two hours of Monday.
    weekday, weekend = [1.0] * 24, [0.25] * 24
    expected = expected_leads_between(weekday, utc(13, 22), utc(16, 2), UTC, weekend_rate=weekend)
    assert expected == pytest.approx(2 + 48 * 0.25 + 2)
    # Without a weekend rate every day is treated alike.
    assert expected_leads_between(weekday, utc(13, 22), utc(16, 2), UTC) == pytest.approx(52.0)


# --- canary ------------------------------------------------------------------------------


def test_canary_ok_when_no_canary_lead_was_ever_sent(client, ctx, clock):
    # No probe configured is not a failure.
    send(client, ads_payload())  # a real lead, left unanswered, is not the canary's business
    clock.advance(minutes=10)
    check = checks(ctx)["canary"]
    assert check.status == "ok"
    assert check.summary == "no canary lead has been sent yet"


def test_canary_ok_when_its_email_was_sent(client, ctx, clock):
    # The probe went all the way through: received, mapped, emailed.
    body = send(client, ads_payload(ALAccountId=CANARY_ACCOUNT))
    assert body["tenant"]["id"] == "tnt_canary"
    clock.advance(seconds=2)
    ctx.worker.deliver_due()
    clock.advance(minutes=30)

    check = checks(ctx)["canary"]
    assert check.status == "ok"
    assert check.detail["lead_id"] == body["leadId"]
    assert check.detail["first_contacted_at"] == iso(START + timedelta(seconds=2))


def test_canary_ok_while_its_email_is_still_within_the_sla(client, ctx, clock):
    # A probe that arrived a minute ago has not failed yet.
    send(client, ads_payload(ALAccountId=CANARY_ACCOUNT))
    clock.advance(seconds=120)
    assert checks(ctx)["canary"].status == "ok"


def test_canary_critical_when_older_than_the_sla_with_no_email(client, ctx, clock):
    # The probe was stored but its email never left: the pipeline is broken end to end.
    body = send(client, ads_payload(ALAccountId=CANARY_ACCOUNT))
    clock.advance(seconds=121)

    check = checks(ctx)["canary"]
    assert check.status == "critical"
    assert check.detail == {"lead_id": body["leadId"], "received_at": iso(START), "first_contacted_at": None}
    assert body["leadId"] in check.summary
    assert "its email never went out" in check.summary


def test_canary_judges_only_the_most_recent_probe(client, ctx, clock):
    # An old failed probe is superseded by a newer one that passed.
    send(client, ads_payload(PhoneNumber="3175550001", ALAccountId=CANARY_ACCOUNT, Email=""))  # can never be emailed
    clock.advance(minutes=10)
    assert checks(ctx)["canary"].status == "critical"

    newer = send(client, ads_payload(**consumer(2), ALAccountId=CANARY_ACCOUNT))
    clock.advance(seconds=1)
    ctx.worker.deliver_due()
    clock.advance(minutes=10)
    check = checks(ctx)["canary"]
    assert check.status == "ok"
    assert check.detail["lead_id"] == newer["leadId"]


def test_canary_that_stopped_arriving_is_critical_once_its_schedule_is_configured(client, ctx, clock, settings):
    # A probe that passed once and then stopped running proves nothing about today.
    settings.canary_interval_minutes = 5
    body = send(client, ads_payload(ALAccountId=CANARY_ACCOUNT))
    clock.advance(seconds=2)
    ctx.worker.deliver_due()

    clock.set(START + timedelta(minutes=15))  # three intervals: the limit itself is still fine
    assert checks(ctx)["canary"].status == "ok"
    clock.advance(seconds=1)
    check = checks(ctx)["canary"]
    assert check.status == "critical"
    assert check.summary == "no canary lead has arrived for 15 min (one is expected every 5 min)"
    assert check.detail["lead_id"] == body["leadId"]

    send(client, ads_payload(**consumer(2), ALAccountId=CANARY_ACCOUNT))  # the probe is back
    clock.advance(seconds=2)
    ctx.worker.deliver_due()
    assert checks(ctx)["canary"].status == "ok"


def test_canary_staleness_is_not_judged_when_no_schedule_is_configured(client, ctx, clock, settings):
    # CANARY_INTERVAL_MINUTES = 0 (the default): the probe is run by hand, so its age means nothing.
    assert settings.canary_interval_minutes == 0
    send(client, ads_payload(ALAccountId=CANARY_ACCOUNT))
    clock.advance(seconds=2)
    ctx.worker.deliver_due()
    clock.advance(days=3)
    assert checks(ctx)["canary"].status == "ok"


def test_canary_never_sent_is_ok_even_with_a_schedule(client, ctx, clock, settings):
    # Nothing to be stale until the first probe has arrived.
    settings.canary_interval_minutes = 5
    clock.advance(days=1)
    assert checks(ctx)["canary"].summary == "no canary lead has been sent yet"


# --- run_checks: checks become alerts -------------------------------------------------------


def test_run_checks_on_a_healthy_system_opens_no_alert_and_records_the_run(client, ctx, clock):
    # Nothing failing means nothing to announce; the time of the run is still recorded.
    result = monitoring.run_checks(ctx.db, ctx.settings, ctx.now())

    assert {check.status for check in result} == {"ok"}
    assert alerts(ctx) == []
    assert db_one(ctx, "SELECT value FROM kv WHERE key = 'monitor:last_run'") == {"value": iso(START)}


def test_run_checks_updates_monitor_last_run_on_every_run(client, ctx, clock):
    # monitor:last_run always holds the most recent run.
    monitoring.run_checks(ctx.db, ctx.settings, ctx.now())
    clock.advance(minutes=7)
    monitoring.run_checks(ctx.db, ctx.settings, ctx.now())

    rows = db_rows(ctx, "SELECT value, updated_at FROM kv WHERE key = 'monitor:last_run'")
    assert rows == [{"value": iso(START + timedelta(minutes=7)), "updated_at": iso(START + timedelta(minutes=7))}]


def test_run_checks_opens_one_alert_per_failing_check_keyed_by_check_name(client, ctx, clock):
    # Three quarantined leads fail two checks; each gets exactly one alert named after its check.
    for n in (1, 2, 3):
        quarantine_one(client, n)
    monitoring.run_checks(ctx.db, ctx.settings, ctx.now())

    check_alerts = {a["dedupe_key"]: a for a in alerts(ctx) if a["dedupe_key"].startswith("check:")}
    assert set(check_alerts) == {"check:ingest_errors", "check:quarantine"}
    assert check_alerts["check:ingest_errors"]["severity"] == "critical"
    assert check_alerts["check:ingest_errors"]["kind"] == "ingest_errors"
    assert check_alerts["check:quarantine"]["severity"] == "warning"
    assert check_alerts["check:quarantine"]["kind"] == "quarantine"
    assert check_alerts["check:quarantine"]["title"].startswith("3 lead(s) are quarantined")
    assert json.loads(check_alerts["check:quarantine"]["detail_json"]) == {
        "count": 3,
        "by_reason": {"unmapped_account": 3},
    }
    assert all(a["status"] == "open" and a["count"] == 1 for a in check_alerts.values())


def test_run_checks_does_not_inflate_the_count_on_every_run(client, ctx, clock):
    # The monitor runs every minute; "count" must mean occurrences, not minutes of being broken.
    quarantine_one(client)
    for _ in range(6):
        clock.advance(minutes=1)
        monitoring.run_checks(ctx.db, ctx.settings, ctx.now())

    rows = alerts(ctx, "check:quarantine")
    assert len(rows) == 1
    assert rows[0]["count"] == 1
    assert rows[0]["first_seen_at"] == iso(START + timedelta(minutes=1))
    assert rows[0]["last_seen_at"] == iso(START + timedelta(minutes=6))  # still refreshed


def test_run_checks_keeps_the_alert_text_up_to_date(client, ctx, clock):
    # While the alert stays open its title and detail follow the current state of the check.
    quarantine_one(client, 1)
    monitoring.run_checks(ctx.db, ctx.settings, ctx.now())
    quarantine_one(client, 2)
    monitoring.run_checks(ctx.db, ctx.settings, ctx.now())

    rows = alerts(ctx, "check:quarantine")
    assert len(rows) == 1
    assert rows[0]["title"].startswith("2 lead(s) are quarantined")
    assert json.loads(rows[0]["detail_json"])["count"] == 2


def test_run_checks_resolves_the_alert_when_the_condition_clears(client, ctx, clock):
    # Nobody should have to close an alert for a problem that fixed itself.
    quarantine_one(client)
    monitoring.run_checks(ctx.db, ctx.settings, ctx.now())
    assert [a["status"] for a in alerts(ctx, "check:quarantine")] == ["open"]

    client.post(
        "/admin/api/tenants/tnt_sparkle/lead-sources", headers=ADMIN, json={"external_account_id": UNMAPPED_ACCOUNT}
    )
    clock.advance(minutes=1)
    monitoring.run_checks(ctx.db, ctx.settings, ctx.now())

    rows = alerts(ctx, "check:quarantine")
    assert len(rows) == 1
    assert rows[0]["status"] == "resolved"
    assert rows[0]["resolved_at"] == iso(START + timedelta(minutes=1))


def test_run_checks_opens_a_new_alert_when_the_problem_comes_back(client, ctx, clock):
    # A recurrence after resolution is a new incident with its own row.
    quarantine_one(client, 1)
    monitoring.run_checks(ctx.db, ctx.settings, ctx.now())
    client.post(
        "/admin/api/tenants/tnt_sparkle/lead-sources", headers=ADMIN, json={"external_account_id": UNMAPPED_ACCOUNT}
    )
    monitoring.run_checks(ctx.db, ctx.settings, ctx.now())
    clock.advance(hours=1)
    quarantine_one(client, 2, account="000222")
    monitoring.run_checks(ctx.db, ctx.settings, ctx.now())

    assert [a["status"] for a in alerts(ctx, "check:quarantine")] == ["resolved", "open"]


def run_and_announce(ctx) -> int:
    """One monitor run followed by one notifier pass, as the worker does them."""
    monitoring.run_checks(ctx.db, ctx.settings, ctx.now())
    return ctx.notifier.dispatch(ctx.db, ctx.now())


def test_check_alert_closed_by_hand_while_still_failing_is_reopened_without_paging_again(client, ctx, clock, slack):
    # "Resolve" on a check alert cannot make the problem go away. The next run opens the
    # alert again, but it is the same incident: nobody is paged a second time for it.
    quarantine_one(client)
    assert run_and_announce(ctx) == 2  # the quarantined lead itself, and the quarantine check
    first = alerts(ctx, "check:quarantine")[0]

    for _ in range(3):  # the operator clicks "resolve" three minutes in a row
        clock.advance(seconds=30)
        with ctx.db.transaction() as conn:
            assert resolve_alert(conn, "check:quarantine", ctx.now()) is True
        clock.advance(seconds=30)
        assert run_and_announce(ctx) == 0

    rows = alerts(ctx, "check:quarantine")
    assert [a["status"] for a in rows] == ["resolved", "resolved", "resolved", "open"]
    assert rows[-1]["notified_at"] == first["notified_at"]  # carried over from the first announcement
    assert len(slack.calls) == 2


def test_check_that_passed_and_fails_again_later_is_a_new_incident_and_is_announced(client, ctx, clock, slack):
    # Only an alert closed by hand keeps quiet. One the monitor closed because the check
    # passed, and that fails again an hour later, is news.
    quarantine_one(client, 1)
    assert run_and_announce(ctx) == 2
    client.post(
        "/admin/api/tenants/tnt_sparkle/lead-sources", headers=ADMIN, json={"external_account_id": UNMAPPED_ACCOUNT}
    )
    ctx.worker.deliver_due()  # the released lead is answered
    clock.advance(minutes=1)
    assert run_and_announce(ctx) == 0
    assert [a["status"] for a in alerts(ctx, "check:quarantine")] == ["resolved"]

    clock.advance(hours=1)
    quarantine_one(client, 2, account="000222")
    assert run_and_announce(ctx) == 2
    assert [a["status"] for a in alerts(ctx, "check:quarantine")] == ["resolved", "open"]


def test_failing_check_is_announced_again_once_a_day_while_it_lasts(client, ctx, clock, slack):
    # A problem nobody acted on is repeated daily, not every minute and not only once.
    send(client, {"hello": "world"})  # quarantined for good unless someone discards it
    announcements = []
    for hour in range(73):  # three days, checked hourly
        clock.set(START + timedelta(hours=hour))
        if run_and_announce(ctx):
            announcements.append(hour)

    # Hour 0 also announces the "payload not recognised" event alert, which then closes
    # itself after a quiet day; the daily reminders are the quarantine check's.
    assert announcements == [0, 24, 48, 72]
    quarantine_texts = [text for text in slack.texts if "quarantined and waiting" in text]
    assert len(quarantine_texts) == 4
    assert [a["status"] for a in alerts(ctx, "check:quarantine")] == ["open"]


def test_processing_error_alert_is_resolved_once_no_event_is_left_failed(client, ctx, clock, monkeypatch):
    # The retry loop got the lead through: the "processing failed" alert is over.
    def explode(*args, **kwargs):
        raise RuntimeError("injected processing failure")

    monkeypatch.setattr("app.ingest.find_duplicate", explode)
    assert send(client, ads_payload())["result"] == "accepted"
    clock.advance(minutes=1)
    monitoring.run_checks(ctx.db, ctx.settings, ctx.now())
    assert [(a["kind"], a["status"]) for a in alerts(ctx, "processing_error:RuntimeError")] == [
        ("processing_error", "open")
    ]

    monkeypatch.undo()  # the "bug" is fixed
    clock.advance(minutes=1)
    ctx.worker.run_maintenance()  # retries the event, then runs the checks
    assert db_rows(ctx, "SELECT status FROM webhook_events") == [{"status": "processed"}]
    assert [a["status"] for a in alerts(ctx, "processing_error:RuntimeError")] == ["resolved"]


def test_event_alert_with_no_new_occurrence_for_a_day_is_resolved_by_the_monitor(client, ctx, clock):
    # Nothing ever reports "the rejected requests have stopped", so a quiet day closes the alert.
    raise_one(ctx, key="auth_failures")
    ctx.notifier.dispatch(ctx.db, ctx.now())  # announced: only an announced alert is closed as quiet
    clock.advance(hours=23, minutes=59)
    monitoring.run_checks(ctx.db, ctx.settings, ctx.now())
    assert [a["status"] for a in alerts(ctx, "auth_failures")] == ["open"]

    clock.advance(minutes=1)
    ctx.worker.run_maintenance()  # the place the worker calls
    row = alerts(ctx, "auth_failures")[0]
    assert (row["status"], row["resolved_at"]) == ("resolved", iso(START + timedelta(hours=24)))


def test_event_alert_that_keeps_happening_stays_open(client, ctx, clock):
    # The quiet day is counted from the LAST occurrence.
    raise_one(ctx, key="auth_failures")
    for _ in range(5):
        clock.advance(hours=20)
        raise_one(ctx, key="auth_failures")
        monitoring.run_checks(ctx.db, ctx.settings, ctx.now())

    rows = alerts(ctx, "auth_failures")
    assert [(a["status"], a["count"]) for a in rows] == [("open", 6)]


def test_recurrence_after_a_quiet_month_opens_a_new_alert_and_is_announced(client, ctx, clock, slack):
    # Week 1: one stray request without a key. Week 5: Angi changes how it sends the key and
    # every lead is rejected. That must reach somebody, not just bump a counter.
    assert client.post("/webhooks/angi/leads", json=ads_payload()).status_code == 401
    assert run_and_announce(ctx) == 1
    key = alerts(ctx)[0]["dedupe_key"]

    clock.advance(days=28)
    assert run_and_announce(ctx) == 0
    for _ in range(5):
        assert client.post("/webhooks/angi/leads", json=ads_payload()).status_code == 401
    assert run_and_announce(ctx) == 1

    assert [(a["status"], a["count"]) for a in alerts(ctx, key)] == [("resolved", 1), ("open", 5)]
    assert len(slack.calls) == 2


def test_check_alerts_are_not_closed_by_the_quiet_day_rule(client, ctx, clock):
    # A check alert is over when its check passes, however long ago it was last raised.
    raise_one(ctx, key="check:something", count_occurrence=False)
    clock.advance(days=3)
    with ctx.db.transaction() as conn:
        assert resolve_quiet_event_alerts(conn, ctx.now()) == 0
    assert [a["status"] for a in alerts(ctx, "check:something")] == ["open"]


def test_run_checks_returns_the_same_checks_as_evaluate(client, ctx, clock):
    # run_checks is evaluate plus bookkeeping; the verdicts are identical.
    quarantine_one(client)
    expected = {name: check.status for name, check in checks(ctx).items()}
    result = monitoring.run_checks(ctx.db, ctx.settings, ctx.now())
    assert {check.name: check.status for check in result} == expected


def test_admin_health_endpoint_is_read_only(client, ctx, clock):
    # Looking at the health page must not open or close alerts; only the monitor run does.
    quarantine_one(client)
    before = alerts(ctx)
    response = client.get("/admin/api/health", headers=ADMIN)
    assert response.json()["status"] == "warning"
    assert alerts(ctx) == before
    assert alerts(ctx, "check:quarantine") == []
    assert db_one(ctx, "SELECT value FROM kv WHERE key = 'monitor:last_run'") is None


# --- raise_alert / resolve_alert ------------------------------------------------------------


def test_raise_alert_opens_one_row_per_key(client, ctx, clock):
    # The first occurrence opens the alert and says "tell somebody".
    opened = raise_one(ctx, title="Disk is full", detail={"free_mb": 3})

    assert opened is True
    rows = alerts(ctx)
    assert len(rows) == 1
    row = rows[0]
    assert (row["kind"], row["dedupe_key"], row["severity"], row["title"]) == (
        "test", "test:problem", "warning", "Disk is full",
    )
    assert json.loads(row["detail_json"]) == {"free_mb": 3}
    assert (row["status"], row["count"]) == ("open", 1)
    assert row["first_seen_at"] == row["last_seen_at"] == iso(START)
    assert row["notified_at"] is None
    assert row["resolved_at"] is None


def test_raise_alert_folds_repeats_into_the_count(client, ctx, clock):
    # The same problem again is the same alert: count goes up, nobody is paged twice.
    raise_one(ctx, title="first", detail={"n": 1})
    clock.advance(minutes=5)
    assert raise_one(ctx, title="second", detail={"n": 2}) is False
    clock.advance(minutes=5)
    assert raise_one(ctx, title="third", detail={"n": 3}) is False

    rows = alerts(ctx)
    assert len(rows) == 1
    assert rows[0]["count"] == 3
    assert rows[0]["first_seen_at"] == iso(START)
    assert rows[0]["last_seen_at"] == iso(START + timedelta(minutes=10))
    assert rows[0]["title"] == "third"  # the latest wording wins
    assert json.loads(rows[0]["detail_json"]) == {"n": 3}


def test_different_keys_are_different_alerts(client, ctx, clock):
    # Two distinct problems never share a row.
    raise_one(ctx, key="unmapped_account:1")
    raise_one(ctx, key="unmapped_account:2")
    raise_one(ctx, key="unmapped_account:1")

    counts = {a["dedupe_key"]: a["count"] for a in alerts(ctx)}
    assert counts == {"unmapped_account:1": 2, "unmapped_account:2": 1}


def test_severity_escalates_from_warning_to_critical(client, ctx, clock):
    # A problem that gets worse while open is shown at its worst.
    raise_one(ctx, severity="warning")
    raise_one(ctx, severity="critical")
    assert alerts(ctx)[0]["severity"] == "critical"


@pytest.mark.parametrize("later", ["info", "warning"])
def test_severity_is_not_downgraded_while_the_alert_is_open(client, ctx, clock, later):
    # A milder repeat must not hide that the incident reached "critical".
    raise_one(ctx, severity="warning")
    raise_one(ctx, severity="critical")
    raise_one(ctx, severity=later)

    rows = alerts(ctx)
    assert len(rows) == 1
    assert rows[0]["severity"] == "critical"
    assert rows[0]["count"] == 3


def test_info_escalates_to_warning(client, ctx, clock):
    # The ranking is info < warning < critical.
    raise_one(ctx, severity="info")
    raise_one(ctx, severity="warning")
    raise_one(ctx, severity="info")
    assert alerts(ctx)[0]["severity"] == "warning"


def test_count_occurrence_false_leaves_the_count_unchanged(client, ctx, clock):
    # State-style alerts (health checks) refresh the row without counting each look as an occurrence.
    raise_one(ctx)
    clock.advance(minutes=1)
    assert raise_one(ctx, title="still wrong", count_occurrence=False) is False

    row = alerts(ctx)[0]
    assert row["count"] == 1
    assert row["title"] == "still wrong"
    assert row["last_seen_at"] == iso(START + timedelta(minutes=1))


def test_count_occurrence_false_on_a_new_alert_still_starts_at_one(client, ctx, clock):
    # Opening an alert is always its first occurrence.
    assert raise_one(ctx, count_occurrence=False) is True
    assert alerts(ctx)[0]["count"] == 1


def test_resolve_alert_closes_it(client, ctx, clock):
    # Resolving stamps when, and reports that something was closed.
    raise_one(ctx)
    clock.advance(minutes=3)
    with ctx.db.transaction() as conn:
        assert resolve_alert(conn, "test:problem", ctx.now()) is True

    row = alerts(ctx)[0]
    assert row["status"] == "resolved"
    assert row["resolved_at"] == iso(START + timedelta(minutes=3))


def test_resolve_alert_returns_false_when_nothing_is_open(client, ctx, clock):
    # Resolving a problem that is not open (never raised, or already resolved) is a no-op.
    with ctx.db.transaction() as conn:
        assert resolve_alert(conn, "test:problem", ctx.now()) is False
    raise_one(ctx)
    with ctx.db.transaction() as conn:
        assert resolve_alert(conn, "test:problem", ctx.now()) is True
        assert resolve_alert(conn, "test:problem", ctx.now()) is False


def test_raise_after_resolve_opens_a_new_row(client, ctx, clock):
    # A recurrence is a new incident: new row, count back to 1, severity starting afresh.
    raise_one(ctx, severity="critical")
    raise_one(ctx, severity="critical")
    with ctx.db.transaction() as conn:
        resolve_alert(conn, "test:problem", ctx.now())
    clock.advance(hours=1)
    assert raise_one(ctx, severity="warning") is True

    rows = alerts(ctx)
    assert [(r["status"], r["count"], r["severity"]) for r in rows] == [
        ("resolved", 2, "critical"),
        ("open", 1, "warning"),
    ]
    assert rows[1]["first_seen_at"] == iso(START + timedelta(hours=1))
    assert rows[1]["notified_at"] is None


def test_resolve_alert_by_id_only_closes_an_open_alert(client, ctx, clock):
    # The dashboard's "resolve" button works by id and reports whether it did anything.
    raise_one(ctx)
    alert_id = alerts(ctx)[0]["id"]
    with ctx.db.transaction() as conn:
        assert resolve_alert_by_id(conn, alert_id, ctx.now()) is True
        assert resolve_alert_by_id(conn, alert_id, ctx.now()) is False
        assert resolve_alert_by_id(conn, 99999, ctx.now()) is False


def test_alert_raised_inside_a_rolled_back_transaction_is_not_kept(client, ctx, clock):
    # Raising an alert is part of the surrounding unit of work.
    with pytest.raises(RuntimeError):
        with ctx.db.transaction() as conn:
            raise_alert(conn, kind="test", key="k", severity="info", title="t", detail=None, now=ctx.now())
            raise RuntimeError("abort")
    assert alerts(ctx) == []


# --- list_alerts -------------------------------------------------------------------------


def listed(ctx, status: str | None = None, **kwargs) -> list[dict]:
    with ctx.db.read() as conn:
        return list_alerts(conn, status, **kwargs)


def test_list_alerts_orders_open_before_resolved_and_critical_first(client, ctx, clock):
    # The operator's eye should land on the worst open problem.
    raise_one(ctx, key="resolved-critical", severity="critical")
    with ctx.db.transaction() as conn:
        resolve_alert(conn, "resolved-critical", ctx.now())
    clock.advance(minutes=1)
    raise_one(ctx, key="open-info", severity="info")
    clock.advance(minutes=1)
    raise_one(ctx, key="open-warning-older", severity="warning")
    clock.advance(minutes=1)
    raise_one(ctx, key="open-warning-newer", severity="warning")
    clock.advance(minutes=1)
    raise_one(ctx, key="open-critical", severity="critical")
    clock.advance(minutes=1)
    raise_one(ctx, key="resolved-info", severity="info")
    with ctx.db.transaction() as conn:
        resolve_alert(conn, "resolved-info", ctx.now())

    assert [a["dedupe_key"] for a in listed(ctx)] == [
        "open-critical",
        "open-warning-newer",  # same severity: most recently seen first
        "open-warning-older",
        "open-info",
        "resolved-critical",
        "resolved-info",
    ]


def test_list_alerts_status_filter(client, ctx, clock):
    # ?status=open / resolved narrows the list.
    raise_one(ctx, key="a")
    raise_one(ctx, key="b")
    with ctx.db.transaction() as conn:
        resolve_alert(conn, "a", ctx.now())

    assert [a["dedupe_key"] for a in listed(ctx, "open")] == ["b"]
    assert [a["dedupe_key"] for a in listed(ctx, "resolved")] == ["a"]
    assert len(listed(ctx)) == 2
    assert listed(ctx, "no-such-status") == []


def test_list_alerts_decodes_the_detail_and_honours_the_limit(client, ctx, clock):
    # Detail comes back as a dict, and the limit caps the list.
    for n in range(5):
        raise_one(ctx, key=f"k{n}", detail={"n": n})

    rows = listed(ctx, limit=3)
    assert len(rows) == 3
    assert all(isinstance(row["detail"], dict) and "detail_json" not in row for row in rows)
    assert listed(ctx)[0]["detail"] in [{"n": n} for n in range(5)]


# --- AlertNotifier -----------------------------------------------------------------------


def test_dispatch_announces_each_new_alert_exactly_once(client, ctx, clock, slack):
    # A new alert is announced on the next pass and never again.
    raise_one(ctx, key="a", title="Problem A")
    raise_one(ctx, key="b", title="Problem B")
    clock.advance(seconds=30)

    assert ctx.notifier.dispatch(ctx.db, ctx.now()) == 2
    assert [a["notified_at"] for a in alerts(ctx)] == [iso(START + timedelta(seconds=30))] * 2
    assert len(slack.calls) == 2

    clock.advance(minutes=1)
    assert ctx.notifier.dispatch(ctx.db, ctx.now()) == 0
    assert len(slack.calls) == 2
    assert [a["notified_at"] for a in alerts(ctx)] == [iso(START + timedelta(seconds=30))] * 2


def test_dispatch_with_nothing_pending_does_nothing(client, ctx, slack):
    # No alerts: nothing announced, no channel touched.
    assert ctx.notifier.dispatch(ctx.db, ctx.now()) == 0
    assert slack.calls == []


def test_repeat_of_an_already_announced_alert_is_not_announced_again(client, ctx, clock, slack):
    # Folding repeats into one alert is the whole point: one page per problem.
    raise_one(ctx)
    ctx.notifier.dispatch(ctx.db, ctx.now())
    for _ in range(3):
        clock.advance(minutes=1)
        raise_one(ctx)

    assert ctx.notifier.dispatch(ctx.db, ctx.now()) == 0
    assert len(slack.calls) == 1
    assert alerts(ctx)[0]["count"] == 4


def test_recurrence_after_resolution_is_announced_again(client, ctx, clock, slack):
    # A problem that comes back is a new alert and deserves a new announcement.
    raise_one(ctx)
    ctx.notifier.dispatch(ctx.db, ctx.now())
    with ctx.db.transaction() as conn:
        resolve_alert(conn, "test:problem", ctx.now())
    clock.advance(hours=1)
    raise_one(ctx)

    assert ctx.notifier.dispatch(ctx.db, ctx.now()) == 1
    assert len(slack.calls) == 2


def test_resolved_alert_is_not_announced(client, ctx, clock, slack):
    # A problem that cleared before the notifier ran is not worth waking anyone for.
    raise_one(ctx)
    with ctx.db.transaction() as conn:
        resolve_alert(conn, "test:problem", ctx.now())

    assert ctx.notifier.dispatch(ctx.db, ctx.now()) == 0
    assert slack.calls == []


def test_dispatch_posts_to_slack_when_a_webhook_url_is_configured(client, ctx, clock, slack):
    # The Slack message carries severity, title and a link to the monitoring page.
    raise_one(ctx, severity="critical", title="Lead processing failed: boom")
    ctx.notifier.dispatch(ctx.db, ctx.now())

    assert len(slack.calls) == 1
    call = slack.calls[0]
    assert call["url"] == SLACK_URL
    assert call["json"] == {"text": f"[CRITICAL] Lead processing failed: boom\n{BASE_URL}/dashboard/monitoring"}
    assert call["timeout"] == 5.0


def test_dispatch_does_not_call_slack_when_no_webhook_url_is_configured(client, ctx, clock, monkeypatch):
    # Slack is optional; without a URL the alert is only logged (and still marked announced).
    fake = FakeSlack()
    monkeypatch.setattr(httpx, "post", fake)
    raise_one(ctx)

    assert ctx.notifier.dispatch(ctx.db, ctx.now()) == 1
    assert fake.calls == []
    assert alerts(ctx)[0]["notified_at"] == iso(START)


def test_dispatch_emails_through_the_provider_when_alert_email_to_is_set(client, ctx, mailer, clock, settings):
    # The alert email goes through the same provider as lead email, to the configured address.
    settings.alert_email_to = "ops@example.test"
    raise_one(ctx, severity="warning", title="2 lead(s) are quarantined", detail={"count": 2})
    ctx.notifier.dispatch(ctx.db, ctx.now())

    assert len(mailer.sent) == 1
    email = mailer.sent[0]
    assert email.to == "ops@example.test"
    assert email.subject == "[Angi integration] [WARNING] 2 lead(s) are quarantined"
    assert email.from_address == "leads@example.test"
    assert email.from_name == "Lead integration alerts"
    assert "[WARNING] 2 lead(s) are quarantined" in email.text
    assert '{"count":2}' in email.text
    assert f"{BASE_URL}/dashboard/monitoring" in email.text
    assert email.html is None


def test_dispatch_sends_no_email_when_alert_email_to_is_empty(client, ctx, mailer, clock):
    # Email is optional too.
    raise_one(ctx)
    ctx.notifier.dispatch(ctx.db, ctx.now())
    assert mailer.sent == []


def test_dispatch_uses_both_channels_when_both_are_configured(client, ctx, mailer, clock, settings, slack):
    # Slack and email are independent; each alert goes to both.
    settings.alert_email_to = "ops@example.test"
    raise_one(ctx, key="a", title="A")
    raise_one(ctx, key="b", title="B")
    ctx.notifier.dispatch(ctx.db, ctx.now())

    assert len(slack.calls) == 2
    assert [email.subject for email in mailer.sent] == ["[Angi integration] [WARNING] A", "[Angi integration] [WARNING] B"]


def test_alert_is_not_marked_announced_while_a_channel_is_down(client, ctx, mailer, clock, settings, slack):
    # Both channels fail: nobody was told, so the alert must stay "to be announced".
    settings.alert_email_to = "ops@example.test"
    slack.raise_on_call = RuntimeError("no network")
    mailer.fail_with = RuntimeError("smtp is down")
    mailer.fail_times = -1
    raise_one(ctx)

    assert ctx.notifier.dispatch(ctx.db, ctx.now()) == 0  # the worker loop keeps going
    row = alerts(ctx)[0]
    assert (row["status"], row["notified_at"]) == ("open", None)


def test_failed_announcement_is_retried_once_per_monitor_interval_until_it_arrives(
    client, ctx, mailer, clock, settings
):
    # The mail provider is down when the alert opens and back a few minutes later. The alert
    # email must still go out, but a dead channel is not hammered on every two-second pass.
    assert settings.monitor_interval_seconds == 60
    settings.alert_email_to = "ops@example.test"
    mailer.fail_with = RuntimeError("smtp is down")
    mailer.fail_times = 2  # two attempts fail, the third gets through
    attempts = []
    original_send = mailer.send

    def counting_send(email):
        attempts.append(clock())
        return original_send(email)

    mailer.send = counting_send
    raise_one(ctx, title="Outbound email is failing")

    announced_at = None
    for second in range(0, 200, 2):  # the worker's poll interval
        clock.set(START + timedelta(seconds=second))
        if ctx.notifier.dispatch(ctx.db, ctx.now()):
            announced_at = second
    assert attempts == [START, START + timedelta(seconds=60), START + timedelta(seconds=120)]
    assert announced_at == 120
    assert [email.subject for email in mailer.sent] == ["[Angi integration] [WARNING] Outbound email is failing"]
    assert alerts(ctx)[0]["notified_at"] == iso(START + timedelta(seconds=120))


def test_alert_needs_every_configured_channel_and_never_repeats_one_that_arrived(
    client, ctx, mailer, clock, settings, slack
):
    # Slack works, email does not. The alert is not done until the email arrived too, and
    # Slack must not get the same message again on every retry.
    settings.alert_email_to = "ops@example.test"
    mailer.fail_with = RuntimeError("smtp is down")
    mailer.fail_times = -1
    raise_one(ctx, title="A")

    for minute in range(5):
        clock.set(START + timedelta(minutes=minute))
        assert ctx.notifier.dispatch(ctx.db, ctx.now()) == 0
    assert slack.texts == [f"[WARNING] A\n{BASE_URL}/dashboard/monitoring"]  # once
    assert alerts(ctx)[0]["notified_at"] is None

    mailer.fail_with = None  # the provider recovers
    clock.advance(minutes=1)
    assert ctx.notifier.dispatch(ctx.db, ctx.now()) == 1
    assert len(slack.calls) == 1
    assert [email.subject for email in mailer.sent] == ["[Angi integration] [WARNING] A"]
    assert alerts(ctx)[0]["notified_at"] == iso(clock())


@pytest.mark.parametrize("failure", ["raises", "error-status"])
def test_slack_failure_leaves_the_alert_to_be_announced(client, ctx, clock, slack, failure):
    # An exception and a 500 from Slack are the same thing: the message did not arrive.
    if failure == "raises":
        slack.raise_on_call = httpx.ConnectError("slack is unreachable")
    else:
        slack.status_code = 500
    raise_one(ctx)

    assert ctx.notifier.dispatch(ctx.db, ctx.now()) == 0
    assert alerts(ctx)[0]["notified_at"] is None

    slack.raise_on_call, slack.status_code = None, 200
    clock.advance(seconds=60)
    assert ctx.notifier.dispatch(ctx.db, ctx.now()) == 1
    assert len(slack.calls) == 2


def test_a_dead_channel_costs_one_attempt_per_pass_not_one_per_alert(client, ctx, clock, slack):
    # Every attempt on a dead channel blocks the thread that sends lead email for a
    # timeout, so a pass stops at the first failure and the next pass takes the next alert.
    slack.raise_on_call = httpx.ConnectError("slack is unreachable")
    for name in ("a", "b", "c"):
        raise_one(ctx, key=name, title=name.upper())

    for expected_calls in (1, 2, 3, 3, 3):  # after three passes all three are waiting for their retry
        assert ctx.notifier.dispatch(ctx.db, ctx.now()) == 0
        assert len(slack.calls) == expected_calls
        clock.advance(seconds=2)

    slack.raise_on_call = None
    clock.advance(seconds=60)
    assert ctx.notifier.dispatch(ctx.db, ctx.now()) == 3
    assert [text.splitlines()[0] for text in slack.texts[3:]] == ["[WARNING] A", "[WARNING] B", "[WARNING] C"]


def test_alerts_behind_one_waiting_for_its_retry_are_still_announced(client, ctx, mailer, clock, settings):
    # One alert whose email failed must not hold up the ones opened after it.
    settings.alert_email_to = "ops@example.test"
    mailer.fail_with = RuntimeError("smtp hiccup")
    mailer.fail_times = 1
    raise_one(ctx, key="a", title="A")
    assert ctx.notifier.dispatch(ctx.db, ctx.now()) == 0

    clock.advance(seconds=2)
    raise_one(ctx, key="b", title="B")
    assert ctx.notifier.dispatch(ctx.db, ctx.now()) == 1  # B goes out; A is not due for a retry yet
    assert [email.subject for email in mailer.sent] == ["[Angi integration] [WARNING] B"]

    clock.advance(seconds=60)
    assert ctx.notifier.dispatch(ctx.db, ctx.now()) == 1
    assert [email.subject for email in mailer.sent][-1] == "[Angi integration] [WARNING] A"


def test_escalation_to_critical_is_announced_again(client, ctx, clock, slack):
    # "Email is failing" (warning) became "we gave up on a lead's email" (critical): that
    # is news, even though the alert was already announced once.
    raise_one(ctx, severity="warning", title="Outbound email is failing")
    assert ctx.notifier.dispatch(ctx.db, ctx.now()) == 1
    clock.advance(minutes=10)
    raise_one(ctx, severity="critical", title="Outbound email is failing: gave up")

    assert alerts(ctx)[0]["notified_at"] is None
    assert ctx.notifier.dispatch(ctx.db, ctx.now()) == 1
    assert [text.splitlines()[0] for text in slack.texts] == [
        "[WARNING] Outbound email is failing",
        "[CRITICAL] Outbound email is failing: gave up",
    ]
    # A repeat at the same or a lower severity is not an escalation.
    clock.advance(minutes=1)
    raise_one(ctx, severity="warning", title="Outbound email is failing")
    raise_one(ctx, severity="critical", title="Outbound email is failing: gave up")
    assert ctx.notifier.dispatch(ctx.db, ctx.now()) == 0


def test_send_error_alert_escalation_is_announced_when_the_worker_gives_up(client, ctx, mailer, clock, slack):
    # The real path: the first failed attempt warns, giving up on the lead's email is critical.
    send(client, ads_payload())
    mailer.fail_with = RuntimeError("smtp is down")
    mailer.fail_times = -1
    clock.advance(seconds=1)
    ctx.worker.deliver_due()
    assert ctx.notifier.dispatch(ctx.db, ctx.now()) == 1

    fail_until_given_up(ctx, clock)
    assert ctx.notifier.dispatch(ctx.db, ctx.now()) == 1
    send_error_lines = [text.splitlines()[0] for text in slack.texts]
    assert send_error_lines[0].startswith("[WARNING] Outbound email is failing")
    assert send_error_lines[-1].startswith("[CRITICAL] Outbound email is failing")


def test_alert_that_keeps_recurring_is_announced_at_most_once_a_day(client, ctx, clock, slack):
    # Rejected requests every hour for two and a half days: three announcements, not sixty.
    announced_at = []
    for hour in range(60):
        clock.set(START + timedelta(hours=hour))
        raise_one(ctx, key="auth_failures")
        if ctx.notifier.dispatch(ctx.db, ctx.now()):
            announced_at.append(hour)

    assert announced_at == [0, 24, 48]
    assert [(a["status"], a["count"]) for a in alerts(ctx)] == [("open", 60)]


def test_open_alert_that_has_not_happened_again_is_not_announced_again(client, ctx, clock, slack):
    # The reminder is for problems that are still happening, not for old rows.
    raise_one(ctx)
    assert ctx.notifier.dispatch(ctx.db, ctx.now()) == 1
    clock.advance(days=5)  # (no monitor run here, so the quiet-day rule has not closed it)
    assert ctx.notifier.dispatch(ctx.db, ctx.now()) == 0
    assert len(slack.calls) == 1


def test_slack_text_escapes_markup_from_lead_controlled_text(client, ctx, clock, slack):
    # An account id or business name ends up in alert titles. In Slack, "<!channel>" pings
    # everyone and "<url|label>" is a disguised link, so &, < and > are escaped.
    body = send(
        client,
        ads_payload(ALAccountId="<!channel> <https://evil.example/login|Open the dashboard> & more"),
    )
    assert body["result"] == "quarantined"
    ctx.notifier.dispatch(ctx.db, ctx.now())

    text = slack.texts[0]
    assert "&lt;!channel&gt; &lt;https://evil.example/login|Open the dashboard&gt; &amp; more" in text
    assert "<" not in text and ">" not in text
    assert text.endswith(f"\n{BASE_URL}/dashboard/monitoring")  # our own link is left alone


def test_slack_webhook_url_is_never_written_to_the_log(client, ctx, clock, slack, caplog):
    # The webhook URL is a credential, and httpx puts the URL in its error messages.
    slack.raise_on_call = httpx.ConnectError(f"All connection attempts failed for url '{SLACK_URL}'")
    raise_one(ctx)
    with caplog.at_level("DEBUG"):
        ctx.notifier.dispatch(ctx.db, ctx.now())

    assert "could not post alert 1 to Slack (ConnectError)" in caplog.text
    assert SLACK_URL not in caplog.text
    assert "hooks.slack.test" not in caplog.text


def test_alert_log_line_stays_on_one_line_whatever_the_key_and_title_contain(client, ctx, clock, caplog):
    # Keys and titles carry upstream text; a line break in one must not forge a second log line.
    raise_one(
        ctx,
        key="unmapped_account:31337\n2026-11-12 15:00:01 INFO app: admin login ok",
        title="Lead quarantined: account 31337\n2026-11-12 15:00:01 INFO app: admin login ok",
    )
    with caplog.at_level("WARNING", logger="app.alerts"):
        ctx.notifier.dispatch(ctx.db, ctx.now())

    alert_lines = [record.getMessage() for record in caplog.records if record.name == "app.alerts"]
    assert len(alert_lines) == 1
    assert "\n" not in alert_lines[0]
    assert alerts(ctx)[0]["title"] == "Lead quarantined: account 31337 2026-11-12 15:00:01 INFO app: admin login ok"


def test_dispatch_announces_in_the_order_alerts_were_opened(client, ctx, clock, slack):
    # Oldest alert first, whatever its severity.
    raise_one(ctx, key="first", severity="info", title="first")
    raise_one(ctx, key="second", severity="critical", title="second")
    raise_one(ctx, key="third", severity="warning", title="third")
    ctx.notifier.dispatch(ctx.db, ctx.now())

    assert [text.splitlines()[0] for text in slack.texts] == ["[INFO] first", "[CRITICAL] second", "[WARNING] third"]


def test_dispatch_works_through_a_backlog_in_batches(client, ctx, clock, slack):
    # An alert storm is announced 25 at a time; nothing is dropped and nothing is repeated.
    for n in range(30):
        raise_one(ctx, key=f"storm:{n}", title=f"storm {n}")

    assert ctx.notifier.dispatch(ctx.db, ctx.now()) == 25
    assert ctx.notifier.dispatch(ctx.db, ctx.now()) == 5
    assert ctx.notifier.dispatch(ctx.db, ctx.now()) == 0
    assert len(slack.calls) == 30
    assert len(set(slack.texts)) == 30


def test_alert_email_for_a_title_containing_a_line_break_can_be_built(client, ctx, mailer, clock, settings):
    # Titles carry exception text and upstream data; the alert email must still be a sendable message.
    settings.alert_email_to = "ops@example.test"
    raise_one(ctx, severity="critical", title="Lead processing failed: ValidationError: 1 error\n  field required")
    ctx.notifier.dispatch(ctx.db, ctx.now())

    email = mailer.sent[0]  # the in-memory provider accepts anything; a real one builds a MIME message
    message = build_mime(email)
    assert "Lead processing failed" in message["Subject"]


def test_alert_email_subject_is_capped_in_length(client, ctx, mailer, clock, settings):
    # A very long title (e.g. a long drift description) must not produce an unbounded Subject.
    settings.alert_email_to = "ops@example.test"
    raise_one(ctx, title="x" * 500)
    ctx.notifier.dispatch(ctx.db, ctx.now())
    assert len(mailer.sent[0].subject) == 180


# --- the worker's maintenance pass ----------------------------------------------------------


def test_maintenance_pass_then_dispatch_announces_a_quarantined_lead(client, ctx, clock, slack):
    # End to end: an unmapped lead is quarantined, the monitor notices, and the operator is told.
    quarantine_one(client)
    clock.advance(seconds=30)

    ctx.worker.run_maintenance()
    announced = ctx.notifier.dispatch(ctx.db, ctx.now())

    assert announced == 2
    assert any(
        text.startswith(f"[CRITICAL] Lead quarantined: Angi account {UNMAPPED_ACCOUNT}") for text in slack.texts
    )
    assert any(
        text.startswith("[WARNING] 1 lead(s) are quarantined and waiting for action (1 unmapped_account)")
        for text in slack.texts
    )
    assert all(text.endswith(f"{BASE_URL}/dashboard/monitoring") for text in slack.texts)
    by_key = {a["dedupe_key"]: a for a in alerts(ctx)}
    assert set(by_key) == {f"unmapped_account:{UNMAPPED_ACCOUNT}", "check:quarantine"}
    assert all(a["notified_at"] == iso(START + timedelta(seconds=30)) for a in by_key.values())
    assert db_one(ctx, "SELECT value FROM kv WHERE key = 'monitor:last_run'") == {
        "value": iso(START + timedelta(seconds=30))
    }


def test_repeated_maintenance_passes_do_not_re_announce(client, ctx, clock, slack):
    # The monitor runs every minute; the same quarantine must be announced once.
    quarantine_one(client)
    ctx.worker.run_maintenance()
    ctx.notifier.dispatch(ctx.db, ctx.now())
    first_round = len(slack.calls)

    for _ in range(5):
        clock.advance(minutes=1)
        ctx.worker.run_maintenance()
        assert ctx.notifier.dispatch(ctx.db, ctx.now()) == 0
    assert len(slack.calls) == first_round


def test_maintenance_pass_on_a_healthy_system_announces_nothing(client, ctx, mailer, clock, slack):
    # A normal lead, answered in time, produces no alert at all.
    send(client, ads_payload())
    clock.advance(seconds=1)
    ctx.worker.deliver_due()
    clock.advance(minutes=1)
    ctx.worker.run_maintenance()

    assert ctx.notifier.dispatch(ctx.db, ctx.now()) == 0
    assert slack.calls == []
    assert alerts(ctx) == []


def test_maintenance_pass_announces_an_email_backlog_and_resolves_it_after_delivery(client, ctx, mailer, clock, slack):
    # Speed-to-lead SLA breached -> critical alert; once the email is out the alert closes itself.
    send(client, ads_payload())
    clock.advance(minutes=5)  # the worker has not delivered anything for five minutes
    ctx.worker.run_maintenance()
    assert ctx.notifier.dispatch(ctx.db, ctx.now()) == 1
    assert slack.texts[0].startswith("[CRITICAL] 1 email(s) have been waiting longer than the 120s speed-to-lead SLA")

    ctx.worker.deliver_due()
    clock.advance(minutes=1)
    ctx.worker.run_maintenance()
    assert [a["status"] for a in alerts(ctx, "check:email_backlog")] == ["resolved"]
