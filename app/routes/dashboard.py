"""Operator dashboard: server-rendered HTML over the same read models as the admin API.

    GET  /dashboard                       overview: KPIs, funnel, leads per day
    GET  /dashboard/leads                 searchable lead list
    GET  /dashboard/leads/{id}            one lead: details, timeline, messages, payload
    GET  /dashboard/credits               duplicate-lead credit claims
    GET  /dashboard/monitoring            health checks, alerts, payload shapes, webhook events
    GET  /dashboard/tenants               tenants and their Angi account mappings

Every action is a plain HTML form: it POSTs, the handler does the work and answers with
a 303 redirect back to the page the form was on (so a browser refresh never repeats the
action). The outcome is shown through a short message *code* in the query string
(``?msg=outcome_saved``), looked up in ``FLASH`` below, so no text from a URL is ever
echoed onto a page.

    POST /dashboard/leads/{id}/outcome           record won / lost
    POST /dashboard/messages/{id}/retry          send a failed email again
    POST /dashboard/credits/{id}/status          move a credit claim along
    POST /dashboard/alerts/{id}/resolve          close an alert
    POST /dashboard/events/{id}/replay           run a stored request through the pipeline again
    POST /dashboard/events/{id}/assign           say which tenant a lead with no Angi account id is for
    POST /dashboard/events/{id}/discard          this stored request will never be a lead
    POST /dashboard/events/replay-quarantined    replay everything in quarantine
    POST /dashboard/tenants                      create a tenant
    POST /dashboard/tenants/{id}/lead-sources    map an Angi account to a tenant

All lead data is untrusted. Templates rely on Jinja's autoescaping and nothing here
marks a value as safe.
"""

from __future__ import annotations

import json
import math
import re
from datetime import date, timedelta
from typing import Any
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from jinja2 import Environment

from app import analytics, credits, monitoring, outbox, queries
from app.alerts import list_alerts, resolve_alert_by_id
from app.booking import slot_from_appointment, tenant_zone
from app.context import AppContext
from app.repo import get_lead, get_tenant, get_tenant_by_slug, kv_get
from app.security import require_admin, require_same_origin
from app.tenants import (
    SOURCE_ANGI,
    add_mapping,
    create_tenant,
    find_mapping,
    list_tenants,
    valid_timezone,
)
from app.util import format_cents, is_example_address, iso

# Every route needs the admin token. The browser sends it with every request to this
# site, including one triggered by a form on somebody else's page, so every POST is
# also checked for where it came from (the check lets GET requests through).
router = APIRouter(
    prefix="/dashboard",
    tags=["dashboard"],
    dependencies=[Depends(require_admin), Depends(require_same_origin)],
)

# --- vocabulary ----------------------------------------------------------------------------

PERIODS = {"7": 7, "30": 30, "90": 90, "all": None}  # the period filter -> days back (None = no limit)
DEFAULT_PERIOD = "30"
LIVE_REFRESH_SECONDS = 10

EVENT_STATUSES = ("received", "processed", "duplicate_delivery", "quarantined", "rejected", "failed", "discarded")
REPLAYABLE_STATUSES = ("quarantined", "failed", "rejected")
# The one quarantine reason no mapping can fix: the payload names no Angi account, so
# an operator has to say which tenant the lead is for.
NEEDS_TENANT_REASON = "no_account_id"
# What `IngestService.process_event` reports for an event that ended up as a lead.
REPLAY_SUCCESSES = ("processed", "updated", "duplicate_delivery")

DEFAULT_TIMEZONE = "America/Los_Angeles"
SLUG_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{1,59}")  # same rule as the admin API
DOLLARS_PATTERN = re.compile(r"[0-9]{1,9}(\.[0-9]{1,2})?")

# Why a stored request is waiting in quarantine, in plain words: shown next to the
# event and in the message after a replay that did not release it.
QUARANTINE_REASONS = {
    "unmapped_account": "its Angi account is not mapped to a tenant. Add the mapping on the Tenants page",
    "no_account_id": "the payload names no Angi account, so no mapping can route it. Assign a tenant to it",
    "tenant_inactive": "the tenant it belongs to is deactivated",
    "mapping_conflict": (
        "it arrived addressed to one tenant (by webhook URL or crmKey) but its Angi account is mapped "
        "to another. If the mapping is wrong, correct it on the Tenants page (this request is then replayed). "
        "If the URL registered with Angi is wrong, correct it there; this stored request then has to be "
        "assigned to the mapped tenant through the admin API, or discarded"
    ),
    "tenant_conflict": (
        "it would change a lead that belongs to a different tenant than the one it arrived for, "
        "which is never done automatically"
    ),
    "unrecognised_payload": "the payload matches no known Angi format and carries no contact details",
}

