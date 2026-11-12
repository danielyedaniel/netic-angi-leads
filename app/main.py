"""Application factory.

Run with:  uv run uvicorn app.main:create_app --factory --port 8000
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import Settings
from app.context import AppContext, build_context
from app.routes import admin, dashboard, health, public, webhooks
from app.security import require_admin
from app.seed import seed_demo
from app.util import format_cents

log = logging.getLogger("app")
TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

MIN_SECRET_LENGTH = 16
PLACEHOLDER_PREFIXES = ("change-me", "changeme", "placeholder", "your-")  # what .env.example ships with

# URLs that carry a credential in the path: a tenant's webhook key, and the token in a
# consumer's booking / unsubscribe link (whoever holds it can book or opt out as them).
_SECRET_IN_PATH = re.compile(r"(/webhooks/angi/leads/+|/book/+|/unsubscribe/+)[^/?\s]+", re.IGNORECASE)
_QUERY_STRING = re.compile(r"\?.*", re.DOTALL)  # may hold ?api_key=..., so all of it goes


def redact_url(path: str) -> str:
    """The request path as it may be written to a log: secrets replaced, the rest kept
    so the line still says which endpoint was called."""
    return _QUERY_STRING.sub("?[redacted]", _SECRET_IN_PATH.sub(r"\1[redacted]", path))


class RedactSecretsInAccessLog(logging.Filter):
    """uvicorn's access log prints every request URL in full. Its record arguments are
    (client address, method, path with query string, HTTP version, status)."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            record.args = (*args[:2], redact_url(args[2]), *args[3:])
        return True


def _weak_secret(secret: str) -> bool:
    """A secret nobody really chose: the placeholder copied from .env.example, or
    something too short to have come from a random generator."""
    text = secret.strip().lower()
    return len(text) < MIN_SECRET_LENGTH or text.startswith(PLACEHOLDER_PREFIXES)


def check_configuration(settings: Settings) -> None:
    """Fail fast on a configuration that would be unsafe in production, and say so
    loudly when running with the development conveniences switched on."""
    if settings.email_provider.lower() == "console":
        log.warning("EMAIL_PROVIDER=console: emails are written to data/outbox/ instead of being sent")
    if settings.is_production:
        problems = []
        if not settings.webhook_api_keys:
            problems.append("ANGI_WEBHOOK_API_KEYS is empty (the webhook would accept anyone)")
        elif any(_weak_secret(key) for key in settings.webhook_api_keys):
            problems.append(
                f"ANGI_WEBHOOK_API_KEYS holds a placeholder or a key shorter than {MIN_SECRET_LENGTH} characters"
            )
        if not settings.admin_token:
            problems.append("ADMIN_TOKEN is empty (the dashboard would be public)")
        elif _weak_secret(settings.admin_token):
            problems.append(f"ADMIN_TOKEN is a placeholder or shorter than {MIN_SECRET_LENGTH} characters")
        if settings.fallback_tenant_slug:
            problems.append("FALLBACK_TENANT_SLUG is set (unmapped leads would be emailed as the wrong business)")
        if problems:
            raise RuntimeError("Refusing to start in production: " + "; ".join(problems))
        return
    if not settings.webhook_api_keys:
        log.warning("ANGI_WEBHOOK_API_KEYS is not set: the webhook accepts unauthenticated requests (dev only)")
    if not settings.admin_token:
        log.warning("ADMIN_TOKEN is not set: the dashboard and admin API are open (dev only)")


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    # httpx logs every request URL at INFO, and the Slack webhook URL is a secret.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    access_log = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, RedactSecretsInAccessLog) for f in access_log.filters):
        access_log.addFilter(RedactSecretsInAccessLog())


def create_app(settings: Settings | None = None, *, context: AppContext | None = None) -> FastAPI:
    configure_logging()
    ctx = context or build_context(settings)
    check_configuration(ctx.settings)

    # The demo tenants own the account ids used in Angi's public documentation. In
    # production that would route anyone's copy of the documentation example to a
    # made-up business and email the address in it, so they are never created there.
    if ctx.settings.seed_demo_data and ctx.settings.is_production:
        log.warning("SEED_DEMO_DATA is ignored in production: demo tenants are only created in development")
    elif ctx.settings.seed_demo_data:
        with ctx.db.transaction() as conn:
            created = seed_demo(conn, ctx.now())
        if created:
            log.info("seeded %d demo tenants", created)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if ctx.settings.run_background_worker:
            ctx.worker.start()
        try:
            yield
        finally:
            ctx.worker.stop()

    app = FastAPI(
        title="Angi lead integration",
        version="0.1.0",
        description="Receives leads from Angi, maps them to a tenant, stores them and starts booking by email.",
        lifespan=lifespan,
        # The generated API reference lists every operator route; it is served below,
        # behind the admin token, instead of publicly.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.ctx = ctx
    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
    templates.env.filters["money"] = format_cents
    dashboard.register_filters(templates.env)
    app.state.templates = templates

    app.include_router(webhooks.router)
    app.include_router(public.router)
    app.include_router(health.router)
    app.include_router(admin.router)
    app.include_router(dashboard.router)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        """422 with what is wrong and where, but without echoing the value that was sent.
        FastAPI's default answer includes it, which fails outright (HTTP 500) when the
        value is NaN or Infinity, because those cannot be written as JSON."""
        errors = [{"type": error["type"], "loc": list(error["loc"]), "msg": error["msg"]} for error in exc.errors()]
        return JSONResponse({"detail": errors}, status_code=422)

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse("/dashboard")

    @app.get("/openapi.json", include_in_schema=False, dependencies=[Depends(require_admin)])
    def openapi_schema() -> JSONResponse:
        return JSONResponse(app.openapi())

    @app.get("/docs", include_in_schema=False, dependencies=[Depends(require_admin)])
    def api_reference() -> HTMLResponse:
        return get_swagger_ui_html(openapi_url="/openapi.json", title=app.title)

    return app
