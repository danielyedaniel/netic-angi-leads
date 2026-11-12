"""The demo tooling: sample payloads, scripts/send_lead.py, scripts/canary.py,
scripts/simulate_history.py, the Makefile and .env.example.

Everything runs in-process against the real application (FastAPI's TestClient, a
temporary SQLite file, the in-memory mail provider and a clock the test moves).
"""

from __future__ import annotations

import json
import random
import re
import shlex
import shutil
import smtplib
import sqlite3
import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.context import build_context
from app.mailer.providers import MemoryEmailProvider
from app.main import create_app
from scripts import canary, send_lead, simulate_history
from tests.conftest import (
    ADMIN,
    ADMIN_TOKEN,
    API_KEY,
    BASE_URL,
    START,
    FakeClock,
    ads_payload,
    db_one,
    db_rows,
    leads_payload,
    post_lead,
)

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"
SAMPLE_FILES = sorted(p.name for p in SAMPLES.iterdir() if p.suffix in (".json", ".txt"))
JSON_SAMPLES = [name for name in SAMPLE_FILES if name.endswith(".json")]
# Angi's own documentation example. Its phone number is Angi's (area code 555 does not
# exist); its email address is ours, on example.com, like every other sample's.
ANGI_DOC_SAMPLES = {"angi_ads_spec_example.json", "malformed_spec_example.txt"}
SPEC_LEAD_ID = "61a7de56-dba3-4e59-8e2a-3fa827f84f7f"
MALFORMED_LEAD_ID = "61a7de56-dba3-4e59-8e2a-3fa827f84f80"
FICTIONAL_PHONE = r"[2-9]\d{2}55501\d{2}"
MAKEFILE = ROOT / "Makefile"
MAKE_TARGETS = (
    "help", "install", "run", "dev", "test", "lint", "e2e", "tunnel", "send", "canary", "simulate", "reset-db",
)
needs_make = pytest.mark.skipif(shutil.which("make") is None, reason="make is not installed")


# --- helpers ---------------------------------------------------------------------------


def sample_bytes(name: str) -> bytes:
    return (SAMPLES / name).read_bytes()


def sample_json(name: str) -> dict[str, Any]:
    return json.loads((SAMPLES / name).read_text(encoding="utf-8"))


def post_sample(client, name: str):
    """POST a sample file byte for byte, the way Angi would."""
    return post_lead(client, sample_bytes(name))


def readme_results() -> dict[str, str]:
    """file name -> expected ``result``, read from the table in samples/README.md."""
    results: dict[str, str] = {}
    for line in (SAMPLES / "README.md").read_text(encoding="utf-8").splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) == 3 and re.fullmatch(r"`[\w.]+`", cells[0]):
            results[cells[0].strip("`")] = cells[2].strip("`")
    return results


def alerts(ctx) -> list[dict[str, Any]]:
    return db_rows(ctx, "SELECT kind, severity, status, title FROM alerts ORDER BY id")


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    """No developer .env and no exported settings: the scripts see only what the test gives them."""
    monkeypatch.chdir(tmp_path)
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    return tmp_path


class WorkerTime:
    """Stands in for time.sleep / time.monotonic in the canary: "sleeping" moves the
    fake clock and lets the worker run, which is what the background thread does in a
    real deployment while the canary waits."""

    def __init__(self, ctx, clock, *, worker_runs: bool = True) -> None:
        self.ctx, self.clock, self.worker_runs = ctx, clock, worker_runs
        self.elapsed = 0.0
        self.sleeps = 0

    def sleep(self, seconds: float) -> None:
        self.sleeps += 1
        self.elapsed += seconds
        self.clock.advance(seconds=seconds)
        if self.worker_runs:
            self.ctx.worker.deliver_due()

    def monotonic(self) -> float:
        return self.elapsed


class WorkerRunsAfterEachPost:
    """The TestClient plus the background worker: one second after each POST, whatever
    is due gets sent."""

    def __init__(self, client, ctx, clock) -> None:
        self.client, self.ctx, self.clock = client, ctx, clock

    def post(self, *args, **kwargs):
        response = self.client.post(*args, **kwargs)
        self.clock.advance(seconds=1)
        self.ctx.worker.deliver_due()
        return response

    def get(self, *args, **kwargs):
        return self.client.get(*args, **kwargs)


class UnreachableService:
    """An HTTP client whose every request fails to connect (tunnel down, DNS gone)."""

    def post(self, *args, **kwargs):
        raise httpx.ConnectError("connection refused")

    get = post


class FakeResponse:
    def __init__(self, status_code: int, text: str) -> None:
        self.status_code, self.text = status_code, text

    def json(self):
        return json.loads(self.text)


# =========================================================================================
# Sample payloads
# =========================================================================================


def test_every_sample_file_is_documented_in_the_readme_table():
    # A sample nobody can look up is a sample nobody dares to send in a demo.
    assert set(readme_results()) == set(SAMPLE_FILES)
    assert len(SAMPLE_FILES) == 13


@pytest.mark.parametrize("name", SAMPLE_FILES)
def test_sample_is_accepted_with_the_result_documented_in_the_readme(client, name):
    # Angi's contract: whatever we manage to store is answered 200 + status "success".
    response = post_sample(client, name)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["result"] == readme_results()[name]


@pytest.mark.parametrize("name", JSON_SAMPLES)
def test_json_samples_are_json_objects(name):
    # Only the "malformed" sample is allowed to be something a JSON parser rejects.
    assert isinstance(sample_json(name), dict)


@pytest.mark.parametrize("name", SAMPLE_FILES)
def test_no_sample_contains_an_address_that_could_be_a_real_mailbox(name):
    # No sample may be able to email a real person, Angi's own example included: every
    # address is on example.com, which the worker never hands to a real mail server.
    text = sample_bytes(name).decode("utf-8")
    addresses = re.findall(r"[\w.+-]+@[\w.-]+\.\w+", text)
    for address in addresses:
        assert address.endswith("@example.com"), address
    assert send_lead.real_looking_addresses(sample_bytes(name)) == []
    assert "gmail" not in text.lower()
    if name in ("angi_ads_spec_example.json", "malformed_spec_example.txt"):
        assert addresses == ["bob.builder@example.com"]


@pytest.mark.parametrize("name", [n for n in SAMPLE_FILES if n not in ANGI_DOC_SAMPLES])
def test_samples_carry_only_fictional_phone_numbers(name):
    # Demo payloads must not be able to ring a real person either: 555-01xx only.
    text = sample_bytes(name).decode("utf-8")

    def phones(node: Any):
        if isinstance(node, dict):
            for key, value in node.items():
                if key.lower() in ("phonenumber", "primaryphone", "secondaryphone", "phone") and value:
                    yield re.sub(r"\D", "", str(value))
                yield from phones(value)

    for digits in phones(json.loads(text)):
        assert re.fullmatch(r"\d{3}55501\d{2}", digits), digits


def test_malformed_sample_keeps_the_three_defects_of_angis_example_and_is_not_valid_json():
    # The file keeps what is wrong with the example Angi printed (curly quotes, an unquoted
    # email, a missing comma). Only the address (example.com) and the lead id are its own.
    left, right, apostrophe, dash = "“", "”", "’", "–"
    text = sample_bytes("malformed_spec_example.txt").decode("utf-8")

    with pytest.raises(ValueError):
        json.loads(text)
    assert f"{left}Email{right}:bob.builder@example.com," in text  # the unquoted email
    assert f"{left}CorrelationId{right}:{right}{MALFORMED_LEAD_ID}{right}" in text
    assert SPEC_LEAD_ID not in text and sample_json("angi_ads_spec_example.json")["CorrelationId"] == SPEC_LEAD_ID
    assert not (SAMPLES / "malformed_spec_verbatim.txt").exists()  # the old file emailed a gmail.com address
    assert f"{left}FirstName{right}:{right}Bob{right}" in text  # a closing quote where an opening one belongs
    assert f"please.{right}\n{left}Category{right}" in text  # no comma after Description
    assert f"Angie{apostrophe}s List Quote Request" in text
    assert f"Indianapolis {dash} House Cleaning" in text
    assert '"' not in text  # not a single straight quote
    assert text.strip().splitlines()[0] == "{" and text.strip().splitlines()[-1] == "}"


def test_malformed_spec_example_is_salvaged_into_a_complete_lead(client, ctx, mailer, clock):
    # A lead must not be lost because the sender's JSON is broken.
    body = post_sample(client, "malformed_spec_example.txt").json()

    assert body["result"] == "processed" and body["duplicate"] is None
    assert body["parseMode"] == "tolerant"
    assert any("<malformed-json>" in warning for warning in body["warnings"])
    lead = db_one(ctx, "SELECT * FROM leads WHERE id = ?", (body["leadId"],))
    assert (lead["first_name"], lead["last_name"]) == ("Bob", "Builder")
    assert lead["email_normalized"] == "bob.builder@example.com"
    assert lead["phone_normalized"] == "+15554332646"
    assert (lead["address_line1"], lead["city"], lead["state"], lead["postal_code"]) == (
        "123 Main St.", "Indianapolis", "IN", "46203",
    )
    assert lead["comments"] == "I'm Looking for recurring house cleaning services please."
    assert (lead["market"], lead["task_name"], lead["urgency"]) == ("Indianapolis", "House Cleaning", "This Week")
    assert lead["external_lead_id"] == MALFORMED_LEAD_ID
    assert lead["tenant_id"] == "tnt_sparkle"
    clock.advance(seconds=1)
    ctx.worker.deliver_due()
    assert [email.to for email in mailer.sent] == ["bob.builder@example.com"]


@pytest.mark.parametrize(
    "first, second",
    [
        ("angi_ads_spec_example.json", "malformed_spec_example.txt"),
        ("malformed_spec_example.txt", "angi_ads_spec_example.json"),
    ],
)
def test_spec_example_and_its_malformed_twin_never_collide_on_one_lead_id(client, ctx, mailer, clock, first, second):
    # Each file has its own CorrelationId, so the second one is never swallowed as a redelivery
    # or applied as a "correction" to the first (which used to re-address a demo lead's emails).
    # Sent exactly as the files are they are the same homeowner asking twice: two leads, the
    # second flagged as a duplicate, each keeping its own address.
    original = post_sample(client, first).json()
    assert original["result"] == "processed"
    clock.advance(minutes=1)

    repeat = post_sample(client, second).json()

    assert repeat["result"] == "processed" and repeat["leadId"] != original["leadId"]
    assert repeat["duplicate"]["ofLeadId"] == original["leadId"]
    assert sorted(repeat["duplicate"]["matchedOn"]) == ["email", "phone"]
    leads = db_rows(ctx, "SELECT external_lead_id, email_normalized FROM leads ORDER BY received_at")
    assert sorted(lead["external_lead_id"] for lead in leads) == [SPEC_LEAD_ID, MALFORMED_LEAD_ID]
    assert {lead["email_normalized"] for lead in leads} == {"bob.builder@example.com"}
    assert db_one(ctx, "SELECT COUNT(*) AS n FROM lead_events WHERE type = 'lead.updated'")["n"] == 0


def test_malformed_sample_after_a_same_id_send_to_a_reviewer_leaves_that_lead_alone(client, ctx, mailer, clock, clean_env):
    # The demo, in order: the spec example goes to a reviewer's address with its own lead id
    # (twice), then the malformed sample is sent. The reviewer's lead must keep its address and
    # its queued follow-ups; the salvaged lead is a lead of its own.
    send_lead.main(["--api-key", API_KEY, "--email", "reviewer@example.org", "--same-id", "--repeat", "2"], client=client)
    clock.advance(seconds=1)
    ctx.worker.deliver_due()

    code = send_lead.main(["--api-key", API_KEY, "--sample", "malformed_spec_example", "--raw"], client=client)

    assert code == 0
    reviewer = db_one(ctx, "SELECT * FROM leads WHERE external_lead_id = ?", (SPEC_LEAD_ID,))
    assert reviewer["email_normalized"] == "reviewer@example.org"
    waiting = db_rows(ctx, "SELECT to_address FROM outbox_messages WHERE lead_id = ? AND status = 'pending'", (reviewer["id"],))
    assert [row["to_address"] for row in waiting] == ["reviewer@example.org"] * 2  # its two follow-ups
    salvaged = db_one(ctx, "SELECT * FROM leads WHERE external_lead_id = ?", (MALFORMED_LEAD_ID,))
    assert salvaged["id"] != reviewer["id"] and salvaged["parse_mode"] == "tolerant"
    assert db_one(ctx, "SELECT COUNT(*) AS n FROM leads")["n"] == 2


def test_emergency_hvac_sample_goes_to_summit_with_its_urgency(client, ctx, mailer, clock):
    # ALAccountId 200001 is Summit Heating & Air; the Angi Ads feed has no price, so the account default applies.
    body = post_sample(client, "angi_ads_emergency_hvac.json").json()

    assert body["tenant"] == {"id": "tnt_summit", "name": "Summit Heating & Air", "routedBy": "account_id"}
    assert body["format"] == "angi_ads" and body["parseMode"] == "strict" and body["warnings"] == []
    lead = db_one(ctx, "SELECT * FROM leads WHERE id = ?", (body["leadId"],))
    assert lead["urgency"] == "Emergency"
    assert (lead["market"], lead["task_name"]) == ("Denver", "Heating & Furnace Repair")
    assert "furnace" in lead["comments"].lower()
    assert (lead["fee_cents"], lead["fee_source"]) == (4500, "account_default")
    assert alerts(ctx) == []
    clock.advance(seconds=1)
    ctx.worker.deliver_due()
    assert mailer.sent[0].to == "maria.alvarez@example.com"
    assert mailer.sent[0].from_name == "Summit Heating & Air"
    assert "Heating & Furnace Repair" in mailer.sent[0].subject


def test_plumbing_sample_goes_to_blueline(client, ctx):
    # ALAccountId 200002 is BlueLine Plumbing in Austin.
    body = post_sample(client, "angi_ads_plumbing.json").json()

    assert body["tenant"]["id"] == "tnt_blueline"
    lead = db_one(ctx, "SELECT * FROM leads WHERE id = ?", (body["leadId"],))
    assert (lead["city"], lead["state"], lead["market"]) == ("Austin", "TX", "Austin")
    assert (lead["fee_cents"], lead["fee_source"]) == (3900, "account_default")


def test_angi_leads_sample_keeps_its_own_fee_and_interview(client, ctx):
    # The Angi Leads feed carries the price and the consumer's interview answers.
    body = post_sample(client, "angi_leads_example.json").json()

    assert body["format"] == "angi_leads" and body["parseMode"] == "strict" and body["warnings"] == []
    assert body["tenant"]["id"] == "tnt_summit" and body["tenant"]["routedBy"] == "account_id"
    lead = db_one(ctx, "SELECT * FROM leads WHERE id = ?", (body["leadId"],))
    assert (lead["fee_cents"], lead["fee_source"]) == (4250, "payload")
    assert lead["external_account_id"] == "12345678"
    assert (lead["external_lead_id"], lead["external_request_id"]) == ("613305872", "284417306")
    assert len(json.loads(lead["interview_json"])) == 3
    assert lead["phone_is_masked"] == 0
    assert lead["automated_contact_ok"] == 1
    assert alerts(ctx) == []


def test_masked_appointment_sample_is_routed_by_crm_key_and_typed_correctly(client, ctx):
    # crmKey is our own tenant id echoed back; "false" as a string must not count as consent.
    body = post_sample(client, "angi_leads_masked_with_appointment.json").json()

    assert body["tenant"]["id"] == "tnt_summit" and body["tenant"]["routedBy"] == "crm_key"
    assert body["parseMode"] == "strict"
    lead = db_one(ctx, "SELECT * FROM leads WHERE id = ?", (body["leadId"],))
    assert lead["phone_is_masked"] == 1
    assert lead["automated_contact_ok"] == 0
    assert lead["contact_status"] == "New Appt - Transferred"
    assert lead["fee_cents"] == 6100
    extras = json.loads(lead["extras_json"])
    assert extras["appointment.status"] == "Confirmed" and extras["appointment.type"] == "Service"
    assert extras["appointment.appointmentOid"] == 77120045
    assert extras["appointment.external_id"] == "angi-appt-77120045"
    assert extras["primaryPhoneDetails.smsEnabled"] is True
    assert "primaryPhoneDetails.expirationDateTime" in extras
    # Its spEntityId was unknown: the mapping is learned, so the next lead needs no crmKey.
    learned = db_one(ctx, "SELECT * FROM tenant_lead_sources WHERE external_account_id = '23456789'")
    assert learned["tenant_id"] == "tnt_summit" and learned["learned"] == 1
    # Everything in it is documented by Angi, so it is not format drift.
    assert [a for a in alerts(ctx) if a["kind"] == "schema_drift"] == []