# Messages shown after a form was handled: code -> (tone, text). {n} and {ok} are counts.
FLASH: dict[str, tuple[str, str]] = {
    "outcome_saved": ("good", "Outcome recorded."),
    "outcome_invalid": ("critical", "Choose whether the job was won or lost."),
    "amount_invalid": ("critical", "That is not a valid dollar amount. Use a figure such as 1250 or 1,250.50."),
    "claim_updated": ("good", "Claim updated."),
    "claim_invalid": ("critical", "Choose a status for the claim."),
    "credit_too_large": ("critical", "The credited amount cannot be larger than the fee charged for the lead."),
    "alert_resolved": ("good", "Alert resolved."),
    "alert_already_resolved": ("good", "That alert was already resolved."),
    "replay_processed": ("good", "Event replayed: it is now a lead."),
    "replay_updated": ("good", "Event replayed: the lead it belongs to was updated."),
    "replay_duplicate_delivery": ("good", "Event replayed: this lead was already stored, so nothing changed."),
    "replay_quarantined": ("warning", "Event replayed, but it is still quarantined: the cause has not been fixed yet."),
    **{
        f"replay_quarantined_{reason}": ("warning", f"Event replayed, but it is still quarantined: {why}.")
        for reason, why in QUARANTINE_REASONS.items()
    },
    "replay_rejected": ("critical", "Event replayed, but its body still cannot be read as a JSON object."),
    "replay_accepted": ("critical", "Event replayed, but processing failed again. See the alerts on this page."),
    "replay_not_needed": ("good", "That event has already been processed; there is nothing to replay."),
    "replay_discarded": ("warning", "That event was discarded, so it is not replayed."),
    "replayed_all": ("good", "Replayed {n} quarantined event(s); {ok} became leads."),
    "assign_invalid": ("critical", "Choose the (active) tenant this lead is for."),
    "assign_not_needed": (
        "warning",
        "Only an event quarantined for want of an Angi account id can be assigned a tenant by hand.",
    ),
    "event_discarded": ("good", "Event discarded. The stored request stays on record."),
    "discard_not_possible": (
        "warning",
        "Only a quarantined, rejected or failed event can be discarded; that one is none of these.",
    ),
    "message_requeued": ("good", "Email queued again; it is sent within a few seconds."),
    "message_not_failed": ("warning", "That email is not in the failed state, so there is nothing to retry."),
    "message_reserved_address": (
        "warning",
        "That address is only ever an example (a reserved example domain, or the one in Angi's documentation), "
        "so it is never handed to the mail server; nothing was queued.",
    ),
    "mapping_added": ("good", "Mapping saved. Replayed {n} quarantined event(s) of that account; {ok} became leads."),
    "mapping_moved": (
        "warning",
        "Mapping saved. That Angi account was mapped to a different tenant before and now belongs to "
        "this one. Replayed {n} quarantined event(s) of that account; {ok} became leads.",
    ),
    "mapping_invalid": ("critical", "An Angi account id is required."),
    "tenant_created": ("good", "Tenant created."),
}
FLASH_PARAMS = ("msg", "n", "ok")

# Sent with every page. The pages hold customer data, so: never cached, never framed by
# another site, and nothing may be loaded from or posted to anywhere but this server
# (which also guarantees the dashboard works offline). Styles and the one small script
# are inline, hence 'unsafe-inline'.
PAGE_HEADERS = {
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
        "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
    ),
}

# Which of the four badge looks a status word gets. Anything not listed is "neutral".
BADGE_TONES = {
    "booked": "good",  # leads
    "won": "good",
    "processed": "good",  # webhook events ("discarded" is deliberately neutral: dealt with, nothing to do)
    "quarantined": "warning",
    "rejected": "critical",
    "failed": "critical",
    "sent": "good",  # outbox messages
    "detected": "warning",  # credit claims: waiting to be filed
    "credited": "good",
    "ok": "good",  # health checks and alerts
    "resolved": "good",
    "warning": "warning",
    "critical": "critical",
    "clean": "good",  # payload shapes
    "drifted": "warning",
}

EVENT_LABELS = {
    "lead.received": "Lead received from Angi",
    "lead.duplicate_detected": "Recognised as a duplicate of an earlier lead",
    "webhook.redelivered": "Angi delivered the same lead again (nothing was created or sent)",
    "lead.updated": "Lead updated by a corrected delivery from Angi",
    "email.queued": "Email queued",
    "email.sent": "Email sent",
    "email.retry_scheduled": "Email could not be sent; a retry is scheduled",
    "email.failed": "Email failed for good",
    "email.skipped": "No email sent",
    "email.cancelled": "Pending email cancelled",
    "booking.link_clicked": "Booking link opened",
    "booking.appointment_booked": "Appointment booked",
    "booking.rescheduled": "Appointment rescheduled",
    "lead.unsubscribed": "Unsubscribed from email",
    "lead.outcome_recorded": "Outcome recorded",
}

FUNNEL_STAGES = (
    ("received", "Received"),
    ("contacted", "Contacted"),
    ("engaged", "Engaged"),
    ("booked", "Booked"),
    ("won", "Won"),
)

# Leads-per-day chart geometry. Horizontal positions are percentages of the chart width
# (so the chart is fluid and its text stays crisp on a phone); vertical ones are pixels.
CHART_PLOT_HEIGHT = 140
CHART_TOP_PADDING = 12  # room for the top gridline's label
CHART_AXIS_BAND = 24  # room for the day labels under the baseline
CHART_MAX_DAYS = 90
CHART_BAR_MAX_PERCENT = 2.0  # about 24px on a wide screen: columns stay thin however few days there are
CHART_DAY_LABELS = 6
CHART_BAR_CORNER = 4  # px radius of a column's rounded top


# --- template filters ------------------------------------------------------------------------


def format_dt(value: Any) -> str:
    """Stored timestamps are fixed-width UTC ISO strings, so the first 16 characters are
    exactly "YYYY-MM-DDTHH:MM"."""
    if not isinstance(value, str) or not value:
        return "—"
    return value[:16].replace("T", " ")


def format_dt_seconds(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return "—"
    return value[:19].replace("T", " ")


def format_duration(seconds: Any) -> str:
    """Seconds -> the two most useful units: "0.4 s", "42 s", "3 min 05 s", "2 h 10 min", "3 d 4 h"."""
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool):
        return "—"
    if seconds < 0:
        return "—"  # a clock went backwards; do not print a negative wait
    if 0 < seconds < 0.1:
        # The console provider answers in milliseconds; "0.0 s" would read as "not measured".
        return f"{max(1, round(seconds * 1000))} ms"
    if seconds < 10:
        return f"{seconds:.1f} s"
    whole = round(seconds)
    if whole < 60:
        return f"{whole} s"
    if whole < 3600:
        return f"{whole // 60} min {whole % 60:02d} s"
    if whole < 86400:
        return f"{whole // 3600} h {whole % 3600 // 60:02d} min"
    return f"{whole // 86400} d {whole % 86400 // 3600} h"


