"""Email delivery behind one small interface.

The outbox worker only knows ``provider.send(email)``. Swapping Gmail SMTP for Resend,
SendGrid or SES is a config change, and tests use the in-memory provider.

Errors are split in two because the right reaction differs:
* ``PermanentEmailError`` - the recipient's address was refused for good; retrying
  cannot help, and it is the lead's problem, not ours.
* anything else            - ours to fix or wait out (outage, credentials, sending
  quota, policy block): retried with backoff, and it raises an alert.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
import threading
from dataclasses import dataclass, field
from email import policy
from email.message import EmailMessage
from email.utils import format_datetime, formataddr, make_msgid
from pathlib import Path
from typing import Protocol

import certifi
import httpx

from app.config import Settings
from app.util import utcnow

log = logging.getLogger("app.mailer")


class PermanentEmailError(Exception):
    """The message can never be delivered to this recipient (e.g. mailbox rejected)."""


@dataclass
class OutboundEmail:
    to: str
    subject: str
    text: str
    html: str | None
    from_name: str
    from_address: str
    reply_to: str | None = None
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class SendResult:
    provider: str
    message_id: str | None


class EmailProvider(Protocol):
    name: str

    def send(self, email: OutboundEmail) -> SendResult: ...


def build_mime(email: OutboundEmail) -> EmailMessage:
    """Multipart text + HTML message with the headers mailbox providers expect."""
    # Lines up to the SMTP limit (998) rather than the 78-column default: a long URL in
    # List-Unsubscribe would otherwise be folded into RFC 2047 encoded words, which
    # mailbox providers do not read as a URL (no one-click unsubscribe).
    msg = EmailMessage(policy=policy.default.clone(max_line_length=998))
    msg["From"] = formataddr((email.from_name, email.from_address))
    msg["To"] = email.to
    # "=?" would start an RFC 2047 encoded word, which the header parser decodes: the
    # subject must carry the text literally (it contains words the lead supplied).
    msg["Subject"] = email.subject.replace("=?", "= ?")
    msg["Date"] = format_datetime(utcnow())
    domain = email.from_address.rsplit("@", 1)[-1] or "localhost"
    msg["Message-ID"] = make_msgid(domain=domain)
    if email.reply_to:
        msg["Reply-To"] = email.reply_to
    for name, value in email.headers.items():
        msg[name] = value
    msg.set_content(email.text)
    if email.html:
        msg.add_alternative(email.html, subtype="html")
    return msg


class ConsoleEmailProvider:
    """Development default: nothing leaves the machine. Each message is logged and
    written to ``data/outbox/*.eml`` so it can be opened in a mail client."""

    name = "console"

    def __init__(self, out_dir: str | None = "data/outbox") -> None:
        self.out_dir = Path(out_dir) if out_dir else None

    def send(self, email: OutboundEmail) -> SendResult:
        msg = build_mime(email)
        message_id = str(msg["Message-ID"])
        log.info("EMAIL (console) to=%s subject=%r\n%s", email.to, email.subject, email.text)
        if self.out_dir is not None:
            self.out_dir.mkdir(parents=True, exist_ok=True)
            stamp = utcnow().strftime("%Y%m%dT%H%M%S%f")
            safe_to = "".join(c if c.isalnum() else "_" for c in email.to)[:60]
            (self.out_dir / f"{stamp}_{safe_to}.eml").write_bytes(bytes(msg))
        return SendResult(provider=self.name, message_id=message_id)


class SmtpEmailProvider:
    """Any SMTP relay. Works with Gmail using an app password
    (smtp.gmail.com, port 465 + ssl, or port 587 + starttls).

    One authenticated connection is kept open between sends. Measured against Gmail,
    setting up a connection costs a few hundred milliseconds at best and, every so
    often, ten seconds (Gmail sometimes holds back its greeting). A lead should not
    wait for that, so the worker opens the connection when it starts and checks it
    once a minute (``warm_up``); sending then only has to hand over the message.
    """

    name = "smtp"
    delivers_externally = True  # real mail leaves the machine

    def __init__(self, settings: Settings) -> None:
        self.host = settings.smtp_host
        self.port = settings.smtp_port
        self.security = settings.smtp_security.strip().lower()
        if self.security not in ("ssl", "starttls", "none"):
            # Never fall back to an unencrypted connection because of a typo.
            raise ValueError(f"Unknown SMTP_SECURITY {settings.smtp_security!r} (expected ssl, starttls or none)")
        self.username = settings.smtp_username
        self.password = settings.smtp_password
        self.timeout = settings.smtp_timeout_seconds
        self._lock = threading.Lock()  # one message at a time on the standing connection
        self._standing: smtplib.SMTP | None = None

    # ------------------------------------------------------------------ connections
    def _connect(self) -> smtplib.SMTP:
        # certifi's CA bundle: some Python builds on macOS ship without usable system
        # roots, which makes TLS verification to the mail server fail.
        context = ssl.create_default_context(cafile=certifi.where())
        if self.security == "ssl":
            return smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout, context=context)
        client = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
        if self.security == "starttls":
            client.starttls(context=context)
        return client

    def _open(self) -> smtplib.SMTP:
        """A new connection, logged in and ready to send."""
        client = self._connect()
        try:
            if self.username:
                client.login(self.username, self.password)
        except BaseException:
            _close_quietly(client)
            raise
        return client

    def _standing_connection(self) -> smtplib.SMTP:
        """The kept-open connection, re-opened if the server has dropped it in the
        meantime. The caller holds ``self._lock``."""
        client = self._standing
        if client is not None:
            try:
                # A healthy server answers NOOP in milliseconds; a connection that went
                # dead without a reset must not cost a lead the whole send timeout.
                # (sock is None when smtplib has already closed the connection itself.)
                sock = getattr(client, "sock", None)
                if sock is not None:
                    sock.settimeout(min(self.timeout, 3.0))
                if client.noop()[0] == 250:
                    if sock is not None:
                        sock.settimeout(self.timeout)
                    return client
            except (smtplib.SMTPException, OSError):
                pass
            self._drop_standing()
        self._standing = self._open()
        return self._standing

    def _drop_standing(self) -> None:
        client, self._standing = self._standing, None
        if client is not None:
            _close_quietly(client)

    def warm_up(self) -> None:
        """Open (or check) the standing connection ahead of need, so that connection
        set-up never lands on a lead's clock. Raises if the server cannot be reached."""
        if not self._lock.acquire(blocking=False):
            return  # a send is using it right now, so it is certainly open
        try:
            self._standing_connection()
        finally:
            self._lock.release()

    def close(self) -> None:
        with self._lock:
            self._drop_standing()

    # ------------------------------------------------------------------ sending
    def _transmit(self, msg: EmailMessage) -> dict:
        """Hand the message to the server. Returns the recipients it refused (normally
        none); raises if the message was not accepted."""
        if not self._lock.acquire(blocking=False):
            # The standing connection is busy with another message (a burst of leads):
            # this one gets a connection of its own rather than waiting in line.
            client = self._open()
            try:
                return client.send_message(msg)
            finally:
                _close_quietly(client)
        try:
            client = self._standing_connection()
            try:
                return client.send_message(msg)
            except smtplib.SMTPServerDisconnected:
                pass  # the server hung up on us; start again below
            except smtplib.SMTPResponseException as exc:
                if exc.smtp_code != 421:  # 421 = "closing connection": also start again
                    self._drop_standing()  # do not tie later sends to a connection that refuses mail
                    raise
            except smtplib.SMTPRecipientsRefused as exc:
                if not _refused_for_good(exc.recipients):
                    # A temporary refusal. On a 421 smtplib has already closed the
                    # connection; either way the next send starts on a fresh one.
                    self._drop_standing()
                raise  # a 5xx is about the address: the connection is fine
            except (smtplib.SMTPException, OSError):
                self._drop_standing()  # a timeout or broken pipe: state unknown, do not reuse
                raise
            # One more try on a fresh connection: what the retry loop would do five
            # seconds later anyway, without making the lead wait for it.
            self._drop_standing()
            return self._standing_connection().send_message(msg)
        finally:
            self._lock.release()

    def send(self, email: OutboundEmail) -> SendResult:
        msg = build_mime(email)
        try:
            refused = self._transmit(msg)
        except smtplib.SMTPRecipientsRefused as exc:
            if _refused_for_good(exc.recipients):
                raise PermanentEmailError(f"recipient refused: {exc.recipients}") from exc
            raise  # a 4xx refusal (greylisting, mailbox busy) is worth retrying
        # Every other error is left to the caller to retry, 5xx included: a refused
        # sender, a rejected login, or a 550/552/554 after the message body, which is how
        # Gmail reports its daily sending limit and policy blocks. Calling those
        # "permanent" would quietly fail one lead per attempt until somebody noticed.
        if refused:
            if _refused_for_good(refused):
                raise PermanentEmailError(f"recipient refused: {refused}")
            raise RuntimeError(f"recipient temporarily refused: {refused}")
        return SendResult(provider=self.name, message_id=str(msg["Message-ID"]))


def _close_quietly(client: smtplib.SMTP) -> None:
    """Say goodbye if the server is still listening; never raise. (The message, if any,
    has already been accepted, so a failed QUIT must not look like a failed send.)"""
    try:
        client.quit()
    except (smtplib.SMTPException, OSError):
        try:
            client.close()
        except OSError:
            pass


def _refused_for_good(recipients: dict) -> bool:
    """smtplib reports refused recipients as {address: (code, message)}. The address is
    bad only if every answer is a 5xx; this is the one permanent SMTP error we know."""
    codes = [reply[0] for reply in recipients.values() if isinstance(reply, tuple) and reply]
    return bool(codes) and all(isinstance(code, int) and 500 <= code < 600 for code in codes)


class ResendEmailProvider:
    """Resend's HTTP API (needs a verified sending domain to reach arbitrary recipients)."""

    name = "resend"
    delivers_externally = True
    endpoint = "https://api.resend.com/emails"

    def __init__(self, settings: Settings) -> None:
        self.api_key = settings.resend_api_key
        self.timeout = settings.smtp_timeout_seconds

    def send(self, email: OutboundEmail) -> SendResult:
        payload: dict = {
            "from": formataddr((email.from_name, email.from_address)),
            "to": [email.to],
            "subject": email.subject.replace("=?", "= ?"),  # as in build_mime: never an encoded word
            "text": email.text,
        }
        if email.html:
            payload["html"] = email.html
        if email.reply_to:
            payload["reply_to"] = email.reply_to
        if email.headers:
            payload["headers"] = email.headers
        response = httpx.post(
            self.endpoint,
            json=payload,
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=self.timeout,
        )
        if response.status_code in (400, 422):
            raise PermanentEmailError(f"resend rejected the message: {response.text[:300]}")
        response.raise_for_status()
        try:
            message_id = response.json()["id"]
        except (ValueError, LookupError, TypeError):
            # Accepted is accepted: an unreadable answer must not look like a failed
            # send, or the email would go out again.
            message_id = None
        return SendResult(provider=self.name, message_id=str(message_id) if message_id is not None else None)


class MemoryEmailProvider:
    """Test double: records messages, and can be told to fail."""

    name = "memory"

    def __init__(self) -> None:
        self.sent: list[OutboundEmail] = []
        self.fail_with: Exception | None = None
        self.fail_times: int = 0  # fail this many sends, then succeed

    def send(self, email: OutboundEmail) -> SendResult:
        if self.fail_with is not None and self.fail_times != 0:
            if self.fail_times > 0:
                self.fail_times -= 1
            raise self.fail_with
        self.sent.append(email)
        return SendResult(provider=self.name, message_id=f"<memory-{len(self.sent)}@test>")


def build_provider(settings: Settings) -> EmailProvider:
    kind = settings.email_provider.lower()
    if kind == "smtp":
        return SmtpEmailProvider(settings)
    if kind == "resend":
        return ResendEmailProvider(settings)
    if kind == "console":
        return ConsoleEmailProvider()
    raise ValueError(f"Unknown EMAIL_PROVIDER {settings.email_provider!r} (expected console, smtp or resend)")