def test_masked_phone_of_the_sample_never_makes_another_consumer_a_duplicate(client):
    # A masked number is a temporary Angi proxy shared by different people over time.
    masked = sample_json("angi_leads_masked_with_appointment.json")
    assert post_lead(client, masked).json()["result"] == "processed"

    stranger = leads_payload(primaryPhone=masked["primaryPhone"], email="someone.else@example.com", name="Sam Else")
    body = post_lead(client, stranger).json()

    assert body["result"] == "processed"
    assert body["duplicate"] is None


def test_new_fields_sample_is_processed_and_reported_as_drift(client, ctx, mailer, clock):
    # New fields must never stop a lead; they are kept and somebody is told.
    body = post_sample(client, "drift_new_fields.json").json()

    assert body["format"] == "angi_ads"
    warnings = " | ".join(body["warnings"])
    assert "PreferredContactTime" in warnings and "TrustedFormCertUrl" in warnings
    lead = db_one(ctx, "SELECT * FROM leads WHERE id = ?", (body["leadId"],))
    extras = json.loads(lead["extras_json"])
    assert extras["PreferredContactTime"] == "Evenings after 6 PM"
    # The Angi Ads feed documents no TrustedForm field, so the undocumented one is kept as
    # it came, not guessed into the certificate column (aliases only fill documented fields).
    assert extras["TrustedFormCertUrl"] == "https://cert.trustedform.com/0f1e2d3c4b5a49788796a5b4c3d2e1f0"
    assert lead["trusted_form_url"] is None and body["parseMode"] == "strict"
    drift = [a for a in alerts(ctx) if a["kind"] == "schema_drift"]
    assert len(drift) == 1 and drift[0]["status"] == "open"
    assert "PreferredContactTime" in drift[0]["title"]
    clock.advance(seconds=1)
    ctx.worker.deliver_due()
    assert [email.to for email in mailer.sent] == ["hannah.brooks@example.com"]


def test_new_fields_drift_raises_one_alert_however_many_leads_show_it(client, ctx):
    # One alert per kind of deviation (with a counter), not one per lead.
    payload = sample_json("drift_new_fields.json")
    for _ in range(3):
        fresh = send_lead.with_fresh_ids(payload)
        fresh["Email"] = f"{uuid.uuid4().hex[:8]}@example.com"
        fresh["PhoneNumber"] = "3175550101"
        assert post_lead(client, fresh).json()["result"] == "processed"

    drift = db_rows(ctx, "SELECT count FROM alerts WHERE kind = 'schema_drift'")
    assert [row["count"] for row in drift] == [3]


def test_renamed_and_recased_sample_is_fully_recovered_by_alias(client, ctx, mailer, clock):
    # A casing change and a renamed email field must not cost us the lead or the email.
    raw = sample_json("drift_renamed_and_recased.json")
    assert "emailAddress" in raw and "Email" not in raw and isinstance(raw["phoneNumber"], int)

    body = post_sample(client, "drift_renamed_and_recased.json").json()

    assert body["format"] == "angi_ads" and body["parseMode"] == "tolerant"
    assert body["tenant"]["id"] == "tnt_sparkle"
    lead = db_one(ctx, "SELECT * FROM leads WHERE id = ?", (body["leadId"],))
    assert lead["full_name"] == "Marcus Hill"
    assert lead["email_normalized"] == "marcus.hill@example.com"
    assert lead["phone_normalized"] == "+13175550126"
    assert (lead["city"], lead["state"], lead["postal_code"]) == ("Indianapolis", "IN", "46205")
    assert (lead["market"], lead["task_name"]) == ("Indianapolis", "House Cleaning")
    assert lead["external_lead_id"] == raw["correlationId"]
    assert lead["needs_review"] == 1
    drift = [a for a in alerts(ctx) if a["kind"] == "schema_drift"]
    assert len(drift) == 1 and drift[0]["severity"] == "warning"
    clock.advance(seconds=1)
    ctx.worker.deliver_due()
    assert [email.to for email in mailer.sent] == ["marcus.hill@example.com"]


def test_unknown_format_sample_still_reaches_the_consumer_and_raises_a_critical_alert(client, ctx, mailer, clock):
    # An unrecognisable shape that still carries a name, email and phone is answered, loudly.
    body = post_sample(client, "unknown_format.json").json()

    assert body["format"] == "unknown" and body["parseMode"] == "tolerant"
    assert body["tenant"]["id"] == "tnt_sparkle"
    lead = db_one(ctx, "SELECT * FROM leads WHERE id = ?", (body["leadId"],))
    assert lead["full_name"] == "Olivia Chen"
    assert lead["email_normalized"] == "olivia.chen@example.com"
    assert lead["phone_normalized"] == "+13175550184"
    assert lead["task_name"] == "Recurring house cleaning"
    assert lead["postal_code"] == "46220"
    assert lead["external_lead_id"] is None and lead["needs_review"] == 1
    drift = [a for a in alerts(ctx) if a["kind"] == "schema_drift"]
    assert len(drift) == 1 and drift[0]["severity"] == "critical" and "unknown format" in drift[0]["title"]
    clock.advance(seconds=1)
    ctx.worker.deliver_due()
    assert [email.to for email in mailer.sent] == ["olivia.chen@example.com"]


def test_unknown_format_sample_sent_twice_is_one_lead(client, ctx):
    # With no lead id to go by, identical bytes are still recognised as the same delivery.
    assert post_sample(client, "unknown_format.json").json()["result"] == "processed"

    assert post_sample(client, "unknown_format.json").json()["result"] == "duplicate_delivery"
    assert db_one(ctx, "SELECT COUNT(*) AS n FROM leads")["n"] == 1


def test_unmapped_account_sample_is_quarantined_without_emailing_anyone(client, ctx, mailer, clock):
    # We never guess which business a consumer contacted.
    body = post_sample(client, "unmapped_account.json").json()

    assert body["reason"] == "unmapped_account" and body["angiAccountId"] == "000000"
    assert db_one(ctx, "SELECT COUNT(*) AS n FROM leads")["n"] == 0
    event = db_one(ctx, "SELECT status, status_reason FROM webhook_events WHERE id = ?", (body["eventId"],))
    assert (event["status"], event["status_reason"]) == ("quarantined", "unmapped_account")
    assert [(a["kind"], a["severity"]) for a in alerts(ctx)] == [("unmapped_account", "critical")]
    clock.advance(seconds=1)
    assert ctx.worker.deliver_due() == 0
    assert mailer.sent == []


def test_unmapped_account_sample_is_processed_once_the_mapping_is_added(client, ctx, mailer, clock):
    # The quarantined lead waits for the mapping and is then answered automatically, exactly once.
    post_sample(client, "unmapped_account.json")
    post_sample(client, "unmapped_account.json")  # Angi retried it meanwhile
    clock.advance(minutes=10)

    response = client.post(
        "/admin/api/tenants/tnt_blueline/lead-sources",
        json={"external_account_id": "000000", "external_account_name": "BlueLine Phoenix", "default_fee": 41},
        headers=ADMIN,
    )

    assert response.status_code == 201
    assert sorted(item["result"] for item in response.json()["replayed"]) == ["duplicate_delivery", "processed"]
    leads = db_rows(ctx, "SELECT * FROM leads")
    assert len(leads) == 1
    assert leads[0]["tenant_id"] == "tnt_blueline" and leads[0]["full_name"] == "Grace Okafor"
    assert leads[0]["fee_cents"] == 4100
    assert leads[0]["received_at"] == "2026-11-12T15:00:00.000Z"  # the speed-to-lead clock kept running
    clock.advance(seconds=1)
    ctx.worker.deliver_due()
    assert [email.to for email in mailer.sent] == ["grace.okafor@example.com"]


def test_unmapped_account_alert_clears_once_the_mapping_is_added(client, ctx):
    # An alert about a missing mapping has nothing left to say once the mapping exists.
    post_sample(client, "unmapped_account.json")
    client.post(
        "/admin/api/tenants/tnt_blueline/lead-sources", json={"external_account_id": "000000"}, headers=ADMIN
    )
    ctx.worker.run_maintenance()

    still_open = db_rows(ctx, "SELECT kind FROM alerts WHERE status = 'open' AND kind = 'unmapped_account'")
    assert still_open == []


def test_missing_account_id_sample_waits_for_an_operator_and_emails_nobody(client, ctx, mailer, clock):
    # A payload that names no Angi account cannot be routed by any mapping; it must not be
    # guessed at either. It is stored, kept out of the leads, and the gap is reported as drift.
    assert "ALAccountId" not in sample_json("missing_account_id.json")

    body = post_sample(client, "missing_account_id.json").json()

    assert body["result"] == "quarantined"
    assert db_one(ctx, "SELECT COUNT(*) AS n FROM leads")["n"] == 0
    event = db_one(ctx, "SELECT status, external_account_id, raw_body FROM webhook_events WHERE id = ?", (body["eventId"],))
    assert (event["status"], event["external_account_id"]) == ("quarantined", None)
    assert "tessa.morgan@example.com" in event["raw_body"]
    drift = [a for a in alerts(ctx) if a["kind"] == "schema_drift"]
    assert len(drift) == 1 and "ALAccountId" in drift[0]["title"]
    clock.advance(seconds=1)
    assert ctx.worker.deliver_due() == 0 and mailer.sent == []


def test_missing_email_sample_is_stored_for_a_phone_call_and_queues_nothing(client, ctx, mailer, clock):
    # No address means no email, but the lead (and its phone number) is still worth money.
    body = post_sample(client, "missing_email.json").json()

    assert body["email"] == {"status": "skipped", "reason": "no_valid_email"}
    lead = db_one(ctx, "SELECT * FROM leads WHERE id = ?", (body["leadId"],))
    assert lead["tenant_id"] == "tnt_blueline"
    assert lead["email_normalized"] is None and lead["phone_normalized"] == "+15125550137"
    assert lead["needs_review"] == 1
    assert db_one(ctx, "SELECT COUNT(*) AS n FROM outbox_messages")["n"] == 0
    clock.advance(hours=48)
    assert ctx.worker.deliver_due() == 0
    assert mailer.sent == []


def test_unrecognised_sample_creates_no_lead_but_is_kept_for_review(client, ctx, mailer):
    # A payload with nobody to contact is not a lead; it is evidence.
    body = post_sample(client, "unrecognised.json").json()

    assert body["reason"] == "unrecognised_payload"
    assert db_one(ctx, "SELECT COUNT(*) AS n FROM leads")["n"] == 0
    event = db_one(ctx, "SELECT status, raw_body FROM webhook_events WHERE id = ?", (body["eventId"],))
    assert event["status"] == "quarantined"
    assert json.loads(event["raw_body"]) == sample_json("unrecognised.json")
    assert [(a["kind"], a["severity"]) for a in alerts(ctx)] == [("unrecognised_payload", "critical")]
    assert mailer.sent == []


def test_all_valid_samples_sent_one_after_another_never_look_like_duplicates(client, ctx):
    # Each JSON sample is a different consumer, so a demo that walks through them triggers no credit claim.
    for name in JSON_SAMPLES:
        assert post_sample(client, name).status_code == 200

    assert db_one(ctx, "SELECT COUNT(*) AS n FROM leads WHERE is_duplicate = 1")["n"] == 0
    assert db_one(ctx, "SELECT COUNT(*) AS n FROM credit_claims")["n"] == 0
    assert db_one(ctx, "SELECT COUNT(*) AS n FROM leads")["n"] == 9  # all but the three quarantined ones


# =========================================================================================
# scripts/send_lead.py - the importable pieces
# =========================================================================================

EXPECTED_FORMATS = {
    "angi_ads_spec_example.json": "angi_ads",
    "angi_ads_emergency_hvac.json": "angi_ads",
    "angi_ads_plumbing.json": "angi_ads",
    "angi_leads_example.json": "angi_leads",
    "angi_leads_masked_with_appointment.json": "angi_leads",
    "drift_new_fields.json": "angi_ads",
    "drift_renamed_and_recased.json": "angi_ads",
    "missing_email.json": "angi_ads",
    "missing_account_id.json": "angi_ads",
    "unmapped_account.json": "angi_ads",
    "unknown_format.json": None,
    "unrecognised.json": None,
}


@pytest.mark.parametrize("name", JSON_SAMPLES)
def test_send_lead_recognises_which_feed_a_sample_is(name):
    # Overrides and fresh ids depend on knowing whether the keys are PascalCase or camelCase.
    assert send_lead.payload_format(sample_json(name)) == EXPECTED_FORMATS[name]


def test_overrides_on_an_angi_ads_payload_set_the_pascal_case_fields():
    # --email / --first-name / --last-name / --phone / --account for the Angi Ads feed.
    original = sample_json("angi_ads_spec_example.json")
    untouched = json.loads(json.dumps(original))

    result = send_lead.apply_overrides(
        original, email="me@example.com", first_name="Dana", last_name="Whitfield", phone="3035550199", account="200001"
    )

    assert result["Email"] == "me@example.com"
    assert (result["FirstName"], result["LastName"]) == ("Dana", "Whitfield")
    assert result["PhoneNumber"] == "3035550199"
    assert result["ALAccountId"] == "200001"
    changed = {"Email", "FirstName", "LastName", "PhoneNumber", "ALAccountId"}
    assert {key: value for key, value in result.items() if key not in changed} == {
        key: value for key, value in untouched.items() if key not in changed
    }
    assert original == untouched  # the sample itself is never modified


def test_overrides_on_an_angi_leads_payload_set_the_camel_case_fields_and_the_full_name():
    # The Angi Leads feed spells the same things differently and also carries "name".
    original = sample_json("angi_leads_example.json")

    result = send_lead.apply_overrides(
        original, email="me@example.com", first_name="Dana", last_name="Whitfield", phone="3035550199", account="567890123"
    )

    assert result["email"] == "me@example.com"
    assert (result["firstName"], result["lastName"], result["name"]) == ("Dana", "Whitfield", "Dana Whitfield")
    assert result["primaryPhone"] == "3035550199"
    assert result["spEntityId"] == 567890123  # a number, as Angi sends it
    assert "Email" not in result and "FirstName" not in result and "ALAccountId" not in result
    assert result["leadOid"] == original["leadOid"] and result["interview"] == original["interview"]


def test_overriding_only_the_last_name_keeps_the_first_name_in_the_full_name():
    # "name" must stay consistent with firstName + lastName.
    result = send_lead.apply_overrides(sample_json("angi_leads_example.json"), last_name="Okoro")

    assert (result["firstName"], result["lastName"], result["name"]) == ("Priya", "Okoro", "Priya Okoro")


def test_no_overrides_returns_an_equal_copy():
    # Not passing an option must leave the sample's own value alone.
    original = sample_json("angi_ads_plumbing.json")

    result = send_lead.apply_overrides(original, email=None, phone=None)

    assert result == original and result is not original


def test_overrides_keep_the_spelling_of_a_drifted_sample():
    # Addressing the drift sample to yourself must not quietly repair the drift it demonstrates.
    result = send_lead.apply_overrides(
        sample_json("drift_renamed_and_recased.json"),
        email="me@example.com", first_name="Dana", last_name="Whitfield", phone="3175550199", account="200002",
    )

    assert result["emailAddress"] == "me@example.com"
    assert (result["firstName"], result["lastName"]) == ("Dana", "Whitfield")
    assert result["phoneNumber"] == "3175550199" and result["alAccountId"] == "200002"
    assert not {"Email", "FirstName", "LastName", "PhoneNumber", "ALAccountId", "name"} & set(result)


def test_overriding_a_field_the_sample_lacks_adds_it_under_its_documented_name():
    # e.g. --email on a payload that has no email field at all.
    payload = sample_json("angi_ads_plumbing.json")
    del payload["Email"]

    assert send_lead.apply_overrides(payload, email="me@example.com")["Email"] == "me@example.com"


