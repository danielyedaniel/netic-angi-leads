"""Pages the consumer sees: self-service booking and unsubscribe.

Both are reached through the unguessable per-lead token in the email, so they need no
login and expose nothing but that one lead's own request.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app import outbox
from app.booking import (
    BookingError,
    Slot,
    available_slots,
    book_appointment,
    record_link_click,
    slot_from_appointment,
    tenant_zone,
)
from app.context import AppContext
from app.repo import (
    EV_UNSUBSCRIBED,
    add_lead_event,
    get_lead_by_token,
    get_scheduled_appointment,
    get_tenant,
)
from app.util import iso

router = APIRouter(tags=["public"])


def _group_by_day(slots: list[Slot]) -> list[dict[str, Any]]:
    days: list[dict[str, Any]] = []
    for slot in slots:
        if not days or days[-1]["label"] != slot.day_label:
            days.append({"label": slot.day_label, "slots": []})
        days[-1]["slots"].append(slot)
    return days


def _render(request: Request, name: str, context: dict[str, Any], status_code: int = 200) -> Response:
    return request.app.state.templates.TemplateResponse(request, name, context, status_code=status_code)


def _not_found(request: Request) -> Response:
    return _render(
        request,
        "public/message.html",
        {"title": "Link not found", "heading": "This link is no longer valid",
         "body": "Please use the most recent email we sent you, or give us a call."},
        status_code=404,
    )


def _booking_page(
    request: Request, ctx: AppContext, token: str, *, selected: str | None, error: str | None, status_code: int = 200
) -> Response:
    now = ctx.now()
    with ctx.db.read() as conn:
        lead = get_lead_by_token(conn, token)
        if lead is None:
            return _not_found(request)
        tenant = get_tenant(conn, lead["tenant_id"])
        appointment = get_scheduled_appointment(conn, lead["id"])
        slots = available_slots(conn, tenant, now) if tenant and tenant["is_active"] else []
    return _render(
        request,
        "public/booking.html",
        {
            "lead": lead,
            "tenant": tenant,
            "first_name": lead["first_name"] or "there",
            "appointment": slot_from_appointment(appointment) if appointment else None,
            "days": _group_by_day(slots),
            "selected": selected if selected in {slot.key for slot in slots} else None,
            "timezone_label": now.astimezone(tenant_zone(tenant)).tzname() if tenant else None,
            "error": error,
            "token": token,
        },
        status_code=status_code,
    )


@router.get("/book/{token}", response_class=HTMLResponse)
def booking_page(request: Request, token: str, slot: str | None = None) -> Response:
    ctx: AppContext = request.app.state.ctx
    now = ctx.now()
    with ctx.db.transaction() as conn:
        lead = get_lead_by_token(conn, token)
        if lead is not None:
            record_link_click(
                conn, lead, now, user_agent=request.headers.get("user-agent", ""), preselected_slot=slot
            )
    return _booking_page(request, ctx, token, selected=slot, error=None)


@router.post("/book/{token}", response_class=HTMLResponse)
def book(request: Request, token: str, slot: str = Form(default="")) -> Response:
    ctx: AppContext = request.app.state.ctx
    now = ctx.now()
    if not slot:
        return _booking_page(request, ctx, token, selected=None, error="Please choose a time first.", status_code=400)
    try:
        with ctx.db.transaction() as conn:
            lead = get_lead_by_token(conn, token)
            tenant = get_tenant(conn, lead["tenant_id"]) if lead else None
            if lead is None or tenant is None or not tenant["is_active"]:
                return _not_found(request)
            book_appointment(
                conn, lead=lead, tenant=tenant, slot_key=slot, now=now,
                max_attempts=ctx.settings.outbox_max_attempts,
            )
    except BookingError as exc:
        return _booking_page(request, ctx, token, selected=None, error=str(exc), status_code=409)
    ctx.worker.wake()  # send the confirmation email now
    # Redirect so a refresh shows the booking instead of re-submitting the form.
    return RedirectResponse(f"/book/{token}", status_code=303)


@router.get("/unsubscribe/{token}", response_class=HTMLResponse)
def unsubscribe_page(request: Request, token: str) -> Response:
    """Asks for confirmation. A GET never unsubscribes, because mail scanners follow
    links in emails and would otherwise opt people out by accident."""
    ctx: AppContext = request.app.state.ctx
    with ctx.db.read() as conn:
        lead = get_lead_by_token(conn, token)
        tenant = get_tenant(conn, lead["tenant_id"]) if lead else None
    if lead is None or tenant is None:
        return _not_found(request)
    return _render(request, "public/unsubscribe.html", {"tenant": tenant, "token": token, "done": False})


@router.post("/unsubscribe/{token}", response_class=HTMLResponse)
def unsubscribe(request: Request, token: str) -> Response:
    """Confirms the opt-out. Also the target of the one-click ``List-Unsubscribe-Post``
    header, which mailbox providers call directly."""
    ctx: AppContext = request.app.state.ctx
    now = ctx.now()
    with ctx.db.transaction() as conn:
        lead = get_lead_by_token(conn, token)
        tenant = get_tenant(conn, lead["tenant_id"]) if lead else None
        if lead is None or tenant is None:
            return _not_found(request)
        if lead["email_normalized"]:
            conn.execute(
                """INSERT INTO email_suppressions (tenant_id, email_normalized, reason, created_at)
                   VALUES (?, ?, 'unsubscribe_link', ?)
                   ON CONFLICT(tenant_id, email_normalized) DO NOTHING""",
                (tenant["id"], lead["email_normalized"], iso(now)),
            )
        # Outreach only: a confirmation of a booking they made themselves still goes.
        outbox.cancel_pending(
            conn, lead["id"], now, reason="unsubscribed", templates=("intro", "repeat_request", "followup")
        )
        add_lead_event(conn, lead["id"], tenant["id"], EV_UNSUBSCRIBED, now, {})
    return _render(request, "public/unsubscribe.html", {"tenant": tenant, "token": token, "done": True})
