"""Synthetic end-to-end probe: does a lead posted to the PUBLIC webhook URL still get
stored, mapped to a tenant and emailed?

What it does
    1. POSTs an Angi Ads lead for the canary account (ALAccountId 999999, which is
       mapped to the test tenant ``tnt_canary``) to <url>/webhooks/angi/leads, with a
       unique CorrelationId and the address given by --email.
    2. Requires HTTP 200 with ``status: success`` and ``result: processed`` - the only
       answer Angi itself would count as delivered.
    3. Polls GET <url>/admin/api/leads/{leadId} until ``first_contacted_at`` is set
       (the intro email was handed to the email provider) or --timeout seconds pass.
       That call needs the admin token, like every operator endpoint.

It prints exactly one JSON line and exits 0 on success, 1 on failure:

    {"ok": true, "webhook_ms": 41.3, "email_ms": 1180.0, "lead_id": "lead_...", "error": null}

    webhook_ms  how long the webhook took to answer, as Angi would experience it
    email_ms    the service's own speed-to-lead for this lead (received -> email sent)

Why it matters
    When no leads arrive there are two very different explanations: Angi stopped
    sending, or we are broken (tunnel down, DNS, expired API key, crashed worker,
    email provider rejecting us). The service's own checks cannot tell them apart,
    because a request that never reaches us leaves no trace. The canary sends a lead
    from the outside every few minutes: while it passes, our side provably works end
    to end, so silence means "Angi stopped sending"; when it fails, we are the problem,
    and we know within minutes instead of when a tenant complains.

    Canary leads belong to a tenant flagged ``is_test``. They stay out of the
    analytics, are never checked for duplicates (every run comes from the same
    synthetic consumer) and never open a credit claim. With a real email provider
    configured, --email receives a real message on every run: use a mailbox you own (a
    filter on the subject keeps it tidy). An @example.com address will not do there: the
    service never hands one to a real mail server, so the probe would report a failure.

Run it every 5 minutes from cron (the settings may also live in ./.env), with your own
mailbox after --email:

    */5 * * * * cd /path/to/netic-angi-leads && PUBLIC_BASE_URL=https://your-name.ngrok-free.dev ANGI_WEBHOOK_API_KEYS=... ADMIN_TOKEN=... uv run python scripts/canary.py --email <your mailbox> --slack-webhook https://hooks.slack.com/services/... >> /var/log/angi-canary.log 2>&1

and tell the service how often that is (CANARY_INTERVAL_MINUTES=5 in its .env). It then
raises an alert of its own when no canary has arrived for three intervals, which is
what a broken tunnel, an expired key or a dead cron job looks like from the inside.

By hand:

    uv run python scripts/canary.py --email <your mailbox>
    uv run python scripts/canary.py --email <your mailbox> --url http://localhost:8000 --timeout 10
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

CANARY_ACCOUNT_ID = "999999"  # mapped to tnt_canary (is_test) by app/seed.py
DEFAULT_URL = "http://localhost:8000"
WEBHOOK_PATH = "/webhooks/angi/leads"


def setting(name: str, default: str = "", dotenv_path: str | Path = ".env") -> str:
    """A setting from the environment, else from ./.env, else the default."""
    value = os.environ.get(name)
    if value:
        return value
    try:
        lines = Path(dotenv_path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return default
    for line in lines:
        key, separator, found = line.strip().partition("=")
        if separator and key.strip() == name and found.strip():
            return found.strip().strip("'\"")
    return default


def canary_lead(email: str, correlation_id: str) -> dict[str, Any]:
    """An Angi Ads lead that is obviously synthetic to anyone who reads it."""
    return {
        "FirstName": "Canary",
        "LastName": "Probe",
        "PhoneNumber": "2065550100",
        "PostalAddress": {
            "AddressFirstLine": "1 Synthetic Way",
            "AddressSecondLine": "",
            "City": "Seattle",
            "State": "WA",
            "PostalCode": "98101",
        },
        "Email": email,
        "Source": "Synthetic canary",
        "Description": "Automated end-to-end probe of the Angi lead integration. No action needed.",
        "Category": "Seattle – Synthetic Monitoring Probe",
        "Urgency": "This Week",
        "CorrelationId": correlation_id,
        "ALAccountId": CANARY_ACCOUNT_ID,
    }


def milliseconds_between(start: str | None, end: str | None) -> float | None:
    """Milliseconds between two of the service's UTC timestamps ("2026-11-11T22:31:05.123Z")."""
    if not start or not end:
        return None
    try:
        first = datetime.fromisoformat(start.replace("Z", "+00:00"))
        second = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError:
        return None
    return round((second - first).total_seconds() * 1000, 1)