def test_overrides_are_refused_for_a_shape_that_is_neither_feed():
    # Guessing where the email lives in an unknown shape would hide what the sample is for.
    with pytest.raises(ValueError, match="neither Angi Ads nor Angi Leads"):
        send_lead.apply_overrides(sample_json("unknown_format.json"), email="me@example.com")

    assert send_lead.apply_overrides(sample_json("unknown_format.json")) == sample_json("unknown_format.json")


def test_fresh_ids_give_an_angi_ads_payload_a_new_correlation_id():
    # A new CorrelationId is what makes Angi (and us) treat a payload as a different lead.
    original = sample_json("angi_ads_spec_example.json")

    first = send_lead.with_fresh_ids(original)
    second = send_lead.with_fresh_ids(original)

    assert str(uuid.UUID(first["CorrelationId"])) == first["CorrelationId"]
    assert len({original["CorrelationId"], first["CorrelationId"], second["CorrelationId"]}) == 3
    assert {k: v for k, v in first.items() if k != "CorrelationId"} == {
        k: v for k, v in original.items() if k != "CorrelationId"
    }
    assert original["CorrelationId"] == "61a7de56-dba3-4e59-8e2a-3fa827f84f7f"


def test_fresh_ids_give_an_angi_leads_payload_a_new_lead_and_request_id():
    # leadOid identifies the lead, srOid the consumer's request; both must change, as numbers.
    random.seed(20261112)
    original = sample_json("angi_leads_example.json")

    fresh = send_lead.with_fresh_ids(original)

    assert isinstance(fresh["leadOid"], int) and isinstance(fresh["srOid"], int)
    assert 100_000_000 <= fresh["leadOid"] <= 999_999_999 and 100_000_000 <= fresh["srOid"] <= 999_999_999
    assert fresh["leadOid"] != original["leadOid"] and fresh["srOid"] != original["srOid"]
    assert fresh["leadOid"] != fresh["srOid"]
    assert {k: v for k, v in fresh.items() if k not in ("leadOid", "srOid")} == {
        k: v for k, v in original.items() if k not in ("leadOid", "srOid")
    }


def test_fresh_ids_keep_the_spelling_of_a_recased_sample_and_leave_other_shapes_alone():
    # correlationId stays correlationId; a shape without a known id field is returned as it is.
    recased = send_lead.with_fresh_ids(sample_json("drift_renamed_and_recased.json"))
    assert "CorrelationId" not in recased
    assert recased["correlationId"] != sample_json("drift_renamed_and_recased.json")["correlationId"]

    assert send_lead.with_fresh_ids(sample_json("unknown_format.json")) == sample_json("unknown_format.json")
    assert send_lead.with_fresh_ids(sample_json("unrecognised.json")) == sample_json("unrecognised.json")


def test_build_body_gives_a_new_lead_by_default_and_the_same_lead_with_same_id():
    # Default = every run is a new lead; --same-id = a redelivery, byte-identical in content.
    raw = sample_bytes("angi_ads_spec_example.json")

    body, lead_id = send_lead.build_body(raw)
    same_body, same_lead_id = send_lead.build_body(raw, same_id=True)

    assert json.loads(body)["CorrelationId"] == lead_id != "61a7de56-dba3-4e59-8e2a-3fa827f84f7f"
    assert json.loads(same_body) == json.loads(raw)
    assert same_lead_id == "61a7de56-dba3-4e59-8e2a-3fa827f84f7f"
    assert send_lead.build_body(raw, same_id=True) == send_lead.build_body(raw, same_id=True)  # nothing random in it


def test_build_body_gives_every_send_a_consumer_of_its_own_unless_told_otherwise():
    # The spec example's phone number is the same for everybody who downloads the spec. If
    # every send kept it, two people trying the demo would be flagged as each other's
    # duplicate. So: a fresh fictional phone per send, unless --phone, --same-consumer or
    # --same-id says who the consumer is.
    raw = sample_bytes("angi_ads_spec_example.json")
    sample_phone = sample_json("angi_ads_spec_example.json")["PhoneNumber"]

    phones = {json.loads(send_lead.build_body(raw)[0])["PhoneNumber"] for _ in range(20)}
    assert sample_phone not in phones and len(phones) >= 19
    assert all(re.fullmatch(FICTIONAL_PHONE, phone) for phone in phones)

    chosen = json.loads(send_lead.build_body(raw, overrides={"phone": "3175550142"})[0])
    assert chosen["PhoneNumber"] == "3175550142"
    same_consumer = json.loads(send_lead.build_body(raw, same_consumer=True)[0])
    assert same_consumer["PhoneNumber"] == sample_phone
    assert same_consumer["CorrelationId"] != "61a7de56-dba3-4e59-8e2a-3fa827f84f7f"  # still a new lead
    assert json.loads(send_lead.build_body(raw, same_id=True)[0])["PhoneNumber"] == sample_phone
    # Everything else in the payload is left exactly as the sample has it.
    fresh = json.loads(send_lead.build_body(raw)[0])
    untouched = {key: value for key, value in sample_json("angi_ads_spec_example.json").items()
                 if key not in ("PhoneNumber", "CorrelationId")}
    assert {key: value for key, value in fresh.items() if key not in ("PhoneNumber", "CorrelationId")} == untouched


def test_fictional_phone_is_a_well_formed_number_nobody_owns():
    # 555-0100..0199 is reserved for fiction in every area code; the area code itself must
    # look real enough for the service to normalise it (and so to compare it for duplicates).
    from app.util import normalize_phone

    random.seed(20261112)
    numbers = [send_lead.fictional_phone() for _ in range(500)]

    assert all(re.fullmatch(FICTIONAL_PHONE, number) for number in numbers)
    assert all(normalize_phone(number) == f"+1{number}" for number in numbers)
    assert not [number for number in numbers if number[1:3] == "11" or number[1] == "9"]  # no 911, no N9X
    assert len({number[:3] for number in numbers}) > 200  # spread over many area codes


def test_fresh_phone_keeps_the_field_name_and_type_of_each_sample():
    # The Angi Leads feed calls it primaryPhone; one drift sample sends it re-cased and as a
    # number, on purpose. A shape that is neither feed is left alone.
    leads = send_lead.with_fresh_phone(sample_json("angi_leads_example.json"))
    assert re.fullmatch(FICTIONAL_PHONE, leads["primaryPhone"])
    assert leads["primaryPhone"] != sample_json("angi_leads_example.json")["primaryPhone"]
    assert "PhoneNumber" not in leads

    drifted = send_lead.with_fresh_phone(sample_json("drift_renamed_and_recased.json"))
    assert isinstance(drifted["phoneNumber"], int) and re.fullmatch(FICTIONAL_PHONE, str(drifted["phoneNumber"]))
    assert "PhoneNumber" not in drifted and "primaryPhone" not in drifted

    assert send_lead.with_fresh_phone(sample_json("unknown_format.json")) == sample_json("unknown_format.json")
    no_phone = sample_json("angi_ads_plumbing.json")
    del no_phone["PhoneNumber"]
    assert send_lead.with_fresh_phone(no_phone) == no_phone  # a sample without a phone stays without one


def test_build_body_raw_sends_the_file_bytes_untouched():
    # --raw exists for the malformed sample: not one byte may change.
    for name in ("malformed_spec_example.txt", "angi_ads_spec_example.json"):
        raw = sample_bytes(name)
        assert send_lead.build_body(raw, send_raw=True) == (raw, None)

    # A file that is not JSON is sent untouched even without --raw.
    malformed = sample_bytes("malformed_spec_example.txt")
    assert send_lead.build_body(malformed) == (malformed, None)


def test_build_body_refuses_overrides_it_cannot_apply():
    # Silently ignoring --email would send the lead to the sample's address instead of yours.
    with pytest.raises(ValueError, match="--raw"):
        send_lead.build_body(sample_bytes("angi_ads_spec_example.json"), send_raw=True, overrides={"email": "a@example.com"})
    with pytest.raises(ValueError, match="not valid JSON"):
        send_lead.build_body(sample_bytes("malformed_spec_example.txt"), overrides={"email": "a@example.com"})
    with pytest.raises(ValueError, match="not a JSON object"):
        send_lead.build_body(b"[1, 2, 3]", overrides={"email": "a@example.com"})

    assert send_lead.build_body(b"[1, 2, 3]", overrides={"email": None}) == (b"[1, 2, 3]", None)


def test_build_body_keeps_non_ascii_text_intact():
    # "Indianapolis – House Cleaning" must arrive with its dash, or the market split stops working.
    body, _ = send_lead.build_body(sample_bytes("angi_ads_spec_example.json"), overrides={"first_name": "Zoë"})

    payload = json.loads(body.decode("utf-8"))
    assert payload["Category"] == "Indianapolis – House Cleaning"
    assert payload["FirstName"] == "Zoë"


def test_resolve_sample_accepts_a_name_or_a_path(tmp_path):
    # --sample angi_ads_plumbing and --sample ./my/own.json both work.
    assert send_lead.resolve_sample("angi_ads_plumbing") == SAMPLES / "angi_ads_plumbing.json"
    assert send_lead.resolve_sample("angi_ads_plumbing.json") == SAMPLES / "angi_ads_plumbing.json"
    assert send_lead.resolve_sample("malformed_spec_example") == SAMPLES / "malformed_spec_example.txt"
    own = tmp_path / "own.json"
    own.write_text("{}")
    assert send_lead.resolve_sample(str(own)) == own

    with pytest.raises(FileNotFoundError, match="angi_ads_spec_example"):  # the error lists what exists
        send_lead.resolve_sample("no_such_sample")


def test_api_key_default_is_the_first_of_the_rotation_list():
    # ANGI_WEBHOOK_API_KEYS can hold "new,old" during a rotation.
    assert send_lead.first_api_key("new-key, old-key") == "new-key"
    assert send_lead.first_api_key(" , only ,") == "only"
    assert send_lead.first_api_key("") == ""


def test_settings_come_from_the_environment_then_dotenv_then_the_default(clean_env, monkeypatch):
    # Same precedence as the server: a real environment variable beats the .env file.
    (clean_env / ".env").write_text(
        '# a comment\nPUBLIC_BASE_URL=https://from-dotenv.example\nANGI_WEBHOOK_API_KEYS="quoted-key,second"\n\nBROKEN LINE\n'
    )

    assert send_lead.setting("PUBLIC_BASE_URL", "http://localhost:8000") == "https://from-dotenv.example"
    assert send_lead.first_api_key(send_lead.setting("ANGI_WEBHOOK_API_KEYS")) == "quoted-key"
    assert send_lead.setting("ADMIN_TOKEN", "fallback") == "fallback"
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://from-env.example")
    assert send_lead.setting("PUBLIC_BASE_URL") == "https://from-env.example"
    assert send_lead.read_dotenv(clean_env / "missing.env") == {}


def test_webhook_url_and_headers_for_the_shared_and_the_tenant_keyed_endpoint():
    # The API key goes in X-API-KEY; a tenant-keyed URL is its own credential and needs no key.
    assert send_lead.webhook_url("http://localhost:8000/") == "http://localhost:8000/webhooks/angi/leads"
    assert send_lead.webhook_url("https://x.example", "k3y") == "https://x.example/webhooks/angi/leads/k3y"
    assert send_lead.request_headers("secret") == {"Content-Type": "application/json", "X-API-KEY": "secret"}
    assert send_lead.request_headers("secret", "k3y") == {"Content-Type": "application/json"}
    assert send_lead.request_headers("") == {"Content-Type": "application/json"}


def test_addresses_that_could_be_a_real_mailbox_are_spotted():
    # Angi's own example was printed with a gmail.com address; reserved example domains are safe.
    as_printed = sample_bytes("malformed_spec_example.txt").replace(b"@example.com", b"@gmail.com")
    assert send_lead.real_looking_addresses(as_printed) == ["bob.builder@gmail.com"]  # found in a body that is not JSON
    assert send_lead.real_looking_addresses(sample_bytes("angi_ads_plumbing.json")) == []
    assert send_lead.real_looking_addresses(b'{"lead": {"contact": {"mail": "deep@nested.org"}}}') == ["deep@nested.org"]
    assert send_lead.real_looking_addresses(b'{"Email": "a@notexample.com", "x": "b@sub.example.com", "y": "c@x.test"}') == [
        "a@notexample.com"
    ]


def test_post_lead_reports_status_time_and_a_non_json_answer_as_text():
    # A tunnel error page is HTML, not JSON; the tool must show it rather than crash.
    class Gateway:
        def post(self, url, content, headers):
            return FakeResponse(502, "<html>Bad Gateway</html>")

    status, elapsed_ms, answer = send_lead.post_lead(Gateway(), "http://x/webhooks/angi/leads", b"{}", {})

    assert status == 502 and answer == "<html>Bad Gateway</html>" and elapsed_ms >= 0
    assert send_lead.format_answer(answer) == "<html>Bad Gateway</html>"
    assert json.loads(send_lead.format_answer({"status": "success"})) == {"status": "success"}


# --- scripts/send_lead.py - the command line, against the real application ---------------


def test_send_lead_cli_sends_the_spec_example_as_a_new_lead_addressed_to_you(client, ctx, clean_env, capsys):
    # The everyday demo command: the default sample, your address, a fresh lead id.
    code = send_lead.main(["--api-key", API_KEY, "--email", "you@example.com"], client=client)

    out = capsys.readouterr().out
    assert code == 0
    assert "HTTP 200 in " in out and " ms" in out
    assert '"result": "processed"' in out
    lead = db_one(ctx, "SELECT * FROM leads")
    assert lead["email_normalized"] == "you@example.com"
    assert lead["external_lead_id"] != "61a7de56-dba3-4e59-8e2a-3fa827f84f7f"
    assert lead["external_lead_id"] in out
    assert lead["tenant_id"] == "tnt_sparkle"


def test_send_lead_cli_never_prints_the_api_key(client, clean_env, capsys, monkeypatch):
    # The key comes from the environment and must not end up in a terminal recording.
    monkeypatch.setenv("ANGI_WEBHOOK_API_KEYS", f"{API_KEY},previous-key")

    code = send_lead.main(["--email", "you@example.com"], client=client)

    captured = capsys.readouterr()
    assert code == 0  # authenticated with the first key of the list
    assert API_KEY not in captured.out and API_KEY not in captured.err
    assert "previous-key" not in captured.out + captured.err


def test_send_lead_cli_reads_the_key_from_a_dotenv_file(client, clean_env, capsys):
    # With no exported variable, ./.env is where the demo's key lives.
    (clean_env / ".env").write_text(f"ANGI_WEBHOOK_API_KEYS={API_KEY}\n")

    assert send_lead.main(["--email", "you@example.com"], client=client) == 0
    assert "HTTP 200" in capsys.readouterr().out


def test_two_people_trying_the_demo_are_not_each_others_duplicate(client, ctx, mailer, clock, clean_env, capsys):
    # The presenter sends the spec example to their own address, then a reviewer does the same
    # with theirs. Different people: the second gets the normal introduction and its follow-ups,
    # nothing is flagged, and no credit claim is opened (they used to share the spec's phone).
    assert send_lead.main(["--api-key", API_KEY, "--email", "presenter@example.com"], client=client) == 0
    clock.advance(minutes=20)
    assert send_lead.main(["--api-key", API_KEY, "--email", "reviewer@example.org"], client=client) == 0

    out = capsys.readouterr().out
    assert out.count('"duplicate": null') == 2 and out.count('"template": "intro"') == 2
    assert out.count('"followUpsScheduled": 2') == 2
    leads = db_rows(ctx, "SELECT is_duplicate, phone_normalized FROM leads")
    assert [lead["is_duplicate"] for lead in leads] == [0, 0]
    assert len({lead["phone_normalized"] for lead in leads}) == 2
    assert db_one(ctx, "SELECT COUNT(*) AS n FROM credit_claims")["n"] == 0
    ctx.worker.deliver_due()
    assert sorted((email.to, email.subject.startswith("Your House Cleaning request")) for email in mailer.sent) == [
        ("presenter@example.com", True), ("reviewer@example.org", True),
    ]


