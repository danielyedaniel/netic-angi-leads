"""Regression tests for the final review's fixes on the operator's side of the service:
what the dashboard pages and their forms, the admin API, the alerts, the access log, the
start-up warnings and the canary script show or say.

Leads arrive the way Angi sends them (through the webhook), the worker and the monitor
are run by hand, and time only moves when a test moves the clock. The only things faked
are external edges: Slack's HTTP call, and a mail provider that declares it really
delivers. Each section names the review finding it belongs to.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime

import httpx
import pytest

from app import monitoring
from app.config import Settings
from app.mailer.providers import PermanentEmailError
from app.main import check_configuration, redact_url
from app.routes import dashboard
from app.tenants import add_mapping
from scripts import canary
from tests.conftest import (
    ADMIN,
    ADMIN_TOKEN,
    ANGI_ADS_EXAMPLE,
    ads_payload,
    db_one,
    db_rows,
    leads_payload,
    post_lead,
)

SLACK_URL = "https://hooks.slack.test/services/T000/B000/SECRET-TOKEN"
UNMAPPED_ACCOUNT = "777777"
WEBHOOK_KEY = "dfrKnBiOSkBXLW09teBkskyk"  # the shape of a tenant's webhook key
LINK_TOKEN = "Zm9vYmFyYmF6cXV4"  # the shape of a booking / unsubscribe token
RESERVED_DOMAIN = "address is on a reserved example domain; nothing was sent"
ANGI_EXAMPLE_ADDRESS = "address is the one printed in Angi's example payload; nothing was sent"


# --- helpers -------------------------------------------------------------------------------


def get(client, path: str):
    return client.get(path, headers=ADMIN)


def post(client, path: str, data: dict[str, str] | None = None):
    """Submit a dashboard form the way a browser does, without following the redirect."""
    return client.post(path, data=data or {}, headers=ADMIN, follow_redirects=False)


def deliver(ctx, clock, seconds: float = 1) -> None:
    """Let ``seconds`` pass, then run one pass of the email worker."""
    clock.advance(seconds=seconds)
    ctx.worker.deliver_due()


def tile(html: str, label: str) -> str:
    """The big number on the stat tile with this label."""
    match = re.search(
        rf'<div class="tile-label">{re.escape(label)}</div>\s*<div class="tile-value">([^<]*)</div>', html
    )
    assert match, f"no tile labelled {label!r}"
    return match.group(1).strip()


def summary(client) -> dict:
    response = client.get("/admin/api/analytics/summary", headers=ADMIN)
    assert response.status_code == 200
    return response.json()


def quarantine_one(client, n: int = 1) -> str:
    """Post a lead for an Angi account no tenant is mapped to. -> the id of the stored event"""
    body = post_lead(
        client,
        ads_payload(ALAccountId=UNMAPPED_ACCOUNT, Email=f"waiting{n}@example.com", PhoneNumber=f"31755501{n:02d}"),
    ).json()
    assert (body["result"], body["reason"]) == ("quarantined", "unmapped_account")
    return body["eventId"]


def monitor_pass(ctx) -> int:
    """What the worker does once a minute: run the checks, then announce what is due."""
    monitoring.run_checks(ctx.db, ctx.settings, ctx.now())
    return ctx.notifier.dispatch(ctx.db, ctx.now())


class FakeSlack:
    """Stands in for httpx.post to Slack: can be made unreachable, and remembers the first
    line of every message that got through."""

    def __init__(self) -> None:
        self.reachable = True
        self.delivered: list[str] = []

    def __call__(self, url, **kwargs):
        if not self.reachable:
            raise httpx.ConnectError("slack is unreachable")
        self.delivered.append(kwargs["json"]["text"].splitlines()[0])
        return httpx.Response(200, request=httpx.Request("POST", url))


@pytest.fixture
def slack(monkeypatch, settings) -> FakeSlack:
    """Slack configured as an alert channel, with the outbound HTTP call faked."""
    fake = FakeSlack()
    monkeypatch.setattr(httpx, "post", fake)
    settings.alert_slack_webhook_url = SLACK_URL
    return fake


@pytest.fixture
def real_mail(mailer, monkeypatch):
    """The in-memory provider, declaring what SMTP and Resend declare: whatever it accepts
    is really delivered."""
    monkeypatch.setattr(mailer, "delivers_externally", True, raising=False)
    return mailer


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    """No developer .env and no exported settings: a script sees only its command line."""
    monkeypatch.chdir(tmp_path)
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)


# --- an alert nobody could be told about (unannounced-alert-swallowed-by-auto-resolve) --------


def test_alert_that_no_channel_has_accepted_is_not_closed_by_the_quiet_day_rule(client, ctx, clock, slack):
    # Rule: an event alert is closed after a quiet day only once it has been announced.
    slack.reachable = False
    quarantine_one(client)  # raises the critical "account not mapped" alert
    assert monitor_pass(ctx) == 0

    clock.advance(hours=25)
    assert monitor_pass(ctx) == 0  # the monitor's next pass, Slack still down

    alert = db_one(
        ctx, "SELECT status, notified_at FROM alerts WHERE dedupe_key = ?", (f"unmapped_account:{UNMAPPED_ACCOUNT}",)
    )
    assert alert == {"status": "open", "notified_at": None}


def test_alert_raised_during_a_slack_outage_longer_than_a_day_is_announced_when_slack_is_back(
    client, ctx, clock, slack
):
    # Rule: a Slack outage delays an alert; it never swallows it.
    slack.reachable = False
    quarantine_one(client)
    monitor_pass(ctx)
    clock.advance(hours=25)
    monitor_pass(ctx)

    slack.reachable = True
    clock.advance(minutes=1)
    monitor_pass(ctx)

    assert len(slack.delivered) == 2  # the lead's own alert, then the quarantine backlog check
    assert slack.delivered[0].startswith(f"[CRITICAL] Lead quarantined: Angi account {UNMAPPED_ACCOUNT}")
    assert slack.delivered[1].startswith("[WARNING] 1 lead(s) are quarantined and waiting for action")


# --- lead search (long-search-term-500) ----------------------------------------------------


@pytest.mark.parametrize("length", [49_999, 50_000])
def test_admin_lead_search_with_a_term_too_long_for_sqlite_is_answered(client, length):
    # Rule: no search term surfaces as a server error. (SQLite refuses a LIKE pattern over
    # 50,000 bytes; the pattern is the term plus a wildcard at each end.)
    post_lead(client, ads_payload())

    response = client.get("/admin/api/leads", params={"q": "a" * length}, headers=ADMIN)

    assert response.status_code == 200
    assert response.json() == {"count": 0, "leads": []}


def test_dashboard_lead_search_with_a_50000_character_term_shows_the_page(client):
    # Rule: the same over-long term pasted into the dashboard's search box gets the page.
    post_lead(client, ads_payload())

    response = client.get("/dashboard/leads", params={"q": "a" * 50_000}, headers=ADMIN)

    assert response.status_code == 200
    assert "No leads match." in response.text


# --- money (credit-on-unpriced-lead-negative-spend) --------------------------------------------


def credit_on_an_unpriced_duplicate(client, ctx, clock) -> None:
    """One homeowner, three leads from an Angi Leads account whose leads carry no fee and
    that has no default fee, so the two duplicates' claims have no amount. Angi's credit
    of $500 is recorded on one of the claims, and the first lead's job is won for $400."""
    lead_ids = []
    for _ in range(3):
        result = post_lead(client, leads_payload(fee=None)).json()
        assert result["result"] == "processed"
        lead_ids.append(result["leadId"])
        clock.advance(hours=1)
    claims = db_rows(ctx, "SELECT id, amount_cents FROM credit_claims ORDER BY created_at")
    assert [claim["amount_cents"] for claim in claims] == [None, None]
    credited = client.post(
        f"/admin/api/claims/{claims[0]['id']}/status", json={"status": "credited", "credited": 500}, headers=ADMIN
    )
    assert credited.status_code == 200
    won = client.post(
        f"/admin/api/leads/{lead_ids[0]}/outcome", json={"outcome": "won", "job_value": 400}, headers=ADMIN
    )
    assert won.status_code == 200


