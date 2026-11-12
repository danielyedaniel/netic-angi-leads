"""Liveness, readiness and metrics endpoints.

``/healthz`` and ``/readyz`` are public, because a load balancer or uptime monitor has
no credentials; they say "up" or "not ready" and nothing about tenants or volumes.
``/metrics`` shows how much business each tenant does and which checks are failing, so
it needs the admin token like the rest of the operator API (Prometheus sends it as a
bearer token: ``authorization: {credentials: <token>}`` in the scrape config).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from app import monitoring
from app.context import AppContext
from app.repo import kv_get
from app.security import require_admin

router = APIRouter(tags=["health"])


@router.get("/healthz")
def healthz() -> dict[str, str]:
    """Liveness: the process is up and serving HTTP."""
    return {"status": "ok"}


@router.get("/readyz")
def readyz(request: Request) -> JSONResponse:
    """Readiness: we can reach the database, and the worker that sends email is
    alive. Suitable for a load balancer or an uptime monitor."""
    ctx: AppContext = request.app.state.ctx
    problems: list[str] = []
    try:
        with ctx.db.read() as conn:
            conn.execute("SELECT 1").fetchone()
    except Exception as exc:  # noqa: BLE001
        problems.append(f"database: {exc}")
    if ctx.settings.run_background_worker and not ctx.worker.is_running:
        problems.append("background worker is not running")
    if problems:
        return JSONResponse({"status": "unavailable", "problems": problems}, status_code=503)
    return JSONResponse({"status": "ok"})


@router.get("/metrics", response_class=PlainTextResponse, dependencies=[Depends(require_admin)])
def metrics(request: Request) -> str:
    """Prometheus text format, so the pipeline can be scraped into Grafana / Datadog
    and alerted on with the same tooling as everything else."""
    ctx: AppContext = request.app.state.ctx
    lines: list[str] = []

    def gauge(name: str, help_text: str, samples: list[tuple[dict[str, str], float]]) -> None:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} gauge")
        for labels, value in samples:
            label_text = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
            lines.append(f"{name}{{{label_text}}} {value}" if label_text else f"{name} {value}")

    with ctx.db.read() as conn:
        gauge(
            "angi_webhook_events",
            "Webhook events by processing status",
            [({"status": r["status"]}, r["n"]) for r in conn.execute(
                "SELECT status, COUNT(*) AS n FROM webhook_events GROUP BY status")],
        )
        gauge(
            "angi_leads",
            "Leads by current status",
            [({"status": r["status"]}, r["n"]) for r in conn.execute(
                "SELECT status, COUNT(*) AS n FROM leads GROUP BY status")],
        )
        gauge(
            "angi_outbox_messages",
            "Outbound messages by delivery status",
            [({"status": r["status"]}, r["n"]) for r in conn.execute(
                "SELECT status, COUNT(*) AS n FROM outbox_messages GROUP BY status")],
        )
        gauge(
            "angi_alerts_open",
            "Open alerts by severity",
            [({"severity": r["severity"]}, r["n"]) for r in conn.execute(
                "SELECT severity, COUNT(*) AS n FROM alerts WHERE status = 'open' GROUP BY severity")],
        )
        checks = monitoring.evaluate(conn, ctx.settings, ctx.now())
        gauge(
            "angi_check_ok",
            "1 when a health check passes, 0 when it does not",
            [({"check": c.name}, 1 if c.status == "ok" else 0) for c in checks],
        )
        last_run = kv_get(conn, "monitor:last_run")
    lines.append(f"# monitor_last_run {last_run or 'never'}")
    return "\n".join(lines) + "\n"
