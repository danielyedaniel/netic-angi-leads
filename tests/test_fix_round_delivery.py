"""Regression tests for the delivery fixes of the final review round: what is sent, to
whom and when, and what the operator is told while sending fails.

One section per finding id. Every test fails on the code as it was before the fix; a
rule that already held (an ordinary lead, an ordinary address) only appears as the
contrast inside the test for the rule that changed.

The worker thread is not running: each test moves the fake clock and calls the worker
itself. SMTP is either the server on 127.0.0.1 from tests/smtp_sink.py or a stand-in
for smtplib, so nothing leaves the machine and nothing waits for real time.
"""

from __future__ import annotations

import json
import re
import smtplib
from datetime import datetime, timedelta
from email import message_from_bytes, policy
from pathlib import Path

import pytest

from app import outbox, util
from app.config import Settings
from app.mailer.providers import ConsoleEmailProvider, OutboundEmail, SmtpEmailProvider
from app.mailer.render import reflect
from app.util import is_example_address, iso, normalize_email, parse_iso
from tests.conftest import (
    ANGI_ADS_EXAMPLE,
    BASE_URL,
    START,
    ads_payload,
    db_one,
    db_rows,
    leads_payload,
    post_lead,
)
from tests.smtp_sink import SmtpSink

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
EMAIL_ALERT_KEY = "email_send_error"
INTRO_SUBJECT = "Your House Cleaning request: pick a time with Sparkle Home Cleaning"
FOLLOWUP_SUBJECT = "Still need help with your House Cleaning? - Sparkle Home Cleaning"
ANGI_EXAMPLE_ADDRESS = ANGI_ADS_EXAMPLE["Email"]  # the consumer printed in Angi's own documentation
ANGI_EXAMPLE_REASON = "address is the one printed in Angi's example payload; nothing was sent"


# --- helpers ---------------------------------------------------------------------------


def consumer(n: int) -> dict[str, str]:
    """Contact details no other lead in the test shares, so the lead is not a duplicate."""
    return {"Email": f"consumer{n}@example.com", "PhoneNumber": f"317555{1000 + n:04d}", "FirstName": f"Lead{n}"}


def new_lead(client, ctx, **overrides) -> dict:
    """Post an Angi Ads lead for Sparkle; return its row."""
    response = post_lead(client, ads_payload(**overrides))
    assert response.status_code == 200, response.text
    assert response.json()["result"] == "processed", response.text
    return lead_row(ctx, response.json()["leadId"])


def lead_row(ctx, lead_id: str) -> dict:
    return db_one(ctx, "SELECT * FROM leads WHERE id = ?", (lead_id,))