def test_send_lead_cli_same_consumer_twice_is_two_leads_and_the_second_is_a_duplicate_lead(client, ctx, clean_env, capsys):
    # Same consumer (--same-consumer keeps the phone), new lead id = Angi charged twice:
    # flagged on phone and email, with a credit claim.
    code = send_lead.main(
        ["--api-key", API_KEY, "--email", "you@example.com", "--same-consumer", "--repeat", "2"], client=client
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "[1/2]" in out and "[2/2]" in out
    leads = db_rows(ctx, "SELECT is_duplicate, duplicate_matched_on, phone FROM leads ORDER BY created_at, is_duplicate")
    assert [lead["is_duplicate"] for lead in leads] == [0, 1]
    assert json.loads(leads[1]["duplicate_matched_on"]) == ["phone", "email"]
    assert {lead["phone"] for lead in leads} == {sample_json("angi_ads_spec_example.json")["PhoneNumber"]}
    assert db_one(ctx, "SELECT COUNT(*) AS n FROM credit_claims")["n"] == 1


def test_send_lead_cli_same_address_twice_is_the_same_consumer_even_with_a_new_phone(client, ctx, clean_env, capsys):
    # The address is the consumer too: two leads for one Angi account with the same --email are
    # a duplicate (matched on the email alone), which is what a presenter re-sending to their
    # own inbox should expect.
    code = send_lead.main(["--api-key", API_KEY, "--email", "you@example.com", "--repeat", "2"], client=client)

    assert code == 0
    leads = db_rows(ctx, "SELECT is_duplicate, duplicate_matched_on FROM leads ORDER BY created_at, is_duplicate")
    assert [lead["is_duplicate"] for lead in leads] == [0, 1]
    assert json.loads(leads[1]["duplicate_matched_on"]) == ["email"]


def test_send_lead_cli_same_id_is_a_redelivery_not_a_second_lead(client, ctx, clean_env, capsys):
    # Angi retrying one lead must never create a second lead or a second email.
    code = send_lead.main(["--api-key", API_KEY, "--same-id", "--repeat", "3", "--sample", "angi_leads_example"], client=client)

    out = capsys.readouterr().out
    assert code == 0
    assert out.count('"result": "processed"') == 1
    assert out.count('"result": "duplicate_delivery"') == 2
    assert db_one(ctx, "SELECT COUNT(*) AS n FROM leads")["n"] == 1
    assert db_one(ctx, "SELECT COUNT(*) AS n FROM outbox_messages WHERE template = 'intro'")["n"] == 1


def test_send_lead_cli_overrides_work_on_the_angi_leads_sample(client, ctx, clean_env, capsys):
    # --account on the camelCase feed selects the tenant through spEntityId.
    code = send_lead.main(
        ["--api-key", API_KEY, "--sample", "angi_leads_example", "--email", "you@example.com",
         "--first-name", "Dana", "--last-name", "Whitfield", "--phone", "(512) 555-0199", "--account", "567890123"],
        client=client,
    )

    assert code == 0
    lead = db_one(ctx, "SELECT * FROM leads")
    assert lead["tenant_id"] == "tnt_blueline"
    assert (lead["full_name"], lead["email_normalized"], lead["phone_normalized"]) == (
        "Dana Whitfield", "you@example.com", "+15125550199",
    )
    assert lead["source_format"] == "angi_leads"


@pytest.mark.parametrize("sample", ["angi_ads_spec_example", "angi_leads_example"])
def test_send_lead_cli_unknown_account_is_quarantined_and_still_a_success(client, ctx, clean_env, capsys, sample):
    # Quarantine is answered 200 (the lead is stored), so the tool exits 0.
    code = send_lead.main(["--api-key", API_KEY, "--sample", sample, "--account", "000000"], client=client)

    out = capsys.readouterr().out
    assert code == 0
    assert '"result": "quarantined"' in out and '"reason": "unmapped_account"' in out
    assert db_one(ctx, "SELECT COUNT(*) AS n FROM leads")["n"] == 0


def test_send_lead_cli_exits_non_zero_when_the_key_is_wrong(client, ctx, clean_env, capsys):
    # Anything but HTTP 200 is a failed delivery in Angi's eyes, and in the tool's exit code.
    code = send_lead.main(["--api-key", "wrong-key", "--email", "you@example.com"], client=client)

    out = capsys.readouterr().out
    assert code == 1
    assert "HTTP 401" in out and '"result": "unauthorized"' in out
    assert db_one(ctx, "SELECT COUNT(*) AS n FROM webhook_events")["n"] == 0


def test_send_lead_cli_without_any_key_says_so_and_is_rejected(client, clean_env, capsys):
    # The server under test requires a key; the tool must make the reason easy to see.
    code = send_lead.main(["--email", "you@example.com"], client=client)

    captured = capsys.readouterr()
    assert code == 1
    assert "no API key found" in captured.err
    assert "HTTP 401" in captured.out


def test_send_lead_cli_posts_to_a_tenant_keyed_url_without_revealing_the_key(client, ctx, clean_env, capsys):
    # /webhooks/angi/leads/<key> routes by the URL alone; the key is a secret, so it is not echoed.
    webhook_key = db_one(ctx, "SELECT webhook_key FROM tenants WHERE id = 'tnt_sparkle'")["webhook_key"]

    code = send_lead.main(["--tenant-key", webhook_key, "--email", "you@example.com"], client=client)

    captured = capsys.readouterr()
    assert code == 0
    assert '"routedBy": "url_key"' in captured.out
    assert webhook_key not in captured.out and webhook_key not in captured.err
    assert "/webhooks/angi/leads/<tenant key>" in captured.out
    assert db_one(ctx, "SELECT tenant_id FROM leads")["tenant_id"] == "tnt_sparkle"


def test_send_lead_cli_unknown_tenant_key_fails(client, clean_env, capsys):
    # A wrong URL key is a wrong credential.
    code = send_lead.main(["--tenant-key", "not-a-real-key", "--api-key", API_KEY], client=client)

    assert code == 1
    assert "HTTP 401" in capsys.readouterr().out


def test_send_lead_cli_raw_sends_the_malformed_sample_and_it_is_processed(client, ctx, clean_env, capsys):
    # The verbatim spec example goes over the wire byte for byte.
    code = send_lead.main(["--api-key", API_KEY, "--sample", "malformed_spec_example", "--raw"], client=client)

    captured = capsys.readouterr()
    assert code == 0
    assert '"result": "processed"' in captured.out and '"parseMode": "tolerant"' in captured.out
    assert captured.err == ""  # its address is on example.com: nothing to warn about, nothing refused
    stored = db_one(ctx, "SELECT raw_body FROM webhook_events")["raw_body"]
    assert stored == sample_bytes("malformed_spec_example.txt").decode("utf-8")


def test_send_lead_cli_refuses_impossible_requests_before_sending_anything(client, ctx, clean_env, capsys):
    # A typo in the command must not turn into a lead in the database.
    assert send_lead.main(["--api-key", API_KEY, "--sample", "malformed_spec_example", "--raw", "--email", "a@example.com"], client=client) == 2
    assert send_lead.main(["--api-key", API_KEY, "--sample", "no_such_sample"], client=client) == 2
    assert send_lead.main(["--api-key", API_KEY, "--sample", "unknown_format", "--email", "a@example.com"], client=client) == 2

    assert "error:" in capsys.readouterr().err
    assert db_one(ctx, "SELECT COUNT(*) AS n FROM webhook_events")["n"] == 0
    with pytest.raises(SystemExit):
        send_lead.main(["--repeat", "0"], client=client)


def test_send_lead_cli_refuses_to_email_an_address_that_is_not_yours(client, ctx, clean_env, capsys):
    # Angi's example as printed carries a gmail.com address that may be somebody's mailbox.
    # A payload with such an address is not sent at all (exit 2), in any of the ways it could go out.
    as_printed = clean_env / "as_printed.json"
    as_printed.write_text(sample_bytes("angi_ads_spec_example.json").decode("utf-8").replace("@example.com", "@gmail.com"))
    not_json = clean_env / "as_printed.txt"
    not_json.write_bytes(sample_bytes("malformed_spec_example.txt").replace(b"@example.com", b"@gmail.com"))

    for arguments in (
        ["--sample", str(as_printed)],
        ["--sample", str(as_printed), "--same-id", "--repeat", "3"],
        ["--sample", str(as_printed), "--raw"],
        ["--sample", str(not_json), "--raw"],
        ["--sample", str(not_json)],
    ):
        assert send_lead.main(["--api-key", API_KEY, *arguments], client=client) == 2, arguments
        captured = capsys.readouterr()
        assert "bob.builder@gmail.com" in captured.err and "Nothing was sent" in captured.err
        assert "--email" in captured.err and captured.out == ""
    assert db_one(ctx, "SELECT COUNT(*) AS n FROM webhook_events")["n"] == 0


def test_send_lead_cli_sends_to_a_real_address_only_when_it_was_given_with_email(client, ctx, clean_env, capsys):
    # --email is the person running the tool saying "this one is mine": it replaces the
    # sample's address and is the only non-example address a payload may carry.
    as_printed = clean_env / "as_printed.json"
    as_printed.write_text(sample_bytes("angi_ads_spec_example.json").decode("utf-8").replace("@example.com", "@gmail.com"))

    assert send_lead.main(["--api-key", API_KEY, "--sample", str(as_printed), "--email", "Me@My-Own-Domain.org"], client=client) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert db_one(ctx, "SELECT email_normalized FROM leads")["email_normalized"] == "me@my-own-domain.org"
    assert db_one(ctx, "SELECT COUNT(*) AS n FROM webhook_events WHERE raw_body LIKE '%gmail.com%'")["n"] == 0

    # A second real-looking address elsewhere in the payload is still a stranger's.
    with_another = json.loads(as_printed.read_text())
    with_another["Description"] = "Call me or write to my husband, someone.else@his-employer.com"
    as_printed.write_text(json.dumps(with_another))
    assert send_lead.main(["--api-key", API_KEY, "--sample", str(as_printed), "--email", "me@my-own-domain.org"], client=client) == 2
    assert "someone.else@his-employer.com" in capsys.readouterr().err
    assert db_one(ctx, "SELECT COUNT(*) AS n FROM webhook_events")["n"] == 1


@pytest.mark.parametrize("name", SAMPLE_FILES)
def test_send_lead_cli_sends_every_committed_sample_without_complaint(client, clean_env, capsys, name):
    # None of the committed samples trips the stranger check: each can be sent as it is.
    arguments = ["--raw"] if name.endswith(".txt") else []

    assert send_lead.main(["--api-key", API_KEY, "--sample", name, *arguments], client=client) == 0
    assert capsys.readouterr().err == ""


def test_send_lead_cli_reports_an_unreachable_server_without_a_traceback(clean_env, capsys):
    # The most common demo failure: the server or the tunnel is not running.
    code = send_lead.main(["--api-key", API_KEY, "--email", "you@example.com"], client=UnreachableService())

    assert code == 1
    assert "could not reach http://localhost:8000" in capsys.readouterr().err


def test_same_id_resend_with_a_corrected_email_sends_exactly_one_intro(client, ctx, mailer, clock, clean_env, capsys):
    # "Never a second intro": Angi corrects the address of a lead whose first email is still queued.
    send_lead.main(["--api-key", API_KEY, "--same-id", "--email", "typo@example.com"], client=client)
    send_lead.main(["--api-key", API_KEY, "--same-id", "--email", "right@example.com"], client=client)
    clock.advance(seconds=1)
    ctx.worker.deliver_due()

    assert db_one(ctx, "SELECT COUNT(*) AS n FROM leads")["n"] == 1
    assert [email.to for email in mailer.sent] == ["right@example.com"]


# =========================================================================================
# scripts/canary.py
# =========================================================================================


def test_canary_passes_when_the_lead_is_stored_and_its_email_leaves(client, ctx, mailer, clock):
    # The probe proves the whole path: webhook -> canary tenant -> intro email handed to the provider.
    worker = WorkerTime(ctx, clock)

    result = canary.run_canary(
        client, email="probe@example.com", api_key=API_KEY, admin_token=ADMIN_TOKEN,
        sleep=worker.sleep, monotonic=worker.monotonic,
    )

    assert list(result) == ["ok", "webhook_ms", "email_ms", "lead_id", "error"]
    assert result["ok"] is True and result["error"] is None
    assert result["webhook_ms"] >= 0
    assert result["email_ms"] == 1000.0  # the worker "ran" one second after the lead arrived
    assert worker.sleeps == 1
    lead = db_one(ctx, "SELECT * FROM leads WHERE id = ?", (result["lead_id"],))
    assert lead["tenant_id"] == "tnt_canary" and lead["external_account_id"] == "999999"
    assert lead["first_contacted_at"] is not None
    assert [email.to for email in mailer.sent] == ["probe@example.com"]
    assert mailer.sent[0].from_name == "Synthetic Canary Co."


def test_canary_leads_stay_out_of_the_business_analytics(client, ctx, clock):
    # The canary tenant is flagged is_test, so probes never inflate lead counts or spend.
    worker = WorkerTime(ctx, clock)
    canary.run_canary(client, email="probe@example.com", api_key=API_KEY, admin_token=ADMIN_TOKEN,
                      sleep=worker.sleep, monotonic=worker.monotonic)

    summary = client.get("/admin/api/analytics/summary", headers=ADMIN).json()

    assert summary["funnel"]["received"] == 0
    assert db_one(ctx, "SELECT is_test FROM tenants WHERE id = 'tnt_canary'")["is_test"] == 1


def test_every_canary_run_is_a_new_lead(client, ctx, clock):
    # A unique CorrelationId per run: a redelivery would not exercise the email path.
    worker = WorkerTime(ctx, clock)
    results = []
    for _ in range(3):
        results.append(canary.run_canary(client, email="probe@example.com", api_key=API_KEY, admin_token=ADMIN_TOKEN,
                                         sleep=worker.sleep, monotonic=worker.monotonic))
        clock.advance(minutes=5)

    assert [r["ok"] for r in results] == [True, True, True]
    assert len({r["lead_id"] for r in results}) == 3
    assert db_one(ctx, "SELECT COUNT(DISTINCT external_lead_id) AS n FROM leads")["n"] == 3


def test_canary_runs_do_not_fill_the_credit_worklist(client, ctx, clock):
    # A probe that runs every five minutes must not create 288 "ready to file" credit claims a day.
    worker = WorkerTime(ctx, clock)
    for _ in range(3):
        canary.run_canary(client, email="probe@example.com", api_key=API_KEY, admin_token=ADMIN_TOKEN,
                          sleep=worker.sleep, monotonic=worker.monotonic)
        clock.advance(minutes=5)

    claims = client.get("/admin/api/claims", headers=ADMIN).json()

    assert claims["summary"]["open_count"] == 0


def test_canary_runs_are_never_flagged_as_duplicate_leads(client, ctx, mailer, clock):
    # Every run is the same synthetic consumer by design. Leads of the test tenant are not
    # checked for duplicates at all: no flag, no claim, and each run gets the real intro
    # email, so the probe keeps exercising the path a real first lead takes.
    worker = WorkerTime(ctx, clock)
    for _ in range(3):
        result = canary.run_canary(client, email="probe@example.com", api_key=API_KEY, admin_token=ADMIN_TOKEN,
                                   sleep=worker.sleep, monotonic=worker.monotonic)
        assert result["ok"] is True
        clock.advance(minutes=5)

    assert db_one(ctx, "SELECT COUNT(*) AS n FROM leads WHERE is_duplicate = 1")["n"] == 0
    assert db_one(ctx, "SELECT COUNT(*) AS n FROM credit_claims")["n"] == 0
    templates = db_rows(ctx, "SELECT template FROM outbox_messages WHERE sequence = 0 AND status = 'sent'")
    assert [row["template"] for row in templates] == ["intro"] * 3


def test_canary_fails_when_the_email_never_leaves(client, ctx, mailer, clock):
    # A stored lead with a dead worker is exactly the silent failure the canary exists to catch.
    worker = WorkerTime(ctx, clock, worker_runs=False)

    result = canary.run_canary(
        client, email="probe@example.com", api_key=API_KEY, admin_token=ADMIN_TOKEN,
        timeout=5, poll_interval=1.0, sleep=worker.sleep, monotonic=worker.monotonic,
    )

    assert result["ok"] is False
    assert result["error"] == "the intro email was not sent within 5s"
    assert result["lead_id"] is not None and result["email_ms"] is None and result["webhook_ms"] is not None
    assert worker.sleeps == 5  # polled once a second, then gave up
    assert mailer.sent == []


def test_canary_failure_names_the_send_error_when_the_provider_is_down(client, ctx, mailer, clock):
    # "Why" matters at 3 AM: surface the provider's error, not just a timeout.
    mailer.fail_with = ConnectionError("smtp.gmail.com refused the connection")
    mailer.fail_times = -1  # keep failing
    worker = WorkerTime(ctx, clock)

    result = canary.run_canary(
        client, email="probe@example.com", api_key=API_KEY, admin_token=ADMIN_TOKEN,
        timeout=3, sleep=worker.sleep, monotonic=worker.monotonic,
    )

    assert result["ok"] is False
    assert "was not sent within 3s" in result["error"]
    assert "smtp.gmail.com refused the connection" in result["error"]


def test_canary_fails_on_a_rejected_api_key(client, ctx, clock):
    # An expired or rotated key is "we are broken", even though the server is up.
    worker = WorkerTime(ctx, clock)

    result = canary.run_canary(client, email="probe@example.com", api_key="stale-key", admin_token=ADMIN_TOKEN,
                               sleep=worker.sleep, monotonic=worker.monotonic)

    assert result["ok"] is False and result["lead_id"] is None
    assert result["error"].startswith("webhook answered HTTP 401")
    assert worker.sleeps == 0


def test_canary_fails_when_the_lead_is_quarantined_instead_of_processed(client, ctx, clock):
    # HTTP 200 alone is not enough: "quarantined" means nobody was emailed.
    with ctx.db.transaction() as conn:
        conn.execute("UPDATE tenant_lead_sources SET is_active = 0 WHERE external_account_id = '999999'")
    worker = WorkerTime(ctx, clock)

    result = canary.run_canary(client, email="probe@example.com", api_key=API_KEY, admin_token=ADMIN_TOKEN,
                               sleep=worker.sleep, monotonic=worker.monotonic)

    assert result["ok"] is False and result["lead_id"] is None
    assert result["error"] == "webhook result was 'quarantined' (unmapped_account), expected 'processed'"


def test_canary_fails_on_a_wrong_admin_token(client, ctx, clock):
    # It cannot confirm the email without the admin API, so it must not report success.
    worker = WorkerTime(ctx, clock)

    result = canary.run_canary(client, email="probe@example.com", api_key=API_KEY, admin_token="wrong",
                               sleep=worker.sleep, monotonic=worker.monotonic)

    assert result["ok"] is False and result["lead_id"] is not None
    assert result["error"].startswith("admin API answered HTTP 401")


def test_canary_fails_cleanly_when_the_service_is_unreachable():
    # Tunnel down / DNS gone: a failure report, not a traceback.
    result = canary.run_canary(UnreachableService(), email="probe@example.com", api_key=API_KEY)

    assert result == {
        "ok": False, "webhook_ms": None, "email_ms": None, "lead_id": None,
        "error": "webhook request failed: ConnectError: connection refused",
    }


def test_canary_rejects_a_200_answer_that_is_not_a_success_body():
    # Something else answering on our URL (a parking page, a proxy) must not pass as us.
    class ParkingPage:
        def post(self, *args, **kwargs):
            return FakeResponse(200, "<html>It works!</html>")

    result = canary.run_canary(ParkingPage(), email="probe@example.com")

    assert result["ok"] is False
    assert result["error"].startswith("webhook answered HTTP 200 without status=success")


def test_canary_lead_is_an_angi_ads_lead_for_the_canary_account():
    # ALAccountId 999999 is what maps the probe to the is_test tenant.
    lead = canary.canary_lead("probe@example.com", "abc-123")

    assert lead["ALAccountId"] == "999999" == canary.CANARY_ACCOUNT_ID
    assert lead["Email"] == "probe@example.com" and lead["CorrelationId"] == "abc-123"
    assert send_lead.payload_format(lead) == "angi_ads"
    assert re.fullmatch(r"\d{3}55501\d{2}", lead["PhoneNumber"])


def test_canary_measures_time_between_two_server_timestamps():
    # email_ms is the service's own speed-to-lead for the canary lead.
    assert canary.milliseconds_between("2026-11-12T15:00:00.000Z", "2026-11-12T15:00:01.250Z") == 1250.0
    assert canary.milliseconds_between(None, "2026-11-12T15:00:01.250Z") is None
    assert canary.milliseconds_between("yesterday", "2026-11-12T15:00:01.250Z") is None


def test_canary_cli_prints_exactly_one_json_line_and_exits_zero_on_success(client, ctx, clock, clean_env, capsys, monkeypatch):
    # Monitoring parses the output; one line of JSON and the exit code are the whole interface.
    monkeypatch.setenv("ANGI_WEBHOOK_API_KEYS", f"{API_KEY},older-key")
    monkeypatch.setenv("ADMIN_TOKEN", ADMIN_TOKEN)
    slack_posts = []
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: slack_posts.append((args, kwargs)))

    code = canary.main(
        ["--email", "probe@example.com", "--slack-webhook", "https://hooks.slack.example/T000"],
        client=WorkerRunsAfterEachPost(client, ctx, clock),
    )

    captured = capsys.readouterr()
    assert code == 0
    lines = captured.out.splitlines()
    assert len(lines) == 1
    report = json.loads(lines[0])
    assert set(report) == {"ok", "webhook_ms", "email_ms", "lead_id", "error"}
    assert report["ok"] is True and report["error"] is None and report["email_ms"] == 1000.0
    assert slack_posts == []  # Slack hears about failures only
    assert API_KEY not in captured.out + captured.err and ADMIN_TOKEN not in captured.out + captured.err


