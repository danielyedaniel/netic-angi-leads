"""End-to-end check against a real server process.

Unlike the test suite (which drives the app in-process with a fake clock), this starts
the actual service with uvicorn on a free port, with its real background worker, and
talks to it over HTTP exactly as Angi, a consumer's browser and an operator would:

    lead in  ->  stored + mapped  ->  email written  ->  booking link opened  ->  booked
    plus: redelivery, quarantine and release, a drifted payload, a duplicate and its
    credit claim, the dashboard, health and metrics.

Email goes to the "console" provider (one .eml file per message in a temp folder), so
nothing leaves the machine. Exit code 0 means every step passed.

    uv run python scripts/e2e_check.py            # or: make e2e
"""

from __future__ import annotations

import email
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from email import policy
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
API_KEY = "e2e-webhook-key-0123456789"
ADMIN_TOKEN = "e2e-admin-token-0123456789"
SPEC_EXAMPLE = json.loads((ROOT / "samples" / "angi_ads_spec_example.json").read_text())

results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> bool:
    results.append((bool(ok), name, detail))
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}{'  -  ' + detail if detail and not ok else ''}")
    return bool(ok)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def lead(**overrides) -> dict:
    """Angi's spec example as a brand-new lead from a brand-new consumer."""
    unique = uuid.uuid4().hex[:8]
    payload = json.loads(json.dumps(SPEC_EXAMPLE))
    payload["CorrelationId"] = str(uuid.uuid4())
    payload["Email"] = f"e2e-{unique}@example.com"
    payload["PhoneNumber"] = f"317555{int(unique[:4], 16) % 10000:04d}"
    payload.update(overrides)
    return payload


