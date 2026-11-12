"""Read models shared by the admin API and the dashboard, plus the one write that
closes the loop for analytics (recording what happened to a lead)."""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from typing import Any

from app import outbox
from app.alerts import resolve_quarantine_alert
from app.repo import EV_OUTCOME, add_lead_event, get_lead, get_scheduled_appointment, get_tenant
from app.util import iso, loads, seconds_between

LEAD_STATUSES = ("new", "contacted", "engaged", "booked", "won", "lost")
_PHONE_TYPED = re.compile(r"[0-9\s()+.\-]+")  # what a phone number looks like when a person types it
_NON_DIGITS = re.compile(r"[^0-9]")


def _contains(term: str) -> str:
    """A LIKE pattern for "contains this text", with LIKE's own wildcards (% and _)
    taken literally: an operator searching for "50%_off" means those characters."""
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _decorate_lead(lead: dict) -> dict:
    lead["interview"] = loads(lead.pop("interview_json", None), [])
    lead["extras"] = loads(lead.pop("extras_json", None), {})
    lead["duplicate_matched_on"] = loads(lead.get("duplicate_matched_on"), [])
    lead["speed_to_lead_seconds"] = seconds_between(lead["received_at"], lead["first_contacted_at"])
    return lead


def list_leads(
    conn: sqlite3.Connection,
    *,
    tenant_id: str | None = None,
    status: str | None = None,
    search: str | None = None,
    duplicates_only: bool = False,
    needs_review: bool = False,
    limit: int = 100,
) -> list[dict]:
    where, params = ["1 = 1"], []
    if tenant_id:
        where.append("l.tenant_id = ?")
        params.append(tenant_id)
    if status:
        where.append("l.status = ?")
        params.append(status)
    if duplicates_only:
        where.append("l.is_duplicate = 1")
    if needs_review:
        where.append("l.needs_review = 1")
    term = (search or "").strip()[:200]  # SQLite refuses a LIKE pattern over 50,000 bytes
    if term:
        fields = ("l.full_name", "l.email", "l.phone", "l.task_name", "l.external_lead_id", "l.id")
        matches = [f"{field} LIKE ? ESCAPE '\\'" for field in fields]
        params.extend([_contains(term)] * len(fields))
        digits = _NON_DIGITS.sub("", term)
        if len(digits) >= 4 and _PHONE_TYPED.fullmatch(term):
            # A phone number typed with punctuation, "(303) 555-0123": compare its digits
            # with the normalised number, however Angi happened to format the original.
            matches.append("l.phone_normalized LIKE ?")
            params.append(f"%{digits}%")
        where.append(f"({' OR '.join(matches)})")
    params.append(max(1, min(limit, 1000)))
    rows = conn.execute(
        f"""SELECT l.*, t.name AS tenant_name, t.slug AS tenant_slug, t.is_test AS tenant_is_test
            FROM leads l JOIN tenants t ON t.id = l.tenant_id
            WHERE {' AND '.join(where)}
            ORDER BY l.received_at DESC, l.created_at DESC LIMIT ?""",
        params,
    )
    return [_decorate_lead(dict(row)) for row in rows]


def lead_detail(conn: sqlite3.Connection, lead_id: str) -> dict[str, Any] | None:
    lead = get_lead(conn, lead_id)
    if lead is None:
        return None
    _decorate_lead(lead)
    events = [dict(r) for r in conn.execute("SELECT * FROM lead_events WHERE lead_id = ? ORDER BY id", (lead_id,))]
    for event in events:
        event["data"] = loads(event.pop("data_json"), {})
    claim = conn.execute("SELECT * FROM credit_claims WHERE lead_id = ?", (lead_id,)).fetchone()
    return {
        "lead": lead,
        "tenant": get_tenant(conn, lead["tenant_id"]),
        "events": events,
        "messages": [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM outbox_messages WHERE lead_id = ? ORDER BY send_after, created_at", (lead_id,)
            )
        ],
        "appointment": get_scheduled_appointment(conn, lead_id),
        "claim": dict(claim) if claim else None,
        "original": get_lead(conn, lead["duplicate_of_lead_id"]) if lead["duplicate_of_lead_id"] else None,
        "duplicates": [
            dict(r)
            for r in conn.execute(
                "SELECT id, full_name, received_at, task_name FROM leads WHERE duplicate_of_lead_id = ? "
                "ORDER BY received_at",
                (lead_id,),
            )
        ],
        "webhook_events": [
            dict(r)
            for r in conn.execute(
                "SELECT id, received_at, status, status_reason, detected_format, parse_mode, processing_ms, raw_body "
                "FROM webhook_events WHERE lead_id = ? ORDER BY received_at",
                (lead_id,),
            )
        ],
    }


