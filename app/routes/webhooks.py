"""The route Angi posts leads to.

    POST /webhooks/angi/leads                   shared URL, authenticated with an API key
    POST /webhooks/angi/leads/{webhook_key}     tenant-specific secret URL

The handler does as little as possible: authenticate, read the body, hand it to the
ingest pipeline (which stores it before doing anything else) and report the outcome.
It never sends the email itself - a slow mail server must not make Angi time out.

Responses follow Angi's delivery contract: only HTTP 200 with a success body counts as
delivered; anything else makes Angi retry three times, fifteen minutes apart.

    200 {"status": "success", "result": ...}   the request is safely stored. `result` says
                                               what happened: processed, duplicate_delivery,
                                               updated, quarantined or accepted. A quarantined
                                               answer carries a `reason` and a `message` that
                                               says what has to happen before it is processed
    400 {"status": "error", ...}               body is not JSON at all (still stored, for forensics)
    401 {"status": "error", ...}               missing or wrong credentials
    413 {"status": "error", ...}               body too large
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from app.alerts import raise_alert
from app.context import AppContext
from app.repo import get_tenant_by_webhook_key
from app.security import client_ip, webhook_auth_ok

router = APIRouter(tags=["webhooks"])


class BodyTooLarge(Exception):
    pass


async def _read_body(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise BodyTooLarge
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise BodyTooLarge
        chunks.append(chunk)
    return b"".join(chunks)


def _tenant_id_for_key(ctx: AppContext, webhook_key: str) -> str | None:
    with ctx.db.read() as conn:
        tenant = get_tenant_by_webhook_key(conn, webhook_key)
    return tenant["id"] if tenant else None


def _note_auth_failure(ctx: AppContext, request: Request, why: str) -> None:
    """Rejected requests are not stored (they are unauthenticated), but a burst of them
    is worth an alert: it is what we would see if Angi changed how it sends the key."""
    with ctx.db.transaction() as conn:
        raise_alert(
            conn,
            kind="auth_failures",
            key="auth_failures",
            severity="warning",
            title="Requests to the Angi webhook are being rejected as unauthenticated",
            detail={
                "reason": why,
                "remote_addr": client_ip(request),
                "header_names": sorted(request.headers.keys())[:40],
                "user_agent": request.headers.get("user-agent", "")[:200],
            },
            now=ctx.now(),
        )


def _note_oversized(ctx: AppContext, request: Request) -> None:
    with ctx.db.transaction() as conn:
        raise_alert(
            conn,
            kind="oversized_payload",
            key="oversized_payload",
            severity="warning",
            title=f"An Angi webhook request was larger than {ctx.settings.max_body_bytes} bytes and was refused",
            detail={
                "remote_addr": client_ip(request),
                "content_length": request.headers.get("content-length"),
                "limit_bytes": ctx.settings.max_body_bytes,
            },
            now=ctx.now(),
        )


async def _handle(request: Request, webhook_key: str | None) -> JSONResponse:
    ctx: AppContext = request.app.state.ctx

    url_tenant_id: str | None = None
    if webhook_key is not None:
        url_tenant_id = await run_in_threadpool(_tenant_id_for_key, ctx, webhook_key)
        if url_tenant_id is None:
            await run_in_threadpool(_note_auth_failure, ctx, request, "unknown webhook key in URL")
            return JSONResponse(
                {"status": "error", "result": "unauthorized", "error": "Unknown webhook URL."}, status_code=401
            )
    elif not webhook_auth_ok(ctx.settings, request):
        await run_in_threadpool(_note_auth_failure, ctx, request, "missing or invalid API key")
        return JSONResponse(
            {
                "status": "error",
                "result": "unauthorized",
                "error": "Missing or invalid API key. Send it in the X-API-KEY header.",
            },
            status_code=401,
        )

    try:
        body = await _read_body(request, ctx.settings.max_body_bytes)
    except BodyTooLarge:
        # Not stored (that would defeat the limit), but not silent either.
        await run_in_threadpool(_note_oversized, ctx, request)
        return JSONResponse(
            {
                "status": "error",
                "result": "rejected",
                "error": f"Body larger than {ctx.settings.max_body_bytes} bytes.",
            },
            status_code=413,
        )

    outcome = await run_in_threadpool(
        ctx.ingest.receive,
        raw_body=body,
        headers=dict(request.headers),
        remote_addr=client_ip(request),
        url_tenant_id=url_tenant_id,
    )
    return JSONResponse(outcome.body, status_code=outcome.http_status)


@router.post("/webhooks/angi/leads")
@router.post("/webhooks/angi/leads/", include_in_schema=False)  # a trailing slash must not cost a lead
async def receive_angi_lead(request: Request) -> JSONResponse:
    return await _handle(request, None)


@router.post("/webhooks/angi/leads/{webhook_key}")
@router.post("/webhooks/angi/leads/{webhook_key}/", include_in_schema=False)  # same as the shared URL
async def receive_angi_lead_for_tenant(request: Request, webhook_key: str) -> JSONResponse:
    return await _handle(request, webhook_key)


@router.get("/webhooks/angi/leads")
def describe_endpoint() -> dict[str, str]:
    """A friendly answer for anyone who opens the webhook URL in a browser."""
    return {
        "status": "ok",
        "message": "This endpoint accepts Angi leads: POST a JSON lead with your API key in the X-API-KEY header.",
    }
