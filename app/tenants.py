"""Tenants and the mapping mechanism from an Angi payload to a tenant.

How a lead finds its tenant, strongest signal first:

1. **Tenant-keyed URL** - ``POST /webhooks/angi/leads/{webhook_key}``. The URL was
   issued to exactly one tenant. (An operator assigning a quarantined event to a tenant
   by hand counts the same way, and comes before the URL.)
2. **crmKey in the payload** - Angi lets the integrator choose a routing value per
   business and echoes it back in every lead (Angi Leads feed). We use the tenant's
   id or slug, so it is our own identifier coming back to us.
3. **Angi account id in the payload** - ``ALAccountId`` (Angi Ads) / ``spEntityId``
   (Angi Leads), looked up in ``tenant_lead_sources``. This is the everyday path.
4. **Fallback tenant** - only if ``FALLBACK_TENANT_SLUG`` is configured (demo use).
5. Otherwise the lead is **quarantined**: stored and alerted on. A lead for an unmapped
   account is processed automatically as soon as someone adds the mapping; a lead that
   names no account at all waits for an operator to assign it. We never guess which
   business a consumer contacted, because the email goes out under that business's name.

When 1 or 2 identifies the tenant and the payload carries an account id we have not
seen, the id is *learned* (added to ``tenant_lead_sources``), so onboarding a tenant's
additional Angi accounts needs no manual step. When 1 or 2 names one tenant and the
account id is already mapped to *another*, nobody wins: the two signals contradict
each other, so the lead is quarantined (``mapping_conflict``) for a person to decide.
"""

from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import available_timezones

from app.alerts import resolve_alert
from app.repo import get_tenant, get_tenant_by_slug
from app.util import iso, new_id, normalize_email, one_line

SOURCE_ANGI = "angi"


@dataclass
class Routing:
    tenant: dict | None
    routed_by: str | None = None  # operator | url_key | crm_key | account_id | fallback
    # Why there is no tenant: unmapped_account | no_account_id | tenant_inactive | mapping_conflict
    reason: str | None = None
    mapping: dict | None = None  # the tenant_lead_sources row for the payload's account id
    conflict: dict | None = None  # explicit routing and the account-id mapping disagree
    learned_mapping: bool = False
    notes: list[str] = field(default_factory=list)


def find_mapping(conn: sqlite3.Connection, source: str, external_account_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM tenant_lead_sources WHERE source = ? AND external_account_id = ? AND is_active = 1",
        (source, external_account_id),
    ).fetchone()
    return dict(row) if row else None


def find_tenant_by_crm_key(conn: sqlite3.Connection, crm_key: str) -> dict | None:
    row = conn.execute("SELECT * FROM tenants WHERE id = ? OR slug = ?", (crm_key, crm_key)).fetchone()
    return dict(row) if row else None


def explicit_tenant_id(
    conn: sqlite3.Connection,
    *,
    operator_tenant_id: str | None = None,
    url_tenant_id: str | None = None,
    crm_key: str | None = None,
) -> tuple[str | None, str | None]:
    """-> (tenant id, how) for a request that names its tenant outright: an operator's
    assignment, the tenant URL it was posted to, or our crmKey echoed back by Angi.
    (None, None) when it does not. The id is returned even if no such tenant exists any
    more, so a stale URL is never mistaken for "no URL"."""
    if operator_tenant_id:
        return operator_tenant_id, "operator"
    if url_tenant_id:
        return url_tenant_id, "url_key"
    tenant = find_tenant_by_crm_key(conn, crm_key) if crm_key else None
    return (tenant["id"], "crm_key") if tenant else (None, None)