def _json(response: Any) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def run_canary(
    client: Any,
    *,
    email: str,
    api_key: str = "",
    admin_token: str = "",
    timeout: float = 30.0,
    poll_interval: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Run the probe once. ``client`` is an ``httpx.Client`` whose base_url is the
    service (FastAPI's TestClient works too). A failed check or an unreachable service
    comes back as ``{"ok": False, "error": ...}`` rather than as an exception."""
    result: dict[str, Any] = {"ok": False, "webhook_ms": None, "email_ms": None, "lead_id": None, "error": None}

    # 1. Deliver a lead the way Angi does.
    headers = {"X-API-KEY": api_key} if api_key else {}
    started = monotonic()
    try:
        response = client.post(WEBHOOK_PATH, json=canary_lead(email, str(uuid.uuid4())), headers=headers)
    except httpx.HTTPError as exc:
        result["error"] = f"webhook request failed: {type(exc).__name__}: {exc}"
        return result
    result["webhook_ms"] = round((monotonic() - started) * 1000, 1)

    # 2. Only HTTP 200 + status "success" counts as delivered; "processed" means a lead was created.
    body = _json(response)
    if response.status_code != 200:
        result["error"] = f"webhook answered HTTP {response.status_code}: {response.text[:200]}"
        return result
    if not isinstance(body, dict) or body.get("status") != "success":
        result["error"] = f"webhook answered HTTP 200 without status=success: {response.text[:200]}"
        return result
    result["lead_id"] = body.get("leadId")
    if body.get("result") != "processed" or not result["lead_id"]:
        reason = f" ({body['reason']})" if body.get("reason") else ""
        result["error"] = f"webhook result was {body.get('result')!r}{reason}, expected 'processed'"
        return result

    # 3. Wait for the intro email to leave.
    admin_headers = {"Authorization": f"Bearer {admin_token}"} if admin_token else {}
    deadline = monotonic() + timeout
    while True:
        try:
            response = client.get(f"/admin/api/leads/{result['lead_id']}", headers=admin_headers)
        except httpx.HTTPError as exc:
            result["error"] = f"admin API request failed: {type(exc).__name__}: {exc}"
            return result
        detail = _json(response)
        if response.status_code != 200 or not isinstance(detail, dict):
            result["error"] = f"admin API answered HTTP {response.status_code}: {response.text[:200]}"
            return result
        lead = detail.get("lead") or {}
        if lead.get("first_contacted_at"):
            result["email_ms"] = milliseconds_between(lead.get("received_at"), lead["first_contacted_at"])
            result["ok"] = True
            return result
        if monotonic() >= deadline:
            # Say why, if the service knows: the last send error of the lead's messages.
            errors = [m["last_error"] for m in detail.get("messages", []) if m.get("last_error")]
            why = f"; last send error: {errors[-1]}" if errors else ""
            result["error"] = f"the intro email was not sent within {timeout:g}s{why}"
            return result
        sleep(poll_interval)


def notify_slack(webhook_url: str, text: str) -> None:
    """Best effort: a Slack outage must not change what the canary reports."""
    try:
        httpx.post(webhook_url, json={"text": text}, timeout=10.0).raise_for_status()
    except httpx.HTTPError as exc:
        # Only the kind of error: httpx puts the request URL in its messages, and the webhook URL is a secret.
        print(f"could not post to Slack ({type(exc).__name__})", file=sys.stderr)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Synthetic end-to-end probe of the Angi lead integration.")
    parser.add_argument("--email", required=True, help="address the canary lead's intro email is sent to")
    parser.add_argument("--url", help=f"public base URL of the service (default: $PUBLIC_BASE_URL or {DEFAULT_URL})")
    parser.add_argument("--api-key", help="webhook API key (default: first entry of $ANGI_WEBHOOK_API_KEYS)")
    parser.add_argument("--admin-token", help="admin token (default: $ADMIN_TOKEN)")
    parser.add_argument("--timeout", type=float, default=30.0, help="seconds to wait for the email (default: 30)")
    parser.add_argument("--slack-webhook", help="Slack incoming-webhook URL to post a failure to")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, client: Any = None) -> int:
    """``client`` lets a test pass FastAPI's TestClient; normally a real httpx.Client is made."""
    args = parse_args(argv)
    url = (args.url or setting("PUBLIC_BASE_URL", DEFAULT_URL)).rstrip("/")
    api_keys = args.api_key or setting("ANGI_WEBHOOK_API_KEYS")
    api_key = next((part.strip() for part in api_keys.split(",") if part.strip()), "")
    admin_token = args.admin_token or setting("ADMIN_TOKEN")

    owns_client = client is None
    if owns_client:
        client = httpx.Client(base_url=url, timeout=15.0)
    try:
        result = run_canary(client, email=args.email, api_key=api_key, admin_token=admin_token, timeout=args.timeout)
    except Exception as exc:  # noqa: BLE001 - a probe must always report, never die with a traceback
        result = {"ok": False, "webhook_ms": None, "email_ms": None, "lead_id": None,
                  "error": f"canary crashed: {type(exc).__name__}: {exc}"}
    finally:
        if owns_client:
            client.close()

    print(json.dumps(result))
    if not result["ok"] and args.slack_webhook:
        notify_slack(args.slack_webhook, f"Angi canary FAILED against {url}: {result['error']}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
