"""Operator API (JSON), protected by the admin token.

Everything the dashboard shows is available here too, so the same actions can be
scripted: adding a tenant mapping, replaying quarantined leads, recording job outcomes,
pulling analytics, exporting the duplicate-lead credit pack.

Answers follow one rule: an id that does not exist is 404, a value that makes no sense
is 422, an action that does not fit the current state is 409. Every number is bounded
(and may not be NaN or Infinity), so a typo can never surface as a server error.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field, field_validator

from app import analytics, credits, monitoring, outbox, queries
from app.alerts import list_alerts, resolve_alert_by_id
from app.context import AppContext
from app.repo import get_tenant
from app.security import require_admin, require_same_origin
from app.tenants import SOURCE_ANGI, add_mapping, create_tenant, list_tenants
from app.util import is_example_address, scrub

# The same-origin check is for browsers: once an operator has logged in to the dashboard,
# the browser attaches those credentials to requests for this API too, including a POST
# triggered by a form on somebody else's page. Scripts send no Origin header and pass.
router = APIRouter(
    prefix="/admin/api", tags=["admin"], dependencies=[Depends(require_admin), Depends(require_same_origin)]
)

MAX_DOLLARS = 1_000_000_000  # keeps amounts inside what the integer-cents columns can hold
MAX_DAYS = 3650  # ten years: far beyond any real report, far below what date arithmetic can take
MAX_ROW_ID = 2**63 - 1  # the largest id SQLite can store


def _ctx(request: Request) -> AppContext:
    return request.app.state.ctx


def _dollars_to_cents(amount: float | None) -> int | None:
    return None if amount is None else round(amount * 100)


# --- leads -------------------------------------------------------------------------------


@router.get("/leads")
def get_leads(
    request: Request,
    tenant_id: str | None = None,
    status: str | None = None,
    q: str | None = None,
    duplicates: bool = False,
    needs_review: bool = False,
    limit: int = 100,
) -> dict[str, Any]:
    with _ctx(request).db.read() as conn:
        leads = queries.list_leads(
            conn, tenant_id=tenant_id, status=status, search=q,
            duplicates_only=duplicates, needs_review=needs_review, limit=limit,
        )
    return {"count": len(leads), "leads": leads}


@router.get("/leads/{lead_id}")
def get_lead_detail(request: Request, lead_id: str) -> dict[str, Any]:
    with _ctx(request).db.read() as conn:
        detail = queries.lead_detail(conn, lead_id)
    if detail is None:
        raise HTTPException(404, "lead not found")
    return detail


def _dollars(description: str) -> Any:
    """An optional dollar amount: zero or more, bounded, and a real number."""
    return Field(default=None, ge=0, le=MAX_DOLLARS, allow_inf_nan=False, description=description)


class OutcomeBody(BaseModel):
    outcome: str = Field(description="won | lost")
    job_value: float | None = _dollars("Revenue from the job, in dollars (won only)")
    reason: str | None = None


@router.post("/leads/{lead_id}/outcome")
def post_outcome(request: Request, lead_id: str, body: OutcomeBody) -> dict[str, Any]:
    """Record how the lead ended. In production this is called by the tenant's
    field-service system (ServiceTitan, Housecall Pro...) when a job closes."""
    ctx = _ctx(request)
    try:
        with ctx.db.transaction() as conn:
            lead = queries.record_outcome(
                conn, lead_id, outcome=body.outcome, now=ctx.now(),
                job_value_cents=_dollars_to_cents(body.job_value), reason=body.reason,
            )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if lead is None:
        raise HTTPException(404, "lead not found")
    return {"lead": lead}


# --- webhook events / replay ---------------------------------------------------------------


@router.get("/events")
def get_events(request: Request, status: str | None = None, limit: int = 100) -> dict[str, Any]:
    with _ctx(request).db.read() as conn:
        events = queries.list_events(conn, status=status, limit=limit)
    return {"count": len(events), "events": events}


@router.get("/events/{event_id}")
def get_event(request: Request, event_id: str) -> dict[str, Any]:
    with _ctx(request).db.read() as conn:
        event = queries.event_detail(conn, event_id)
    if event is None:
        raise HTTPException(404, "event not found")
    return event


class ReplayBody(BaseModel):
    tenant_id: str | None = Field(default=None, description="The tenant this lead belongs to, decided by an operator")


@router.post("/events/{event_id}/replay")
def replay_event(request: Request, event_id: str, body: ReplayBody | None = None) -> dict[str, Any]:
    """Run a stored webhook event through the pipeline again (after fixing a mapping
    or the parser). Idempotent: an event that already produced a lead is a no-op.

    The optional body names the tenant. That is how a lead whose payload carries no
    Angi account id gets out of quarantine: nothing in it says which business it is
    for, so a person has to."""
    ctx = _ctx(request)
    # Scrubbed like any text from outside: half an emoji cannot even be looked up in SQLite.
    tenant_id = scrub(body.tenant_id) if body and body.tenant_id else None
    with ctx.db.read() as conn:
        if queries.event_detail(conn, event_id) is None:
            raise HTTPException(404, "event not found")
        if tenant_id and get_tenant(conn, tenant_id) is None:
            raise HTTPException(404, "tenant not found")
    outcome = ctx.ingest.process_event(event_id, assign_tenant_id=tenant_id)
    return {"httpStatus": outcome.http_status, **outcome.body}


@router.post("/events/{event_id}/discard")
def discard_event(request: Request, event_id: str) -> dict[str, Any]:
    """Declare that a stored request will never become a lead (junk, a ping, a test).
    It leaves the quarantine, so the health checks stop counting it; the raw body
    stays on record."""
    ctx = _ctx(request)
    with ctx.db.transaction() as conn:
        event = queries.event_detail(conn, event_id)
        discarded = event is not None and queries.discard_event(conn, event_id, ctx.now())
    if event is None:
        raise HTTPException(404, "event not found")
    if not discarded:
        raise HTTPException(409, f"an event with status {event['status']!r} cannot be discarded")
    return {"discarded": True, "eventId": event_id}


@router.post("/events/replay-quarantined")
def replay_quarantined(request: Request, reason: str | None = None) -> dict[str, Any]:
    results = _ctx(request).ingest.replay_quarantined(reason)
    return {"replayed": len(results), "results": results}


# --- outbound email -----------------------------------------------------------------------


@router.post("/messages/{message_id}/retry")
def retry_message(request: Request, message_id: str) -> dict[str, Any]:
    """Give an email we had given up on a fresh set of attempts, for example after the
    mail credentials were fixed. The worker is woken, so it goes out straight away."""
    ctx = _ctx(request)
    with ctx.db.transaction() as conn:
        message = conn.execute(
            "SELECT status, to_address FROM outbox_messages WHERE id = ?", (message_id,)
        ).fetchone()
        # The same rule as the dashboard's Retry: the worker refuses such an address again at once.
        refused = (
            message is not None
            and message["status"] == "failed"
            and getattr(ctx.email_provider, "delivers_externally", False)
            and is_example_address(ctx.settings.email_redirect_all_to or message["to_address"])
        )
        requeued = message is not None and not refused and outbox.requeue_failed(conn, message_id, ctx.now())
    if message is None:
        raise HTTPException(404, "message not found")
    if refused:
        raise HTTPException(
            409,
            "that address is only ever an example (a reserved example domain, or the one in Angi's documentation), "
            "so it is never handed to the mail server; nothing was queued",
        )
    if not requeued:
        raise HTTPException(409, f"only a failed message can be retried; this one is {message['status']!r}")
    ctx.worker.wake()
    return {"requeued": True, "messageId": message_id}


# --- tenants and mappings -----------------------------------------------------------------


@router.get("/tenants")
def get_tenants(request: Request) -> dict[str, Any]:
    with _ctx(request).db.read() as conn:
        return {"tenants": list_tenants(conn)}


class TenantBody(BaseModel):
    slug: str = Field(min_length=2, max_length=60, pattern=r"^[a-z0-9][a-z0-9-]*$")
    name: str = Field(min_length=1, max_length=120)
    industry: str | None = None
    timezone: str = "America/Los_Angeles"
    phone: str | None = None
    reply_to_email: str | None = None
    business_hours_start: int = Field(default=8, ge=0, le=23)
    business_hours_end: int = Field(default=17, ge=1, le=24)
    appointment_minutes: int = Field(default=120, ge=1, le=24 * 60)


@router.post("/tenants", status_code=201)
def post_tenant(request: Request, body: TenantBody) -> dict[str, Any]:
    ctx = _ctx(request)
    try:
        with ctx.db.transaction() as conn:
            if conn.execute("SELECT 1 FROM tenants WHERE slug = ?", (body.slug,)).fetchone():
                raise HTTPException(409, f"a tenant with slug {body.slug!r} already exists")
            tenant = create_tenant(conn, now=ctx.now(), **body.model_dump())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"tenant": tenant}


class MappingBody(BaseModel):
    # Angi sends this id as a string in one feed and as a number in the other.
    external_account_id: str | int = Field(description="ALAccountId / spEntityId")
    external_account_name: str | None = None
    default_fee: float | None = _dollars("Price per lead in dollars")

    @field_validator("external_account_id")
    @classmethod
    def _account_id_as_text(cls, value: str | int) -> str:
        text = str(value).strip()
        if not text or len(text) > 100:
            raise ValueError("external_account_id must be 1-100 characters")
        return text


@router.post("/tenants/{tenant_id}/lead-sources", status_code=201)
def post_mapping(request: Request, tenant_id: str, body: MappingBody) -> dict[str, Any]:
    """Map an Angi account to a tenant, then immediately process any leads that were
    quarantined waiting for exactly this mapping."""
    ctx = _ctx(request)
    try:
        with ctx.db.transaction() as conn:
            if get_tenant(conn, tenant_id) is None:
                raise HTTPException(404, "tenant not found")
            mapping = add_mapping(
                conn,
                tenant_id=tenant_id,
                source=SOURCE_ANGI,
                external_account_id=body.external_account_id,
                external_account_name=body.external_account_name,
                default_fee_cents=_dollars_to_cents(body.default_fee),
                now=ctx.now(),
            )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    # Everything waiting on this account, whatever the reason it was held for (not
    # mapped yet, mapped to another tenant, tenant inactive): the mapping just changed.
    replayed = ctx.ingest.replay_quarantined(account_id=mapping["external_account_id"])
    return {"mapping": mapping, "replayed": replayed}


# --- analytics ----------------------------------------------------------------------------


@router.get("/analytics/summary")
def get_summary(
    request: Request,
    tenant_id: str | None = None,
    days: int | None = Query(default=None, ge=0, le=MAX_DAYS, description="Look back this many days; 0 = all time"),
) -> dict[str, Any]:
    ctx = _ctx(request)
    since = ctx.now() - timedelta(days=days) if days else None
    with ctx.db.read() as conn:
        return analytics.summary(conn, tenant_id=tenant_id, since=since)


# --- duplicate-lead credit claims -----------------------------------------------------------


@router.get("/claims")
def get_claims(request: Request, tenant_id: str | None = None, status: str | None = None) -> dict[str, Any]:
    ctx = _ctx(request)
    with ctx.db.read() as conn:
        claims = credits.list_claims(conn, tenant_id=tenant_id, status=status, now=ctx.now(), settings=ctx.settings)
        summary = credits.summarize(claims)
    return {"summary": summary, "claims": claims}


@router.get("/claims/export.csv", response_class=PlainTextResponse)
def export_claims(request: Request, tenant_id: str | None = None, status: str | None = None) -> PlainTextResponse:
    ctx = _ctx(request)
    with ctx.db.read() as conn:
        claims = credits.list_claims(conn, tenant_id=tenant_id, status=status, now=ctx.now(), settings=ctx.settings)
    return PlainTextResponse(
        credits.export_csv(claims),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="angi-duplicate-lead-credits.csv"'},
    )


class ClaimStatusBody(BaseModel):
    status: str = Field(description="detected | submitted | credited | denied | dismissed")
    credited: float | None = _dollars("Dollars actually credited")
    notes: str | None = None


@router.post("/claims/{claim_id}/status")
def post_claim_status(request: Request, claim_id: str, body: ClaimStatusBody) -> dict[str, Any]:
    ctx = _ctx(request)
    try:
        with ctx.db.transaction() as conn:
            claim = credits.set_status(
                conn, claim_id, body.status, ctx.now(),
                credited_cents=_dollars_to_cents(body.credited), notes=body.notes,
            )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if claim is None:
        raise HTTPException(404, "claim not found")
    return {"claim": claim}


# --- monitoring ---------------------------------------------------------------------------


@router.get("/health")
def get_health(request: Request) -> dict[str, Any]:
    """Every health check, evaluated now (read-only: does not open or close alerts)."""
    ctx = _ctx(request)
    with ctx.db.read() as conn:
        checks = monitoring.evaluate(conn, ctx.settings, ctx.now())
    return {
        "status": monitoring.overall_status(checks),
        "worker_running": ctx.worker.is_running,
        "checks": [check.as_dict() for check in checks],
    }


@router.get("/alerts")
def get_alerts(request: Request, status: str | None = None) -> dict[str, Any]:
    with _ctx(request).db.read() as conn:
        return {"alerts": list_alerts(conn, status)}


@router.post("/alerts/{alert_id}/resolve")
def post_resolve_alert(request: Request, alert_id: int = Path(ge=1, le=MAX_ROW_ID)) -> dict[str, Any]:
    ctx = _ctx(request)
    with ctx.db.transaction() as conn:
        resolved = resolve_alert_by_id(conn, alert_id, ctx.now())
    if not resolved:
        raise HTTPException(404, "no open alert with that id")
    return {"resolved": True}


@router.get("/payload-shapes")
def get_payload_shapes(request: Request) -> dict[str, Any]:
    with _ctx(request).db.read() as conn:
        return {"shapes": queries.payload_shapes(conn)}