def test_credit_on_a_lead_whose_fee_is_unknown_does_not_make_net_spend_negative(client, ctx, clock):
    # Rule: net spend never goes below zero, so no cost-per figure and no return on spend is negative.
    credit_on_an_unpriced_duplicate(client, ctx, clock)

    assert summary(client)["money"] == {
        "spend_cents": 0,
        "unpriced_leads": 3,  # still tells the reader that spend is understated
        "credited_cents": 50000,
        "net_spend_cents": 0,
        "cost_per_lead_cents": 0,
        "cost_per_booking_cents": 0,
        "cost_per_won_job_cents": 0,
        "revenue_cents": 40000,
        "return_on_spend": None,
    }


def test_credit_on_an_unpriced_lead_does_not_make_a_breakdown_row_negative(client, ctx, clock):
    # Rule: the rows per service, tenant, ZIP and feed use the same net spend as the headline.
    credit_on_an_unpriced_duplicate(client, ctx, clock)

    result = summary(client)

    for breakdown in ("by_task", "by_tenant", "by_postal_code", "by_format"):
        (row,) = result[breakdown]
        assert (row["net_spend_cents"], row["cost_per_booking_cents"]) == (0, 0), breakdown


def test_overview_shows_no_negative_amount_after_a_credit_on_an_unpriced_lead(client, ctx, clock):
    # Rule: what the operator reads on the Overview follows: a cost can be $0.00, never minus $500.
    credit_on_an_unpriced_duplicate(client, ctx, clock)

    html = get(client, "/dashboard").text

    assert tile(html, "Cost per booking") == "$0.00"
    assert "-$" not in html


