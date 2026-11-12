"""The operator API (app/routes/admin.py), the health endpoints (app/routes/health.py), the
admin side of authentication (app/security.py) and what the application does when it
starts (app/main.py, app/config.py): logging, the configuration check, demo data.

Everything is exercised over HTTP with the real application; data is created by posting
leads to the webhook and letting the worker run.
"""

from __future__ import annotations

import base64
import json
import logging
import re

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.context import build_context
from app.mailer.providers import PermanentEmailError
from app.main import RedactSecretsInAccessLog, check_configuration, create_app, redact_url
from app.util import iso
from tests.conftest import (
    ADMIN,
    ADMIN_TOKEN,
    API_KEY,
    AUTH,
    BLUELINE_ACCOUNT,
    CANARY_ACCOUNT,
    START,
    SUMMIT_ADS_ACCOUNT,
    ads_payload,
    db_one,
    db_rows,
    leads_payload,
    post_lead,
)

UNMAPPED_ACCOUNT = "000111"
JSON_HEADERS = {**ADMIN, "Content-Type": "application/json"}


def basic(username: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def send(client, payload, headers=None) -> dict:
    response = post_lead(client, payload, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def deliver(ctx, clock, seconds: float = 1) -> None:
    clock.advance(seconds=seconds)
    ctx.worker.deliver_due()


def get(client, path: str, **params):
    return client.get(path, params=params or None, headers=ADMIN)


def lead_ids(client, **params) -> list[str]:
    response = get(client, "/admin/api/leads", **params)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["count"] == len(body["leads"])
    return [lead["id"] for lead in body["leads"]]


@pytest.fixture
def probe(client) -> TestClient:
    """A second client on the same app that turns an unhandled server error into a 500
    response instead of re-raising it, so tests can assert on the status code."""
    return TestClient(client.app, raise_server_exceptions=False)


# --- authentication: every protected route ---------------------------------------------------


def walk_routes(routes, prefix: str = ""):
    """Yield (method, path) for every route, descending into included routers."""
    for route in routes:
        included = getattr(route, "original_router", None)
        if included is not None:
            extra = getattr(getattr(route, "include_context", None), "prefix", "") or ""
            yield from walk_routes(included.routes, prefix + extra)
            continue
        nested = getattr(route, "routes", None)
        path = getattr(route, "path", None)
        if nested is not None and path is not None and not getattr(route, "methods", None):
            yield from walk_routes(nested, prefix + path)  # a Mount / sub-application
            continue
        if path and getattr(route, "methods", None):
            for method in route.methods:
                yield method, prefix + path


def protected_routes(app) -> list[tuple[str, str]]:
    """Every (method, concrete path) under /admin/api and /dashboard that the app serves.
    Discovered at run time so that routes added later are covered automatically."""
    found = set(walk_routes(app.routes))
    for path, operations in app.openapi()["paths"].items():
        for method in operations:
            found.add((method.upper(), path))
    protected = set()
    for method, path in found:
        if method in ("HEAD", "OPTIONS"):
            continue
        if path.startswith("/admin/api") or path == "/dashboard" or path.startswith("/dashboard/"):
            protected.add((method, re.sub(r"\{[^}]+\}", "1", path)))  # fill path parameters
    return sorted(protected)


EXPECTED_ADMIN_ROUTES = {
    ("GET", "/admin/api/leads"),
    ("GET", "/admin/api/leads/1"),
    ("POST", "/admin/api/leads/1/outcome"),
    ("GET", "/admin/api/events"),
    ("GET", "/admin/api/events/1"),
    ("POST", "/admin/api/events/1/replay"),
    ("POST", "/admin/api/events/1/discard"),
    ("POST", "/admin/api/events/replay-quarantined"),
    ("POST", "/admin/api/messages/1/retry"),
    ("GET", "/admin/api/tenants"),
    ("POST", "/admin/api/tenants"),
    ("POST", "/admin/api/tenants/1/lead-sources"),
    ("GET", "/admin/api/analytics/summary"),
    ("GET", "/admin/api/claims"),
    ("GET", "/admin/api/claims/export.csv"),
    ("POST", "/admin/api/claims/1/status"),
    ("GET", "/admin/api/health"),
    ("GET", "/admin/api/alerts"),
    ("POST", "/admin/api/alerts/1/resolve"),
    ("GET", "/admin/api/payload-shapes"),
}


def test_route_inventory_finds_the_admin_api_and_the_dashboard(client):
    # Guard for the tests below: the discovery must really see the routes it claims to check.
    routes = set(protected_routes(client.app))
    assert EXPECTED_ADMIN_ROUTES <= routes
    assert any(path == "/dashboard" or path.startswith("/dashboard/") for _, path in routes)


def test_every_protected_route_rejects_a_request_without_credentials(client, probe):
    # No route under /admin/api or /dashboard may answer an anonymous caller.
    problems = []
    for method, path in protected_routes(client.app):
        response = probe.request(method, path, follow_redirects=False)
        if response.status_code != 401 or "www-authenticate" not in response.headers:
            problems.append(f"{method} {path} -> {response.status_code} {dict(response.headers)}")
    assert problems == []


@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization": "Bearer wrong-token"},
        {"Authorization": f"Bearer {ADMIN_TOKEN}x"},
        {"Authorization": f"Bearer {ADMIN_TOKEN[:-1]}"},
        basic("admin", "wrong-token"),
        {"Authorization": f"Bearer {API_KEY}"},  # the Angi webhook key is not the admin token
    ],
    ids=["wrong-bearer", "bearer-with-suffix", "bearer-prefix-only", "wrong-basic-password", "angi-api-key"],
)
def test_every_protected_route_rejects_a_wrong_token(client, probe, headers):
    # A wrong, truncated or extended token is as good as none.
    problems = []
    for method, path in protected_routes(client.app):
        response = probe.request(method, path, headers=headers, follow_redirects=False)
        if response.status_code != 401 or "www-authenticate" not in response.headers:
            problems.append(f"{method} {path} -> {response.status_code}")
    assert problems == []


@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization": f"Bearer {ADMIN_TOKEN}"},
        basic("admin", ADMIN_TOKEN),
        basic("anything-at-all", ADMIN_TOKEN),
        basic("", ADMIN_TOKEN),
    ],
    ids=["bearer", "basic-admin", "basic-any-username", "basic-empty-username"],
)
def test_every_protected_route_accepts_the_token_as_bearer_or_basic_password(client, probe, headers):
    # Scripts send a Bearer token; browsers send it as the HTTP Basic password under any username.
    problems = []
    for method, path in protected_routes(client.app):
        response = probe.request(method, path, headers=headers, follow_redirects=False)
        if response.status_code == 401:
            problems.append(f"{method} {path} -> 401")
    assert problems == []


def test_unauthenticated_401_invites_basic_auth_and_leaks_nothing(client, ctx):
    # The browser needs "WWW-Authenticate: Basic" to prompt; the body must not carry lead data.
    send(client, ads_payload(Email="private.person@example.com"))
    response = client.get("/admin/api/leads")

    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Basic ")
    assert "realm=" in response.headers["www-authenticate"]
    assert "private.person@example.com" not in response.text
    assert set(response.json()) == {"detail"}


@pytest.mark.parametrize(
    "authorization",
    [
        "",
        "Bearer",
        "Bearer ",
        "Basic",
        "Basic !!!not-base64!!!",
        "Basic " + base64.b64encode(b"\xff\xfe:\xff").decode(),  # not UTF-8 once decoded
        "Basic " + base64.b64encode(b"no-colon-here").decode(),
        ADMIN_TOKEN[:-1],  # a bare value without a scheme that is not the token
        f"Token {ADMIN_TOKEN}",
        f"Digest {ADMIN_TOKEN}",
        "Bearer \x00",
    ],
)
def test_malformed_authorization_headers_are_a_401_never_a_500(probe, authorization):
    # Garbage in the Authorization header must be rejected cleanly.
    response = probe.get("/admin/api/leads", headers={"Authorization": authorization})
    assert response.status_code == 401
    assert "www-authenticate" in response.headers


def test_bare_token_without_a_scheme_is_accepted(client):
    # "Authorization: <token>" with no "Bearer" in front is how some simple clients send a
    # key. The header is parsed the same way for Angi's key and for the admin token.
    assert client.get("/admin/api/leads", headers={"Authorization": ADMIN_TOKEN}).status_code == 200


def test_bearer_scheme_is_case_insensitive(client):
    # RFC 7235: the auth scheme name is case-insensitive.
    assert client.get("/admin/api/leads", headers={"Authorization": f"bearer {ADMIN_TOKEN}"}).status_code == 200
    assert client.get("/admin/api/leads", headers={"Authorization": f"BEARER {ADMIN_TOKEN}"}).status_code == 200


def test_admin_token_is_only_accepted_in_the_authorization_header(client):
    # Unlike the Angi key, the admin token is never read from a query string or a custom header.
    assert client.get("/admin/api/leads", params={"token": ADMIN_TOKEN, "api_key": ADMIN_TOKEN}).status_code == 401
    assert client.get("/admin/api/leads", headers={"X-API-KEY": ADMIN_TOKEN}).status_code == 401
    assert client.get("/admin/api/leads", headers={"X-Admin-Token": ADMIN_TOKEN}).status_code == 401


def test_angi_api_key_does_not_open_the_admin_api(client):
    # The two secrets are separate: the sender's key must not read leads back.
    assert client.get("/admin/api/leads", headers=AUTH).status_code == 401
    assert client.get("/admin/api/leads", headers=basic("angi", API_KEY)).status_code == 401


def test_with_no_admin_token_configured_the_api_is_open_in_development(client, ctx, probe):
    # Local development convenience: no token configured means no login.
    ctx.settings.admin_token = ""
    assert ctx.settings.is_production is False

    assert client.get("/admin/api/leads").status_code == 200
    for method, path in protected_routes(client.app):
        assert probe.request(method, path, follow_redirects=False).status_code != 401, (method, path)


@pytest.mark.parametrize("env", ["production", "prod", "PRODUCTION"])
def test_with_no_admin_token_configured_the_api_is_closed_in_production(client, ctx, probe, env):
    # In production an empty token must lock everyone out rather than let everyone in.
    ctx.settings.admin_token = ""
    ctx.settings.app_env = env

    for headers in ({}, ADMIN, basic("admin", ""), {"Authorization": "Bearer "}):
        for method, path in protected_routes(client.app):
            response = probe.request(method, path, headers=headers, follow_redirects=False)
            assert response.status_code == 401, (method, path, headers)


@pytest.mark.parametrize("env", ["staging", "prd", "live", " production", "production ", "", "Prod"])
def test_any_environment_that_is_not_explicitly_development_is_treated_as_production(client, ctx, probe, env):
    # Fail safe: a typo in APP_ENV must not switch authentication off.
    ctx.settings.admin_token = ""
    ctx.settings.app_env = env
    assert ctx.settings.is_production is True
    assert probe.get("/admin/api/leads").status_code == 401
    assert probe.get("/metrics").status_code == 401


@pytest.mark.parametrize("env", ["development", "dev", "local", "test", " Development "])
def test_named_development_environments_are_not_production(settings, env):
    # The explicit list of environments that may run with the conveniences switched on.
    settings.app_env = env
    assert settings.is_production is False


def test_production_refuses_to_start_without_an_admin_token(settings, mailer, clock):
    # Belt and braces: the app will not even boot in production with the dashboard unprotected.
    settings.app_env = "production"
    settings.admin_token = ""
    context = build_context(settings, email_provider=mailer, clock=clock)
    with pytest.raises(RuntimeError, match="ADMIN_TOKEN"):
        create_app(context=context)


def test_configured_token_is_still_required_in_production(client, ctx):
    # Production with a token behaves like any other environment.
    ctx.settings.app_env = "production"
    assert client.get("/admin/api/leads").status_code == 401
    assert client.get("/admin/api/leads", headers=ADMIN).status_code == 200


# --- /admin/api/leads ------------------------------------------------------------------------


@pytest.fixture
def four_leads(client, ctx, clock) -> dict[str, str]:
    """Four leads in different states, a minute apart:
    alice (Sparkle, contacted), bob (Summit, contacted), alice again (Sparkle, duplicate,
    contacted) and carol (BlueLine, no email -> needs review, never contacted)."""
    payloads = {
        "alice": ads_payload(
            FirstName="Alice", LastName="Anderson", Email="alice@example.com", PhoneNumber="3175550001",
            Category="Indianapolis – House Cleaning",
        ),
        "bob": ads_payload(
            FirstName="Bob", LastName="Brown", Email="bob.brown@example.org", PhoneNumber="3035550002",
            Category="Furnace Repair", ALAccountId=SUMMIT_ADS_ACCOUNT,
        ),
        "alice_again": ads_payload(
            FirstName="Alice", LastName="Anderson", Email="alice@example.com", PhoneNumber="3175550001",
            Category="Indianapolis – House Cleaning",
        ),
    }
    ids: dict[str, str] = {}
    for name, payload in payloads.items():
        ids[name] = send(client, payload)["leadId"]
        ids[f"{name}_angi_id"] = payload["CorrelationId"]
        clock.advance(minutes=1)
    deliver(ctx, clock)
    clock.advance(minutes=1)
    ids["carol"] = send(
        client,
        ads_payload(
            FirstName="Carol", LastName="Clark", Email="", PhoneNumber="5125550004",
            Category="Drain Cleaning", ALAccountId=BLUELINE_ACCOUNT,
        ),
    )["leadId"]
    return ids


