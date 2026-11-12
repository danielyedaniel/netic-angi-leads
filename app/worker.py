"""Background worker: one daemon thread that

* delivers due outbox messages - woken the instant a lead is stored, so the first
  email normally leaves within about a second of the webhook arriving;
* retries webhook events that failed or were interrupted by a crash;
* runs the monitoring checks and announces new alerts.

Everything it does is driven by rows in the database, so it can be stopped and started
at any time without losing work, and the same code could run as a separate process.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from app import monitoring, outbox
from app.alerts import AlertNotifier, raise_alert, resolve_alert
from app.booking import available_slots
from app.cadence import FOLLOWUP_MIN_GAP, defer_past_quiet_hours
from app.config import Settings
from app.db import Database
from app.mailer.providers import EmailProvider, OutboundEmail, PermanentEmailError, SendResult
from app.mailer.render import EmailRenderer, unsubscribe_url
from app.repo import (
    EV_EMAIL_CANCELLED,
    EV_EMAIL_FAILED,
    EV_EMAIL_RETRY,
    EV_EMAIL_SENT,
    add_lead_event,
    get_lead,
    get_scheduled_appointment,
    get_tenant,
    is_suppressed,
)
from app.util import iso, one_line, parse_iso, scrub, seconds_between, undeliverable_example_reason

if TYPE_CHECKING:
    from app.ingest import IngestService

log = logging.getLogger("app.worker")

SEND_CONCURRENCY = 4
FIRST_TOUCH_TEMPLATES = ("intro", "repeat_request")
# A first response is the one email we cannot afford to drop. After the quick retries
# it keeps being retried (at the slowest backoff step, every 30 minutes) until the lead
# is a day old, so it survives a long mail outage.
FIRST_TOUCH_RETRY_WINDOW = timedelta(hours=24)
RECORD_SENT_TRIES = 3
EMAIL_ALERT_KEY = "email_send_error"


class BackgroundWorker:
    def __init__(
        self,
        *,
        db: Database,
        settings: Settings,
        email_provider: EmailProvider,
        renderer: EmailRenderer,
        notifier: AlertNotifier,
        clock: Callable[[], datetime],
    ) -> None:
        self.db = db
        self.settings = settings
        self.email_provider = email_provider
        self.renderer = renderer
        self.notifier = notifier
        self.clock = clock
        self.ingest: IngestService | None = None  # wired up by the app factory
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._pool = ThreadPoolExecutor(max_workers=SEND_CONCURRENCY, thread_name_prefix="email-send")

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="background-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None
        close = getattr(self.email_provider, "close", None)
        if close is not None:
            close()  # hang up the standing mail connection politely

    def _warm_up_mail(self) -> None:
        """Keep the mail connection open and checked, so that the next lead's email
        only has to be handed over. Providers without a connection have no warm_up."""
        warm_up = getattr(self.email_provider, "warm_up", None)
        if warm_up is None:
            return
        try:
            warm_up()
        except Exception as exc:  # noqa: BLE001 - not fatal here: sending retries and alerts on its own
            log.warning("could not open the mail connection ahead of time (%s: %s)", type(exc).__name__, exc)

    def wake(self) -> None:
        """Called right after a lead is stored: do not wait for the next poll."""
        self._wake.set()

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self) -> None:
        log.info("background worker started (poll every %.1fs)", self.settings.outbox_poll_seconds)
        try:
            # Nothing is being sent while we start (there is one worker), so a message
            # still marked 'sending' was cut off by the last shutdown. Send it now
            # rather than after the grace period: it may be a lead's first response.
            outbox.requeue_stuck(self.db, self.clock(), all_sending=True)
        except Exception:  # noqa: BLE001 - the periodic pass will pick them up instead
            log.exception("could not re-queue interrupted messages at start")
        next_maintenance = 0.0
        while not self._stop.is_set():
            try:
                self.deliver_due()
                if time.monotonic() >= next_maintenance:
                    self.run_maintenance()
                    next_maintenance = time.monotonic() + self.settings.monitor_interval_seconds
                self.notifier.dispatch(self.db, self.clock())
            except Exception:  # noqa: BLE001 - the loop must survive anything
                log.exception("background worker iteration failed")
            self._wake.wait(timeout=self.settings.outbox_poll_seconds)
            self._wake.clear()
        log.info("background worker stopped")

    # ------------------------------------------------------------------ jobs
    def deliver_due(self) -> int:
        """Send every message that is due. Returns how many were handled."""
        handled = 0
        while not self._stop.is_set():
            batch = []
            for _ in range(SEND_CONCURRENCY):
                try:
                    message = outbox.claim_next(self.db, self.clock())
                except Exception:  # noqa: BLE001 - e.g. the database is locked
                    if not batch:
                        raise
                    # Whatever is already claimed must still be sent in this pass,
                    # or it would sit in 'sending' until the grace period runs out.
                    log.exception("could not claim another message; sending the %d already claimed", len(batch))
                    break
                if message is None:
                    break
                batch.append(message)
            if not batch:
                break
            # First responses go first, in parallel: a burst of new leads must not queue
            # behind one slow send. Follow-ups then go one at a time, so each can see
            # what has just been sent to its recipient and wait its turn.
            first_touch = [m for m in batch if m["sequence"] == 0]
            follow_ups = [m for m in batch if m["sequence"] != 0]
            if len(first_touch) == 1:
                self.deliver(first_touch[0])
            elif first_touch:
                list(self._pool.map(self.deliver, first_touch))
            for message in follow_ups:
                self.deliver(message)
            handled += len(batch)
        return handled

    def run_maintenance(self) -> None:
        now = self.clock()
        # Off the worker thread: opening a connection can take seconds, and a lead that
        # arrives meanwhile must not wait behind it (it would get its own connection).
        self._pool.submit(self._warm_up_mail)
        outbox.requeue_stuck(self.db, now)
        if self.ingest is not None:
            self.ingest.retry_unfinished()
        monitoring.run_checks(self.db, self.settings, now)

    # ------------------------------------------------------------------ delivery
    def deliver(self, message: dict) -> str:
        """Deliver one claimed message. Returns sent | retry | failed | cancelled | postponed.

        Never raises: one message's problem must not stop the rest of its batch."""
        try:
            return self._deliver(message)
        except Exception as exc:  # noqa: BLE001 - e.g. a template bug; treat as a failed attempt
            log.exception("delivering %s failed unexpectedly", message["id"])
            try:
                return self._record_failure(message, f"{type(exc).__name__}: {exc}", permanent=False)
            except Exception:  # noqa: BLE001 - the database itself is unavailable
                # Nothing was sent and nothing could be written. The row stays 'sending'
                # and is put back in the queue by requeue_stuck.
                log.exception("could not record the failed attempt for %s", message["id"])
                return "retry"

    def _deliver(self, message: dict) -> str:
        now = self.clock()
        with self.db.read() as conn:
            lead = get_lead(conn, message["lead_id"])
            tenant = get_tenant(conn, message["tenant_id"])
            appointment = get_scheduled_appointment(conn, message["lead_id"])
            suppressed = is_suppressed(conn, message["tenant_id"], message["to_address"])
            slots = available_slots(conn, tenant, now) if tenant else []
            booked_elsewhere = recently_emailed = first_touch_open = superseded = False
            if message["template"] == "followup" and lead is not None:
                # The same person may have several leads (a repeat request). If one of
                # them is booked there is nothing left to nudge about. Only leads from
                # the same stretch of time count: a booking months ago says nothing
                # about today's request.
                same_need_since = iso(
                    parse_iso(lead["received_at"]) - timedelta(days=self.settings.duplicate_window_days)
                )
                booked_elsewhere = conn.execute(
                    "SELECT 1 FROM leads WHERE tenant_id = ? AND email_normalized = ? AND booked_at IS NOT NULL "
                    "AND received_at >= ? LIMIT 1",
                    (message["tenant_id"], message["to_address"], same_need_since),
                ).fetchone() is not None
                # And nobody should get a nudge minutes after another email from the same business.
                recently_emailed = conn.execute(
                    "SELECT 1 FROM outbox_messages WHERE tenant_id = ? AND to_address = ? AND status = 'sent' "
                    "AND sent_at > ? LIMIT 1",
                    (message["tenant_id"], message["to_address"], iso(now - FOLLOWUP_MIN_GAP)),
                ).fetchone() is not None
                # "Just checking in" only makes sense after the introduction. Is this
                # lead's first email still on its way (queued, or between retries)?
                first_touch_open = conn.execute(
                    "SELECT 1 FROM outbox_messages WHERE lead_id = ? AND template IN ('intro', 'repeat_request') "
                    "AND status IN ('pending', 'sending') LIMIT 1",
                    (lead["id"],),
                ).fetchone() is not None
                # After downtime several nudges can be overdue at once. Only the latest
                # one is sent: the same text twice, half an hour apart, is nagging.
                superseded = conn.execute(
                    "SELECT 1 FROM outbox_messages WHERE lead_id = ? AND template = 'followup' AND sequence > ? "
                    "AND status IN ('pending', 'sending') AND send_after <= ? LIMIT 1",
                    (lead["id"], message["sequence"], iso(now)),
                ).fetchone() is not None

        # The world may have changed since this message was queued.
        cancel_reason = None
        if lead is None or tenant is None:
            cancel_reason = "lead_or_tenant_missing"
        elif not tenant["is_active"]:
            cancel_reason = "tenant_inactive"
        elif suppressed and message["template"] != "booking_confirmation":
            # An opt-out stops our outreach. A confirmation of a booking the person
            # has just made themselves is not outreach, so it still goes.
            cancel_reason = "unsubscribed"
        elif message["template"] == "followup" and (lead["booked_at"] or lead["outcome"] or booked_elsewhere):
            cancel_reason = "no_longer_needed"
        elif message["template"] == "followup" and not lead["first_contacted_at"] and not first_touch_open:
            # The lead never heard from us and no first email is on its way (it failed,
            # was cancelled or never existed). A nudge must not be the first contact.
            cancel_reason = "first_touch_not_delivered"
        elif message["template"] == "followup" and superseded:
            cancel_reason = "superseded"
        elif message["template"] == "booking_confirmation" and appointment is None:
            cancel_reason = "appointment_cancelled"
        if cancel_reason:
            with self.db.transaction() as conn:
                conn.execute(
                    "UPDATE outbox_messages SET status = 'cancelled', last_error = ?, locked_at = NULL WHERE id = ?",
                    (cancel_reason, message["id"]),
                )
                if lead is not None:
                    add_lead_event(
                        conn, lead["id"], message["tenant_id"], EV_EMAIL_CANCELLED, now,
                        {"reason": cancel_reason, "count": 1, "templates": [message["template"]]},
                    )
            return "cancelled"

        if message["template"] == "followup":
            # Follow-ups wait for the first email to go out, for a gap after our last
            # email, and for daytime. (The schedule already respects quiet hours; this
            # covers a follow-up that became due late, for example after downtime or a
            # postponement.)
            wait = recently_emailed or first_touch_open
            send_at = defer_past_quiet_hours(tenant, now + FOLLOWUP_MIN_GAP if wait else now)
            if send_at > now:
                with self.db.transaction() as conn:
                    # Held back, not failing: an error left by an earlier attempt would keep
                    # the "email is failing" alert open until this message finally goes out.
                    conn.execute(
                        "UPDATE outbox_messages SET status = 'pending', send_after = ?, attempts = attempts - 1, "
                        "last_error = NULL, locked_at = NULL WHERE id = ?",
                        (iso(send_at), message["id"]),
                    )
                return "postponed"

        rendered = self.renderer.render(
            message["template"], lead=lead, tenant=tenant, slots=slots, appointment=appointment
        )
        # Defence in depth: whatever the templates produced must be storable and
        # sendable (no stray half of an emoji, no control characters), so the content
        # of an email can never be the reason a send cannot be recorded.
        subject, text, html = scrub(rendered.subject), scrub(rendered.text), scrub(rendered.html)
        recipient = message["to_address"]
        sent_subject = subject
        if self.settings.email_redirect_all_to:  # development safety valve
            sent_subject = f"[for {recipient}] {subject}"
            recipient = self.settings.email_redirect_all_to
        headers = {}
        if message["template"] != "booking_confirmation":
            # RFC 8058 one-click unsubscribe; mailbox providers show an "Unsubscribe" button.
            headers["List-Unsubscribe"] = f"<{unsubscribe_url(self.settings.base_url, lead['booking_token'])}>"
            headers["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
        email = OutboundEmail(
            to=recipient,
            subject=sent_subject,
            text=text,
            html=html,
            from_name=one_line(tenant["name"], 80) or "Customer care",
            from_address=self.settings.sender_address,
            reply_to=tenant.get("reply_to_email") or self.settings.email_reply_to or None,
            headers=headers,
        )
        example = undeliverable_example_reason(recipient)
        if example and getattr(self.email_provider, "delivers_externally", False):
            # Demo and documentation leads use example.com. Nobody is there, so a real
            # mail server would only bounce it (and bounces hurt the sending account).
            # The address in Angi's own example may be somebody's: they did not ask.
            return self._record_failure(message, example, permanent=True)
        try:
            result = self.email_provider.send(email)
        except PermanentEmailError as exc:
            return self._record_failure(message, str(exc), permanent=True)
        except Exception as exc:  # noqa: BLE001 - network, auth, provider outage...
            return self._record_failure(message, f"{type(exc).__name__}: {exc}", permanent=False)

        # The email is out. From here on a problem is a bookkeeping problem: it must
        # never be reported as "email is failing", and never cause a second send.
        for attempt in range(1, RECORD_SENT_TRIES + 1):
            try:
                self._record_sent(message, lead, tenant, subject, text, recipient, result)
                return "sent"
            except Exception:  # noqa: BLE001 - typically the database is locked or the disk is full
                log.exception(
                    "sent %s but could not record it (try %d of %d)", message["id"], attempt, RECORD_SENT_TRIES
                )
        try:
            # Last resort: mark only the row, so that at least it is never sent again.
            with self.db.transaction() as conn:
                conn.execute(
                    "UPDATE outbox_messages SET status = 'sent', sent_at = ?, locked_at = NULL, "
                    "last_error = 'sent, but the details could not be recorded' WHERE id = ?",
                    (iso(self.clock()), message["id"]),
                )
        except Exception:  # noqa: BLE001
            # The database cannot be written at all. The row stays 'sending' and
            # requeue_stuck will send it once more: the documented at-least-once case.
            log.exception("%s stays 'sending' and will be sent again", message["id"])
        return "sent"

    def _record_sent(
        self, message: dict, lead: dict, tenant: dict, subject: str, text: str, recipient: str, result: SendResult
    ) -> None:
        """One transaction: the audit copy of what went out, the lead's progress, the timeline."""
        sent_at = self.clock()
        stamp = iso(sent_at)
        with self.db.transaction() as conn:
            conn.execute(
                """UPDATE outbox_messages SET status = 'sent', sent_at = ?, subject = ?, body_text = ?,
                       provider = ?, provider_message_id = ?, last_error = NULL, locked_at = NULL
                   WHERE id = ?""",
                (stamp, subject, text, result.provider, result.message_id, message["id"]),
            )
            conn.execute(
                """UPDATE leads SET
                       first_contacted_at = COALESCE(first_contacted_at, ?),
                       status = CASE WHEN status = 'new' THEN 'contacted' ELSE status END,
                       updated_at = ?
                   WHERE id = ?""",
                (stamp, stamp, lead["id"]),
            )
            data = {
                "message_id": message["id"],
                "template": message["template"],
                "to": recipient,
                "provider": result.provider,
                "attempts": message["attempts"],
            }
            if message["template"] in FIRST_TOUCH_TEMPLATES:
                data["seconds_since_received"] = seconds_between(lead["received_at"], stamp)
            add_lead_event(conn, lead["id"], tenant["id"], EV_EMAIL_SENT, sent_at, data)
            # Sending works again - unless some other message is still between retries.
            # One lead's stuck email must not open and close the alert with every other
            # send: each re-opening would be announced as a new incident.
            still_failing = conn.execute(
                "SELECT 1 FROM outbox_messages WHERE status IN ('pending', 'sending') "
                "AND last_error IS NOT NULL AND id != ? LIMIT 1",
                (message["id"],),
            ).fetchone()
            if still_failing is None:
                resolve_alert(conn, EMAIL_ALERT_KEY, sent_at)

    def _record_failure(self, message: dict, error: str, *, permanent: bool) -> str:
        now = self.clock()
        error = error[:500]
        out_of_attempts = message["attempts"] >= message["max_attempts"]
        # Follow-ups and confirmations are given up once the quick attempts are used.
        # A first response is not: see FIRST_TOUCH_RETRY_WINDOW.
        keep_trying = (
            message["template"] in FIRST_TOUCH_TEMPLATES
            and now - parse_iso(message["created_at"]) < FIRST_TOUCH_RETRY_WINDOW
        )
        exhausted = permanent or (out_of_attempts and not keep_trying)
        with self.db.transaction() as conn:
            if exhausted:
                conn.execute(
                    "UPDATE outbox_messages SET status = 'failed', last_error = ?, locked_at = NULL WHERE id = ?",
                    (error, message["id"]),
                )
                conn.execute(
                    "UPDATE leads SET needs_review = 1, updated_at = ? WHERE id = ?", (iso(now), message["lead_id"])
                )
                add_lead_event(
                    conn, message["lead_id"], message["tenant_id"], EV_EMAIL_FAILED, now,
                    {"message_id": message["id"], "template": message["template"], "error": error,
                     "permanent": permanent, "attempts": message["attempts"]},
                )
                if message["template"] in FIRST_TOUCH_TEMPLATES:
                    # The introduction never arrived, so "just checking in" would make no sense.
                    outbox.cancel_pending(
                        conn, message["lead_id"], now, reason="first_touch_failed", templates=("followup",)
                    )
            else:
                retry_at = now + outbox.retry_delay(message["attempts"])
                conn.execute(
                    "UPDATE outbox_messages SET status = 'pending', send_after = ?, last_error = ?, locked_at = NULL "
                    "WHERE id = ?",
                    (iso(retry_at), error, message["id"]),
                )
                add_lead_event(
                    conn, message["lead_id"], message["tenant_id"], EV_EMAIL_RETRY, now,
                    {"message_id": message["id"], "template": message["template"], "error": error,
                     "attempt": message["attempts"], "retry_at": iso(retry_at)},
                )
            if not permanent:
                # A bad address is the lead's problem; anything else is ours.
                raise_alert(
                    conn,
                    kind="email_send_error",
                    key=EMAIL_ALERT_KEY,
                    # Out of quick attempts means the response-time promise is long
                    # broken, even if a first response is still being retried.
                    severity="critical" if out_of_attempts else "warning",
                    title=f"Outbound email is failing: {error[:160]}",
                    detail={"message_id": message["id"], "lead_id": message["lead_id"], "error": error,
                            "attempts": message["attempts"], "gave_up": exhausted},
                    now=now,
                )
        return "failed" if exhausted else "retry"
