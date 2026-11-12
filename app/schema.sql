-- Schema for the Angi lead integration.
-- SQLite for the prototype; every statement is plain SQL that ports to Postgres.
-- Timestamps are UTC ISO-8601 text ("2026-11-11T22:31:05.123Z"); money is integer cents.

-- ---------------------------------------------------------------------------------
-- Tenants: the businesses we answer leads for (dummy data in this prototype).
-- ---------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tenants (
    id                   TEXT PRIMARY KEY,
    slug                 TEXT NOT NULL UNIQUE,
    name                 TEXT NOT NULL,
    industry             TEXT,
    timezone             TEXT NOT NULL DEFAULT 'America/Los_Angeles',
    phone                TEXT,
    reply_to_email       TEXT,
    business_hours_start INTEGER NOT NULL DEFAULT 8,    -- local hour, inclusive
    business_hours_end   INTEGER NOT NULL DEFAULT 17,   -- local hour, exclusive
    appointment_minutes  INTEGER NOT NULL DEFAULT 120,  -- length of an arrival window
    -- Secret path segment for a tenant-specific webhook URL:
    --   POST /webhooks/angi/leads/{webhook_key}
    webhook_key          TEXT UNIQUE,
    is_active            INTEGER NOT NULL DEFAULT 1,
    is_test              INTEGER NOT NULL DEFAULT 0,    -- synthetic-canary tenant
    created_at           TEXT NOT NULL
);

-- ---------------------------------------------------------------------------------
-- The mapping mechanism: which lead-source account belongs to which tenant.
-- Angi identifies the business in every payload (spEntityId / ALAccountId); one tenant
-- can own several Angi accounts (multi-location businesses).
-- ---------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tenant_lead_sources (
    id                    TEXT PRIMARY KEY,
    tenant_id             TEXT NOT NULL REFERENCES tenants(id),
    source                TEXT NOT NULL,              -- 'angi'
    external_account_id   TEXT NOT NULL,              -- Angi's id for the business
    external_account_name TEXT,
    -- What this account pays per lead, used when the payload carries no fee (the
    -- Angi Ads feed has none) so spend and credit amounts can still be computed.
    default_fee_cents     INTEGER,
    learned               INTEGER NOT NULL DEFAULT 0, -- 1 = auto-registered from a tenant-keyed URL / crmKey
    is_active             INTEGER NOT NULL DEFAULT 1,
    created_at            TEXT NOT NULL,
    UNIQUE (source, external_account_id)
);