def resolve_tenant(
    conn: sqlite3.Connection,
    *,
    source: str,
    external_account_id: str | None,
    external_account_name: str | None,
    operator_tenant_id: str | None = None,
    url_tenant_id: str | None = None,
    crm_key: str | None = None,
    fallback_slug: str | None = None,
    now: datetime,
) -> Routing:
    mapping = find_mapping(conn, source, external_account_id) if external_account_id else None

    # 1 + 2. Explicit routing: an operator's decision, our own URL key, or our own
    # crmKey echoed back by Angi.
    explicit_id, routed_by = explicit_tenant_id(
        conn, operator_tenant_id=operator_tenant_id, url_tenant_id=url_tenant_id, crm_key=crm_key
    )
    if explicit_id:
        explicit = get_tenant(conn, explicit_id)
        if explicit is None or not explicit["is_active"]:
            # Deactivated, or a URL whose tenant has since been deleted. Not re-routed
            # by the payload: whoever holds that URL must not reach another tenant.
            return Routing(tenant=None, reason="tenant_inactive", mapping=mapping)
        if mapping and mapping["tenant_id"] != explicit["id"]:
            # The request says one tenant, our records say this Angi account belongs to
            # another. Letting the URL or crmKey win would let one tenant's URL create
            # leads (and send emails) for another tenant's account, so nobody wins.
            return Routing(
                tenant=None,
                reason="mapping_conflict",
                conflict={
                    "external_account_id": external_account_id,
                    "routed_tenant_id": explicit["id"],
                    "mapped_tenant_id": mapping["tenant_id"],
                    "routed_by": routed_by,
                },
            )
        routing = Routing(tenant=explicit, routed_by=routed_by, mapping=mapping)
        if external_account_id and not mapping:
            routing.mapping = add_mapping(
                conn,
                tenant_id=explicit["id"],
                source=source,
                external_account_id=external_account_id,
                external_account_name=external_account_name,
                learned=True,
                now=now,
            )
            routing.learned_mapping = True
            routing.notes.append(f"learned mapping {source}:{external_account_id} -> {explicit['slug']}")
        return routing

    # 3. Account id carried in the payload.
    if mapping:
        tenant = get_tenant(conn, mapping["tenant_id"])
        if tenant and tenant["is_active"]:
            return Routing(tenant=tenant, routed_by="account_id", mapping=mapping)
        return Routing(tenant=None, reason="tenant_inactive", mapping=mapping)

    # 4. Explicitly configured fallback (demo convenience).
    if fallback_slug:
        tenant = get_tenant_by_slug(conn, fallback_slug)
        if tenant and tenant["is_active"]:
            return Routing(
                tenant=tenant,
                routed_by="fallback",
                notes=[f"no tenant mapping for Angi account {external_account_id!r}; used the fallback tenant"],
            )

    # 5. Unknown business: quarantine rather than guess. With an account id the lead is
    # released as soon as that account is mapped; with none, only a person can place it.
    return Routing(tenant=None, reason="unmapped_account" if external_account_id else "no_account_id")


# --- management ----------------------------------------------------------------------


def valid_timezone(name: str) -> bool:
    """A real IANA zone name, spelled exactly. (Asking ZoneInfo to open the name is not
    enough: "America" is a directory, and a wrong-case name opens on some filesystems
    and not others.)"""
    return isinstance(name, str) and name in available_timezones()


