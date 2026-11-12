"""Demo data: a few made-up tenants and their Angi account mappings.

The account ids deliberately include the ones used in Angi's own documentation
samples (``ALAccountId`` 123456 in the JSON Lead Feed spec, ``spEntityId`` 12345678 in
the Lead Integration API article), so posting a documentation example works out of the
box. Everything here is fictional.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from app.tenants import SOURCE_ANGI, add_mapping, create_tenant

CANARY_ACCOUNT_ID = "999999"

DEMO_TENANTS = [
    {
        "tenant_id": "tnt_sparkle",
        "slug": "sparkle-home-cleaning",
        "name": "Sparkle Home Cleaning",
        "industry": "House cleaning",
        "timezone": "America/Indiana/Indianapolis",
        "phone": "(317) 555-0142",
        "accounts": [
            # (Angi account id, name on Angi, what the account pays per lead in cents)
            ("123456", "Sparkle Home Cleaning", 2800),
        ],
    },
    {
        "tenant_id": "tnt_summit",
        "slug": "summit-heating-air",
        "name": "Summit Heating & Air",
        "industry": "HVAC",
        "timezone": "America/Denver",
        "phone": "(303) 555-0117",
        "accounts": [
            ("200001", "Summit Heating & Air", 4500),
            ("12345678", "Summit Heating & Air (Angi Leads)", None),
        ],
    },
    {
        "tenant_id": "tnt_blueline",
        "slug": "blueline-plumbing",
        "name": "BlueLine Plumbing",
        "industry": "Plumbing",
        "timezone": "America/Chicago",
        "phone": "(512) 555-0188",
        "accounts": [
            ("200002", "BlueLine Plumbing", 3900),
            ("567890123", "BlueLine Plumbing (Angi Leads)", None),
        ],
    },
    {
        "tenant_id": "tnt_canary",
        "slug": "synthetic-canary",
        "name": "Synthetic Canary Co.",
        "industry": "Monitoring probe",
        "timezone": "America/Los_Angeles",
        "phone": None,
        "is_test": True,
        "accounts": [(CANARY_ACCOUNT_ID, "Synthetic canary", None)],
    },
]


def seed_demo(conn: sqlite3.Connection, now: datetime) -> int:
    """Insert the demo tenants if the database has none. Returns how many were created."""
    if conn.execute("SELECT 1 FROM tenants LIMIT 1").fetchone() is not None:
        return 0
    for spec in DEMO_TENANTS:
        tenant = create_tenant(
            conn,
            tenant_id=spec["tenant_id"],
            slug=spec["slug"],
            name=spec["name"],
            industry=spec["industry"],
            timezone=spec["timezone"],
            phone=spec["phone"],
            is_test=spec.get("is_test", False),
            now=now,
        )
        for account_id, account_name, fee_cents in spec["accounts"]:
            add_mapping(
                conn,
                tenant_id=tenant["id"],
                source=SOURCE_ANGI,
                external_account_id=account_id,
                external_account_name=account_name,
                default_fee_cents=fee_cents,
                now=now,
            )
    return len(DEMO_TENANTS)
