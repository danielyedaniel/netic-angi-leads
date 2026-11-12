"""Lead-credit ("rebate") claims: the tenant-facing side of duplicate detection.

``duplicates.py`` decides *that* a lead is a duplicate and records a claim. This module
turns claims into money:

* a worklist ordered by deadline, with Angi's own eligibility conditions checked for
  each claim;
* an evidence export (CSV) with both lead ids, timestamps, the matching contact
  details and ready-to-paste request text;
* status tracking, so we can report how much was actually recovered.

Angi has no API for credit requests (and its terms forbid scripting the pro portal),
so the last step is a person pasting the prepared text into the "Request credit" form.
The job here is to make that a thirty-second task that never gets forgotten.
"""

from __future__ import annotations

import csv
import io
import sqlite3
from datetime import datetime
from typing import Any

from app.config import Settings
from app.util import iso, loads, parse_iso, seconds_between

CLAIM_STATUSES = ("detected", "submitted", "credited", "denied", "dismissed")
OPEN_STATUSES = ("detected", "submitted")
DEADLINE_WARNING_DAYS = 7

_CLAIM_SQL = """
SELECT c.*,
       t.name  AS tenant_name,
       d.source_format         AS source_format,
       d.external_account_id   AS angi_account_id,
       d.external_account_name AS angi_account_name,
       d.external_lead_id      AS dup_angi_lead_id,
       d.external_request_id   AS dup_angi_request_id,
       d.received_at           AS dup_received_at,
       d.first_contacted_at    AS dup_first_contacted_at,
       d.full_name             AS dup_name,
       d.phone                 AS dup_phone,
       d.email                 AS dup_email,
       d.task_name             AS dup_task,
       d.address_line1         AS dup_address,
       d.city                  AS dup_city,
       d.state                 AS dup_state,
       d.postal_code           AS dup_postal_code,
       o.external_lead_id      AS orig_angi_lead_id,
       o.external_request_id   AS orig_angi_request_id,
       o.received_at           AS orig_received_at,
       o.full_name             AS orig_name,
       o.phone                 AS orig_phone,
       o.email                 AS orig_email,
       o.task_name             AS orig_task,
       o.fee_cents             AS orig_fee_cents
FROM credit_claims c
JOIN tenants t ON t.id = c.tenant_id
JOIN leads   d ON d.id = c.lead_id
LEFT JOIN leads o ON o.id = c.original_lead_id
"""


def _decorate(claim: dict[str, Any], now: datetime | None, settings: Settings | None) -> dict[str, Any]:
    claim["matched_on"] = loads(claim["matched_on"], [])
    claim["evidence"] = evidence_text(claim)
    claim["eligibility"] = assess(claim, now, settings) if now and settings else None
    return claim


def list_claims(
    conn: sqlite3.Connection,
    *,
    tenant_id: str | None = None,
    status: str | None = None,
    now: datetime | None = None,
    settings: Settings | None = None,
) -> list[dict[str, Any]]:
    where, params = ["t.is_test = 0"], []  # the monitoring probe's tenant is not a paying customer
    if tenant_id:
        where.append("c.tenant_id = ?")
        params.append(tenant_id)
    if status:
        where.append("c.status = ?")
        params.append(status)
    sql = f"{_CLAIM_SQL} WHERE {' AND '.join(where)} ORDER BY c.request_deadline, c.created_at"
    return [_decorate(dict(row), now, settings) for row in conn.execute(sql, params)]


def get_claim(
    conn: sqlite3.Connection, claim_id: str, now: datetime | None = None, settings: Settings | None = None
) -> dict[str, Any] | None:
    row = conn.execute(f"{_CLAIM_SQL} WHERE c.id = ?", (claim_id,)).fetchone()
    return _decorate(dict(row), now, settings) if row else None


