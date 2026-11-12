"""Self-service appointment booking.

The intro email links to ``/book/{token}``; the lead picks an arrival window and is
booked without anyone at the tenant lifting a finger. Availability here is generated
from the tenant's business hours (one job per window). In production this module is the
seam where a real scheduling system (ServiceTitan, Housecall Pro, a dispatch board)
plugs in: replace ``generate_slots`` and the insert in ``book_appointment``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app import outbox
from app.repo import (
    EV_BOOKED,
    EV_LINK_CLICKED,
    EV_RESCHEDULED,
    add_lead_event,
    get_scheduled_appointment,
)
from app.util import iso, new_id, parse_iso

MIN_LEAD_MINUTES = 120  # do not offer a window that starts in less than two hours
DAYS_OFFERED = 5  # how many business days of availability to show
SEARCH_HORIZON_DAYS = 21
CLOSED_WEEKDAYS = {6}  # Sunday


class BookingError(Exception):
    """The requested slot cannot be booked (taken, in the past, or not a real slot)."""


@dataclass(frozen=True)
class Slot:
    starts_at: datetime  # UTC
    ends_at: datetime  # UTC
    key: str  # iso(starts_at); the value posted back by the booking form
    day_label: str  # "Thursday, November 12"
    time_label: str  # "8:00 - 10:00 AM"
    label: str  # "Thu, Nov 12, 8:00 - 10:00 AM"


def tenant_zone(tenant: dict) -> ZoneInfo:
    try:
        return ZoneInfo(tenant["timezone"])
    except (ZoneInfoNotFoundError, ValueError, KeyError, OSError):  # OSError: e.g. "America" is a directory
        return ZoneInfo("UTC")


def _clock(dt: datetime) -> tuple[str, str]:
    hour = dt.hour % 12 or 12
    return f"{hour}:{dt.minute:02d}", "AM" if dt.hour < 12 else "PM"


def format_window(start_local: datetime, end_local: datetime) -> str:
    start_text, start_ampm = _clock(start_local)
    end_text, end_ampm = _clock(end_local)
    if start_ampm == end_ampm:
        return f"{start_text} - {end_text} {end_ampm}"
    return f"{start_text} {start_ampm} - {end_text} {end_ampm}"


def make_slot(start_local: datetime, end_local: datetime) -> Slot:
    starts_at = start_local.astimezone(UTC)
    window = format_window(start_local, end_local)
    return Slot(
        starts_at=starts_at,
        ends_at=end_local.astimezone(UTC),
        key=iso(starts_at),
        day_label=f"{start_local:%A}, {start_local:%B} {start_local.day}",
        time_label=window,
        label=f"{start_local:%a}, {start_local:%b} {start_local.day}, {window}",
    )


def slot_from_appointment(appointment: dict) -> Slot:
    zone = ZoneInfo(appointment["timezone"])
    return make_slot(
        parse_iso(appointment["starts_at"]).astimezone(zone),
        parse_iso(appointment["ends_at"]).astimezone(zone),
    )


def generate_slots(tenant: dict, now: datetime, taken: set[str] | None = None) -> list[Slot]:
    """Arrival windows inside business hours for the next few business days,
    in the tenant's own timezone, skipping windows that are already booked."""
    taken = taken or set()
    zone = tenant_zone(tenant)
    local_now = now.astimezone(zone)
    earliest = local_now + timedelta(minutes=MIN_LEAD_MINUTES)
    length = timedelta(minutes=int(tenant["appointment_minutes"]))
    open_hour, close_hour = int(tenant["business_hours_start"]), int(tenant["business_hours_end"])

    slots: list[Slot] = []
    days_with_slots = 0
    first_day: date = local_now.date()
    for offset in range(SEARCH_HORIZON_DAYS):
        day = first_day + timedelta(days=offset)
        if day.weekday() in CLOSED_WEEKDAYS:
            continue
        closing = datetime.combine(day, time(0), tzinfo=zone) + timedelta(hours=close_hour)
        start_local = datetime.combine(day, time(open_hour), tzinfo=zone)
        day_slots: list[Slot] = []
        while start_local + length <= closing:
            slot = make_slot(start_local, start_local + length)
            if start_local >= earliest and slot.key not in taken:
                day_slots.append(slot)
            start_local += length
        if day_slots:
            slots.extend(day_slots)
            days_with_slots += 1
            if days_with_slots >= DAYS_OFFERED:
                break
    return slots


def taken_slot_keys(conn: sqlite3.Connection, tenant_id: str) -> set[str]:
    rows = conn.execute(
        "SELECT starts_at FROM appointments WHERE tenant_id = ? AND status = 'scheduled'", (tenant_id,)
    )
    return {r["starts_at"] for r in rows}