def messages(ctx, lead_id: str, template: str | None = None) -> list[dict]:
    rows = db_rows(
        ctx, "SELECT * FROM outbox_messages WHERE lead_id = ? ORDER BY sequence, template, created_at, id", (lead_id,)
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


def unsubscribe_path(lead: dict) -> str:
    return f"/unsubscribe/{lead['booking_token']}"


def book_first_open_slot(client, lead: dict) -> str:
    page = client.get(f"/book/{lead['booking_token']}")
    slot = re.search(r'name="slot" value="([^"]+)"', page.text).group(1)
    response = client.post(f"/book/{lead['booking_token']}", data={"slot": slot}, follow_redirects=False)
    assert response.status_code == 303, response.text
    return slot


def run_worker_until(ctx, clock, moment: datetime) -> int:
    """What the worker does in real time up to ``moment``: at each time a message falls
    due, one delivery pass and then the alert announcements. Returns how many alerts
    were announced on the way."""
    announced = 0
    while (due := outbox.next_due_at(ctx.db)) and parse_iso(due) <= moment:
        clock.set(max(clock(), parse_iso(due)))
        ctx.worker.deliver_due()
        announced += ctx.notifier.dispatch(ctx.db, clock())
    clock.set(moment)
    return announced


def refuse_mail_for(mailer, *addresses: str) -> set[str]:
    """Fault injection: the provider fails for these recipients (a full mailbox) and
    delivers to everybody else. Returns the set, so a test can empty it when the
    mailbox has room again."""
    refused = set(addresses)
    real_send = mailer.send

    def send(email):
        if email.to in refused:
            raise RuntimeError("452 4.2.2 mailbox over quota")
        return real_send(email)

    mailer.send = send
    return refused


def really_sends(mailer, monkeypatch) -> None:
    """Make the in-memory provider declare what the SMTP and Resend providers declare:
    that mail handed to it leaves the machine."""
    monkeypatch.setattr(mailer, "delivers_externally", True, raising=False)


def outbound(to: str = "pat@example.com") -> OutboundEmail:
    return OutboundEmail(
        to=to,
        subject=INTRO_SUBJECT,
        text="Hi Pat,\n\nPick a time: https://leads.example.test/book/abc\n",
        html="<p>Hi Pat</p>",
        from_name="Sparkle Home Cleaning",
        from_address="mailer@example.test",
    )


def smtp_provider(port: int, timeout: float = 5.0) -> SmtpEmailProvider:
    settings = Settings(
        _env_file=None, smtp_host="127.0.0.1", smtp_port=port, smtp_security="none",
        smtp_username="mailer@example.test", smtp_password="app-password", smtp_timeout_seconds=timeout,
    )
    return SmtpEmailProvider(settings)


# --- email-alert-flaps-on-one-bad-lead -----------------------------------------------


def test_one_lead_whose_email_fails_all_day_is_one_alert_while_other_leads_are_answered(client, ctx, mailer, clock):
    # Rule: one incident, one alert. A first email that is retried every half hour for a
    # day must not open (and announce) a new "Outbound email is failing" alert after
    # every email that works for somebody else.
    refuse_mail_for(mailer, "consumer1@example.com")
    stuck = new_lead(client, ctx, **consumer(1))
    announced = 0
    for hour in range(1, 24):  # 23 hours: another lead every hour, each answered at once
        announced += run_worker_until(ctx, clock, START + timedelta(hours=hour))
        new_lead(client, ctx, **consumer(1 + hour))
        announced += run_worker_until(ctx, clock, clock())

    intro = message(ctx, stuck["id"], "intro")
    assert (intro["status"], intro["attempts"]) == ("pending", 50)
    assert len([email for email in mailer.sent if email.subject == INTRO_SUBJECT]) == 23  # everybody else was answered
    assert [(a["status"], a["severity"], a["count"]) for a in email_alerts(ctx)] == [("open", "critical", 50)]
    assert announced == 2  # when it opened, and once more when it turned critical


def test_alert_stays_open_while_the_failing_email_waits_for_its_retry_and_closes_when_it_goes_out(
    client, ctx, mailer, clock
):
    # Rule: "sending works again" is only true once nothing is waiting for a retry. An
    # email that works for another lead leaves the alert open; the failing one going out closes it.
    refused = refuse_mail_for(mailer, "consumer1@example.com")
    stuck = new_lead(client, ctx, **consumer(1))
    assert ctx.worker.deliver_due() == 1  # fails: the alert opens, the retry is due in 5 seconds
    new_lead(client, ctx, **consumer(2))

    assert ctx.worker.deliver_due() == 1  # the other lead is answered at once
    assert [(a["status"], a["count"]) for a in email_alerts(ctx)] == [("open", 1)]

    refused.clear()  # the mailbox has room again
    clock.advance(seconds=5)
    assert ctx.worker.deliver_due() == 1
    assert message(ctx, stuck["id"], "intro")["status"] == "sent"
    assert [(a["status"], a["resolved_at"]) for a in email_alerts(ctx)] == [("resolved", iso(clock()))]


def test_a_working_send_does_not_close_the_alert_under_a_failing_retry_claimed_in_the_same_batch(
    client, ctx, mailer, clock
):
    # Rule: a retry the worker has already claimed ('sending', with its last error) still
    # counts as failing, so a first response to somebody else in the same batch does not
    # close the alert a moment before that retry fails again.
    refuse_mail_for(mailer, "consumer1@example.com")
    stuck = new_lead(client, ctx, **consumer(1))
    assert ctx.worker.deliver_due() == 1  # fails: the alert opens, the retry is due in 5 seconds
    clock.advance(seconds=5)
    healthy = new_lead(client, ctx, **consumer(2))
    batch = {m["lead_id"]: m for m in (outbox.claim_next(ctx.db, clock()), outbox.claim_next(ctx.db, clock()))}
    assert message(ctx, stuck["id"], "intro")["status"] == "sending"

    assert ctx.worker.deliver(batch[healthy["id"]]) == "sent"
    assert [(a["status"], a["count"]) for a in email_alerts(ctx)] == [("open", 1)]

    assert ctx.worker.deliver(batch[stuck["id"]]) == "retry"
    assert [(a["status"], a["count"]) for a in email_alerts(ctx)] == [("open", 2)]


# --- followups-bunch-after-downtime --------------------------------------------------


def test_after_days_of_downtime_only_the_latest_overdue_follow_up_is_sent(client, ctx, mailer, clock):
    # Rule: when several nudges are overdue at once the consumer gets one of them, not
    # the same text twice half an hour apart.
    lead = new_lead(client, ctx, **consumer(1))
    assert ctx.worker.deliver_due() == 1  # the intro
    clock.advance(days=3)  # the service was stopped that day and started again three days later

    run_worker_until(ctx, clock, clock() + timedelta(hours=2))

    assert [email.subject for email in mailer.sent] == [INTRO_SUBJECT, FOLLOWUP_SUBJECT]
    first, second = messages(ctx, lead["id"], "followup")
    assert (first["status"], first["last_error"], first["sent_at"]) == ("cancelled", "superseded", None)
    assert (second["status"], second["sent_at"]) == ("sent", iso(START + timedelta(days=3)))
    assert lead_events(ctx, lead["id"], "email.cancelled") == [
        {"reason": "superseded", "count": 1, "templates": ["followup"]}
    ]


def test_overdue_follow_up_is_dropped_when_the_later_one_is_due_but_not_claimed_yet(client, ctx, mailer, clock):
    # Rule: a later nudge that is due supersedes the earlier one wherever it is: here it
    # is still waiting in the queue ('pending') because the batch ended with follow-up 1.
    lead = new_lead(client, ctx, **consumer(1))
    ctx.worker.deliver_due()
    clock.advance(days=3)
    claimed = outbox.claim_next(ctx.db, clock())
    assert (claimed["template"], claimed["sequence"]) == ("followup", 1)
    assert message(ctx, lead["id"], "followup", 2)["status"] == "pending"

    assert ctx.worker.deliver(claimed) == "cancelled"

    assert message(ctx, lead["id"], "followup", 1)["last_error"] == "superseded"
    assert [email.subject for email in mailer.sent] == [INTRO_SUBJECT]
    assert ctx.worker.deliver_due() == 1  # the one nudge the consumer does get
    assert [email.subject for email in mailer.sent] == [INTRO_SUBJECT, FOLLOWUP_SUBJECT]


def test_with_three_follow_ups_overdue_only_the_last_one_goes_out(client, ctx, settings, mailer, clock):
    # Rule: "only the latest" holds for any cadence, not just the default two nudges.
    settings.followup_delays_minutes = "60,1440,4320"
    lead = new_lead(client, ctx, **consumer(1))
    ctx.worker.deliver_due()
    clock.advance(days=5)

    run_worker_until(ctx, clock, clock() + timedelta(hours=2))

    assert [(m["sequence"], m["status"], m["last_error"]) for m in messages(ctx, lead["id"], "followup")] == [
        (1, "cancelled", "superseded"),
        (2, "cancelled", "superseded"),
        (3, "sent", None),
    ]
    assert [email.subject for email in mailer.sent] == [INTRO_SUBJECT, FOLLOWUP_SUBJECT]


# --- unsubscribe-scope-silently-drops-new-requests -----------------------------------


def test_new_request_from_an_address_that_opted_out_is_flagged_for_a_person_to_pick_up(client, ctx, mailer, clock):
    # Rule: a lead that will get no first response because its address opted out must not
    # sit unseen: it is stored, not emailed, and marked as needing review.
    first = new_lead(client, ctx, Email="pat@example.com")
    ctx.worker.deliver_due()
    assert first["needs_review"] == 0
    assert client.post(unsubscribe_path(first)).status_code == 200
    clock.advance(days=120)  # long past the duplicate window: a brand-new, paid-for request

    response = post_lead(client, ads_payload(Email="pat@example.com"))

    body = response.json()
    assert body["email"] == {"status": "skipped", "reason": "unsubscribed"}
    again = lead_row(ctx, body["leadId"])
    assert (again["is_duplicate"], again["status"], again["first_contacted_at"]) == (0, "new", None)
    assert messages(ctx, again["id"]) == []
    assert again["needs_review"] == 1


@pytest.mark.parametrize("template", ["intro", "repeat_request", "followup"])
def test_email_footer_says_the_link_stops_all_email_from_the_business(client, ctx, template):
    # Rule: the footer promises what the link does. It opts the address out of this
    # business's email for every request, so it must not say "about this request".
    lead = new_lead(client, ctx, **consumer(1))
    tenant = db_one(ctx, "SELECT * FROM tenants WHERE id = ?", (lead["tenant_id"],))
    link = f"{BASE_URL}{unsubscribe_path(lead)}"

    email = ctx.renderer.render(template, lead=lead, tenant=tenant)

    assert email.text.rstrip().endswith(f"Don't want emails from Sparkle Home Cleaning? {link}")
    assert re.search(rf'<a href="{re.escape(link)}"[^>]*>Stop emails from Sparkle Home Cleaning</a>', email.html)
    assert "about this request" not in email.text + email.html


def test_unsubscribe_question_names_the_business_and_every_request_made_to_it(client, ctx):
    # Rule: the page that asks for confirmation describes the scope of the opt-out.
    lead = new_lead(client, ctx, Email="pat@example.com")

    page = client.get(unsubscribe_path(lead))

    assert "<h1>Stop emails from Sparkle Home Cleaning?</h1>" in page.text
    assert "Sparkle Home Cleaning will no longer email you about requests you make through Angi." in page.text
    assert "about this request" not in page.text and "about the request you made" not in page.text


def test_unsubscribe_confirmation_says_the_business_will_not_email_again(client, ctx):
    # Rule: after confirming, the page states what was done: no more email from this
    # business (it used to promise silence "about this request" only).
    lead = new_lead(client, ctx, Email="pat@example.com")

    page = client.post(unsubscribe_path(lead))

    assert "You're unsubscribed" in page.text
    assert "Sparkle Home Cleaning won't email you again." in page.text
    assert "about this request" not in page.text


# --- email-encoded-word-address ------------------------------------------------------


@pytest.mark.parametrize(
    "address",
    [
        "=?utf-8?q?victim?=@example.com",  # the mail library would send this to victim@example.com
        "=?UTF-8?Q?victim?=@example.com",
        "=?iso-8859-1?q?victim?=@example.com",
        "=?utf-8?b?dmljdGlt?=@example.com",  # the base64 form
        "=?utf-8?q?vic?=tim@example.com",  # only part of the mailbox name encoded
        "=?utf-8?q??=@example.com",  # an empty word: the message could not even be built
    ],
    ids=["q", "upper-case", "latin-1", "base64", "partly-encoded", "empty-word"],
)
def test_address_written_as_an_encoded_word_is_not_a_usable_address(address):
    # Rule: "=?charset?q?...?=" (RFC 2047) in an address is decoded by the mail library,
    # so the email would go to another mailbox than the one on record. Not usable.
    assert normalize_email(address) is None


def test_encoded_word_spelling_of_an_opted_out_address_gets_no_email(client, ctx, mailer):
    # Rule: an opt-out cannot be walked around by spelling the same mailbox as an encoded
    # word. The lead is kept as Angi sent it, flagged for review, and nothing is queued.
    victim = new_lead(client, ctx, Email="victim@example.com")
    ctx.worker.deliver_due()
    assert client.post(unsubscribe_path(victim)).status_code == 200
    spelling = "=?utf-8?q?victim?=@example.com"

    response = post_lead(client, ads_payload(Email=spelling, PhoneNumber="3175552000"))

    body = response.json()
    assert body["result"] == "processed"
    assert body["email"] == {"status": "skipped", "reason": "no_valid_email"}
    assert "email is not a valid address" in body["warnings"]
    lead = lead_row(ctx, body["leadId"])
    assert (lead["email"], lead["email_normalized"], lead["needs_review"]) == (spelling, None, 1)
    assert messages(ctx, lead["id"]) == []
    assert ctx.worker.deliver_due() == 0
    assert [email.to for email in mailer.sent] == ["victim@example.com"]  # the intro, before the opt-out


def test_encoded_word_in_a_task_name_reaches_the_subject_as_the_literal_text(client, ctx, tmp_path):
    # Rule: words the lead supplied are shown in the subject as typed. An encoded word
    # must not be decoded by the reader's mail client into a link ("evil.com/claim").
    outbox_dir = tmp_path / "outbox"
    ctx.worker.email_provider = ConsoleEmailProvider(out_dir=str(outbox_dir))  # writes the message as it goes on the wire
    payload = leads_payload(email="jane@example.com", taskName="=?utf-8?q?evil=2Ecom/claim?=")
    assert post_lead(client, payload).json()["result"] == "processed"

    assert ctx.worker.deliver_due() == 1

    (written,) = outbox_dir.glob("*.eml")
    shown = message_from_bytes(written.read_bytes(), policy=policy.default)["Subject"]
    assert shown == "Your = ?utf-8?q?evil=2Ecom/claim?= request: pick a time with Summit Heating & Air"
    assert "evil.com" not in shown


# --- unsubscribe-cancels-pending-confirmation ----------------------------------------


def test_unsubscribing_leaves_a_booking_confirmation_that_has_not_gone_out_yet(client, ctx, mailer):
    # Rule: an opt-out stops outreach. The confirmation of a booking the consumer made
    # themselves is still sent, also when it was queued before they unsubscribed.
    lead = new_lead(client, ctx, **consumer(1))
    ctx.worker.deliver_due()  # the intro
    book_first_open_slot(client, lead)  # the confirmation is queued; the worker has not run yet

    page = client.post(unsubscribe_path(lead))

    assert "if you book a time yourself, we'll confirm it by email" in page.text
    assert message(ctx, lead["id"], "booking_confirmation")["status"] == "pending"
    assert [e["reason"] for e in lead_events(ctx, lead["id"], "email.cancelled")] == ["booked"]  # the nudges, earlier
    assert ctx.worker.deliver_due() == 1
    confirmation = message(ctx, lead["id"], "booking_confirmation")
    assert (confirmation["status"], confirmation["last_error"]) == ("sent", None)
    assert (mailer.sent[-1].to, mailer.sent[-1].subject[:15]) == ("consumer1@example.com", "You're booked: ")


def test_unsubscribing_between_retries_does_not_drop_the_booking_confirmation(client, ctx, mailer, clock):
    # Rule: the same when the confirmation is waiting for its retry after a mail hiccup.
    lead = new_lead(client, ctx, **consumer(1))
    ctx.worker.deliver_due()
    book_first_open_slot(client, lead)
    mailer.fail_with = RuntimeError("smtp hiccup")
    mailer.fail_times = 1
    assert ctx.worker.deliver_due() == 1  # first attempt fails; the retry is due in 5 seconds

    assert client.post(unsubscribe_path(lead)).status_code == 200
    clock.advance(seconds=5)
    ctx.worker.deliver_due()

    confirmation = message(ctx, lead["id"], "booking_confirmation")
    assert (confirmation["status"], confirmation["attempts"]) == ("sent", 2)
    assert mailer.sent[-1].subject.startswith("You're booked: ")


# --- smtp-standing-connection-never-dropped-on-non-421-error -------------------------


class OneBadSessionSink(SmtpSink):
    """The mail server of the finding: its first session has gone bad. It still answers
    NOOP with 250 but refuses mail there, while any new connection works. Built on the
    sink's own switches, which read as set for as long as the first connection is the
    only one that was ever opened."""

    def __init__(self, *, refuse_data: str | None = None, refuse_recipient: str | None = None) -> None:
        self._bad_session = {"data": refuse_data, "recipient": refuse_recipient}
        super().__init__()

    @property
    def refuse_data(self) -> str | None:
        return self._bad_session["data"] if self.connections == 1 else None

    @refuse_data.setter
    def refuse_data(self, value: str | None) -> None:
        pass  # the base class clears its switches when it is built

    @property
    def refuse_recipient(self) -> str | None:
        return self._bad_session["recipient"] if self.connections == 1 else None

    @refuse_recipient.setter
    def refuse_recipient(self, value: str | None) -> None:
        pass


@pytest.mark.parametrize(
    "refusal",
    ["452 4.5.3 Too many messages in this session", "530 5.7.0 Authentication Required"],
    ids=["4xx", "5xx"],
)
def test_a_session_that_refuses_every_message_does_not_keep_the_next_sends(refusal):
    # Rule: a failed send does not pin later sends to the same connection. The refused
    # message is an error for that lead; the next ones go out on a fresh connection.
    with OneBadSessionSink(refuse_data=refusal) as sink:
        provider = smtp_provider(sink.port)
        with pytest.raises(smtplib.SMTPDataError):
            provider.send(outbound(to="lead0@example.com"))

        for n in (1, 2, 3):
            provider.send(outbound(to=f"lead{n}@example.com"))
        provider.close()

    assert len(sink.messages) == 3
    assert sink.connections == 2


def test_a_session_that_temporarily_refuses_every_recipient_does_not_keep_the_next_sends():
    # Rule: a 4xx answer to RCPT is about the session or the moment, not about the
    # address, so that connection is given up as well.
    with OneBadSessionSink(refuse_recipient="452 4.5.3 Too many recipients in this session") as sink:
        provider = smtp_provider(sink.port)
        with pytest.raises(smtplib.SMTPRecipientsRefused):
            provider.send(outbound(to="lead0@example.com"))

        for n in (1, 2, 3):
            provider.send(outbound(to=f"lead{n}@example.com"))
        provider.close()

    assert len(sink.messages) == 3
    assert sink.connections == 2


# --- smtp-dead-connection-costs-full-timeout -----------------------------------------


class SilentSmtp:
    """Stands in for smtplib.SMTP where the real thing would need real waiting. Each
    connection has a socket that only remembers its timeout. After ``go_silent()`` the
    open connections stop answering, like a connection that died without a reset: a
    command then waits for the socket's timeout - added to ``waited``, not slept - and
    ends the way smtplib ends it."""

    def __init__(self) -> None:
        self.opened = 0
        self.waited = 0.0  # seconds spent waiting for answers that never came
        self.commands: list[tuple[str, float]] = []  # (command, socket timeout it ran under)
        self.delivered: list = []
        self._silent_up_to = 0  # connections opened up to here no longer answer

    def go_silent(self) -> None:
        self._silent_up_to = self.opened

    def connection_class(self) -> type:
        server = self

        class Socket:
            def __init__(self, timeout: float) -> None:
                self.timeout = timeout

            def settimeout(self, value: float) -> None:
                self.timeout = value

        class Connection:
            def __init__(self, host, port, timeout=None, context=None):
                server.opened += 1
                self.number = server.opened
                self.sock = Socket(timeout)

            def _command(self, name: str) -> None:
                if self.sock is None:
                    raise smtplib.SMTPServerDisconnected("please run connect() first")
                server.commands.append((name, self.sock.timeout))
                if self.number <= server._silent_up_to:
                    server.waited += self.sock.timeout
                    self.sock = None  # smtplib closes the connection when a read times out
                    raise smtplib.SMTPServerDisconnected("Connection unexpectedly closed: timed out")

            def login(self, username, password):
                pass

            def noop(self):
                self._command("NOOP")
                return (250, b"2.0.0 OK")

            def send_message(self, msg):
                self._command("DATA")
                server.delivered.append(msg)
                return {}

            def quit(self):
                self._command("QUIT")

            def close(self):
                self.sock = None

        return Connection


@pytest.fixture
def silent_smtp(monkeypatch) -> SilentSmtp:
    server = SilentSmtp()
    monkeypatch.setattr(smtplib, "SMTP", server.connection_class())
    return server


def test_a_standing_connection_that_died_silently_costs_three_seconds_not_the_whole_smtp_timeout(silent_smtp):
    # Rule: the liveness check before a send is bounded by a short timeout (a healthy
    # server answers NOOP in milliseconds), so a dead connection delays the lead's email
    # by 3 seconds before the reconnect, not by SMTP_TIMEOUT_SECONDS.
    provider = smtp_provider(port=25, timeout=15.0)
    provider.send(outbound())  # opens the standing connection
    silent_smtp.go_silent()  # Wi-Fi change, NAT timeout, laptop sleep: no reset, no answers

    provider.send(outbound())

    assert silent_smtp.waited == 3.0
    assert silent_smtp.opened == 2
    assert len(silent_smtp.delivered) == 2


def test_the_short_timeout_is_for_the_liveness_check_only_and_the_send_gets_the_full_one_back(silent_smtp):
    # Rule: only NOOP has to be answered within 3 seconds. Handing over a message may
    # take longer, so the configured timeout is back in force before the send.
    provider = smtp_provider(port=25, timeout=15.0)
    provider.send(outbound())

    provider.send(outbound())  # a healthy connection: checked, then used

    assert silent_smtp.commands == [("DATA", 15.0), ("NOOP", 3.0), ("DATA", 15.0)]
    assert silent_smtp.opened == 1


# --- reflect-misses-domains-next-to-non-ascii ----------------------------------------


@pytest.mark.parametrize(
    ("value", "kept"),
    [
        ("点击evil.com领取奖品", ""),  # letters of another script right after the domain
        ("visit evil.comé now", "visit now"),  # an accented letter right after it
        ("evil.com_x", "_x"),  # an underscore right after it
        ("evil.рф", ""),  # a top-level domain that is not written in Latin letters
    ],
    ids=["cjk-after", "accent-after", "underscore-after", "cyrillic-tld"],
)
def test_a_bare_domain_is_removed_whatever_touches_it_and_whatever_script_its_ending_is_in(value, kept):
    # Rule: text a stranger typed into Angi must not carry a domain into an email the
    # business signs, also when no space or ASCII punctuation follows it.
    assert reflect(value) == kept


def test_a_domain_glued_to_non_ascii_text_never_reaches_the_email(client, ctx, mailer):
    # Rule: the same through the whole pipeline: greeting, subject, first sentence, summary.
    payload = leads_payload(
        email="victim@example.com",
        firstName="点击evil.com领取奖品",
        name="点击evil.com领取奖品 Doe",
        taskName="Roof repair evil.comé",
        city="evil.рф",
        interview=[{"question": "Anything else?", "answer": "see evil.com_x"}],
    )
    assert post_lead(client, payload).json()["result"] == "processed"

    ctx.worker.deliver_due()

    (email,) = mailer.sent
    for part in (email.subject, email.text, email.html):
        assert "evil" not in part.lower()
    assert email.subject == "Your Roof repair request: pick a time with Summit Heating & Air"
    assert email.text.splitlines()[0] == "Hi there,"
    assert "  - Anything else: see _x\n" in email.text


# --- confirmation-ignores-max-attempts-setting ---------------------------------------


def test_booking_confirmation_is_queued_with_the_configured_number_of_attempts(client, ctx, settings):
    # Rule: OUTBOX_MAX_ATTEMPTS applies to every message, the booking confirmation included.
    settings.outbox_max_attempts = 2
    lead = new_lead(client, ctx, **consumer(1))
    ctx.worker.deliver_due()

    book_first_open_slot(client, lead)

    assert [(m["template"], m["sequence"], m["max_attempts"]) for m in messages(ctx, lead["id"])] == [
        ("booking_confirmation", 0, 2),
        ("intro", 0, 2),
        ("followup", 1, 2),
        ("followup", 2, 2),
    ]


def test_booking_confirmation_is_given_up_after_the_configured_number_of_attempts(client, ctx, settings, mailer, clock):
    # Rule: ...so with OUTBOX_MAX_ATTEMPTS=2 a confirmation that cannot be sent is failed
    # after the second attempt (5 seconds after the first), not after the default five.
    settings.outbox_max_attempts = 2
    lead = new_lead(client, ctx, **consumer(1))
    ctx.worker.deliver_due()
    book_first_open_slot(client, lead)
    mailer.fail_with = RuntimeError("smtp down")
    mailer.fail_times = -1

    run_worker_until(ctx, clock, START + timedelta(hours=1))

    confirmation = message(ctx, lead["id"], "booking_confirmation")
    assert (confirmation["status"], confirmation["attempts"]) == ("failed", 2)
    failed = db_one(ctx, "SELECT occurred_at FROM lead_events WHERE lead_id = ? AND type = 'email.failed'", (lead["id"],))
    assert failed["occurred_at"] == iso(START + timedelta(seconds=5))
    assert lead_row(ctx, lead["id"])["needs_review"] == 1


# --- reviewer-posts-spec-example-emails-stranger -------------------------------------


def test_angi_example_address_is_never_handed_to_a_provider_that_really_sends(client, ctx, mailer, monkeypatch):
    # Rule: Angi's example payload, posted as printed, must not make the live service
    # email the stranger who may own that mailbox. An ordinary Gmail address in the same
    # batch is not affected.
    really_sends(mailer, monkeypatch)
    response = post_lead(client, ANGI_ADS_EXAMPLE)
    assert response.json()["result"] == "processed", response.text
    example = lead_row(ctx, response.json()["leadId"])
    real = new_lead(client, ctx, Email="pat.lee@gmail.com", PhoneNumber="3175552000", FirstName="Pat")

    assert ctx.worker.deliver_due() == 2

    intro = message(ctx, example["id"], "intro")
    assert (intro["to_address"], intro["status"], intro["attempts"]) == (ANGI_EXAMPLE_ADDRESS, "failed", 1)
    assert intro["last_error"] == ANGI_EXAMPLE_REASON
    assert [m["status"] for m in messages(ctx, example["id"], "followup")] == ["cancelled", "cancelled"]
    assert lead_row(ctx, example["id"])["needs_review"] == 1
    assert email_alerts(ctx) == []  # not an outage
    assert [email.to for email in mailer.sent] == ["pat.lee@gmail.com"]
    assert message(ctx, real["id"], "intro")["status"] == "sent"


@pytest.mark.parametrize(
    "spelling", ["BOB.BUILDER@GMAIL.COM", "  Bob.Builder@Gmail.com  "], ids=["upper-case", "spaces"]
)
def test_angi_example_address_is_refused_however_the_lead_spells_it(client, ctx, mailer, monkeypatch, spelling):
    # Rule: capitals and stray spaces do not make it a different mailbox.
    really_sends(mailer, monkeypatch)
    lead = new_lead(client, ctx, Email=spelling)

    assert ctx.worker.deliver_due() == 1

    intro = message(ctx, lead["id"], "intro")
    assert (intro["status"], intro["last_error"]) == ("failed", ANGI_EXAMPLE_REASON)
    assert mailer.sent == []


def test_angi_example_as_it_stands_in_the_pdf_posted_raw_is_refused_as_well(client, ctx, mailer, monkeypatch):
    # Rule: the same example copied out of Angi's PDF (curly quotes, a missing comma, an
    # unquoted address) is salvaged into the same lead, so its address is protected too.
    really_sends(mailer, monkeypatch)
    as_printed = (SAMPLES / "malformed_spec_example.txt").read_bytes().replace(b"@example.com", b"@gmail.com")
    response = post_lead(client, as_printed)
    assert response.json()["result"] == "processed", response.text
    lead_id = response.json()["leadId"]
    assert lead_row(ctx, lead_id)["email_normalized"] == ANGI_EXAMPLE_ADDRESS

    assert ctx.worker.deliver_due() == 1

    intro = message(ctx, lead_id, "intro")
    assert (intro["status"], intro["last_error"]) == ("failed", ANGI_EXAMPLE_REASON)
    assert mailer.sent == []


@pytest.mark.parametrize(
    "target", [ANGI_EXAMPLE_ADDRESS, "  Bob.Builder@GMAIL.com "], ids=["as-printed", "upper-case-and-spaces"]
)
def test_redirecting_all_mail_to_the_angi_example_address_sends_nothing(
    client, ctx, settings, mailer, monkeypatch, target
):
    # Rule: the check is made on the address the provider would be given, so
    # EMAIL_REDIRECT_ALL_TO cannot point real mail at that mailbox either, however it is typed.
    really_sends(mailer, monkeypatch)
    settings.email_redirect_all_to = target
    lead = new_lead(client, ctx, Email="pat.lee@gmail.com")

    assert ctx.worker.deliver_due() == 1

    intro = message(ctx, lead["id"], "intro")
    assert (intro["to_address"], intro["status"]) == ("pat.lee@gmail.com", "failed")
    assert intro["last_error"] == ANGI_EXAMPLE_REASON
    assert mailer.sent == []


def test_each_kind_of_example_address_has_its_own_reason_and_an_ordinary_address_has_none():
    # Rule: the lead's page says why nothing was sent: Angi's example address is one
    # reason, a reserved example domain another; a real consumer's address is neither.
    assert util.undeliverable_example_reason(ANGI_EXAMPLE_ADDRESS) == ANGI_EXAMPLE_REASON
    assert util.undeliverable_example_reason("  Bob.Builder@GMAIL.com ") == ANGI_EXAMPLE_REASON
    assert util.undeliverable_example_reason("pat@example.com") == (
        "address is on a reserved example domain; nothing was sent"
    )
    assert util.undeliverable_example_reason("pat.lee@gmail.com") is None


@pytest.mark.parametrize("kind", ["memory", "console"])
def test_a_provider_that_does_not_deliver_externally_still_gets_the_email_for_the_angi_example_address(
    client, ctx, mailer, tmp_path, kind
):
    # Rule: the guard is about real delivery only. The address is known to be Angi's
    # example, and the in-memory and console providers still produce its email (the
    # console provider is how a reviewer sees the introduction without sending anything).
    outbox_dir = tmp_path / "outbox"
    if kind == "console":
        ctx.worker.email_provider = ConsoleEmailProvider(out_dir=str(outbox_dir))
    lead_id = post_lead(client, ANGI_ADS_EXAMPLE).json()["leadId"]

    assert ctx.worker.deliver_due() == 1

    assert is_example_address(ANGI_EXAMPLE_ADDRESS)
    assert message(ctx, lead_id, "intro")["status"] == "sent"
    if kind == "console":
        (written,) = outbox_dir.glob("*.eml")
        assert message_from_bytes(written.read_bytes(), policy=policy.default)["To"] == ANGI_EXAMPLE_ADDRESS
    else:
        assert [email.to for email in mailer.sent] == [ANGI_EXAMPLE_ADDRESS]