def test_leads_list_is_empty_on_a_new_install(client):
    # No leads yet is an empty list, not an error.
    assert get(client, "/admin/api/leads").json() == {"count": 0, "leads": []}


def test_leads_list_returns_newest_first_with_tenant_and_derived_fields(client, four_leads):
    # The inbox view: most recent lead on top, each row ready to render.
    body = get(client, "/admin/api/leads").json()

    assert body["count"] == 4
    assert [lead["id"] for lead in body["leads"]] == [
        four_leads["carol"], four_leads["alice_again"], four_leads["bob"], four_leads["alice"],
    ]
    alice = body["leads"][-1]
    assert alice["tenant_id"] == "tnt_sparkle"
    assert alice["tenant_name"] == "Sparkle Home Cleaning"
    assert alice["tenant_slug"] == "sparkle-home-cleaning"
    assert alice["tenant_is_test"] == 0
    assert alice["full_name"] == "Alice Anderson"
    assert alice["task_name"] == "House Cleaning"
    assert alice["status"] == "contacted"
    assert alice["fee_cents"] == 2800
    assert alice["interview"] == []
    assert isinstance(alice["extras"], dict)
    assert alice["duplicate_matched_on"] == []
    assert alice["speed_to_lead_seconds"] == 181.0  # received at 15:00:00, emailed at 15:03:01
    carol = body["leads"][0]
    assert carol["speed_to_lead_seconds"] is None
    assert "interview_json" not in alice and "extras_json" not in alice


def test_leads_filter_by_tenant(client, four_leads):
    # ?tenant_id= shows one business's leads.
    assert lead_ids(client, tenant_id="tnt_sparkle") == [four_leads["alice_again"], four_leads["alice"]]
    assert lead_ids(client, tenant_id="tnt_summit") == [four_leads["bob"]]
    assert lead_ids(client, tenant_id="tnt_blueline") == [four_leads["carol"]]
    assert lead_ids(client, tenant_id="tnt_does_not_exist") == []


def test_leads_filter_by_status(client, four_leads):
    # ?status= follows the lead's current stage.
    assert lead_ids(client, status="new") == [four_leads["carol"]]
    assert lead_ids(client, status="contacted") == [
        four_leads["alice_again"], four_leads["bob"], four_leads["alice"],
    ]
    assert lead_ids(client, status="booked") == []


def test_leads_filter_duplicates_only(client, four_leads):
    # ?duplicates=true is the list behind the credit worklist.
    assert lead_ids(client, duplicates="true") == [four_leads["alice_again"]]
    assert len(lead_ids(client, duplicates="false")) == 4
    duplicate = get(client, "/admin/api/leads", duplicates="true").json()["leads"][0]
    assert duplicate["is_duplicate"] == 1
    assert duplicate["duplicate_of_lead_id"] == four_leads["alice"]
    assert duplicate["duplicate_matched_on"] == ["phone", "email"]


def test_leads_filter_needs_review(client, four_leads):
    # ?needs_review=true lists the leads a human should look at (here: no usable email).
    assert lead_ids(client, needs_review="true") == [four_leads["carol"]]


def test_leads_filters_combine(client, four_leads):
    # Filters are ANDed together.
    assert lead_ids(client, tenant_id="tnt_sparkle", duplicates="true") == [four_leads["alice_again"]]
    assert lead_ids(client, tenant_id="tnt_summit", duplicates="true") == []
    assert lead_ids(client, tenant_id="tnt_blueline", status="new", needs_review="true") == [four_leads["carol"]]
    assert lead_ids(client, tenant_id="tnt_sparkle", q="bob") == []


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("Anderson", ["alice_again", "alice"]),  # name
        ("alice anderson", ["alice_again", "alice"]),  # full name, case-insensitive
        ("CAROL", ["carol"]),
        ("bob.brown@example.org", ["bob"]),  # email
        ("example.org", ["bob"]),  # part of an email
        ("3035550002", ["bob"]),  # phone
        ("5550004", ["carol"]),  # part of a phone
        ("furnace", ["bob"]),  # service
        ("Cleaning", ["carol", "alice_again", "alice"]),  # service shared by several leads
        ("  Brown  ", ["bob"]),  # surrounding whitespace is ignored
        ("no-such-thing", []),
    ],
)
def test_leads_search_over_name_email_phone_and_service(client, four_leads, query, expected):
    # ?q= is a contains-search across the fields an operator would type.
    assert lead_ids(client, q=query) == [four_leads[name] for name in expected]


def test_leads_search_by_angi_lead_id_and_by_our_own_id(client, four_leads):
    # Angi support quotes their id; our logs quote ours. Both must find the lead.
    assert lead_ids(client, q=four_leads["bob_angi_id"]) == [four_leads["bob"]]
    assert lead_ids(client, q=four_leads["bob_angi_id"][:13]) == [four_leads["bob"]]
    assert lead_ids(client, q=four_leads["carol"]) == [four_leads["carol"]]


def test_leads_search_finds_an_angi_leads_numeric_lead_id(client, ctx):
    # The Angi Leads feed uses numeric ids; they are searchable as text.
    payload = leads_payload(leadOid=555000111)
    lead_id = send(client, payload)["leadId"]
    assert lead_ids(client, q="555000111") == [lead_id]


@pytest.mark.parametrize(
    "query", ["(303) 555-0002", "303-555-0002", "303.555.0002", "+1 303 555 0002", "1 (303) 555-0002", "555-0002"]
)
def test_leads_search_finds_a_phone_number_typed_with_punctuation(client, four_leads, query):
    # An operator copies the number from an email or a voicemail note, in whatever form.
    # Angi sent "3035550002"; the digits are what is compared.
    assert lead_ids(client, q=query) == [four_leads["bob"]]


@pytest.mark.parametrize("query", ["%", "a%e", "b_b", "ali_e", "%%", "\\"])
def test_leads_search_takes_percent_and_underscore_literally(client, four_leads, query):
    # "%" and "_" are wildcards to the database; to the person searching they are characters.
    # None of these leads contains them, so nothing may match ("a%e" would match "alice").
    assert lead_ids(client, q=query) == []


def test_leads_search_finds_text_that_really_contains_a_percent_sign(client, ctx):
    # ...and a lead that does contain the character is found by it.
    wanted = send(client, ads_payload(Category="Gutters 50%_off promo", PhoneNumber="3175550011"))["leadId"]
    send(client, ads_payload(Category="Gutters 50 percent off promo", PhoneNumber="3175550012", Email="x@example.com"))
    assert lead_ids(client, q="50%") == [wanted]
    assert lead_ids(client, q="50%_off") == [wanted]


def test_leads_limit_is_applied_and_clamped(client, four_leads):
    # ?limit= caps the list; zero and negative values are clamped up to one row.
    assert lead_ids(client, limit=2) == [four_leads["carol"], four_leads["alice_again"]]
    assert lead_ids(client, limit=1) == [four_leads["carol"]]
    assert lead_ids(client, limit=0) == [four_leads["carol"]]
    assert lead_ids(client, limit=-10) == [four_leads["carol"]]
    assert len(lead_ids(client, limit=1000000)) == 4  # an oversized limit is clamped down, not an error
    assert len(lead_ids(client, limit=10**30)) == 4  # however oversized


@pytest.mark.parametrize("params", [{"limit": "many"}, {"duplicates": "maybe"}, {"needs_review": "perhaps"}])
def test_leads_list_rejects_malformed_query_values(client, params):
    # A non-numeric limit or a non-boolean flag is a client error.
    assert client.get("/admin/api/leads", params=params, headers=ADMIN).status_code == 422


def test_leads_list_includes_canary_leads_marked_as_test(client, ctx):
    # The probe's leads are visible to operators, flagged so they can be told apart.
    send(client, ads_payload(ALAccountId=CANARY_ACCOUNT))
    leads = get(client, "/admin/api/leads").json()["leads"]
    assert [(lead["tenant_id"], lead["tenant_is_test"]) for lead in leads] == [("tnt_canary", 1)]


# --- /admin/api/leads/{id} -------------------------------------------------------------------


def test_lead_detail_structure(client, ctx, clock):
    # Everything known about one lead in one response.
    payload = ads_payload(Email="pat@example.com")
    lead_id = send(client, payload)["leadId"]
    deliver(ctx, clock)

    response = get(client, f"/admin/api/leads/{lead_id}")
    assert response.status_code == 200
    detail = response.json()
    assert set(detail) == {
        "lead", "tenant", "events", "messages", "appointment", "claim", "original", "duplicates", "webhook_events",
    }
    assert detail["lead"]["id"] == lead_id
    assert detail["lead"]["email_normalized"] == "pat@example.com"
    assert detail["lead"]["status"] == "contacted"
    assert detail["lead"]["speed_to_lead_seconds"] == 1.0
    assert detail["lead"]["interview"] == []
    assert detail["tenant"]["id"] == "tnt_sparkle"
    assert detail["tenant"]["name"] == "Sparkle Home Cleaning"
    assert detail["appointment"] is None
    assert detail["claim"] is None
    assert detail["original"] is None
    assert detail["duplicates"] == []

    # The timeline, oldest first, with decoded data.
    assert [event["type"] for event in detail["events"]] == [
        "lead.received", "email.queued", "email.queued", "email.queued", "email.sent",
    ]
    assert all(isinstance(event["data"], dict) and "data_json" not in event for event in detail["events"])
    assert detail["events"][0]["data"]["format"] == "angi_ads"
    assert detail["events"][-1]["data"]["seconds_since_received"] == 1.0

    # Outbound messages in the order they are (to be) sent.
    assert [(m["template"], m["status"]) for m in detail["messages"]] == [
        ("intro", "sent"), ("followup", "pending"), ("followup", "pending"),
    ]
    assert detail["messages"][0]["to_address"] == "pat@example.com"
    assert "Sparkle Home Cleaning" in detail["messages"][0]["subject"]

    # The request exactly as Angi sent it.
    assert len(detail["webhook_events"]) == 1
    event = detail["webhook_events"][0]
    assert event["status"] == "processed"
    assert event["detected_format"] == "angi_ads"
    assert json.loads(event["raw_body"]) == payload


def test_lead_detail_shows_the_appointment_once_booked(client, ctx, clock):
    # After the consumer books, the detail carries the scheduled appointment.
    lead_id = send(client, ads_payload())["leadId"]
    deliver(ctx, clock)
    token = db_one(ctx, "SELECT booking_token FROM leads WHERE id = ?", (lead_id,))["booking_token"]
    page = client.get(f"/book/{token}")
    slot = re.search(r'name="slot" value="([^"]+)"', page.text).group(1)
    assert client.post(f"/book/{token}", data={"slot": slot}, follow_redirects=False).status_code == 303

    detail = get(client, f"/admin/api/leads/{lead_id}").json()
    assert detail["appointment"]["starts_at"] == slot
    assert detail["appointment"]["status"] == "scheduled"
    assert detail["lead"]["status"] == "booked"
    assert "booking.appointment_booked" in [event["type"] for event in detail["events"]]


def test_lead_detail_lists_every_delivery_of_the_lead(client, ctx, clock):
    # A redelivery is a second webhook event on the same lead, with its own raw body.
    payload = ads_payload()
    lead_id = send(client, payload)["leadId"]
    clock.advance(minutes=15)
    assert send(client, payload)["result"] == "duplicate_delivery"

    events = get(client, f"/admin/api/leads/{lead_id}").json()["webhook_events"]
    assert [event["status"] for event in events] == ["processed", "duplicate_delivery"]
    assert all(json.loads(event["raw_body"]) == payload for event in events)


def test_lead_detail_for_a_duplicate_and_its_original(client, ctx, clock, four_leads):
    # The duplicate links to its claim and original; the original lists its duplicates.
    duplicate = get(client, f"/admin/api/leads/{four_leads['alice_again']}").json()
    assert duplicate["original"]["id"] == four_leads["alice"]
    assert duplicate["claim"]["lead_id"] == four_leads["alice_again"]
    assert duplicate["claim"]["status"] == "detected"

    original = get(client, f"/admin/api/leads/{four_leads['alice']}").json()
    assert [d["id"] for d in original["duplicates"]] == [four_leads["alice_again"]]
    assert set(original["duplicates"][0]) == {"id", "full_name", "received_at", "task_name"}


