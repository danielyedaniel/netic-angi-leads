"""SmtpEmailProvider against a real SMTP conversation.

tests/test_email.py checks the provider's decisions with a fake smtplib. These tests let
the real smtplib talk to a small SMTP server on 127.0.0.1 (tests/smtp_sink.py), so the
kept-open connection is exercised at the protocol level: reuse, the server hanging up,
a slow greeting, and refusals. Nothing leaves the machine.
"""

from __future__ import annotations

import email
import smtplib
import time
from email import policy

import pytest

from app.config import Settings
from app.mailer.providers import OutboundEmail, PermanentEmailError, SmtpEmailProvider
from tests.smtp_sink import SmtpSink


@pytest.fixture
def sink():
    with SmtpSink() as server:
        yield server


def provider_for(sink: SmtpSink, **overrides) -> SmtpEmailProvider:
    fields = {
        "smtp_host": "127.0.0.1",
        "smtp_port": sink.port,
        "smtp_security": "none",
        "smtp_username": "mailer@example.test",
        "smtp_password": "app-password",
        "smtp_timeout_seconds": 5.0,
    }
    fields.update(overrides)
    return SmtpEmailProvider(Settings(_env_file=None, **fields))


def message(to: str = "pat@example.com", subject: str = "Your House Cleaning request") -> OutboundEmail:
    return OutboundEmail(
        to=to,
        subject=subject,
        text="Hi Pat,\n\nPick a time: https://leads.example.test/book/abc\n",
        html="<p>Hi Pat</p>",
        from_name="Sparkle Home Cleaning",
        from_address="mailer@example.test",
        headers={"List-Unsubscribe": "<https://leads.example.test/unsubscribe/abc>"},
    )


def test_message_arrives_intact_over_real_smtp(sink):
    # The whole message as a mail server receives it: headers, both parts, the unsubscribe URL.
    provider = provider_for(sink)

    result = provider.send(message(subject="Your Café & Ünïcode request"))
    provider.close()

    (raw,) = sink.messages
    received = email.message_from_bytes(raw, policy=policy.default)
    assert received["To"] == "pat@example.com"
    assert received["Subject"] == "Your Café & Ünïcode request"
    assert received["From"] == "Sparkle Home Cleaning <mailer@example.test>"
    assert received["Message-ID"] == result.message_id
    assert received["List-Unsubscribe"] == "<https://leads.example.test/unsubscribe/abc>"
    assert [part.get_content_type() for part in received.iter_parts()] == ["text/plain", "text/html"]


def test_one_connection_and_one_login_serve_many_messages(sink):
    # Rule: connection set-up is paid once. Five leads, one TCP connection, one AUTH.
    provider = provider_for(sink)

    for n in range(5):
        provider.send(message(to=f"lead{n}@example.com"))
    provider.close()

    assert len(sink.messages) == 5
    assert sink.connections == 1
    assert sink.count("AUTH") == 1
    assert sink.count("QUIT") == 1


def test_server_hanging_up_on_the_idle_connection_costs_a_reconnect_not_a_failure(sink):
    # Rule: mail servers drop idle clients. The next send notices and reconnects by itself.
    provider = provider_for(sink)
    provider.send(message(to="first@example.com"))

    sink.drop_idle()
    provider.send(message(to="second@example.com"))
    provider.close()

    assert len(sink.messages) == 2
    assert sink.connections == 2


def test_slow_greeting_is_paid_by_the_warm_up_not_by_the_lead(sink):
    # Rule: Gmail sometimes holds back its greeting for seconds. With the connection opened
    # ahead of time, sending the lead's email does not wait for it.
    sink.greeting_delay = 0.5
    provider = provider_for(sink)

    started = time.monotonic()
    provider.warm_up()
    warm_up_took = time.monotonic() - started

    started = time.monotonic()
    provider.send(message())
    send_took = time.monotonic() - started
    provider.close()

    assert warm_up_took >= 0.5
    assert send_took < 0.25
    assert sink.connections == 1


def test_refused_recipient_is_permanent_and_leaves_the_connection_usable(sink):
    # Rule: only a 5xx refusal of the recipient is a bad address. The server answered, so
    # the same connection carries the next lead's email.
    provider = provider_for(sink)
    sink.refuse_recipient = "550 5.1.1 The email account that you tried to reach does not exist"

    with pytest.raises(PermanentEmailError):
        provider.send(message(to="nobody@example.com"))

    sink.refuse_recipient = None
    provider.send(message(to="somebody@example.com"))
    provider.close()

    assert len(sink.messages) == 1
    assert sink.connections == 1


def test_greylisted_recipient_is_temporary(sink):
    # Rule: a 4xx refusal of the recipient is retried, not written off.
    provider = provider_for(sink)
    sink.refuse_recipient = "450 4.2.0 Greylisted, try again in five minutes"

    with pytest.raises(smtplib.SMTPRecipientsRefused):
        provider.send(message())
    provider.close()


def test_sending_limit_reported_after_the_message_body_is_not_a_bad_address(sink):
    # Rule: Gmail reports its daily sending limit as a 550 after DATA. That is our problem,
    # not the lead's: it must be retried and alerted on, and the next send uses a fresh connection.
    provider = provider_for(sink)
    sink.refuse_data = "550 5.4.5 Daily user sending limit exceeded"

    with pytest.raises(smtplib.SMTPDataError) as raised:
        provider.send(message())
    assert not isinstance(raised.value, PermanentEmailError)

    sink.refuse_data = None
    provider.send(message())
    provider.close()

    assert len(sink.messages) == 1
    assert sink.connections == 2


def test_unreachable_server_is_an_error_at_warm_up_and_at_send(sink):
    # Rule: nothing listening is an outage: both calls raise, neither hangs.
    provider = provider_for(sink, smtp_port=1, smtp_timeout_seconds=2.0)

    with pytest.raises(OSError):
        provider.warm_up()
    with pytest.raises(OSError):
        provider.send(message())
