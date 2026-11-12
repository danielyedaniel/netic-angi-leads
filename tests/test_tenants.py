"""Tenant mapping (app/tenants.py): which business a lead belongs to.

Three layers: ``resolve_tenant`` branch by branch, the management helpers
(``create_tenant`` / ``add_mapping``), and routing end to end through the webhook and
the admin API.
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.context import build_context
from app.main import create_app
from app.seed import seed_demo
from app.tenants import (
    SOURCE_ANGI,
    add_mapping,
    create_tenant,
    find_mapping,
    list_tenants,
    resolve_tenant,
    valid_timezone,
)
from app.util import iso
from tests.conftest import (
    ADMIN,
    START,
    ads_payload,
    db_one,
    db_rows,
    leads_payload,
    post_lead,
)

WEBHOOK = "/webhooks/angi/leads"
UNMAPPED_ACCOUNT = "555000"  # no tenant has this Angi account id


# --- helpers -----------------------------------------------------------------------------


@pytest.fixture
def make_client(settings, mailer, clock):
    """Build an application whose settings differ from the default fixture."""
    opened: list[TestClient] = []

    def build(**overrides):
        custom = Settings(_env_file=None, **{**settings.model_dump(), **overrides})
        ctx = build_context(custom, email_provider=mailer, clock=clock)
        client = TestClient(create_app(context=ctx))
        client.__enter__()
        opened.append(client)
        return ctx, client

    yield build
    for client in opened:
        client.__exit__(None, None, None)


@pytest.fixture
def conn(ctx, clock):
    """An open write transaction on a database holding the demo tenants (unit tests)."""
    with ctx.db.transaction() as connection:
        seed_demo(connection, clock())
        yield connection


def resolve(conn, clock, **kwargs):
    """``resolve_tenant`` with the arguments a test does not care about filled in."""
    params = {"source": SOURCE_ANGI, "external_account_id": None, "external_account_name": None, "now": clock()}
    params.update(kwargs)
    return resolve_tenant(conn, **params)


def mapping_row(conn, account_id: str, source: str = SOURCE_ANGI) -> dict | None:
    row = conn.execute(
        "SELECT * FROM tenant_lead_sources WHERE source = ? AND external_account_id = ?", (source, account_id)
    ).fetchone()
    return dict(row) if row else None


def scalar(conn, sql: str, params: tuple = ()) -> int:
    return conn.execute(sql, params).fetchone()[0]


def count(ctx, table: str, where: str = "1 = 1", params: tuple = ()) -> int:
    return db_one(ctx, f"SELECT COUNT(*) AS n FROM {table} WHERE {where}", params)["n"]


def webhook_url(ctx, tenant_id: str) -> str:
    key = db_one(ctx, "SELECT webhook_key FROM tenants WHERE id = ?", (tenant_id,))["webhook_key"]
    return f"{WEBHOOK}/{key}"


def stored_mapping(ctx, account_id: str) -> dict | None:
    return db_one(
        ctx, "SELECT * FROM tenant_lead_sources WHERE source = 'angi' AND external_account_id = ?", (account_id,)
    )


def set_active(ctx, tenant_id: str, active: bool) -> None:
    with ctx.db.transaction() as connection:
        connection.execute("UPDATE tenants SET is_active = ? WHERE id = ?", (1 if active else 0, tenant_id))


def leads_payload_without_account(**overrides):
    payload = leads_payload(**overrides)
    del payload["spEntityId"]
    return payload


# --- resolve_tenant, branch by branch ----------------------------------------------------


def test_resolve_by_account_id(conn, clock):
    # The everyday path: the Angi account id in the payload is mapped to a tenant.
    routing = resolve(conn, clock, external_account_id="123456")

    assert routing.tenant["id"] == "tnt_sparkle"
    assert routing.routed_by == "account_id"
    assert routing.reason is None
    assert routing.mapping["external_account_id"] == "123456"
    assert routing.mapping["default_fee_cents"] == 2800
    assert routing.conflict is None
    assert routing.learned_mapping is False
    assert routing.notes == []


def test_resolve_unknown_account_is_unmapped(conn, clock):
    # We never guess which business a consumer contacted.
    routing = resolve(conn, clock, external_account_id=UNMAPPED_ACCOUNT)

    assert routing.tenant is None
    assert routing.reason == "unmapped_account"
    assert routing.routed_by is None
    assert routing.mapping is None


def test_resolve_with_no_identifiers_at_all_has_no_account_id(conn, clock):
    # No URL key, no crmKey, no account id: nothing to route on, and no mapping could
    # ever release it - which is a different situation from an account nobody mapped yet.
    routing = resolve(conn, clock)

    assert routing.tenant is None
    assert routing.reason == "no_account_id"


def test_resolve_by_url_key_without_an_account_id(conn, clock):
    # The tenant URL alone is enough, and with no account id there is nothing to learn.
    before = scalar(conn, "SELECT COUNT(*) FROM tenant_lead_sources")

    routing = resolve(conn, clock, url_tenant_id="tnt_summit")

    assert routing.tenant["id"] == "tnt_summit"
    assert routing.routed_by == "url_key"
    assert routing.mapping is None
    assert routing.learned_mapping is False
    assert scalar(conn, "SELECT COUNT(*) FROM tenant_lead_sources") == before


def test_resolve_by_url_key_learns_an_unseen_account(conn, clock):
    # Explicit routing + an account id nobody has mapped: remember it for next time.
    routing = resolve(
        conn, clock, url_tenant_id="tnt_summit",
        external_account_id=UNMAPPED_ACCOUNT, external_account_name="Summit East",
    )

    assert routing.tenant["id"] == "tnt_summit"
    assert routing.routed_by == "url_key"
    assert routing.learned_mapping is True
    assert routing.conflict is None
    assert routing.mapping["tenant_id"] == "tnt_summit"
    assert routing.mapping["learned"] == 1
    assert any(UNMAPPED_ACCOUNT in note for note in routing.notes)
    row = mapping_row(conn, UNMAPPED_ACCOUNT)
    assert row["tenant_id"] == "tnt_summit"
    assert row["learned"] == 1
    assert row["external_account_name"] == "Summit East"
    assert row["default_fee_cents"] is None
    assert row["created_at"] == iso(clock())


def test_resolve_by_url_key_with_the_tenants_own_account_changes_nothing(conn, clock):
    # URL and account id agree: use the existing mapping, no conflict, nothing re-learned.
    before = mapping_row(conn, "200001")

    routing = resolve(conn, clock, url_tenant_id="tnt_summit", external_account_id="200001")

    assert routing.tenant["id"] == "tnt_summit"
    assert routing.routed_by == "url_key"
    assert routing.mapping == before
    assert routing.conflict is None
    assert routing.learned_mapping is False
    assert mapping_row(conn, "200001") == before


def test_resolve_url_key_does_not_beat_an_account_mapped_to_another_tenant(conn, clock):
    # One tenant's URL must not create leads for another tenant's Angi account. The two
    # signals contradict each other, so neither wins: no tenant, and both are reported.
    before = mapping_row(conn, "123456")

    routing = resolve(conn, clock, url_tenant_id="tnt_summit", external_account_id="123456")

    assert routing.tenant is None
    assert routing.reason == "mapping_conflict"
    assert routing.routed_by is None
    assert routing.mapping is None  # tnt_sparkle's mapping (and its fee) must not be applied
    assert routing.conflict == {
        "external_account_id": "123456",
        "routed_tenant_id": "tnt_summit",
        "mapped_tenant_id": "tnt_sparkle",
        "routed_by": "url_key",
    }
    assert routing.learned_mapping is False
    assert mapping_row(conn, "123456") == before


def test_resolve_operator_assignment_comes_before_the_url_key(conn, clock):
    # A person's decision about a quarantined event is the strongest signal there is.
    routing = resolve(conn, clock, operator_tenant_id="tnt_blueline", url_tenant_id="tnt_summit")

    assert routing.tenant["id"] == "tnt_blueline"
    assert routing.routed_by == "operator"


def test_resolve_operator_assignment_does_not_beat_an_account_mapped_to_another_tenant(conn, clock):
    # Even an operator cannot route around the mapping: change the mapping first.
    routing = resolve(conn, clock, operator_tenant_id="tnt_summit", external_account_id="123456")

    assert routing.tenant is None
    assert routing.reason == "mapping_conflict"
    assert routing.conflict["routed_by"] == "operator"
    assert mapping_row(conn, "123456")["tenant_id"] == "tnt_sparkle"


def test_resolve_operator_assignment_to_an_unknown_or_deactivated_tenant(conn, clock):
    # Nothing is guessed when the assignment cannot be honoured.
    conn.execute("UPDATE tenants SET is_active = 0 WHERE id = 'tnt_summit'")

    for tenant_id in ("tnt_gone", "tnt_summit"):
        routing = resolve(conn, clock, operator_tenant_id=tenant_id, external_account_id="123456")
        assert routing.tenant is None
        assert routing.reason == "tenant_inactive"


def test_resolve_url_key_of_a_deactivated_tenant(conn, clock):
    # A tenant that was switched off does not receive leads, even on its own URL.
    conn.execute("UPDATE tenants SET is_active = 0 WHERE id = 'tnt_summit'")

    routing = resolve(conn, clock, url_tenant_id="tnt_summit", external_account_id="200001")

    assert routing.tenant is None
    assert routing.reason == "tenant_inactive"
    assert routing.mapping["external_account_id"] == "200001"


def test_resolve_url_key_of_a_deleted_tenant_does_not_fall_back_to_the_account_id(conn, clock):
    # A URL whose tenant no longer exists is not silently re-routed by the payload.
    routing = resolve(conn, clock, url_tenant_id="tnt_gone", external_account_id="123456")

    assert routing.tenant is None
    assert routing.reason == "tenant_inactive"


@pytest.mark.parametrize("crm_key", ["tnt_blueline", "blueline-plumbing"])
def test_resolve_by_crm_key_matches_tenant_id_or_slug(conn, clock, crm_key):
    # crmKey is our own identifier echoed back by Angi: the tenant id or its slug.
    routing = resolve(conn, clock, crm_key=crm_key)

    assert routing.tenant["id"] == "tnt_blueline"
    assert routing.routed_by == "crm_key"
    assert routing.mapping is None
    assert routing.learned_mapping is False


def test_resolve_unknown_crm_key_falls_through_to_the_account_id(conn, clock):
    # A crmKey we do not recognise is ignored rather than fatal.
    routing = resolve(conn, clock, crm_key="no-such-tenant", external_account_id="12345678")

    assert routing.tenant["id"] == "tnt_summit"
    assert routing.routed_by == "account_id"
    assert routing.conflict is None


def test_resolve_unknown_crm_key_and_unknown_account_is_unmapped(conn, clock):
    # Nothing recognisable at all.
    routing = resolve(conn, clock, crm_key="no-such-tenant", external_account_id=UNMAPPED_ACCOUNT)

    assert routing.tenant is None
    assert routing.reason == "unmapped_account"
    assert mapping_row(conn, UNMAPPED_ACCOUNT) is None


def test_resolve_crm_key_learns_an_unseen_account(conn, clock):
    # crmKey is explicit routing too, so it also teaches us the account id.
    routing = resolve(conn, clock, crm_key="blueline-plumbing", external_account_id=UNMAPPED_ACCOUNT)

    assert routing.tenant["id"] == "tnt_blueline"
    assert routing.routed_by == "crm_key"
    assert routing.learned_mapping is True
    assert mapping_row(conn, UNMAPPED_ACCOUNT)["tenant_id"] == "tnt_blueline"
    assert mapping_row(conn, UNMAPPED_ACCOUNT)["learned"] == 1


def test_resolve_crm_key_does_not_beat_an_account_mapped_to_another_tenant(conn, clock):
    # Same rule as the URL key: a contradiction is reported, not decided in favour of one side.
    routing = resolve(conn, clock, crm_key="tnt_blueline", external_account_id="12345678")

    assert routing.tenant is None
    assert routing.reason == "mapping_conflict"
    assert routing.mapping is None
    assert routing.conflict["mapped_tenant_id"] == "tnt_summit"
    assert routing.conflict["routed_tenant_id"] == "tnt_blueline"
    assert routing.conflict["routed_by"] == "crm_key"
    assert mapping_row(conn, "12345678")["tenant_id"] == "tnt_summit"


def test_resolve_crm_key_of_a_deactivated_tenant(conn, clock):
    # A crmKey naming a switched-off tenant quarantines the lead instead of re-routing it.
    conn.execute("UPDATE tenants SET is_active = 0 WHERE id = 'tnt_blueline'")

    routing = resolve(conn, clock, crm_key="tnt_blueline", external_account_id="123456")

    assert routing.tenant is None
    assert routing.reason == "tenant_inactive"


def test_resolve_url_key_takes_precedence_over_crm_key(conn, clock):
    # Strongest signal first: URL key, then crmKey.
    routing = resolve(conn, clock, url_tenant_id="tnt_summit", crm_key="tnt_blueline")

    assert routing.tenant["id"] == "tnt_summit"
    assert routing.routed_by == "url_key"


def test_resolve_account_mapped_to_a_deactivated_tenant(conn, clock):
    # The mapping exists but the tenant is off: quarantine with a distinct reason.
    conn.execute("UPDATE tenants SET is_active = 0 WHERE id = 'tnt_sparkle'")

    routing = resolve(conn, clock, external_account_id="123456")

    assert routing.tenant is None
    assert routing.reason == "tenant_inactive"
    assert routing.mapping["tenant_id"] == "tnt_sparkle"


def test_resolve_ignores_a_deactivated_mapping(conn, clock):
    # A mapping row that was switched off no longer routes anything.
    conn.execute("UPDATE tenant_lead_sources SET is_active = 0 WHERE external_account_id = '123456'")

    routing = resolve(conn, clock, external_account_id="123456")

    assert find_mapping(conn, SOURCE_ANGI, "123456") is None
    assert routing.tenant is None
    assert routing.reason == "unmapped_account"


def test_resolve_mappings_are_scoped_to_their_lead_source(conn, clock):
    # An account id from another lead source must not route an Angi lead.
    add_mapping(
        conn, tenant_id="tnt_sparkle", source="thumbtack", external_account_id="777001",
        external_account_name=None, now=clock(),
    )

    assert resolve(conn, clock, external_account_id="777001").reason == "unmapped_account"
    assert resolve(conn, clock, source="thumbtack", external_account_id="777001").tenant["id"] == "tnt_sparkle"


def test_resolve_fallback_tenant_for_an_unmapped_account(conn, clock):
    # Demo convenience: with a fallback configured, unmapped leads go there (and say so).
    routing = resolve(conn, clock, external_account_id=UNMAPPED_ACCOUNT, fallback_slug="synthetic-canary")

    assert routing.tenant["id"] == "tnt_canary"
    assert routing.routed_by == "fallback"
    assert routing.mapping is None
    assert routing.learned_mapping is False
    assert routing.notes and UNMAPPED_ACCOUNT in routing.notes[0]
    assert mapping_row(conn, UNMAPPED_ACCOUNT) is None  # a guess is never remembered


def test_resolve_fallback_also_covers_a_payload_with_no_account_id(conn, clock):
    # No account id at all is "unmapped" as well.
    routing = resolve(conn, clock, fallback_slug="synthetic-canary")

    assert routing.tenant["id"] == "tnt_canary"
    assert routing.routed_by == "fallback"


def test_resolve_fallback_is_not_used_when_the_account_is_mapped(conn, clock):
    # The fallback is a last resort, not an override.
    routing = resolve(conn, clock, external_account_id="123456", fallback_slug="synthetic-canary")

    assert routing.tenant["id"] == "tnt_sparkle"
    assert routing.routed_by == "account_id"


def test_resolve_fallback_is_not_used_for_a_deactivated_tenants_account(conn, clock):
    # A known-but-off business must not have its leads emailed as the fallback business.
    conn.execute("UPDATE tenants SET is_active = 0 WHERE id = 'tnt_sparkle'")

    routing = resolve(conn, clock, external_account_id="123456", fallback_slug="synthetic-canary")

    assert routing.tenant is None
    assert routing.reason == "tenant_inactive"


def test_resolve_unknown_or_deactivated_fallback_tenant_quarantines(conn, clock):
    # A fallback that does not exist (or is switched off) is the same as no fallback.
    unknown = resolve(conn, clock, external_account_id=UNMAPPED_ACCOUNT, fallback_slug="no-such-slug")
    conn.execute("UPDATE tenants SET is_active = 0 WHERE id = 'tnt_canary'")
    inactive = resolve(conn, clock, external_account_id=UNMAPPED_ACCOUNT, fallback_slug="synthetic-canary")

    assert unknown.tenant is None and unknown.reason == "unmapped_account"
    assert inactive.tenant is None and inactive.reason == "unmapped_account"


# --- create_tenant -----------------------------------------------------------------------


def new_tenant(conn, clock, **overrides):
    params = {"slug": "acme-roofing", "name": "Acme Roofing", "now": clock()}
    params.update(overrides)
    return create_tenant(conn, **params)


def test_create_tenant_applies_defaults_and_issues_a_webhook_key(conn, clock):
    # A new tenant is active, has sensible hours, and gets its own secret webhook URL key.
    tenant = new_tenant(conn, clock)

    assert tenant["id"].startswith("tnt_")
    assert tenant["slug"] == "acme-roofing"
    assert tenant["name"] == "Acme Roofing"
    assert tenant["timezone"] == "America/Los_Angeles"
    assert (tenant["business_hours_start"], tenant["business_hours_end"]) == (8, 17)
    assert tenant["appointment_minutes"] == 120
    assert tenant["is_active"] == 1
    assert tenant["is_test"] == 0
    assert tenant["created_at"] == iso(clock())
    assert len(tenant["webhook_key"]) >= 20
    other = new_tenant(conn, clock, slug="acme-two", name="Acme Two")
    assert other["webhook_key"] != tenant["webhook_key"]
    assert other["id"] != tenant["id"]


def test_create_tenant_honours_explicit_id_key_and_flags(conn, clock):
    # Seeding passes fixed ids; they are used as given.
    tenant = new_tenant(
        conn, clock, tenant_id="tnt_fixed", webhook_key="fixed-key", is_test=True,
        timezone="America/Chicago", business_hours_start=0, business_hours_end=24, appointment_minutes=1,
    )

    assert tenant["id"] == "tnt_fixed"
    assert tenant["webhook_key"] == "fixed-key"
    assert tenant["is_test"] == 1
    assert (tenant["business_hours_start"], tenant["business_hours_end"]) == (0, 24)
    assert tenant["appointment_minutes"] == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"timezone": "Mars/Olympus_Mons"},
        {"timezone": "Not A Zone"},
        {"timezone": ""},
        {"timezone": "America/Denver "},  # trailing space
        {"timezone": "../../etc/passwd"},
        {"business_hours_start": 17, "business_hours_end": 8},  # closes before it opens
        {"business_hours_start": 9, "business_hours_end": 9},  # zero-length day
        {"business_hours_start": -1},
        {"business_hours_end": 25},
        {"appointment_minutes": 0},
        {"appointment_minutes": -30},
        {"slug": ""},
        {"name": ""},
    ],
)
def test_create_tenant_rejects_invalid_values(conn, clock, overrides):
    # Bad timezone / hours / appointment length / names are refused before anything is stored.
    before = scalar(conn, "SELECT COUNT(*) FROM tenants")

    with pytest.raises(ValueError):
        new_tenant(conn, clock, **overrides)

    assert scalar(conn, "SELECT COUNT(*) FROM tenants") == before


@pytest.mark.parametrize(
    "reply_to",
    ["not an email", "office@acme.test, other@acme.test", "office@acme.test\nBcc: someone@else.test"],
)
def test_create_tenant_rejects_a_reply_to_that_is_not_one_address(conn, clock, reply_to):
    # The value goes into the Reply-To header of every email sent for the tenant.
    with pytest.raises(ValueError, match="reply_to_email"):
        new_tenant(conn, clock, reply_to_email=reply_to)


def test_create_tenant_accepts_a_valid_reply_to_and_treats_blank_as_none(conn, clock):
    # A blank form field means "no reply-to", not an invalid one.
    assert new_tenant(conn, clock, reply_to_email=" office@example.com ")["reply_to_email"] == "office@example.com"
    assert new_tenant(conn, clock, slug="acme-two", reply_to_email="   ")["reply_to_email"] is None


def test_create_tenant_stores_the_name_on_one_line(conn, clock):
    # The name becomes the From display name of the tenant's emails: a line break in it
    # would make every message unbuildable (or let the name add headers of its own).
    tenant = new_tenant(conn, clock, name="Acme\r\nBcc: someone@else.test  Roofing\x00")

    assert tenant["name"] == "Acme Bcc: someone@else.test Roofing"


def test_create_tenant_rejects_a_duplicate_slug(conn, clock):
    # Slugs are unique: the database refuses a second tenant with the same one.
    with pytest.raises(sqlite3.IntegrityError):
        new_tenant(conn, clock, slug="sparkle-home-cleaning")


@pytest.mark.parametrize("name, expected", [("UTC", True), ("America/Denver", True), ("Nope/Nowhere", False), ("", False)])
def test_valid_timezone(name, expected):
    # Only IANA names the runtime can load are accepted.
    assert valid_timezone(name) is expected


def test_timezone_that_is_only_a_region_name_is_rejected_as_invalid(conn, clock):
    # "America" is not a timezone; it must be a validation error like any other bad name.
    assert valid_timezone("America") is False
    with pytest.raises(ValueError):
        new_tenant(conn, clock, timezone="America")


# --- add_mapping -------------------------------------------------------------------------


def test_add_mapping_creates_an_active_manual_mapping(conn, clock):
    # A mapping added by an operator is active and not marked as learned.
    mapping = add_mapping(
        conn, tenant_id="tnt_blueline", source=SOURCE_ANGI, external_account_id="777001",
        external_account_name="BlueLine North", default_fee_cents=3100, now=clock(),
    )

    assert mapping["id"].startswith("src_")
    assert mapping["tenant_id"] == "tnt_blueline"
    assert mapping["external_account_id"] == "777001"
    assert mapping["external_account_name"] == "BlueLine North"
    assert mapping["default_fee_cents"] == 3100
    assert mapping["learned"] == 0
    assert mapping["is_active"] == 1
    assert mapping["created_at"] == iso(clock())
    assert find_mapping(conn, SOURCE_ANGI, "777001") == mapping


def test_add_mapping_repointing_an_account_updates_the_single_row(conn, clock):
    # An account can change hands: the one row moves, no second row appears.
    before = mapping_row(conn, "123456")

    moved = add_mapping(
        conn, tenant_id="tnt_blueline", source=SOURCE_ANGI, external_account_id="123456",
        external_account_name=None, now=clock(),
    )

    assert moved["tenant_id"] == "tnt_blueline"
    assert moved["id"] == before["id"]
    assert scalar(conn, "SELECT COUNT(*) FROM tenant_lead_sources WHERE external_account_id = '123456'") == 1
    assert resolve(conn, clock, external_account_id="123456").tenant["id"] == "tnt_blueline"


def test_add_mapping_keeps_name_and_fee_when_readded_without_them(conn, clock):
    # Re-adding a mapping with blanks must not wipe the account name or the default fee.
    again = add_mapping(
        conn, tenant_id="tnt_sparkle", source=SOURCE_ANGI, external_account_id="123456",
        external_account_name=None, default_fee_cents=None, now=clock(),
    )

    assert again["external_account_name"] == "Sparkle Home Cleaning"
    assert again["default_fee_cents"] == 2800


def test_add_mapping_replaces_name_and_fee_when_given(conn, clock):
    # Supplying new values does update them (0 is a real fee, not "missing").
    again = add_mapping(
        conn, tenant_id="tnt_sparkle", source=SOURCE_ANGI, external_account_id="123456",
        external_account_name="Sparkle (renamed)", default_fee_cents=0, now=clock(),
    )

    assert again["external_account_name"] == "Sparkle (renamed)"
    assert again["default_fee_cents"] == 0


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_add_mapping_rejects_a_blank_account_id(conn, clock, blank):
    # A mapping needs an account id.
    before = scalar(conn, "SELECT COUNT(*) FROM tenant_lead_sources")

    with pytest.raises(ValueError):
        add_mapping(
            conn, tenant_id="tnt_sparkle", source=SOURCE_ANGI, external_account_id=blank,
            external_account_name=None, now=clock(),
        )

    assert scalar(conn, "SELECT COUNT(*) FROM tenant_lead_sources") == before


def test_add_mapping_normalises_the_account_id(conn, clock):
    # Spaces are trimmed and numbers stringified, matching how payload ids are read.
    spaced = add_mapping(
        conn, tenant_id="tnt_sparkle", source=SOURCE_ANGI, external_account_id="  777002 ",
        external_account_name=None, now=clock(),
    )
    numeric = add_mapping(
        conn, tenant_id="tnt_sparkle", source=SOURCE_ANGI, external_account_id=777003,
        external_account_name=None, now=clock(),
    )

    assert spaced["external_account_id"] == "777002"
    assert numeric["external_account_id"] == "777003"
    assert resolve(conn, clock, external_account_id="777002").tenant["id"] == "tnt_sparkle"


def test_add_mapping_learned_flag_and_reactivation(conn, clock):
    # A learned mapping confirmed by an operator becomes a manual, active one.
    learned = add_mapping(
        conn, tenant_id="tnt_summit", source=SOURCE_ANGI, external_account_id="777004",
        external_account_name=None, learned=True, now=clock(),
    )
    conn.execute("UPDATE tenant_lead_sources SET is_active = 0 WHERE external_account_id = '777004'")

    confirmed = add_mapping(
        conn, tenant_id="tnt_summit", source=SOURCE_ANGI, external_account_id="777004",
        external_account_name=None, now=clock(),
    )

    assert learned["learned"] == 1
    assert confirmed["learned"] == 0
    assert confirmed["is_active"] == 1
    assert confirmed["id"] == learned["id"]


def test_add_mapping_same_account_id_under_another_source_is_a_separate_row(conn, clock):
    # Account ids are only unique per lead source.
    other = add_mapping(
        conn, tenant_id="tnt_blueline", source="thumbtack", external_account_id="123456",
        external_account_name=None, now=clock(),
    )

    assert other["tenant_id"] == "tnt_blueline"
    assert mapping_row(conn, "123456")["tenant_id"] == "tnt_sparkle"
    assert scalar(conn, "SELECT COUNT(*) FROM tenant_lead_sources WHERE external_account_id = '123456'") == 2


def test_list_tenants_includes_each_tenants_lead_sources(conn):
    # The admin view shows every tenant with the Angi accounts mapped to it.
    tenants = {tenant["id"]: tenant for tenant in list_tenants(conn)}

    assert set(tenants) == {"tnt_sparkle", "tnt_summit", "tnt_blueline", "tnt_canary"}
    assert sorted(s["external_account_id"] for s in tenants["tnt_summit"]["lead_sources"]) == ["12345678", "200001"]
    assert [s["external_account_id"] for s in tenants["tnt_sparkle"]["lead_sources"]] == ["123456"]


# --- routing through the webhook: account ids ---------------------------------------------


@pytest.mark.parametrize(
    "account_id, tenant_id",
    [("123456", "tnt_sparkle"), ("200001", "tnt_summit"), ("200002", "tnt_blueline"), ("999999", "tnt_canary")],
)
def test_angi_ads_lead_is_mapped_by_al_account_id(client, ctx, account_id, tenant_id):
    # ALAccountId decides the tenant for the Angi Ads feed.
    body = post_lead(client, ads_payload(ALAccountId=account_id)).json()

    assert body["result"] == "processed"
    assert body["tenant"]["id"] == tenant_id
    assert body["tenant"]["routedBy"] == "account_id"
    lead = db_one(ctx, "SELECT * FROM leads")
    assert lead["tenant_id"] == tenant_id
    assert lead["external_account_id"] == account_id
    assert lead["routed_by"] == "account_id"
    assert db_one(ctx, "SELECT tenant_id FROM webhook_events")["tenant_id"] == tenant_id


@pytest.mark.parametrize("account_id, tenant_id", [(12345678, "tnt_summit"), (567890123, "tnt_blueline")])
def test_angi_leads_lead_is_mapped_by_sp_entity_id(client, ctx, account_id, tenant_id):
    # spEntityId decides the tenant for the Angi Leads feed.
    body = post_lead(client, leads_payload(spEntityId=account_id)).json()

    assert body["result"] == "processed"
    assert body["tenant"]["id"] == tenant_id
    assert body["tenant"]["routedBy"] == "account_id"
    assert db_one(ctx, "SELECT external_account_id FROM leads")["external_account_id"] == str(account_id)


@pytest.mark.parametrize("account_id", [123456, 123456.0, " 123456 ", "123456\n", "\t123456"])
def test_al_account_id_as_a_number_or_with_spaces_still_maps(client, ctx, account_id):
    # The id's JSON type and stray whitespace must not send a lead to quarantine.
    body = post_lead(client, ads_payload(ALAccountId=account_id)).json()

    assert body["result"] == "processed"
    assert body["tenant"]["id"] == "tnt_sparkle"
    assert db_one(ctx, "SELECT external_account_id FROM leads")["external_account_id"] == "123456"


@pytest.mark.parametrize("account_id", ["12345678", " 12345678 ", 12345678.0])
def test_sp_entity_id_as_a_string_or_with_spaces_still_maps(client, account_id):
    # Same tolerance for the Angi Leads feed.
    body = post_lead(client, leads_payload(spEntityId=account_id)).json()

    assert body["result"] == "processed"
    assert body["tenant"]["id"] == "tnt_summit"


# --- routing through the webhook: crmKey ---------------------------------------------------


@pytest.mark.parametrize("crm_key", ["tnt_blueline", "blueline-plumbing"])
def test_crm_key_routes_by_tenant_id_or_slug_when_sp_entity_id_is_absent(client, ctx, crm_key):
    # Angi only "sometimes" sends spEntityId; the crmKey we configured is enough.
    mappings_before = count(ctx, "tenant_lead_sources")

    body = post_lead(client, leads_payload_without_account(crmKey=crm_key)).json()

    assert body["result"] == "processed"
    assert body["tenant"]["id"] == "tnt_blueline"
    assert body["tenant"]["routedBy"] == "crm_key"
    lead = db_one(ctx, "SELECT * FROM leads")
    assert lead["tenant_id"] == "tnt_blueline"
    assert lead["routed_by"] == "crm_key"
    assert lead["external_account_id"] is None
    assert count(ctx, "tenant_lead_sources") == mappings_before  # no account id, nothing to learn


def test_crm_key_routes_an_unmapped_sp_entity_id_and_learns_it(client, ctx):
    # crmKey + an account nobody mapped: route by crmKey and remember the account.
    first = post_lead(client, leads_payload(spEntityId=31337, crmKey="blueline-plumbing")).json()

    assert first["result"] == "processed"
    assert first["tenant"]["id"] == "tnt_blueline"
    assert first["tenant"]["routedBy"] == "crm_key"
    mapping = stored_mapping(ctx, "31337")
    assert mapping["tenant_id"] == "tnt_blueline"
    assert mapping["learned"] == 1
    assert mapping["external_account_name"] == "Summit Heating & Air"  # spCompanyName from the payload

    # Later leads for that account need no crmKey.
    second = post_lead(client, leads_payload(spEntityId=31337)).json()
    assert second["tenant"]["id"] == "tnt_blueline"
    assert second["tenant"]["routedBy"] == "account_id"


def test_unknown_crm_key_falls_through_to_the_account_id(client, ctx):
    # A crmKey that names no tenant is ignored; spEntityId still routes the lead.
    body = post_lead(client, leads_payload(crmKey="some-other-crm-value")).json()

    assert body["result"] == "processed"
    assert body["tenant"]["id"] == "tnt_summit"
    assert body["tenant"]["routedBy"] == "account_id"
    assert count(ctx, "alerts", "kind = 'mapping_conflict'") == 0


def test_unknown_crm_key_without_a_mapped_account_is_quarantined(client, ctx):
    # Neither signal is usable: quarantine, do not guess. With no account id in the
    # payload no mapping can release it, and the reason says so.
    body = post_lead(client, leads_payload_without_account(crmKey="some-other-crm-value")).json()

    assert body["result"] == "quarantined"
    assert body["reason"] == "no_account_id"
    assert count(ctx, "leads") == 0


def test_crm_key_for_one_tenant_with_an_sp_entity_id_mapped_to_another_is_quarantined(client, ctx, mailer):
    # crmKey says BlueLine, the account mapping says Summit: nobody is emailed under either
    # name until a person has decided. The conflict is alerted and the mapping stays.
    body = post_lead(client, leads_payload(crmKey="tnt_blueline")).json()  # spEntityId 12345678 -> tnt_summit

    assert body["result"] == "quarantined"
    assert body["reason"] == "mapping_conflict"
    assert count(ctx, "leads") == 0
    alert = db_one(ctx, "SELECT * FROM alerts WHERE kind = 'mapping_conflict'")
    assert alert["severity"] == "critical"
    assert "12345678" in alert["title"]
    assert stored_mapping(ctx, "12345678")["tenant_id"] == "tnt_summit"
    ctx.worker.deliver_due()
    assert mailer.sent == []


def test_crm_key_agreeing_with_the_account_mapping_raises_no_conflict(client, ctx):
    # Both signals name the same tenant: nothing to report.
    body = post_lead(client, leads_payload(crmKey="tnt_summit")).json()

    assert body["tenant"]["id"] == "tnt_summit"
    assert body["tenant"]["routedBy"] == "crm_key"
    assert count(ctx, "alerts") == 0
    assert stored_mapping(ctx, "12345678")["learned"] == 0


# --- routing through the webhook: tenant-keyed URL ------------------------------------------


def test_tenant_url_with_an_account_mapped_to_another_tenant_is_quarantined(client, ctx, mailer):
    # Tenant A's URL + an account mapped to tenant B: neither gets the lead. Letting the
    # URL win would let whoever holds A's URL create leads (and send emails) for B's account.
    before = stored_mapping(ctx, "123456")

    response = post_lead(
        client, ads_payload(ALAccountId="123456"), headers={}, path=webhook_url(ctx, "tnt_summit")
    )

    body = response.json()
    assert response.status_code == 200  # stored, so still a success for Angi
    assert body["status"] == "success"
    assert body["result"] == "quarantined"
    assert body["reason"] == "mapping_conflict"
    assert "addressed to one tenant" in body["message"]
    assert "processed automatically" not in body["message"]  # no promise we cannot keep
    assert count(ctx, "leads") == 0
    event = db_one(ctx, "SELECT * FROM webhook_events")
    assert (event["status"], event["status_reason"]) == ("quarantined", "mapping_conflict")
    alerts = db_rows(ctx, "SELECT * FROM alerts")
    assert len(alerts) == 1
    assert alerts[0]["kind"] == "mapping_conflict"
    assert alerts[0]["severity"] == "critical"
    assert alerts[0]["dedupe_key"] == "mapping_conflict:123456"
    # The alert names both businesses, so the operator knows who to ask.
    assert "Summit Heating & Air" in alerts[0]["title"]
    assert "Sparkle Home Cleaning" in alerts[0]["title"]
    detail = json.loads(alerts[0]["detail_json"])
    assert detail["routed_tenant_id"] == "tnt_summit"
    assert detail["mapped_tenant_id"] == "tnt_sparkle"
    assert stored_mapping(ctx, "123456") == before
    ctx.worker.deliver_due()
    assert mailer.sent == []


def test_mapping_conflict_is_released_by_assigning_the_event_to_the_mapped_tenant(client, ctx):
    # The operator decides for the tenant the mapping names: the lead is created there,
    # and once the account routes cleanly the conflict alert is closed.
    held = post_lead(
        client, ads_payload(ALAccountId="123456"), headers={}, path=webhook_url(ctx, "tnt_summit")
    ).json()

    outcome = ctx.ingest.process_event(held["eventId"], assign_tenant_id="tnt_sparkle")

    assert outcome.body["result"] == "processed"
    assert outcome.body["tenant"] == {"id": "tnt_sparkle", "name": "Sparkle Home Cleaning", "routedBy": "operator"}
    assert db_one(ctx, "SELECT status FROM alerts WHERE kind = 'mapping_conflict'")["status"] == "resolved"


def test_mapping_conflict_cannot_be_overridden_by_assigning_the_other_tenant(client, ctx):
    # An operator assignment is explicit routing too, so it meets the same rule: the
    # mapping has to be changed first, it cannot be bypassed for one lead.
    held = post_lead(
        client, ads_payload(ALAccountId="123456"), headers={}, path=webhook_url(ctx, "tnt_summit")
    ).json()

    outcome = ctx.ingest.process_event(held["eventId"], assign_tenant_id="tnt_blueline")

    assert outcome.body["result"] == "quarantined"
    assert outcome.body["reason"] == "mapping_conflict"
    assert count(ctx, "leads") == 0


def test_repeated_mapping_conflicts_are_one_alert_with_a_count(client, ctx):
    # A misconfigured account produces one alert, not one per lead.
    for _ in range(3):
        post_lead(client, ads_payload(ALAccountId="123456"), headers={}, path=webhook_url(ctx, "tnt_summit"))

    assert count(ctx, "alerts", "kind = 'mapping_conflict'") == 1
    assert db_one(ctx, "SELECT count FROM alerts WHERE kind = 'mapping_conflict'")["count"] == 3


def test_tenant_url_beats_a_crm_key_for_another_tenant(client, ctx):
    # URL key first, crmKey second.
    payload = leads_payload_without_account(crmKey="tnt_blueline")

    body = post_lead(client, payload, headers={}, path=webhook_url(ctx, "tnt_sparkle")).json()

    assert body["tenant"]["id"] == "tnt_sparkle"
    assert body["tenant"]["routedBy"] == "url_key"


def test_tenant_url_learns_an_unmapped_account_for_the_shared_url(client, ctx):
    # Onboarding an extra Angi account needs no manual step: the tenant URL teaches us.
    first = post_lead(
        client, ads_payload(ALAccountId=UNMAPPED_ACCOUNT), headers={}, path=webhook_url(ctx, "tnt_summit")
    ).json()

    assert first["result"] == "processed"
    assert first["tenant"]["id"] == "tnt_summit"
    assert first["tenant"]["routedBy"] == "url_key"
    assert any("learned mapping" in warning for warning in first["warnings"])
    mapping = stored_mapping(ctx, UNMAPPED_ACCOUNT)
    assert mapping["tenant_id"] == "tnt_summit"
    assert mapping["learned"] == 1
    assert mapping["is_active"] == 1
    # Convenient, but a person should glance at it: one informational alert, nothing worse.
    alerts = db_rows(ctx, "SELECT kind, severity, dedupe_key FROM alerts")
    assert alerts == [
        {"kind": "mapping_learned", "severity": "info", "dedupe_key": f"mapping_learned:{UNMAPPED_ACCOUNT}"}
    ]

    # The same account on the SHARED url now finds the same tenant.
    second = post_lead(client, ads_payload(ALAccountId=UNMAPPED_ACCOUNT)).json()
    assert second["result"] == "processed"
    assert second["tenant"]["id"] == "tnt_summit"
    assert second["tenant"]["routedBy"] == "account_id"
    assert count(ctx, "tenant_lead_sources", "external_account_id = ?", (UNMAPPED_ACCOUNT,)) == 1
    assert count(ctx, "leads", "tenant_id = 'tnt_summit'") == 2


def test_tenant_url_with_no_account_id_in_the_payload_routes_and_learns_nothing(client, ctx):
    # The URL alone is enough to route; there is no id to remember.
    payload = ads_payload()
    del payload["ALAccountId"]
    mappings_before = count(ctx, "tenant_lead_sources")

    body = post_lead(client, payload, headers={}, path=webhook_url(ctx, "tnt_blueline")).json()

    assert body["result"] == "processed"
    assert body["tenant"]["id"] == "tnt_blueline"
    assert count(ctx, "tenant_lead_sources") == mappings_before


# --- unmapped accounts: quarantine, then replay when the mapping is added -------------------


def test_unmapped_account_is_quarantined_with_a_critical_alert_and_no_email(client, ctx, mailer):
    # Unknown business: store the request, alert, create no lead and email nobody.
    response = post_lead(client, ads_payload(ALAccountId=UNMAPPED_ACCOUNT))

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "success"
    assert body["result"] == "quarantined"
    assert body["reason"] == "unmapped_account"
    assert body["angiAccountId"] == UNMAPPED_ACCOUNT
    event = db_one(ctx, "SELECT * FROM webhook_events")
    assert event["status"] == "quarantined"
    assert event["status_reason"] == "unmapped_account"
    assert event["tenant_id"] is None and event["lead_id"] is None
    alerts = db_rows(ctx, "SELECT * FROM alerts")
    assert len(alerts) == 1
    assert alerts[0]["kind"] == "unmapped_account"
    assert alerts[0]["severity"] == "critical"
    assert alerts[0]["status"] == "open"
    assert UNMAPPED_ACCOUNT in alerts[0]["title"]
    assert json.loads(alerts[0]["detail_json"])["external_account_id"] == UNMAPPED_ACCOUNT
    assert count(ctx, "leads") == 0
    assert count(ctx, "outbox_messages") == 0

    ctx.worker.deliver_due()
    assert mailer.sent == []


def test_unmapped_alert_names_the_business_when_the_payload_does(client, ctx):
    # The Angi Leads feed carries the company name, which makes the alert actionable.
    post_lead(client, leads_payload(spEntityId=31337, spCompanyName="Peak Roofing LLC"))

    alert = db_one(ctx, "SELECT * FROM alerts WHERE kind = 'unmapped_account'")
    assert "31337" in alert["title"]
    assert "Peak Roofing LLC" in alert["title"]


def test_each_unmapped_account_gets_its_own_alert(client, ctx):
    # Two unknown accounts are two problems; repeats of one only bump its count.
    post_lead(client, ads_payload(ALAccountId="555001"))
    post_lead(client, ads_payload(ALAccountId="555001"))
    post_lead(client, ads_payload(ALAccountId="555002"))

    alerts = {a["dedupe_key"]: a["count"] for a in db_rows(ctx, "SELECT * FROM alerts")}
    assert alerts == {"unmapped_account:555001": 2, "unmapped_account:555002": 1}


def test_adding_the_mapping_replays_the_quarantined_lead_with_its_original_time(client, ctx, mailer, clock):
    # Once an operator maps the account, the waiting lead is processed as if it had just
    # arrived - but its speed-to-lead clock still starts at the original arrival.
    quarantined = post_lead(client, ads_payload(ALAccountId=UNMAPPED_ACCOUNT, Email="waiting@example.com")).json()
    assert quarantined["result"] == "quarantined"
    clock.advance(hours=2)

    response = client.post(
        "/admin/api/tenants/tnt_blueline/lead-sources",
        json={"external_account_id": UNMAPPED_ACCOUNT, "external_account_name": "BlueLine North", "default_fee": 31.5},
        headers=ADMIN,
    )

    assert response.status_code == 201
    body = response.json()
    assert body["mapping"]["tenant_id"] == "tnt_blueline"
    assert body["mapping"]["external_account_id"] == UNMAPPED_ACCOUNT
    assert body["mapping"]["default_fee_cents"] == 3150
    assert body["mapping"]["learned"] == 0
    assert body["replayed"] == [{"eventId": quarantined["eventId"], "result": "processed"}]

    lead = db_one(ctx, "SELECT * FROM leads")
    assert lead["tenant_id"] == "tnt_blueline"
    assert lead["routed_by"] == "account_id"
    assert lead["received_at"] == iso(START)
    assert lead["created_at"] == iso(clock())
    assert lead["fee_cents"] == 3150
    assert lead["fee_source"] == "account_default"
    event = db_one(ctx, "SELECT * FROM webhook_events")
    assert event["status"] == "processed"
    assert event["status_reason"] is None
    assert event["tenant_id"] == "tnt_blueline"
    assert event["lead_id"] == lead["id"]
    assert event["attempts"] == 2

    assert mailer.sent == []
    ctx.worker.deliver_due()
    assert [(email.to, email.from_name) for email in mailer.sent] == [("waiting@example.com", "BlueLine Plumbing")]
    assert db_one(ctx, "SELECT first_contacted_at FROM leads")["first_contacted_at"] == iso(clock())


def test_adding_the_mapping_resolves_the_unmapped_account_alert(client, ctx):
    # The problem the alert describes is over the moment the account has a tenant; an
    # alert that stayed open would send the operator looking for nothing.
    post_lead(client, ads_payload(ALAccountId=UNMAPPED_ACCOUNT))
    post_lead(client, ads_payload(ALAccountId="555001"))

    client.post(
        "/admin/api/tenants/tnt_blueline/lead-sources", json={"external_account_id": UNMAPPED_ACCOUNT}, headers=ADMIN
    )

    alerts = {a["dedupe_key"]: a["status"] for a in db_rows(ctx, "SELECT * FROM alerts WHERE kind = 'unmapped_account'")}
    assert alerts == {f"unmapped_account:{UNMAPPED_ACCOUNT}": "resolved", "unmapped_account:555001": "open"}


def test_unmapped_account_alert_resolves_when_a_lead_for_the_account_routes(client, ctx):
    # Also without anyone touching the mapping screen: the tenant's own URL taught us.
    post_lead(client, ads_payload(ALAccountId=UNMAPPED_ACCOUNT))

    post_lead(client, ads_payload(ALAccountId=UNMAPPED_ACCOUNT), headers={}, path=webhook_url(ctx, "tnt_summit"))

    alert = db_one(ctx, "SELECT * FROM alerts WHERE kind = 'unmapped_account'")
    assert alert["status"] == "resolved"


def test_adding_a_mapping_for_another_account_leaves_the_lead_quarantined(client, ctx):
    # Only the account that was mapped is released.
    post_lead(client, ads_payload(ALAccountId=UNMAPPED_ACCOUNT))

    response = client.post(
        "/admin/api/tenants/tnt_blueline/lead-sources", json={"external_account_id": "555999"}, headers=ADMIN
    )

    assert response.status_code == 201
    assert response.json()["replayed"] == []  # not even re-run: its account was not the one mapped
    event = db_one(ctx, "SELECT status, attempts FROM webhook_events")
    assert event == {"status": "quarantined", "attempts": 1}
    assert count(ctx, "leads") == 0


def test_lead_sources_endpoint_requires_admin_and_an_existing_tenant(client, ctx):
    # Mapping accounts is an operator action on a real tenant.
    mappings_before = count(ctx, "tenant_lead_sources")

    no_auth = client.post("/admin/api/tenants/tnt_sparkle/lead-sources", json={"external_account_id": "1"})
    no_tenant = client.post(
        "/admin/api/tenants/tnt_missing/lead-sources", json={"external_account_id": "1"}, headers=ADMIN
    )
    no_account = client.post("/admin/api/tenants/tnt_sparkle/lead-sources", json={}, headers=ADMIN)
    empty_account = client.post(
        "/admin/api/tenants/tnt_sparkle/lead-sources", json={"external_account_id": ""}, headers=ADMIN
    )

    assert no_auth.status_code == 401
    assert no_tenant.status_code == 404
    assert no_account.status_code == 422
    assert empty_account.status_code == 422
    assert count(ctx, "tenant_lead_sources") == mappings_before


def test_whitespace_only_account_id_is_a_validation_error_not_a_server_error(client, ctx):
    # A blank account id is the caller's mistake and must be reported as one.
    quiet = TestClient(client.app, raise_server_exceptions=False)

    response = quiet.post(
        "/admin/api/tenants/tnt_sparkle/lead-sources", json={"external_account_id": "   "}, headers=ADMIN
    )

    assert response.status_code == 422


def test_repointing_an_account_via_the_api_moves_future_leads_only(client, ctx):
    # After an account changes hands, new leads follow it; old leads stay where they were.
    first = post_lead(client, ads_payload(ALAccountId="123456")).json()

    response = client.post(
        "/admin/api/tenants/tnt_blueline/lead-sources", json={"external_account_id": "123456"}, headers=ADMIN
    )
    second = post_lead(client, ads_payload(ALAccountId="123456", Email="next@example.com", PhoneNumber="3175552999")).json()

    assert response.status_code == 201
    assert response.json()["mapping"]["default_fee_cents"] == 2800  # kept from the existing row
    assert count(ctx, "tenant_lead_sources", "external_account_id = '123456'") == 1
    assert first["tenant"]["id"] == "tnt_sparkle"
    assert second["tenant"]["id"] == "tnt_blueline"
    assert db_one(ctx, "SELECT tenant_id FROM leads WHERE id = ?", (first["leadId"],))["tenant_id"] == "tnt_sparkle"


# --- fallback tenant -----------------------------------------------------------------------


def test_fallback_tenant_receives_unmapped_leads_with_a_warning(make_client, mailer):
    # FALLBACK_TENANT_SLUG (demo use): the lead is processed, but flagged as a guess.
    ctx, client = make_client(fallback_tenant_slug="sparkle-home-cleaning")
    mappings_before = count(ctx, "tenant_lead_sources")

    body = post_lead(client, ads_payload(ALAccountId=UNMAPPED_ACCOUNT)).json()

    assert body["result"] == "processed"
    assert body["tenant"] == {"id": "tnt_sparkle", "name": "Sparkle Home Cleaning", "routedBy": "fallback"}
    assert any("fallback" in warning for warning in body["warnings"])
    lead = db_one(ctx, "SELECT * FROM leads")
    assert lead["tenant_id"] == "tnt_sparkle"
    assert lead["routed_by"] == "fallback"
    assert lead["fee_cents"] is None  # no mapping, so no account price either
    alerts = db_rows(ctx, "SELECT * FROM alerts")
    assert len(alerts) == 1
    assert alerts[0]["kind"] == "unmapped_account"
    assert alerts[0]["severity"] == "warning"
    assert UNMAPPED_ACCOUNT in alerts[0]["title"]
    assert "fallback" in alerts[0]["title"]
    assert count(ctx, "tenant_lead_sources") == mappings_before  # the guess is not remembered

    ctx.worker.deliver_due()
    assert [email.from_name for email in mailer.sent] == ["Sparkle Home Cleaning"]


def test_fallback_tenant_does_not_override_a_mapped_account(make_client):
    # Mapped accounts still go to their own tenant.
    ctx, client = make_client(fallback_tenant_slug="sparkle-home-cleaning")

    body = post_lead(client, ads_payload(ALAccountId="200002")).json()

    assert body["tenant"]["id"] == "tnt_blueline"
    assert body["tenant"]["routedBy"] == "account_id"
    assert count(ctx, "alerts") == 0


def test_fallback_tenant_also_takes_leads_with_no_account_id(make_client):
    # A payload that names no account is handled the same way.
    ctx, client = make_client(fallback_tenant_slug="sparkle-home-cleaning")
    payload = ads_payload()
    del payload["ALAccountId"]

    body = post_lead(client, payload).json()

    assert body["result"] == "processed"
    assert body["tenant"]["routedBy"] == "fallback"
    assert count(ctx, "alerts", "kind = 'unmapped_account' AND severity = 'warning'") == 1


def test_fallback_slug_that_matches_no_tenant_still_quarantines(make_client):
    # A typo in FALLBACK_TENANT_SLUG must not lose or misroute leads.
    ctx, client = make_client(fallback_tenant_slug="no-such-tenant")

    body = post_lead(client, ads_payload(ALAccountId=UNMAPPED_ACCOUNT)).json()

    assert body["result"] == "quarantined"
    assert body["reason"] == "unmapped_account"
    assert count(ctx, "leads") == 0
    assert db_one(ctx, "SELECT severity FROM alerts WHERE kind = 'unmapped_account'")["severity"] == "critical"


# --- deactivated tenants -------------------------------------------------------------------


def test_lead_for_a_deactivated_tenant_is_quarantined(client, ctx, mailer):
    # A switched-off business gets no leads and its consumers get no email.
    set_active(ctx, "tnt_sparkle", False)

    body = post_lead(client, ads_payload(ALAccountId="123456")).json()

    assert body["status"] == "success"
    assert body["result"] == "quarantined"
    assert body["reason"] == "tenant_inactive"
    event = db_one(ctx, "SELECT * FROM webhook_events")
    assert event["status"] == "quarantined"
    assert event["status_reason"] == "tenant_inactive"
    assert count(ctx, "leads") == 0
    alert = db_one(ctx, "SELECT * FROM alerts")
    assert alert["severity"] == "critical"
    assert "deactivated" in alert["title"]
    assert "123456" in alert["title"]

    ctx.worker.deliver_due()
    assert mailer.sent == []


def test_lead_on_a_deactivated_tenants_url_is_quarantined_not_rejected(client, ctx):
    # The URL key still authenticates (so the lead is stored), but nothing is processed.
    url = webhook_url(ctx, "tnt_summit")
    set_active(ctx, "tnt_summit", False)

    response = post_lead(client, ads_payload(ALAccountId="200001"), headers={}, path=url)

    assert response.status_code == 200
    assert response.json()["result"] == "quarantined"
    assert response.json()["reason"] == "tenant_inactive"
    assert count(ctx, "webhook_events", "status = 'quarantined' AND status_reason = 'tenant_inactive'") == 1
    assert count(ctx, "leads") == 0


def test_deactivated_tenants_url_does_not_fall_back_to_another_tenants_account(client, ctx):
    # Even if the payload's account id maps to an active tenant, the explicit URL decides.
    url = webhook_url(ctx, "tnt_summit")
    set_active(ctx, "tnt_summit", False)

    body = post_lead(client, ads_payload(ALAccountId="123456"), headers={}, path=url).json()

    assert body["result"] == "quarantined"
    assert body["reason"] == "tenant_inactive"
    assert count(ctx, "leads") == 0


def test_reactivating_a_tenant_lets_its_quarantined_leads_be_replayed(client, ctx, mailer, clock):
    # Nothing was lost while the tenant was off.
    set_active(ctx, "tnt_sparkle", False)
    post_lead(client, ads_payload(ALAccountId="123456", Email="patient@example.com"))
    clock.advance(hours=1)
    set_active(ctx, "tnt_sparkle", True)

    response = client.post("/admin/api/events/replay-quarantined", params={"reason": "tenant_inactive"}, headers=ADMIN)

    assert response.status_code == 200
    assert [r["result"] for r in response.json()["results"]] == ["processed"]
    lead = db_one(ctx, "SELECT * FROM leads")
    assert lead["tenant_id"] == "tnt_sparkle"
    assert lead["received_at"] == iso(START)
    ctx.worker.deliver_due()
    assert [email.to for email in mailer.sent] == ["patient@example.com"]


# --- default fee from the account mapping ----------------------------------------------------


def test_angi_ads_lead_gets_the_accounts_default_fee(client, ctx):
    # The Angi Ads feed has no price, so the account's per-lead price is used.
    post_lead(client, ads_payload(ALAccountId="123456"))

    lead = db_one(ctx, "SELECT fee_cents, fee_source FROM leads")
    assert lead == {"fee_cents": 2800, "fee_source": "account_default"}


def test_mapping_without_a_default_fee_leaves_the_fee_unknown(client, ctx):
    # No price in the payload and none on the account: NULL, never a made-up number.
    post_lead(client, ads_payload(ALAccountId="999999"))

    lead = db_one(ctx, "SELECT fee_cents, fee_source FROM leads")
    assert lead == {"fee_cents": None, "fee_source": None}


def test_fee_in_the_payload_wins_over_the_accounts_default(client, ctx, clock):
    # What Angi says it charged beats our configured default (including a fee of 0).
    with ctx.db.transaction() as connection:
        add_mapping(
            connection, tenant_id="tnt_summit", source=SOURCE_ANGI, external_account_id="12345678",
            external_account_name=None, default_fee_cents=9900, now=clock(),
        )

    priced = post_lead(client, leads_payload(fee=25.67)).json()
    free = post_lead(
        client, leads_payload(fee=0, email="free@example.com", primaryPhone="7205550142")
    ).json()

    assert db_one(ctx, "SELECT fee_cents, fee_source FROM leads WHERE id = ?", (priced["leadId"],)) == {
        "fee_cents": 2567, "fee_source": "payload",
    }
    assert db_one(ctx, "SELECT fee_cents, fee_source FROM leads WHERE id = ?", (free["leadId"],)) == {
        "fee_cents": 0, "fee_source": "payload",
    }


def test_angi_leads_payload_without_a_fee_uses_the_accounts_default(client, ctx, clock):
    # The default also covers an Angi Leads payload whose fee went missing.
    with ctx.db.transaction() as connection:
        add_mapping(
            connection, tenant_id="tnt_summit", source=SOURCE_ANGI, external_account_id="12345678",
            external_account_name=None, default_fee_cents=9900, now=clock(),
        )
    payload = leads_payload()
    del payload["fee"]

    post_lead(client, payload)

    lead = db_one(ctx, "SELECT fee_cents, fee_source FROM leads")
    assert lead == {"fee_cents": 9900, "fee_source": "account_default"}


def test_learned_mapping_carries_no_default_fee(client, ctx):
    # A mapping learned from a tenant URL knows the tenant, not the price.
    post_lead(client, ads_payload(ALAccountId=UNMAPPED_ACCOUNT), headers={}, path=webhook_url(ctx, "tnt_summit"))

    lead = db_one(ctx, "SELECT fee_cents, fee_source FROM leads")
    assert lead == {"fee_cents": None, "fee_source": None}


def test_setting_the_default_fee_later_prices_the_leads_that_arrived_without_one(client, ctx, clock):
    # Two leads from one homeowner on a learned account: the fee is unknown, so is the
    # claim's amount. Once the operator says what the account pays per lead, both the
    # leads and the open claim carry it (instead of counting as $0 for ever).
    url = webhook_url(ctx, "tnt_sparkle")
    first = post_lead(client, ads_payload(ALAccountId=UNMAPPED_ACCOUNT), headers={}, path=url).json()
    clock.advance(hours=1)
    second = post_lead(client, ads_payload(ALAccountId=UNMAPPED_ACCOUNT), headers={}, path=url).json()
    claim = db_one(ctx, "SELECT * FROM credit_claims")
    assert claim["lead_id"] == second["leadId"]
    assert claim["amount_cents"] is None

    response = client.post(
        "/admin/api/tenants/tnt_sparkle/lead-sources",
        json={"external_account_id": UNMAPPED_ACCOUNT, "default_fee": 28},
        headers=ADMIN,
    )

    assert response.status_code == 201
    leads = db_rows(ctx, "SELECT id, fee_cents, fee_source FROM leads ORDER BY received_at")
    assert leads == [
        {"id": first["leadId"], "fee_cents": 2800, "fee_source": "account_default"},
        {"id": second["leadId"], "fee_cents": 2800, "fee_source": "account_default"},
    ]
    assert db_one(ctx, "SELECT amount_cents FROM credit_claims")["amount_cents"] == 2800


def test_default_fee_backfill_only_touches_that_accounts_unpriced_leads_and_open_claims(conn, clock):
    # A known price is never overwritten, other accounts and tenants are left alone, and a
    # claim that has already been settled keeps the amount it was settled with.
    def lead(lead_id, tenant_id, account, fee_cents, fee_source):
        conn.execute(
            """INSERT INTO leads (id, tenant_id, source, source_format, idempotency_key, external_account_id,
                                  fee_cents, fee_source, status, parse_mode, booking_token, received_at,
                                  created_at, updated_at)
               VALUES (?, ?, 'angi', 'angi_ads', ?, ?, ?, ?, 'new', 'strict', ?, ?, ?, ?)""",
            (lead_id, tenant_id, lead_id, account, fee_cents, fee_source, lead_id, iso(clock()), iso(clock()), iso(clock())),
        )

    def claim(lead_id, status):
        conn.execute(
            "INSERT INTO credit_claims (id, tenant_id, lead_id, reason, confidence, matched_on, status, created_at) "
            "VALUES (?, 'tnt_summit', ?, 'duplicate', 'high', '[]', ?, ?)",
            ("clm_" + lead_id, lead_id, status, iso(clock())),
        )

    lead("unpriced", "tnt_summit", "200001", None, None)
    lead("unpriced_submitted", "tnt_summit", "200001", None, None)
    lead("unpriced_denied", "tnt_summit", "200001", None, None)
    lead("priced_by_angi", "tnt_summit", "200001", 3300, "payload")
    lead("other_account", "tnt_summit", "12345678", None, None)
    lead("other_tenant", "tnt_blueline", "200001", None, None)  # from before the account changed hands
    claim("unpriced", "detected")
    claim("unpriced_submitted", "submitted")
    claim("unpriced_denied", "denied")
    claim("other_account", "detected")

    add_mapping(
        conn, tenant_id="tnt_summit", source=SOURCE_ANGI, external_account_id="200001",
        external_account_name=None, default_fee_cents=5000, now=clock(),
    )

    fees = {row["id"]: (row["fee_cents"], row["fee_source"]) for row in conn.execute("SELECT * FROM leads")}
    assert fees == {
        "unpriced": (5000, "account_default"),
        "unpriced_submitted": (5000, "account_default"),
        "unpriced_denied": (5000, "account_default"),
        "priced_by_angi": (3300, "payload"),
        "other_account": (None, None),
        "other_tenant": (None, None),
    }
    amounts = {row["lead_id"]: row["amount_cents"] for row in conn.execute("SELECT * FROM credit_claims")}
    assert amounts == {"unpriced": 5000, "unpriced_submitted": 5000, "unpriced_denied": None, "other_account": None}


def test_mapping_without_a_default_fee_backfills_nothing(client, ctx):
    # Re-saving a mapping without a price must not invent one.
    post_lead(client, ads_payload(ALAccountId=UNMAPPED_ACCOUNT), headers={}, path=webhook_url(ctx, "tnt_summit"))

    client.post("/admin/api/tenants/tnt_summit/lead-sources", json={"external_account_id": UNMAPPED_ACCOUNT}, headers=ADMIN)

    assert db_one(ctx, "SELECT fee_cents, fee_source FROM leads") == {"fee_cents": None, "fee_source": None}


# --- creating a tenant through the admin API --------------------------------------------------


def test_tenant_created_via_the_api_can_receive_leads_on_its_own_url(client, ctx, mailer):
    # Onboarding end to end: create the tenant, post to its URL, the account is learned.
    created = client.post(
        "/admin/api/tenants",
        json={"slug": "acme-roofing", "name": "Acme Roofing", "timezone": "America/New_York"},
        headers=ADMIN,
    )
    assert created.status_code == 201
    tenant = created.json()["tenant"]
    assert tenant["is_active"] == 1
    assert tenant["webhook_key"]

    body = post_lead(
        client, ads_payload(ALAccountId="700700"), headers={}, path=f"{WEBHOOK}/{tenant['webhook_key']}"
    ).json()

    assert body["result"] == "processed"
    assert body["tenant"] == {"id": tenant["id"], "name": "Acme Roofing", "routedBy": "url_key"}
    assert stored_mapping(ctx, "700700")["tenant_id"] == tenant["id"]
    ctx.worker.deliver_due()
    assert [email.from_name for email in mailer.sent] == ["Acme Roofing"]


@pytest.mark.parametrize(
    "overrides, expected_status",
    [
        ({"timezone": "Mars/Olympus_Mons"}, 422),
        ({"business_hours_start": 18, "business_hours_end": 9}, 422),
        ({"appointment_minutes": 0}, 422),
        ({"slug": "Has Spaces"}, 422),
        ({"slug": ""}, 422),
        ({"name": ""}, 422),
        ({"slug": "sparkle-home-cleaning"}, 409),  # already taken by a demo tenant
    ],
)
def test_tenant_api_rejects_invalid_or_duplicate_tenants(client, ctx, overrides, expected_status):
    # Validation errors are 422, a taken slug is 409, and nothing is created either way.
    tenants_before = count(ctx, "tenants")
    body = {"slug": "acme-roofing", "name": "Acme Roofing", **overrides}

    response = client.post("/admin/api/tenants", json=body, headers=ADMIN)

    assert response.status_code == expected_status
    assert count(ctx, "tenants") == tenants_before