@pytest.mark.parametrize("lead_id", ["lead_does_not_exist", "1", "%25", "' OR 1=1 --"])
def test_lead_detail_404_for_an_unknown_lead(client, four_leads, lead_id):
    # An id that matches nothing is "not found" (and is never interpreted as SQL or a pattern).
    response = client.get(f"/admin/api/leads/{lead_id}", headers=ADMIN)
    assert response.status_code == 404
    assert response.json() == {"detail": "lead not found"}


# --- POST /admin/api/leads/{id}/outcome ------------------------------------------------------


def post_outcome(client, lead_id: str, **body):
    return client.post(f"/admin/api/leads/{lead_id}/outcome", headers=ADMIN, json=body)


def test_outcome_won_with_a_job_value(client, ctx, clock):
    # Closing the loop: the job was won for $1,234.56.
    lead_id = send(client, ads_payload())["leadId"]
    deliver(ctx, clock)
    clock.advance(days=2)

    response = post_outcome(client, lead_id, outcome="won", job_value=1234.56)

    assert response.status_code == 200
    lead = response.json()["lead"]
    assert lead["outcome"] == "won"
    assert lead["status"] == "won"
    assert lead["job_value_cents"] == 123456  # stored in cents
    assert lead["closed_at"] == iso(clock())
    stored = db_one(ctx, "SELECT outcome, status, job_value_cents, closed_at FROM leads WHERE id = ?", (lead_id,))
    assert stored == {"outcome": "won", "status": "won", "job_value_cents": 123456, "closed_at": iso(clock())}


def test_outcome_cancels_pending_follow_ups(client, ctx, mailer, clock):
    # A closed lead must not be nudged again.
    lead_id = send(client, ads_payload())["leadId"]
    deliver(ctx, clock)
    post_outcome(client, lead_id, outcome="won", job_value=500)

    messages = db_rows(
        ctx, "SELECT template, status, last_error FROM outbox_messages WHERE lead_id = ? ORDER BY sequence", (lead_id,)
    )
    assert messages == [
        {"template": "intro", "status": "sent", "last_error": None},
        {"template": "followup", "status": "cancelled", "last_error": "lead_won"},
        {"template": "followup", "status": "cancelled", "last_error": "lead_won"},
    ]
    clock.advance(days=3)
    ctx.worker.deliver_due()
    assert len(mailer.sent) == 1  # only the intro ever went out


def test_outcome_records_an_event_on_the_timeline(client, ctx, clock):
    # The audit trail shows who won what and when.
    lead_id = send(client, ads_payload())["leadId"]
    clock.advance(hours=5)
    post_outcome(client, lead_id, outcome="won", job_value=450, reason="signed on site")

    events = db_rows(
        ctx, "SELECT * FROM lead_events WHERE lead_id = ? AND type = 'lead.outcome_recorded'", (lead_id,)
    )
    assert len(events) == 1
    assert events[0]["occurred_at"] == iso(clock())
    assert json.loads(events[0]["data_json"]) == {"outcome": "won", "job_value_cents": 45000, "reason": "signed on site"}
    detail = get(client, f"/admin/api/leads/{lead_id}").json()
    assert detail["events"][-2]["type"] == "lead.outcome_recorded"
    assert detail["events"][-1]["type"] == "email.cancelled"


def test_won_outcome_shows_up_as_revenue_in_analytics(client, ctx, clock):
    # Revenue and return on spend come from recorded outcomes.
    lead_id = send(client, ads_payload())["leadId"]
    post_outcome(client, lead_id, outcome="won", job_value=1234.56)

    summary = get(client, "/admin/api/analytics/summary").json()
    assert summary["funnel"]["won"] == 1
    assert summary["money"]["revenue_cents"] == 123456
    assert summary["money"]["return_on_spend"] == round(123456 / 2800, 4)


def test_outcome_lost_with_a_reason(client, ctx, clock):
    # A lost lead keeps the reason and never carries a job value.
    lead_id = send(client, ads_payload())["leadId"]
    response = post_outcome(client, lead_id, outcome="lost", reason="went with a competitor", job_value=900)

    assert response.status_code == 200
    lead = response.json()["lead"]
    assert lead["outcome"] == "lost"
    assert lead["status"] == "lost"
    assert lead["outcome_reason"] == "went with a competitor"
    assert lead["job_value_cents"] is None
    assert lead["closed_at"] == iso(clock())
    summary = get(client, "/admin/api/analytics/summary").json()
    assert (summary["funnel"]["lost"], summary["money"]["revenue_cents"]) == (1, 0)


def test_outcome_can_be_corrected_from_won_to_lost(client, ctx, clock):
    # A cancelled job: the correction replaces the outcome and removes the revenue.
    lead_id = send(client, ads_payload())["leadId"]
    post_outcome(client, lead_id, outcome="won", job_value=800)
    clock.advance(days=1)
    post_outcome(client, lead_id, outcome="lost", reason="customer cancelled")

    stored = db_one(ctx, "SELECT outcome, status, job_value_cents, outcome_reason FROM leads WHERE id = ?", (lead_id,))
    assert stored == {
        "outcome": "lost", "status": "lost", "job_value_cents": None, "outcome_reason": "customer cancelled",
    }
    assert get(client, "/admin/api/analytics/summary").json()["money"]["revenue_cents"] == 0


def test_outcome_won_without_a_job_value_is_allowed(client, ctx):
    # The amount may not be known yet.
    lead_id = send(client, ads_payload())["leadId"]
    response = post_outcome(client, lead_id, outcome="won")
    assert response.status_code == 200
    assert response.json()["lead"]["job_value_cents"] is None


def test_outcome_won_again_without_a_job_value_keeps_the_recorded_value(client, ctx, clock):
    # The job was won for $450. Reporting "won" again to add a note must not erase the revenue.
    lead_id = send(client, ads_payload())["leadId"]
    post_outcome(client, lead_id, outcome="won", job_value=450)
    clock.advance(days=3)

    response = post_outcome(client, lead_id, outcome="won", reason="invoice paid")

    assert response.status_code == 200
    lead = response.json()["lead"]
    assert (lead["job_value_cents"], lead["outcome_reason"]) == (45000, "invoice paid")
    assert get(client, "/admin/api/analytics/summary").json()["money"]["revenue_cents"] == 45000
    # A new amount still replaces the old one, and "lost" still removes it.
    assert post_outcome(client, lead_id, outcome="won", job_value=500).json()["lead"]["job_value_cents"] == 50000
    assert post_outcome(client, lead_id, outcome="lost").json()["lead"]["job_value_cents"] is None
    assert post_outcome(client, lead_id, outcome="won").json()["lead"]["job_value_cents"] is None


@pytest.mark.parametrize(("dollars", "cents"), [(0, 0), (19.99, 1999), (0.29, 29), (1000, 100000), (4.35, 435)])
def test_outcome_job_value_dollars_are_converted_to_exact_cents(client, ctx, dollars, cents):
    # Binary floats like 19.99 and 0.29 must not lose a cent on the way in.
    lead_id = send(client, ads_payload())["leadId"]
    response = post_outcome(client, lead_id, outcome="won", job_value=dollars)
    assert response.json()["lead"]["job_value_cents"] == cents


@pytest.mark.parametrize(
    "body",
    [
        {"outcome": "won", "job_value": -1},
        {"outcome": "won", "job_value": -0.01},
        {"outcome": "maybe"},
        {"outcome": "WON"},
        {"outcome": ""},
        {"outcome": None},
        {},
        {"job_value": 100},
        {"outcome": "won", "job_value": "a lot"},
    ],
)
def test_outcome_rejects_bad_input_with_422_and_changes_nothing(client, ctx, clock, body):
    # A negative value or an unknown outcome must not half-update the lead.
    lead_id = send(client, ads_payload())["leadId"]
    deliver(ctx, clock)
    response = client.post(f"/admin/api/leads/{lead_id}/outcome", headers=ADMIN, json=body)

    assert response.status_code == 422
    stored = db_one(ctx, "SELECT outcome, status, job_value_cents, closed_at FROM leads WHERE id = ?", (lead_id,))
    assert stored == {"outcome": None, "status": "contacted", "job_value_cents": None, "closed_at": None}
    assert db_one(ctx, "SELECT COUNT(*) AS n FROM outbox_messages WHERE status = 'pending'")["n"] == 2
    assert db_rows(ctx, "SELECT id FROM lead_events WHERE type = 'lead.outcome_recorded'") == []


def test_outcome_404_for_an_unknown_lead(client):
    # A valid body for a lead that does not exist.
    response = post_outcome(client, "lead_does_not_exist", outcome="won", job_value=10)
    assert response.status_code == 404


def test_outcome_requires_the_admin_token(client, ctx):
    # Recording revenue is an operator action.
    lead_id = send(client, ads_payload())["leadId"]
    response = client.post(f"/admin/api/leads/{lead_id}/outcome", json={"outcome": "won", "job_value": 5})
    assert response.status_code == 401
    assert db_one(ctx, "SELECT outcome FROM leads WHERE id = ?", (lead_id,)) == {"outcome": None}


# --- tenants and mappings -------------------------------------------------------------------


def new_tenant(client, **overrides):
    body = {"slug": "acme-roofing", "name": "Acme Roofing", "timezone": "America/New_York", **overrides}
    return client.post("/admin/api/tenants", headers=ADMIN, json=body)


def test_create_tenant_returns_201_and_the_new_tenant(client, ctx, clock):
    # Onboarding a business.
    response = new_tenant(client, industry="Roofing", phone="(212) 555-0100", reply_to_email="office@example.com")

    assert response.status_code == 201
    tenant = response.json()["tenant"]
    assert tenant["id"].startswith("tnt_")
    assert tenant["slug"] == "acme-roofing"
    assert tenant["name"] == "Acme Roofing"
    assert tenant["industry"] == "Roofing"
    assert tenant["timezone"] == "America/New_York"
    assert tenant["phone"] == "(212) 555-0100"
    assert tenant["reply_to_email"] == "office@example.com"
    assert (tenant["business_hours_start"], tenant["business_hours_end"], tenant["appointment_minutes"]) == (8, 17, 120)
    assert (tenant["is_active"], tenant["is_test"]) == (1, 0)
    assert tenant["webhook_key"]  # a secret URL key is generated
    assert tenant["created_at"] == iso(clock())
    assert db_one(ctx, "SELECT name FROM tenants WHERE id = ?", (tenant["id"],)) == {"name": "Acme Roofing"}


def test_created_tenant_receives_leads_once_an_account_is_mapped(client, ctx, mailer, clock):
    # The full onboarding path: create tenant, map its Angi account, lead arrives and is answered.
    tenant_id = new_tenant(client).json()["tenant"]["id"]
    mapping = client.post(
        f"/admin/api/tenants/{tenant_id}/lead-sources",
        headers=ADMIN,
        json={"external_account_id": "700700", "external_account_name": "Acme Roofing on Angi", "default_fee": 52},
    )
    assert mapping.status_code == 201

    body = send(client, ads_payload(ALAccountId="700700"))
    assert body["tenant"] == {"id": tenant_id, "name": "Acme Roofing", "routedBy": "account_id"}
    deliver(ctx, clock)
    assert mailer.sent[0].from_name == "Acme Roofing"
    lead = db_one(ctx, "SELECT fee_cents, fee_source FROM leads WHERE id = ?", (body["leadId"],))
    assert lead == {"fee_cents": 5200, "fee_source": "account_default"}


def test_each_new_tenant_gets_its_own_webhook_key(client):
    # The per-tenant URL secret must be unique.
    first = new_tenant(client, slug="one-co").json()["tenant"]
    second = new_tenant(client, slug="two-co").json()["tenant"]
    assert first["webhook_key"] != second["webhook_key"]
    assert first["id"] != second["id"]


def test_create_tenant_with_a_duplicate_slug_is_a_409(client, ctx):
    # Slugs identify tenants in URLs and routing keys; a clash is a conflict, not a second tenant.
    assert new_tenant(client).status_code == 201
    before = db_one(ctx, "SELECT COUNT(*) AS n FROM tenants")["n"]

    response = new_tenant(client, name="Another Acme")
    assert response.status_code == 409
    assert "acme-roofing" in response.json()["detail"]
    assert db_one(ctx, "SELECT COUNT(*) AS n FROM tenants")["n"] == before
    assert db_one(ctx, "SELECT name FROM tenants WHERE slug = 'acme-roofing'") == {"name": "Acme Roofing"}


def test_create_tenant_with_a_seeded_slug_is_a_409(client):
    # The demo tenants count too.
    assert new_tenant(client, slug="sparkle-home-cleaning").status_code == 409