def test_canary_cli_exits_one_and_tells_slack_on_failure(client, ctx, clock, clean_env, capsys, monkeypatch):
    # A failing probe must be loud: exit code 1 for cron/monitoring, a message for humans.
    slack_posts = []

    def fake_slack(url, **kwargs):
        slack_posts.append((url, kwargs["json"]["text"]))
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_slack)

    code = canary.main(
        ["--email", "probe@example.com", "--api-key", "stale-key", "--admin-token", ADMIN_TOKEN,
         "--url", "https://leads.example.test/", "--slack-webhook", "https://hooks.slack.example/T000"],
        client=client,
    )

    lines = capsys.readouterr().out.splitlines()
    assert code == 1
    assert len(lines) == 1
    report = json.loads(lines[0])
    assert report["ok"] is False and "HTTP 401" in report["error"]
    assert len(slack_posts) == 1
    url, text = slack_posts[0]
    assert url == "https://hooks.slack.example/T000"
    assert "FAILED" in text and "https://leads.example.test" in text and "HTTP 401" in text


def test_canary_cli_still_reports_when_slack_itself_is_down(client, clean_env, capsys, monkeypatch):
    # The notification is best effort; it must not swallow the report or change the exit code.
    def slack_down(url, **kwargs):
        raise httpx.ConnectError("slack unreachable")

    monkeypatch.setattr(httpx, "post", slack_down)

    code = canary.main(
        ["--email", "probe@example.com", "--api-key", "stale-key", "--slack-webhook", "https://hooks.slack.example/T000"],
        client=client,
    )

    captured = capsys.readouterr()
    assert code == 1
    assert len(captured.out.splitlines()) == 1 and json.loads(captured.out)["ok"] is False
    assert "could not post to Slack" in captured.err


def test_canary_cli_reports_a_crash_as_a_failed_probe(clean_env, capsys):
    # Whatever goes wrong, the contract is one JSON line and exit code 1.
    class Exploding:
        def post(self, *args, **kwargs):
            raise RuntimeError("boom")

    code = canary.main(["--email", "probe@example.com"], client=Exploding())

    out = capsys.readouterr().out
    assert code == 1
    assert json.loads(out) == {
        "ok": False, "webhook_ms": None, "email_ms": None, "lead_id": None,
        "error": "canary crashed: RuntimeError: boom",
    }


def test_canary_cli_requires_an_email_address(clean_env, capsys):
    # --email is where a real message lands when a real provider is configured; there is no safe default.
    with pytest.raises(SystemExit) as exit_info:
        canary.main([])

    assert exit_info.value.code == 2
    assert "--email" in capsys.readouterr().err


def test_canary_reads_its_settings_from_dotenv_when_not_exported(clean_env, monkeypatch):
    # Cron has a bare environment; ./.env is the usual home of these values.
    (clean_env / ".env").write_text("# demo\nADMIN_TOKEN='from-dotenv'\nPUBLIC_BASE_URL=\n")

    assert canary.setting("ADMIN_TOKEN") == "from-dotenv"
    assert canary.setting("PUBLIC_BASE_URL", "http://localhost:8000") == "http://localhost:8000"  # empty = unset
    assert canary.setting("ANGI_WEBHOOK_API_KEYS") == ""
    monkeypatch.setenv("ADMIN_TOKEN", "from-env")
    assert canary.setting("ADMIN_TOKEN") == "from-env"


# =========================================================================================
# scripts/simulate_history.py
# =========================================================================================


def sim_settings(**overrides: Any) -> Settings:
    """Settings for a simulation that do not depend on the developer's .env or environment."""
    values: dict[str, Any] = {
        "public_base_url": BASE_URL,
        "followup_delays_minutes": "60,1440",
        "duplicate_window_days": 45,
        "credit_request_window_days": 30,
        "outbox_max_attempts": 5,
        "email_sla_seconds": 120.0,
        "error_rate_threshold": 0.2,
        "error_rate_min_events": 3,
        "error_rate_window_minutes": 60,
        "silence_alert_minutes": 0,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def forbid_real_delivery(patch: pytest.MonkeyPatch) -> list[str]:
    """Make every real way out of the machine record the attempt and fail."""
    attempts: list[str] = []

    def refuse(kind: str):
        def call(*args, **kwargs):
            attempts.append(kind)
            raise AssertionError(f"the simulation tried to use {kind}")

        return call

    patch.setattr(smtplib, "SMTP", refuse("smtplib.SMTP"))
    patch.setattr(smtplib, "SMTP_SSL", refuse("smtplib.SMTP_SSL"))
    patch.setattr(httpx, "post", refuse("httpx.post"))
    return attempts


def sim_rows(database: str, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql, params)]
    finally:
        conn.close()


def sim_count(database: str, table_and_where: str) -> int:
    return sim_rows(database, f"SELECT COUNT(*) AS n FROM {table_and_where}")[0]["n"]


def copy_database(database: str, folder: Path) -> str:
    """A private copy, so a test that changes the simulated database cannot affect another."""
    for suffix in ("", "-wal", "-shm"):
        if Path(database + suffix).exists():
            shutil.copy(database + suffix, folder / f"leads.db{suffix}")
    return str(folder / "leads.db")


@pytest.fixture(scope="module")
def simulated(tmp_path_factory):
    """One small simulated history (40 leads, 14 days, ending at the test clock's START)."""
    database = str(tmp_path_factory.mktemp("simulated") / "leads.db")
    with pytest.MonkeyPatch.context() as patch:
        attempts = forbid_real_delivery(patch)
        result = simulate_history.simulate(
            database=database, leads=40, days=14, seed=7, now=START, settings=sim_settings()
        )
    result.real_delivery_attempts = attempts
    return result


def test_simulation_leaves_no_email_waiting_to_be_sent(simulated):
    # The safety property: the real server, started later on this database, has nothing to send.
    assert sim_count(simulated.database, "outbox_messages WHERE status IN ('pending', 'sending')") == 0
    assert simulated.summary["pending_outbox"] == 0
    leftovers = sim_rows(simulated.database, "SELECT status FROM outbox_messages WHERE last_error = 'simulation'")
    assert all(row["status"] == "cancelled" for row in leftovers)


def test_simulation_covers_the_three_demo_tenants_and_never_the_canary(simulated):
    # History is for the real demo tenants; the canary tenant is for the probe only.
    by_tenant = {
        row["tenant_id"]: row["n"]
        for row in sim_rows(simulated.database, "SELECT tenant_id, COUNT(*) AS n FROM leads GROUP BY tenant_id")
    }

    assert set(by_tenant) == {"tnt_sparkle", "tnt_summit", "tnt_blueline"}
    assert all(count >= 5 for count in by_tenant.values())
    assert sum(by_tenant.values()) == simulated.summary["leads"] == 39  # 40 arrivals, one quarantined
    assert simulated.summary["leads_by_tenant"] == by_tenant


def test_simulation_produces_a_plausible_funnel(simulated):
    # Emailed within seconds; some click, fewer book, some of those are won; values $150 - $9,000.
    db = simulated.database
    summary = simulated.summary

    assert summary["booked"] >= 1
    assert summary["leads"] > summary["contacted"] >= summary["clicked"] >= summary["booked"] >= summary["won"] >= 1
    assert summary["lost"] >= 1
    slow = sim_count(db, "leads WHERE first_contacted_at IS NOT NULL "
                         "AND (julianday(first_contacted_at) - julianday(received_at)) * 86400 > 15")
    assert slow == 0
    values = [row["job_value_cents"] for row in sim_rows(db, "SELECT job_value_cents FROM leads WHERE outcome = 'won'")]
    assert all(15_000 <= value <= 900_000 for value in values)
    assert summary["revenue_cents"] == sum(values)
    # Every booking is a real appointment with a confirmation email, made after the click.
    assert sim_count(db, "appointments WHERE status = 'scheduled'") == summary["booked"]
    assert sim_count(db, "outbox_messages WHERE template = 'booking_confirmation' AND status = 'sent'") == summary["booked"]
    assert sim_count(db, "leads WHERE booked_at IS NOT NULL AND booked_at < first_engaged_at") == 0
    assert sim_count(db, "leads WHERE first_engaged_at IS NOT NULL AND first_engaged_at < first_contacted_at") == 0
    assert sim_count(db, "leads WHERE outcome IS NOT NULL AND closed_at < received_at") == 0


def test_simulation_sends_mail_only_through_the_in_memory_provider(simulated):
    # Not one message may leave the machine: no SMTP connection, no HTTP call.
    db = simulated.database

    assert simulated.real_delivery_attempts == []
    assert {row["provider"] for row in sim_rows(db, "SELECT DISTINCT provider FROM outbox_messages WHERE status = 'sent'")} == {"memory"}
    assert len(simulated.mailer.sent) == sim_count(db, "outbox_messages WHERE status = 'sent'") > 0
    assert sim_count(db, "outbox_messages WHERE status = 'failed'") == 0


def test_simulation_uses_only_example_dot_com_addresses(simulated):
    # Even the in-memory messages are addressed to mailboxes that cannot exist.
    db = simulated.database

    assert all(email.to.endswith("@example.com") for email in simulated.mailer.sent)
    assert sim_count(db, "outbox_messages WHERE to_address NOT LIKE '%@example.com'") == 0
    assert sim_count(db, "leads WHERE email IS NOT NULL AND email NOT LIKE '%@example.com'") == 0
    assert sim_count(db, "webhook_events WHERE raw_body LIKE '%@gmail.com%'") == 0


def test_simulation_includes_duplicate_leads_with_credit_claims(simulated):
    # Same consumer, new lead, through the same Angi account (the one charged twice): flagged
    # and claimable.
    db = simulated.database
    duplicates = sim_rows(db, "SELECT * FROM leads WHERE is_duplicate = 1")

    assert len(duplicates) == simulated.summary["duplicates"] == 2
    assert sim_count(db, "credit_claims") == len(duplicates)
    for lead in duplicates:
        original = sim_rows(db, "SELECT * FROM leads WHERE id = ?", (lead["duplicate_of_lead_id"],))[0]
        assert original["tenant_id"] == lead["tenant_id"]
        assert original["external_account_id"] == lead["external_account_id"] is not None
        assert original["phone_normalized"] == lead["phone_normalized"]
        assert original["received_at"] < lead["received_at"]
        assert original["external_lead_id"] != lead["external_lead_id"]
    # One of the two wrote from another address of theirs (matched on the phone alone).
    matched = sorted(json.loads(lead["duplicate_matched_on"]) for lead in duplicates)
    assert matched == [["phone"], ["phone", "email"]]