def wait_for(predicate, timeout: float = 15.0, interval: float = 0.1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


def emails_to(outbox: Path, address: str) -> list:
    found = []
    for path in sorted(outbox.glob("*.eml")):
        message = email.message_from_bytes(path.read_bytes(), policy=policy.default)
        if address.lower() in str(message["To"]).lower():
            found.append(message)
    return found


def body_text(message) -> str:
    part = message.get_body(preferencelist=("plain",))
    return part.get_content() if part else ""


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="angi-e2e-"))
    outbox = workdir / "data" / "outbox"
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT),
        "APP_ENV": "development",
        "DATABASE_PATH": str(workdir / "leads.db"),
        "PUBLIC_BASE_URL": base,
        "ANGI_WEBHOOK_API_KEYS": API_KEY,
        "ADMIN_TOKEN": ADMIN_TOKEN,
        "EMAIL_PROVIDER": "console",
        "EMAIL_FROM_ADDRESS": "leads@example.com",
        "EMAIL_REDIRECT_ALL_TO": "",
        "EMAIL_REPLY_TO": "",
        "FALLBACK_TENANT_SLUG": "",
        "FOLLOWUP_DELAYS_MINUTES": "60,1440",
        "SEED_DEMO_DATA": "true",
        "RUN_BACKGROUND_WORKER": "true",
        "ALERT_SLACK_WEBHOOK_URL": "",
        "ALERT_EMAIL_TO": "",
    }
    log_path = workdir / "server.log"
    with open(log_path, "w") as log_file:
        server = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:create_app", "--factory", "--host", "127.0.0.1",
             "--port", str(port)],
            cwd=workdir, env=env, stdout=log_file, stderr=subprocess.STDOUT,
        )
    angi = {"X-API-KEY": API_KEY}
    admin = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
    try:
        with httpx.Client(base_url=base, timeout=20.0) as http:
            print(f"server: {base}   workdir: {workdir}")
            up = wait_for(lambda: _is_up(http), timeout=30)
            if not check(bool(up), "server starts and answers /healthz"):
                print(log_path.read_text()[-3000:])
                return 1
            check(http.get("/readyz").status_code == 200, "/readyz: database reachable and worker running")

            print("\n1. A lead arrives")
            first = lead(FirstName="Pat", LastName="Lee")
            response = http.post("/webhooks/angi/leads", json=first)
            check(response.status_code == 401 and response.json()["status"] == "error", "no API key -> 401")
            started = time.monotonic()
            response = http.post("/webhooks/angi/leads", json=first, headers=angi)
            answer = response.json()
            check(response.status_code == 200 and answer.get("status") == "success", "HTTP 200 + status success")
            check(answer.get("result") == "processed", "result is 'processed'", str(answer))
            check(answer.get("tenant", {}).get("id") == "tnt_sparkle" and answer["tenant"]["routedBy"] == "account_id",
                  "mapped to Sparkle Home Cleaning by ALAccountId", str(answer.get("tenant")))
            check(answer.get("email", {}).get("status") == "queued", "intro email queued in the same transaction")
            lead_id = answer.get("leadId")

            intro = wait_for(lambda: emails_to(outbox, first["Email"]))
            waited = time.monotonic() - started
            check(bool(intro), "intro email written by the worker", f"none after {waited:.1f}s")
            check(waited < 5, f"email out within 5 s of the request (took {waited:.2f}s)")
            if intro:
                text = body_text(intro[0])
                check("Sparkle Home Cleaning" in str(intro[0]["From"]), "From name is the tenant", str(intro[0]["From"]))
                check("House Cleaning" in str(intro[0]["Subject"]), "subject names the service", str(intro[0]["Subject"]))
                check("Hi Pat" in text and "Indianapolis" in text, "body is about this consumer's request")
                check(intro[0]["List-Unsubscribe"] is not None, "List-Unsubscribe header present")
                link = re.search(rf"{re.escape(base)}/book/[A-Za-z0-9_-]+", text)
                check(bool(link), "booking link points at PUBLIC_BASE_URL")
            else:
                link = None

            detail = http.get(f"/admin/api/leads/{lead_id}", headers=admin).json()
            speed = detail["lead"].get("speed_to_lead_seconds")
            check(speed is not None and speed < 5, f"speed to lead recorded ({speed}s)")
            check(detail["lead"]["status"] == "contacted", "lead status is 'contacted'", detail["lead"]["status"])

            print("\n2. Angi delivers the same lead again")
            again = http.post("/webhooks/angi/leads", json=first, headers=angi).json()
            check(again.get("result") == "duplicate_delivery" and again.get("leadId") == lead_id,
                  "redelivery answered 'duplicate_delivery' for the same lead", str(again))
            time.sleep(1.5)
            check(len(emails_to(outbox, first["Email"])) == 1, "no second email")

            print("\n3. The consumer books")
            if link:
                page = http.get(link.group(0))
                check(page.status_code == 200 and "Pick a time" in page.text, "booking page opens")
                slot = re.search(r'name="slot" value="([^"]+)"', page.text)
                booked = http.post(link.group(0), data={"slot": slot.group(1)}) if slot else None
                check(booked is not None and booked.status_code == 303, "choosing a slot redirects (303)")
                page = http.get(link.group(0))
                check("You're booked" in page.text, "page now shows the booking")
                confirmation = wait_for(lambda: [m for m in emails_to(outbox, first["Email"])
                                                 if str(m["Subject"]).startswith("You're booked")])
                check(bool(confirmation), "confirmation email written")
                detail = http.get(f"/admin/api/leads/{lead_id}", headers=admin).json()
                check(detail["lead"]["status"] == "booked" and detail["appointment"] is not None,
                      "lead is booked with an appointment")
                pending = [m for m in detail["messages"] if m["status"] == "pending"]
                check(not pending, "follow-ups cancelled once booked", str([m["template"] for m in pending]))
                types = [e["type"] for e in detail["events"]]
                check(all(t in types for t in ("lead.received", "email.sent", "booking.link_clicked",
                                                "booking.appointment_booked")), "timeline has every step", str(types))

            print("\n4. A lead for an Angi account we do not know")
            stranger = lead(ALAccountId="777001", FirstName="Quinn")
            answer = http.post("/webhooks/angi/leads", json=stranger, headers=angi).json()
            check(answer.get("status") == "success" and answer.get("result") == "quarantined",
                  "stored and quarantined, Angi still told success", str(answer))
            time.sleep(1.0)
            check(not emails_to(outbox, stranger["Email"]), "no email sent under a guessed business")
            alerts = http.get("/admin/api/alerts?status=open", headers=admin).json()["alerts"]
            check(any(a["kind"] == "unmapped_account" and "777001" in a["title"] for a in alerts),
                  "critical alert names the unmapped account")
            mapped = http.post("/admin/api/tenants/tnt_blueline/lead-sources", headers=admin,
                               json={"external_account_id": "777001", "external_account_name": "BlueLine East",
                                     "default_fee": 31})
            check(mapped.status_code == 201, "mapping added through the admin API", mapped.text[:200])
            released = wait_for(lambda: emails_to(outbox, stranger["Email"]))
            check(bool(released) and "BlueLine Plumbing" in str(released[0]["From"]),
                  "quarantined lead released and emailed by the right tenant")
            alerts = http.get("/admin/api/alerts?status=open", headers=admin).json()["alerts"]
            check(not any(a["kind"] == "unmapped_account" and "777001" in a["title"] for a in alerts),
                  "the unmapped-account alert resolved itself")

            print("\n5. Angi changes the format")
            drifted = json.loads((ROOT / "samples" / "drift_renamed_and_recased.json").read_text())
            drifted_email = f"e2e-drift-{uuid.uuid4().hex[:6]}@example.com"
            for key in list(drifted):
                if key.lower() in ("email", "emailaddress"):
                    drifted[key] = drifted_email
                if key.lower() == "correlationid":
                    drifted[key] = str(uuid.uuid4())
                if key.lower() == "phonenumber":
                    drifted[key] = 3175550000 + int(uuid.uuid4().hex[:3], 16) % 10000
            answer = http.post("/webhooks/angi/leads", json=drifted, headers=angi).json()
            check(answer.get("result") == "processed" and answer.get("parseMode") == "tolerant",
                  "renamed / re-cased payload still processed (tolerant)", str(answer)[:300])
            check(bool(wait_for(lambda: emails_to(outbox, drifted_email))), "and still emailed")
            alerts = http.get("/admin/api/alerts?status=open", headers=admin).json()["alerts"]
            check(any(a["kind"] == "schema_drift" for a in alerts), "schema drift alert raised")
            garbage = http.post("/webhooks/angi/leads", content=b"this is not json", headers=angi)
            check(garbage.status_code == 400 and garbage.json()["status"] == "error", "non-JSON body -> 400")
            events = http.get("/admin/api/events?status=rejected", headers=admin).json()["events"]
            check(len(events) >= 1, "the rejected body is still stored")

            print("\n6. The same consumer comes back as a new lead")
            repeat = dict(first, CorrelationId=str(uuid.uuid4()), Description="Following up on my request")
            answer = http.post("/webhooks/angi/leads", json=repeat, headers=angi).json()
            duplicate = answer.get("duplicate") or {}
            check(answer.get("result") == "processed" and duplicate.get("ofLeadId") == lead_id,
                  "flagged as a duplicate of the first lead", str(answer)[:300])
            check(set(duplicate.get("matchedOn", [])) >= {"phone", "email"}, "matched on phone and email")
            claims = http.get("/admin/api/claims", headers=admin).json()
            check(any(c["original_lead_id"] == lead_id for c in claims["claims"]), "credit claim opened")
            csv_text = http.get("/admin/api/claims/export.csv", headers=admin).text
            check(first["CorrelationId"] in csv_text and repeat["CorrelationId"] in csv_text,
                  "CSV export carries both Angi lead ids")
            acknowledged = wait_for(lambda: [m for m in emails_to(outbox, first["Email"])
                                             if "new request" in str(m["Subject"])])
            check(bool(acknowledged), "consumer gets the acknowledgement, not a second intro")

            print("\n7. Operator surfaces")
            check(http.get("/dashboard").status_code == 401, "dashboard refuses without the admin token")
            for page in ("", "/leads", f"/leads/{lead_id}", "/credits", "/monitoring", "/tenants"):
                response = http.get(f"/dashboard{page}", auth=("operator", ADMIN_TOKEN))
                check(response.status_code == 200 and "</html>" in response.text, f"dashboard{page or ' (overview)'} renders")
            summary = http.get("/admin/api/analytics/summary", headers=admin).json()
            check(summary["funnel"]["received"] >= 4 and summary["funnel"]["booked"] >= 1, "analytics funnel has the leads")
            health = http.get("/admin/api/health", headers=admin).json()
            check(health.get("worker_running") is True, "health reports the worker running")
            metrics = http.get("/metrics", headers=admin)
            check(metrics.status_code == 200 and "angi_leads" in metrics.text, "/metrics serves Prometheus text")

            print("\n8. Nothing went wrong behind the scenes")
            failed = http.get("/admin/api/events?status=failed", headers=admin).json()["events"]
            check(not failed, "no webhook event failed to process", str(failed)[:300])
            log_text = log_path.read_text()
            check("Traceback" not in log_text, "no traceback in the server log", _tail(log_text, "Traceback"))
            check(API_KEY not in log_text and ADMIN_TOKEN not in log_text, "no secret in the server log")
    finally:
        server.terminate()
        try:
            server.wait(timeout=15)
        except subprocess.TimeoutExpired:
            server.kill()
        stopped = server.poll() is not None
        failures = [name for ok, name, _ in results if not ok]
        if failures:
            print(f"\nserver log kept at {log_path}")
        else:
            shutil.rmtree(workdir, ignore_errors=True)
    check(stopped, "server stopped cleanly")
    failures = [name for ok, name, _ in results if not ok]
    print(f"\n{len(results) - len(failures)}/{len(results)} checks passed")
    for name in failures:
        print("  FAILED:", name)
    return 1 if failures else 0


def _is_up(http: httpx.Client) -> bool:
    try:
        return http.get("/healthz").status_code == 200
    except httpx.HTTPError:
        return False


def _tail(text: str, marker: str) -> str:
    index = text.find(marker)
    return text[index:index + 1500] if index >= 0 else ""


if __name__ == "__main__":
    sys.exit(main())