@pytest.mark.parametrize(
    "slug",
    ["Has Spaces", "UPPERCASE", "-leading-dash", "a", "", "sl/ash", "under_score", "dot.dot", "x" * 61, "émile"],
)
def test_create_tenant_rejects_an_invalid_slug(client, ctx, slug):
    # Slugs are lower-case letters, digits and dashes, 2-60 characters, not starting with a dash.
    before = db_one(ctx, "SELECT COUNT(*) AS n FROM tenants")["n"]
    assert new_tenant(client, slug=slug).status_code == 422
    assert db_one(ctx, "SELECT COUNT(*) AS n FROM tenants")["n"] == before


@pytest.mark.parametrize("timezone", ["Mars/Phobos", "", "America/Nowhere", "Not A Zone", "../../etc/passwd"])
def test_create_tenant_rejects_an_unknown_timezone(client, ctx, timezone):
    # Slots and quiet hours depend on the timezone; a typo must be caught at onboarding.
    before = db_one(ctx, "SELECT COUNT(*) AS n FROM tenants")["n"]
    response = new_tenant(client, timezone=timezone)
    assert response.status_code == 422
    assert "timezone" in response.text
    assert db_one(ctx, "SELECT COUNT(*) AS n FROM tenants")["n"] == before


@pytest.mark.parametrize("timezone", ["America", "America/Indiana", "america/new_york"])
def test_create_tenant_rejects_a_timezone_that_is_only_a_region_name(probe, timezone):
    # "America" is a folder of the time zone database, not a zone, and zone names are
    # spelled exactly; each must be a 422 like any other unknown zone.
    response = probe.post(
        "/admin/api/tenants", headers=ADMIN, json={"slug": "region-only", "name": "X", "timezone": timezone}
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    "reply_to", ["not an email", "office@example.com\nBcc: someone@example.org", "a@example.com, b@example.com"]
)
def test_create_tenant_rejects_a_reply_to_that_is_not_an_email_address(client, ctx, reply_to):
    # Consumers answer the intro email; replies must go to one real address at the business
    # (a line break would also smuggle a second header into every email).
    before = db_one(ctx, "SELECT COUNT(*) AS n FROM tenants")["n"]
    assert new_tenant(client, reply_to_email=reply_to).status_code == 422
    assert db_one(ctx, "SELECT COUNT(*) AS n FROM tenants")["n"] == before


@pytest.mark.parametrize(
    "overrides",
    [
        {"business_hours_start": 9, "business_hours_end": 9},
        {"business_hours_start": 17, "business_hours_end": 8},
        {"business_hours_start": -1},
        {"business_hours_end": 25},
        {"appointment_minutes": 0},
        {"appointment_minutes": -30},
        {"name": ""},
        {"name": None},
    ],
)
def test_create_tenant_rejects_impossible_hours_and_missing_name(client, overrides):
    # Opening hours must be a real interval inside one day and appointments must have a length.
    assert new_tenant(client, **overrides).status_code == 422


def test_create_tenant_requires_slug_and_name(client):
    # Both are mandatory.
    assert client.post("/admin/api/tenants", headers=ADMIN, json={"name": "No Slug"}).status_code == 422
    assert client.post("/admin/api/tenants", headers=ADMIN, json={"slug": "no-name"}).status_code == 422
    assert client.post("/admin/api/tenants", headers=ADMIN, json={}).status_code == 422


def test_tenants_list_includes_lead_sources(client):
    # The mapping table is shown with each tenant.
    response = get(client, "/admin/api/tenants")
    assert response.status_code == 200
    tenants = {tenant["id"]: tenant for tenant in response.json()["tenants"]}

    assert set(tenants) == {"tnt_sparkle", "tnt_summit", "tnt_blueline", "tnt_canary"}
    sparkle_sources = tenants["tnt_sparkle"]["lead_sources"]
    assert [(s["source"], s["external_account_id"], s["default_fee_cents"]) for s in sparkle_sources] == [
        ("angi", "123456", 2800)
    ]
    summit_sources = {s["external_account_id"]: s["default_fee_cents"] for s in tenants["tnt_summit"]["lead_sources"]}
    assert summit_sources == {"200001": 4500, "12345678": None}
    assert tenants["tnt_canary"]["is_test"] == 1
    assert tenants["tnt_sparkle"]["timezone"] == "America/Indiana/Indianapolis"


def test_tenants_list_shows_a_newly_created_tenant_and_its_mapping(client):
    # What was just onboarded is visible straight away.
    tenant_id = new_tenant(client).json()["tenant"]["id"]
    client.post(
        f"/admin/api/tenants/{tenant_id}/lead-sources", headers=ADMIN, json={"external_account_id": "700700"}
    )

    tenants = {tenant["id"]: tenant for tenant in get(client, "/admin/api/tenants").json()["tenants"]}
    assert tenants[tenant_id]["lead_sources"][0]["external_account_id"] == "700700"
    assert tenants[tenant_id]["lead_sources"][0]["learned"] == 0


def test_add_mapping_stores_the_default_fee_in_cents(client, ctx):
    # The fee is entered in dollars and stored as integer cents.
    response = client.post(
        "/admin/api/tenants/tnt_sparkle/lead-sources",
        headers=ADMIN,
        json={"external_account_id": "654321", "external_account_name": "Sparkle North", "default_fee": 32.5},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["replayed"] == []
    mapping = body["mapping"]
    assert mapping["tenant_id"] == "tnt_sparkle"
    assert mapping["source"] == "angi"
    assert mapping["external_account_id"] == "654321"
    assert mapping["external_account_name"] == "Sparkle North"
    assert mapping["default_fee_cents"] == 3250
    assert (mapping["is_active"], mapping["learned"]) == (1, 0)
    stored = db_one(ctx, "SELECT default_fee_cents FROM tenant_lead_sources WHERE external_account_id = '654321'")
    assert stored == {"default_fee_cents": 3250}


@pytest.mark.parametrize(("dollars", "cents"), [(19.99, 1999), (0.29, 29), (0, 0), (45, 4500)])
def test_add_mapping_fee_conversion_is_exact(client, dollars, cents):
    # No float drift when converting the per-lead price to cents.
    response = client.post(
        "/admin/api/tenants/tnt_sparkle/lead-sources",
        headers=ADMIN,
        json={"external_account_id": "654321", "default_fee": dollars},
    )
    assert response.json()["mapping"]["default_fee_cents"] == cents


def test_add_mapping_without_a_fee_leaves_it_unknown(client):
    # The Angi Leads feed states the fee per lead, so a default is optional.
    response = client.post(
        "/admin/api/tenants/tnt_sparkle/lead-sources", headers=ADMIN, json={"external_account_id": "654321"}
    )
    assert response.status_code == 201
    assert response.json()["mapping"]["default_fee_cents"] is None


def test_add_mapping_immediately_processes_leads_quarantined_for_that_account(client, ctx, mailer, clock):
    # The reason the endpoint exists: add the mapping and the waiting leads are answered at once.
    first = send(client, ads_payload(Email="one@example.com", PhoneNumber="3175550001", ALAccountId=UNMAPPED_ACCOUNT))
    second = send(client, ads_payload(Email="two@example.com", PhoneNumber="3175550002", ALAccountId=UNMAPPED_ACCOUNT))
    other = send(client, ads_payload(Email="three@example.com", PhoneNumber="3175550003", ALAccountId="000222"))
    assert {first["result"], second["result"], other["result"]} == {"quarantined"}
    clock.advance(minutes=20)

    response = client.post(
        "/admin/api/tenants/tnt_summit/lead-sources",
        headers=ADMIN,
        json={"external_account_id": UNMAPPED_ACCOUNT, "default_fee": 40},
    )

    assert response.status_code == 201
    replayed = response.json()["replayed"]
    # Only this account's leads are run again; the other account's lead is left alone.
    assert {r["eventId"]: r["result"] for r in replayed} == {
        first["eventId"]: "processed",
        second["eventId"]: "processed",
    }
    untouched = db_one(ctx, "SELECT status, attempts FROM webhook_events WHERE id = ?", (other["eventId"],))
    assert untouched == {"status": "quarantined", "attempts": 1}
    leads = db_rows(ctx, "SELECT tenant_id, fee_cents, received_at FROM leads ORDER BY email")
    assert leads == [
        {"tenant_id": "tnt_summit", "fee_cents": 4000, "received_at": iso(START)},
        {"tenant_id": "tnt_summit", "fee_cents": 4000, "received_at": iso(START)},
    ]
    ctx.worker.deliver_due()
    assert sorted(email.to for email in mailer.sent) == ["one@example.com", "two@example.com"]


def test_add_mapping_can_move_an_account_to_another_tenant(client, ctx):
    # An Angi account can change hands; re-posting the mapping re-points it and keeps the fee.
    response = client.post(
        "/admin/api/tenants/tnt_blueline/lead-sources", headers=ADMIN, json={"external_account_id": "123456"}
    )
    assert response.status_code == 201
    mapping = response.json()["mapping"]
    assert mapping["tenant_id"] == "tnt_blueline"
    assert mapping["default_fee_cents"] == 2800  # not supplied, so the existing fee is kept
    assert db_one(ctx, "SELECT COUNT(*) AS n FROM tenant_lead_sources WHERE external_account_id = '123456'")["n"] == 1
    assert send(client, ads_payload())["tenant"]["id"] == "tnt_blueline"


def test_add_mapping_404_for_an_unknown_tenant(client, ctx):
    # A mapping needs an existing tenant.
    response = client.post(
        "/admin/api/tenants/tnt_nobody/lead-sources", headers=ADMIN, json={"external_account_id": "654321"}
    )
    assert response.status_code == 404
    assert db_rows(ctx, "SELECT id FROM tenant_lead_sources WHERE external_account_id = '654321'") == []


@pytest.mark.parametrize(
    "body",
    [{}, {"external_account_id": ""}, {"external_account_id": "654321", "default_fee": -1}, {"default_fee": 10}],
)
def test_add_mapping_rejects_bad_input(client, ctx, body):
    # An account id is required and a fee cannot be negative.
    response = client.post("/admin/api/tenants/tnt_sparkle/lead-sources", headers=ADMIN, json=body)
    assert response.status_code == 422
    assert db_one(ctx, "SELECT COUNT(*) AS n FROM tenant_lead_sources WHERE tenant_id = 'tnt_sparkle'")["n"] == 1


@pytest.mark.parametrize(("sent", "stored"), [(654321, "654321"), ("654321", "654321"), ("  654321 ", "654321")])
def test_add_mapping_accepts_the_account_id_as_a_number_or_as_text(client, sent, stored):
    # Angi writes this id as a string in one feed and as a number in the other.
    response = client.post(
        "/admin/api/tenants/tnt_sparkle/lead-sources", headers=ADMIN, json={"external_account_id": sent}
    )
    assert response.status_code == 201
    assert response.json()["mapping"]["external_account_id"] == stored


def test_add_mapping_rejects_a_blank_account_id_with_422(probe):
    # "   " is not an account id; that is a client error, not a crash.
    response = probe.post(
        "/admin/api/tenants/tnt_sparkle/lead-sources", headers=ADMIN, json={"external_account_id": "   "}
    )
    assert response.status_code == 422


# --- out-of-range numbers ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "raw_body"),
    [
        ("/admin/api/leads/{lead_id}/outcome", '{"outcome": "won", "job_value": 1e400}'),
        ("/admin/api/leads/{lead_id}/outcome", '{"outcome": "won", "job_value": 1e300}'),
        ("/admin/api/leads/{lead_id}/outcome", '{"outcome": "won", "job_value": NaN}'),
        ("/admin/api/leads/{lead_id}/outcome", '{"outcome": "won", "job_value": -Infinity}'),
        ("/admin/api/leads/{lead_id}/outcome", '{"outcome": "won", "job_value": 1000000001}'),
        ("/admin/api/tenants/tnt_sparkle/lead-sources", '{"external_account_id": "654321", "default_fee": 1e400}'),
        ("/admin/api/tenants/tnt_sparkle/lead-sources", '{"external_account_id": "654321", "default_fee": NaN}'),
        ("/admin/api/tenants/tnt_sparkle/lead-sources", '{"external_account_id": NaN}'),
        ("/admin/api/tenants", '{"slug": "huge", "name": "Huge", "appointment_minutes": 99999999999999999999}'),
        ("/admin/api/tenants", '{"slug": "huge", "name": "Huge", "appointment_minutes": 1441}'),
        ("/admin/api/tenants", '{"slug": "huge", "name": "Huge", "business_hours_start": 1e400}'),
        ("/admin/api/tenants", '{"slug": "huge", "name": "Huge", "business_hours_end": NaN}'),
        ("/admin/api/alerts/99999999999999999999/resolve", ""),
        ("/admin/api/alerts/0/resolve", ""),
        ("/admin/api/alerts/-1/resolve", ""),
    ],
    ids=[
        "job-value-infinity", "job-value-1e300", "job-value-nan", "job-value-minus-infinity", "job-value-too-large",
        "default-fee-infinity", "default-fee-nan", "account-id-nan",
        "appointment-minutes-huge", "appointment-minutes-over-a-day", "hours-infinity", "hours-nan",
        "alert-id-huge", "alert-id-zero", "alert-id-negative",
    ],
)
def test_out_of_range_numbers_are_a_422_not_a_server_error(client, ctx, probe, path, raw_body):
    # A nonsense number in a request is the caller's mistake and must be reported as such.
    # (Python's JSON reader accepts NaN and Infinity, so "is it a number" is not enough.)
    lead_id = send(client, ads_payload())["leadId"]
    tenants_before = db_one(ctx, "SELECT COUNT(*) AS n FROM tenants")["n"]
    response = probe.post(path.format(lead_id=lead_id), headers=JSON_HEADERS, content=raw_body)

    assert response.status_code == 422
    assert db_one(ctx, "SELECT outcome FROM leads WHERE id = ?", (lead_id,)) == {"outcome": None}
    assert db_one(ctx, "SELECT COUNT(*) AS n FROM tenants")["n"] == tenants_before
    assert db_rows(ctx, "SELECT id FROM tenant_lead_sources WHERE external_account_id IN ('654321', 'nan')") == []


