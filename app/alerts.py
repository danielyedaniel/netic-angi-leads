"""Alerts: one open row per distinct problem, plus pluggable notification channels.

Raising an alert is just a database write inside whatever transaction noticed the
problem, so it can never slow down or break lead intake. The background worker then
announces alerts (log line always; Slack and email when configured).

The life of an alert:

* A repeat of a problem that is already open is folded into its row (``count`` goes
  up); nobody is paged twice for the same thing.
* It is announced when it opens, again if it gets worse (warning -> critical), and as
  a reminder at most once a day for as long as it keeps happening.
* Health-check alerts (key ``check:<name>``) are closed by the monitor when the check
  passes again. Alerts raised by a single event (a rejected request, a quarantined
  lead) have no "passes again", so they are closed after a day without a new
  occurrence. The next occurrence then opens a new alert and is announced as news,
  instead of being folded silently into a row somebody was told about weeks ago.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Any

import httpx

from app.config import Settings
from app.db import Database
from app.mailer.providers import EmailProvider, OutboundEmail
from app.util import dumps, iso, loads, one_line

log = logging.getLogger("app.alerts")

SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}
CHECK_KEY_PREFIX = "check:"  # alerts owned by a health check (app/monitoring.py)
QUIET_BEFORE_CLOSING = timedelta(hours=24)  # an event alert with no new occurrence for this long is over
REMIND_EVERY = timedelta(hours=24)  # an alert that keeps happening is announced again this often
ANNOUNCE_BATCH = 25  # alerts announced per pass, so an alert storm cannot hold up lead email


def raise_alert(
    conn: sqlite3.Connection,
    *,
    kind: str,
    key: str,
    severity: str,
    title: str,
    detail: dict[str, Any] | None,
    now: datetime,
    count_occurrence: bool = True,
) -> bool:
    """Open an alert, or fold this occurrence into the one already open for ``key``.
    Returns True when a new alert was opened (i.e. somebody should be told)."""
    stamp = iso(now)
    title = one_line(title, 300)  # titles end up in log lines and email subjects
    row = conn.execute(
        "SELECT id, severity FROM alerts WHERE dedupe_key = ? AND status = 'open'", (key,)
    ).fetchone()
    if row is not None:
        # Keep the worst severity seen while the alert has been open. If it just got
        # worse (a warning became critical), announce it again.
        worst = max(severity, row["severity"], key=lambda s: SEVERITY_RANK.get(s, 0))
        escalated = SEVERITY_RANK.get(worst, 0) > SEVERITY_RANK.get(row["severity"], 0)
        conn.execute(
            "UPDATE alerts SET count = count + ?, last_seen_at = ?, title = ?, detail_json = ?, severity = ?, "
            "notified_at = CASE WHEN ? THEN NULL ELSE notified_at END WHERE id = ?",
            (1 if count_occurrence else 0, stamp, title, dumps(detail or {}), worst, 1 if escalated else 0, row["id"]),
        )
        return False
    conn.execute(
        """INSERT INTO alerts (kind, dedupe_key, severity, title, detail_json, status, count, first_seen_at, last_seen_at)
           VALUES (?, ?, ?, ?, ?, 'open', 1, ?, ?)""",
        (kind, key, severity, title, dumps(detail or {}), stamp, stamp),
    )
    return True


def resolve_alert(conn: sqlite3.Connection, key: str, now: datetime) -> bool:
    cursor = conn.execute(
        "UPDATE alerts SET status = 'resolved', resolved_at = ? WHERE dedupe_key = ? AND status = 'open'",
        (iso(now), key),
    )
    return cursor.rowcount > 0


def resolve_quarantine_alert(conn: sqlite3.Connection, reason: str | None, now: datetime) -> None:
    """An event has just left the quarantine (processed, a duplicate, discarded). The two
    alerts that are not about one Angi account say "leads are waiting for an operator",
    so they are over when no event is waiting for that reason any more."""
    if reason in ("no_account_id", "unrecognised_payload") and conn.execute(
        "SELECT 1 FROM webhook_events WHERE status = 'quarantined' AND status_reason = ? LIMIT 1", (reason,)
    ).fetchone() is None:
        resolve_alert(conn, reason, now)


def resolve_alert_by_id(conn: sqlite3.Connection, alert_id: int, now: datetime) -> bool:
    cursor = conn.execute(
        "UPDATE alerts SET status = 'resolved', resolved_at = ? WHERE id = ? AND status = 'open'",
        (iso(now), alert_id),
    )
    return cursor.rowcount > 0


def resolve_quiet_event_alerts(conn: sqlite3.Connection, now: datetime) -> int:
    """Close event-raised alerts that have not happened again for a day. Nothing ever
    reports "the rejected requests have stopped", so without this such an alert would
    stay open for good and a new incident months later would only bump its counter.
    Check alerts are left alone: their check decides when they are over."""
    cursor = conn.execute(
        "UPDATE alerts SET status = 'resolved', resolved_at = ? "
        "WHERE status = 'open' AND last_seen_at <= ? AND notified_at IS NOT NULL "
        "AND substr(dedupe_key, 1, ?) != ?",
        (iso(now), iso(now - QUIET_BEFORE_CLOSING), len(CHECK_KEY_PREFIX), CHECK_KEY_PREFIX),
    )
    return cursor.rowcount


def inherit_announcement(conn: sqlite3.Connection, key: str, closed_after: str) -> None:
    """For a check alert that was just re-opened. If the previous alert for the same
    check was closed after ``closed_after`` (the last monitor run), the check never
    passed in between: an operator closed the alert by hand while the problem was
    still there. That is one ongoing incident, so the new row takes over the old
    row's announcement time instead of paging again a minute after every click on
    "resolve". (The daily reminder still applies.)"""
    previous = conn.execute(
        "SELECT notified_at FROM alerts WHERE dedupe_key = ? AND status = 'resolved' AND resolved_at > ? "
        "ORDER BY id DESC LIMIT 1",
        (key, closed_after),
    ).fetchone()
    if previous is not None:
        conn.execute(
            "UPDATE alerts SET notified_at = ? WHERE dedupe_key = ? AND status = 'open'",
            (previous["notified_at"], key),
        )


def list_alerts(conn: sqlite3.Connection, status: str | None = None, limit: int = 200) -> list[dict]:
    sql = "SELECT * FROM alerts"
    params: list[Any] = []
    if status:
        sql += " WHERE status = ?"
        params.append(status)
    sql += (
        " ORDER BY CASE status WHEN 'open' THEN 0 ELSE 1 END,"
        " CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END,"
        " last_seen_at DESC LIMIT ?"
    )
    params.append(limit)
    alerts = [dict(r) for r in conn.execute(sql, params)]
    for alert in alerts:
        alert["detail"] = loads(alert.pop("detail_json"), {})
    return alerts


class AlertNotifier:
    """Announces alerts: a log line always, plus Slack and email when configured.

    An alert only counts as announced once every configured channel has accepted it.
    Until then it is tried again, but at most once per monitor interval: a channel
    that is down costs a timeout on every attempt, on the same thread that sends
    lead email. Channel failures are logged and never propagate."""

    def __init__(self, settings: Settings, email_provider: EmailProvider | None = None) -> None:
        self.settings = settings
        self.email_provider = email_provider
        # Announcements some channel has not accepted yet: (alert id, severity) -> (when
        # to try again, channels that did accept it, so a retry never repeats a message
        # that arrived). The severity is part of the key because an escalation is a new
        # announcement. Kept in memory only: after a restart the worst case is one
        # repeated message.
        self._unfinished: dict[tuple[int, str], tuple[datetime, frozenset[str]]] = {}

    def dispatch(self, db: Database, now: datetime) -> int:
        """Announce every alert that is due. Returns how many were fully announced."""
        with db.read() as conn:
            due = [
                dict(r)
                for r in conn.execute(
                    # Never announced (new, or just escalated), or announced more than a
                    # day ago and seen again since: the daily reminder.
                    "SELECT * FROM alerts WHERE status = 'open' AND (notified_at IS NULL "
                    "OR (notified_at <= ? AND last_seen_at > notified_at)) ORDER BY id",
                    (iso(now - REMIND_EVERY),),
                )
            ]
        still_due = {(alert["id"], alert["severity"]) for alert in due}
        self._unfinished = {key: state for key, state in self._unfinished.items() if key in still_due}

        channels = self._channels()
        announced = 0
        for alert in due:
            key = (alert["id"], alert["severity"])
            retry_at, accepted = self._unfinished.get(key, (now, frozenset()))
            if now < retry_at:
                continue
            if announced == ANNOUNCE_BATCH:
                break
            text = f"[{alert['severity'].upper()}] {alert['title']}"
            if key not in self._unfinished:  # first attempt: the log line always happens
                log.warning("ALERT %s (kind=%s key=%s)", text, alert["kind"], one_line(alert["dedupe_key"], 200))
            for name, send in channels.items():
                if name not in accepted and send(alert, text):
                    accepted = accepted | {name}
            if accepted != frozenset(channels):
                retry_at = now + timedelta(seconds=self.settings.monitor_interval_seconds)
                self._unfinished[key] = (retry_at, accepted)
                break  # a channel is down: do not spend another timeout on each remaining alert
            self._unfinished.pop(key, None)
            with db.transaction() as conn:
                # Not if it escalated while we were sending: that is still to be announced.
                conn.execute(
                    "UPDATE alerts SET notified_at = ? WHERE id = ? AND severity = ?",
                    (iso(now), alert["id"], alert["severity"]),
                )
            announced += 1
        return announced

    def _channels(self) -> dict[str, Any]:
        """The external channels that are configured, by name."""
        channels: dict[str, Any] = {}
        if self.settings.alert_slack_webhook_url:
            channels["slack"] = self._post_to_slack
        if self.settings.alert_email_to and self.email_provider is not None:
            channels["email"] = self._send_email
        return channels

    def _dashboard_url(self) -> str:
        return f"{self.settings.base_url}/dashboard/monitoring"

    def _post_to_slack(self, alert: dict, text: str) -> bool:
        # Alert titles carry text from the lead (a business name, an account id). Slack
        # reads "<!channel>" and "<https://...|label>" as markup, so the three control
        # characters are escaped the way Slack documents.
        safe_text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        try:
            httpx.post(
                self.settings.alert_slack_webhook_url,
                json={"text": f"{safe_text}\n{self._dashboard_url()}"},
                timeout=5.0,
            ).raise_for_status()
        except Exception as exc:  # noqa: BLE001
            # Only the kind of error: httpx puts the request URL in its messages, and
            # the Slack webhook URL is a secret.
            log.error("could not post alert %s to Slack (%s)", alert["id"], type(exc).__name__)
            return False
        return True

    def _send_email(self, alert: dict, text: str) -> bool:
        try:
            self.email_provider.send(
                OutboundEmail(
                    to=self.settings.alert_email_to,
                    subject=one_line(f"[Angi integration] {text}", 180),
                    text=f"{text}\n\nDetails: {alert.get('detail_json') or '{}'}\n\n{self._dashboard_url()}\n",
                    html=None,
                    from_name="Lead integration alerts",
                    from_address=self.settings.sender_address,
                )
            )
        except Exception as exc:  # noqa: BLE001
            log.error("could not email alert %s (%s: %s)", alert["id"], type(exc).__name__, exc)
            return False
        return True