# --- leads-per-day chart (overview-chart-utc-last-day) ----------------------------------------


def chart_days(html: str) -> list[str]:
    """The hover text of every column of the leads-per-day chart, oldest day first."""
    chart = html[html.index("<h2>Leads per day</h2>"):html.index("<h2>By service</h2>")]
    return re.findall(r"<title>([^<]+)</title>", chart)


def test_overview_chart_ends_on_the_tenants_local_day_not_on_the_utc_date(client, clock):
    # Rule: the chart's days are the tenants' local days, so its last column is their "today";
    # in the US evening the UTC date is already tomorrow.
    clock.set(datetime(2026, 11, 13, 1, 0, tzinfo=UTC))  # 8 PM on Thursday Nov 12 in Indianapolis
    post_lead(client, ads_payload())

    days = chart_days(get(client, "/dashboard?days=7").text)

    assert days[-1] == "Thu Nov 12: 1 received, 0 booked"
    assert len(days) == 7 and days[0] == "Fri Nov 6: 0 received, 0 booked"


def test_overview_chart_for_one_tenant_ends_on_that_tenants_local_day(client, clock):
    # Rule: "today" is taken from the tenants shown: the selected tenant's, or with all tenants
    # the latest local date among them.
    clock.set(datetime(2026, 11, 13, 6, 30, tzinfo=UTC))  # 01:30 Friday in Indianapolis, 23:30 Thursday in Denver
    post_lead(client, leads_payload())  # for Summit Heating & Air, in Denver

    summit = chart_days(get(client, "/dashboard?days=7&tenant=tnt_summit").text)
    everyone = chart_days(get(client, "/dashboard?days=7").text)

    assert summit[-1] == "Thu Nov 12: 1 received, 0 booked"
    assert everyone[-2:] == ["Thu Nov 12: 1 received, 0 booked", "Fri Nov 13: 0 received, 0 booked"]


# --- access log (access-log-leak-malformed-paths) ----------------------------------------------


@pytest.mark.parametrize(
    ("path", "logged"),
    [
        (f"/webhooks/angi/leads//{WEBHOOK_KEY}", "/webhooks/angi/leads//[redacted]"),
        (f"/WEBHOOKS/angi/leads/{WEBHOOK_KEY}", "/WEBHOOKS/angi/leads/[redacted]"),
        (f"/Webhooks/Angi/Leads///{WEBHOOK_KEY}?attempt=2", "/Webhooks/Angi/Leads///[redacted]?[redacted]"),
        (f"/book//{LINK_TOKEN}", "/book//[redacted]"),
        (f"/BOOK/{LINK_TOKEN}", "/BOOK/[redacted]"),
        (f"/unsubscribe//{LINK_TOKEN}", "/unsubscribe//[redacted]"),
        (f"/Unsubscribe/{LINK_TOKEN}", "/Unsubscribe/[redacted]"),
    ],
)
def test_redact_url_hides_the_secret_in_double_slash_and_wrong_case_urls(path, logged):
    # Rule: a mistyped URL carries the same credential as the working one, so it is redacted too.
    assert redact_url(path) == logged


def test_access_log_line_for_a_webhook_url_with_a_doubled_slash_does_not_show_the_key(client, ctx, caplog):
    # Rule: the 404 that a base URL registered with a trailing slash earns (and that Angi retries)
    # is logged without the tenant's webhook key.
    key = db_one(ctx, "SELECT webhook_key FROM tenants WHERE id = 'tnt_sparkle'")["webhook_key"]
    access_log = logging.getLogger("uvicorn.access")
    with caplog.at_level(logging.INFO, logger="uvicorn.access"):
        # The arguments uvicorn passes: client address, method, path with query, HTTP version, status.
        access_log.info(
            '%s - "%s %s HTTP/%s" %d', "203.0.113.9:51234", "POST", f"/webhooks/angi/leads//{key}", "1.1", 404
        )

    lines = [record.getMessage() for record in caplog.records if record.name == "uvicorn.access"]
    assert lines == ['203.0.113.9:51234 - "POST /webhooks/angi/leads//[redacted] HTTP/1.1" 404']
    assert key not in caplog.text