def available_slots(conn: sqlite3.Connection, tenant: dict, now: datetime) -> list[Slot]:
    return generate_slots(tenant, now, taken_slot_keys(conn, tenant["id"]))


CLICK_EVENT_MIN_GAP_SECONDS = 60


def record_link_click(
    conn: sqlite3.Connection, lead: dict, now: datetime, *, user_agent: str = "", preselected_slot: str | None = None
) -> None:
    """The consumer opened their booking link: the first sign of engagement.

    The funnel timestamp is set once. The timeline gets at most one "link opened"
    event per minute per lead, so reloading the page (or a script hammering it) cannot
    grow the database without bound."""
    stamp = iso(now)
    conn.execute(
        """UPDATE leads SET
               first_engaged_at = COALESCE(first_engaged_at, ?),
               status = CASE WHEN status IN ('new', 'contacted') THEN 'engaged' ELSE status END,
               updated_at = ?
           WHERE id = ?""",
        (stamp, stamp, lead["id"]),
    )
    recent = conn.execute(
        "SELECT 1 FROM lead_events WHERE lead_id = ? AND type = ? AND occurred_at > ? LIMIT 1",
        (lead["id"], EV_LINK_CLICKED, iso(now - timedelta(seconds=CLICK_EVENT_MIN_GAP_SECONDS))),
    ).fetchone()
    if recent is None:
        add_lead_event(
            conn, lead["id"], lead["tenant_id"], EV_LINK_CLICKED, now,
            {"user_agent": user_agent[:200], "preselected_slot": (preselected_slot or "")[:40] or None},
        )


def book_appointment(
    conn: sqlite3.Connection, *, lead: dict, tenant: dict, slot_key: str, now: datetime, max_attempts: int = 5
) -> dict:
    """Book (or re-book) the lead into a slot. Must run inside a write transaction.

    The slot is validated against freshly generated availability, so a tampered form
    cannot book 3 AM on a Sunday, and the unique index on (tenant, starts_at) is the
    final guard against two leads grabbing the same window at the same instant.
    """
    previous = get_scheduled_appointment(conn, lead["id"])
    if previous and previous["starts_at"] == slot_key:
        return previous  # same choice submitted twice (double click / refresh): nothing to do

    slots = {slot.key: slot for slot in available_slots(conn, tenant, now)}
    slot = slots.get(slot_key)
    if slot is None:
        raise BookingError("That time is no longer available. Please pick another.")

    if previous:
        conn.execute("UPDATE appointments SET status = 'cancelled' WHERE id = ?", (previous["id"],))
        # A confirmation for the old time that has not gone out yet would now be wrong.
        outbox.cancel_pending(conn, lead["id"], now, reason="rescheduled", templates=("booking_confirmation",))

    appointment_id = new_id("appt")
    try:
        conn.execute(
            """INSERT INTO appointments (id, lead_id, tenant_id, starts_at, ends_at, timezone, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 'scheduled', ?)""",
            (
                appointment_id,
                lead["id"],
                tenant["id"],
                slot.key,
                iso(slot.ends_at),
                tenant_zone(tenant).key,
                iso(now),
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise BookingError("That time was just taken. Please pick another.") from exc

    stamp = iso(now)
    conn.execute(
        """UPDATE leads SET
               booked_at = COALESCE(booked_at, ?),
               first_engaged_at = COALESCE(first_engaged_at, ?),
               status = CASE WHEN status IN ('won', 'lost') THEN status ELSE 'booked' END,
               updated_at = ?
           WHERE id = ?""",
        (stamp, stamp, stamp, lead["id"]),
    )
    add_lead_event(
        conn,
        lead["id"],
        tenant["id"],
        EV_RESCHEDULED if previous else EV_BOOKED,
        now,
        {"appointment_id": appointment_id, "starts_at": slot.key, "label": slot.label},
    )
    # The nudges have done their job; confirm the booking instead.
    outbox.cancel_pending(conn, lead["id"], now, reason="booked", templates=("followup",))
    if lead.get("email_normalized"):
        outbox.enqueue(
            conn,
            lead_id=lead["id"],
            tenant_id=tenant["id"],
            template="booking_confirmation",
            to_address=lead["email_normalized"],
            dedupe_key=f"{lead['id']}:booking_confirmation:{appointment_id}",
            send_after=now,
            now=now,
            max_attempts=max_attempts,
        )
    row = conn.execute("SELECT * FROM appointments WHERE id = ?", (appointment_id,)).fetchone()
    return dict(row)
