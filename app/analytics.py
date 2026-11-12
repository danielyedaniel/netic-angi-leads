"""Conversion analytics for Angi leads.

Everything is computed from two things the pipeline already records:

* the funnel timestamps on each lead (received -> first contacted -> engaged -> booked
  -> won/lost), and
* what the lead cost (``fee_cents``) and what the job was worth (``job_value_cents``).

The numbers are worked out in Python over the matching rows, which keeps each formula
readable; at real volume the same definitions would become SQL over a warehouse table.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import datetime
from typing import Any

from app.booking import tenant_zone
from app.util import iso, parse_iso, seconds_between


def percentile(values: list[float], pct: float) -> float | None:
    """Linear-interpolated percentile (pct in 0..100)."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * pct / 100
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def ratio(numerator: float, denominator: float) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


# --- funnel stages -------------------------------------------------------------------
# The funnel is cumulative: a lead counts at every stage it reached OR PASSED. A job that
# was won was booked (by phone, if not through our link), someone who booked was engaged,
# and so on. Counting only the timestamps we happen to record ourselves would let a later
# stage be bigger than an earlier one (three jobs won, one booked: a 300% win rate).


def _won(lead: dict) -> bool:
    return lead["outcome"] == "won"


def _booked(lead: dict) -> bool:
    return bool(lead["booked_at"]) or _won(lead)


def _engaged(lead: dict) -> bool:
    return bool(lead["first_engaged_at"]) or _booked(lead)


def _contacted(lead: dict) -> bool:
    return bool(lead["first_contacted_at"]) or _engaged(lead)


def _funnel(leads: list[dict]) -> dict[str, Any]:
    received = len(leads)
    contacted = sum(1 for lead in leads if _contacted(lead))
    engaged = sum(1 for lead in leads if _engaged(lead))
    booked = sum(1 for lead in leads if _booked(lead))
    won = sum(1 for lead in leads if _won(lead))
    lost = sum(1 for lead in leads if lead["outcome"] == "lost")  # an exit, not a stage
    return {
        "received": received,
        "unique": sum(1 for lead in leads if not lead["is_duplicate"]),
        "contacted": contacted,
        "engaged": engaged,
        "booked": booked,
        "won": won,
        "lost": lost,
        "contact_rate": ratio(contacted, received),
        "engagement_rate": ratio(engaged, contacted),
        "booking_rate": ratio(booked, received),  # the headline: lead -> booked job
        "win_rate": ratio(won, booked),  # never above 1: every won lead is also booked
    }


def _money(leads: list[dict], credited_cents: int) -> dict[str, Any]:
    spend = sum(lead["fee_cents"] or 0 for lead in leads)
    # Never below zero: a credit recorded for a lead whose fee we do not know (it counts
    # as $0 of spend) must not turn the cost-per figures negative.
    net_spend = max(spend - credited_cents, 0)
    booked = sum(1 for lead in leads if _booked(lead))
    won = [lead for lead in leads if _won(lead)]
    revenue = sum(lead["job_value_cents"] or 0 for lead in won)
    return {
        "spend_cents": spend,
        # Leads whose price we do not know (no fee in the payload, no default on the
        # account mapping). They count as $0 above, so the reader is told how many.
        "unpriced_leads": sum(1 for lead in leads if lead["fee_cents"] is None),
        "credited_cents": credited_cents,
        "net_spend_cents": net_spend,
        "cost_per_lead_cents": round(net_spend / len(leads)) if leads else None,
        "cost_per_booking_cents": round(net_spend / booked) if booked else None,
        "cost_per_won_job_cents": round(net_spend / len(won)) if won else None,
        "revenue_cents": revenue,
        "return_on_spend": ratio(revenue, net_spend),  # revenue per $1 of lead spend
    }


def _speed(leads: list[dict]) -> dict[str, Any]:
    to_contact = [
        s for lead in leads if (s := seconds_between(lead["received_at"], lead["first_contacted_at"])) is not None
    ]
    to_book = [s for lead in leads if (s := seconds_between(lead["received_at"], lead["booked_at"])) is not None]
    return {
        # To the millisecond: the console provider answers in a few of them, and rounded to
        # a tenth of a second the Overview would say "0.0 s" above leads that say "6 ms".
        "speed_to_lead_p50_seconds": _round(percentile(to_contact, 50), 3),
        "speed_to_lead_p90_seconds": _round(percentile(to_contact, 90), 3),
        "speed_to_lead_max_seconds": _round(max(to_contact), 3) if to_contact else None,
        "contacted_within_1_min": ratio(sum(1 for s in to_contact if s <= 60), len(leads)),
        "contacted_within_5_min": ratio(sum(1 for s in to_contact if s <= 300), len(leads)),
        "time_to_book_p50_minutes": _round(p / 60) if (p := percentile(to_book, 50)) is not None else None,
    }


def _round(value: float | None, digits: int = 1) -> float | None:
    return round(value, digits) if value is not None else None


