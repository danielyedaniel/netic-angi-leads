"""Health checks for the lead pipeline.

Angi can change its payload or simply stop sending without telling us, so the checks
look at the pipeline from several angles:

* **ingest_errors**   - a spike in events we could not turn into leads (format change?)
* **unprocessed**     - events stored but never processed (our bug / crash)
* **quarantine**      - leads waiting on a human (unmapped account, unknown payload)
* **email_backlog**   - first responses not leaving within the speed-to-lead SLA
* **email_failures**  - messages we gave up on
* **lead_silence**    - no leads at all when history says there should have been some
* **canary**          - the synthetic end-to-end probe stopped passing, or stopped arriving

Schema drift itself is detected inline at ingest time (``ingest._record_shape``), since
it is a property of a single payload rather than of a time window.

Each check maps to at most one open alert (key ``check:<name>``) that opens when the
check fails and resolves by itself when it passes again, or when the check no longer
applies (for example a per-tenant check after the tenant was deactivated).
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from app.alerts import (
    CHECK_KEY_PREFIX,
    inherit_announcement,
    raise_alert,
    resolve_alert,
    resolve_quiet_event_alerts,
)
from app.booking import tenant_zone
from app.config import Settings
from app.db import Database
from app.repo import EV_EMAIL_FAILED, kv_get, kv_set
from app.util import iso, loads, parse_iso

SILENCE_HISTORY_DAYS = 14  # the baseline is this many days, ending at the tenant's last lead
SILENCE_MIN_HISTORY = 20  # leads needed before we trust a tenant's baseline...
SILENCE_MIN_DAYS = 7  # ...spread over at least a week, so one busy afternoon is not a "rhythm"
SILENCE_P_THRESHOLD = 0.001  # alert when this much quiet would happen by chance < 0.1% of the time
UNPROCESSED_GRACE_SECONDS = 120
FIRST_TOUCH_TEMPLATES = ("intro", "repeat_request")  # the first response to a lead
CANARY_MISSED_RUNS = 3  # the probe is "missing" once this many scheduled runs have not arrived
LAST_RUN_KEY = "monitor:last_run"


@dataclass
class Check:
    name: str
    status: str  # ok | warning | critical
    summary: str
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status, "summary": self.summary, "detail": self.detail}


def run_checks(db: Database, settings: Settings, now: datetime) -> list[Check]:
    """Evaluate every check, then bring the alerts in line with what was found."""
    with db.transaction() as conn:
        last_run = kv_get(conn, LAST_RUN_KEY)
        checks = evaluate(conn, settings, now)
        for check in checks:
            key = CHECK_KEY_PREFIX + check.name
            if check.status == "ok":
                resolve_alert(conn, key, now)
                continue
            opened = raise_alert(
                conn,
                kind=check.name.split(":")[0],
                key=key,
                severity=check.status,
                title=check.summary,
                detail=check.detail,
                now=now,
                count_occurrence=False,
            )
            if opened and last_run:
                # Closed by hand since the last run, and still failing: the same incident.
                inherit_announcement(conn, key, closed_after=last_run)

        # A check that was not evaluated this time no longer applies (its tenant was
        # deactivated, or the fixed silence threshold was switched off). Nothing would
        # ever report it "ok", so its alert is closed here.
        evaluated = {CHECK_KEY_PREFIX + check.name for check in checks}
        open_keys = [row["dedupe_key"] for row in conn.execute("SELECT dedupe_key FROM alerts WHERE status = 'open'")]
        for key in open_keys:
            if key.startswith(CHECK_KEY_PREFIX) and key not in evaluated:
                resolve_alert(conn, key, now)

        # A processing error is over once no event is left in the failed state (the
        # retry loop, or a deploy with the fix, got them all through).
        if conn.execute("SELECT 1 FROM webhook_events WHERE status = 'failed' LIMIT 1").fetchone() is None:
            conn.execute(
                "UPDATE alerts SET status = 'resolved', resolved_at = ? WHERE kind = 'processing_error' AND status = 'open'",
                (iso(now),),
            )
        # Every event-raised alert (the two kinds above included) is over after a day
        # without a new occurrence; the quarantine and unprocessed checks keep reporting
        # what is still waiting.
        resolve_quiet_event_alerts(conn, now)
        kv_set(conn, LAST_RUN_KEY, iso(now), now)
    return checks


def evaluate(conn: sqlite3.Connection, settings: Settings, now: datetime) -> list[Check]:
    return [
        check_ingest_errors(conn, settings, now),
        check_unprocessed(conn, now),
        check_quarantine(conn),
        check_email_backlog(conn, settings, now),
        check_email_failures(conn, now),
        *check_lead_silence(conn, settings, now),
        check_canary(conn, settings, now),
    ]


def overall_status(checks: list[Check]) -> str:
    if any(c.status == "critical" for c in checks):
        return "critical"
    if any(c.status == "warning" for c in checks):
        return "warning"
    return "ok"


# --- individual checks -------------------------------------------------------------------


def check_ingest_errors(conn: sqlite3.Connection, settings: Settings, now: datetime) -> Check:
    window = settings.error_rate_window_minutes
    since = iso(now - timedelta(minutes=window))
    counts = {
        row["status"]: row["n"]
        for row in conn.execute(
            "SELECT status, COUNT(*) AS n FROM webhook_events WHERE received_at >= ? GROUP BY status", (since,)
        )
    }
    total = sum(counts.values())
    bad = sum(counts.get(status, 0) for status in ("quarantined", "rejected", "failed"))
    rate = bad / total if total else 0.0
    detail = {"window_minutes": window, "total": total, "not_processed": bad, "by_status": counts}
    if total >= settings.error_rate_min_events and rate >= settings.error_rate_threshold:
        return Check(
            "ingest_errors",
            "critical",
            f"{bad} of {total} Angi webhook events in the last {window} min could not be turned into "
            f"leads ({rate:.0%}) - possible payload format change or missing tenant mapping",
            detail,
        )
    return Check("ingest_errors", "ok", f"{bad} of {total} events failed in the last {window} min", detail)


def check_unprocessed(conn: sqlite3.Connection, now: datetime) -> Check:
    cutoff = iso(now - timedelta(seconds=UNPROCESSED_GRACE_SECONDS))
    row = conn.execute(
        "SELECT COUNT(*) AS n, MIN(received_at) AS oldest FROM webhook_events "
        "WHERE status IN ('received', 'failed') AND received_at < ?",
        (cutoff,),
    ).fetchone()
    if row["n"]:
        return Check(
            "unprocessed",
            "critical",
            f"{row['n']} stored webhook event(s) have not been processed (oldest from {row['oldest']})",
            {"count": row["n"], "oldest": row["oldest"]},
        )
    return Check("unprocessed", "ok", "every stored event has been processed")


def check_quarantine(conn: sqlite3.Connection) -> Check:
    """Leads waiting on a human. An event the operator discarded (junk, a test, a ping)
    has left the quarantine, so the check can go green again."""
    reasons = {
        row["status_reason"] or "unknown": row["n"]
        for row in conn.execute(
            "SELECT status_reason, COUNT(*) AS n FROM webhook_events WHERE status = 'quarantined' GROUP BY status_reason"
        )
    }
    total = sum(reasons.values())
    if total:
        parts = ", ".join(f"{n} {reason}" for reason, n in sorted(reasons.items()))
        return Check(
            "quarantine",
            "warning",
            f"{total} lead(s) are quarantined and waiting for action ({parts})",
            {"count": total, "by_reason": reasons},
        )
    return Check("quarantine", "ok", "nothing in quarantine")


def check_email_backlog(conn: sqlite3.Connection, settings: Settings, now: datetime) -> Check:
    sla = settings.email_sla_seconds
    cutoff = iso(now - timedelta(seconds=sla))
    # "Waiting since": a first-touch message (sequence 0) has been waiting since it was
    # queued, even while it sits between retries; a follow-up only since it fell due.
    waiting_since = "CASE WHEN sequence = 0 THEN created_at ELSE send_after END"
    row = conn.execute(
        f"SELECT COUNT(*) AS n, MIN({waiting_since}) AS oldest FROM outbox_messages "
        f"WHERE status IN ('pending', 'sending') AND {waiting_since} < ?",
        (cutoff,),
    ).fetchone()
    if row["n"]:
        waiting = (now - parse_iso(row["oldest"])).total_seconds()
        return Check(
            "email_backlog",
            "critical",
            f"{row['n']} email(s) have been waiting longer than the {sla:.0f}s speed-to-lead SLA "
            f"(oldest {waiting / 60:.1f} min)",
            {"count": row["n"], "oldest_due": row["oldest"], "sla_seconds": sla},
        )
    return Check("email_backlog", "ok", f"no email is waiting longer than {sla:.0f}s")


def check_email_failures(conn: sqlite3.Connection, now: datetime) -> Check:
    """Messages we gave up on in the last 24 hours and that are still undelivered.

    "In the last 24 hours" is judged by when we gave up (the ``email.failed`` lead
    event), not by when the message was queued: a follow-up is queued a day before it
    is due, so its failure would otherwise never be seen. A message that an operator
    retried successfully since then no longer counts."""
    since = iso(now - timedelta(hours=24))
    given_up: dict[str, dict] = {}  # message id -> what the failure event recorded
    events = conn.execute(
        "SELECT data_json FROM lead_events WHERE type = ? AND occurred_at >= ? ORDER BY id", (EV_EMAIL_FAILED, since)
    ).fetchall()
    for event in events:
        failure = loads(event["data_json"], {})
        message = conn.execute(
            "SELECT status FROM outbox_messages WHERE id = ?", (failure.get("message_id"),)
        ).fetchone()
        if message is not None and message["status"] == "failed":
            given_up[failure["message_id"]] = failure
    if not given_up:
        return Check("email_failures", "ok", "no undeliverable email in the last 24h")
    # A bad address is the lead's problem (warning). A first response abandoned for a
    # reason on our side means a consumer who asked for help never heard from us,
    # which is as serious as a late first response (critical).
    abandoned = sum(
        1
        for failure in given_up.values()
        if failure.get("template") in FIRST_TOUCH_TEMPLATES and not failure.get("permanent")
    )
    summary = f"{len(given_up)} email(s) could not be delivered in the last 24h"
    if abandoned:
        summary += f"; {abandoned} first response(s) were abandoned because sending failed on our side"
    return Check(
        "email_failures",
        "critical" if abandoned else "warning",
        summary,
        {"count": len(given_up), "first_responses_abandoned": abandoned},
    )


def _is_weekend(day: date) -> bool:
    return day.weekday() >= 5


def expected_leads_between(
    hourly_rate: list[float], start: datetime, end: datetime, zone: Any, weekend_rate: list[float] | None = None
) -> float:
    """Leads we would expect between two instants, given a per-hour-of-day arrival rate
    (and, optionally, a separate one for Saturdays and Sundays). Walking hour by hour
    means a quiet night does not look like an outage."""
    expected = 0.0
    cursor = start
    while cursor < end:
        local = cursor.astimezone(zone)
        next_hour = (local.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)).astimezone(cursor.tzinfo)
        step_end = min(end, next_hour)
        if step_end <= cursor:  # guard against a zero-length step around DST changes
            step_end = min(end, cursor + timedelta(hours=1))
        rate = weekend_rate if weekend_rate is not None and _is_weekend(local) else hourly_rate
        expected += rate[local.hour] * (step_end - cursor).total_seconds() / 3600
        cursor = step_end
    return expected


def check_lead_silence(conn: sqlite3.Connection, settings: Settings, now: datetime) -> list[Check]:
    """Dead man's switch: Angi may stop sending (or start sending somewhere else)
    without any error ever reaching us, so silence itself has to raise the alarm."""
    checks: list[Check] = []

    # (a) Optional fixed threshold across all Angi traffic. Real traffic only: the
    #     canary posts every few minutes and a junk body can come from anyone, so
    #     neither proves that Angi is still sending. A quarantined lead does.
    if settings.silence_alert_minutes > 0:
        last = conn.execute(
            """SELECT MAX(e.received_at) AS last
               FROM webhook_events e LEFT JOIN tenants t ON t.id = e.tenant_id
               WHERE e.status NOT IN ('rejected', 'discarded') AND COALESCE(t.is_test, 0) = 0"""
        ).fetchone()["last"]
        if last is None:
            checks.append(Check("lead_silence", "ok", "no Angi traffic received yet"))
        else:
            minutes = (now - parse_iso(last)).total_seconds() / 60
            limit = settings.silence_alert_minutes
            status = "warning" if minutes > limit else "ok"
            checks.append(
                Check(
                    "lead_silence",
                    status,
                    f"last Angi webhook was {minutes:.0f} min ago (limit {limit} min)",
                    {"last_event_at": last, "minutes_since": round(minutes, 1), "limit_minutes": limit},
                )
            )

    # (b) Per tenant, learned from that tenant's own history. A deactivated tenant and
    #     the canary's test tenant are expected to be silent and are not checked.
    tenants = conn.execute("SELECT * FROM tenants WHERE is_active = 1 AND is_test = 0").fetchall()
    checks.extend(_tenant_silence(conn, dict(tenant), now) for tenant in tenants)
    return checks


def _tenant_silence(conn: sqlite3.Connection, tenant: dict, now: datetime) -> Check:
    """Has this tenant been quiet for longer than its own history can explain?

    Arrivals are modelled as a Poisson process whose rate depends on the hour of the
    day and on whether it is a weekend. From that we add up how many leads should have
    arrived since the last one; getting none when ``expected`` were due happens by
    chance with probability exp(-expected), and we alert below 0.1% (about 7 leads).

    Three decisions keep the check honest:

    * The baseline is the 14 days that END AT THE LAST LEAD, not at "now". Measured up
      to now, the outage would count as part of the normal rhythm: every silent day
      lowers the rate, until the window is empty and the alert clears itself while
      still nothing is arriving. Anchored at the last lead, the baseline cannot change
      during an outage, so only a new lead (or deactivating the tenant) ends the alert.
    * A baseline needs at least 20 leads over at least 7 days. A burst from a tenant
      that went live this morning is not a rhythm, and a week always contains both
      weekdays and a weekend.
    * Weekdays and weekend days have separate rates, so a business whose leads come
      Monday to Friday is not "silent" every Saturday.
    """
    name = f"lead_silence:{tenant['slug']}"
    zone = tenant_zone(tenant)
    last_at = conn.execute(
        "SELECT MAX(received_at) AS last FROM leads WHERE tenant_id = ? AND received_at <= ?",
        (tenant["id"], iso(now)),
    ).fetchone()["last"]
    history: list[datetime] = []  # arrival times in the tenant's local time, oldest first
    if last_at:
        history = [
            parse_iso(row["received_at"]).astimezone(zone)
            for row in conn.execute(
                "SELECT received_at FROM leads WHERE tenant_id = ? AND received_at >= ? AND received_at <= ? "
                "ORDER BY received_at",
                (tenant["id"], iso(parse_iso(last_at) - timedelta(days=SILENCE_HISTORY_DAYS)), last_at),
            )
        ]
    observed_days = (history[-1].date() - history[0].date()).days + 1 if history else 0
    if len(history) < SILENCE_MIN_HISTORY or observed_days < SILENCE_MIN_DAYS:
        return Check(
            name,
            "ok",
            f"{tenant['name']}: not enough history for a baseline ({len(history)} leads over {observed_days} days)",
        )

    # Leads per hour of the day, averaged over the weekdays and over the weekend days
    # in the observed span. (Seven or more days always include both kinds.)
    days = [history[0].date() + timedelta(days=offset) for offset in range(observed_days)]
    weekend_days = sum(1 for day in days if _is_weekend(day))
    weekdays = observed_days - weekend_days
    weekday_rate, weekend_rate = [0.0] * 24, [0.0] * 24
    for moment in history:
        if _is_weekend(moment):
            weekend_rate[moment.hour] += 1 / weekend_days
        else:
            weekday_rate[moment.hour] += 1 / weekdays

    last = history[-1]
    expected = expected_leads_between(weekday_rate, last, now, zone, weekend_rate=weekend_rate)
    probability = math.exp(-expected)
    hours_quiet = (now - last).total_seconds() / 3600
    detail = {
        "tenant_id": tenant["id"],
        "last_lead_at": iso(last),
        "hours_quiet": round(hours_quiet, 1),
        "expected_leads": round(expected, 1),
        "probability_by_chance": round(probability, 5),
        "baseline_leads_per_day": round(len(history) / observed_days, 1),
        "baseline_days": observed_days,
    }
    if probability < SILENCE_P_THRESHOLD:
        return Check(
            name,
            "warning",
            f"No Angi leads for {tenant['name']} in {hours_quiet:.1f}h; about {expected:.0f} were "
            f"expected from the {observed_days} days of history before that",
            detail,
        )
    return Check(name, "ok", f"{tenant['name']}: lead flow is within its normal range", detail)


def check_canary(conn: sqlite3.Connection, settings: Settings, now: datetime) -> Check:
    """The synthetic probe (scripts/canary.py) posts a lead for a test tenant through
    the public URL. Its leads are ordinary rows, so we can judge them here: the newest
    one must have been emailed, and (when the probe's schedule is configured) it must
    not be older than a few runs, because a probe that stopped arriving proves nothing."""
    row = conn.execute(
        """SELECT l.id, l.received_at, l.first_contacted_at
           FROM leads l JOIN tenants t ON t.id = l.tenant_id
           WHERE t.is_test = 1 ORDER BY l.received_at DESC LIMIT 1"""
    ).fetchone()
    if row is None:
        return Check("canary", "ok", "no canary lead has been sent yet")
    age_seconds = (now - parse_iso(row["received_at"])).total_seconds()
    detail = {"lead_id": row["id"], "received_at": row["received_at"], "first_contacted_at": row["first_contacted_at"]}
    if row["first_contacted_at"] is None and age_seconds > settings.email_sla_seconds:
        return Check(
            "canary",
            "critical",
            f"canary lead {row['id']} was received {age_seconds / 60:.1f} min ago but its email never went out",
            detail,
        )
    interval = settings.canary_interval_minutes
    if interval > 0 and age_seconds > CANARY_MISSED_RUNS * interval * 60:
        return Check(
            "canary",
            "critical",
            f"no canary lead has arrived for {age_seconds / 60:.0f} min (one is expected every {interval} min)",
            detail,
        )
    return Check("canary", "ok", f"last canary lead processed ({age_seconds / 60:.0f} min ago)", detail)