def create_tenant(
    conn: sqlite3.Connection,
    *,
    slug: str,
    name: str,
    now: datetime,
    industry: str | None = None,
    timezone: str = "America/Los_Angeles",
    phone: str | None = None,
    reply_to_email: str | None = None,
    business_hours_start: int = 8,
    business_hours_end: int = 17,
    appointment_minutes: int = 120,
    is_test: bool = False,
    webhook_key: str | None = None,
    tenant_id: str | None = None,
) -> dict:
    name = one_line(name, 120)  # it becomes an email From name: one line, no control characters
    if not slug or not name:
        raise ValueError("slug and name are required")
    if not valid_timezone(timezone):
        raise ValueError(f"unknown timezone {timezone!r}")
    if reply_to_email is not None:
        reply_to_email = reply_to_email.strip() or None
    if reply_to_email is not None and normalize_email(reply_to_email) is None:
        raise ValueError("reply_to_email must be a single valid email address")
    if not (0 <= business_hours_start < business_hours_end <= 24):
        raise ValueError("business hours must satisfy 0 <= start < end <= 24")
    if appointment_minutes <= 0:
        raise ValueError("appointment_minutes must be positive")
    tenant_id = tenant_id or new_id("tnt")
    conn.execute(
        """INSERT INTO tenants (id, slug, name, industry, timezone, phone, reply_to_email,
                                business_hours_start, business_hours_end, appointment_minutes,
                                webhook_key, is_test, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            tenant_id,
            slug,
            name,
            industry,
            timezone,
            phone,
            reply_to_email,
            business_hours_start,
            business_hours_end,
            appointment_minutes,
            webhook_key or secrets.token_urlsafe(18),
            1 if is_test else 0,
            iso(now),
        ),
    )
    tenant = get_tenant(conn, tenant_id)
    assert tenant is not None
    return tenant


def add_mapping(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    source: str,
    external_account_id: str,
    external_account_name: str | None,
    now: datetime,
    learned: bool = False,
    default_fee_cents: int | None = None,
) -> dict:
    """Map a lead-source account to a tenant. Re-pointing an existing account id at a
    different tenant is allowed (an account can change hands) and is an explicit update."""
    external_account_id = str(external_account_id).strip()
    if not external_account_id:
        raise ValueError("external_account_id is required")
    conn.execute(
        """INSERT INTO tenant_lead_sources
               (id, tenant_id, source, external_account_id, external_account_name,
                default_fee_cents, learned, is_active, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
           ON CONFLICT(source, external_account_id) DO UPDATE SET
               tenant_id = excluded.tenant_id,
               external_account_name = COALESCE(excluded.external_account_name, external_account_name),
               default_fee_cents = COALESCE(excluded.default_fee_cents, default_fee_cents),
               learned = excluded.learned,
               is_active = 1""",
        (
            new_id("src"),
            tenant_id,
            source,
            external_account_id,
            external_account_name,
            default_fee_cents,
            1 if learned else 0,
            iso(now),
        ),
    )
    # The account has a tenant now, so "this account is not mapped" is no longer true.
    resolve_alert(conn, f"unmapped_account:{external_account_id}", now)
    if default_fee_cents is not None:
        # Leads of this account that arrived before its price was known (the Angi Ads
        # feed carries none) get it now, and so do their open credit claims. Otherwise
        # they would keep counting as $0 of spend and $0 to claim back.
        scope = "tenant_id = ? AND source = ? AND external_account_id = ?"
        account = (tenant_id, source, external_account_id)
        conn.execute(
            f"UPDATE leads SET fee_cents = ?, fee_source = 'account_default', updated_at = ? "
            f"WHERE {scope} AND fee_cents IS NULL",
            (default_fee_cents, iso(now), *account),
        )
        conn.execute(
            f"""UPDATE credit_claims
                SET amount_cents = (SELECT fee_cents FROM leads WHERE leads.id = credit_claims.lead_id)
                WHERE amount_cents IS NULL AND status IN ('detected', 'submitted')
                  AND lead_id IN (SELECT id FROM leads WHERE {scope})""",
            account,
        )
    row = conn.execute(
        "SELECT * FROM tenant_lead_sources WHERE source = ? AND external_account_id = ?",
        (source, external_account_id),
    ).fetchone()
    return dict(row)


def list_tenants(conn: sqlite3.Connection) -> list[dict]:
    tenants = [dict(r) for r in conn.execute("SELECT * FROM tenants ORDER BY created_at, slug")]
    for tenant in tenants:
        tenant["lead_sources"] = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM tenant_lead_sources WHERE tenant_id = ? ORDER BY created_at",
                (tenant["id"],),
            )
        ]
    return tenants