# --- start-up warning (production-console-provider-silent) -------------------------------------


def console_warnings(caplog) -> list[str]:
    """What start-up logged, at warning level, about EMAIL_PROVIDER=console."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "app" and record.levelno == logging.WARNING and "EMAIL_PROVIDER=console" in record.getMessage()
    ]


def test_console_provider_is_warned_about_once_at_start_in_production_as_in_development(settings, caplog):
    # Rule: EMAIL_PROVIDER=console (the default) is said out loud whatever the environment: with it
    # every lead is marked contacted while no consumer receives anything.
    for app_env in ("production", "development"):
        configured = Settings(_env_file=None, **{**settings.model_dump(), "app_env": app_env})
        assert configured.email_provider == "console"
        caplog.clear()

        with caplog.at_level(logging.WARNING, logger="app"):
            check_configuration(configured)

        assert console_warnings(caplog) == [
            "EMAIL_PROVIDER=console: emails are written to data/outbox/ instead of being sent"
        ], app_env


def test_console_provider_warning_does_not_depend_on_how_the_setting_is_capitalised(settings, caplog):
    # Rule: "Console" selects the console provider just as "console" does, so it is warned about too.
    capitalised = Settings(_env_file=None, **{**settings.model_dump(), "email_provider": "Console"})

    with caplog.at_level(logging.WARNING, logger="app"):
        check_configuration(capitalised)

    assert len(console_warnings(caplog)) == 1


# --- the canary and Slack (canary-prints-slack-webhook-url) -------------------------------------


def test_canary_does_not_print_the_slack_webhook_url_when_slack_refuses_the_post(
    client, clean_env, capsys, monkeypatch
):
    # Rule: the Slack webhook URL is a secret, and httpx puts the request URL in its error text;
    # only the kind of error is printed, on one line (cron appends stderr to the canary's log).
    def slack_answers_404(url, **kwargs):
        return httpx.Response(404, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", slack_answers_404)

    code = canary.main(
        ["--email", "probe@example.com", "--api-key", "stale-key", "--admin-token", ADMIN_TOKEN,
         "--url", "http://testserver", "--slack-webhook", SLACK_URL],
        client=client,
    )

    captured = capsys.readouterr()
    assert code == 1  # the probe failed (a stale key), which is why Slack was tried at all
    assert "hooks.slack.test" not in captured.out + captured.err
    assert "SECRET-TOKEN" not in captured.out + captured.err
    assert captured.err.splitlines() == ["could not post to Slack (HTTPStatusError)"]


# --- credit claims that are closed (credits-countdown-on-closed-claims) -------------------------

DEADLINE = "2026-12-12 20:00"  # 30 days after the duplicate below was charged


def duplicate_claim(client, ctx, clock) -> str:
    """The same homeowner charged twice through Sparkle's Angi account, five hours apart,
    both leads emailed. -> the id of the credit claim opened for the second lead"""
    consumer = {"Email": "twice@example.com", "PhoneNumber": "3175550101"}
    post_lead(client, ads_payload(**consumer))
    deliver(ctx, clock)
    clock.advance(hours=5)
    duplicate = post_lead(client, ads_payload(**consumer)).json()
    deliver(ctx, clock)
    return duplicate["duplicate"]["creditClaimId"]


def claim_row(client, claim_id: str) -> dict[str, str]:
    """A claim's row of facts on the Credits page: column heading -> the cell's HTML, with
    single spaces."""
    html = get(client, "/dashboard/credits").text
    table = html[html.index("<h2>Claims, soonest deadline first</h2>"):]
    headings = re.findall(r"<th[^>]*>(.*?)</th>", table[:table.index("</thead>")])
    block = table[table.index(f'<tbody id="claim-{claim_id}"'):]
    cells = re.findall(r"<td[^>]*>(.*?)</td>", block[:block.index("</tr>")], re.DOTALL)
    return dict(zip(headings, (" ".join(cell.split()) for cell in cells), strict=True))


@pytest.mark.parametrize("status", ["credited", "denied", "dismissed"])
def test_closed_claim_shows_its_deadline_without_a_countdown_or_eligibility_advice(client, ctx, clock, status):
    # Rule: the countdown and the eligibility check are advice for filing. A claim Angi has answered,
    # or that was dismissed, shows the deadline date and a dash, before that date and after it.
    claim_id = duplicate_claim(client, ctx, clock)
    post(client, f"/dashboard/credits/{claim_id}/status", {"status": status})

    for days_later in (0, 40):
        clock.advance(days=days_later)
        row = claim_row(client, claim_id)
        assert (row["Request by (UTC)"], row["Eligibility"]) == (DEADLINE, '<span class="muted">—</span>'), days_later


def test_submitted_claim_keeps_its_eligibility_but_has_no_countdown(client, ctx, clock):
    # Rule: once a claim is filed its deadline has been met, so nothing counts down; until Angi
    # answers, what Angi will check still matters and stays on the row.
    claim_id = duplicate_claim(client, ctx, clock)
    post(client, f"/dashboard/credits/{claim_id}/status", {"status": "submitted"})

    row = claim_row(client, claim_id)

    assert row["Request by (UTC)"] == DEADLINE
    assert row["Eligibility"].startswith('<span class="badge good">eligible</span>')
    assert "call the homeowner" in row["Eligibility"]


# --- speed to lead in milliseconds (speed-to-lead-0-0s) -----------------------------------------


@pytest.mark.parametrize(("seconds", "text"), [(0.006, "6 ms"), (0.012, "12 ms"), (0.0951, "95 ms"), (0.0004, "1 ms")])
def test_duration_under_a_tenth_of_a_second_is_given_in_milliseconds(seconds, text):
    # Rule: a wait too short to show as "0.1 s" is given in milliseconds (at least 1), never as
    # "0.0 s", which reads as "not measured".
    assert dashboard.format_duration(seconds) == text


def test_lead_answered_within_milliseconds_does_not_show_a_speed_to_lead_of_zero(client, ctx, clock):
    # Rule: with the console provider the first email leaves milliseconds after the lead arrived;
    # the lead list and the lead's own page say how many.
    lead_id = post_lead(client, ads_payload()).json()["leadId"]
    clock.advance(milliseconds=6)
    ctx.worker.deliver_due()

    assert '<td class="num">6 ms</td>' in get(client, "/dashboard/leads").text
    assert "(6 ms after it arrived)" in get(client, f"/dashboard/leads/{lead_id}").text


# --- the "return to" address of a form (next-path-traversal) -------------------------------------


def outcome_redirect(client, lead_id: str, next_url: str) -> str:
    """Record an outcome on the lead's page with this "return to" address. -> where the
    browser is sent afterwards"""
    response = post(client, f"/dashboard/leads/{lead_id}/outcome", {"outcome": "lost", "next": next_url})
    assert response.status_code == 303
    return response.headers["location"]


@pytest.mark.parametrize(
    "next_url",
    [
        "/dashboard/leads/../../book/x",
        "/dashboard/..",
        "/dashboard/../admin/api/leads",
        "/dashboard/leads/%2e%2e/%2E%2E/book/x",  # the dots percent-encoded, which a browser resolves too
        "/dashboard/leads/..%2f..%2fbook/x",  # the slashes percent-encoded
        "/dashboard/leads\\..\\..\\book\\x",  # backslashes for slashes
    ],
)
def test_return_address_with_a_parent_directory_segment_is_not_used_as_the_redirect(client, next_url):
    # Rule: a browser resolves ".." before it follows a redirect, which would lead out of the
    # dashboard, so such a "return to" address is ignored and the form's default page is used.
    lead_id = post_lead(client, ads_payload()).json()["leadId"]

    assert outcome_redirect(client, lead_id, next_url) == f"/dashboard/leads/{lead_id}?msg=outcome_saved"


def test_only_a_whole_dot_dot_segment_is_refused_in_a_return_address(client):
    # Rule: ".." counts as a path segment of its own; two dots inside a name are an ordinary page.
    lead_id = post_lead(client, ads_payload()).json()["leadId"]

    assert outcome_redirect(client, lead_id, "/dashboard/leads/x/..") == f"/dashboard/leads/{lead_id}?msg=outcome_saved"
    assert outcome_redirect(client, lead_id, "/dashboard/leads/x..y") == "/dashboard/leads/x..y?msg=outcome_saved"


# --- Retry on an address that is only an example (retry-flash-on-reserved-address) ---------------


def intro(ctx, lead_id: str) -> dict:
    return db_one(ctx, "SELECT * FROM outbox_messages WHERE lead_id = ? AND template = 'intro'", (lead_id,))


def failures_on_record(ctx, lead_id: str) -> int:
    """How many times the lead's timeline says "Email failed for good"."""
    return db_one(
        ctx, "SELECT COUNT(*) AS n FROM lead_events WHERE lead_id = ? AND type = 'email.failed'", (lead_id,)
    )["n"]


