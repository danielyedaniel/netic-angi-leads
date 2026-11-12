"""Shared test fixtures.

Every test gets its own SQLite file, a controllable clock and an in-memory mail
provider, and drives the real application through FastAPI's TestClient. The background
worker thread is not started; tests call ``ctx.worker.deliver_due()`` /
``ctx.worker.run_maintenance()`` themselves so nothing depends on timing.
"""

from __future__ import annotations

import copy
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.context import AppContext, build_context
from app.mailer.providers import MemoryEmailProvider
from app.main import create_app

API_KEY = "test-angi-key-0123456789"  # 16+ characters: production refuses shorter secrets
ADMIN_TOKEN = "test-admin-token"
AUTH = {"X-API-KEY": API_KEY}
ADMIN = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
BASE_URL = "https://leads.example.test"

# Thursday 2026-11-12 15:00 UTC = 10:00 in Indianapolis, 08:00 in Denver, 09:00 in Austin.
START = datetime(2026, 11, 12, 15, 0, 0, tzinfo=UTC)

# Demo tenants seeded by app/seed.py (ALAccountId / spEntityId -> tenant id).
SPARKLE_ACCOUNT = "123456"  # -> tnt_sparkle, default fee $28.00
SUMMIT_ADS_ACCOUNT = "200001"  # -> tnt_summit, default fee $45.00
SUMMIT_LEADS_ACCOUNT = "12345678"  # -> tnt_summit (Angi Leads feed, fee comes in the payload)
BLUELINE_ACCOUNT = "200002"  # -> tnt_blueline, default fee $39.00
CANARY_ACCOUNT = "999999"  # -> tnt_canary (is_test)


class FakeClock:
    """A clock tests can move. Call it to get "now"."""

    def __init__(self, start: datetime = START) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> datetime:
        self.now += timedelta(**kwargs)
        return self.now

    def set(self, moment: datetime) -> None:
        self.now = moment


ANGI_ADS_EXAMPLE: dict[str, Any] = {
    "FirstName": "Bob",
    "LastName": "Builder",
    "PhoneNumber": "5554332646",
    "PostalAddress": {
        "AddressFirstLine": "123 Main St.",
        "AddressSecondLine": "",
        "City": "Indianapolis",
        "State": "IN",
        "PostalCode": "46203",
    },
    "Email": "bob.builder@gmail.com",
    "Source": "Angie's List Quote Request",
    "Description": "I'm Looking for recurring house cleaning services please.",
    "Category": "Indianapolis – House Cleaning",
    "Urgency": "This Week",
    "CorrelationId": "61a7de56-dba3-4e59-8e2a-3fa827f84f7f",
    "ALAccountId": SPARKLE_ACCOUNT,
}

ANGI_LEADS_EXAMPLE: dict[str, Any] = {
    "name": "Jane Doe",
    "firstName": "Jane",
    "lastName": "Doe",
    "address": "123 Main Street",
    "city": "Denver",
    "stateProvince": "CO",
    "postalCode": "80210",
    "primaryPhone": "3035550123",
    "phoneExt": "",
    "secondaryPhone": None,
    "secondaryPhoneExt": "",
    "email": "janedoe@example.com",
    "srOid": 123456789,
    "leadOid": 987654321,
    "fee": 25.67,
    "taskName": "Central A/C - Repair",
    "comments": "AC is blowing warm air.",
    "matchType": "Lead",
    "leadDescription": "Standard",
    "spEntityId": int(SUMMIT_LEADS_ACCOUNT),
    "spCompanyName": "Summit Heating & Air",
    "leadSource": "HomeAdvisor",
    "trustedFormUrl": "https://cert.trustedform.com/abc123",
    "primaryPhoneDetails": {"maskedNumber": False},
    "automatedContactCompliant": True,
    "interview": [
        {"question": "What kind of location is this?", "answer": "Home/Residence"},
        {"question": "When do you need this work done?", "answer": "Within 1 week"},
    ],
}


def ads_payload(**overrides: Any) -> dict[str, Any]:
    """The Angi Ads spec example with a fresh CorrelationId (so it is a new lead) and
    any top-level overrides applied. Pass ``PostalAddress={...}`` to replace the address."""
    payload = copy.deepcopy(ANGI_ADS_EXAMPLE)
    payload["CorrelationId"] = str(uuid.uuid4())
    payload.update(overrides)
    return payload


def leads_payload(**overrides: Any) -> dict[str, Any]:
    """The Angi Leads example with a fresh leadOid / srOid and any overrides applied."""
    payload = copy.deepcopy(ANGI_LEADS_EXAMPLE)
    unique = uuid.uuid4().int % 10**9
    payload["leadOid"] = unique
    payload["srOid"] = unique + 1
    payload.update(overrides)
    return payload


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def mailer() -> MemoryEmailProvider:
    return MemoryEmailProvider()


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        database_path=str(tmp_path / "leads.db"),
        public_base_url=BASE_URL,
        angi_webhook_api_keys=API_KEY,
        admin_token=ADMIN_TOKEN,
        run_background_worker=False,
        seed_demo_data=True,
        followup_delays_minutes="60,1440",
        email_from_address="leads@example.test",
        # Pinned explicitly so a variable exported in the developer's shell cannot
        # change what the tests exercise.
        app_env="development",
        email_provider="console",
        email_reply_to="",
        email_redirect_all_to="",
        fallback_tenant_slug="",
        alert_slack_webhook_url="",
        alert_email_to="",
        silence_alert_minutes=0,
    )


@pytest.fixture
def ctx(settings: Settings, mailer: MemoryEmailProvider, clock: FakeClock) -> AppContext:
    return build_context(settings, email_provider=mailer, clock=clock)


@pytest.fixture
def client(ctx: AppContext) -> Iterator[TestClient]:
    with TestClient(create_app(context=ctx)) as test_client:
        yield test_client


def post_lead(client: TestClient, payload: Any, headers: dict[str, str] | None = None, path: str = "/webhooks/angi/leads"):
    """POST a lead the way Angi does. ``payload`` may be a dict (sent as JSON) or raw bytes/str."""
    headers = AUTH if headers is None else headers
    if isinstance(payload, (bytes, str)):
        return client.post(path, content=payload, headers={"Content-Type": "application/json", **headers})
    return client.post(path, json=payload, headers=headers)


def db_rows(ctx: AppContext, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    with ctx.db.read() as conn:
        return [dict(row) for row in conn.execute(sql, params)]


def db_one(ctx: AppContext, sql: str, params: tuple = ()) -> dict[str, Any] | None:
    rows = db_rows(ctx, sql, params)
    return rows[0] if rows else None
