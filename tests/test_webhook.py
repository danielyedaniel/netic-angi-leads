"""The webhook endpoint: authentication, Angi's delivery contract and raw capture.

Covers app/routes/webhooks.py and app/security.py through the real HTTP stack. What
happens to a stored request afterwards (idempotency, retries, drift) is in
test_ingest.py; which tenant a lead lands on is in test_tenants.py.
"""

from __future__ import annotations

import base64
import hashlib
import json

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.config import Settings
from app.context import build_context
from app.main import check_configuration, create_app
from app.security import webhook_auth_ok
from app.util import iso
from tests.conftest import (
    API_KEY,
    AUTH,
    START,
    ads_payload,
    db_one,
    db_rows,
    leads_payload,
    post_lead,
)

WEBHOOK = "/webhooks/angi/leads"


# --- helpers -----------------------------------------------------------------------------


def custom_settings(settings: Settings, **overrides) -> Settings:
    """The test settings with some values replaced (never reads the environment)."""
    return Settings(_env_file=None, **{**settings.model_dump(), **overrides})


@pytest.fixture
def make_client(settings, mailer, clock):
    """Build a second application whose settings differ from the default fixture."""
    opened: list[TestClient] = []

    def build(**overrides):
        ctx = build_context(custom_settings(settings, **overrides), email_provider=mailer, clock=clock)
        client = TestClient(create_app(context=ctx))
        client.__enter__()
        opened.append(client)
        return ctx, client

    yield build
    for client in opened:
        client.__exit__(None, None, None)


def count(ctx, table: str, where: str = "1 = 1", params: tuple = ()) -> int:
    return db_one(ctx, f"SELECT COUNT(*) AS n FROM {table} WHERE {where}", params)["n"]


def database_dump(ctx) -> str:
    """Every row of every table except `tenants`, as one string to search for secrets."""
    tables = [
        row["name"]
        for row in db_rows(ctx, "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")
    ]
    return json.dumps({t: db_rows(ctx, f"SELECT * FROM {t}") for t in tables if t != "tenants"}, default=str)


