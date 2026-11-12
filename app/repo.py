"""Small query helpers shared by the services, plus the lead-event vocabulary."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from app.util import dumps, iso

# --- lead lifecycle events (append-only log in `lead_events`) ---------------------------
EV_RECEIVED = "lead.received"
EV_DUPLICATE = "lead.duplicate_detected"
EV_REDELIVERED = "webhook.redelivered"
EV_UPDATED = "lead.updated"
EV_EMAIL_QUEUED = "email.queued"
EV_EMAIL_SENT = "email.sent"
EV_EMAIL_RETRY = "email.retry_scheduled"
EV_EMAIL_FAILED = "email.failed"
EV_EMAIL_SKIPPED = "email.skipped"
EV_EMAIL_CANCELLED = "email.cancelled"
EV_LINK_CLICKED = "booking.link_clicked"
EV_BOOKED = "booking.appointment_booked"
EV_RESCHEDULED = "booking.rescheduled"
EV_UNSUBSCRIBED = "lead.unsubscribed"
EV_OUTCOME = "lead.outcome_recorded"


def add_lead_event(
    conn: sqlite3.Connection,
    lead_id: str,
    tenant_id: str | None,
    type_: str,
    now: datetime,
    data: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        "INSERT INTO lead_events (lead_id, tenant_id, type, occurred_at, data_json) VALUES (?, ?, ?, ?, ?)",
        (lead_id, tenant_id, type_, iso(now), dumps(data) if data else None),
    )


def get_tenant(conn: sqlite3.Connection, tenant_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
    return dict(row) if row else None


def get_tenant_by_slug(conn: sqlite3.Connection, slug: str) -> dict | None:
    row = conn.execute("SELECT * FROM tenants WHERE slug = ?", (slug,)).fetchone()
    return dict(row) if row else None


def get_tenant_by_webhook_key(conn: sqlite3.Connection, key: str) -> dict | None:
    row = conn.execute("SELECT * FROM tenants WHERE webhook_key = ?", (key,)).fetchone()
    return dict(row) if row else None


def get_lead(conn: sqlite3.Connection, lead_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
    return dict(row) if row else None


def get_lead_by_token(conn: sqlite3.Connection, token: str) -> dict | None:
    row = conn.execute("SELECT * FROM leads WHERE booking_token = ?", (token,)).fetchone()
    return dict(row) if row else None


def get_scheduled_appointment(conn: sqlite3.Connection, lead_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM appointments WHERE lead_id = ? AND status = 'scheduled'", (lead_id,)
    ).fetchone()
    return dict(row) if row else None


def is_suppressed(conn: sqlite3.Connection, tenant_id: str, email_normalized: str | None) -> bool:
    if not email_normalized:
        return False
    row = conn.execute(
        "SELECT 1 FROM email_suppressions WHERE tenant_id = ? AND email_normalized = ?",
        (tenant_id, email_normalized),
    ).fetchone()
    return row is not None


def kv_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def kv_set(conn: sqlite3.Connection, key: str, value: str, now: datetime) -> None:
    conn.execute(
        "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, value, iso(now)),
    )