def test_validation_errors_say_what_is_wrong_without_echoing_the_value(client, probe):
    # The answer names the field and the problem. It does not repeat what was sent: a
    # value that cannot be written as JSON (NaN) would otherwise turn the 422 into a 500,
    # and whatever was typed into the wrong field stays out of responses and proxies' logs.
    lead_id = send(client, ads_payload())["leadId"]
    response = probe.post(
        f"/admin/api/leads/{lead_id}/outcome",
        headers=JSON_HEADERS,
        content='{"outcome": "won", "job_value": "typed-into-the-wrong-box-123"}',
    )

    assert response.status_code == 422
    assert response.json() == {
        "detail": [
            {
                "type": "float_parsing",
                "loc": ["body", "job_value"],
                "msg": "Input should be a valid number, unable to parse string as a number",
            }
        ]
    }
    assert "typed-into-the-wrong-box-123" not in response.text


@pytest.mark.parametrize("limit", ["1e3", "NaN", "9" * 5000, "ten"])
def test_list_limits_that_are_not_whole_numbers_are_a_422(probe, limit):
    # Limits are clamped when they are numbers; anything else is a client error on every list.
    for path in ("/admin/api/leads", "/admin/api/events"):
        assert probe.get(f"{path}?limit={limit}", headers=ADMIN).status_code == 422, path


# --- webhook events ---------------------------------------------------------------------------


@pytest.fixture
def mixed_events(client, ctx, clock) -> dict[str, str]:
    """One event of each kind, a minute apart: processed, duplicate_delivery, quarantined
    (unmapped account), quarantined (unrecognised payload) and rejected (not JSON)."""
    ids: dict[str, str] = {}
    payload = ads_payload()
    ids["processed"] = send(client, payload)["eventId"]
    clock.advance(minutes=1)
    ids["redelivery"] = send(client, payload)["eventId"]
    clock.advance(minutes=1)
    ids["unmapped"] = send(client, ads_payload(ALAccountId=UNMAPPED_ACCOUNT))["eventId"]
    clock.advance(minutes=1)
    ids["unrecognised"] = send(client, {"hello": "world", "n": 1})["eventId"]
    clock.advance(minutes=1)
    rejected = post_lead(client, "<xml>not json</xml>")
    assert rejected.status_code == 400
    ids["rejected"] = rejected.json()["eventId"]
    return ids


def event_ids(client, **params) -> list[str]:
    response = get(client, "/admin/api/events", **params)
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == len(body["events"])
    return [event["id"] for event in body["events"]]


def test_events_list_newest_first_with_a_body_preview(client, mixed_events):
    # The raw intake log, most recent first.
    body = get(client, "/admin/api/events").json()

    assert [event["id"] for event in body["events"]] == [
        mixed_events["rejected"],
        mixed_events["unrecognised"],
        mixed_events["unmapped"],
        mixed_events["redelivery"],
        mixed_events["processed"],
    ]
    processed = body["events"][-1]
    assert processed["status"] == "processed"
    assert processed["detected_format"] == "angi_ads"
    assert processed["parse_mode"] == "strict"
    assert processed["tenant_id"] == "tnt_sparkle"
    assert processed["tenant_name"] == "Sparkle Home Cleaning"
    assert processed["lead_id"].startswith("lead_")
    assert processed["attempts"] == 1
    assert processed["body_preview"].startswith('{"FirstName"')
    assert isinstance(processed["issues"], dict)
    assert "raw_body" not in processed and "headers_json" not in processed  # the list stays light
    rejected = body["events"][0]
    assert (rejected["status"], rejected["tenant_id"], rejected["lead_id"]) == ("rejected", None, None)
    assert rejected["body_preview"] == "<xml>not json</xml>"


def test_events_list_status_filter(client, mixed_events):
    # ?status= narrows the log to one outcome.
    assert event_ids(client, status="processed") == [mixed_events["processed"]]
    assert event_ids(client, status="duplicate_delivery") == [mixed_events["redelivery"]]
    assert event_ids(client, status="quarantined") == [mixed_events["unrecognised"], mixed_events["unmapped"]]
    assert event_ids(client, status="rejected") == [mixed_events["rejected"]]
    assert event_ids(client, status="failed") == []


def test_events_list_limit_is_applied_and_clamped(client, mixed_events):
    # Same clamping as the leads list.
    assert event_ids(client, limit=2) == [mixed_events["rejected"], mixed_events["unrecognised"]]
    assert event_ids(client, limit=0) == [mixed_events["rejected"]]
    assert event_ids(client, limit=-3) == [mixed_events["rejected"]]
    assert len(event_ids(client, limit=99999)) == 5


def test_events_list_body_preview_is_truncated(client, ctx):
    # A huge body is previewed, not shipped whole, in the list.
    send(client, ads_payload(Description="x" * 5000))
    event = get(client, "/admin/api/events").json()["events"][0]
    assert len(event["body_preview"]) == 400


def test_event_detail_has_the_raw_body_and_redacted_headers(client, ctx):
    # Forensics need the exact request; credentials must never be stored or shown.
    payload = ads_payload()
    secret_headers = {
        "X-API-KEY": API_KEY,
        "Authorization": "Bearer partner-secret-value",
        "Cookie": "session=cookie-secret-value",
        "X-Custom-Token": "token-secret-value",
        "X-Hub-Signature": "sha256=signature-secret-value",
        "User-Agent": "AngiLeadSender/1.0",
        "X-Request-Id": "req-12345",
    }
    event_id = send(client, payload, headers=secret_headers)["eventId"]

    response = get(client, f"/admin/api/events/{event_id}")
    assert response.status_code == 200
    event = response.json()
    assert json.loads(event["raw_body"]) == payload
    assert event["status"] == "processed"
    assert event["detected_format"] == "angi_ads"
    assert event["issues"]["issues"] == []
    assert set(event["issues"]["drift"]) == {"unknown", "missing", "mistyped", "recovered"}

    headers = event["headers"]
    assert headers["x-api-key"] == "[redacted]"
    assert headers["authorization"] == "[redacted]"
    assert headers["cookie"] == "[redacted]"
    assert headers["x-custom-token"] == "[redacted]"
    assert headers["x-hub-signature"] == "[redacted]"
    assert headers["user-agent"] == "AngiLeadSender/1.0"  # harmless headers are kept for debugging
    assert headers["x-request-id"] == "req-12345"
    assert headers["content-type"] == "application/json"
    for secret in (API_KEY, "partner-secret-value", "cookie-secret-value", "token-secret-value", "signature-secret-value"):
        assert secret not in response.text
    stored = db_one(ctx, "SELECT headers_json FROM webhook_events WHERE id = ?", (event_id,))["headers_json"]
    assert API_KEY not in stored and "partner-secret-value" not in stored


def test_event_detail_404_for_an_unknown_event(client, mixed_events):
    # Not found is not found.
    response = get(client, "/admin/api/events/evt_does_not_exist")
    assert response.status_code == 404
    assert response.json() == {"detail": "event not found"}


def test_replay_404_for_an_unknown_event(client, ctx):
    # Replaying something we never stored must not create anything.
    response = client.post("/admin/api/events/evt_does_not_exist/replay", headers=ADMIN)
    assert response.status_code == 404
    assert db_rows(ctx, "SELECT id FROM webhook_events") == []
    assert db_rows(ctx, "SELECT id FROM alerts") == []


def test_replay_of_a_processed_event_is_a_no_op(client, ctx, mailer, clock, mixed_events):
    # Idempotent: the event already produced its lead, so nothing is created or sent again.
    deliver(ctx, clock)
    response = client.post(f"/admin/api/events/{mixed_events['processed']}/replay", headers=ADMIN)

    assert response.status_code == 200
    body = response.json()
    assert body["httpStatus"] == 200
    assert body["status"] == "success"
    assert body["result"] == "processed"
    assert body["message"] == "Already processed."
    assert db_one(ctx, "SELECT COUNT(*) AS n FROM leads")["n"] == 1
    assert db_one(ctx, "SELECT COUNT(*) AS n FROM outbox_messages")["n"] == 3
    clock.advance(seconds=5)
    ctx.worker.deliver_due()
    assert len(mailer.sent) == 1


def test_replay_of_a_still_broken_event_reports_the_same_outcome(client, mixed_events):
    # Replay does not hide problems: an unmapped account is still quarantined, junk is still rejected.
    unmapped = client.post(f"/admin/api/events/{mixed_events['unmapped']}/replay", headers=ADMIN).json()
    assert (unmapped["httpStatus"], unmapped["result"], unmapped["reason"]) == (200, "quarantined", "unmapped_account")

    rejected = client.post(f"/admin/api/events/{mixed_events['rejected']}/replay", headers=ADMIN)
    assert rejected.status_code == 200  # the replay request itself succeeded
    assert (rejected.json()["httpStatus"], rejected.json()["result"]) == (400, "rejected")


def test_replay_processes_a_quarantined_event_once_the_cause_is_fixed(client, ctx, mailer, clock):
    # Tenant switched back on -> replaying the held event creates and answers the lead.
    with ctx.db.transaction() as conn:  # there is no API to (de)activate a tenant; flip the flag directly
        conn.execute("UPDATE tenants SET is_active = 0 WHERE id = 'tnt_blueline'")
    held = send(client, ads_payload(ALAccountId=BLUELINE_ACCOUNT))
    assert (held["result"], held["reason"]) == ("quarantined", "tenant_inactive")
    with ctx.db.transaction() as conn:
        conn.execute("UPDATE tenants SET is_active = 1 WHERE id = 'tnt_blueline'")
    clock.advance(minutes=30)

    body = client.post(f"/admin/api/events/{held['eventId']}/replay", headers=ADMIN).json()

    assert (body["result"], body["tenant"]["id"]) == ("processed", "tnt_blueline")
    lead = db_one(ctx, "SELECT received_at, created_at FROM leads WHERE id = ?", (body["leadId"],))
    assert lead["received_at"] == iso(START)  # the speed-to-lead clock started when Angi sent it
    assert lead["created_at"] == iso(clock())
    ctx.worker.deliver_due()
    assert len(mailer.sent) == 1


def lead_without_an_account_id(client) -> dict:
    """A lead whose payload does not say which business it is for: it can only wait."""
    payload = ads_payload()
    del payload["ALAccountId"]
    body = send(client, payload)
    assert body["result"] == "quarantined"
    return body


def test_replay_with_a_tenant_releases_a_lead_that_names_no_account(client, ctx, mailer, clock):
    # Nothing in the payload can route it, so an operator says whose lead it is.
    held = lead_without_an_account_id(client)
    clock.advance(minutes=10)

    response = client.post(
        f"/admin/api/events/{held['eventId']}/replay", headers=ADMIN, json={"tenant_id": "tnt_blueline"}
    )

    assert response.status_code == 200
    body = response.json()
    assert (body["result"], body["tenant"]["id"], body["tenant"]["routedBy"]) == ("processed", "tnt_blueline", "operator")
    event = db_one(ctx, "SELECT status, tenant_id, operator_tenant_id FROM webhook_events WHERE id = ?", (held["eventId"],))
    assert event == {"status": "processed", "tenant_id": "tnt_blueline", "operator_tenant_id": "tnt_blueline"}
    ctx.worker.deliver_due()
    assert [email.from_name for email in mailer.sent] == ["BlueLine Plumbing"]


def test_replay_with_an_unknown_tenant_is_a_404_and_changes_nothing(client, ctx):
    # A mistyped tenant id must not be written onto the event.
    held = lead_without_an_account_id(client)
    response = client.post(
        f"/admin/api/events/{held['eventId']}/replay", headers=ADMIN, json={"tenant_id": "tnt_nobody"}
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "tenant not found"}
    event = db_one(ctx, "SELECT status, attempts, operator_tenant_id FROM webhook_events WHERE id = ?", (held["eventId"],))
    assert event == {"status": "quarantined", "attempts": 1, "operator_tenant_id": None}