-- ---------------------------------------------------------------------------------
-- Every inbound request, verbatim, written BEFORE any parsing. This is what makes the
-- system safe against upstream format changes: nothing is lost and anything can be
-- replayed once the parser or the mapping is fixed.
-- ---------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS webhook_events (
    id              TEXT PRIMARY KEY,
    source          TEXT NOT NULL,
    received_at     TEXT NOT NULL,
    remote_addr     TEXT,
    content_type    TEXT,
    headers_json    TEXT NOT NULL,       -- credentials redacted
    raw_body        TEXT NOT NULL,
    body_sha256     TEXT NOT NULL,
    url_tenant_id   TEXT,                -- tenant implied by the URL key, if one was used
    operator_tenant_id TEXT,             -- tenant an operator assigned by hand when replaying the event
    -- received | processed | duplicate_delivery | quarantined | rejected | failed | discarded
    status          TEXT NOT NULL,
    status_reason   TEXT,
    detected_format TEXT,
    parse_mode      TEXT,                -- strict | tolerant | failed
    external_account_id TEXT,            -- Angi account in the payload (a new mapping replays just its leads)
    issues_json     TEXT,
    tenant_id       TEXT,
    lead_id         TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0,
    processing_ms   REAL,
    processed_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_received ON webhook_events(received_at);
CREATE INDEX IF NOT EXISTS idx_events_status   ON webhook_events(status, received_at);

-- ---------------------------------------------------------------------------------
-- Leads in one canonical shape, whatever format Angi sent.
-- ---------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS leads (
    id                    TEXT PRIMARY KEY,
    tenant_id             TEXT REFERENCES tenants(id),   -- always set: an unroutable request stays in webhook_events
    source                TEXT NOT NULL,                 -- 'angi'
    source_format         TEXT NOT NULL,                 -- which Angi payload variant
    -- Idempotency key: the same Angi lead is stored exactly once however many times
    -- it is delivered. "<source>:<account id or ->:<lead id>" or, with no lead id,
    -- "<source>:body:<hash of the request body>". The payload format is not part of it.
    -- (A delivery that lost or gained its account id is matched to the stored lead by
    -- lead id; see IngestService._stored_lead.)
    idempotency_key       TEXT NOT NULL UNIQUE,
    external_lead_id      TEXT,
    external_request_id   TEXT,
    external_account_id   TEXT,
    external_account_name TEXT,
    routed_by             TEXT,                          -- operator | url_key | crm_key | account_id | fallback

    first_name            TEXT,
    last_name             TEXT,
    full_name             TEXT,
    email                 TEXT,                          -- as received
    email_normalized      TEXT,                          -- NULL when not a usable address
    phone                 TEXT,
    phone_normalized      TEXT,
    phone_ext             TEXT,
    phone_is_masked       INTEGER,                       -- 1 = temporary Angi proxy number
    secondary_phone       TEXT,
    secondary_phone_normalized TEXT,
    address_line1         TEXT,
    address_line2         TEXT,
    city                  TEXT,
    state                 TEXT,
    postal_code           TEXT,

    task_name             TEXT,                          -- the service requested
    market                TEXT,                          -- Angi's market, when the payload names one
    comments              TEXT,
    urgency               TEXT,
    match_type            TEXT,
    lead_description      TEXT,
    lead_source_detail    TEXT,
    contact_status        TEXT,
    fee_cents             INTEGER,                       -- what the tenant paid for the lead
    fee_source            TEXT,                          -- payload | account_default
    interview_json        TEXT,
    trusted_form_url      TEXT,
    automated_contact_ok  INTEGER,                       -- consent to automated calls/texts (NULL = unknown)
    extras_json           TEXT,                          -- fields we do not model yet

    -- new | contacted | engaged | booked | won | lost
    status                TEXT NOT NULL,
    parse_mode            TEXT NOT NULL,                 -- strict | tolerant
    needs_review          INTEGER NOT NULL DEFAULT 0,

    is_duplicate          INTEGER NOT NULL DEFAULT 0,
    duplicate_of_lead_id  TEXT REFERENCES leads(id),
    duplicate_matched_on  TEXT,                          -- JSON list, e.g. ["phone","email"]

    booking_token         TEXT NOT NULL UNIQUE,

    -- Funnel timestamps. Analytics is computed from these, never from `status`,
    -- so a lead that was booked and later lost still counts as booked.
    received_at           TEXT NOT NULL,                 -- speed-to-lead clock starts here
    last_delivery_at      TEXT,                          -- arrival time of the newest delivery applied to this row
    first_contacted_at    TEXT,
    first_engaged_at      TEXT,
    booked_at             TEXT,
    closed_at             TEXT,
    outcome               TEXT,                          -- won | lost
    outcome_reason        TEXT,
    job_value_cents       INTEGER,

    first_event_id        TEXT REFERENCES webhook_events(id),
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_leads_tenant_received ON leads(tenant_id, received_at);
CREATE INDEX IF NOT EXISTS idx_leads_phone  ON leads(tenant_id, phone_normalized);
CREATE INDEX IF NOT EXISTS idx_leads_phone2 ON leads(tenant_id, secondary_phone_normalized);
CREATE INDEX IF NOT EXISTS idx_leads_email  ON leads(tenant_id, email_normalized);
CREATE INDEX IF NOT EXISTS idx_leads_request ON leads(source, external_request_id);
CREATE INDEX IF NOT EXISTS idx_leads_external ON leads(source, external_lead_id);   -- the redelivery lookup

-- Append-only lifecycle log: the raw material for funnel analytics and the audit trail
-- shown on each lead's timeline.
CREATE TABLE IF NOT EXISTS lead_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id     TEXT NOT NULL REFERENCES leads(id),
    tenant_id   TEXT,
    type        TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    data_json   TEXT
);
CREATE INDEX IF NOT EXISTS idx_lead_events_lead ON lead_events(lead_id, id);
CREATE INDEX IF NOT EXISTS idx_lead_events_type ON lead_events(type, occurred_at);

-- ---------------------------------------------------------------------------------
-- Transactional outbox: a message row is written in the same transaction as the lead,
-- then a worker delivers it. A crash between "lead stored" and "email sent" cannot
-- lose the email, and `dedupe_key` guarantees one message of each kind per lead.
-- ---------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS outbox_messages (
    id                  TEXT PRIMARY KEY,
    lead_id             TEXT NOT NULL REFERENCES leads(id),
    tenant_id           TEXT NOT NULL REFERENCES tenants(id),
    channel             TEXT NOT NULL DEFAULT 'email',
    template            TEXT NOT NULL,       -- intro | repeat_request | followup | booking_confirmation
    sequence            INTEGER NOT NULL DEFAULT 0,
    to_address          TEXT NOT NULL,
    dedupe_key          TEXT NOT NULL UNIQUE,
    -- pending | sending | sent | failed | cancelled
    status              TEXT NOT NULL DEFAULT 'pending',
    send_after          TEXT NOT NULL,
    attempts            INTEGER NOT NULL DEFAULT 0,
    max_attempts        INTEGER NOT NULL DEFAULT 5,
    last_error          TEXT,
    locked_at           TEXT,
    subject             TEXT,                -- filled in when sent (audit copy)
    body_text           TEXT,
    provider            TEXT,
    provider_message_id TEXT,
    created_at          TEXT NOT NULL,
    sent_at             TEXT
);
CREATE INDEX IF NOT EXISTS idx_outbox_due  ON outbox_messages(status, send_after);
CREATE INDEX IF NOT EXISTS idx_outbox_lead ON outbox_messages(lead_id);
-- "Has this tenant already written to this address?" (intro or acknowledgement for a duplicate)
CREATE INDEX IF NOT EXISTS idx_outbox_address ON outbox_messages(tenant_id, to_address);

CREATE TABLE IF NOT EXISTS appointments (
    id         TEXT PRIMARY KEY,
    lead_id    TEXT NOT NULL REFERENCES leads(id),
    tenant_id  TEXT NOT NULL REFERENCES tenants(id),
    starts_at  TEXT NOT NULL,
    ends_at    TEXT NOT NULL,
    timezone   TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'scheduled',   -- scheduled | cancelled
    created_at TEXT NOT NULL
);
-- One live appointment per lead, and no double-booking of a tenant's slot.
CREATE UNIQUE INDEX IF NOT EXISTS idx_appt_lead ON appointments(lead_id) WHERE status = 'scheduled';
CREATE UNIQUE INDEX IF NOT EXISTS idx_appt_slot ON appointments(tenant_id, starts_at) WHERE status = 'scheduled';

-- Addresses that asked not to be emailed again (per tenant).
CREATE TABLE IF NOT EXISTS email_suppressions (
    tenant_id        TEXT NOT NULL,
    email_normalized TEXT NOT NULL,
    reason           TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    PRIMARY KEY (tenant_id, email_normalized)
);

-- ---------------------------------------------------------------------------------
-- Lead-credit ("rebate") claims: one row per lead we believe the tenant should not
-- have paid for, with the evidence needed to request the credit from Angi.
-- ---------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS credit_claims (
    id               TEXT PRIMARY KEY,
    tenant_id        TEXT NOT NULL REFERENCES tenants(id),
    lead_id          TEXT NOT NULL UNIQUE REFERENCES leads(id),   -- the lead to be credited
    original_lead_id TEXT REFERENCES leads(id),                   -- the earlier lead it duplicates
    reason           TEXT NOT NULL,          -- duplicate
    confidence       TEXT NOT NULL,          -- high | medium | low
    matched_on       TEXT NOT NULL,          -- JSON list of matching fields
    same_task        INTEGER NOT NULL DEFAULT 0,
    hours_apart      REAL,
    amount_cents     INTEGER,                -- fee paid for the duplicate
    -- detected | submitted | credited | denied | dismissed
    status           TEXT NOT NULL DEFAULT 'detected',
    request_deadline TEXT,                   -- last day Angi accepts the credit request
    submitted_at     TEXT,
    resolved_at      TEXT,
    credited_cents   INTEGER,
    notes            TEXT,
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_claims_tenant ON credit_claims(tenant_id, status);

-- ---------------------------------------------------------------------------------
-- Monitoring.
-- ---------------------------------------------------------------------------------
-- Every distinct way a payload has deviated from the documented format.
CREATE TABLE IF NOT EXISTS payload_shapes (
    signature       TEXT PRIMARY KEY,
    source          TEXT NOT NULL,
    detected_format TEXT NOT NULL,
    parse_mode      TEXT NOT NULL,
    is_clean        INTEGER NOT NULL,        -- 1 = exactly the documented shape
    detail_json     TEXT NOT NULL,           -- unknown / missing / mistyped fields
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    seen_count      INTEGER NOT NULL DEFAULT 1,
    sample_event_id TEXT
);

CREATE TABLE IF NOT EXISTS alerts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    kind          TEXT NOT NULL,
    dedupe_key    TEXT NOT NULL,
    severity      TEXT NOT NULL,             -- info | warning | critical
    title         TEXT NOT NULL,
    detail_json   TEXT,
    status        TEXT NOT NULL DEFAULT 'open',   -- open | resolved
    count         INTEGER NOT NULL DEFAULT 1,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    notified_at   TEXT,
    resolved_at   TEXT
);
-- At most one open alert per problem: repeats bump `count` instead of paging again.
CREATE UNIQUE INDEX IF NOT EXISTS idx_alerts_open ON alerts(dedupe_key) WHERE status = 'open';

CREATE TABLE IF NOT EXISTS kv (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