def format_pct(ratio: Any) -> str:
    """0.375 -> "37.5%", 1 -> "100%"."""
    if not isinstance(ratio, (int, float)) or isinstance(ratio, bool):
        return "—"
    text = f"{ratio * 100:.1f}"
    return text.removesuffix(".0") + "%"


def format_times(ratio: Any) -> str:
    """Return on spend: 3.2 -> "3.2x" (revenue per $1 spent)."""
    if not isinstance(ratio, (int, float)) or isinstance(ratio, bool):
        return "—"
    return f"{ratio:.1f}x"


def format_gap(hours: Any) -> str:
    """Time between a duplicate and its original: hours up to two days, then days."""
    if not isinstance(hours, (int, float)) or isinstance(hours, bool):
        return "—"
    return f"{hours:.1f} h" if hours < 48 else f"{hours / 24:.1f} d"


def format_days_left(days: Any) -> str:
    if not isinstance(days, (int, float)) or isinstance(days, bool):
        return "—"
    if days < 0:
        return f"overdue by {-days:.0f} d" if days <= -1 else "overdue"
    if days < 1:
        return "under a day left"
    return f"{int(days)} d left"


def pretty_json(value: Any) -> str:
    """Indented JSON for a <pre>. Returns an ordinary string, so the template escapes it
    like any other untrusted text. A string that is not JSON is shown as it is."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return value
    return json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True, default=str)


def plain_value(value: Any) -> str:
    """One value from an event's data, readable on one line."""
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, str) and len(value) == 24 and value[10] == "T" and value.endswith("Z"):
        return format_dt_seconds(value) + " UTC"  # one of our own timestamps
    if isinstance(value, list) and all(isinstance(item, (str, int, float)) for item in value):
        return ", ".join(str(item) for item in value) or "—"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return str(value)


def event_details(data: Any) -> list[tuple[str, str]]:
    """A lifecycle event's data as (label, value) pairs for the timeline; empty values are left out."""
    if not isinstance(data, dict):
        return []
    details = []
    for key, value in sorted(data.items()):
        if value in (None, "", [], {}):
            continue
        if key.endswith("_cents") and isinstance(value, int):
            details.append((key.removesuffix("_cents").replace("_", " "), format_cents(value)))  # money in dollars
        else:
            details.append((key.replace("_", " "), plain_value(value)))
    return details


def badge_tone(status: Any) -> str:
    return BADGE_TONES.get(str(status), "neutral")


def event_label(event_type: Any) -> str:
    return EVENT_LABELS.get(str(event_type), str(event_type))


def quarantine_reason(reason: Any) -> str | None:
    """What a quarantine reason code means, as a sentence; None for any other text."""
    why = QUARANTINE_REASONS.get(str(reason))
    return f"{why[0].upper()}{why[1:]}." if why else None


def register_filters(env: Environment) -> None:
    """Template filters used by the dashboard pages (``money`` is registered by main.py)."""
    env.filters["dt"] = format_dt
    env.filters["dt_seconds"] = format_dt_seconds
    env.filters["duration"] = format_duration
    env.filters["pct"] = format_pct
    env.filters["times"] = format_times
    env.filters["gap"] = format_gap
    env.filters["days_left"] = format_days_left
    env.filters["pretty_json"] = pretty_json
    env.filters["plain"] = plain_value
    env.filters["details"] = event_details
    env.filters["tone"] = badge_tone
    env.filters["event_label"] = event_label
    env.filters["quarantine_reason"] = quarantine_reason


# --- request helpers -------------------------------------------------------------------------


def _ctx(request: Request) -> AppContext:
    return request.app.state.ctx


def _url(path: str, **params: Any) -> str:
    """``path?query`` with empty parameters left out."""
    query = urlencode({key: value for key, value in params.items() if value not in (None, "")})
    return f"{path}?{query}" if query else path


def _whole_number(value: str | None) -> int:
    """A small non-negative integer from text; 0 for anything else."""
    return int(value) if value and value.isascii() and value.isdigit() and len(value) <= 9 else 0


def _flash(request: Request) -> dict[str, str] | None:
    entry = FLASH.get(request.query_params.get("msg", ""))
    if entry is None:
        return None
    tone, text = entry
    counts = {name: _whole_number(request.query_params.get(name)) for name in ("n", "ok")}
    return {"tone": tone, "text": text.format(**counts)}