@pytest.mark.parametrize("body", [None, {}, {"tenant_id": None}])
def test_replay_without_a_tenant_is_a_plain_replay(client, ctx, body):
    # The body is optional; without a tenant the event is simply run again.
    held = lead_without_an_account_id(client)
    kwargs = {} if body is None else {"json": body}
    response = client.post(f"/admin/api/events/{held['eventId']}/replay", headers=ADMIN, **kwargs)

    assert response.status_code == 200
    assert response.json()["result"] == "quarantined"
    assert db_one(ctx, "SELECT operator_tenant_id FROM webhook_events") == {"operator_tenant_id": None}


def test_replay_with_a_tenant_id_that_is_not_storable_text_is_a_404_not_a_crash(client, ctx, probe):
    # Half of an emoji (an unpaired surrogate) is valid JSON but cannot be given to SQLite.
    held = lead_without_an_account_id(client)
    response = probe.post(
        f"/admin/api/events/{held['eventId']}/replay", headers=JSON_HEADERS, content='{"tenant_id": "tnt_\\ud83d"}'
    )
    assert response.status_code == 404
    assert db_one(ctx, "SELECT attempts, operator_tenant_id FROM webhook_events") == {
        "attempts": 1, "operator_tenant_id": None,
    }


@pytest.mark.parametrize("body", [{"tenant_id": 5}, {"tenant_id": ["tnt_sparkle"]}, ["tnt_sparkle"]])
def test_replay_rejects_a_malformed_body_with_422(client, ctx, body):
    # A tenant id is text.
    held = lead_without_an_account_id(client)
    response = client.post(f"/admin/api/events/{held['eventId']}/replay", headers=ADMIN, json=body)
    assert response.status_code == 422
    assert db_one(ctx, "SELECT attempts FROM webhook_events") == {"attempts": 1}


@pytest.mark.parametrize("kind", ["unrecognised", "unmapped", "rejected"])
def test_discard_takes_an_event_out_of_the_backlog_and_keeps_the_raw_body(client, ctx, clock, mixed_events, kind):
    # Junk, a ping, a test: the operator says it will never be a lead. It stops counting as
    # waiting, but what was received stays on record.
    event_id = mixed_events[kind]
    before = db_one(ctx, "SELECT raw_body FROM webhook_events WHERE id = ?", (event_id,))
    clock.advance(minutes=1)

    response = client.post(f"/admin/api/events/{event_id}/discard", headers=ADMIN)

    assert response.status_code == 200
    assert response.json() == {"discarded": True, "eventId": event_id}
    after = db_one(ctx, "SELECT status, raw_body, processed_at FROM webhook_events WHERE id = ?", (event_id,))
    assert after == {"status": "discarded", "raw_body": before["raw_body"], "processed_at": iso(clock())}
    assert event_ids(client, status="discarded") == [event_id]


def test_discarding_the_quarantined_events_turns_the_quarantine_check_green(client, mixed_events):
    # The reason the action exists: an unrecoverable event must not keep the check failing forever.
    def quarantine_check() -> dict:
        checks = get(client, "/admin/api/health").json()["checks"]
        return next(check for check in checks if check["name"] == "quarantine")

    assert quarantine_check()["detail"]["count"] == 2
    client.post(f"/admin/api/events/{mixed_events['unrecognised']}/discard", headers=ADMIN)
    assert quarantine_check()["detail"] == {"count": 1, "by_reason": {"unmapped_account": 1}}
    client.post(f"/admin/api/events/{mixed_events['unmapped']}/discard", headers=ADMIN)
    assert quarantine_check()["status"] == "ok"
    # Nothing is left for "replay everything" to churn through.
    assert client.post("/admin/api/events/replay-quarantined", headers=ADMIN).json() == {"replayed": 0, "results": []}


@pytest.mark.parametrize("kind", ["processed", "redelivery"])
def test_discard_refuses_an_event_that_became_a_lead(client, ctx, mixed_events, kind):
    # A processed delivery is a lead's history, not backlog: 409, nothing changes.
    event_id = mixed_events[kind]
    before = db_one(ctx, "SELECT status FROM webhook_events WHERE id = ?", (event_id,))

    response = client.post(f"/admin/api/events/{event_id}/discard", headers=ADMIN)

    assert response.status_code == 409
    assert before["status"] in response.json()["detail"]
    assert db_one(ctx, "SELECT status FROM webhook_events WHERE id = ?", (event_id,)) == before


def test_discard_twice_is_a_409_and_an_unknown_event_is_a_404(client, mixed_events):
    # Already discarded is a conflict; never stored is not found.
    url = f"/admin/api/events/{mixed_events['unrecognised']}/discard"
    assert client.post(url, headers=ADMIN).status_code == 200
    assert client.post(url, headers=ADMIN).status_code == 409
    missing = client.post("/admin/api/events/evt_does_not_exist/discard", headers=ADMIN)
    assert (missing.status_code, missing.json()) == (404, {"detail": "event not found"})


def test_a_discarded_event_can_still_be_replayed_on_purpose(client, ctx, mixed_events):
    # Discarding is not deleting: if it turns out to be a lead after all, replay still works.
    event_id = mixed_events["unmapped"]
    client.post(f"/admin/api/events/{event_id}/discard", headers=ADMIN)
    client.post(
        "/admin/api/tenants/tnt_sparkle/lead-sources", headers=ADMIN, json={"external_account_id": UNMAPPED_ACCOUNT}
    )
    assert db_one(ctx, "SELECT status FROM webhook_events WHERE id = ?", (event_id,)) == {"status": "discarded"}

    replayed = client.post(f"/admin/api/events/{event_id}/replay", headers=ADMIN).json()
    assert replayed["result"] == "processed"


# --- POST /admin/api/messages/{id}/retry ------------------------------------------------------


@pytest.fixture
def failed_intro(client, ctx, mailer, clock) -> dict[str, str]:
    """A lead whose first email was refused for good by the mail server."""
    lead_id = send(client, ads_payload())["leadId"]
    mailer.fail_with = PermanentEmailError("550 mailbox does not exist")
    mailer.fail_times = 1
    deliver(ctx, clock)
    message = db_one(ctx, "SELECT id, status FROM outbox_messages WHERE lead_id = ? AND template = 'intro'", (lead_id,))
    assert message["status"] == "failed"
    return {"lead_id": lead_id, "message_id": message["id"]}


def test_retry_gives_a_failed_message_a_fresh_set_of_attempts_and_wakes_the_worker(
    client, ctx, mailer, clock, failed_intro, monkeypatch
):
    # After the cause is fixed the operator retries; the email goes out without waiting for a poll.
    woken = []
    monkeypatch.setattr(ctx.worker, "wake", lambda: woken.append(True))
    clock.advance(minutes=30)

    response = client.post(f"/admin/api/messages/{failed_intro['message_id']}/retry", headers=ADMIN)

    assert response.status_code == 200
    assert response.json() == {"requeued": True, "messageId": failed_intro["message_id"]}
    assert woken == [True]
    message = db_one(
        ctx, "SELECT status, attempts, send_after, last_error FROM outbox_messages WHERE id = ?",
        (failed_intro["message_id"],),
    )
    assert message == {"status": "pending", "attempts": 0, "send_after": iso(clock()), "last_error": None}

    ctx.worker.deliver_due()
    assert len(mailer.sent) == 1
    lead = db_one(ctx, "SELECT status, first_contacted_at FROM leads WHERE id = ?", (failed_intro["lead_id"],))
    assert lead == {"status": "contacted", "first_contacted_at": iso(clock())}


def test_retry_refuses_a_message_that_is_not_failed(client, ctx, clock, failed_intro, monkeypatch):
    # Retrying something pending or already sent would send it twice: 409, and the worker is left alone.
    woken = []
    url = f"/admin/api/messages/{failed_intro['message_id']}/retry"
    assert client.post(url, headers=ADMIN).status_code == 200  # now pending
    monkeypatch.setattr(ctx.worker, "wake", lambda: woken.append(True))

    pending = client.post(url, headers=ADMIN)
    assert pending.status_code == 409
    assert "'pending'" in pending.json()["detail"]
    ctx.worker.deliver_due()
    sent = client.post(url, headers=ADMIN)
    assert sent.status_code == 409
    assert "'sent'" in sent.json()["detail"]
    assert woken == []
    assert db_one(ctx, "SELECT attempts FROM outbox_messages WHERE id = ?", (failed_intro["message_id"],)) == {"attempts": 1}


def test_retry_404_for_an_unknown_message(client, ctx, monkeypatch):
    # Not found is not found.
    woken = []
    monkeypatch.setattr(ctx.worker, "wake", lambda: woken.append(True))
    response = client.post("/admin/api/messages/msg_does_not_exist/retry", headers=ADMIN)
    assert (response.status_code, response.json()) == (404, {"detail": "message not found"})
    assert woken == []


def test_replay_quarantined_replays_everything_waiting(client, ctx, mixed_events):
    # Without a filter every quarantined event is run again, and nothing else is.
    response = client.post("/admin/api/events/replay-quarantined", headers=ADMIN)

    assert response.status_code == 200
    body = response.json()
    assert body["replayed"] == 2
    assert {r["eventId"] for r in body["results"]} == {mixed_events["unmapped"], mixed_events["unrecognised"]}
    assert {r["result"] for r in body["results"]} == {"quarantined"}  # nothing was fixed, so nothing changed
    attempts = {
        row["id"]: row["attempts"] for row in db_rows(ctx, "SELECT id, attempts FROM webhook_events")
    }
    assert attempts[mixed_events["unmapped"]] == 2
    assert attempts[mixed_events["unrecognised"]] == 2
    assert attempts[mixed_events["processed"]] == 1
    assert attempts[mixed_events["rejected"]] == 1


def test_replay_quarantined_with_a_reason_filter(client, ctx, mixed_events):
    # ?reason= replays only the events held for that reason.
    response = client.post(
        "/admin/api/events/replay-quarantined", params={"reason": "unrecognised_payload"}, headers=ADMIN
    )

    body = response.json()
    assert body["replayed"] == 1
    assert body["results"] == [{"eventId": mixed_events["unrecognised"], "result": "quarantined"}]
    attempts = {
        row["id"]: row["attempts"] for row in db_rows(ctx, "SELECT id, attempts FROM webhook_events")
    }
    assert attempts[mixed_events["unrecognised"]] == 2
    assert attempts[mixed_events["unmapped"]] == 1  # untouched

    none = client.post("/admin/api/events/replay-quarantined", params={"reason": "no_such_reason"}, headers=ADMIN)
    assert none.json() == {"replayed": 0, "results": []}


def test_replay_quarantined_releases_leads_whose_cause_was_fixed(client, ctx, clock):
    # After reactivating the tenant, one call releases every lead that was waiting for it.
    with ctx.db.transaction() as conn:  # there is no API to (de)activate a tenant; flip the flag directly
        conn.execute("UPDATE tenants SET is_active = 0 WHERE id = 'tnt_blueline'")
    for n in (1, 2):
        send(client, ads_payload(Email=f"h{n}@example.com", PhoneNumber=f"512555000{n}", ALAccountId=BLUELINE_ACCOUNT))
    send(client, ads_payload(ALAccountId=UNMAPPED_ACCOUNT))  # held for a different reason
    with ctx.db.transaction() as conn:
        conn.execute("UPDATE tenants SET is_active = 1 WHERE id = 'tnt_blueline'")

    body = client.post(
        "/admin/api/events/replay-quarantined", params={"reason": "tenant_inactive"}, headers=ADMIN
    ).json()

    assert body["replayed"] == 2
    assert [r["result"] for r in body["results"]] == ["processed", "processed"]
    assert db_one(ctx, "SELECT COUNT(*) AS n FROM leads WHERE tenant_id = 'tnt_blueline'")["n"] == 2
    assert event_ids(client, status="quarantined") != []  # the unmapped one is still waiting


def test_replay_quarantined_with_nothing_waiting(client):
    # Nothing to do is a normal answer.
    response = client.post("/admin/api/events/replay-quarantined", headers=ADMIN)
    assert response.status_code == 200
    assert response.json() == {"replayed": 0, "results": []}


# --- alerts -----------------------------------------------------------------------------------


def test_alerts_list_is_empty_when_all_is_well(client):
    # No alerts on a healthy system.
    assert get(client, "/admin/api/alerts").json() == {"alerts": []}


def test_alerts_list_shows_an_alert_raised_by_intake(client, ctx, clock):
    # A quarantined lead raises an alert the operator can read through the API.
    send(client, ads_payload(ALAccountId=UNMAPPED_ACCOUNT))

    alerts = get(client, "/admin/api/alerts").json()["alerts"]
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert["kind"] == "unmapped_account"
    assert alert["dedupe_key"] == f"unmapped_account:{UNMAPPED_ACCOUNT}"
    assert alert["severity"] == "critical"
    assert alert["status"] == "open"
    assert alert["count"] == 1
    assert UNMAPPED_ACCOUNT in alert["title"]
    assert alert["detail"]["external_account_id"] == UNMAPPED_ACCOUNT
    assert alert["detail"]["reason"] == "unmapped_account"
    assert alert["first_seen_at"] == iso(clock())
    assert "detail_json" not in alert