def _group(
    leads: list[dict], key_fn, credited_by_lead: dict[str, int], limit: int = 12
) -> list[dict[str, Any]]:
    """One row per value of ``key_fn`` (a service, a tenant, a ZIP code), using the same
    definitions as the headline numbers: booked includes won, cost is net of credits."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for lead in leads:
        groups[key_fn(lead) or "(not provided)"].append(lead)
    rows = []
    for key, members in groups.items():
        booked = sum(1 for lead in members if _booked(lead))
        spend = sum(lead["fee_cents"] or 0 for lead in members)
        net_spend = max(spend - sum(credited_by_lead.get(lead["id"], 0) for lead in members), 0)
        rows.append(
            {
                "key": key,
                "received": len(members),
                "booked": booked,
                "booking_rate": ratio(booked, len(members)),
                "spend_cents": spend,
                "net_spend_cents": net_spend,
                "cost_per_booking_cents": round(net_spend / booked) if booked else None,
                "revenue_cents": sum(lead["job_value_cents"] or 0 for lead in members if _won(lead)),
            }
        )
    rows.sort(key=lambda row: (-row["received"], row["key"]))
    return rows[:limit]


def summary(
    conn: sqlite3.Connection,
    *,
    tenant_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    include_test: bool = False,
) -> dict[str, Any]:
    where = ["1 = 1"]
    params: list[Any] = []
    if tenant_id:
        where.append("l.tenant_id = ?")
        params.append(tenant_id)
    if since:
        where.append("l.received_at >= ?")
        params.append(iso(since))
    if until:
        where.append("l.received_at < ?")
        params.append(iso(until))
    if not include_test:
        where.append("t.is_test = 0")
    condition = " AND ".join(where)

    leads = [
        dict(row)
        for row in conn.execute(
            f"""SELECT l.*, t.name AS tenant_name, t.timezone AS tenant_timezone
                FROM leads l JOIN tenants t ON t.id = l.tenant_id
                WHERE {condition} ORDER BY l.received_at""",
            params,
        )
    ]
    lead_ids = {lead["id"] for lead in leads}

    claims = [
        dict(row)
        for row in conn.execute(
            f"""SELECT c.* FROM credit_claims c
                JOIN leads l ON l.id = c.lead_id JOIN tenants t ON t.id = l.tenant_id
                WHERE {condition}""",
            params,
        )
    ]
    # Credits actually received, by the lead that was credited (slices need them per lead).
    credited_by_lead = {c["lead_id"]: c["credited_cents"] or 0 for c in claims if c["status"] == "credited"}
    credited = sum(credited_by_lead.values())
    open_claims = [c for c in claims if c["status"] in ("detected", "submitted")]

    # Which touch produced the booking? Counts follow-ups sent before each lead booked.
    # Only bookings made through our link count here (and in time-to-book): a job won
    # over the phone has no booking time to compare the emails with.
    followups_before_booking: dict[str, int] = defaultdict(int)
    for row in conn.execute(
        """SELECT m.lead_id, COUNT(*) AS n FROM outbox_messages m JOIN leads l ON l.id = m.lead_id
           WHERE m.template = 'followup' AND m.status = 'sent' AND l.booked_at IS NOT NULL
             AND m.sent_at < l.booked_at GROUP BY m.lead_id"""
    ):
        if row["lead_id"] in lead_ids:
            followups_before_booking[row["lead_id"]] = row["n"]
    booked_leads = [lead for lead in leads if lead["booked_at"]]
    booked_after_followup = sum(1 for lead in booked_leads if followups_before_booking.get(lead["id"]))

    by_day: dict[str, dict[str, int]] = defaultdict(lambda: {"received": 0, "booked": 0})
    by_hour = [{"hour": hour, "received": 0, "booked": 0} for hour in range(24)]
    for lead in leads:
        local = parse_iso(lead["received_at"]).astimezone(tenant_zone({"timezone": lead["tenant_timezone"]}))
        day = by_day[local.date().isoformat()]
        day["received"] += 1
        by_hour[local.hour]["received"] += 1
        if _booked(lead):
            day["booked"] += 1
            by_hour[local.hour]["booked"] += 1

    return {
        "filters": {
            "tenant_id": tenant_id,
            "since": iso(since) if since else None,
            "until": iso(until) if until else None,
        },
        "funnel": _funnel(leads),
        "speed": _speed(leads),
        "money": _money(leads, credited),
        "duplicates": {
            "count": sum(1 for lead in leads if lead["is_duplicate"]),
            "rate": ratio(sum(1 for lead in leads if lead["is_duplicate"]), len(leads)),
            "open_claims": len(open_claims),
            "claimable_cents": sum(c["amount_cents"] or 0 for c in open_claims),
            "credited_cents": credited,
        },
        "attribution": {
            "booked_after_intro_only": len(booked_leads) - booked_after_followup,
            "booked_after_followup": booked_after_followup,
        },
        "by_task": _group(leads, lambda lead: lead["task_name"], credited_by_lead),
        "by_tenant": _group(leads, lambda lead: lead["tenant_name"], credited_by_lead),
        "by_postal_code": _group(leads, lambda lead: lead["postal_code"], credited_by_lead, limit=10),
        "by_format": _group(leads, lambda lead: lead["source_format"], credited_by_lead),
        "by_day": [{"day": day, **counts} for day, counts in sorted(by_day.items())],
        "by_hour": by_hour,
    }