def _is_dashboard_path(url: str) -> bool:
    """True for a local URL inside the dashboard. Anything else (another site, another
    part of this one) is never used as a redirect target."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme or parts.netloc:
        return False
    # A browser resolves "." and ".." segments before it follows the redirect (also when
    # they are written "%2e", or with "\" for "/"), which would lead out of the dashboard.
    segments = unquote(parts.path).replace("\\", "/").split("/")
    if "." in segments or ".." in segments:
        return False
    return parts.path == "/dashboard" or parts.path.startswith("/dashboard/")


def _back(next_url: str, default: str, msg: str, **counts: int) -> RedirectResponse:
    """303 back to the page the form was on (the form's hidden ``next`` field), carrying
    the message code. 303 makes the browser follow with a GET, so refreshing the page
    afterwards does not repeat the action."""
    target = urlsplit(next_url if _is_dashboard_path(next_url) else default)
    query = [(key, value) for key, value in parse_qsl(target.query) if key not in FLASH_PARAMS]
    query.append(("msg", msg))
    query.extend((name, str(value)) for name, value in counts.items())
    return RedirectResponse(f"{target.path}?{urlencode(query)}", status_code=303)


def _render(request: Request, template: str, context: dict[str, Any], status_code: int = 200) -> Response:
    """Render a dashboard page, adding what the shared layout needs."""
    ctx = _ctx(request)
    with ctx.db.read() as conn:
        open_alerts = conn.execute("SELECT COUNT(*) AS n FROM alerts WHERE status = 'open'").fetchone()["n"]
    carried = {key: value for key, value in request.query_params.items() if key not in FLASH_PARAMS}
    page = {
        "open_alerts": open_alerts,
        "flash": _flash(request),
        "here": _url(request.url.path, **carried),  # where forms on this page send the browser back to
        "now": iso(ctx.now()),
        **context,
    }
    return request.app.state.templates.TemplateResponse(
        request, f"dashboard/{template}", page, status_code=status_code, headers=PAGE_HEADERS
    )


def _refresh_alerts(ctx: AppContext) -> None:
    """After an operator action that changes what is waiting: bring the alerts in line
    with the checks now, not at the worker's next monitoring pass. Until then the page
    would show a passing check above its own, still open, alert."""
    try:
        monitoring.run_checks(ctx.db, ctx.settings, ctx.now())
    except Exception:  # noqa: BLE001 - the action itself is done; the worker's next pass catches up
        pass


def _not_found(request: Request, what: str) -> Response:
    return _render(
        request,
        "error.html",
        {"active": "", "heading": f"{what.capitalize()} not found",
         "message": f"There is no {what} with that id. It may have been typed wrongly, or the link is out of date."},
        status_code=404,
    )


def _live(live: str, path: str, **params: Any) -> dict[str, Any]:
    """``?live=1`` makes a page reload itself, so a new lead shows up during a demo."""
    return {
        "on": live == "1",
        "seconds": LIVE_REFRESH_SECONDS,
        "on_url": _url(path, **params, live="1"),
        "off_url": _url(path, **params),
    }


def _dollars_to_cents(text: str) -> int | None:
    """ "1,250.50" -> 125050; empty -> None. Whole-number arithmetic only, so no rounding
    surprises. Raises ValueError for anything that is not a plain positive amount."""
    text = text.strip().replace("$", "").replace(",", "")
    if not text:
        return None
    if not DOLLARS_PATTERN.fullmatch(text):
        raise ValueError("not a dollar amount")
    dollars, _, cents = text.partition(".")
    return int(dollars) * 100 + int(cents.ljust(2, "0"))


# --- chart data ------------------------------------------------------------------------------


def funnel_rows(funnel: dict[str, Any]) -> list[dict[str, Any]]:
    """One bar per funnel stage: its count, its width relative to the largest stage, and
    the share of the previous stage that made it this far."""
    largest = max(funnel[key] for key, _ in FUNNEL_STAGES) or 1
    rows: list[dict[str, Any]] = []
    for step, (key, label) in enumerate(FUNNEL_STAGES, start=1):
        count = funnel[key]
        previous = rows[-1] if rows else None
        rows.append(
            {
                "step": step,
                "label": label,
                "count": count,
                "width": round(100 * count / largest, 1),
                "step_rate": analytics.ratio(count, previous["count"]) if previous else None,
                "previous_label": previous["label"].lower() if previous else None,
            }
        )
    return rows


def axis_step(largest: int) -> int:
    """Gridline spacing for a count axis: the smallest of 1, 2, 5, 10, 20, 50, ... that
    covers ``largest`` in at most four steps."""
    magnitude = 1
    while True:
        for factor in (1, 2, 5):
            if largest <= 4 * factor * magnitude:
                return factor * magnitude
        magnitude *= 10


def daily_chart(by_day: list[dict[str, Any]], first_day: date | None, last_day: date) -> dict[str, Any] | None:
    """Geometry for the leads-per-day column chart (received and booked, side by side).

    ``by_day`` only lists days that had leads; the chart shows every day from
    ``first_day`` (or the first day with a lead) to ``last_day`` so that quiet days are
    visible as gaps. Returns None when there is nothing to draw.
    """
    if not by_day:
        return None
    counts = {row["day"]: row for row in by_day}
    data_days = sorted(date.fromisoformat(day) for day in counts)
    start = min(data_days[0], first_day) if first_day else data_days[0]
    end = max(data_days[-1], last_day)
    truncated = (end - start).days + 1 > CHART_MAX_DAYS
    if truncated:
        start = end - timedelta(days=CHART_MAX_DAYS - 1)
    days = [start + timedelta(days=offset) for offset in range((end - start).days + 1)]

    largest = max((counts[d.isoformat()]["received"] for d in days if d.isoformat() in counts), default=0)
    step = axis_step(max(largest, 1))
    top = max(math.ceil(largest / step), 1) * step
    baseline = CHART_TOP_PADDING + CHART_PLOT_HEIGHT

    def column(count: int, x: float) -> dict[str, float] | None:
        """Where to draw one column, or None for a zero. It is drawn CHART_BAR_CORNER too
        tall and clipped at the baseline, which leaves the top rounded and the bottom square."""
        if not count:
            return None
        height = max(round(CHART_PLOT_HEIGHT * count / top, 1), 2.0)  # never thinner than 2px
        return {"x": round(x, 3), "y": round(baseline - height, 1), "height": round(height + CHART_BAR_CORNER, 1)}

    slot = 100 / len(days)  # each day's share of the width, in percent
    bar = min(slot * 0.34, CHART_BAR_MAX_PERCENT)
    label_every = max(1, math.ceil(len(days) / CHART_DAY_LABELS))
    columns: list[dict[str, Any]] = []
    labelled_month = None
    for index, day in enumerate(days):
        row = counts.get(day.isoformat(), {"received": 0, "booked": 0})
        centre = slot * (index + 0.5)
        label = None
        if (len(days) - 1 - index) % label_every == 0:  # counted from the right, so the latest day is always named
            # Name the month once, then only when it changes: "Nov 12", "13", "14", "Dec 1".
            label = f"{day:%b} {day.day}" if day.month != labelled_month else str(day.day)
            labelled_month = day.month
        # A centred label under the first or last of many narrow slots would be cut off
        # by the chart's edge, so those two are anchored to the edge instead.
        anchor, label_x = "middle", centre
        if slot < 14 and index == 0:
            anchor, label_x = "start", 0.0
        elif slot < 14 and index == len(days) - 1:
            anchor, label_x = "end", 100.0
        columns.append(
            {
                "title": f"{day:%a} {day:%b} {day.day}: {row['received']} received, {row['booked']} booked",
                "label": label,
                "anchor": anchor,
                "label_x": round(label_x, 3),
                "slot_x": round(slot * index, 3),
                "received": column(row["received"], centre - bar),
                "booked": column(row["booked"], centre),
            }
        )
    return {
        "columns": columns,
        "slot_width": round(slot, 3),
        "bar_width": round(bar, 3),
        "ticks": [
            {"value": value, "y": round(baseline - CHART_PLOT_HEIGHT * value / top, 1)}
            for value in range(0, top + 1, step)
        ],
        "baseline": baseline,
        "height": baseline + CHART_AXIS_BAND,
        "corner": CHART_BAR_CORNER,
        "table": by_day,
        "truncated": truncated,
        "max_days": CHART_MAX_DAYS,
    }


# --- 1. overview -------------------------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
def overview(request: Request, tenant: str = "", days: str = DEFAULT_PERIOD, live: str = "") -> Response:
    ctx = _ctx(request)
    now = ctx.now()
    period = days if days in PERIODS else DEFAULT_PERIOD
    period_days = PERIODS[period]
    since = now - timedelta(days=period_days) if period_days else None

    with ctx.db.read() as conn:
        tenants = list_tenants(conn)
        selected = next((t for t in tenants if t["id"] == tenant), None)
        tenant_id = selected["id"] if selected else None
        # Test (canary) tenants are left out of the numbers unless one is picked explicitly.
        summary = analytics.summary(
            conn, tenant_id=tenant_id, since=since, include_test=bool(selected and selected["is_test"])
        )
        recent = queries.list_leads(conn, tenant_id=tenant_id, limit=10)
    if since:
        recent = [lead for lead in recent if lead["received_at"] >= iso(since)]

    booked = summary["attribution"]
    booked_total = booked["booked_after_intro_only"] + booked["booked_after_followup"]
    # The chart's days are the tenants' local days (as in analytics), so it must end on
    # their "today": after midnight UTC that is still yesterday's date in the US.
    shown = [selected] if selected else [t for t in tenants if not t["is_test"]]
    today = max((now.astimezone(tenant_zone(t)).date() for t in shown), default=now.date())
    first_day = today - timedelta(days=period_days - 1) if period_days else None
    return _render(
        request,
        "overview.html",
        {
            "active": "overview",
            "tenants": tenants,
            "tenant_id": tenant_id or "",
            "period": period,
            "summary": summary,
            # Leads with no fee in the payload and none configured on their Angi account
            # count as $0, so spend and cost per booking are understated while there are any.
            "unpriced_leads": summary["money"].get("unpriced_leads") or 0,
            "funnel": funnel_rows(summary["funnel"]),
            "chart": daily_chart(summary["by_day"], first_day, today),
            "touch": {
                "total": booked_total,
                "intro_only": booked["booked_after_intro_only"],
                "after_followup": booked["booked_after_followup"],
                "intro_share": analytics.ratio(booked["booked_after_intro_only"], booked_total),
                "followup_share": analytics.ratio(booked["booked_after_followup"], booked_total),
            },
            "recent": recent,
            "live": _live(live, "/dashboard", tenant=tenant_id, days=period),
        },
    )


# --- 2. leads ----------------------------------------------------------------------------------


@router.get("/leads", response_class=HTMLResponse)
def leads(
    request: Request, tenant: str = "", status: str = "", duplicates: str = "", review: str = "", q: str = ""
) -> Response:
    ctx = _ctx(request)
    limit = 200
    with ctx.db.read() as conn:
        tenants = list_tenants(conn)
        rows = queries.list_leads(
            conn,
            tenant_id=tenant or None,
            status=status or None,
            search=q.strip() or None,
            duplicates_only=duplicates == "1",
            needs_review=review == "1",
            limit=limit,
        )
    return _render(
        request,
        "leads.html",
        {
            "active": "leads",
            "tenants": tenants,
            "statuses": queries.LEAD_STATUSES,
            "filters": {"tenant": tenant, "status": status, "duplicates": duplicates == "1",
                        "review": review == "1", "q": q.strip()},
            "leads": rows,
            "limit": limit,
        },
    )


# --- 3. one lead -------------------------------------------------------------------------------


@router.get("/leads/{lead_id}", response_class=HTMLResponse)
def lead_detail(request: Request, lead_id: str) -> Response:
    ctx = _ctx(request)
    with ctx.db.read() as conn:
        detail = queries.lead_detail(conn, lead_id)
    if detail is None:
        return _not_found(request, "lead")
    appointment = detail["appointment"]
    return _render(
        request,
        "lead.html",
        {
            "active": "leads",
            **detail,
            "appointment_label": slot_from_appointment(appointment).label if appointment else None,
            "booking_url": f"{ctx.settings.base_url}/book/{detail['lead']['booking_token']}",
        },
    )


@router.post("/leads/{lead_id}/outcome")
def record_lead_outcome(
    request: Request,
    lead_id: str,
    outcome: str = Form(default=""),
    job_value: str = Form(default=""),
    reason: str = Form(default=""),
    next_url: str = Form(default="", alias="next"),
) -> Response:
    """Close the loop: won (with what the job was worth) or lost (with why)."""
    ctx = _ctx(request)
    here = f"/dashboard/leads/{lead_id}"
    problem = None
    job_value_cents = None
    if outcome not in ("won", "lost"):
        problem = "outcome_invalid"
    elif outcome == "won":
        try:
            job_value_cents = _dollars_to_cents(job_value)
        except ValueError:
            problem = "amount_invalid"

    with ctx.db.transaction() as conn:
        found = get_lead(conn, lead_id) is not None
        if found and problem is None:
            queries.record_outcome(
                conn,
                lead_id,
                outcome=outcome,
                now=ctx.now(),
                job_value_cents=job_value_cents,
                reason=(reason.strip()[:500] or None) if outcome == "lost" else None,
            )
    if not found:
        return _not_found(request, "lead")
    return _back(next_url, here, problem or "outcome_saved")


@router.post("/messages/{message_id}/retry")
def retry_message(request: Request, message_id: str, next_url: str = Form(default="", alias="next")) -> Response:
    """Give an email we had given up on a fresh set of attempts, for example after the
    mail credentials were fixed. Only a failed message can be retried, so a second
    click (or a stale page) never sends anything twice."""
    ctx = _ctx(request)
    with ctx.db.transaction() as conn:
        message = conn.execute(
            "SELECT lead_id, to_address, status FROM outbox_messages WHERE id = ?", (message_id,)
        ).fetchone()
        # The worker refuses such an address again at once; say so instead of "queued".
        refused = (
            message is not None
            and message["status"] == "failed"
            and getattr(ctx.email_provider, "delivers_externally", False)
            and is_example_address(ctx.settings.email_redirect_all_to or message["to_address"])
        )
        requeued = outbox.requeue_failed(conn, message_id, ctx.now()) if message and not refused else False
    if message is None:
        return _not_found(request, "message")
    if refused:
        return _back(next_url, f"/dashboard/leads/{message['lead_id']}", "message_reserved_address")
    if requeued:
        ctx.worker.wake()  # send it now rather than at the next poll
    return _back(
        next_url, f"/dashboard/leads/{message['lead_id']}", "message_requeued" if requeued else "message_not_failed"
    )


# --- 4. credits --------------------------------------------------------------------------------


@router.get("/credits", response_class=HTMLResponse)
def credit_claims(request: Request, tenant: str = "", status: str = "") -> Response:
    ctx = _ctx(request)
    status = status if status in credits.CLAIM_STATUSES else ""
    with ctx.db.read() as conn:
        tenants = list_tenants(conn)
        claims = credits.list_claims(
            conn, tenant_id=tenant or None, status=status or None, now=ctx.now(), settings=ctx.settings
        )
    return _render(
        request,
        "credits.html",
        {
            "active": "credits",
            "tenants": tenants,
            "statuses": credits.CLAIM_STATUSES,
            "filters": {"tenant": tenant, "status": status},
            "claims": claims,  # already ordered by deadline
            "summary": credits.summarize(claims),
            "warning_days": credits.DEADLINE_WARNING_DAYS,
            "export_url": _url("/admin/api/claims/export.csv", tenant_id=tenant, status=status),
            "rules": {
                "duplicate_window_days": ctx.settings.duplicate_window_days,
                "request_window_days": ctx.settings.credit_request_window_days,
                "contact_sla_hours": ctx.settings.credit_contact_sla_hours,
            },
        },
    )


@router.post("/credits/{claim_id}/status")
def update_claim_status(
    request: Request,
    claim_id: str,
    status: str = Form(default=""),
    credited: str = Form(default=""),
    next_url: str = Form(default="", alias="next"),
) -> Response:
    """Track a claim through Angi's (manual) credit process."""
    ctx = _ctx(request)
    problem = None
    credited_cents = None
    if status not in credits.CLAIM_STATUSES:
        problem = "claim_invalid"
    elif status == "credited":
        try:
            credited_cents = _dollars_to_cents(credited)  # None = the full amount of the claim
        except ValueError:
            problem = "amount_invalid"

    with ctx.db.transaction() as conn:
        found = credits.get_claim(conn, claim_id) is not None
        if found and problem is None:
            try:
                credits.set_status(conn, claim_id, status, ctx.now(), credited_cents=credited_cents)
            except ValueError:  # e.g. a credit larger than the fee that was charged
                problem = "credit_too_large"
    if not found:
        return _not_found(request, "claim")
    return _back(next_url, "/dashboard/credits", problem or "claim_updated")


# --- 5. monitoring -----------------------------------------------------------------------------


@router.get("/monitoring", response_class=HTMLResponse)
def monitoring_page(request: Request, status: str = "", live: str = "") -> Response:
    ctx = _ctx(request)
    status = status if status in EVENT_STATUSES else ""
    with ctx.db.read() as conn:
        tenants = list_tenants(conn)
        checks = monitoring.evaluate(conn, ctx.settings, ctx.now())  # read-only: does not open alerts
        alerts = list_alerts(conn)  # open ones first
        shapes = queries.payload_shapes(conn)
        events = queries.list_events(conn, status=status or None, limit=50)
        quarantined = conn.execute(
            "SELECT COUNT(*) AS n FROM webhook_events WHERE status = 'quarantined'"
        ).fetchone()["n"]
        last_run = kv_get(conn, "monitor:last_run")
    resolved = [alert for alert in alerts if alert["status"] != "open"]
    resolved.sort(key=lambda alert: alert["resolved_at"] or "", reverse=True)
    return _render(
        request,
        "monitoring.html",
        {
            "active": "monitoring",
            "overall": monitoring.overall_status(checks),
            "checks": checks,
            "open": [alert for alert in alerts if alert["status"] == "open"],
            "resolved": resolved[:20],
            "shapes": shapes,
            "events": events,
            "event_statuses": EVENT_STATUSES,
            "replayable": REPLAYABLE_STATUSES,
            "discardable": queries.DISCARDABLE_STATUSES,
            "needs_tenant": NEEDS_TENANT_REASON,
            "tenants": [tenant for tenant in tenants if tenant["is_active"]],  # who an event can be assigned to
            "status": status,
            "quarantined": quarantined,
            "worker_running": ctx.worker.is_running,
            "last_run": last_run,
            "live": _live(live, "/dashboard/monitoring", status=status),
        },
    )


@router.post("/alerts/{alert_id}/resolve")
def resolve_alert(request: Request, alert_id: str, next_url: str = Form(default="", alias="next")) -> Response:
    ctx = _ctx(request)
    # The id is taken as text so that a mistyped one gets the 404 page, not a validation error.
    number = _whole_number(alert_id)  # 0 (never a real id) when it is not a number
    with ctx.db.transaction() as conn:
        found = conn.execute("SELECT 1 FROM alerts WHERE id = ?", (number,)).fetchone() is not None
        resolved = resolve_alert_by_id(conn, number, ctx.now()) if found else False
    if not found:
        return _not_found(request, "alert")
    return _back(next_url, "/dashboard/monitoring", "alert_resolved" if resolved else "alert_already_resolved")


@router.post("/events/replay-quarantined")
def replay_all_quarantined(request: Request, next_url: str = Form(default="", alias="next")) -> Response:
    """Run every quarantined event through the pipeline again (after a mapping or parser fix)."""
    results = _ctx(request).ingest.replay_quarantined()
    _refresh_alerts(_ctx(request))
    succeeded = sum(1 for result in results if result["result"] in REPLAY_SUCCESSES)
    return _back(next_url, "/dashboard/monitoring", "replayed_all", n=len(results), ok=succeeded)


def _replay_message(outcome: Any) -> str:
    """The message code for what ``IngestService.process_event`` reported."""
    result = str(outcome.body.get("result"))
    if result == "quarantined":
        specific = f"replay_quarantined_{outcome.body.get('reason')}"
        return specific if specific in FLASH else "replay_quarantined"
    return f"replay_{result}" if f"replay_{result}" in FLASH else "replay_accepted"


@router.post("/events/{event_id}/replay")
def replay_event(request: Request, event_id: str, next_url: str = Form(default="", alias="next")) -> Response:
    ctx = _ctx(request)
    with ctx.db.read() as conn:
        event = queries.event_detail(conn, event_id)
    if event is None:
        return _not_found(request, "webhook event")
    if event["status"] == "discarded":
        return _back(next_url, "/dashboard/monitoring", "replay_discarded")
    if event["status"] not in REPLAYABLE_STATUSES:
        return _back(next_url, "/dashboard/monitoring", "replay_not_needed")
    outcome = ctx.ingest.process_event(event_id)
    _refresh_alerts(ctx)
    return _back(next_url, "/dashboard/monitoring", _replay_message(outcome))


@router.post("/events/{event_id}/assign")
def assign_event(
    request: Request,
    event_id: str,
    tenant_id: str = Form(default=""),
    next_url: str = Form(default="", alias="next"),
) -> Response:
    """A lead whose payload names no Angi account cannot be released by adding a
    mapping. A person who knows which business it is for says so here, and the stored
    request is processed for that tenant. It is the one place a human routes a lead by
    hand, so it is only offered for exactly that case."""
    ctx = _ctx(request)
    with ctx.db.read() as conn:
        event = queries.event_detail(conn, event_id)
        tenant = get_tenant(conn, tenant_id) if tenant_id else None
    if event is None:
        return _not_found(request, "webhook event")
    if event["status"] != "quarantined" or event["status_reason"] != NEEDS_TENANT_REASON:
        return _back(next_url, "/dashboard/monitoring", "assign_not_needed")
    if tenant is None or not tenant["is_active"]:
        return _back(next_url, "/dashboard/monitoring", "assign_invalid")
    outcome = ctx.ingest.process_event(event_id, assign_tenant_id=tenant["id"])
    _refresh_alerts(ctx)
    return _back(next_url, "/dashboard/monitoring", _replay_message(outcome))


@router.post("/events/{event_id}/discard")
def discard_event(request: Request, event_id: str, next_url: str = Form(default="", alias="next")) -> Response:
    """A ping, junk or a test will never become a lead. Discarding takes it out of the
    quarantine backlog (so the health check can clear) and keeps the stored request."""
    ctx = _ctx(request)
    with ctx.db.transaction() as conn:
        found = queries.event_detail(conn, event_id) is not None
        discarded = queries.discard_event(conn, event_id, ctx.now()) if found else False
    if not found:
        return _not_found(request, "webhook event")
    if discarded:
        _refresh_alerts(ctx)
    return _back(next_url, "/dashboard/monitoring", "event_discarded" if discarded else "discard_not_possible")


# --- 6. tenants --------------------------------------------------------------------------------


def _tenants_page(
    request: Request, *, form: dict[str, str] | None = None, errors: list[str] | None = None, status_code: int = 200
) -> Response:
    ctx = _ctx(request)
    with ctx.db.read() as conn:
        tenants = list_tenants(conn)
        lead_counts = {
            row["tenant_id"]: row["n"]
            for row in conn.execute("SELECT tenant_id, COUNT(*) AS n FROM leads GROUP BY tenant_id")
        }
        # Accounts whose leads are quarantined for want of a mapping: tells the operator which
        # account id to add. Read from the quarantine itself, so the hint is there for as long
        # as a lead is waiting, whatever has happened to the alert. (A lead with no account id
        # at all is not listed: a mapping cannot help it.)
        unmapped = [
            dict(row)
            for row in conn.execute(
                """SELECT external_account_id, COUNT(*) AS waiting, MAX(received_at) AS last_received_at
                   FROM webhook_events
                   WHERE status = 'quarantined' AND status_reason = 'unmapped_account'
                     AND external_account_id IS NOT NULL
                   GROUP BY external_account_id ORDER BY last_received_at DESC"""
            )
        ]
        # The business name Angi sent with those leads is kept on the alert about the account.
        names = {
            alert["detail"].get("external_account_id"): alert["detail"].get("external_account_name")
            for alert in list_alerts(conn)
            if alert["kind"] == "unmapped_account"
        }
    for account in unmapped:
        account["name"] = names.get(account["external_account_id"])
    for tenant in tenants:
        tenant["lead_count"] = lead_counts.get(tenant["id"], 0)
        tenant["webhook_url"] = f"{ctx.settings.base_url}/webhooks/angi/leads/{tenant['webhook_key']}"
    return _render(
        request,
        "tenants.html",
        {
            "active": "tenants",
            "tenants": tenants,
            "unmapped": unmapped,
            "form": form or {"slug": "", "name": "", "timezone": DEFAULT_TIMEZONE, "phone": ""},
            "errors": errors or [],
        },
        status_code=status_code,
    )


@router.get("/tenants", response_class=HTMLResponse)
def tenants_page(request: Request) -> Response:
    return _tenants_page(request)


@router.post("/tenants")
def create_tenant_from_form(
    request: Request,
    slug: str = Form(default=""),
    name: str = Form(default=""),
    timezone: str = Form(default=""),
    phone: str = Form(default=""),
) -> Response:
    """Create a tenant. On a validation problem the page is shown again (HTTP 422) with
    the messages and what was typed, instead of redirecting and losing the input."""
    ctx = _ctx(request)
    form = {
        "slug": slug.strip().lower(),
        "name": name.strip(),
        "timezone": timezone.strip() or DEFAULT_TIMEZONE,
        "phone": phone.strip(),
    }
    errors: list[str] = []
    if not SLUG_PATTERN.fullmatch(form["slug"]):
        errors.append("Slug: 2 to 60 characters; lowercase letters, digits and hyphens; must start with a letter or digit.")
    if not 1 <= len(form["name"]) <= 120:
        errors.append("Name: required, at most 120 characters.")
    if not valid_timezone(form["timezone"]):
        errors.append("Timezone: use an IANA name such as America/Denver.")
    if len(form["phone"]) > 40:
        errors.append("Phone: at most 40 characters.")

    if not errors:
        try:
            with ctx.db.transaction() as conn:
                if get_tenant_by_slug(conn, form["slug"]) is not None:
                    errors.append("Slug: a tenant with this slug already exists.")
                else:
                    create_tenant(
                        conn,
                        slug=form["slug"],
                        name=form["name"],
                        timezone=form["timezone"],
                        phone=form["phone"] or None,
                        now=ctx.now(),
                    )
        except ValueError as exc:  # create_tenant's own checks
            errors.append(str(exc))
    if errors:
        return _tenants_page(request, form=form, errors=errors, status_code=422)
    return _back("", "/dashboard/tenants", "tenant_created")


@router.post("/tenants/{tenant_id}/lead-sources")
def add_lead_source(
    request: Request,
    tenant_id: str,
    external_account_id: str = Form(default=""),
    external_account_name: str = Form(default=""),
    default_fee: str = Form(default=""),
) -> Response:
    """Map an Angi account to a tenant, then immediately process the leads of that
    account that were waiting in quarantine (and only those: another account's leads
    are not touched)."""
    ctx = _ctx(request)
    account_id = external_account_id.strip()[:100]
    problem = None
    fee_cents = None
    if not account_id:
        problem = "mapping_invalid"
    else:
        try:
            fee_cents = _dollars_to_cents(default_fee)
        except ValueError:
            problem = "amount_invalid"

    moved = False
    with ctx.db.transaction() as conn:
        found = get_tenant(conn, tenant_id) is not None
        if found and problem is None:
            existing = find_mapping(conn, SOURCE_ANGI, account_id)
            moved = existing is not None and existing["tenant_id"] != tenant_id
            add_mapping(
                conn,
                tenant_id=tenant_id,
                source=SOURCE_ANGI,
                external_account_id=account_id,
                external_account_name=external_account_name.strip()[:200] or None,
                default_fee_cents=fee_cents,
                now=ctx.now(),
            )
    if not found:
        return _not_found(request, "tenant")
    if problem:
        return _back("", "/dashboard/tenants", problem)
    results = ctx.ingest.replay_quarantined(account_id=account_id)
    _refresh_alerts(ctx)
    succeeded = sum(1 for result in results if result["result"] in REPLAY_SUCCESSES)
    return _back(
        "", "/dashboard/tenants", "mapping_moved" if moved else "mapping_added", n=len(results), ok=succeeded
    )