def test_simulated_duplicate_gets_the_short_note_only_at_an_address_that_was_already_written_to(simulated):
    # The repeat request from the address the business already emailed is acknowledged with
    # the short "we got your new request" note and starts no second cadence. The one from an
    # address that never heard from the business gets the normal introduction and follow-ups.
    db = simulated.database
    first_touch = {}
    for lead in sim_rows(db, "SELECT * FROM leads WHERE is_duplicate = 1"):
        original = sim_rows(db, "SELECT email_normalized FROM leads WHERE id = ?", (lead["duplicate_of_lead_id"],))[0]
        same_address = original["email_normalized"] == lead["email_normalized"]
        sent = sim_rows(db, "SELECT template FROM outbox_messages WHERE lead_id = ? AND sequence = 0 "
                            "AND template != 'booking_confirmation'", (lead["id"],))
        follow_ups = sim_count(db, f"outbox_messages WHERE lead_id = '{lead['id']}' AND template = 'followup'")
        first_touch[same_address] = ([row["template"] for row in sent], follow_ups)

    assert first_touch[True] == (["repeat_request"], 0)
    assert first_touch[False] == (["intro"], 2)
    assert sim_count(db, "leads WHERE email LIKE '%.work@example.com'") == 1


def test_simulation_leaves_one_unmapped_lead_in_quarantine(simulated):
    # Something for the demo to fix live: add the mapping and watch the lead get processed.
    db = simulated.database
    quarantined = sim_rows(db, "SELECT * FROM webhook_events WHERE status = 'quarantined'")

    assert len(quarantined) == simulated.summary["quarantined"] == 1
    assert quarantined[0]["status_reason"] == "unmapped_account"
    assert json.loads(quarantined[0]["raw_body"])["ALAccountId"] == "000000"
    assert sim_count(db, "leads WHERE external_account_id = '000000'") == 0
    assert sim_count(db, "webhook_events WHERE status NOT IN ('processed', 'quarantined')") == 0


def test_simulation_includes_leads_without_email_and_one_drifted_payload(simulated):
    # The awkward cases a dashboard has to show: nothing to email, and a format that moved.
    db = simulated.database
    no_email = sim_rows(db, "SELECT * FROM leads WHERE email_normalized IS NULL")

    assert 1 <= len(no_email) <= 2
    assert all(lead["needs_review"] == 1 and lead["first_contacted_at"] is None for lead in no_email)
    assert sim_count(db, "lead_events WHERE type = 'email.skipped'") == len(no_email)
    drifted = sim_rows(db, "SELECT * FROM payload_shapes WHERE is_clean = 0")
    assert len(drifted) == 1 and drifted[0]["seen_count"] == 1 and drifted[0]["parse_mode"] == "tolerant"
    tolerant = sim_rows(db, "SELECT * FROM leads WHERE parse_mode = 'tolerant'")
    assert len(tolerant) == 1 and tolerant[0]["first_contacted_at"] is not None  # still answered


def test_simulation_uses_both_payload_formats(simulated):
    # Both Angi feeds show up in the analytics "by format" breakdown.
    formats = {
        row["source_format"]
        for row in sim_rows(simulated.database, "SELECT DISTINCT source_format FROM leads")
    }

    assert formats == {"angi_ads", "angi_leads"}
    # Sparkle only buys Angi Ads (it has no Angi Leads account).
    assert sim_count(simulated.database, "leads WHERE tenant_id = 'tnt_sparkle' AND source_format = 'angi_leads'") == 0


def test_simulated_history_lies_between_its_start_and_now(simulated):
    # History must not contain the future: nothing is stamped after "now" or before the first day.
    db = simulated.database
    start, end = simulate_history.iso(simulated.start), simulate_history.iso(simulated.end)

    assert simulated.end == START and simulated.end - simulated.start == timedelta(days=14)
    assert sim_count(db, f"leads WHERE received_at < '{start}' OR received_at > '{end}' OR updated_at > '{end}'") == 0
    assert sim_count(db, f"lead_events WHERE occurred_at < '{start}' OR occurred_at > '{end}'") == 0
    assert sim_count(db, f"outbox_messages WHERE sent_at > '{end}' OR created_at > '{end}'") == 0
    assert sim_count(db, f"webhook_events WHERE received_at > '{end}' OR processed_at > '{end}'") == 0
    assert sim_count(db, f"alerts WHERE first_seen_at > '{end}' OR last_seen_at > '{end}'") == 0
    assert sim_count(db, f"credit_claims WHERE created_at > '{end}' OR resolved_at > '{end}'") == 0
    assert sim_count(db, f"tenants WHERE created_at != '{start}'") == 0  # the tenants predate their leads


def test_simulation_leaves_alerts_as_they_fell(simulated):
    # Nothing is tidied up by the script. The lead in quarantine keeps its health check failing
    # (an open alert), and the alerts raised when the unmapped account and the drifted payload
    # arrived are on record, open or resolved by the application's own rules, never deleted.
    alerts = sim_rows(simulated.database, "SELECT kind, status FROM alerts")
    open_kinds = {row["kind"] for row in alerts if row["status"] == "open"}

    assert "quarantine" in open_kinds
    assert {"unmapped_account", "schema_drift", "quarantine", "email_send_error"} <= {row["kind"] for row in alerts}
    assert simulated.summary["open_alerts"] == sim_count(simulated.database, "alerts WHERE status = 'open'") >= 1


def test_summary_counts_match_the_database(simulated):
    # The printed summary is read back from what the application stored, not from the plan.
    db = simulated.database
    summary = simulated.summary

    assert summary["webhook_events"] == sim_count(db, "webhook_events") == 40
    assert summary["contacted"] == sim_count(db, "leads WHERE first_contacted_at IS NOT NULL")
    assert summary["clicked"] == sim_count(db, "lead_events WHERE type = 'booking.link_clicked'")
    assert summary["emails_sent"] == len(simulated.mailer.sent)
    text = simulate_history.format_summary(simulated, seed=7)
    assert "pending outbox  0" in text and "leads           39" in text and "quarantined     1" in text


def test_real_worker_started_on_a_simulated_database_emails_nobody(simulated, tmp_path):
    # The whole point of cancelling the outbox: the live server cannot email a simulated lead, now or later.
    database = copy_database(simulated.database, tmp_path)
    mailer = MemoryEmailProvider()
    clock = FakeClock(START)
    ctx = build_context(sim_settings(database_path=database, run_background_worker=False), email_provider=mailer, clock=clock)

    for hours in (0, 1, 26, 72, 24 * 40):
        clock.set(START + timedelta(hours=hours))
        assert ctx.worker.deliver_due() == 0
        ctx.worker.run_maintenance()

    assert mailer.sent == []
    assert db_one(ctx, "SELECT COUNT(*) AS n FROM outbox_messages WHERE status IN ('pending', 'sending')")["n"] == 0


def test_every_dashboard_page_renders_on_the_simulated_history(simulated, tmp_path):
    # What the demo opens right after "make simulate": every page, every filter and every
    # lead's own page must render from the simulated database, and the buttons the history
    # invites (discard the quarantined request, or map its account) must work on it.
    database = copy_database(simulated.database, tmp_path)
    settings = sim_settings(database_path=database, run_background_worker=False, admin_token=ADMIN_TOKEN,
                            angi_webhook_api_keys=API_KEY, seed_demo_data=False)
    ctx = build_context(settings, email_provider=MemoryEmailProvider(), clock=FakeClock(START))
    lead_ids = [row["id"] for row in db_rows(ctx, "SELECT id FROM leads")]
    pages = [
        "/dashboard", "/dashboard?days=7", "/dashboard?days=all&live=1", "/dashboard?tenant=tnt_summit&days=90",
        "/dashboard/leads", "/dashboard/leads?duplicates=1", "/dashboard/leads?review=1&status=new",
        "/dashboard/credits", "/dashboard/credits?status=submitted", "/dashboard/credits?tenant=tnt_blueline",
        "/dashboard/monitoring", "/dashboard/monitoring?status=quarantined&live=1", "/dashboard/tenants",
        *(f"/dashboard/leads/{lead_id}" for lead_id in lead_ids),
    ]
    with TestClient(create_app(context=ctx)) as browser:
        for path in pages:
            response = browser.get(path, headers=ADMIN)
            assert response.status_code == 200, path
            assert response.text.rstrip().endswith("</html>"), path
        overview = browser.get("/dashboard?days=all", headers=ADMIN).text
        assert f'<div class="tile-value">{len(lead_ids)}</div>' in overview and overview.count("<svg") == 2
        monitoring = browser.get("/dashboard/monitoring?status=quarantined", headers=ADMIN).text
        event_id = re.search(r'action="/dashboard/events/(evt_[0-9a-f]+)/discard"', monitoring).group(1)
        assert "Replay all quarantined (1)" in monitoring and "Its Angi account is not mapped to a tenant." in monitoring
        assert "Leads are waiting for a mapping." in browser.get("/dashboard/tenants", headers=ADMIN).text

        # Either way out of the quarantine works: here the account gets its mapping...
        mapped = browser.post("/dashboard/tenants/tnt_blueline/lead-sources", data={"external_account_id": "000000"},
                              headers=ADMIN, follow_redirects=False)
        assert mapped.headers["location"] == "/dashboard/tenants?msg=mapping_added&n=1&ok=1"
        assert db_one(ctx, "SELECT status FROM webhook_events WHERE id = ?", (event_id,))["status"] == "processed"
        # ...and a junk request that arrives later is discarded.
        junk = browser.post("/webhooks/angi/leads", content=b"ping", headers={"X-API-KEY": API_KEY}).json()["eventId"]
        discarded = browser.post(f"/dashboard/events/{junk}/discard", headers=ADMIN, follow_redirects=False)
        assert discarded.headers["location"] == "/dashboard/monitoring?msg=event_discarded"
        after = browser.get("/dashboard/monitoring", headers=ADMIN)
        assert after.status_code == 200 and '<span class="badge neutral">discarded</span>' in after.text
        new_lead = db_one(ctx, "SELECT id FROM leads WHERE external_account_id = '000000'")["id"]
        assert browser.get(f"/dashboard/leads/{new_lead}", headers=ADMIN).status_code == 200
    # The lead released from quarantine is real work for the live server: it is the only thing queued.
    queued = db_rows(ctx, "SELECT DISTINCT lead_id FROM outbox_messages WHERE status = 'pending'")
    assert [row["lead_id"] for row in queued] == [new_lead]


def test_simulation_refuses_a_database_that_already_has_leads(simulated, tmp_path):
    # Demo data must never be poured over existing leads by accident.
    database = copy_database(simulated.database, tmp_path)
    before = sim_count(database, "leads")

    with pytest.raises(simulate_history.SimulationRefused, match="already contains 39 lead"):
        simulate_history.simulate(database=database, leads=40, now=START, settings=sim_settings())

    assert sim_count(database, "leads") == before
    assert sim_count(database, "webhook_events") == 40


def test_force_adds_a_second_batch_and_still_leaves_nothing_pending(simulated, tmp_path):
    # --force is an explicit "yes, on top of what is there"; the safety property still holds.
    database = copy_database(simulated.database, tmp_path)

    result = simulate_history.simulate(database=database, leads=20, days=14, seed=8, force=True, now=START, settings=sim_settings())

    assert result.summary["leads"] > 39
    assert result.summary["pending_outbox"] == 0
    assert sim_count(database, "outbox_messages WHERE status IN ('pending', 'sending')") == 0
    assert all(email.to.endswith("@example.com") for email in result.mailer.sent)


def test_simulation_refuses_a_database_with_real_email_still_queued(client, ctx, mailer, settings):
    # Stepping the clock would "send" a real lead's queued email into thin air; even --force refuses.
    post_lead(client, ads_payload(Email="real.customer@example.org"))

    with pytest.raises(simulate_history.SimulationRefused, match="waiting to be sent"):
        simulate_history.simulate(database=settings.database_path, force=True, now=START, settings=sim_settings())

    waiting = db_rows(ctx, "SELECT to_address FROM outbox_messages WHERE status = 'pending'")
    assert len(waiting) == 3 and {row["to_address"] for row in waiting} == {"real.customer@example.org"}
    assert db_one(ctx, "SELECT COUNT(*) AS n FROM leads")["n"] == 1
    assert mailer.sent == []


def test_cancel_unsent_cancels_every_pending_message_with_reason_simulation(client, ctx, mailer, clock):
    # The last step of the simulation, on its own: intro and follow-ups all cancelled, none sendable.
    lead_id = post_lead(client, ads_payload(Email="pat@example.com")).json()["leadId"]

    cancelled = simulate_history.cancel_unsent(ctx, clock())

    assert cancelled == 3
    messages = db_rows(ctx, "SELECT status, last_error FROM outbox_messages WHERE lead_id = ?", (lead_id,))
    assert [(m["status"], m["last_error"]) for m in messages] == [("cancelled", "simulation")] * 3
    event = db_one(ctx, "SELECT data_json FROM lead_events WHERE lead_id = ? AND type = 'email.cancelled'", (lead_id,))
    assert json.loads(event["data_json"])["reason"] == "simulation"
    clock.advance(days=3)
    assert ctx.worker.deliver_due() == 0 and mailer.sent == []
    assert simulate_history.cancel_unsent(ctx, clock()) == 0  # nothing left; running it again is harmless


def test_same_seed_gives_the_same_history_and_another_seed_a_different_one(simulated, tmp_path):
    # --seed makes a demo repeatable: same people, same times, same outcomes.
    again = simulate_history.simulate(
        database=str(tmp_path / "again.db"), leads=40, days=14, seed=7, now=START, settings=sim_settings()
    )
    story = (
        "SELECT received_at, tenant_id, full_name, email, task_name, status, outcome, job_value_cents, is_duplicate, "
        "first_engaged_at IS NOT NULL AS clicked, booked_at IS NOT NULL AS booked "
        "FROM leads ORDER BY received_at, email"
    )

    assert sim_rows(again.database, story) == sim_rows(simulated.database, story)
    assert again.summary == simulated.summary

    start = START - timedelta(days=14)
    other = simulate_history.plan_leads(random.Random(8), 40, start, START)
    same = simulate_history.plan_leads(random.Random(7), 40, start, START)
    assert [lead.person.email for lead in other] != [lead.person.email for lead in same]


def test_plan_is_pure_and_complete():
    # The plan decides everything up front: N arrivals in time order, inside the window.
    start = START - timedelta(days=14)
    plan = simulate_history.plan_leads(random.Random(7), 160, start, START)
    plan_again = simulate_history.plan_leads(random.Random(7), 160, start, START)

    assert [(lead.at, lead.payload) for lead in plan] == [(lead.at, lead.payload) for lead in plan_again]
    assert len(plan) == 160
    assert [lead.at for lead in plan] == sorted(lead.at for lead in plan)
    assert all(start <= lead.at < START for lead in plan)
    repeats = [lead for lead in plan if lead.is_repeat]
    assert len(repeats) == 10  # about 6%
    # A repeat comes through the same feed (so the same Angi account) as the original, and
    # about one in four of them from another address of the same person.
    for repeat in repeats:
        original = next(lead for lead in plan if lead.person is repeat.person and not lead.is_repeat)
        assert ("leadOid" in repeat.payload) == ("leadOid" in original.payload)
        assert repeat.payload.get("ALAccountId") == original.payload.get("ALAccountId")
        assert repeat.payload.get("spEntityId") == original.payload.get("spEntityId")
    other_address = [lead for lead in repeats if (lead.payload.get("Email") or lead.payload.get("email")) != lead.person.email]
    assert len(other_address) == 3
    assert all((lead.payload.get("Email") or lead.payload.get("email")).endswith(".work@example.com") for lead in other_address)
    unmapped = [lead for lead in plan if lead.payload.get("ALAccountId") == "000000"]
    assert len(unmapped) == 1
    without_email = [lead for lead in plan if lead.payload.get("Email") == ""]
    assert len(without_email) == 2
    drifted = [lead for lead in plan if "EmailAddress" in lead.payload]
    assert len(drifted) == 1 and "Email" not in drifted[0].payload
    # Different people never share a phone number or an email address (that would be a false duplicate).
    people = {id(lead.person): lead.person for lead in plan}.values()
    assert len({person.email for person in people}) == len(people)
    assert len({(person.phone) for person in people}) == len(people)
    assert all(re.fullmatch(r"\d{3}55501\d{2}", person.phone) for person in people)
    assert all(person.email.endswith("@example.com") for person in people)
    # A repeat request comes from the same person as an earlier lead of the same tenant.
    for repeat in repeats:
        earlier = [lead for lead in plan if lead.person is repeat.person and not lead.is_repeat]
        assert len(earlier) == 1 and earlier[0].at < repeat.at and earlier[0].profile is repeat.profile