def list_events(conn: sqlite3.Connection, *, status: str | None = None, limit: int = 100) -> list[dict]:
    where, params = ["1 = 1"], []
    if status:
        where.append("e.status = ?")
        params.append(status)
    params.append(max(1, min(limit, 1000)))
    rows = conn.execute(
        f"""SELECT e.id, e.received_at, e.status, e.status_reason, e.detected_format, e.parse_mode,
                   e.tenant_id, e.lead_id, e.attempts, e.processing_ms, e.remote_addr, e.issues_json,
                   substr(e.raw_body, 1, 400) AS body_preview, t.name AS tenant_name
            FROM webhook_events e LEFT JOIN tenants t ON t.id = e.tenant_id
            WHERE {' AND '.join(where)}
            ORDER BY e.received_at DESC LIMIT ?""",
        params,
    )
    events = [dict(row) for row in rows]
    for event in events:
        event["issues"] = loads(event.pop("issues_json"), {})
    return events


def event_detail(conn: sqlite3.Connection, event_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM webhook_events WHERE id = ?", (event_id,)).fetchone()
    if row is None:
        return None
    event = dict(row)
    event["issues"] = loads(event.pop("issues_json"), {})
    event["headers"] = loads(event.pop("headers_json"), {})
    return event


DISCARDABLE_STATUSES = ("quarantined", "rejected", "failed")


def discard_event(conn: sqlite3.Connection, event_id: str, now: datetime) -> bool:
    """Operator action: this stored request will never become a lead (a ping, junk, a
    test). It leaves the quarantine, so the backlog check can clear; the raw body
    stays on record. Returns False if the event is not in a discardable state."""
    marks = ",".join("?" * len(DISCARDABLE_STATUSES))
    before = conn.execute("SELECT status, status_reason FROM webhook_events WHERE id = ?", (event_id,)).fetchone()
    cursor = conn.execute(
        f"UPDATE webhook_events SET status = 'discarded', processed_at = ? WHERE id = ? AND status IN ({marks})",
        (iso(now), event_id, *DISCARDABLE_STATUSES),
    )
    if cursor.rowcount and before["status"] == "quarantined":
        resolve_quarantine_alert(conn, before["status_reason"], now)  # nothing may be waiting for it any more
    return cursor.rowcount > 0


def payload_shapes(conn: sqlite3.Connection) -> list[dict]:
    shapes = [dict(r) for r in conn.execute("SELECT * FROM payload_shapes ORDER BY is_clean, last_seen_at DESC")]
    for shape in shapes:
        shape["detail"] = loads(shape.pop("detail_json"), {})
    return shapes


def record_outcome(
    conn: sqlite3.Connection,
    lead_id: str,
    *,
    outcome: str,
    now: datetime,
    job_value_cents: int | None = None,
    reason: str | None = None,
) -> dict | None:
    """What finally happened to the lead (reported by the tenant's field-service
    system, or entered by hand). This is what turns "booked" into revenue and ROI."""
    if outcome not in ("won", "lost"):
        raise ValueError("outcome must be 'won' or 'lost'")
    if job_value_cents is not None and job_value_cents < 0:
        raise ValueError("job value cannot be negative")
    lead = get_lead(conn, lead_id)
    if lead is None:
        return None
    stamp = iso(now)
    if outcome == "lost":
        value = None  # a job that was not done earned nothing
    elif job_value_cents is None:
        # "Won" reported again without an amount (say, to add a note): keep the value
        # already on record instead of wiping the revenue.
        value = lead["job_value_cents"]
    else:
        value = job_value_cents
    conn.execute(
        """UPDATE leads SET outcome = ?, outcome_reason = ?, job_value_cents = ?, closed_at = ?,
               status = ?, updated_at = ? WHERE id = ?""",
        (outcome, reason, value, stamp, outcome, stamp, lead_id),
    )
    add_lead_event(
        conn, lead_id, lead["tenant_id"], EV_OUTCOME, now,
        {"outcome": outcome, "job_value_cents": job_value_cents, "reason": reason},
    )
    # The lead is closed either way: stop nudging them.
    outbox.cancel_pending(conn, lead_id, now, reason=f"lead_{outcome}", templates=("followup",))
    return get_lead(conn, lead_id)
