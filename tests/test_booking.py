"""Self-service booking (app/booking.py): which arrival windows are offered, and what
happens when a consumer books, re-books, double-clicks, races another consumer or
tampers with the form.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.booking import (
    DAYS_OFFERED,
    MIN_LEAD_MINUTES,
    BookingError,
    book_appointment,
    generate_slots,
    record_link_click,
    tenant_zone,
)
from app.util import iso
from tests.conftest import ADMIN, START, SUMMIT_ADS_ACCOUNT, ads_payload, db_one, db_rows, post_lead

INDIANAPOLIS = ZoneInfo("America/Indiana/Indianapolis")
DENVER = ZoneInfo("America/Denver")

# Sparkle's first open windows at START (Thu 2026-11-12 10:00 in Indianapolis, UTC-5).
THU_NOON = "2026-11-12T17:00:00.000Z"  # Thu 12:00 - 2:00 PM
THU_2PM = "2026-11-12T19:00:00.000Z"  # Thu 2:00 - 4:00 PM
FRI_8AM = "2026-11-13T13:00:00.000Z"  # Fri 8:00 - 10:00 AM


# --- helpers ---------------------------------------------------------------------------


def tenant(**overrides) -> dict:
    """The tenant columns slot generation reads (defaults = Sparkle's)."""
    fields = {
        "id": "tnt_test",
        "timezone": "America/Indiana/Indianapolis",
        "business_hours_start": 8,
        "business_hours_end": 17,
        "appointment_minutes": 120,
    }
    fields.update(overrides)
    return fields


def consumer(n: int) -> dict[str, str]:
    """Contact details no other lead in the test shares, so the lead is not a duplicate."""
    return {"Email": f"consumer{n}@example.com", "PhoneNumber": f"317555{1000 + n:04d}", "FirstName": f"Lead{n}"}


def new_lead(client, ctx, **overrides) -> dict:
    response = post_lead(client, ads_payload(**overrides))
    assert response.json()["result"] == "processed", response.text
    return lead_row(ctx, response.json()["leadId"])


def lead_row(ctx, lead_id: str) -> dict:
    return db_one(ctx, "SELECT * FROM leads WHERE id = ?", (lead_id,))


def appointments(ctx, lead_id: str) -> list[dict]:
    return db_rows(ctx, "SELECT * FROM appointments WHERE lead_id = ? ORDER BY created_at, id", (lead_id,))


def messages(ctx, lead_id: str, template: str) -> list[dict]:
    return db_rows(
        ctx,
        "SELECT * FROM outbox_messages WHERE lead_id = ? AND template = ? ORDER BY sequence, created_at",
        (lead_id, template),
    )


def lead_events(ctx, lead_id: str, event_type: str) -> list[dict]:
    rows = db_rows(
        ctx, "SELECT data_json FROM lead_events WHERE lead_id = ? AND type = ? ORDER BY id", (lead_id, event_type)
    )
    return [json.loads(row["data_json"] or "{}") for row in rows]


def submit(client, lead: dict, slot: str):
    """Submit the booking form the way a browser does (without following the redirect)."""
    return client.post(f"/book/{lead['booking_token']}", data={"slot": slot}, follow_redirects=False)


def local_days(slots, zone: ZoneInfo) -> list:
    return sorted({slot.starts_at.astimezone(zone).date() for slot in slots})


# --- generate_slots: what is offered ---------------------------------------------------


def test_windows_lie_inside_business_hours_in_the_tenants_timezone():
    # Rule: 8:00-17:00 means the tenant's wall clock, not UTC and not the server's zone.
    slots = generate_slots(tenant(), START)

    assert slots
    for slot in slots:
        start, end = slot.starts_at.astimezone(INDIANAPOLIS), slot.ends_at.astimezone(INDIANAPOLIS)
        assert (start.hour, start.minute) in {(8, 0), (10, 0), (12, 0), (14, 0)}
        assert start.date() == end.date()
        assert end.hour < 17 or (end.hour, end.minute) == (17, 0)
        assert slot.ends_at - slot.starts_at == timedelta(minutes=120)
        assert slot.starts_at.utcoffset() == timedelta(0) and slot.key == iso(slot.starts_at)
    assert slots == sorted(slots, key=lambda s: s.starts_at)
    assert len({slot.key for slot in slots}) == len(slots)


def test_same_instant_gives_different_slots_for_tenants_in_different_timezones():
    # Rule: each tenant's day starts at its own 8 AM.
    tokyo = generate_slots(tenant(timezone="Asia/Tokyo"), START)  # START is 00:00 Fri Nov 13 in Tokyo
    denver = generate_slots(tenant(timezone="America/Denver"), START)  # START is 08:00 Thu Nov 12 in Denver

    assert tokyo[0].key == "2026-11-12T23:00:00.000Z"
    assert tokyo[0].label == "Fri, Nov 13, 8:00 - 10:00 AM"
    assert denver[0].key == "2026-11-12T17:00:00.000Z"
    assert denver[0].label == "Thu, Nov 12, 10:00 AM - 12:00 PM"


def test_nothing_starts_sooner_than_the_minimum_lead_time():
    # Rule: a technician needs two hours' notice; exactly two hours is still bookable.
    assert MIN_LEAD_MINUTES == 120
    at_ten = generate_slots(tenant(), START)  # 10:00 local
    a_minute_later = generate_slots(tenant(), START + timedelta(minutes=1))

    assert at_ten[0].key == THU_NOON  # 12:00 is exactly 120 minutes away
    assert a_minute_later[0].key == THU_2PM
    for now, slots in ((START, at_ten), (START + timedelta(minutes=1), a_minute_later)):
        assert all(slot.starts_at >= now + timedelta(minutes=MIN_LEAD_MINUTES) for slot in slots)


def test_sundays_are_skipped_and_exactly_five_days_are_offered():
    # Rule: five business days of availability, Monday to Saturday.
    slots = generate_slots(tenant(), START)

    days = local_days(slots, INDIANAPOLIS)
    assert DAYS_OFFERED == 5
    assert [day.isoformat() for day in days] == [
        "2026-11-12", "2026-11-13", "2026-11-14", "2026-11-16", "2026-11-17",
    ]
    assert all(day.weekday() != 6 for day in days)
    # Today only has the two windows that are still two hours away; the other days have all four.
    assert len(slots) == 2 + 4 * 4


@pytest.mark.parametrize(
    ("now", "first_day"),
    [
        (datetime(2026, 11, 15, 1, 0, tzinfo=UTC), "2026-11-16"),  # Sat 20:00 local: today is over
        (datetime(2026, 11, 15, 14, 0, tzinfo=UTC), "2026-11-16"),  # Sun 09:00 local: closed today
        (datetime(2026, 11, 13, 3, 0, tzinfo=UTC), "2026-11-13"),  # Thu 22:00 local (already Fri in UTC)
        (datetime(2026, 11, 12, 19, 0, 1, tzinfo=UTC), "2026-11-13"),  # Thu 14:00:01: last window too soon
    ],
)
def test_first_offered_day_depends_on_local_time_not_utc(now, first_day):
    # Rule: "today" is the tenant's calendar day, and a day with nothing left is skipped.
    slots = generate_slots(tenant(), now)

    days = local_days(slots, INDIANAPOLIS)
    assert days[0].isoformat() == first_day
    assert len(days) == DAYS_OFFERED
    assert all(day.weekday() != 6 for day in days)
    assert all(slot.starts_at >= now + timedelta(minutes=MIN_LEAD_MINUTES) for slot in slots)


def test_taken_slots_are_not_offered():
    # Rule: one job per window.
    everything = generate_slots(tenant(), START)
    taken = {everything[0].key, everything[5].key}

    remaining = generate_slots(tenant(), START, taken)

    assert [slot.key for slot in remaining] == [slot.key for slot in everything if slot.key not in taken]
    assert len(remaining) == len(everything) - 2


def test_a_fully_booked_day_is_replaced_by_the_next_business_day():
    # Rule: always show five days that actually have openings.
    everything = generate_slots(tenant(), START)
    friday = {s.key for s in everything if s.starts_at.astimezone(INDIANAPOLIS).date().isoformat() == "2026-11-13"}
    assert len(friday) == 4

    remaining = generate_slots(tenant(), START, friday)

    assert [day.isoformat() for day in local_days(remaining, INDIANAPOLIS)] == [
        "2026-11-12", "2026-11-14", "2026-11-16", "2026-11-17", "2026-11-18",
    ]


def test_search_stops_at_the_three_week_horizon_when_everything_is_taken():
    # Rule: a full calendar yields "no times" instead of searching ever further ahead.
    taken: set[str] = set()
    for _ in range(10):  # each pass books out the five days currently on offer
        offered = generate_slots(tenant(), START, taken)
        if not offered:
            break
        taken |= {slot.key for slot in offered}

    assert generate_slots(tenant(), START, taken) == []
    # 21 days from Thu Nov 12 = up to Wed Dec 2, minus three Sundays = 18 business days.
    booked_days = sorted({datetime.fromisoformat(key.replace("Z", "+00:00")).astimezone(INDIANAPOLIS).date() for key in taken})
    assert len(booked_days) == 18
    assert booked_days[-1].isoformat() == "2026-12-02"
    assert len(taken) == 2 + 17 * 4


@pytest.mark.parametrize(
    ("start", "end", "minutes"),
    [(8, 9, 120), (8, 17, 541), (0, 24, 1500), (16, 17, 61)],
)
def test_a_day_with_no_room_for_a_window_yields_no_slots(start, end, minutes):
    # Rule: an arrival window longer than the working day means nothing to offer (and no hang).
    slots = generate_slots(
        tenant(business_hours_start=start, business_hours_end=end, appointment_minutes=minutes), START
    )

    assert slots == []


@pytest.mark.parametrize("bad_timezone", ["Not/AZone", "", "../../etc/passwd", "America/Denver\n", "Mars/Olympus"])
def test_invalid_timezone_falls_back_to_utc(bad_timezone):
    # Rule: a typo in a tenant's timezone must not take the booking page down.
    assert tenant_zone({"timezone": bad_timezone}).key == "UTC"
    assert tenant_zone({}).key == "UTC"

    slots = generate_slots(tenant(timezone=bad_timezone), START)

    assert slots == generate_slots(tenant(timezone="UTC"), START)
    assert slots[0].key == "2026-11-13T08:00:00.000Z"  # 08:00 UTC tomorrow (today's windows are too soon)


def test_timezone_that_is_a_zoneinfo_directory_falls_back_to_utc():
    # Rule: same as above for the "forgot the city" typo: "America" instead of "America/Denver".
    assert tenant_zone({"timezone": "America"}).key == "UTC"


@pytest.mark.parametrize(
    ("minutes", "starts"),
    [
        (90, ["8:00", "9:30", "11:00", "12:30", "14:00", "15:30"]),
        (100, ["8:00", "9:40", "11:20", "13:00", "14:40"]),
        (45, ["8:00", "8:45", "9:30", "10:15", "11:00", "11:45", "12:30", "13:15", "14:00", "14:45", "15:30", "16:15"]),
        (240, ["8:00", "12:00"]),
        (540, ["8:00"]),
        (7, None),
    ],
)
def test_window_length_that_does_not_divide_the_day_never_runs_past_closing(minutes, starts):
    # Rule: the last window must END by closing time; a leftover stub of the day is not offered.
    now = datetime(2026, 11, 12, 5, 0, tzinfo=UTC)  # midnight local, so the whole of Thursday is open
    slots = generate_slots(tenant(appointment_minutes=minutes), now)

    thursday = [s for s in slots if s.starts_at.astimezone(INDIANAPOLIS).date().isoformat() == "2026-11-12"]
    closing = datetime(2026, 11, 12, 17, 0, tzinfo=INDIANAPOLIS)
    assert all(slot.ends_at <= closing for slot in thursday)
    assert all(slot.ends_at - slot.starts_at == timedelta(minutes=minutes) for slot in slots)
    assert len(thursday) == 540 // minutes
    if starts is not None:
        local_starts = [s.starts_at.astimezone(INDIANAPOLIS) for s in thursday]
        assert [f"{t.hour}:{t.minute:02d}" for t in local_starts] == starts


def test_label_formats():
    # Rule: labels read naturally: AM/PM once when both ends share it, no zero-padded hours or days.
    now = datetime(2026, 11, 12, 5, 0, tzinfo=UTC)  # midnight local
    slots = generate_slots(tenant(), now)

    assert [slot.time_label for slot in slots[:4]] == [
        "8:00 - 10:00 AM",
        "10:00 AM - 12:00 PM",
        "12:00 - 2:00 PM",
        "2:00 - 4:00 PM",
    ]
    assert slots[0].label == "Thu, Nov 12, 8:00 - 10:00 AM"
    assert slots[0].day_label == "Thursday, November 12"
    assert slots[1].label == "Thu, Nov 12, 10:00 AM - 12:00 PM"
    assert slots[0].key == "2026-11-12T13:00:00.000Z"
    assert slots[0].ends_at == datetime(2026, 11, 12, 15, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("overrides", "now", "first_labels"),
    [
        # Half-hour boundaries.
        ({"appointment_minutes": 90}, datetime(2026, 11, 12, 5, 0, tzinfo=UTC),
         ["Thu, Nov 12, 8:00 - 9:30 AM", "Thu, Nov 12, 9:30 - 11:00 AM", "Thu, Nov 12, 11:00 AM - 12:30 PM"]),
        # An evening shift that ends at midnight.
        ({"business_hours_start": 20, "business_hours_end": 24}, datetime(2026, 11, 12, 5, 0, tzinfo=UTC),
         ["Thu, Nov 12, 8:00 - 10:00 PM", "Thu, Nov 12, 10:00 PM - 12:00 AM"]),
        # A night shift that starts at midnight; single-digit day of month is not padded.
        ({"business_hours_start": 0, "business_hours_end": 4}, datetime(2026, 11, 1, 20, 0, tzinfo=UTC),
         ["Mon, Nov 2, 12:00 - 2:00 AM", "Mon, Nov 2, 2:00 - 4:00 AM"]),
    ],
)
def test_label_formats_at_half_hours_noon_and_midnight(overrides, now, first_labels):
    # Rule: 12-hour clock edge cases (12:00 AM / 12:00 PM, :30) are printed correctly.
    slots = generate_slots(tenant(**overrides), now)

    assert [slot.label for slot in slots[: len(first_labels)]] == first_labels


@pytest.mark.parametrize(
    ("now", "expected_days", "saturday_8am_utc", "monday_8am_utc"),
    [
        # Spring forward: Sunday 2026-03-08, MST (UTC-7) -> MDT (UTC-6).
        (
            datetime(2026, 3, 6, 12, 0, tzinfo=UTC),
            ["2026-03-06", "2026-03-07", "2026-03-09", "2026-03-10", "2026-03-11"],
            "2026-03-07T15:00:00.000Z",
            "2026-03-09T14:00:00.000Z",
        ),
        # Fall back: Sunday 2026-11-01, MDT (UTC-6) -> MST (UTC-7).
        (
            datetime(2026, 10, 30, 12, 0, tzinfo=UTC),
            ["2026-10-30", "2026-10-31", "2026-11-02", "2026-11-03", "2026-11-04"],
            "2026-10-31T14:00:00.000Z",
            "2026-11-02T15:00:00.000Z",
        ),
    ],
)
def test_slots_keep_their_wall_clock_hours_across_a_dst_change(now, expected_days, saturday_8am_utc, monday_8am_utc):
    # Rule: 8 AM stays 8 AM on the tenant's clock before and after the clocks change.
    slots = generate_slots(tenant(timezone="America/Denver"), now)

    assert [day.isoformat() for day in local_days(slots, DENVER)] == expected_days
    assert len(slots) == 5 * 4
    for slot in slots:
        start, end = slot.starts_at.astimezone(DENVER), slot.ends_at.astimezone(DENVER)
        assert (start.hour, start.minute) in {(8, 0), (10, 0), (12, 0), (14, 0)}
        assert slot.ends_at - slot.starts_at == timedelta(hours=2)  # real elapsed time
        assert (end.hour - start.hour, end.minute) == (2, 0)  # and on the wall clock
    keys = [slot.key for slot in slots]
    assert len(set(keys)) == len(keys)
    assert saturday_8am_utc in keys and monday_8am_utc in keys
    by_key = {slot.key: slot for slot in slots}
    assert by_key[saturday_8am_utc].time_label == by_key[monday_8am_utc].time_label == "8:00 - 10:00 AM"


# --- opening the booking link ----------------------------------------------------------


def open_link(ctx, lead: dict, now: datetime, **kwargs) -> None:
    with ctx.db.transaction() as conn:
        record_link_click(conn, lead, now, **kwargs)


def test_first_link_click_marks_the_lead_engaged_and_goes_on_the_timeline(client, ctx, clock):
    # Rule: opening the link is the first sign of engagement; the funnel timestamp is set once.
    lead = new_lead(client, ctx, **consumer(1))
    clock.advance(minutes=3)

    open_link(ctx, lead, clock(), user_agent="Mozilla/5.0 (test)", preselected_slot=FRI_8AM)

    row = lead_row(ctx, lead["id"])
    assert (row["status"], row["first_engaged_at"], row["updated_at"]) == ("engaged", iso(clock()), iso(clock()))
    assert lead_events(ctx, lead["id"], "booking.link_clicked") == [
        {"user_agent": "Mozilla/5.0 (test)", "preselected_slot": FRI_8AM}
    ]

    later = clock() + timedelta(days=1)
    open_link(ctx, lead, later)

    assert lead_row(ctx, lead["id"])["first_engaged_at"] == iso(clock())  # not moved by the second visit
    assert lead_events(ctx, lead["id"], "booking.link_clicked")[1] == {"user_agent": "", "preselected_slot": None}


@pytest.mark.parametrize(("seconds_later", "events"), [(0, 1), (1, 1), (59, 1), (60, 2), (3600, 2)])
def test_link_clicks_are_recorded_at_most_once_a_minute_per_lead(client, ctx, seconds_later, events):
    # Rule: a reload within the minute is the same visit; a minute later it is a new one.
    lead = new_lead(client, ctx, **consumer(1))

    open_link(ctx, lead, START)
    open_link(ctx, lead, START + timedelta(seconds=seconds_later))

    assert len(lead_events(ctx, lead["id"], "booking.link_clicked")) == events
    assert lead_row(ctx, lead["id"])["first_engaged_at"] == iso(START)


def test_one_leads_clicks_do_not_use_up_another_leads_minute(client, ctx):
    # Rule: the one-a-minute limit is per lead.
    first = new_lead(client, ctx, **consumer(1))
    second = new_lead(client, ctx, **consumer(2))

    open_link(ctx, first, START)
    open_link(ctx, second, START)

    assert len(lead_events(ctx, first["id"], "booking.link_clicked")) == 1
    assert len(lead_events(ctx, second["id"], "booking.link_clicked")) == 1


def test_link_click_stores_only_a_bounded_slice_of_what_the_browser_sent(client, ctx):
    # Rule: the slot parameter and the user agent come from the internet; neither is stored whole.
    lead = new_lead(client, ctx, **consumer(1))

    open_link(ctx, lead, START, user_agent="U" * 5000, preselected_slot="S" * 60000)

    assert lead_events(ctx, lead["id"], "booking.link_clicked") == [
        {"user_agent": "U" * 200, "preselected_slot": "S" * 40}
    ]


@pytest.mark.parametrize("status", ["booked", "won", "lost"])
def test_link_click_never_moves_a_lead_backwards(client, ctx, status):
    # Rule: status only moves forward; looking at the page again does not undo a booking or an outcome.
    lead = new_lead(client, ctx, **consumer(1))
    with ctx.db.transaction() as conn:
        conn.execute("UPDATE leads SET status = ? WHERE id = ?", (status, lead["id"]))

    open_link(ctx, lead, START)

    assert lead_row(ctx, lead["id"])["status"] == status


# --- booking through the form ----------------------------------------------------------


def test_booking_creates_the_appointment_and_swaps_follow_ups_for_a_confirmation(client, ctx, mailer, clock):
    # Rule: one form post books the window, marks the lead booked, stops nudges, queues a confirmation.
    lead = new_lead(client, ctx, **consumer(1))
    ctx.worker.deliver_due()  # intro
    clock.advance(minutes=5)

    response = submit(client, lead, THU_2PM)

    assert response.status_code == 303
    assert response.headers["location"] == f"/book/{lead['booking_token']}"
    (appointment,) = appointments(ctx, lead["id"])
    assert appointment["starts_at"] == THU_2PM
    assert appointment["ends_at"] == "2026-11-12T21:00:00.000Z"  # a two-hour window
    assert appointment["timezone"] == "America/Indiana/Indianapolis"
    assert appointment["status"] == "scheduled"
    assert appointment["tenant_id"] == "tnt_sparkle"
    assert appointment["created_at"] == iso(clock())

    booked = lead_row(ctx, lead["id"])
    assert booked["status"] == "booked"
    assert booked["booked_at"] == iso(clock())
    assert booked["first_engaged_at"] == iso(clock())  # booking counts as engagement

    assert lead_events(ctx, lead["id"], "booking.appointment_booked") == [
        {"appointment_id": appointment["id"], "starts_at": THU_2PM, "label": "Thu, Nov 12, 2:00 - 4:00 PM"}
    ]
    assert lead_events(ctx, lead["id"], "booking.rescheduled") == []
    follow_ups = messages(ctx, lead["id"], "followup")
    assert [(m["status"], m["last_error"]) for m in follow_ups] == [("cancelled", "booked")] * 2
    (confirmation,) = messages(ctx, lead["id"], "booking_confirmation")
    assert (confirmation["status"], confirmation["to_address"]) == ("pending", "consumer1@example.com")
    assert confirmation["send_after"] == iso(clock())

    assert ctx.worker.deliver_due() == 1
    assert mailer.sent[-1].subject == "You're booked: Thu, Nov 12, 2:00 - 4:00 PM - Sparkle Home Cleaning"
    assert mailer.sent[-1].to == "consumer1@example.com"
    assert "  When:  Thu, Nov 12, 2:00 - 4:00 PM\n" in mailer.sent[-1].text


def test_submitting_the_same_slot_twice_is_idempotent(client, ctx, mailer):
    # Rule: a double click or a refresh must not create a second appointment or a second email.
    lead = new_lead(client, ctx, **consumer(1))

    assert submit(client, lead, THU_NOON).status_code == 303
    assert submit(client, lead, THU_NOON).status_code == 303

    assert [a["status"] for a in appointments(ctx, lead["id"])] == ["scheduled"]
    assert len(messages(ctx, lead["id"], "booking_confirmation")) == 1
    assert len(lead_events(ctx, lead["id"], "booking.appointment_booked")) == 1
    assert lead_events(ctx, lead["id"], "booking.rescheduled") == []
    ctx.worker.deliver_due()
    assert len([e for e in mailer.sent if e.subject.startswith("You're booked")]) == 1


def test_slot_held_by_another_lead_is_refused_and_nothing_changes(client, ctx):
    # Rule: first come, first served; the loser sees a message and keeps a clean record.
    winner = new_lead(client, ctx, **consumer(1))
    loser = new_lead(client, ctx, **consumer(2))
    assert submit(client, winner, THU_NOON).status_code == 303
    before = lead_row(ctx, loser["id"])

    response = submit(client, loser, THU_NOON)

    assert response.status_code == 409
    assert "That time is no longer available. Please pick another." in response.text
    assert 'role="alert"' in response.text
    assert f'value="{THU_NOON}"' not in response.text  # the taken window is no longer offered
    assert f'value="{THU_2PM}"' in response.text
    assert appointments(ctx, loser["id"]) == []
    assert lead_row(ctx, loser["id"]) == before
    assert lead_events(ctx, loser["id"], "booking.appointment_booked") == []
    assert messages(ctx, loser["id"], "booking_confirmation") == []
    assert [m["status"] for m in messages(ctx, loser["id"], "followup")] == ["pending", "pending"]
    assert [a["starts_at"] for a in appointments(ctx, winner["id"])] == [THU_NOON]


@pytest.mark.parametrize(
    "slot",
    [
        "2026-11-15T08:00:00.000Z",  # Sunday 03:00 local
        "2026-11-15T15:00:00.000Z",  # Sunday 10:00 local: right hour, closed day
        "2026-11-12T13:00:00.000Z",  # today 08:00 local: already in the past
        "2026-11-12T15:00:00.000Z",  # today 10:00 local: right now, less than two hours' notice
        "2026-11-13T02:00:00.000Z",  # Thursday 21:00 local: outside business hours
        "2026-11-13T14:00:00.000Z",  # Friday 09:00 local: inside hours but not a window boundary
        "2026-11-13T21:00:00.000Z",  # Friday 16:00 local: would run past closing
        "2026-12-25T15:00:00.000Z",  # a real window, but weeks beyond what is offered
        "garbage",
        "'; DROP TABLE appointments; --",
        "2026-11-12T17:00:00Z",  # a real slot written without milliseconds
        "2026-11-12T17:00:00.000+00:00",  # a real slot with a numeric offset
        "2026-11-12T12:00:00.000-05:00",  # a real slot in local time
        "2026-11-12 17:00:00.000Z",  # a real slot with a space
        " 2026-11-12T17:00:00.000Z",  # a real slot with a leading space
        "2026-11-12t17:00:00.000z",  # a real slot in lower case
    ],
)
def test_tampered_slot_values_are_refused(client, ctx, slot):
    # Rule: only a slot we would offer right now, in exactly the form we offered it, can be booked.
    lead = new_lead(client, ctx, **consumer(1))
    before = lead_row(ctx, lead["id"])

    response = submit(client, lead, slot)

    assert response.status_code == 409
    assert "That time is no longer available. Please pick another." in response.text
    assert db_rows(ctx, "SELECT id FROM appointments") == []
    assert lead_row(ctx, lead["id"]) == before
    assert messages(ctx, lead["id"], "booking_confirmation") == []
    assert lead_events(ctx, lead["id"], "booking.appointment_booked") == []


def test_slot_that_slipped_inside_the_notice_period_since_the_page_loaded_is_refused(client, ctx, clock):
    # Rule: availability is re-checked at submit time, not trusted from the page.
    lead = new_lead(client, ctx, **consumer(1))
    page = client.get(f"/book/{lead['booking_token']}")
    assert f'value="{THU_NOON}"' in page.text  # offered at 10:00...

    clock.advance(minutes=1)  # ...but at 10:01 it is less than two hours away
    response = submit(client, lead, THU_NOON)

    assert response.status_code == 409
    assert appointments(ctx, lead["id"]) == []


def test_rebooking_cancels_the_old_appointment_and_frees_its_slot(client, ctx, mailer, clock):
    # Rule: changing the time moves the appointment; the old window goes back on offer.
    lead = new_lead(client, ctx, **consumer(1))
    other = new_lead(client, ctx, **consumer(2))
    assert submit(client, lead, THU_2PM).status_code == 303
    ctx.worker.deliver_due()  # intros + first confirmation
    first_booked_at = lead_row(ctx, lead["id"])["booked_at"]
    clock.advance(minutes=10)

    assert submit(client, lead, FRI_8AM).status_code == 303

    old, new = appointments(ctx, lead["id"])
    assert (old["starts_at"], old["status"]) == (THU_2PM, "cancelled")
    assert (new["starts_at"], new["status"]) == (FRI_8AM, "scheduled")
    assert lead_events(ctx, lead["id"], "booking.rescheduled") == [
        {"appointment_id": new["id"], "starts_at": FRI_8AM, "label": "Fri, Nov 13, 8:00 - 10:00 AM"}
    ]
    assert len(lead_events(ctx, lead["id"], "booking.appointment_booked")) == 1
    rebooked = lead_row(ctx, lead["id"])
    assert rebooked["status"] == "booked"
    assert rebooked["booked_at"] == first_booked_at  # the funnel keeps the FIRST booking time

    confirmations = messages(ctx, lead["id"], "booking_confirmation")
    assert [m["status"] for m in confirmations] == ["sent", "pending"]
    assert ctx.worker.deliver_due() == 1
    assert mailer.sent[-1].subject == "You're booked: Fri, Nov 13, 8:00 - 10:00 AM - Sparkle Home Cleaning"

    # The freed window can now be taken by someone else.
    assert submit(client, other, THU_2PM).status_code == 303
    assert [a["starts_at"] for a in appointments(ctx, other["id"])] == [THU_2PM]


def test_failed_rebooking_keeps_the_existing_appointment(client, ctx):
    # Rule: asking for a window someone else holds must not cost you the one you already have.
    lead = new_lead(client, ctx, **consumer(1))
    other = new_lead(client, ctx, **consumer(2))
    assert submit(client, lead, THU_NOON).status_code == 303
    assert submit(client, other, THU_2PM).status_code == 303

    response = submit(client, lead, THU_2PM)

    assert response.status_code == 409
    assert [(a["starts_at"], a["status"]) for a in appointments(ctx, lead["id"])] == [(THU_NOON, "scheduled")]
    assert lead_events(ctx, lead["id"], "booking.rescheduled") == []
    assert len(messages(ctx, lead["id"], "booking_confirmation")) == 1


def test_going_back_to_a_previously_held_slot_works(client, ctx):
    # Rule: A -> B -> A is allowed; cancelled appointments do not block their old window.
    lead = new_lead(client, ctx, **consumer(1))

    for slot in (THU_NOON, FRI_8AM, THU_NOON):
        assert submit(client, lead, slot).status_code == 303

    rows = appointments(ctx, lead["id"])
    assert [a["starts_at"] for a in rows if a["status"] == "scheduled"] == [THU_NOON]
    assert sorted(a["starts_at"] for a in rows if a["status"] == "cancelled") == [THU_NOON, FRI_8AM]
    assert len(lead_events(ctx, lead["id"], "booking.rescheduled")) == 2


def test_the_same_instant_can_be_booked_with_two_different_tenants(client, ctx):
    # Rule: a window is exclusive per business, not globally.
    sparkle = new_lead(client, ctx, **consumer(1))
    summit = new_lead(client, ctx, **consumer(2), ALAccountId=SUMMIT_ADS_ACCOUNT)

    assert submit(client, sparkle, THU_NOON).status_code == 303  # 12:00 in Indianapolis
    assert submit(client, summit, THU_NOON).status_code == 303  # 10:00 in Denver, the same instant

    (summit_appointment,) = appointments(ctx, summit["id"])
    assert summit_appointment["timezone"] == "America/Denver"
    assert summit_appointment["starts_at"] == appointments(ctx, sparkle["id"])[0]["starts_at"] == THU_NOON


def test_lead_without_a_valid_email_can_book_and_gets_no_confirmation(client, ctx, mailer):
    # Rule: booking does not depend on email; we simply have nowhere to send a confirmation.
    lead = new_lead(client, ctx, **{**consumer(1), "Email": "not-an-email"})
    assert lead["email_normalized"] is None

    assert submit(client, lead, THU_NOON).status_code == 303

    assert [a["starts_at"] for a in appointments(ctx, lead["id"])] == [THU_NOON]
    assert lead_row(ctx, lead["id"])["status"] == "booked"
    assert db_rows(ctx, "SELECT id FROM outbox_messages WHERE lead_id = ?", (lead["id"],)) == []
    assert ctx.worker.deliver_due() == 0
    assert mailer.sent == []


@pytest.mark.parametrize("outcome", ["won", "lost"])
def test_closed_lead_keeps_its_outcome_status_after_booking(client, ctx, outcome):
    # Rule: a booking never overwrites a recorded outcome (the funnel still gets booked_at).
    lead = new_lead(client, ctx, **consumer(1))
    recorded = client.post(f"/admin/api/leads/{lead['id']}/outcome", json={"outcome": outcome}, headers=ADMIN)
    assert recorded.status_code == 200, recorded.text

    assert submit(client, lead, THU_NOON).status_code == 303

    row = lead_row(ctx, lead["id"])
    assert row["status"] == outcome
    assert row["outcome"] == outcome
    assert row["booked_at"] is not None
    assert [a["status"] for a in appointments(ctx, lead["id"])] == ["scheduled"]


def test_booking_for_a_deactivated_tenant_is_refused(client, ctx):
    # Rule: a switched-off business takes no new appointments.
    lead = new_lead(client, ctx, **consumer(1))
    with ctx.db.transaction() as conn:
        conn.execute("UPDATE tenants SET is_active = 0 WHERE id = 'tnt_sparkle'")

    response = submit(client, lead, THU_NOON)

    assert response.status_code == 404
    assert appointments(ctx, lead["id"]) == []
    assert lead_row(ctx, lead["id"])["booked_at"] is None


# --- concurrency -----------------------------------------------------------------------


def race_for_slot(ctx, clock, leads: list[dict], slot: str) -> list[str]:
    """Each lead tries to book ``slot`` from its own thread, all released at the same moment."""
    tenant_row = db_one(ctx, "SELECT * FROM tenants WHERE id = 'tnt_sparkle'")
    start = threading.Barrier(len(leads))
    outcomes: list[str] = []

    def attempt(lead: dict) -> None:
        start.wait(timeout=30)
        try:
            with ctx.db.transaction() as conn:
                book_appointment(conn, lead=lead, tenant=tenant_row, slot_key=slot, now=clock())
            outcomes.append("booked")
        except BookingError:
            outcomes.append("refused")
        except Exception as exc:  # noqa: BLE001 - reported through the assertion below
            outcomes.append(f"error: {type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=attempt, args=(lead,)) for lead in leads]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    return outcomes


def test_two_leads_racing_for_one_slot_exactly_one_wins(client, ctx, clock):
    # Rule: two consumers clicking the same window at the same instant cannot both get it.
    leads = [new_lead(client, ctx, **consumer(n)) for n in (1, 2)]

    outcomes = race_for_slot(ctx, clock, leads, THU_NOON)

    assert sorted(outcomes) == ["booked", "refused"]
    scheduled = db_rows(ctx, "SELECT lead_id FROM appointments WHERE starts_at = ? AND status = 'scheduled'", (THU_NOON,))
    assert len(scheduled) == 1
    winner = scheduled[0]["lead_id"]
    loser = next(lead["id"] for lead in leads if lead["id"] != winner)
    assert lead_row(ctx, winner)["status"] == "booked"
    assert lead_row(ctx, loser)["booked_at"] is None
    assert appointments(ctx, loser) == []
    assert messages(ctx, loser, "booking_confirmation") == []
    assert len(messages(ctx, winner, "booking_confirmation")) == 1


def test_eight_leads_racing_for_one_slot_still_only_one_wins(client, ctx, clock):
    # Rule: the same guarantee under heavier contention.
    leads = [new_lead(client, ctx, **consumer(n)) for n in range(8)]

    outcomes = race_for_slot(ctx, clock, leads, FRI_8AM)

    assert sorted(outcomes) == ["booked"] + ["refused"] * 7
    assert len(db_rows(ctx, "SELECT id FROM appointments WHERE status = 'scheduled'")) == 1
    assert len(db_rows(ctx, "SELECT id FROM outbox_messages WHERE template = 'booking_confirmation'")) == 1
    assert len(db_rows(ctx, "SELECT id FROM leads WHERE booked_at IS NOT NULL")) == 1