def test_simulated_consumers_never_share_a_phone_number_with_a_sample():
    # After "make simulate" the demo sends samples as they are (the redelivery in step 2 keeps
    # the sample's phone). If a simulated consumer of that tenant had the same number, the
    # sample would come back flagged as a repeat request from a stranger.
    start = START - timedelta(days=14)
    in_samples = set()
    for name in SAMPLE_FILES:
        for number in re.findall(r"\(?\d{3}\)?[ .-]?\d{3}[ .-]?\d{4}\b", sample_bytes(name).decode("utf-8")):
            in_samples.add(re.sub(r"\D", "", number))
    assert {"5125550163", "3035550147", "3175550126", "3175550184", "7205550119"} <= in_samples

    for seed in (3, 7, 8):
        plan = simulate_history.plan_leads(random.Random(seed), 400, start, START)
        assert not {lead.person.phone for lead in plan} & in_samples, seed
    assert in_samples >= simulate_history.SAMPLE_PHONES and len(simulate_history.SAMPLE_PHONES) == 10


def test_plan_follows_a_daily_and_weekly_rhythm():
    # Business hours are busier than nights, weekdays busier than weekends (in the tenant's own timezone).
    start = START - timedelta(days=28)
    plan = simulate_history.plan_leads(random.Random(7), 400, start, START)
    local = [lead.at.astimezone(ZoneInfo(lead.profile.timezone)) for lead in plan]

    business_hours = sum(1 for moment in local if 8 <= moment.hour < 18)
    night = sum(1 for moment in local if moment.hour < 6)
    weekend = sum(1 for moment in local if moment.weekday() >= 5)
    weekday = len(local) - weekend

    assert business_hours / len(local) > 0.55  # 10 of 24 hours carry well over half the leads
    assert night / len(local) < 0.10
    assert weekday / 5 > 1.5 * (weekend / 2)  # per day, a weekday sees clearly more than a weekend day


def test_plan_revenue_and_rates_are_in_the_intended_ranges():
    # Roughly 45% click and 30% book; a won job is worth between $150 and $9,000.
    start = START - timedelta(days=14)
    plan = simulate_history.plan_leads(random.Random(7), 400, start, START)
    emailed = [lead for lead in plan if not lead.is_repeat and lead.payload.get("Email") != ""
               and lead.payload.get("ALAccountId") != "000000"]

    clicks = sum(1 for lead in emailed if lead.clicks_after is not None)
    books = sum(1 for lead in emailed if lead.books_after is not None)
    won = [lead for lead in emailed if lead.closes_as == "won"]

    assert 0.38 <= clicks / len(emailed) <= 0.52
    assert 0.24 <= books / len(emailed) <= 0.36
    assert won and all(15_000 <= lead.job_value_cents <= 900_000 for lead in won)
    assert all(lead.books_after is not None for lead in won)  # nobody wins a job that was never booked
    assert any(lead.closes_as == "lost" for lead in emailed)


def test_simulation_switches_off_everything_that_could_reach_the_outside(tmp_path):
    # Whatever the server's .env says (Slack, alert emails, redirect, fallback), the simulation stays silent.
    risky = sim_settings(
        alert_slack_webhook_url="https://hooks.slack.example/T000",
        alert_email_to="oncall@example.org",
        email_redirect_all_to="developer@example.org",
        fallback_tenant_slug="sparkle-home-cleaning",
        email_provider="smtp",
        run_background_worker=True,
    )
    with pytest.MonkeyPatch.context() as patch:
        attempts = forbid_real_delivery(patch)
        result = simulate_history.simulate(
            database=str(tmp_path / "leads.db"), leads=12, days=2, seed=3, now=START, settings=risky
        )

    assert attempts == []
    recipients = {email.to for email in result.mailer.sent}
    assert recipients and all(address.endswith("@example.com") for address in recipients)  # no redirect, no alert mail
    assert result.summary["quarantined"] == 1  # the fallback tenant was not used
    assert result.summary["pending_outbox"] == 0
    assert risky.alert_slack_webhook_url and risky.email_redirect_all_to  # the caller's settings were not modified


def test_simulation_rejects_sizes_it_cannot_fill_with_fictional_people(tmp_path):
    # The 555-01xx range is small; refuse rather than reuse a phone number (a false duplicate).
    for leads in (0, 9, 401):
        with pytest.raises(simulate_history.SimulationRefused, match="--leads must be between 10 and 400"):
            simulate_history.simulate(database=str(tmp_path / "x.db"), leads=leads, now=START, settings=sim_settings())
    with pytest.raises(simulate_history.SimulationRefused, match="--days"):
        simulate_history.simulate(database=str(tmp_path / "x.db"), days=0, now=START, settings=sim_settings())

    assert not (tmp_path / "x.db").exists()


def test_simulate_cli_prints_a_summary_then_refuses_to_run_twice(clean_env, capsys):
    # The operator's view: a short summary ending in "pending outbox 0", and a clear refusal the second time.
    database = str(clean_env / "demo.db")
    arguments = ["--database", database, "--leads", "12", "--days", "2", "--seed", "3", "--now", "2026-11-12T15:00:00Z"]

    with pytest.MonkeyPatch.context() as patch:
        attempts = forbid_real_delivery(patch)
        first = simulate_history.main(arguments)
        first_output = capsys.readouterr()
        second = simulate_history.main(arguments)
        second_output = capsys.readouterr()

    assert attempts == []
    assert first == 0
    assert "Simulated 2 days of history ending 2026-11-12T15:00:00.000Z" in first_output.out
    assert "pending outbox  0" in first_output.out
    for label in ("leads", "booked", "won", "duplicates", "quarantined"):
        assert re.search(rf"^\s+{label}\s+\d+", first_output.out, re.MULTILINE), label
    assert second == 2
    assert "already contains" in second_output.err and "--force" in second_output.err
    assert second_output.out == ""
    assert simulate_history.main(["--database", database, "--now", "last tuesday"]) == 2
    assert "--now must be an ISO timestamp" in capsys.readouterr().err


# =========================================================================================
# The documents: README.md, docs/DESIGN.md, docs/DEMO.md, samples/README.md
# =========================================================================================

DOCUMENTS = ("README.md", "docs/DESIGN.md", "docs/DEMO.md", "samples/README.md")
REVIEWER = "reviewer@reviewers-company.com"
PRESENTER = "presenter@presenters-mail.com"