def test_retry_of_an_email_to_a_reserved_example_address_is_refused_with_a_message(client, ctx, clock, real_mail):
    # Rule: Retry never says "queued again, sent within a few seconds" for an address the worker
    # refuses again at once. It says why, and the message and the timeline stay as they were.
    lead_id = post_lead(client, ads_payload(Email="pat.doe@example.com")).json()["leadId"]
    deliver(ctx, clock)
    message = intro(ctx, lead_id)
    assert (message["status"], message["last_error"]) == ("failed", RESERVED_DOMAIN)
    path = f"/dashboard/leads/{lead_id}"

    response = post(client, f"/dashboard/messages/{message['id']}/retry", {"next": path})

    assert response.status_code == 303
    assert response.headers["location"] == f"{path}?msg=message_reserved_address"
    page = get(client, response.headers["location"]).text
    assert "it is never handed to the mail server; nothing was queued." in page
    assert "Email queued again" not in page
    deliver(ctx, clock)
    assert intro(ctx, lead_id)["status"] == "failed" and failures_on_record(ctx, lead_id) == 1
    assert real_mail.sent == []


def test_retry_of_an_email_to_the_address_in_angis_example_payload_is_refused_too(client, ctx, clock, real_mail):
    # Rule: the address printed in Angi's own example is held back like an example.com one (it may
    # be a stranger's mailbox), and Retry on it is refused the same way.
    lead_id = post_lead(client, ANGI_ADS_EXAMPLE).json()["leadId"]  # Angi's example, posted as printed
    deliver(ctx, clock)
    message = intro(ctx, lead_id)
    assert (message["to_address"], message["last_error"]) == ("bob.builder@gmail.com", ANGI_EXAMPLE_ADDRESS)

    response = post(client, f"/dashboard/messages/{message['id']}/retry")

    assert response.headers["location"] == f"/dashboard/leads/{lead_id}?msg=message_reserved_address"
    assert "the one in Angi&#39;s documentation" in get(client, response.headers["location"]).text
    deliver(ctx, clock)
    assert intro(ctx, lead_id)["status"] == "failed" and failures_on_record(ctx, lead_id) == 1
    assert real_mail.sent == []