def basic(username: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()


def webhook_key(ctx, tenant_id: str) -> str:
    return db_one(ctx, "SELECT webhook_key FROM tenants WHERE id = ?", (tenant_id,))["webhook_key"]


def fake_request(headers: dict[str, str] | None = None, query: str = "") -> Request:
    """A bare Starlette request, for calling app.security functions directly."""
    scope = {
        "type": "http",
        "method": "POST",
        "path": WEBHOOK,
        "query_string": query.encode(),
        "headers": [(name.lower().encode(), value.encode()) for name, value in (headers or {}).items()],
    }
    return Request(scope)


# Every way the shared key may be presented: name -> (headers, query parameters).
KEY_TRANSPORTS: dict[str, tuple[dict[str, str], dict[str, str]]] = {
    "X-API-KEY header": ({"X-API-KEY": API_KEY}, {}),
    "lowercase x-api-key header": ({"x-api-key": API_KEY}, {}),
    "Api-Key header": ({"Api-Key": API_KEY}, {}),
    "X-Angi-Api-Key header": ({"X-Angi-Api-Key": API_KEY}, {}),
    "X-Webhook-Key header": ({"X-Webhook-Key": API_KEY}, {}),
    "Authorization Bearer": ({"Authorization": f"Bearer {API_KEY}"}, {}),
    "lowercase bearer scheme": ({"authorization": f"bearer {API_KEY}"}, {}),
    "HTTP Basic password": ({"Authorization": basic("angi", API_KEY)}, {}),
    "HTTP Basic username": ({"Authorization": basic(API_KEY, "")}, {}),
    # Some senders put the bare key in Authorization, with no scheme (app/security.py accepts it).
    "Authorization without a scheme": ({"Authorization": API_KEY}, {}),
    "api_key query parameter": ({}, {"api_key": API_KEY}),
    "apikey query parameter": ({}, {"apikey": API_KEY}),
    "key query parameter": ({}, {"key": API_KEY}),
}


# --- authentication on the shared URL ----------------------------------------------------


def test_request_without_api_key_is_rejected_with_an_error_body(client, ctx):
    # Angi only treats 200 + "success" as delivered, so a 401 must say "error".
    response = client.post(WEBHOOK, json=ads_payload())

    assert response.status_code == 401
    body = response.json()
    assert body["status"] == "error"
    assert body["result"] == "unauthorized"
    assert "X-API-KEY" in body["error"]
    assert count(ctx, "leads") == 0


def test_request_with_wrong_api_key_is_rejected(client, ctx):
    # A wrong key is treated exactly like no key.
    response = post_lead(client, ads_payload(), headers={"X-API-KEY": "not-the-key"})

    assert response.status_code == 401
    assert response.json()["status"] == "error"
    assert count(ctx, "webhook_events") == 0


@pytest.mark.parametrize("transport", list(KEY_TRANSPORTS))
def test_api_key_is_accepted_however_it_is_sent(client, ctx, transport):
    # Angi does not document how it sends the key, so every common placement works.
    headers, params = KEY_TRANSPORTS[transport]

    response = client.post(WEBHOOK, json=ads_payload(), headers=headers, params=params)

    assert response.status_code == 200
    assert response.json()["status"] == "success"
    assert response.json()["result"] == "processed"
    assert count(ctx, "leads") == 1


@pytest.mark.parametrize("transport", list(KEY_TRANSPORTS))
def test_stored_event_never_contains_the_api_key(client, ctx, transport):
    # Credentials must never be written to the database, whichever way they arrived.
    headers, params = KEY_TRANSPORTS[transport]

    response = client.post(WEBHOOK, json=ads_payload(), headers=headers, params=params)
    assert response.status_code == 200

    event = db_one(ctx, "SELECT * FROM webhook_events")
    stored_headers = json.loads(event["headers_json"])
    dump = database_dump(ctx)
    for name, value in headers.items():
        assert stored_headers[name.lower()] == "[redacted]"
        assert value not in dump  # also covers the base64 form used by HTTP Basic
    assert API_KEY not in dump


@pytest.mark.parametrize(
    "headers, params",
    [
        ({"X-API-KEY": "wrong"}, {}),
        ({"Authorization": "Bearer wrong"}, {}),
        ({"Authorization": basic("angi", "wrong")}, {}),
        ({}, {"api_key": "wrong"}),
        ({"X-Some-Other-Header": API_KEY}, {}),  # right key, header we do not read
        ({}, {"token": API_KEY}),  # right key, query parameter we do not read
    ],
)
def test_wrong_key_is_rejected_in_every_position(client, ctx, headers, params):
    # Each accepted placement really checks the value.
    response = client.post(WEBHOOK, json=ads_payload(), headers=headers, params=params)

    assert response.status_code == 401
    assert count(ctx, "webhook_events") == 0


@pytest.mark.parametrize(
    "candidate",
    [
        API_KEY[:-1],  # a prefix of the real key
        API_KEY + "x",  # the real key plus one character
        "x" + API_KEY,
        API_KEY[:4],
        API_KEY.upper(),
        API_KEY.replace("-", ""),
        f"{API_KEY},{API_KEY}",
        "",
    ],
)
def test_key_comparison_is_exact_not_prefix_based(client, ctx, candidate):
    # Only the whole key matches: no prefix, extension or case-insensitive match.
    response = post_lead(client, ads_payload(), headers={"X-API-KEY": candidate})

    assert response.status_code == 401
    assert count(ctx, "webhook_events") == 0


@pytest.mark.parametrize(
    "authorization",
    [
        "Basic !!!not-base64!!!",
        "Basic " + base64.b64encode(b"\xff\xfe\xfd").decode(),  # decodes to invalid UTF-8
        "Basic",
        "Bearer",
        "Bearer ",
        f"Digest {API_KEY}",
        "not-the-key",  # a bare value with no scheme is read as a key, and this is not it
    ],
)
def test_malformed_authorization_header_is_rejected_without_crashing(client, ctx, authorization):
    # Garbage credentials are a 401, never a 500.
    response = post_lead(client, ads_payload(), headers={"Authorization": authorization})

    assert response.status_code == 401
    assert response.json()["status"] == "error"


def test_any_one_valid_credential_is_enough(client):
    # A sender that puts junk in one place and the key in another is still accepted.
    response = client.post(
        WEBHOOK, json=ads_payload(), headers={"X-API-KEY": "junk"}, params={"api_key": API_KEY}
    )

    assert response.status_code == 200


def test_key_surrounded_by_whitespace_is_accepted(client):
    # Copy-paste whitespace around the key in a header does not lock Angi out.
    response = post_lead(client, ads_payload(), headers={"X-API-KEY": f"  {API_KEY} "})

    assert response.status_code == 200


def test_key_rotation_both_configured_keys_work_and_a_third_does_not(make_client):
    # Two comma-separated keys are accepted side by side so a key can be rotated.
    ctx, client = make_client(angi_webhook_api_keys="old-key, new-key")

    assert post_lead(client, ads_payload(), headers={"X-API-KEY": "old-key"}).status_code == 200
    assert post_lead(client, ads_payload(), headers={"X-API-KEY": "new-key"}).status_code == 200
    assert post_lead(client, ads_payload(), headers={"X-API-KEY": "third-key"}).status_code == 401
    # The raw setting string is not itself a key.
    assert post_lead(client, ads_payload(), headers={"X-API-KEY": "old-key, new-key"}).status_code == 401
    assert post_lead(client, ads_payload(), headers={"X-API-KEY": "old-key,new-key"}).status_code == 401
    assert post_lead(client, ads_payload(), headers={"X-API-KEY": API_KEY}).status_code == 401
    assert count(ctx, "leads") == 2


def test_with_no_keys_configured_the_webhook_is_open_in_development(make_client):
    # Local development works without any secret (a startup warning is logged).
    ctx, client = make_client(angi_webhook_api_keys="", app_env="development")

    response = client.post(WEBHOOK, json=ads_payload())

    assert response.status_code == 200
    assert response.json()["result"] == "processed"
    assert count(ctx, "leads") == 1


def test_with_no_keys_configured_the_webhook_is_closed_in_production(make_client):
    # Production never falls back to "accept anyone". The app refuses to start like
    # this (see the check_configuration tests), so the keys are removed after start-up.
    ctx, client = make_client(app_env="production")
    ctx.settings.angi_webhook_api_keys = ""

    assert client.post(WEBHOOK, json=ads_payload()).status_code == 401
    assert post_lead(client, ads_payload(), headers=AUTH).status_code == 401
    assert count(ctx, "webhook_events") == 0


def test_production_with_a_key_accepts_only_that_key(make_client):
    # Production behaves like development once a key is configured.
    _, client = make_client(app_env="production")

    assert post_lead(client, ads_payload(), headers=AUTH).status_code == 200
    assert client.post(WEBHOOK, json=ads_payload()).status_code == 401


@pytest.mark.parametrize(
    "app_env, keys, headers, expected",
    [
        ("development", "", {}, True),  # open
        ("development", " , ", {}, True),  # only separators = no keys
        ("production", "", {}, False),
        ("production", "", {"X-API-KEY": API_KEY}, False),
        ("production", " , ", {"X-API-KEY": ""}, False),
        ("PRODUCTION", "", {}, False),
        ("prod", "", {}, False),
        ("production", API_KEY, {"X-API-KEY": API_KEY}, True),
        ("production", API_KEY, {}, False),
        ("development", API_KEY, {}, False),  # a configured key is enforced in development too
        ("development", API_KEY, {"X-API-KEY": API_KEY}, True),
    ],
)
def test_webhook_auth_ok_decision_table(settings, app_env, keys, headers, expected):
    # No keys: open in development, closed in production. Keys: always enforced.
    configured = custom_settings(settings, app_env=app_env, angi_webhook_api_keys=keys)

    assert webhook_auth_ok(configured, fake_request(headers)) is expected


def test_webhook_auth_ok_reads_the_query_string(settings):
    # Senders that can only be given a URL put the key in the query string.
    assert webhook_auth_ok(settings, fake_request(query=f"api_key={API_KEY}")) is True
    assert webhook_auth_ok(settings, fake_request(query="api_key=nope")) is False


# --- start-up configuration check --------------------------------------------------------


@pytest.mark.parametrize(
    "overrides, named_setting",
    [
        ({"angi_webhook_api_keys": ""}, "ANGI_WEBHOOK_API_KEYS"),
        ({"angi_webhook_api_keys": " , "}, "ANGI_WEBHOOK_API_KEYS"),
        ({"admin_token": ""}, "ADMIN_TOKEN"),
        ({"fallback_tenant_slug": "sparkle-home-cleaning"}, "FALLBACK_TENANT_SLUG"),
    ],
)
def test_production_refuses_to_start_with_an_unsafe_configuration(settings, overrides, named_setting):
    # Missing secrets or a fallback tenant must stop a production start, naming the setting.
    unsafe = custom_settings(settings, app_env="production", **overrides)

    with pytest.raises(RuntimeError, match=named_setting):
        check_configuration(unsafe)


def test_production_reports_every_problem_at_once(settings):
    # One restart should be enough to see everything that is wrong.
    unsafe = custom_settings(
        settings, app_env="production", angi_webhook_api_keys="", admin_token="", fallback_tenant_slug="x"
    )

    with pytest.raises(RuntimeError) as raised:
        check_configuration(unsafe)

    message = str(raised.value)
    assert "ANGI_WEBHOOK_API_KEYS" in message
    assert "ADMIN_TOKEN" in message
    assert "FALLBACK_TENANT_SLUG" in message


def test_production_starts_with_a_complete_configuration(settings):
    # Keys + admin token + no fallback tenant is a valid production configuration.
    assert check_configuration(custom_settings(settings, app_env="production")) is None


def test_development_starts_even_with_nothing_configured(settings):
    # Development only warns.
    relaxed = custom_settings(
        settings, app_env="development", angi_webhook_api_keys="", admin_token="", fallback_tenant_slug="x"
    )

    assert check_configuration(relaxed) is None


def test_create_app_raises_in_production_without_secrets(settings):
    # The application factory itself fails, so a misconfigured deploy never serves traffic.
    unsafe = custom_settings(settings, app_env="production", angi_webhook_api_keys="")

    with pytest.raises(RuntimeError, match="Refusing to start in production"):
        create_app(unsafe)


# --- what a rejected request leaves behind -----------------------------------------------


def test_rejected_requests_store_nothing_and_open_one_alert_that_counts_repeats(client, ctx):
    # Unauthenticated bodies are not stored, but a burst of 401s is one alert with a counter.
    for _ in range(3):
        assert client.post(WEBHOOK, json=ads_payload()).status_code == 401

    assert count(ctx, "webhook_events") == 0
    assert count(ctx, "leads") == 0
    alerts = db_rows(ctx, "SELECT * FROM alerts")
    assert len(alerts) == 1
    assert alerts[0]["kind"] == "auth_failures"
    assert alerts[0]["status"] == "open"
    assert alerts[0]["severity"] == "warning"
    assert alerts[0]["count"] == 3

    assert post_lead(client, ads_payload(), headers={"X-API-KEY": "wrong"}).status_code == 401
    assert db_one(ctx, "SELECT count FROM alerts WHERE kind = 'auth_failures'")["count"] == 4
    assert count(ctx, "alerts") == 1


def test_auth_failure_alert_records_header_names_but_no_values(client, ctx):
    # The alert helps diagnose "Angi changed how it sends the key" without storing secrets.
    client.post(WEBHOOK, json=ads_payload(), headers={"X-New-Angi-Secret": "s3cr3t-value", "User-Agent": "AngiBot/2"})

    alert = db_one(ctx, "SELECT * FROM alerts WHERE kind = 'auth_failures'")
    detail = json.loads(alert["detail_json"])
    assert "x-new-angi-secret" in detail["header_names"]
    assert detail["user_agent"] == "AngiBot/2"
    assert "s3cr3t-value" not in database_dump(ctx)


def test_accepted_request_after_failures_does_not_touch_the_alert(client, ctx):
    # A successful delivery is not counted as an auth failure.
    client.post(WEBHOOK, json=ads_payload())
    post_lead(client, ads_payload())

    assert db_one(ctx, "SELECT count FROM alerts WHERE kind = 'auth_failures'")["count"] == 1
    assert count(ctx, "webhook_events") == 1


# --- tenant-keyed URL --------------------------------------------------------------------


def test_tenant_url_needs_no_api_key_and_routes_to_that_tenant(client, ctx):
    # The secret URL issued to a tenant is itself the credential and the routing.
    key = webhook_key(ctx, "tnt_summit")

    response = post_lead(client, ads_payload(ALAccountId="200001"), headers={}, path=f"{WEBHOOK}/{key}")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["result"] == "processed"
    assert body["tenant"] == {"id": "tnt_summit", "name": "Summit Heating & Air", "routedBy": "url_key"}
    event = db_one(ctx, "SELECT * FROM webhook_events")
    assert event["url_tenant_id"] == "tnt_summit"
    assert event["tenant_id"] == "tnt_summit"
    assert db_one(ctx, "SELECT tenant_id FROM leads")["tenant_id"] == "tnt_summit"


def test_tenant_url_ignores_a_wrong_api_key_header(client, ctx):
    # On a tenant URL the header is irrelevant, even if it carries a wrong key.
    key = webhook_key(ctx, "tnt_blueline")

    response = post_lead(
        client, ads_payload(ALAccountId="200002"), headers={"X-API-KEY": "wrong"}, path=f"{WEBHOOK}/{key}"
    )

    assert response.status_code == 200
    assert response.json()["tenant"]["id"] == "tnt_blueline"


def test_unknown_tenant_url_key_is_rejected_and_nothing_is_stored(client, ctx):
    # A guessed URL key is a 401 like a wrong API key, even with the real API key attached.
    response = post_lead(client, ads_payload(), headers=AUTH, path=f"{WEBHOOK}/not-a-real-key")

    assert response.status_code == 401
    assert response.json()["status"] == "error"
    assert response.json()["result"] == "unauthorized"
    assert count(ctx, "webhook_events") == 0
    assert count(ctx, "leads") == 0
    alert = db_one(ctx, "SELECT * FROM alerts WHERE kind = 'auth_failures'")
    assert alert["count"] == 1
    assert "unknown webhook key" in json.loads(alert["detail_json"])["reason"]


def test_tenant_url_key_must_match_exactly(client, ctx):
    # A prefix, an extension or another casing of a real URL key is not accepted.
    key = webhook_key(ctx, "tnt_summit")

    for candidate in (key[:-1], key + "x", key.swapcase()):
        response = post_lead(client, ads_payload(), headers={}, path=f"{WEBHOOK}/{candidate}")
        assert response.status_code == 401, candidate
    assert count(ctx, "webhook_events") == 0


def test_tenant_url_key_is_not_written_to_the_event(client, ctx):
    # The URL key is a credential too; the event only records which tenant it meant.
    key = webhook_key(ctx, "tnt_summit")

    post_lead(client, ads_payload(ALAccountId="200001"), headers={}, path=f"{WEBHOOK}/{key}")

    assert key not in database_dump(ctx)


# --- Angi's delivery contract: 200 + "success" for everything we stored ------------------


def test_new_lead_answers_200_success_processed(client):
    # A brand-new lead.
    response = post_lead(client, ads_payload())

    assert response.status_code == 200
    assert response.json()["status"] == "success"
    assert response.json()["result"] == "processed"
    assert response.json()["leadId"].startswith("lead_")
    assert response.json()["eventId"].startswith("evt_")


def test_exact_redelivery_answers_200_success_duplicate_delivery(client):
    # Angi retrying the same lead must be told "delivered", or it keeps retrying.
    payload = ads_payload()
    first = post_lead(client, payload)

    response = post_lead(client, payload)

    assert response.status_code == 200
    assert response.json()["status"] == "success"
    assert response.json()["result"] == "duplicate_delivery"
    assert response.json()["leadId"] == first.json()["leadId"]


def test_corrected_redelivery_answers_200_success_updated(client):
    # The same lead id with different content is an update, still a success.
    payload = ads_payload()
    first = post_lead(client, payload)

    response = post_lead(client, {**payload, "Description": "Actually, a one-off deep clean."})

    assert response.status_code == 200
    assert response.json()["status"] == "success"
    assert response.json()["result"] == "updated"
    assert response.json()["leadId"] == first.json()["leadId"]


def test_unmapped_account_answers_200_success_quarantined(client, ctx):
    # A retry cannot fix a missing mapping, so the lead is stored and acknowledged.
    response = post_lead(client, ads_payload(ALAccountId="424242"))

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["result"] == "quarantined"
    assert body["reason"] == "unmapped_account"
    assert body["angiAccountId"] == "424242"
    assert db_one(ctx, "SELECT status FROM webhook_events")["status"] == "quarantined"


@pytest.mark.parametrize("payload", [{"foo": "bar", "baz": 1}, {}, {"lead": {"note": "hello"}}])
def test_unrecognised_payload_answers_200_success_quarantined(client, ctx, payload):
    # JSON we cannot make a lead from is still stored for a human, and acknowledged.
    response = post_lead(client, payload)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["result"] == "quarantined"
    assert body["reason"] == "unrecognised_payload"
    event = db_one(ctx, "SELECT * FROM webhook_events")
    assert event["status"] == "quarantined"
    assert event["status_reason"] == "unrecognised_payload"
    assert count(ctx, "leads") == 0


def test_single_object_array_is_accepted_as_one_lead(client, ctx):
    # A body of [ {lead} ] is unwrapped rather than rejected (and reported as drift).
    response = post_lead(client, [ads_payload()])

    assert response.status_code == 200
    assert response.json()["result"] == "processed"
    assert count(ctx, "leads") == 1


def test_angi_spec_example_that_is_not_valid_json_still_becomes_a_lead(client, ctx):
    # The example in Angi's own spec has curly quotes, an unquoted email and a missing
    # comma; it is salvaged instead of being answered with 400.
    body = """{
“FirstName”: “Bob”,
“LastName”: “Builder”,
“PhoneNumber”: “5554332646”,
“PostalAddress”: {
“AddressFirstLine”: “123 Main St.”,
“AddressSecondLine”: “”,
“City”: “Indianapolis”,
“State”: “IN”,
“PostalCode”: “46203”
},
“Email”: bob.builder@gmail.com,
“Source”: “Angie’s List Quote Request”,
“Description”: “I’m Looking for recurring house cleaning services please.”,
“Category”: “Indianapolis – House Cleaning”,
“Urgency”: “This Week”,
“CorrelationId”: “61a7de56-dba3-4e59-8e2a-3fa827f84f7f”
“ALAccountId”: “123456”
}"""

    response = post_lead(client, body.encode("utf-8"))

    assert response.status_code == 200
    assert response.json()["status"] == "success"
    assert response.json()["result"] == "processed"
    assert response.json()["parseMode"] == "tolerant"
    lead = db_one(ctx, "SELECT * FROM leads")
    assert lead["tenant_id"] == "tnt_sparkle"
    assert lead["email_normalized"] == "bob.builder@gmail.com"
    assert lead["external_lead_id"] == "61a7de56-dba3-4e59-8e2a-3fa827f84f7f"
    assert lead["city"] == "Indianapolis"
    assert lead["postal_code"] == "46203"
    assert lead["needs_review"] == 1
    assert db_one(ctx, "SELECT raw_body FROM webhook_events")["raw_body"] == body


# --- 400: nothing usable in the body (still stored) --------------------------------------

UNUSABLE_BODIES = {
    "not json": b"this is not json at all",
    "html error page": b"<html><body>502 Bad Gateway</body></html>",
    "form encoded": b"FirstName=Bob&LastName=Builder",
    "truncated json": b'{"FirstName": "Bob", "LastNa',
    "empty": b"",
    "whitespace only": b"  \r\n\t ",
    "array of two objects": json.dumps([ads_payload(), ads_payload()]).encode(),
    "empty array": b"[]",
    "array of a scalar": b"[1]",
    "json string": b'"hello"',
    "json number": b"42",
    "json null": b"null",
    "json true": b"true",
}


@pytest.mark.parametrize("name", list(UNUSABLE_BODIES))
def test_unusable_body_is_400_error_but_still_stored_as_rejected(client, ctx, name):
    # The one stored case answered with a failure: nothing in the body can become a lead.
    raw = UNUSABLE_BODIES[name]

    response = post_lead(client, raw)

    assert response.status_code == 400
    body = response.json()
    assert body["status"] == "error"
    assert body["result"] == "rejected"
    assert body["error"]
    events = db_rows(ctx, "SELECT * FROM webhook_events")
    assert len(events) == 1
    assert events[0]["id"] == body["eventId"]
    assert events[0]["status"] == "rejected"
    assert events[0]["status_reason"] == body["error"]
    assert events[0]["raw_body"] == raw.decode("utf-8")
    assert events[0]["body_sha256"] == hashlib.sha256(raw).hexdigest()
    assert events[0]["attempts"] == 1
    assert count(ctx, "leads") == 0
    assert count(ctx, "outbox_messages") == 0


def test_rejected_bodies_open_one_invalid_payload_alert_that_counts_repeats(client, ctx):
    # Garbage bodies are worth a warning, but only one alert however many arrive.
    post_lead(client, b"garbage one")
    post_lead(client, b"garbage two")

    alerts = db_rows(ctx, "SELECT * FROM alerts")
    assert len(alerts) == 1
    assert alerts[0]["kind"] == "invalid_payload"
    assert alerts[0]["severity"] == "warning"
    assert alerts[0]["count"] == 2
    assert count(ctx, "webhook_events", "status = 'rejected'") == 2


def test_absurdly_nested_json_is_rejected_like_any_other_unusable_body(client, ctx):
    # A body json.loads cannot parse is "not usable", whatever exception the parser raises.
    response = post_lead(client, b"[" * 100_000)

    assert response.status_code == 400
    assert response.json()["status"] == "error"
    assert db_one(ctx, "SELECT status FROM webhook_events")["status"] == "rejected"
    assert count(ctx, "alerts", "kind = 'processing_error'") == 0


def test_utf8_bom_before_valid_json_is_parsed_as_ordinary_json(client, ctx):
    # A BOM is an encoding detail of the sender, not a change of payload format.
    response = post_lead(client, b"\xef\xbb\xbf" + json.dumps(leads_payload()).encode("utf-8"))

    assert response.status_code == 200
    assert response.json()["result"] == "processed"
    assert response.json()["parseMode"] == "strict"
    lead = db_one(ctx, "SELECT * FROM leads")
    assert lead["needs_review"] == 0
    assert len(json.loads(lead["interview_json"])) == 2
    assert count(ctx, "alerts", "kind = 'schema_drift'") == 0


# --- 413: body too large -----------------------------------------------------------------


def test_body_over_the_limit_with_honest_content_length_is_413_and_not_stored(client, ctx):
    # An oversized body is refused from its Content-Length, before it is read or stored.
    limit = ctx.settings.max_body_bytes
    body = b"x" * (limit + 1)

    response = client.post(WEBHOOK, content=body, headers=AUTH)

    assert response.request.headers["content-length"] == str(limit + 1)
    assert response.status_code == 413
    assert response.json()["status"] == "error"
    assert response.json()["result"] == "rejected"
    assert str(limit) in response.json()["error"]
    assert count(ctx, "webhook_events") == 0


def test_streamed_body_over_the_limit_is_413_and_not_stored(client, ctx):
    # Without a Content-Length (chunked upload) the size is enforced while reading.
    limit = ctx.settings.max_body_bytes
    body = b"x" * (limit + 1)

    def chunks():
        for start in range(0, len(body), 64 * 1024):
            yield body[start : start + 64 * 1024]

    response = client.post(WEBHOOK, content=chunks(), headers=AUTH)

    assert "content-length" not in response.request.headers
    assert response.status_code == 413
    assert response.json()["status"] == "error"
    assert count(ctx, "webhook_events") == 0


def test_small_limit_applies_to_real_leads_too(make_client):
    # The limit is the configured one, not a hard-coded number.
    ctx, client = make_client(max_body_bytes=200)

    response = post_lead(client, ads_payload())

    assert response.status_code == 413
    assert count(ctx, "webhook_events") == 0
    assert count(ctx, "leads") == 0


def test_body_of_exactly_the_limit_is_accepted(client, ctx):
    # The limit is inclusive: max_body_bytes itself is still a valid lead.
    limit = ctx.settings.max_body_bytes
    payload = ads_payload(Description="")
    padding = limit - len(json.dumps(payload).encode("utf-8"))
    payload["Description"] = "x" * padding
    body = json.dumps(payload).encode("utf-8")
    assert len(body) == limit

    response = post_lead(client, body)

    assert response.status_code == 200
    assert response.json()["result"] == "processed"
    assert len(db_one(ctx, "SELECT raw_body FROM webhook_events")["raw_body"]) == limit


def test_oversized_body_is_refused_but_not_silently(client, ctx):
    # The body is not stored (that would defeat the limit), so the alert is the only trace
    # that a lead may have been lost; repeats fold into the one alert.
    for _ in range(2):
        response = client.post(WEBHOOK, content=b"x" * (ctx.settings.max_body_bytes + 1), headers=AUTH)
        assert response.status_code == 413

    alerts = db_rows(ctx, "SELECT * FROM alerts")
    assert len(alerts) == 1
    assert (alerts[0]["kind"], alerts[0]["severity"], alerts[0]["count"]) == ("oversized_payload", "warning", 2)
    detail = json.loads(alerts[0]["detail_json"])
    assert detail["limit_bytes"] == ctx.settings.max_body_bytes
    assert int(detail["content_length"]) == ctx.settings.max_body_bytes + 1
    assert count(ctx, "webhook_events") == 0


def test_trailing_slash_on_the_shared_url_is_the_same_endpoint(client, ctx):
    # A sender configured with ".../leads/" must not lose leads to a redirect or be taken
    # for a tenant URL with an empty key.
    response = client.post(WEBHOOK + "/", json=ads_payload(), headers=AUTH, follow_redirects=False)

    assert response.status_code == 200
    assert response.json()["result"] == "processed"
    assert count(ctx, "leads") == 1
    # Still the authenticated endpoint: without the key it is a 401, not an open door.
    assert client.post(WEBHOOK + "/", json=ads_payload(), follow_redirects=False).status_code == 401


def test_oversized_body_on_a_tenant_url_is_413(client, ctx):
    # The size limit applies to tenant-keyed URLs as well.
    key = webhook_key(ctx, "tnt_summit")

    response = client.post(f"{WEBHOOK}/{key}", content=b"x" * (ctx.settings.max_body_bytes + 1))

    assert response.status_code == 413
    assert count(ctx, "webhook_events") == 0


def test_credentials_are_checked_before_the_body_size(client, ctx):
    # An unauthenticated sender learns nothing about limits: 401 comes first.
    response = client.post(WEBHOOK, content=b"x" * (ctx.settings.max_body_bytes + 1))

    assert response.status_code == 401


# --- no 4xx may look like a success ------------------------------------------------------


def _unauthenticated(client, ctx):
    return client.post(WEBHOOK, json=ads_payload())


def _wrong_key(client, ctx):
    return post_lead(client, ads_payload(), headers={"X-API-KEY": "success"})


def _unknown_url_key(client, ctx):
    return post_lead(client, ads_payload(), headers={}, path=f"{WEBHOOK}/success")


def _not_json(client, ctx):
    return post_lead(client, b"success success success")


def _empty(client, ctx):
    return post_lead(client, b"")


def _two_objects(client, ctx):
    return post_lead(client, [ads_payload(Description="success"), ads_payload()])


def _too_large(client, ctx):
    return client.post(WEBHOOK, content=b"x" * (ctx.settings.max_body_bytes + 1), headers=AUTH)


def _put(client, ctx):
    return client.put(WEBHOOK, json=ads_payload(), headers=AUTH)


def _unknown_path(client, ctx):
    return client.post(f"{WEBHOOK}/a/b", json=ads_payload(), headers=AUTH)


@pytest.mark.parametrize(
    "send, expected_status",
    [
        (_unauthenticated, 401),
        (_wrong_key, 401),
        (_unknown_url_key, 401),
        (_not_json, 400),
        (_empty, 400),
        (_two_objects, 400),
        (_too_large, 413),
        (_put, 405),
        (_unknown_path, 404),
    ],
)
def test_no_4xx_body_contains_the_word_success(client, ctx, send, expected_status):
    # Angi looks for "success" in the body; a failure must never contain it.
    response = send(client, ctx)

    assert response.status_code == expected_status
    assert "success" not in response.text.lower()


# --- other methods and content types -----------------------------------------------------


def test_get_on_the_webhook_url_returns_a_hint_not_a_delivery(client, ctx):
    # Opening the URL in a browser explains what it is, without auth and without storing.
    response = client.get(WEBHOOK)

    assert response.status_code == 200
    body = response.json()
    assert "POST" in body["message"]
    assert body["status"] != "success"
    assert "success" not in response.text.lower()
    assert count(ctx, "webhook_events") == 0
    assert count(ctx, "alerts") == 0


@pytest.mark.parametrize("method", ["PUT", "DELETE", "PATCH"])
def test_wrong_http_method_is_405_on_both_urls(client, ctx, method):
    # Only POST delivers a lead.
    key = webhook_key(ctx, "tnt_summit")

    shared = client.request(method, WEBHOOK, json=ads_payload(), headers=AUTH)
    keyed = client.request(method, f"{WEBHOOK}/{key}", json=ads_payload())

    assert shared.status_code == 405
    assert keyed.status_code == 405
    assert count(ctx, "webhook_events") == 0


@pytest.mark.parametrize(
    "content_type",
    [
        "application/x-www-form-urlencoded",
        "text/plain",
        "text/plain; charset=utf-8",
        "application/json; charset=utf-8",
        "application/octet-stream",
        None,  # no Content-Type header at all
    ],
)
def test_valid_json_is_accepted_whatever_the_content_type(client, ctx, content_type):
    # Angi's Content-Type is not guaranteed; the body decides, not the header.
    headers = dict(AUTH)
    if content_type is not None:
        headers["Content-Type"] = content_type

    response = client.post(WEBHOOK, content=json.dumps(ads_payload()).encode("utf-8"), headers=headers)

    assert response.status_code == 200
    assert response.json()["result"] == "processed"
    assert db_one(ctx, "SELECT content_type FROM webhook_events")["content_type"] == content_type
    assert count(ctx, "leads") == 1


# --- raw capture -------------------------------------------------------------------------


def test_event_row_keeps_the_request_byte_for_byte_with_its_metadata(client, ctx, clock):
    # The stored event is the forensic record: exact body, hash, clock time and outcome.
    payload = ads_payload(
        FirstName="Zoë",
        Description="Deep clean – 3 bed / 2 bath, por favor 🙂   (two   spaces kept)",
    )
    raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    assert "–".encode() in raw and "🙂".encode() in raw

    response = post_lead(client, raw)

    assert response.status_code == 200
    body = response.json()
    events = db_rows(ctx, "SELECT * FROM webhook_events")
    assert len(events) == 1
    event = events[0]
    assert event["id"] == body["eventId"]
    assert event["raw_body"].encode("utf-8") == raw
    assert event["body_sha256"] == hashlib.sha256(raw).hexdigest()
    assert event["received_at"] == iso(START)
    assert event["processed_at"] == iso(START)
    assert event["source"] == "angi"
    assert event["status"] == "processed"
    assert event["status_reason"] is None
    assert event["detected_format"] == "angi_ads"
    assert event["parse_mode"] == "strict"
    assert event["tenant_id"] == "tnt_sparkle"
    assert event["lead_id"] == body["leadId"]
    assert event["url_tenant_id"] is None
    assert event["attempts"] == 1
    assert isinstance(event["processing_ms"], float) and event["processing_ms"] >= 0
    assert event["content_type"] == "application/json"

    lead = db_one(ctx, "SELECT * FROM leads WHERE id = ?", (body["leadId"],))
    assert lead["first_name"] == "Zoë"
    assert lead["comments"] == "Deep clean – 3 bed / 2 bath, por favor 🙂   (two   spaces kept)"
    assert lead["market"] == "Indianapolis"
    assert lead["first_event_id"] == event["id"]


def test_received_at_comes_from_the_clock_for_each_request(client, ctx, clock):
    # Each delivery is stamped with the time it arrived.
    post_lead(client, ads_payload())
    clock.advance(minutes=3, seconds=7)
    post_lead(client, ads_payload())

    stamps = [row["received_at"] for row in db_rows(ctx, "SELECT received_at FROM webhook_events ORDER BY received_at")]
    assert stamps == ["2026-11-12T15:00:00.000Z", "2026-11-12T15:03:07.000Z"]


def test_invalid_utf8_inside_a_value_does_not_crash_and_the_lead_is_kept(client, ctx):
    # Mojibake in one field must not cost the lead.
    raw = json.dumps(ads_payload(Description="needs CLEANING soon")).encode("utf-8")
    raw = raw.replace(b"CLEANING", b"CLEAN\xff\xfeING")

    response = post_lead(client, raw)

    assert response.status_code == 200
    assert response.json()["result"] == "processed"
    event = db_one(ctx, "SELECT * FROM webhook_events")
    assert event["body_sha256"] == hashlib.sha256(raw).hexdigest()
    assert event["raw_body"] == raw.decode("utf-8", errors="replace")
    lead = db_one(ctx, "SELECT * FROM leads")
    assert lead["comments"].startswith("needs CLEAN") and lead["comments"].endswith("ING soon")


def test_body_of_only_invalid_utf8_is_a_400_not_a_500(client, ctx):
    # Binary junk is rejected cleanly and still recorded.
    raw = b"\xff\xfe\xfd\x80\x81"

    response = post_lead(client, raw)

    assert response.status_code == 400
    assert response.json()["status"] == "error"
    event = db_one(ctx, "SELECT * FROM webhook_events")
    assert event["status"] == "rejected"
    assert event["body_sha256"] == hashlib.sha256(raw).hexdigest()


def test_sensitive_headers_are_redacted_and_ordinary_ones_kept(client, ctx):
    # Anything that looks like a credential is masked; the rest stays for debugging.
    headers = {
        **AUTH,
        "Cookie": "session=abc123",
        "X-Signature": "sha256=deadbeef",
        "X-Auth-Token": "tok_live_1",
        "Proxy-Authorization": "Basic Zm9vOmJhcg==",
        "X-Client-Secret": "shh",
        "User-Agent": "AngiLeadSender/1.0",
        "X-Request-Id": "req-777",
    }

    post_lead(client, ads_payload(), headers=headers)

    stored = json.loads(db_one(ctx, "SELECT headers_json FROM webhook_events")["headers_json"])
    for name in ("x-api-key", "cookie", "x-signature", "x-auth-token", "proxy-authorization", "x-client-secret"):
        assert stored[name] == "[redacted]", name
    assert stored["user-agent"] == "AngiLeadSender/1.0"
    assert stored["x-request-id"] == "req-777"
    assert stored["content-type"] == "application/json"
    dump = database_dump(ctx)
    for secret in ("abc123", "deadbeef", "tok_live_1", "Zm9vOmJhcg==", "shh"):
        assert secret not in dump


def test_remote_address_prefers_the_first_forwarded_for_entry(client, ctx):
    # Behind ngrok / a load balancer the real sender is the first X-Forwarded-For hop.
    post_lead(client, ads_payload(), headers={**AUTH, "X-Forwarded-For": "203.0.113.7, 10.0.0.1"})

    assert db_one(ctx, "SELECT remote_addr FROM webhook_events")["remote_addr"] == "203.0.113.7"


# --- the webhook never sends email itself ------------------------------------------------


def test_webhook_only_queues_the_email_and_the_worker_sends_it(client, ctx, mailer, clock):
    # A slow mail server must not make Angi time out: the POST only writes an outbox row.
    response = post_lead(client, ads_payload(Email="pat@example.com"))

    assert response.status_code == 200
    assert response.json()["email"]["status"] == "queued"
    assert mailer.sent == []
    first_touch = db_rows(ctx, "SELECT * FROM outbox_messages WHERE sequence = 0")
    assert len(first_touch) == 1
    assert first_touch[0]["status"] == "pending"
    assert first_touch[0]["template"] == "intro"
    assert first_touch[0]["to_address"] == "pat@example.com"
    assert first_touch[0]["send_after"] == iso(clock())

    ctx.worker.deliver_due()
    assert [email.to for email in mailer.sent] == ["pat@example.com"]


def test_broken_mail_server_cannot_fail_the_webhook(client, ctx, mailer):
    # Even with email completely down, the delivery is acknowledged and the lead stored.
    mailer.fail_with = RuntimeError("smtp is down")
    mailer.fail_times = -1

    response = post_lead(client, ads_payload())

    assert response.status_code == 200
    assert response.json()["status"] == "success"
    assert response.json()["result"] == "processed"
    assert count(ctx, "leads") == 1
    assert count(ctx, "outbox_messages", "sequence = 0 AND status = 'pending'") == 1