def test_alerts_list_status_filter_and_ordering(client, ctx, clock):
    # Open alerts first (worst first); ?status= narrows to open or resolved.
    send(client, ads_payload(ALAccountId=UNMAPPED_ACCOUNT))  # critical: unmapped account
    clock.advance(minutes=1)
    assert post_lead(client, "not json").status_code == 400  # warning: invalid payload
    all_alerts = get(client, "/admin/api/alerts").json()["alerts"]
    assert [a["severity"] for a in all_alerts] == ["critical", "warning"]
    warning_id = all_alerts[1]["id"]

    assert client.post(f"/admin/api/alerts/{warning_id}/resolve", headers=ADMIN).status_code == 200
    open_only = get(client, "/admin/api/alerts", status="open").json()["alerts"]
    resolved_only = get(client, "/admin/api/alerts", status="resolved").json()["alerts"]
    assert [a["kind"] for a in open_only] == ["unmapped_account"]
    assert [a["kind"] for a in resolved_only] == ["invalid_payload"]
    assert [a["status"] for a in get(client, "/admin/api/alerts").json()["alerts"]] == ["open", "resolved"]


def test_resolve_alert_closes_it_once(client, ctx, clock):
    # Acknowledging an alert resolves it; doing it again is "no open alert with that id".
    send(client, ads_payload(ALAccountId=UNMAPPED_ACCOUNT))
    alert_id = get(client, "/admin/api/alerts").json()["alerts"][0]["id"]
    clock.advance(minutes=4)

    first = client.post(f"/admin/api/alerts/{alert_id}/resolve", headers=ADMIN)
    assert first.status_code == 200
    assert first.json() == {"resolved": True}
    stored = db_one(ctx, "SELECT status, resolved_at FROM alerts WHERE id = ?", (alert_id,))
    assert stored == {"status": "resolved", "resolved_at": iso(clock())}

    second = client.post(f"/admin/api/alerts/{alert_id}/resolve", headers=ADMIN)
    assert second.status_code == 404


def test_resolve_alert_404_for_an_unknown_id_and_422_for_a_non_numeric_one(client):
    # Unknown ids are not found; ids are integers.
    assert client.post("/admin/api/alerts/424242/resolve", headers=ADMIN).status_code == 404
    assert client.post("/admin/api/alerts/abc/resolve", headers=ADMIN).status_code == 422


def test_a_resolved_alert_reopens_as_a_new_alert_when_the_problem_recurs(client, ctx, clock):
    # Resolving is an acknowledgement, not a mute: the next occurrence alerts again.
    send(client, ads_payload(ALAccountId=UNMAPPED_ACCOUNT))
    alert_id = get(client, "/admin/api/alerts").json()["alerts"][0]["id"]
    client.post(f"/admin/api/alerts/{alert_id}/resolve", headers=ADMIN)
    clock.advance(hours=1)
    send(client, ads_payload(ALAccountId=UNMAPPED_ACCOUNT))

    alerts = get(client, "/admin/api/alerts").json()["alerts"]
    assert [(a["status"], a["count"]) for a in alerts] == [("open", 1), ("resolved", 1)]
    assert alerts[0]["id"] != alert_id


# --- cross-site requests ----------------------------------------------------------------------
# A browser that has logged in to the dashboard attaches the same credentials to requests
# for the API, including a POST fired by a form on somebody else's page.


def open_alert_id(client) -> int:
    send(client, ads_payload(ALAccountId=UNMAPPED_ACCOUNT))
    return get(client, "/admin/api/alerts").json()["alerts"][0]["id"]


@pytest.mark.parametrize("origin", ["https://evil.example", "null", "http://testserver.evil.example", "http://evil.example/testserver"])
def test_cross_site_post_is_refused_even_with_valid_credentials(client, ctx, origin):
    # The forged form carries the operator's cached Basic credentials; the Origin gives it away.
    alert_id = open_alert_id(client)
    forged = {**basic("admin", ADMIN_TOKEN), "Origin": origin, "Content-Type": "application/x-www-form-urlencoded"}

    response = client.post(f"/admin/api/alerts/{alert_id}/resolve", headers=forged)

    assert response.status_code == 403
    assert db_one(ctx, "SELECT status FROM alerts WHERE id = ?", (alert_id,)) == {"status": "open"}


def test_every_admin_post_refuses_a_cross_site_origin(client, probe):
    # Not just the routes someone remembered: every state-changing route under /admin/api.
    forged = {**basic("admin", ADMIN_TOKEN), "Origin": "https://evil.example"}
    posts = [path for method, path in protected_routes(client.app) if method == "POST" and path.startswith("/admin/api")]
    assert len(posts) >= 9
    for path in posts:
        assert probe.post(path, headers=forged).status_code == 403, path


def test_same_origin_browser_posts_and_scripts_without_an_origin_are_accepted(client, ctx):
    # The dashboard's own pages name this host as Origin; curl and scripts send no Origin at all.
    alert_id = open_alert_id(client)
    same_origin = {**basic("admin", ADMIN_TOKEN), "Origin": "http://testserver"}
    assert client.post(f"/admin/api/alerts/{alert_id}/resolve", headers=same_origin).status_code == 200
    assert client.post("/admin/api/events/replay-quarantined", headers=ADMIN).status_code == 200


def test_reading_is_not_restricted_by_origin(client):
    # Only state-changing requests are checked; a GET changes nothing.
    response = client.get("/admin/api/leads", headers={**ADMIN, "Origin": "https://evil.example"})
    assert response.status_code == 200


# --- /admin/api/health ------------------------------------------------------------------------


def test_health_structure_on_a_healthy_system(client):
    # One entry per check, each with the same four fields, and an overall verdict.
    response = get(client, "/admin/api/health")
    assert response.status_code == 200
    body = response.json()

    assert set(body) == {"status", "worker_running", "checks"}
    assert body["status"] == "ok"
    assert body["worker_running"] is False  # tests do not start the worker thread
    names = [check["name"] for check in body["checks"]]
    assert names[:5] == ["ingest_errors", "unprocessed", "quarantine", "email_backlog", "email_failures"]
    assert names[-1] == "canary"
    assert "lead_silence:sparkle-home-cleaning" in names
    for check in body["checks"]:
        assert set(check) == {"name", "status", "summary", "detail"}
        assert check["status"] == "ok"
        assert isinstance(check["summary"], str) and check["summary"]
        assert isinstance(check["detail"], dict)


def test_health_overall_status_is_warning_when_something_is_quarantined(client):
    # A lead waiting on a human is a warning.
    send(client, ads_payload(ALAccountId=UNMAPPED_ACCOUNT))
    body = get(client, "/admin/api/health").json()

    assert body["status"] == "warning"
    by_name = {check["name"]: check for check in body["checks"]}
    assert by_name["quarantine"]["status"] == "warning"
    assert by_name["quarantine"]["detail"] == {"count": 1, "by_reason": {"unmapped_account": 1}}
    assert by_name["ingest_errors"]["status"] == "ok"


def test_health_overall_status_is_critical_when_the_first_email_is_late(client, ctx, clock):
    # A breached speed-to-lead SLA is critical and outranks warnings.
    send(client, ads_payload())
    send(client, ads_payload(Email="x@example.com", PhoneNumber="3175550009", ALAccountId=UNMAPPED_ACCOUNT))
    clock.advance(minutes=5)
    body = get(client, "/admin/api/health").json()

    assert body["status"] == "critical"
    by_name = {check["name"]: check["status"] for check in body["checks"]}
    assert by_name["email_backlog"] == "critical"
    assert by_name["quarantine"] == "warning"

    ctx.worker.deliver_due()
    assert get(client, "/admin/api/health").json()["status"] == "warning"


def test_health_reports_a_running_worker(client, ctx):
    # worker_running reflects the real thread.
    ctx.settings.outbox_poll_seconds = 60  # one idle pass, then it waits to be woken
    ctx.worker.start()
    try:
        assert get(client, "/admin/api/health").json()["worker_running"] is True
    finally:
        ctx.worker.stop()
    assert get(client, "/admin/api/health").json()["worker_running"] is False


# --- /admin/api/payload-shapes ----------------------------------------------------------------


def test_payload_shapes_is_empty_before_any_traffic(client):
    # Nothing received, nothing recorded.
    assert get(client, "/admin/api/payload-shapes").json() == {"shapes": []}


def test_payload_shapes_records_each_clean_format_once_with_a_counter(client, ctx, clock):
    # Documented payloads collapse into one "clean" shape per format, counted.
    first = send(client, ads_payload(Email="a@example.com", PhoneNumber="3175550001"))
    clock.advance(minutes=1)
    send(client, ads_payload(Email="b@example.com", PhoneNumber="3175550002"))
    clock.advance(minutes=1)
    send(client, leads_payload())

    shapes = {shape["signature"]: shape for shape in get(client, "/admin/api/payload-shapes").json()["shapes"]}
    assert set(shapes) == {"angi_ads:clean", "angi_leads:clean"}
    ads = shapes["angi_ads:clean"]
    assert ads["detected_format"] == "angi_ads"
    assert ads["parse_mode"] == "strict"
    assert ads["is_clean"] == 1
    assert ads["seen_count"] == 2
    assert ads["first_seen_at"] == iso(START)
    assert ads["last_seen_at"] == iso(START.replace(minute=1))
    assert ads["sample_event_id"] == first["eventId"]
    assert ads["detail"] == {"unknown": [], "missing": [], "mistyped": [], "recovered": []}
    assert shapes["angi_leads:clean"]["seen_count"] == 1


def test_payload_shapes_lists_a_drifting_shape_first_with_its_details(client, ctx, clock):
    # A payload with a field we have never seen is recorded as its own shape, shown before clean ones.
    send(client, ads_payload())
    clock.advance(minutes=1)
    drifted = send(client, ads_payload(Email="b@example.com", PhoneNumber="3175550002", BrandNewField="surprise"))
    assert drifted["result"] == "processed"  # still a lead; the drift is only reported

    shapes = get(client, "/admin/api/payload-shapes").json()["shapes"]
    assert [shape["is_clean"] for shape in shapes] == [0, 1]
    drift = shapes[0]
    assert drift["signature"].startswith("angi_ads:")
    assert drift["signature"] != "angi_ads:clean"
    assert drift["detail"]["unknown"] == ["BrandNewField"]
    assert drift["seen_count"] == 1
    assert drift["sample_event_id"] == drifted["eventId"]
    assert "detail_json" not in drift


# --- /healthz, /readyz, /metrics --------------------------------------------------------------


def test_healthz_is_200_without_credentials(client):
    # Liveness is for load balancers: no auth, no data.
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_is_200_when_the_database_is_reachable_and_no_worker_is_expected(client):
    # In the test configuration the worker is switched off on purpose, so its absence is fine.
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_is_503_when_the_worker_should_be_running_but_is_not(client, ctx):
    # If the worker thread is dead, leads are stored but nobody is emailed: not ready.
    ctx.settings.run_background_worker = True
    assert ctx.worker.is_running is False

    response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "problems": ["background worker is not running"]}


def test_readyz_is_200_when_the_expected_worker_is_running(client, ctx):
    # With the worker thread alive the service is ready.
    ctx.settings.run_background_worker = True
    ctx.settings.outbox_poll_seconds = 60
    ctx.worker.start()
    try:
        response = client.get("/readyz")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
    finally:
        ctx.worker.stop()
    assert client.get("/readyz").status_code == 503  # stopped again