def assess(claim: dict[str, Any], now: datetime, settings: Settings) -> dict[str, Any]:
    """Check a claim against the conditions Angi publishes for considering a credit:
    both leads within the duplicate window, the request made in time, and a contact
    attempt on the lead within 24 hours of receiving it.

    The last one can only be checked in part. Angi asks for a CALL attempt; what this
    system makes and records is an EMAIL. So "no contact at all" or "too late" blocks
    the claim, but an email in time does not prove the condition is met: every claim
    carries a note telling the tenant to call, or to have their call log at hand."""
    blockers: list[str] = []

    window_days = settings.duplicate_window_days
    if claim["hours_apart"] is not None and claim["hours_apart"] > window_days * 24:
        blockers.append(f"the two leads are more than {window_days} days apart")

    days_left = None
    if claim["request_deadline"]:
        deadline = parse_iso(claim["request_deadline"])
        days_left = round((deadline - now).total_seconds() / 86400, 1)  # rounded for display only
        if claim["status"] == "detected" and now > deadline:
            blockers.append("the deadline for requesting this credit has passed")

    sla_hours = settings.credit_contact_sla_hours
    contact_delay = seconds_between(claim["dup_received_at"], claim["dup_first_contacted_at"])
    if contact_delay is None:
        blockers.append("no contact attempt has been recorded for this lead yet")
    elif contact_delay > sla_hours * 3600:
        blockers.append(f"the first contact attempt came more than {sla_hours}h after the lead arrived")

    notes = [
        f"Angi asks for a call attempt within {sla_hours}h; this system records the email it sent, "
        "not calls: call the homeowner or have your call log ready"
    ]
    if claim["amount_cents"] is None:
        notes.append("the lead's fee is unknown; set a default fee on the Angi account mapping")
    elif claim["amount_cents"] == 0:
        notes.append("this lead was not charged, so there is nothing to credit")
    if claim["confidence"] != "high":
        notes.append("same homeowner but a different service; Angi may treat it as a separate project")

    return {
        "eligible": not blockers,
        "blockers": blockers,
        "notes": notes,
        "days_left": days_left,
        "contacted_within_sla": contact_delay is not None and contact_delay <= sla_hours * 3600,
    }


def evidence_text(claim: dict[str, Any]) -> str:
    """One paragraph a tenant can paste into Angi's credit request form."""
    matched = " and ".join(claim["matched_on"]) or "contact details"
    hours = claim.get("hours_apart")
    if hours is None:
        gap = ""
    elif hours < 48:
        gap = f", {hours:.1f} hours later"
    else:
        gap = f", {hours / 24:.1f} days later"
    task = " for the same service" if claim.get("same_task") else ""
    who = ", ".join(p for p in (claim.get("dup_name"), claim.get("dup_phone"), claim.get("dup_email")) if p)
    text = (
        f"Duplicate lead. Lead {claim.get('dup_angi_lead_id') or '(no id)'} received "
        f"{_date(claim.get('dup_received_at'))} is the same homeowner{task} as lead "
        f"{claim.get('orig_angi_lead_id') or '(no id)'} received {_date(claim.get('orig_received_at'))}"
        f"{gap} (matching {matched})."
    )
    if who:
        text += f" Homeowner: {who}."
    if claim.get("dup_first_contacted_at"):
        # "Emailed", because that is what happened; a call is the tenant's to add.
        text += f" We emailed the homeowner on {_date(claim['dup_first_contacted_at'])}."
    if claim.get("amount_cents"):
        text += f" Requesting a credit of {_dollars(claim['amount_cents'])}."
    return text


def summarize(claims: list[dict[str, Any]]) -> dict[str, Any]:
    by_status: dict[str, dict[str, int]] = {s: {"count": 0, "amount_cents": 0} for s in CLAIM_STATUSES}
    for claim in claims:
        bucket = by_status[claim["status"]]
        bucket["count"] += 1
        bucket["amount_cents"] += claim["amount_cents"] or 0
    open_claims = [c for c in claims if c["status"] in OPEN_STATUSES]
    to_file = [c for c in claims if c["status"] == "detected" and (c["eligibility"] or {}).get("eligible", True)]
    expiring = [
        {"claim_id": c["id"], "days_left": c["eligibility"]["days_left"]}
        for c in to_file
        if c["eligibility"]
        and c["eligibility"]["days_left"] is not None
        and c["eligibility"]["days_left"] <= DEADLINE_WARNING_DAYS
    ]
    return {
        "by_status": by_status,
        "open_count": len(open_claims),
        "claimable_cents": sum(c["amount_cents"] or 0 for c in open_claims),
        "ready_to_file_count": len(to_file),
        "ready_to_file_cents": sum(c["amount_cents"] or 0 for c in to_file),
        "recovered_cents": sum(c["credited_cents"] or 0 for c in claims if c["status"] == "credited"),
        "expiring_soon": expiring,
    }


