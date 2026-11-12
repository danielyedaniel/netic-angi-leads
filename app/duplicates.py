"""Duplicate-lead detection and lead-credit ("rebate") claims.

Two different things are both called "duplicates":

* **Redelivery** - Angi sends the *same lead* (same lead id) more than once. That is
  handled by idempotency in ``ingest.py``; the tenant was charged once, so there is
  nothing to claim.
* **Duplicate lead** - the *same consumer* comes through as a *new, separately billed*
  lead within the credit window. This module finds those and records a claim with the
  evidence Angi asks for, so the tenant can get the fee back.

Matching is on normalised contact details within one tenant and one Angi account: same
phone (primary or secondary, compared as E.164), same email, or the same Angi
service-request id. A tenant with two Angi accounts is charged by each of them in its
own right, so the same consumer reaching both is not a double charge.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.models import NormalizedLead
from app.repo import EV_DUPLICATE, add_lead_event
from app.util import dumps, iso, new_id, parse_iso


@dataclass
class DuplicateMatch:
    original: dict  # the earlier lead this one duplicates
    matched_on: list[str]  # which of: phone, email, service_request
    same_task: bool
    hours_apart: float
    confidence: str  # high | medium

    @property
    def strength(self) -> int:
        """How good the evidence is: the same service request, then two contact
        details agreeing, then a single one."""
        if "service_request" in self.matched_on:
            return 3
        return 2 if {"phone", "email"} <= set(self.matched_on) else 1


def _same_text(a: str | None, b: str | None) -> bool:
    return bool(a and b and a.strip().casefold() == b.strip().casefold())


def _own_phones(lead: NormalizedLead) -> list[str]:
    # A masked number is a temporary Angi proxy, not the consumer's own phone, so two
    # different people can share one over time: never match on it.
    primary = None if lead.phone_is_masked else lead.phone_normalized
    return [p for p in (primary, lead.secondary_phone_normalized) if p]


def _same_consumer_sql(lead: NormalizedLead) -> tuple[str, list] | None:
    """SQL condition (and its parameters) selecting stored leads that share a contact
    detail or the service request with ``lead``. None when it has nothing to match on."""
    phones = _own_phones(lead)
    clauses: list[str] = []
    params: list = []
    if phones:
        marks = ",".join("?" * len(phones))
        clauses.append(f"(phone_normalized IN ({marks}) AND COALESCE(phone_is_masked, 0) = 0)")
        clauses.append(f"secondary_phone_normalized IN ({marks})")
        params.extend(phones + phones)
    if lead.email_normalized:
        clauses.append("email_normalized = ?")
        params.append(lead.email_normalized)
    if lead.external_request_id:
        clauses.append("(external_request_id = ? AND source_format = ?)")
        params.extend([lead.external_request_id, lead.source_format])
    if not clauses:
        return None
    # Angi charges per account. When both leads say which account bought them, it has to
    # be the same one; a lead that does not say is compared with every account.
    sql = f"({' OR '.join(clauses)}) AND (external_account_id IS NULL OR ? IS NULL OR external_account_id = ?)"
    return sql, [*params, lead.external_account_id, lead.external_account_id]


def _compare(lead: NormalizedLead, original: dict, received_at: str) -> DuplicateMatch:
    """Describe how ``lead`` (received at ``received_at``) matches the earlier ``original``."""
    earlier_phones = {
        None if original["phone_is_masked"] else original["phone_normalized"],
        original["secondary_phone_normalized"],
    } - {None}
    matched_on: list[str] = []
    if earlier_phones & set(_own_phones(lead)):
        matched_on.append("phone")
    if lead.email_normalized and lead.email_normalized == original["email_normalized"]:
        matched_on.append("email")
    if (
        lead.external_request_id
        and lead.external_request_id == original["external_request_id"]
        and lead.source_format == original["source_format"]
    ):
        matched_on.append("service_request")

    same_task = _same_text(lead.task_name, original["task_name"])
    strong = "service_request" in matched_on or {"phone", "email"} <= set(matched_on)
    hours_apart = (parse_iso(received_at) - parse_iso(original["received_at"])).total_seconds() / 3600
    return DuplicateMatch(
        original=original,
        matched_on=matched_on,
        same_task=same_task,
        hours_apart=round(hours_apart, 2),
        confidence="high" if strong or same_task else "medium",
    )


def find_duplicate(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    lead: NormalizedLead,
    received_at: str,
    window_days: int,
    exclude_lead_id: str | None = None,
) -> DuplicateMatch | None:
    """The earlier lead from the same consumer, for this tenant and Angi account,
    inside the window. Of several, the strongest match (it is the evidence the claim
    will quote), and of equally strong ones the most recent. None when there is none."""
    same_consumer = _same_consumer_sql(lead)
    if same_consumer is None:
        return None
    condition, params = same_consumer
    window_start = iso(parse_iso(received_at) - timedelta(days=window_days))
    rows = conn.execute(
        f"""SELECT * FROM leads
            WHERE tenant_id = ? AND source = ? AND received_at >= ? AND received_at <= ?
              AND id != ? AND {condition}
            ORDER BY received_at DESC, created_at DESC
            LIMIT 50""",
        [tenant_id, lead.source, window_start, received_at, exclude_lead_id or "", *params],
    ).fetchall()
    matches = [_compare(lead, dict(row), received_at) for row in rows]
    # max() keeps the first of equals, and the rows come newest first.
    return max(matches, key=lambda match: match.strength, default=None)


# What the matcher reads of a lead. A stored row has all of it, under the same names.
_MATCH_FIELDS = (
    "source",
    "source_format",
    "external_request_id",
    "external_account_id",
    "email_normalized",
    "phone_normalized",
    "phone_is_masked",
    "secondary_phone_normalized",
    "task_name",
)


def flag_later_duplicates(
    conn: sqlite3.Connection,
    *,
    lead_row: dict,
    window_days: int,
    request_window_days: int,
    now: datetime,
) -> int:
    """Leads are not always processed in the order they arrived: a quarantined event is
    replayed once its mapping exists, a failed one is retried. When ``lead_row`` is
    stored late like that, a lead from the same consumer that arrived *after* it may
    already be here, and was judged "not a duplicate" because this one did not exist
    yet. Flag it now and open its claim, so the pair yields exactly one duplicate and
    one claim whatever the processing order. Returns how many leads were flagged.
    (For a lead processed on arrival nothing later exists and this is one empty query.)"""
    earlier = NormalizedLead(**{name: lead_row[name] for name in _MATCH_FIELDS})
    same_consumer = _same_consumer_sql(earlier)
    if same_consumer is None:
        return 0
    condition, params = same_consumer
    window_end = iso(parse_iso(lead_row["received_at"]) + timedelta(days=window_days))
    later_rows = conn.execute(
        f"""SELECT * FROM leads
            WHERE tenant_id = ? AND source = ? AND received_at > ? AND received_at <= ?
              AND is_duplicate = 0 AND id != ? AND {condition}
            ORDER BY received_at""",
        [lead_row["tenant_id"], lead_row["source"], lead_row["received_at"], window_end, lead_row["id"], *params],
    ).fetchall()
    for row in later_rows:
        later = NormalizedLead(**{name: row[name] for name in _MATCH_FIELDS})
        match = _compare(later, lead_row, row["received_at"])
        conn.execute(
            "UPDATE leads SET is_duplicate = 1, duplicate_of_lead_id = ?, duplicate_matched_on = ?, updated_at = ? "
            "WHERE id = ?",
            (lead_row["id"], dumps(match.matched_on), iso(now), row["id"]),
        )
        record_duplicate(
            conn,
            tenant_id=row["tenant_id"],
            lead_id=row["id"],
            fee_cents=row["fee_cents"],
            received_at=row["received_at"],
            match=match,
            request_window_days=request_window_days,
            now=now,
        )
    return len(later_rows)


def record_duplicate(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    lead_id: str,
    fee_cents: int | None,
    received_at: str,
    match: DuplicateMatch,
    request_window_days: int,
    now: datetime,
) -> str:
    """Open the credit claim for a lead found to be a duplicate and note the finding on
    the lead's timeline. Returns the claim id."""
    claim_id = create_claim(
        conn,
        tenant_id=tenant_id,
        lead_id=lead_id,
        fee_cents=fee_cents,
        received_at=received_at,
        match=match,
        request_window_days=request_window_days,
        now=now,
    )
    add_lead_event(
        conn,
        lead_id,
        tenant_id,
        EV_DUPLICATE,
        now,
        {
            "original_lead_id": match.original["id"],
            "matched_on": match.matched_on,
            "confidence": match.confidence,
            "hours_apart": match.hours_apart,
            "claim_id": claim_id,
        },
    )
    return claim_id


def create_claim(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    lead_id: str,
    fee_cents: int | None,
    received_at: str,
    match: DuplicateMatch,
    request_window_days: int,
    now: datetime,
) -> str:
    """Record that ``lead_id`` duplicates an earlier lead and is worth a credit request."""
    claim_id = new_id("clm")
    deadline = iso(parse_iso(received_at) + timedelta(days=request_window_days))
    conn.execute(
        """INSERT INTO credit_claims
               (id, tenant_id, lead_id, original_lead_id, reason, confidence, matched_on, same_task,
                hours_apart, amount_cents, status, request_deadline, created_at)
           VALUES (?, ?, ?, ?, 'duplicate', ?, ?, ?, ?, ?, 'detected', ?, ?)
           ON CONFLICT(lead_id) DO NOTHING""",
        (
            claim_id,
            tenant_id,
            lead_id,
            match.original["id"],
            match.confidence,
            dumps(match.matched_on),
            1 if match.same_task else 0,
            match.hours_apart,
            fee_cents,
            deadline,
            iso(now),
        ),
    )
    row = conn.execute("SELECT id FROM credit_claims WHERE lead_id = ?", (lead_id,)).fetchone()
    return row["id"]