def test_readyz_is_503_with_a_reason_when_the_database_is_unreachable(client, ctx, tmp_path, monkeypatch):
    # A broken database path must be reported, not raised.
    monkeypatch.setattr(ctx.db, "path", str(tmp_path / "no-such-directory" / "leads.db"))

    response = client.get("/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unavailable"
    assert len(body["problems"]) == 1
    assert body["problems"][0].startswith("database: ")


def metrics(client) -> str:
    response = client.get("/metrics", headers=ADMIN)
    assert response.status_code == 200
    return response.text


def test_metrics_is_plain_text_with_every_gauge(client):
    # Prometheus text format: HELP and TYPE lines for each gauge, even with no data yet.
    response = client.get("/metrics", headers=ADMIN)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    text = response.text
    for gauge in ("angi_webhook_events", "angi_leads", "angi_outbox_messages", "angi_alerts_open", "angi_check_ok"):
        assert f"# HELP {gauge} " in text
        assert f"# TYPE {gauge} gauge" in text
    assert 'angi_check_ok{check="quarantine"} 1' in text
    assert 'angi_check_ok{check="canary"} 1' in text
    assert "# monitor_last_run never" in text
    assert text.endswith("\n")


def test_metrics_requires_the_admin_token(client, ctx, probe):
    # Volumes per status, open alerts and one line per tenant are business data: the same
    # rule as the admin API. Prometheus sends the token as a bearer token.
    send(client, ads_payload())
    for headers in ({}, AUTH, {"Authorization": "Bearer wrong-token"}):
        response = probe.get("/metrics", headers=headers)
        assert response.status_code == 401
        assert "www-authenticate" in response.headers
        assert "angi_" not in response.text
        assert "sparkle" not in response.text
    assert probe.get("/metrics", headers=ADMIN).status_code == 200
    assert probe.get("/metrics", headers=basic("prometheus", ADMIN_TOKEN)).status_code == 200


def test_metrics_reports_leads_by_status(client, ctx, clock):
    # The gauges follow the data: two new leads, then one contacted and one still new.
    send(client, ads_payload(Email="a@example.com", PhoneNumber="3175550001"))
    send(client, ads_payload(Email="", PhoneNumber="3175550002"))
    text = metrics(client)
    assert 'angi_leads{status="new"} 2' in text.splitlines()
    assert 'angi_webhook_events{status="processed"} 2' in text.splitlines()
    assert 'angi_outbox_messages{status="pending"} 3' in text.splitlines()

    deliver(ctx, clock)
    lines = metrics(client).splitlines()
    assert 'angi_leads{status="new"} 1' in lines
    assert 'angi_leads{status="contacted"} 1' in lines
    assert 'angi_outbox_messages{status="sent"} 1' in lines
    assert 'angi_outbox_messages{status="pending"} 2' in lines


def test_metrics_reports_failing_checks_and_open_alerts(client, ctx, clock):
    # A quarantined lead flips its check to 0 and shows up as an open critical alert.
    send(client, ads_payload(ALAccountId=UNMAPPED_ACCOUNT))
    lines = metrics(client).splitlines()

    assert 'angi_check_ok{check="quarantine"} 0' in lines
    assert 'angi_check_ok{check="unprocessed"} 1' in lines
    assert 'angi_webhook_events{status="quarantined"} 1' in lines
    assert 'angi_alerts_open{severity="critical"} 1' in lines


def test_metrics_shows_when_the_monitor_last_ran(client, ctx, clock):
    # A stale or missing timestamp here means the monitor itself has stopped.
    clock.advance(minutes=3)
    ctx.worker.run_maintenance()
    assert f"# monitor_last_run {iso(clock())}" in metrics(client)


def test_metrics_every_sample_line_is_a_name_labels_and_a_number(client, ctx, clock):
    # Each non-comment line must parse as `name{labels} value` so a scraper accepts the page.
    send(client, ads_payload())
    send(client, ads_payload(ALAccountId=UNMAPPED_ACCOUNT))
    deliver(ctx, clock)
    sample = re.compile(r'^[a-z_]+(\{[a-z_]+="[^"\\\n]*"(,[a-z_]+="[^"\\\n]*")*\})? -?\d+(\.\d+)?$')
    lines = [line for line in metrics(client).splitlines() if line and not line.startswith("#")]
    assert lines
    for line in lines:
        assert sample.match(line), line


def test_public_health_endpoints_need_no_credentials_and_reveal_nothing(client, ctx, clock):
    # /healthz and /readyz are for load balancers: no login, no consumer details, and
    # nothing about which tenants exist or how much traffic they get.
    send(client, ads_payload(Email="private.person@example.com", FirstName="Priscilla"))
    send(client, ads_payload(ALAccountId=UNMAPPED_ACCOUNT))
    deliver(ctx, clock)
    for path in ("/healthz", "/readyz"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
    # What /metrics would say stays behind the token, and even there no consumer appears.
    assert client.get("/metrics").status_code == 401
    text = metrics(client)
    assert "private.person" not in text
    assert "Priscilla" not in text


def test_metrics_and_health_do_not_open_alerts(client, ctx):
    # Scraping is read-only: only the monitor run turns a failing check into an alert.
    send(client, ads_payload(ALAccountId=UNMAPPED_ACCOUNT))
    before = db_rows(ctx, "SELECT dedupe_key FROM alerts ORDER BY id")
    metrics(client)
    get(client, "/admin/api/health")
    assert db_rows(ctx, "SELECT dedupe_key FROM alerts ORDER BY id") == before
    assert [row["dedupe_key"] for row in before] == [f"unmapped_account:{UNMAPPED_ACCOUNT}"]


# --- the generated API reference ---------------------------------------------------------------


def test_api_reference_is_behind_the_admin_token(client, probe):
    # The OpenAPI schema lists every operator route; it is not for anonymous visitors.
    for path in ("/docs", "/openapi.json"):
        assert probe.get(path).status_code == 401, path
        assert probe.get(path, headers=ADMIN).status_code == 200, path
    assert "/admin/api/leads" in probe.get("/openapi.json", headers=ADMIN).json()["paths"]
    assert probe.get("/redoc").status_code == 404


# --- start-up: logging, secrets, demo data (app/main.py) ----------------------------------------


@pytest.mark.parametrize(
    ("path", "logged"),
    [
        ("/webhooks/angi/leads/m59Zd4UvhCsM10uqRzdoFDlH", "/webhooks/angi/leads/[redacted]"),
        ("/webhooks/angi/leads?api_key=SECRET-KEY", "/webhooks/angi/leads?[redacted]"),
        ("/webhooks/angi/leads/m59Zd4Uv?key=SECRET-KEY&x=1", "/webhooks/angi/leads/[redacted]?[redacted]"),
        ("/book/Zm9vYmFyYmF6cXV4", "/book/[redacted]"),
        ("/book/Zm9vYmFyYmF6cXV4?slot=2026-11-13T15:00", "/book/[redacted]?[redacted]"),
        ("/unsubscribe/Zm9vYmFyYmF6cXV4", "/unsubscribe/[redacted]"),
        ("/dashboard/leads?q=alice%40example.com", "/dashboard/leads?[redacted]"),
        ("/webhooks/angi/leads", "/webhooks/angi/leads"),
        ("/webhooks/angi/leads/", "/webhooks/angi/leads/"),
        ("/healthz", "/healthz"),
        ("/dashboard/leads/lead_9f2c4e1ab03d77c5", "/dashboard/leads/lead_9f2c4e1ab03d77c5"),
    ],
)
def test_redact_url_removes_secrets_and_keeps_the_endpoint(path, logged):
    # Some URLs are the credential (a tenant's webhook key, a consumer's booking token), and
    # a query string may carry the API key or a consumer's details.
    assert redact_url(path) == logged


def test_access_log_lines_have_their_secrets_redacted(client, caplog):
    # uvicorn logs every request URL. create_app puts a filter on that logger, so what
    # reaches any handler is already clean.
    access_log = logging.getLogger("uvicorn.access")
    assert sum(isinstance(f, RedactSecretsInAccessLog) for f in access_log.filters) == 1  # added once, not per app
    with caplog.at_level(logging.INFO, logger="uvicorn.access"):
        # The arguments uvicorn passes: client address, method, path with query, HTTP version, status.
        access_log.info(
            '%s - "%s %s HTTP/%s" %d', "203.0.113.9:51234", "POST",
            "/webhooks/angi/leads/m59Zd4UvhCsM10uqRzdoFDlH?api_key=test-angi-key", "1.1", 200,
        )
        access_log.info('%s - "%s %s HTTP/%s" %d', "203.0.113.9:51234", "GET", "/healthz", "1.1", 200)

    lines = [record.getMessage() for record in caplog.records if record.name == "uvicorn.access"]
    assert lines == [
        '203.0.113.9:51234 - "POST /webhooks/angi/leads/[redacted]?[redacted] HTTP/1.1" 200',
        '203.0.113.9:51234 - "GET /healthz HTTP/1.1" 200',
    ]
    assert "m59Zd4UvhCsM10uqRzdoFDlH" not in caplog.text
    assert "test-angi-key" not in caplog.text


def test_httpx_request_logging_is_switched_off(client):
    # httpx logs "HTTP Request: POST <full url>" at INFO, and the Slack webhook URL is a secret.
    # The level is set on that logger itself, so it holds whatever the root logger is set to.
    assert logging.getLogger("httpx").level == logging.WARNING


def production_settings(settings: Settings, **overrides) -> Settings:
    """The test settings as a production configuration with real-looking secrets."""
    values = {
        **settings.model_dump(),
        "app_env": "production",
        "angi_webhook_api_keys": "wh-3f9a1c7e5b2d4086a1c7",
        "admin_token": "adm-7e5b2d40863f9a1c7e5b",
        **overrides,
    }
    return Settings(_env_file=None, **values)


@pytest.mark.parametrize(
    ("overrides", "named_setting"),
    [
        ({"angi_webhook_api_keys": "change-me-webhook-key"}, "ANGI_WEBHOOK_API_KEYS"),  # the .env.example value
        ({"angi_webhook_api_keys": "wh-3f9a1c7e5b2d4086a1c7,Change-Me-Old-Key-000000"}, "ANGI_WEBHOOK_API_KEYS"),
        ({"angi_webhook_api_keys": "short-key"}, "ANGI_WEBHOOK_API_KEYS"),
        ({"angi_webhook_api_keys": "wh-3f9a1c7e5b2d4086a1c7,short"}, "ANGI_WEBHOOK_API_KEYS"),  # every key counts
        ({"admin_token": "change-me-admin-token"}, "ADMIN_TOKEN"),
        ({"admin_token": "fifteen-chars-x"}, "ADMIN_TOKEN"),
        ({"admin_token": "                    "}, "ADMIN_TOKEN"),  # twenty spaces are not a secret
        ({"admin_token": "your-admin-token-goes-here"}, "ADMIN_TOKEN"),
    ],
)
def test_production_refuses_placeholder_and_short_secrets(settings, overrides, named_setting):
    # A deploy that copied .env.example, or typed something guessable, must not serve traffic.
    with pytest.raises(RuntimeError, match=named_setting):
        check_configuration(production_settings(settings, **overrides))


def test_production_starts_with_real_secrets_and_development_does_not_judge_them(settings):
    # Sixteen characters is the minimum; development keeps its short, convenient values.
    assert check_configuration(production_settings(settings)) is None
    assert check_configuration(production_settings(settings, admin_token="sixteen-chars-xx")) is None
    relaxed = production_settings(
        settings, app_env="development", angi_webhook_api_keys="change-me-webhook-key", admin_token="x"
    )
    assert check_configuration(relaxed) is None


@pytest.mark.parametrize(
    ("from_address", "smtp_username", "expected"),
    [
        ("leads@acme.example", "me@gmail.com", "leads@acme.example"),  # an explicit From wins
        ("", "me@gmail.com", "me@gmail.com"),  # Gmail only sends as the account that logged in
        ("", "", "leads@example.com"),  # nothing configured: a reserved address nobody owns
    ],
)
def test_sender_address_falls_back_to_the_smtp_account(settings, from_address, smtp_username, expected):
    # EMAIL_FROM_ADDRESS is optional; the address mail is sent from is worked out in one place.
    configured = Settings(
        _env_file=None,
        **{**settings.model_dump(), "email_from_address": from_address, "smtp_username": smtp_username},
    )
    assert configured.sender_address == expected


def test_demo_tenants_are_not_seeded_in_production(settings, mailer, clock):
    # The demo tenants own the account ids from Angi's public documentation. In production
    # the documentation example must be quarantined, not emailed as "Sparkle Home Cleaning".
    production = production_settings(settings, seed_demo_data=True)
    context = build_context(production, email_provider=mailer, clock=clock)
    with TestClient(create_app(context=context)) as production_client:
        assert db_rows(context, "SELECT id FROM tenants") == []
        response = post_lead(
            production_client, ads_payload(), headers={"X-API-KEY": production.angi_webhook_api_keys}
        )
    assert response.status_code == 200
    assert (response.json()["result"], response.json()["reason"]) == ("quarantined", "unmapped_account")
    assert db_rows(context, "SELECT id FROM leads") == []
    assert db_rows(context, "SELECT id FROM outbox_messages") == []


def test_demo_tenants_are_seeded_in_development_only_when_asked(settings, mailer, clock, tmp_path):
    # SEED_DEMO_DATA=true in development creates the four demo tenants; false creates none.
    seeded = build_context(settings, email_provider=mailer, clock=clock)
    create_app(context=seeded)
    assert len(db_rows(seeded, "SELECT id FROM tenants")) == 4

    empty_settings = Settings(
        _env_file=None,
        **{**settings.model_dump(), "seed_demo_data": False, "database_path": str(tmp_path / "empty.db")},
    )
    empty = build_context(empty_settings, email_provider=mailer, clock=clock)
    create_app(context=empty)
    assert db_rows(empty, "SELECT id FROM tenants") == []