def test_retry_goes_by_where_the_email_would_really_be_sent_when_everything_is_redirected(
    client, ctx, clock, real_mail, settings
):
    # Rule: with EMAIL_REDIRECT_ALL_TO set, the redirect address decides, as it does in the worker.
    # Redirected to an example address, a real lead's Retry is refused; redirected to a real
    # mailbox, an example.com lead's Retry is queued and sent.
    settings.email_redirect_all_to = "dev@example.com"
    real_lead = post_lead(client, ads_payload(Email="pat.lee@gmail.com", PhoneNumber="3175552000")).json()["leadId"]
    deliver(ctx, clock)
    assert intro(ctx, real_lead)["last_error"] == RESERVED_DOMAIN
    refused = post(client, f"/dashboard/messages/{intro(ctx, real_lead)['id']}/retry")
    assert refused.headers["location"] == f"/dashboard/leads/{real_lead}?msg=message_reserved_address"

    settings.email_redirect_all_to = "presenter@gmail.com"
    real_mail.fail_with, real_mail.fail_times = PermanentEmailError("550 mailbox unavailable"), 1
    demo_lead = post_lead(client, ads_payload(Email="demo@example.com", PhoneNumber="3175552001")).json()["leadId"]
    deliver(ctx, clock)
    assert intro(ctx, demo_lead)["last_error"] == "550 mailbox unavailable"
    queued = post(client, f"/dashboard/messages/{intro(ctx, demo_lead)['id']}/retry")
    assert queued.headers["location"] == f"/dashboard/leads/{demo_lead}?msg=message_requeued"
    deliver(ctx, clock)
    assert [(email.to, email.subject.split("]")[0]) for email in real_mail.sent] == [
        ("presenter@gmail.com", "[for demo@example.com")
    ]


# --- the quarantine backlog alert after an operator's action (stale-quarantine-alert-after-operator-action) ---