def document(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def shell_commands(name: str) -> list[str]:
    """The commands inside the ```bash blocks of a document, in order, comments removed."""
    commands: list[str] = []
    for block in re.findall(r"```bash\n(.*?)```", document(name), re.DOTALL):
        for line in block.replace("\\\n", " ").splitlines():
            line = re.sub(r"\s+#.*$", "", line).strip()
            if line and not line.startswith("#"):
                commands.append(line)
    return commands


def send_lead_arguments(command: str) -> list[str] | None:
    """What a documented command passes to scripts/send_lead.py (None if it is not one).
    ``make send`` is expanded the way the Makefile's recipe expands it."""
    command = command.replace("<reviewer's address>", REVIEWER).replace("<your address>", PRESENTER)
    words = shlex.split(command)
    if words[:2] == ["make", "send"]:
        variables = dict(word.split("=", 1) for word in words[2:])
        assert set(variables) <= {"EMAIL", "SAMPLE", "ARGS"}, command
        email = ["--email", variables["EMAIL"]] if variables.get("EMAIL") else []
        return ["--sample", variables.get("SAMPLE", "angi_ads_spec_example"), *email, *shlex.split(variables.get("ARGS", ""))]
    if words[:4] == ["uv", "run", "python", "scripts/send_lead.py"]:
        return words[4:]
    return None


class RealMailServer(MemoryEmailProvider):
    """Stands in for Gmail: what it accepts would really have been delivered."""

    delivers_externally = True


class Recorder:
    """The TestClient, remembering the JSON answer to every POST."""

    def __init__(self, client) -> None:
        self.client, self.answers = client, []

    def post(self, *args, **kwargs):
        response = self.client.post(*args, **kwargs)
        self.answers.append(response.json())
        return response


@pytest.mark.parametrize("name", DOCUMENTS)
def test_documents_name_only_commands_samples_and_settings_that_exist(name):
    # A document that tells the reader to run something that is not there is worse than none.
    text = document(name)
    options = set(re.findall(r"--[a-z][a-z-]+", " ".join(
        line for line in text.splitlines() if "send_lead" in line or "ARGS=" in line)))
    assert options <= {"--sample", "--email", "--same-id", "--same-consumer", "--repeat", "--raw", "--help", "--phone",
                       "--first-name", "--last-name", "--account", "--tenant-key", "--url", "--api-key"}
    send_lead.parse_args([option for option in options if option in ("--same-id", "--same-consumer", "--raw")])
    make_commands = re.findall(r"`make ([a-z][a-z-]*)", text) + [
        word for command in shell_commands(name) for word in re.findall(r"\bmake ([a-z][a-z-]*)", command)
    ]
    for target in make_commands:
        assert target in MAKE_TARGETS, target
    for sample in re.findall(r"(?:SAMPLE=|--sample )([a-z_]+)", text):
        assert send_lead.resolve_sample(sample).is_file(), sample
    for file_name in re.findall(r"`([a-z_]+\.(?:json|txt))`", text):
        assert (SAMPLES / file_name).is_file(), file_name
    settings_named = set(re.findall(r"`([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)`", text))
    assert settings_named - {field.upper() for field in Settings.model_fields} == set()
    # Nothing a reader could copy would email a possibly real person, and nothing refers to
    # the variable that never existed (the setting is ANGI_WEBHOOK_API_KEYS, a list).
    assert not re.findall(r"[\w.+-]+@(?!example\.com)[\w-]+\.[a-z]{2,}", text), name
    assert "$ANGI_WEBHOOK_API_KEY" not in text and "malformed_spec_verbatim" not in text
    assert not re.search(r"^#+ .*(Q&A|Questions and answers)", text, re.MULTILINE | re.IGNORECASE)


def test_endpoints_named_in_the_documents_exist(client):
    # "POST /admin/api/events/{id}/discard" in a document must be a route of the application.
    routes = {
        (method.upper(), re.sub(r"\{\w+\}", "{}", path))
        for path, operations in client.app.openapi()["paths"].items() for method in operations
    }
    named = set()
    for name in DOCUMENTS:
        for method, path in re.findall(r"`(GET|POST) (/[\w/{}.-]+)", document(name)):
            named.add((method, re.sub(r"\{\w+\}", "{}", path)))
        for path in re.findall(r"`(/(?:healthz|readyz|metrics))`", document(name)):
            named.add(("GET", path))
    assert len(named) >= 12
    assert sorted(named - routes) == []


def test_readme_curl_example_works_as_printed_and_gets_the_documented_answer(client, ctx):
    # The first thing a reviewer does is copy this command. It must be valid, be authenticated
    # by nothing but the key (no shell variable that may not exist), and get the answer shown.
    text = document("README.md")
    block = re.search(r"```bash\n(curl .*?)```", text, re.DOTALL).group(1)
    documented = json.loads(re.search(r"```json\n(.*?)```", text, re.DOTALL).group(1))
    arguments = shlex.split(block.replace("\\\n", " "))
    headers = dict(header.split(": ", 1) for flag, header in zip(arguments, arguments[1:], strict=False) if flag == "-H")
    body = arguments[arguments.index("-d") + 1]

    assert "$" not in block
    assert arguments[arguments.index("-X") + 1] == "POST"
    assert arguments[3] == "http://localhost:8000/webhooks/angi/leads"
    assert headers == {"X-API-KEY": "<your key>", "Content-Type": "application/json"}
    assert send_lead.real_looking_addresses(body.encode()) == []  # copied as it is, it emails nobody

    response = client.post("/webhooks/angi/leads", content=body.encode("utf-8"),
                           headers={**headers, "X-API-KEY": API_KEY})

    assert response.status_code == 200
    answer = response.json()
    assert list(answer) == list(documented)  # the same fields, in the same order
    for key in ("eventId", "leadId"):
        assert re.fullmatch(r"[a-z]+_[0-9a-f]{16}", answer.pop(key)) and re.fullmatch(r"[a-z]+_[0-9a-f]{16}", documented.pop(key))
    assert answer == documented
    # The same body again, as the text under the example says.
    again = client.post("/webhooks/angi/leads", content=body.encode("utf-8"), headers={**headers, "X-API-KEY": API_KEY})
    assert again.json()["result"] == "duplicate_delivery"
    # And with the placeholder key left in, it is refused rather than accepted by accident.
    assert client.post("/webhooks/angi/leads", content=body.encode("utf-8"), headers=headers).status_code == 401


def test_demo_commands_are_the_ones_the_walkthrough_test_runs():
    # The next test runs the walkthrough step by step. If DEMO.md changes, this list (and the
    # expectations there) must change with it.
    commands = shell_commands("docs/DEMO.md")

    assert [send_lead_arguments(command) for command in commands if send_lead_arguments(command)] == [
        ["--sample", "angi_leads_example", "--email", PRESENTER],
        ["--sample", "angi_ads_spec_example", "--email", REVIEWER],
        ["--sample", "angi_ads_plumbing", "--email", REVIEWER, "--same-id", "--repeat", "2"],
        ["--sample", "angi_ads_emergency_hvac", "--email", REVIEWER],
        ["--sample", "unmapped_account", "--email", REVIEWER],
        ["--sample", "missing_account_id"],
        ["--sample", "drift_renamed_and_recased", "--email", PRESENTER],
        ["--sample", "malformed_spec_example", "--raw"],
        ["--sample", "unrecognised"],
        ["--sample", "angi_ads_spec_example", "--email", REVIEWER],
    ]
    others = [command for command in commands if send_lead_arguments(command) is None]
    assert others == ["make test", "make reset-db && make simulate", "make run", "make tunnel",
                      f"make canary EMAIL={PRESENTER}".replace(PRESENTER, "<your address>")]


def test_demo_walkthrough_in_order_does_what_the_document_says(simulated, tmp_path, clean_env):
    # docs/DEMO.md, command by command, on a simulated history, against a mail server that
    # really delivers. Each step must answer what the document says it answers; no two steps
    # may land on one lead; and only the reviewer and the presenter may receive mail.
    database = copy_database(simulated.database, tmp_path)
    settings = sim_settings(database_path=database, run_background_worker=False, admin_token=ADMIN_TOKEN,
                            angi_webhook_api_keys=API_KEY, seed_demo_data=False)
    mailer = RealMailServer()
    clock = FakeClock(START)
    ctx = build_context(settings, email_provider=mailer, clock=clock)
    steps = [arguments for command in shell_commands("docs/DEMO.md") if (arguments := send_lead_arguments(command))]
    leads_before = db_one(ctx, "SELECT COUNT(*) AS n FROM leads")["n"]

    with TestClient(create_app(context=ctx)) as browser:
        recorder = Recorder(browser)

        def run(step: int) -> list[dict[str, Any]]:
            """Send the step's lead(s), let a minute pass and the worker run. -> the answers."""
            recorder.answers.clear()
            assert send_lead.main(["--api-key", API_KEY, *steps[step]], client=recorder) == 0, steps[step]
            clock.advance(minutes=1)
            ctx.worker.deliver_due()
            return list(recorder.answers)

        def dashboard_post(path: str, **form: str) -> str:
            response = browser.post(path, data=form, headers=ADMIN, follow_redirects=False)
            assert response.status_code == 303, path
            clock.advance(minutes=1)
            ctx.worker.deliver_due()
            return response.headers["location"]

        # Before the demo: one lead to yourself.
        (preflight,) = run(0)
        assert (preflight["result"], preflight["tenant"]["id"], preflight["duplicate"]) == ("processed", "tnt_summit", None)
        assert [email.to for email in mailer.sent] == [PRESENTER]

        # 1. The happy path, down to the booking.
        (happy,) = run(1)
        assert (happy["result"], happy["tenant"]["id"], happy["tenant"]["routedBy"]) == ("processed", "tnt_sparkle", "account_id")
        assert happy["duplicate"] is None and happy["email"]["status"] == "queued" and happy["email"]["template"] == "intro"
        intro = mailer.sent[-1]
        assert intro.to == REVIEWER and "House Cleaning" in intro.subject and intro.text.count("  - ") >= 3
        link = re.search(r"/book/[\w-]+\?slot=[^\s\"]+", intro.html).group(0).replace("&amp;", "&")
        page = browser.get(link)
        slot = re.search(r'name="slot" value="([^"]+)"[^>]*checked', page.text).group(1)  # the window is pre-selected
        token = link.split("/book/")[1].split("?")[0]
        assert browser.post(f"/book/{token}", data={"slot": slot}, follow_redirects=False).status_code == 303
        ctx.worker.deliver_due()
        assert mailer.sent[-1].to == REVIEWER and mailer.sent[-1].subject.startswith("You're booked")
        timeline = browser.get(f"/dashboard/leads/{happy['leadId']}", headers=ADMIN).text
        for label in ("Lead received from Angi", "Email queued", "Email sent", "seconds since received",
                      "Booking link opened", "Appointment booked"):
            assert label in timeline, label

        # 2. The same lead id twice.
        first, second = run(2)
        assert (first["result"], first["tenant"]["name"], first["duplicate"]) == ("processed", "BlueLine Plumbing", None)
        assert second["result"] == "duplicate_delivery" and second["leadId"] == first["leadId"]
        assert sum(1 for email in mailer.sent if email.from_name == "BlueLine Plumbing") == 1

        # 3. Another tenant; an unmapped account, fixed on the Tenants page; a lead with no account id.
        (hvac,) = run(3)
        assert (hvac["result"], hvac["tenant"]["name"], hvac["duplicate"]) == ("processed", "Summit Heating & Air", None)
        (unmapped,) = run(4)
        assert (unmapped["result"], unmapped["reason"]) == ("quarantined", "unmapped_account")
        monitoring = browser.get("/dashboard/monitoring", headers=ADMIN).text
        assert "Lead quarantined: Angi account 000000" in monitoring and '<span class="badge critical">critical</span>' in monitoring
        assert '<span class="mono">000000</span>: 2 lead(s) waiting' in browser.get("/dashboard/tenants", headers=ADMIN).text
        mapped = dashboard_post("/dashboard/tenants/tnt_blueline/lead-sources", external_account_id="000000")
        assert mapped == "/dashboard/tenants?msg=mapping_added&n=2&ok=2"
        released = db_one(ctx, "SELECT l.* FROM leads l JOIN webhook_events e ON e.lead_id = l.id WHERE e.id = ?", (unmapped["eventId"],))
        assert (released["tenant_id"], released["email_normalized"], released["is_duplicate"]) == ("tnt_blueline", REVIEWER, 0)
        assert (mailer.sent[-1].to, mailer.sent[-1].from_name) == (REVIEWER, "BlueLine Plumbing")
        assert mailer.sent[-1].subject.startswith("Your Drain Clearing request")  # the introduction, not a repeat note

        (no_account,) = run(5)
        assert (no_account["result"], no_account["reason"]) == ("quarantined", "no_account_id")
        row = browser.get("/dashboard/monitoring?status=quarantined", headers=ADMIN).text
        assert f'action="/dashboard/events/{no_account["eventId"]}/assign"' in row and "Assign and replay" in row
        assigned = dashboard_post(f"/dashboard/events/{no_account['eventId']}/assign", tenant_id="tnt_summit")
        assert assigned == "/dashboard/monitoring?msg=replay_processed"
        tessa = db_one(ctx, "SELECT tenant_id, is_duplicate FROM leads WHERE email_normalized = 'tessa.morgan@example.com'")
        assert tessa == {"tenant_id": "tnt_summit", "is_duplicate": 0}

        # 4. Format drift; Angi's own broken example; a heartbeat that is discarded.
        (drifted,) = run(6)
        assert (drifted["result"], drifted["parseMode"], drifted["duplicate"]) == ("processed", "tolerant", None)
        assert any("recovered by alias" in warning and "emailAddress" in warning for warning in drifted["warnings"])
        assert mailer.sent[-1].to == PRESENTER
        assert "Angi payload format drift (angi_ads)" in browser.get("/dashboard/monitoring", headers=ADMIN).text
        (salvaged,) = run(7)
        assert (salvaged["result"], salvaged["parseMode"], salvaged["duplicate"]) == ("processed", "tolerant", None)
        assert any("malformed-json" in warning for warning in salvaged["warnings"])
        assert salvaged["leadId"] not in (happy["leadId"], drifted["leadId"])
        salvaged_page = browser.get(f"/dashboard/leads/{salvaged['leadId']}", headers=ADMIN).text
        assert "address is on a reserved example domain; nothing was sent" in salvaged_page
        assert 'action="/dashboard/messages/' in salvaged_page and "Retry</button>" in salvaged_page  # step 6 points at it
        (heartbeat,) = run(8)
        assert (heartbeat["result"], heartbeat["reason"]) == ("quarantined", "unrecognised_payload")
        row = browser.get("/dashboard/monitoring?status=quarantined", headers=ADMIN).text
        assert f'/dashboard/events/{heartbeat["eventId"]}/replay' in row and f'/dashboard/events/{heartbeat["eventId"]}/discard' in row
        assert dashboard_post(f"/dashboard/events/{heartbeat['eventId']}/discard") == "/dashboard/monitoring?msg=event_discarded"
        after_discard = browser.get("/dashboard/monitoring", headers=ADMIN).text
        assert "nothing in quarantine" in after_discard and "Replay all quarantined (0)" in after_discard
        assert "Preview" in browser.get("/dashboard/monitoring?status=discarded", headers=ADMIN).text

        # 5. The homeowner from step 1 again.
        (repeat,) = run(9)
        assert repeat["result"] == "processed" and repeat["leadId"] != happy["leadId"]
        assert repeat["duplicate"]["ofLeadId"] == happy["leadId"]
        assert (repeat["duplicate"]["matchedOn"], repeat["duplicate"]["confidence"]) == (["email"], "high")
        assert repeat["email"]["template"] == "repeat_request"
        assert (mailer.sent[-1].to, mailer.sent[-1].subject) == (REVIEWER, "We got your new request - Sparkle Home Cleaning")
        credits_page = browser.get("/dashboard/credits", headers=ADMIN).text
        assert f'id="claim-{repeat["duplicate"]["creditClaimId"]}"' in credits_page and "Export CSV" in credits_page

        # 6. The pages, and the canary.
        for path in ("/dashboard", "/dashboard?days=all", "/dashboard/monitoring", "/dashboard/leads", "/dashboard/tenants"):
            assert browser.get(path, headers=ADMIN).status_code == 200
        probe = canary.run_canary(WorkerRunsAfterEachPost(browser, ctx, clock), email=PRESENTER, api_key=API_KEY,
                                  admin_token=ADMIN_TOKEN, sleep=lambda seconds: None)
        assert probe["ok"] is True and probe["error"] is None

    # Ten leads sent, nine became leads of their own (the heartbeat was discarded), and none
    # of them was ever "corrected" by another step.
    assert db_one(ctx, "SELECT COUNT(*) AS n FROM leads")["n"] == leads_before + 9 + 1 + 1  # + simulated 000000 lead + canary
    assert db_one(ctx, "SELECT COUNT(*) AS n FROM lead_events WHERE type = 'lead.updated'")["n"] == 0
    assert db_one(ctx, "SELECT COUNT(*) AS n FROM leads WHERE is_duplicate = 1")["n"] == simulated.summary["duplicates"] + 1
    # Mail really left for two people only. Everything addressed to a sample's own
    # @example.com mailbox was stopped before the mail server.
    assert {email.to for email in mailer.sent} == {REVIEWER, PRESENTER}
    stopped = db_rows(ctx, "SELECT to_address, last_error FROM outbox_messages WHERE status = 'failed'")
    assert {row["to_address"] for row in stopped} >= {"bob.builder@example.com", "tessa.morgan@example.com"}
    assert all(row["to_address"].endswith("@example.com") for row in stopped)
    assert {row["last_error"] for row in stopped} == {"address is on a reserved example domain; nothing was sent"}


# =========================================================================================
# .env.example
# =========================================================================================


def env_example() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        assert separator, f"not a KEY=VALUE line: {line!r}"
        assert key not in values, f"{key} is listed twice"
        values[key] = value
    return values


def test_env_example_lists_exactly_the_settings_the_application_reads():
    # A variable the app ignores is a trap; a setting missing from the file is undocumented.
    documented = set(env_example())
    settings_fields = {name.upper() for name in Settings.model_fields}

    assert documented - settings_fields == set(), "unknown names in .env.example"
    assert settings_fields - documented == set(), "settings missing from .env.example"


def test_env_example_loads_and_carries_the_demo_defaults(clean_env):
    # Copying the file to .env must give a server that starts, set up for the Gmail + ngrok demo.
    loaded = Settings(_env_file=str(ROOT / ".env.example"))

    assert loaded.app_env == "development" and not loaded.is_production
    assert loaded.public_base_url == "https://your-name.ngrok-free.dev"
    assert loaded.email_provider == "smtp"
    assert (loaded.smtp_host, loaded.smtp_port, loaded.smtp_security) == ("smtp.gmail.com", 465, "ssl")
    assert loaded.fallback_tenant_slug == ""
    assert loaded.followup_delays == [60, 1440]
    assert loaded.email_redirect_all_to == "" and loaded.alert_slack_webhook_url == "" and loaded.alert_email_to == ""
    assert loaded.seed_demo_data is True and loaded.run_background_worker is True


def test_env_example_differs_from_the_code_defaults_only_where_the_demo_needs_it(clean_env):
    # Everything else documents the real default, so the file cannot drift from app/config.py unnoticed.
    loaded = Settings(_env_file=str(ROOT / ".env.example"))
    defaults = Settings(_env_file=None)
    demo_specific = {
        "public_base_url", "angi_webhook_api_keys", "admin_token", "email_provider",
        "smtp_username", "smtp_password",
    }

    different = {name for name in Settings.model_fields if getattr(loaded, name) != getattr(defaults, name)}

    assert different == demo_specific


def test_env_example_needs_no_separate_from_address(clean_env):
    # One value fewer to get wrong: with EMAIL_FROM_ADDRESS left empty, mail is sent from the
    # SMTP account, which is the only sender Gmail accepts anyway.
    values = env_example()
    loaded = Settings(_env_file=str(ROOT / ".env.example"))

    assert values["EMAIL_FROM_ADDRESS"] == ""
    assert loaded.sender_address == loaded.smtp_username == values["SMTP_USERNAME"]
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "Leave it empty to send from SMTP_USERNAME" in text


def test_env_example_documents_the_canary_schedule_and_leaves_it_off(clean_env):
    # CANARY_INTERVAL_MINUTES tells the service how often the probe is scheduled; off by
    # default, because nothing schedules it until somebody adds the cron line.
    values = env_example()
    text = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert values["CANARY_INTERVAL_MINUTES"] == "0"
    assert Settings(_env_file=str(ROOT / ".env.example")).canary_interval_minutes == 0
    assert "scripts/canary.py" in text and "three intervals" in text
    assert "CANARY_INTERVAL_MINUTES" in (ROOT / "scripts" / "canary.py").read_text(encoding="utf-8")


def test_env_example_contains_placeholders_not_secrets():
    # The file is committed: every credential in it must be an obvious placeholder (or empty).
    values = env_example()

    assert values["ANGI_WEBHOOK_API_KEYS"].startswith("change-me")
    assert values["ADMIN_TOKEN"].startswith("change-me")
    assert values["SMTP_PASSWORD"].startswith("your-")
    assert values["SMTP_USERNAME"].startswith("your-")
    assert values["RESEND_API_KEY"] == "" and values["ALERT_SLACK_WEBHOOK_URL"] == ""
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert text.count("SECRET") >= 5  # each credential is labelled
    assert not re.search(r"hooks\.slack\.com/services/\w", text) and "re_" not in values["RESEND_API_KEY"]


# =========================================================================================
# Makefile
# =========================================================================================


def make(*arguments: str, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["make", "-f", str(MAKEFILE), *arguments], cwd=cwd, env=env, capture_output=True, text=True, timeout=60
    )


def test_makefile_uses_tabs_and_declares_every_target_phony():
    # make rejects recipes indented with spaces, and a file named "test" must not shadow the target.
    lines = MAKEFILE.read_text(encoding="utf-8").splitlines()

    assert not [line for line in lines if line.startswith(" ")]
    assert sum(1 for line in lines if line.startswith("\t")) >= len(MAKE_TARGETS)
    phony = next(line for line in lines if line.startswith(".PHONY:"))
    assert set(phony.split(":", 1)[1].split()) == set(MAKE_TARGETS)
    for target in MAKE_TARGETS:
        assert any(re.match(rf"^{re.escape(target)}:.*## \S", line) for line in lines), f"{target} has no help text"


@needs_make
def test_make_help_lists_every_target(tmp_path):
    # "make" on its own is the documentation.
    for arguments in (("help",), ()):
        result = make(*arguments, cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        listed = [line.split()[0] for line in result.stdout.splitlines() if line.strip()]
        assert listed == list(MAKE_TARGETS)


@needs_make
def test_make_targets_run_the_documented_commands(tmp_path):
    # Dry run (make -n): the commands the targets would execute.
    def dry(*arguments: str) -> str:
        result = make("-n", *arguments, cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    serve = "uv run uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000"
    assert dry("install") == "uv sync"
    assert dry("run") == serve
    assert dry("dev") == f"{serve} --reload"
    assert dry("test") == "uv run pytest"
    assert dry("lint") == "uv run ruff check ."
    assert dry("simulate").split() == "uv run python scripts/simulate_history.py".split()
    assert dry("simulate", "ARGS=--leads 300").endswith("scripts/simulate_history.py --leads 300")
    assert dry("send").split() == "uv run python scripts/send_lead.py --sample angi_ads_spec_example".split()
    assert dry("send", "EMAIL=you@example.com", "SAMPLE=angi_leads_example", "ARGS=--repeat 2").split() == (
        "uv run python scripts/send_lead.py --sample angi_leads_example --email you@example.com --repeat 2".split()
    )
    assert "uv run python scripts/canary.py --email you@example.com" in dry("canary", "EMAIL=you@example.com")


@needs_make
def test_make_reset_db_announces_what_it_removes_before_removing_it(tmp_path):
    # Dry run only: the recipe lists data/leads.db* and data/outbox, then deletes exactly those.
    result = make("-n", "reset-db", cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    recipe = result.stdout
    assert "ls -d data/leads.db* data/outbox" in recipe
    assert recipe.index('echo "Removing:"') < recipe.index("rm -rf data/leads.db* data/outbox")
    removals = re.findall(r"rm -rf ([^;\n]+)", recipe)
    assert removals == ["data/leads.db* data/outbox"]  # nothing else, and no variable that could be empty
    assert "Nothing to remove" in recipe


@needs_make
def test_make_canary_without_an_address_explains_itself(tmp_path):
    # The canary sends a real email when a real provider is configured; there is no default recipient.
    result = make("canary", cwd=tmp_path)

    assert result.returncode != 0
    assert "usage: make canary EMAIL=<an address you own>" in result.stdout


@needs_make
def test_make_tunnel_uses_the_reserved_url_only_when_one_is_set(tmp_path):
    # NGROK_URL keeps the public address stable between runs; without it ngrok picks one.
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    ngrok = fake_bin / "ngrok"
    ngrok.write_text('#!/bin/sh\necho "ngrok $*"\n')  # stands in for the real ngrok binary
    ngrok.chmod(0o755)
    base_env = {"PATH": f"{fake_bin}:/usr/bin:/bin"}

    plain = make("tunnel", cwd=tmp_path, env=base_env)
    reserved = make("tunnel", cwd=tmp_path, env={**base_env, "NGROK_URL": "https://your-name.ngrok-free.dev"})
    on_command_line = make("tunnel", "NGROK_URL=https://other.ngrok-free.dev", cwd=tmp_path, env=base_env)

    assert plain.stdout.strip() == "ngrok http 8000"
    assert reserved.stdout.strip() == "ngrok http 8000 --url https://your-name.ngrok-free.dev"
    assert on_command_line.stdout.strip() == "ngrok http 8000 --url https://other.ngrok-free.dev"


def test_now_helper_used_by_the_tests_is_timezone_aware():
    # Guard for this file's own fixtures: the simulated "now" must be an aware UTC datetime.
    assert START.tzinfo is not None and START.utcoffset() == timedelta(0)
    assert datetime(2026, 11, 12, 15, 0, tzinfo=UTC) == START