def set_status(
    conn: sqlite3.Connection,
    claim_id: str,
    status: str,
    now: datetime,
    *,
    credited_cents: int | None = None,
    notes: str | None = None,
) -> dict[str, Any] | None:
    """Move a claim through Angi's (manual) credit process. Each status leaves the row
    saying exactly what that status means, whatever state it was in before:

    * detected  - nothing filed yet: no filing date, no resolution, no money;
    * submitted - filed and waiting: the first filing date is kept, nothing resolved;
    * credited  - Angi paid: the amount received (the whole fee unless told otherwise);
    * denied / dismissed - closed without money.

    Saving the same status again (to add a note) changes neither the dates nor the
    amount already recorded."""
    if status not in CLAIM_STATUSES:
        raise ValueError(f"status must be one of {', '.join(CLAIM_STATUSES)}")
    row = conn.execute("SELECT * FROM credit_claims WHERE id = ?", (claim_id,)).fetchone()
    if row is None:
        return None
    stamp = iso(now)
    unchanged = row["status"] == status

    if status == "detected":
        submitted_at = None
    elif status == "dismissed":
        submitted_at = row["submitted_at"]  # we may have decided not to file at all
    else:  # submitted, credited and denied all mean a request was filed
        submitted_at = row["submitted_at"] or stamp

    if status in OPEN_STATUSES:
        resolved_at = None
    else:
        resolved_at = row["resolved_at"] if unchanged and row["resolved_at"] else stamp

    credited = None
    if status == "credited":
        if credited_cents is not None:
            credited = credited_cents
        elif unchanged and row["credited_cents"] is not None:
            credited = row["credited_cents"]  # e.g. a partial credit recorded earlier
        else:
            credited = row["amount_cents"] or 0
        if row["amount_cents"] is not None and credited > row["amount_cents"]:
            # Angi cannot give back more than it charged; a typo here would turn net
            # spend (and every cost-per figure built on it) negative.
            raise ValueError("the credit cannot be larger than the fee paid for the lead")

    conn.execute(
        """UPDATE credit_claims SET status = ?, submitted_at = ?, resolved_at = ?, credited_cents = ?,
               notes = COALESCE(?, notes) WHERE id = ?""",
        (status, submitted_at, resolved_at, credited, notes, claim_id),
    )
    return get_claim(conn, claim_id)


def _eligibility_cell(claim: dict[str, Any], field: str) -> str:
    eligibility = claim.get("eligibility")
    if not eligibility:
        return ""
    if field == "eligible":
        return "yes" if eligibility["eligible"] else "no"
    return "; ".join(eligibility["blockers"] + eligibility["notes"])


CSV_COLUMNS = [
    ("Claim ID", lambda c: c["id"]),
    ("Claim status", lambda c: c["status"]),
    ("Business", lambda c: c["tenant_name"]),
    ("Angi account ID", lambda c: c["angi_account_id"]),
    ("Credit reason", lambda c: c["reason"]),
    # Not "meets Angi's conditions": the call attempt Angi asks for is not ours to verify.
    ("Passes the checks we can verify", lambda c: _eligibility_cell(c, "eligible")),
    ("Notes", lambda c: _eligibility_cell(c, "notes")),
    ("Request credit by (UTC)", lambda c: c["request_deadline"]),
    ("Confidence", lambda c: c["confidence"]),
    ("Matched on", lambda c: ", ".join(c["matched_on"])),
    ("Same service requested", lambda c: "yes" if c["same_task"] else "no"),
    ("Hours between leads", lambda c: c["hours_apart"]),
    ("Duplicate lead ID (Angi)", lambda c: c["dup_angi_lead_id"]),
    ("Duplicate service request ID (Angi)", lambda c: c["dup_angi_request_id"]),
    ("Duplicate received (UTC)", lambda c: c["dup_received_at"]),
    ("First contact attempt (UTC)", lambda c: c["dup_first_contacted_at"]),
    ("Customer name", lambda c: c["dup_name"]),
    ("Customer phone", lambda c: c["dup_phone"]),
    ("Customer email", lambda c: c["dup_email"]),
    ("Service", lambda c: c["dup_task"]),
    (
        "Address",
        lambda c: ", ".join(
            p for p in (c["dup_address"], c["dup_city"], c["dup_state"], c["dup_postal_code"]) if p
        ),
    ),
    ("Fee charged for duplicate", lambda c: _dollars(c["amount_cents"])),
    ("Original lead ID (Angi)", lambda c: c["orig_angi_lead_id"]),
    ("Original service request ID (Angi)", lambda c: c["orig_angi_request_id"]),
    ("Original received (UTC)", lambda c: c["orig_received_at"]),
    ("Original customer name", lambda c: c["orig_name"]),
    ("Original service", lambda c: c["orig_task"]),
    ("Original fee", lambda c: _dollars(c["orig_fee_cents"])),
    ("Credit request text", lambda c: c["evidence"]),
]


def export_csv(claims: list[dict[str, Any]]) -> str:
    """The evidence pack: one row per duplicate with both lead ids, timestamps, the
    matching contact details, the fee and ready-to-paste request text."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([title for title, _ in CSV_COLUMNS])
    for claim in claims:
        writer.writerow([_cell(getter(claim)) for _, getter in CSV_COLUMNS])
    return buffer.getvalue()


def _cell(value: Any) -> str:
    """Neutralise spreadsheet formulas: lead data is untrusted and this file is opened
    in Excel / Sheets by the tenant."""
    text = "" if value is None else str(value)
    if text[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text


def _dollars(cents: int | None) -> str:
    return "" if cents is None else f"${cents / 100:.2f}"


def _date(stamp: str | None) -> str:
    return stamp[:16].replace("T", " ") + " UTC" if stamp else "(unknown date)"