def backlog_alerts(ctx) -> list[tuple[str, str]]:
    """(status, title) of every alert the quarantine backlog check has opened, oldest first."""
    rows = db_rows(ctx, "SELECT status, title FROM alerts WHERE dedupe_key = 'check:quarantine' ORDER BY id")
    return [(row["status"], row["title"]) for row in rows]


def waiting_lead_with_its_backlog_alert(client, ctx) -> str:
    """One lead waits in quarantine for an unmapped account, and the monitor has opened the
    backlog alert about it. -> the id of the waiting event"""
    event_id = quarantine_one(client)
    monitoring.run_checks(ctx.db, ctx.settings, ctx.now())
    assert backlog_alerts(ctx) == [("open", "1 lead(s) are quarantined and waiting for action (1 unmapped_account)")]
    return event_id


def map_the_account_without_replaying(ctx) -> None:
    """The mapping appears behind the dashboard's back, so the lead is still waiting."""
    with ctx.db.transaction() as conn:
        add_mapping(conn, tenant_id="tnt_blueline", source="angi", external_account_id=UNMAPPED_ACCOUNT,
                    external_account_name=None, now=ctx.now())


def open_alerts_card(html: str) -> str:
    """The "Open alerts" card of the Monitoring page, without its "Recently resolved" list."""
    card = html[html.index("<h2>Open alerts</h2>"):html.index("<h2>Payload shapes seen</h2>")]
    return card.split("Recently resolved")[0]


def test_monitoring_page_lists_no_backlog_alert_right_after_the_mapping_released_the_lead(client, ctx):
    # Rule: an operator action that empties the quarantine closes the backlog alert in the same
    # breath, not at the monitor's next pass: the page never shows "nothing in quarantine" above an
    # open "1 lead(s) are quarantined" alert.
    waiting_lead_with_its_backlog_alert(client, ctx)

    post(client, "/dashboard/tenants/tnt_blueline/lead-sources", {"external_account_id": UNMAPPED_ACCOUNT})
    html = get(client, "/dashboard/monitoring").text

    assert "nothing in quarantine" in html
    assert "are quarantined and waiting for action" not in open_alerts_card(html)
    assert "No open alerts." in open_alerts_card(html)


def test_discarding_the_last_quarantined_event_closes_the_backlog_alert_at_once(client, ctx):
    # Rule: Discard brings the alerts in line with the checks straight away.
    event_id = waiting_lead_with_its_backlog_alert(client, ctx)

    post(client, f"/dashboard/events/{event_id}/discard")

    assert [status for status, _ in backlog_alerts(ctx)] == ["resolved"]


def test_replaying_the_last_quarantined_event_into_a_lead_closes_the_backlog_alert_at_once(client, ctx):
    # Rule: so does Replay on one event.
    event_id = waiting_lead_with_its_backlog_alert(client, ctx)
    map_the_account_without_replaying(ctx)

    response = post(client, f"/dashboard/events/{event_id}/replay")

    assert response.headers["location"] == "/dashboard/monitoring?msg=replay_processed"
    assert [status for status, _ in backlog_alerts(ctx)] == ["resolved"]


def test_replay_all_quarantined_closes_the_backlog_alert_at_once(client, ctx):
    # Rule: and so does "Replay all quarantined".
    waiting_lead_with_its_backlog_alert(client, ctx)
    map_the_account_without_replaying(ctx)

    response = post(client, "/dashboard/events/replay-quarantined")

    assert response.headers["location"] == "/dashboard/monitoring?msg=replayed_all&n=1&ok=1"
    assert [status for status, _ in backlog_alerts(ctx)] == ["resolved"]


def test_assigning_a_tenant_to_the_last_quarantined_event_closes_the_backlog_alert_at_once(client, ctx):
    # Rule: and so does assigning a tenant to a lead whose payload names no Angi account.
    payload = ads_payload(Email="nora@example.com", PhoneNumber="3175550133")
    del payload["ALAccountId"]
    event_id = post_lead(client, payload).json()["eventId"]
    monitoring.run_checks(ctx.db, ctx.settings, ctx.now())
    assert backlog_alerts(ctx) == [("open", "1 lead(s) are quarantined and waiting for action (1 no_account_id)")]

    response = post(client, f"/dashboard/events/{event_id}/assign", {"tenant_id": "tnt_sparkle"})

    assert response.headers["location"] == "/dashboard/monitoring?msg=replay_processed"
    assert [status for status, _ in backlog_alerts(ctx)] == ["resolved"]


def test_backlog_alert_stays_open_and_counts_what_is_left_while_another_lead_still_waits(client, ctx):
    # Rule: the alert follows the check in both directions. With one of two waiting leads discarded
    # it stays open and says that one is waiting, not two.
    first = quarantine_one(client, 1)
    quarantine_one(client, 2)
    monitoring.run_checks(ctx.db, ctx.settings, ctx.now())
    assert backlog_alerts(ctx) == [("open", "2 lead(s) are quarantined and waiting for action (2 unmapped_account)")]

    post(client, f"/dashboard/events/{first}/discard")

    assert backlog_alerts(ctx) == [("open", "1 lead(s) are quarantined and waiting for action (1 unmapped_account)")]


# --- "By service" on the Overview (by-service-top-12-unlabelled) ---------------------------------


def by_service(client) -> str:
    html = get(client, "/dashboard").text
    return html[html.index("<h2>By service</h2>"):html.index("<h2>By tenant</h2>")]


def lead_for_service(client, n: int) -> None:
    result = post_lead(
        client,
        ads_payload(Category=f"Indianapolis – Service {n:02d}", Email=f"service{n}@example.com",
                    PhoneNumber=f"31755502{n:02d}"),
    ).json()
    assert result["result"] == "processed"


def test_by_service_says_how_many_leads_its_twelve_rows_leave_out(client):
    # Rule: "By service" lists the 12 services with the most leads. When that leaves leads out the
    # card says how many, so its columns are not read as the totals; when every service is listed
    # it says nothing.
    for n in range(1, 13):
        lead_for_service(client, n)
    assert "not listed" not in by_service(client)  # twelve services: all of them are in the table

    lead_for_service(client, 13)
    lead_for_service(client, 14)

    card = by_service(client)
    assert card.count("<td>Service ") == 12
    assert "The 12 services with the most leads; 2 lead(s) for other services are not listed." in card


# --- the note about leads without a fee (overview-fee-note-contradicts-backfill) -----------------


def fee_note(client) -> str:
    """The note under the Overview's money tiles about leads without a price, as plain
    text ("" when it is not shown)."""
    html = get(client, "/dashboard").text
    match = re.search(r'<p class="muted small" role="note">(.*?)</p>', html, re.DOTALL)
    return " ".join(re.sub(r"<[^>]+>", "", match.group(1)).split()) if match else ""


def test_overview_fee_note_says_a_default_fee_also_prices_the_leads_already_received(client, ctx):
    # Rule: the note says what setting a default fee does, and that is what happens: the account's
    # unpriced leads are priced straight away, not only the leads that arrive from then on.
    webhook_key = db_one(ctx, "SELECT webhook_key FROM tenants WHERE id = 'tnt_blueline'")["webhook_key"]
    for n in range(2):  # a new Angi account, learned from the tenant's own URL: no default fee yet
        post_lead(client, ads_payload(ALAccountId="31337", Email=f"free{n}@example.com", PhoneNumber=f"512555014{n}"),
                  headers={}, path=f"/webhooks/angi/leads/{webhook_key}")

    note = fee_note(client)
    assert note.startswith("2 lead(s) in this period have no fee")
    assert "it is applied to that account's unpriced leads straight away and to new ones." in note
    assert "from then on" not in note

    post(client, "/dashboard/tenants/tnt_blueline/lead-sources", {"external_account_id": "31337", "default_fee": "39"})
    assert fee_note(client) == ""
    assert tile(get(client, "/dashboard").text, "Spend on leads") == "$78.00"


def test_overview_fee_note_says_a_lead_assigned_by_hand_stays_unpriced(client, ctx):
    # Rule: a lead an operator assigned to a tenant has no Angi account, so no account's default fee
    # reaches it; the note says so instead of recommending a step that cannot work for it.
    payload = ads_payload(Email="tessa@example.com", PhoneNumber="3035550144")
    del payload["ALAccountId"]
    event_id = post_lead(client, payload).json()["eventId"]
    post(client, f"/dashboard/events/{event_id}/assign", {"tenant_id": "tnt_summit"})
    # The step the note recommends, taken for the tenant's own Angi account: it cannot reach this lead.
    post(client, "/dashboard/tenants/tnt_summit/lead-sources", {"external_account_id": "200001", "default_fee": "45"})

    note = fee_note(client)

    assert note.startswith("1 lead(s) in this period have no fee")
    assert note.endswith("A lead that was assigned to a tenant by hand has no Angi account, so it stays unpriced.")
