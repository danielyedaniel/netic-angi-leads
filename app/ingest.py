"""The lead intake pipeline.

    HTTP request
      -> 1. store the raw request            (webhook_events, status=received)
      -> 2. decode + parse                    (app/angi/parser.py)
      -> 3. one transaction:
            idempotency check  -> same Angi lead again? update or no-op, never a 2nd lead
            tenant mapping     -> unknown business, or signals that disagree? quarantine + alert
            duplicate check    -> same consumer, new lead? flag + credit claim
            insert lead + lifecycle events + outbox rows (intro email, follow-ups)
      -> 4. wake the worker so the email leaves within about a second

Design rule: once step 1 has committed, the lead cannot be lost. Whatever goes wrong
afterwards (a format change, a missing mapping, a bug), the raw request is on disk and
``process_event`` can simply be run again - by the retry loop or from the dashboard.

That is also why the HTTP answer is "success" for everything we managed to store.
Angi treats anything other than HTTP 200 with a success body as a failed delivery and
retries it three times, fifteen minutes apart. A retry of the same payload cannot fix
an unmapped account or a parser bug on our side - only we can, by replaying the stored
event - and fifteen minutes is far too slow for a lead anyway.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from app import outbox
from app.alerts import raise_alert, resolve_alert, resolve_quarantine_alert
from app.angi.parser import UNKNOWN_FORMAT, ParseResult, json_type, parse_angi_payload
from app.cadence import defer_past_quiet_hours
from app.config import Settings
from app.db import Database
from app.duplicates import DuplicateMatch, find_duplicate, flag_later_duplicates, record_duplicate
from app.models import NormalizedLead
from app.repo import (
    EV_EMAIL_SKIPPED,
    EV_RECEIVED,
    EV_REDELIVERED,
    EV_UPDATED,
    add_lead_event,
    get_tenant,
    is_suppressed,
)
from app.tenants import SOURCE_ANGI, Routing, explicit_tenant_id, resolve_tenant
from app.util import dumps, iso, new_id, new_token, parse_iso

log = logging.getLogger("app.ingest")

SENSITIVE_HEADER_HINTS = ("authorization", "cookie", "key", "token", "secret", "password", "signature")

# Why an event is waiting in quarantine, in words the sender (and our own logs) can act
# on. Each one says truthfully what has to happen before the lead is processed.
QUARANTINE_MESSAGES = {
    "unrecognised_payload": "Stored for review. No lead was created: the payload matched no known Angi format "
    "and contained no usable contact details.",
    "unmapped_account": "Stored, but not processed yet: this Angi account is not mapped to a tenant. "
    "It will be processed automatically once the mapping is added.",
    "no_account_id": "Stored, but not processed yet: the payload names no Angi account, so it cannot be "
    "matched to a tenant. An operator has to assign it to one.",
    "tenant_inactive": "Stored, but not processed: the tenant this lead is for is deactivated. "
    "It can be replayed once the tenant is active again.",
    "mapping_conflict": "Stored, but not processed: the request is addressed to one tenant while its Angi "
    "account is mapped to another. An operator has to decide which of the two is right.",
    "tenant_conflict": "Stored, but nothing was changed: this lead id already belongs to a different tenant "
    "than the one the request is addressed to. An operator has to review it.",
}


@dataclass
class IngestOutcome:
    http_status: int
    body: dict[str, Any]
    # Set when this pass taught us which tenant an Angi account belongs to (not sent to
    # Angi): leads quarantined for that account can be released once it has committed.
    learned_account_id: str | None = None


def success(result: str, message: str, **fields: Any) -> IngestOutcome:
    """HTTP 200 + ``{"status": "success"}`` is the only thing Angi counts as delivered
    (both of its feed specs say so); ``result`` carries what actually happened."""
    return IngestOutcome(200, {"status": "success", "result": result, "message": message, **fields})


def _log_event(event: dict, result: str, started: float) -> None:
    """One line per pass over an event, for tracing a lead through the logs. Ids only:
    never a name, an address, a phone number or a key."""
    log.info(
        "webhook event %s: result=%s lead=%s tenant=%s format=%s parse_mode=%s ms=%.1f",
        event["id"],
        result,
        event.get("lead_id"),
        event.get("tenant_id"),
        event.get("detected_format"),
        event.get("parse_mode"),
        (time.perf_counter() - started) * 1000,
    )


def lead_columns(lead: NormalizedLead) -> dict[str, Any]:
    """The lead's own data as database columns (shared by insert and update)."""

    def flag(value: bool | None) -> int | None:
        return None if value is None else int(value)

    return {
        "first_name": lead.first_name,
        "last_name": lead.last_name,
        "full_name": lead.full_name,
        "email": lead.email,
        "email_normalized": lead.email_normalized,
        "phone": lead.phone,
        "phone_normalized": lead.phone_normalized,
        "phone_ext": lead.phone_ext,
        "phone_is_masked": flag(lead.phone_is_masked),
        "secondary_phone": lead.secondary_phone,
        "secondary_phone_normalized": lead.secondary_phone_normalized,
        "address_line1": lead.address_line1,
        "address_line2": lead.address_line2,
        "city": lead.city,
        "state": lead.state,
        "postal_code": lead.postal_code,
        "task_name": lead.task_name,
        "market": lead.market,
        "comments": lead.comments,
        "urgency": lead.urgency,
        "match_type": lead.match_type,
        "lead_description": lead.lead_description,
        "lead_source_detail": lead.lead_source_detail,
        "contact_status": lead.contact_status,
        "fee_cents": lead.fee_cents,
        "interview_json": dumps([item.model_dump() for item in lead.interview]) if lead.interview else None,
        "trusted_form_url": lead.trusted_form_url,
        "automated_contact_ok": flag(lead.automated_contact_ok),
        "extras_json": dumps(lead.extras) if lead.extras else None,
    }


def idempotency_key(lead: NormalizedLead, body_sha256: str) -> str:
    """What makes two deliveries "the same lead": Angi's lead id, scoped to the Angi
    account it was sold to. The payload format is deliberately not part of it: how we
    happened to parse a delivery must never turn a retry into a second lead. With no
    lead id (a drifted payload) fall back to a hash of the body, which still collapses
    Angi's exact retries."""
    if lead.external_lead_id:
        return f"{SOURCE_ANGI}:{lead.external_account_id or '-'}:{lead.external_lead_id}"
    return f"{SOURCE_ANGI}:body:{body_sha256}"


def _needs_review(lead: NormalizedLead, parsed: ParseResult) -> bool:
    """A person should look at a lead we cannot email, one whose fields had to be
    guessed by alias, and one without an Angi lead id (retries cannot be recognised)."""
    return not lead.email_normalized or bool(parsed.drift.recovered) or not lead.external_lead_id


# --- decoding ----------------------------------------------------------------------------

_CURLY_DOUBLE_QUOTES = str.maketrans({"\u201c": '"', "\u201d": '"'})
_CURLY_SINGLE_QUOTES = str.maketrans({"\u2018": "'", "\u2019": "'"})
_KEY_VALUE = re.compile(
    r'"(?P<key>[A-Za-z_][\w .-]{0,60})"\s*:\s*'
    r'(?:"(?P<quoted>(?:[^"\\]|\\.)*)"|(?P<bare>[^\s,{}\[\]"][^,}\]\r\n]*))'
)
_TRAILING_COMMA = re.compile(r",(\s*[}\]])")
_INTERVIEW_ITEM_KEYS = frozenset({"question", "answer"})


def _repair_json(text: str) -> dict | None:
    """Fix the two slips that leave a body one character away from valid JSON, keeping
    its structure: a raw line break or tab inside a string (``strict=False`` accepts
    those) and a comma before a closing bracket."""
    for candidate in (text, _TRAILING_COMMA.sub(r"\1", text)):
        try:
            repaired = _loads(candidate, strict=False)
        except (ValueError, RecursionError):
            continue
        if isinstance(repaired, dict):
            return repaired
    return None


def salvage_json(raw: str) -> dict | None:
    """Recover a JSON object from a body that is *almost* JSON.

    Not hypothetical: the example in Angi's own spec is not valid JSON (curly quotes,
    an unquoted email, a missing comma). Rather than drop such a lead we repair what
    can be repaired without losing anything; only if that fails do we pull out the
    ``"key": value`` pairs we can see and let the tolerant parser take it from there.
    That last resort loses nesting (``PostalAddress.City`` becomes ``City``, which alias
    matching handles) and the interview list, hence "last".
    """
    text = raw.translate(_CURLY_SINGLE_QUOTES)
    if '"' not in text:
        # No straight quotes at all: the curly ones are standing in for the delimiters.
        # (When the delimiters are fine, curly quotes inside a value are just text.)
        text = text.translate(_CURLY_DOUBLE_QUOTES)
    for candidate in (raw, text):  # the body as sent first, so the consumer's own text is kept as written
        repaired = _repair_json(candidate)
        if repaired is not None:
            return repaired
    pairs = _visible_pairs(text)
    if pairs is None and ("\u201c" in text or "\u201d" in text):
        # Curly and straight delimiters mixed (the spec example with one line retyped by
        # hand): nothing was readable as it stands, so read every one of them as a quote.
        straightened = text.translate(_CURLY_DOUBLE_QUOTES)
        pairs = _repair_json(straightened) or _visible_pairs(straightened)
    return pairs


class _ReadPairByPair(dict):
    """A body that could only be read pair by pair: its nesting and its interview list
    are gone. An ordinary dict otherwise; the type is how ``_warnings`` knows."""


def _visible_pairs(text: str) -> dict | None:
    """The ``"key": value`` pairs that can be seen in ``text``, or None when there are
    too few of them to be a lead or they belong to more than one object."""
    pairs: dict[str, Any] = _ReadPairByPair()
    for match in _KEY_VALUE.finditer(text):
        if match["quoted"] is not None:
            value: Any = _unescape(match["quoted"])
        else:
            bare = match["bare"].strip()
            value = {"null": None, "true": True, "false": False}.get(bare, bare)
        key = match["key"]
        if key.lower() in _INTERVIEW_ITEM_KEYS:
            continue  # items of the interview list, which cannot be rebuilt without the nesting
        if key in pairs and pairs[key] != value:
            # The same key twice with different values means more than one object
            # (two leads glued together). Never merge them into one lead.
            return None
        pairs[key] = value
    return pairs if len(pairs) >= 3 else None


def _loads(text: str, strict: bool = True) -> Any:
    """``json.loads`` that turns the non-standard NaN / Infinity tokens into null, so
    they can never become an id, a phone number or an amount."""
    return json.loads(text, parse_constant=lambda _token: None, strict=strict)


def _unescape(quoted: str) -> str:
    """Decode JSON escapes (a newline, a \\uXXXX character, an escaped slash) in a
    salvaged string value."""
    try:
        value = json.loads(f'"{quoted}"')
    except ValueError:
        return quoted
    return value if isinstance(value, str) else quoted


def _has_trailing_data(raw: str) -> bool:
    """True when the body starts with one complete JSON value and then keeps going
    (two leads concatenated, newline-delimited JSON)."""
    try:
        json.JSONDecoder().raw_decode(raw.lstrip())
    except (ValueError, RecursionError):
        return False
    return True


def decode_body(raw: str) -> tuple[dict | None, tuple[str, ...], str | None]:
    """-> (payload, envelope, error). ``envelope`` lists what had to be undone to get
    a JSON object, and is reported as drift."""
    raw = raw.lstrip("\ufeff")  # a byte-order mark is not part of the JSON
    if not raw.strip():
        return None, (), "request body is empty"
    try:
        payload = _loads(raw)
    except RecursionError:
        return None, (), "request body is nested too deeply to be a lead"
    except ValueError as exc:
        if _has_trailing_data(raw):
            # One request is one lead. Like a multi-element array, this is rejected
            # rather than guessed at.
            return None, (), "request body has more content after the first JSON value"
        salvaged = salvage_json(raw)
        if salvaged is None:
            return None, (), f"request body is not valid JSON ({exc})"
        return salvaged, ("malformed-json",), None
    if isinstance(payload, list) and len(payload) == 1 and isinstance(payload[0], dict):
        return payload[0], ("array",), None
    if not isinstance(payload, dict):
        return None, (), f"expected a JSON object, got {json_type(payload)}"
    return payload, (), None


class IngestService:
    def __init__(
        self,
        *,
        db: Database,
        settings: Settings,
        clock: Callable[[], datetime],
        wake_worker: Callable[[], None] = lambda: None,
    ) -> None:
        self.db = db
        self.settings = settings
        self.clock = clock
        self.wake_worker = wake_worker

    # ------------------------------------------------------------------ step 1
    def receive(
        self,
        *,
        raw_body: bytes,
        headers: Mapping[str, str],
        remote_addr: str | None = None,
        url_tenant_id: str | None = None,
    ) -> IngestOutcome:
        """Durably record the request, then process it."""
        now = self.clock()
        event_id = new_id("evt")
        # Keep the headers for debugging, but never write a credential to the database.
        safe_headers = {
            name.lower(): ("[redacted]" if any(h in name.lower() for h in SENSITIVE_HEADER_HINTS) else value)
            for name, value in headers.items()
        }
        with self.db.transaction() as conn:
            conn.execute(
                """INSERT INTO webhook_events
                       (id, source, received_at, remote_addr, content_type, headers_json, raw_body,
                        body_sha256, url_tenant_id, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'received')""",
                (
                    event_id,
                    SOURCE_ANGI,
                    iso(now),
                    remote_addr,
                    safe_headers.get("content-type"),
                    dumps(safe_headers),
                    raw_body.decode("utf-8", errors="replace"),
                    hashlib.sha256(raw_body).hexdigest(),
                    url_tenant_id,
                ),
            )
        return self.process_event(event_id)

    # ------------------------------------------------------------------ steps 2-4
    def process_event(self, event_id: str, assign_tenant_id: str | None = None) -> IngestOutcome:
        """Process (or re-process) a stored event. Safe to call any number of times, and
        it never raises: the request is on disk, so whatever goes wrong here the sender
        still gets its "success" and the event is picked up again later.

        ``assign_tenant_id`` is an operator saying "this lead belongs to that tenant"
        for a quarantined event the payload itself cannot route (no account id)."""
        started = time.perf_counter()
        refused: str | None = None
        try:
            if assign_tenant_id:
                refused = self._assign_tenant(event_id, assign_tenant_id)
            outcome = self._process(event_id, started)
        except Exception as exc:  # noqa: BLE001 - the request is stored; never fail the sender
            log.exception("processing webhook event %s failed", event_id)
            self._record_failure(event_id, exc)
            outcome = success(
                "accepted",
                "Lead stored. Processing hit an error on our side and will be retried automatically.",
                eventId=event_id,
            )
            _log_event({"id": event_id}, "accepted", started)
        if refused:
            outcome.body["assignment"] = f"not applied: {refused}"
        self.wake_worker()
        if outcome.learned_account_id:
            self._release_quarantined(outcome)
        return outcome

    def _assign_tenant(self, event_id: str, tenant_id: str) -> str | None:
        """Record an operator's decision that a quarantined event belongs to
        ``tenant_id``. Returns why it was not recorded, or None when it was. The decision
        is only stored: routing still applies every safety check to it."""
        with self.db.transaction() as conn:
            tenant = get_tenant(conn, tenant_id)
            if tenant is None or not tenant["is_active"]:
                return "that tenant does not exist or is deactivated"
            cursor = conn.execute(
                "UPDATE webhook_events SET operator_tenant_id = ? WHERE id = ? AND status = 'quarantined'",
                (tenant_id, event_id),
            )
            return None if cursor.rowcount else "only a quarantined event can be assigned to a tenant"

    def _record_failure(self, event_id: str, exc: Exception) -> None:
        now = self.clock()
        reason = f"{type(exc).__name__}: {exc}"[:500]
        try:
            with self.db.transaction() as conn:
                row = conn.execute(
                    "SELECT attempts, status, status_reason FROM webhook_events WHERE id = ?", (event_id,)
                ).fetchone()
                conn.execute(
                    "UPDATE webhook_events SET status = 'failed', status_reason = ?, attempts = attempts + 1, "
                    "processed_at = ? WHERE id = ?",
                    (reason, iso(now), event_id),
                )
                if row is not None and row["status"] == "quarantined":
                    # It no longer waits for an operator: the retry loop has it now, and
                    # _finish will not see it leave the quarantine.
                    resolve_quarantine_alert(conn, row["status_reason"], now)
                raise_alert(
                    conn,
                    kind="processing_error",
                    key=f"processing_error:{type(exc).__name__}",
                    severity="critical",
                    title=f"Lead processing failed: {reason[:140]}",
                    detail={"event_id": event_id, "error": reason},
                    now=now,
                    count_occurrence=row is None or _first_pass(dict(row)),
                )
        except Exception:  # noqa: BLE001 - typically the database is still locked
            # Nothing was recorded, so the event keeps the status it had ("received" for
            # a new request) and the retry loop finds it. Failing the sender instead
            # would only make Angi resend a request we already hold.
            log.exception("could not record the failure of webhook event %s", event_id)

    def _release_quarantined(self, outcome: IngestOutcome) -> None:
        """A tenant URL or crmKey just taught us whose Angi account this is. Leads that
        were quarantined waiting for exactly that mapping can go out now. Runs after the
        mapping has committed; a problem here must not undo the lead that taught us."""
        try:
            released = self.replay_quarantined("unmapped_account", account_id=outcome.learned_account_id)
        except Exception:  # noqa: BLE001 - they stay quarantined and can be replayed by hand
            log.exception("could not release the leads quarantined for a newly learned account")
            return
        if released:
            outcome.body["released"] = released

    def _process(self, event_id: str, started: float) -> IngestOutcome:
        now = self.clock()
        with self.db.read() as conn:
            row = conn.execute("SELECT * FROM webhook_events WHERE id = ?", (event_id,)).fetchone()
        if row is None:
            raise LookupError(f"webhook event {event_id} not found")
        event = dict(row)

        payload, envelope, error = decode_body(event["raw_body"])
        if payload is None:
            # Nothing usable at all. This is the one case answered with a failure, so
            # that Angi's own failure handling (retries, then an email to the business)
            # kicks in alongside our alert.
            with self.db.transaction() as conn:
                self._finish(conn, event, now, started, status="rejected", reason=error)
                raise_alert(
                    conn,
                    kind="invalid_payload",
                    key="invalid_payload",
                    severity="warning",
                    title="Angi webhook received a body that is not a JSON object",
                    detail={"event_id": event_id, "error": error, "content_type": event["content_type"]},
                    now=now,
                    count_occurrence=_first_pass(event),
                )
            _log_event(event, "rejected", started)
            return IngestOutcome(400, {"status": "error", "result": "rejected", "eventId": event_id, "error": error})

        parsed = parse_angi_payload(payload, envelope=envelope)
        with self.db.transaction() as conn:
            outcome = self._store(conn, event, parsed, now, started, isinstance(payload, _ReadPairByPair))
        _log_event(event, outcome.body["result"], started)  # after the commit: it reports what was stored
        return outcome

    def _store(
        self,
        conn: sqlite3.Connection,
        event: dict,
        parsed: ParseResult,
        now: datetime,
        started: float,
        read_pair_by_pair: bool = False,
    ) -> IngestOutcome:
        """Step 3: everything that happens to a parsed event, inside one transaction."""
        event_id = event["id"]
        self._record_shape(conn, parsed, event, now)

        if parsed.lead is None:
            raise_alert(
                conn,
                kind="unrecognised_payload",
                key="unrecognised_payload",
                severity="critical",
                title="Angi payload not recognised and no contact details could be recovered",
                detail={"event_id": event_id, "issues": parsed.issues, "keys": parsed.drift.unknown[:40]},
                now=now,
                count_occurrence=_first_pass(event),
            )
            return self._quarantine(
                conn, event, now, started, parsed, "unrecognised_payload", warnings=parsed.issues
            )

        lead = parsed.lead
        key = idempotency_key(lead, event["body_sha256"])
        existing = self._stored_lead(conn, lead, key, event)
        if existing is not None:
            return self._cross_tenant_write(conn, event, existing, lead, parsed, now, started) or self._redelivery(
                conn, event, existing, lead, parsed, now, started
            )

        routing = resolve_tenant(
            conn,
            source=SOURCE_ANGI,
            external_account_id=lead.external_account_id,
            external_account_name=lead.external_account_name,
            operator_tenant_id=event["operator_tenant_id"],
            url_tenant_id=event["url_tenant_id"],
            crm_key=lead.crm_key,
            fallback_slug=self.settings.fallback_tenant_slug or None,
            now=now,
        )
        self._routing_alerts(conn, routing, lead, event, now)
        if routing.tenant is None:
            return self._quarantine(
                conn, event, now, started, parsed, routing.reason, angiAccountId=lead.external_account_id
            )
        tenant = routing.tenant

        # The canary posts the same consumer every few minutes by design. Those are not
        # double charges, and every run has to exercise the real intro email.
        look_for_duplicates = not tenant["is_test"]
        duplicate = None
        if look_for_duplicates:
            duplicate = find_duplicate(
                conn,
                tenant_id=tenant["id"],
                lead=lead,
                received_at=event["received_at"],
                window_days=self.settings.duplicate_window_days,
            )
        lead_row = self._insert_lead(conn, lead, parsed, tenant, routing, event, key, duplicate, now)
        claim_id = None
        if duplicate is not None:
            claim_id = record_duplicate(
                conn,
                tenant_id=tenant["id"],
                lead_id=lead_row["id"],
                fee_cents=lead_row["fee_cents"],
                received_at=event["received_at"],
                match=duplicate,
                request_window_days=self.settings.credit_request_window_days,
                now=now,
            )
        later_duplicates = 0
        if look_for_duplicates:
            later_duplicates = flag_later_duplicates(
                conn,
                lead_row=lead_row,
                window_days=self.settings.duplicate_window_days,
                request_window_days=self.settings.credit_request_window_days,
                now=now,
            )
        email_plan = self._plan_emails(
            conn, lead_row, tenant, now, same_consumer=duplicate is not None or later_duplicates > 0
        )
        self._finish(
            conn, event, now, started, status="processed", parsed=parsed,
            tenant_id=tenant["id"], lead_id=lead_row["id"],
        )
        outcome = success(
            "processed",
            "Lead received.",
            eventId=event_id,
            leadId=lead_row["id"],
            tenant={"id": tenant["id"], "name": tenant["name"], "routedBy": routing.routed_by},
            format=parsed.detected_format,
            parseMode=parsed.mode,
            duplicate=(
                {
                    "ofLeadId": duplicate.original["id"],
                    "matchedOn": duplicate.matched_on,
                    "confidence": duplicate.confidence,
                    "creditClaimId": claim_id,
                }
                if duplicate
                else None
            ),
            email=email_plan,
            warnings=_warnings(parsed, routing, read_pair_by_pair),
        )
        if routing.learned_mapping:
            outcome.learned_account_id = lead.external_account_id
        return outcome

    # ------------------------------------------------------------------ helpers
    def _quarantine(
        self,
        conn: sqlite3.Connection,
        event: dict,
        now: datetime,
        started: float,
        parsed: ParseResult,
        reason: str,
        **fields: Any,
    ) -> IngestOutcome:
        """Leave the event stored but unprocessed, and tell the sender truthfully why."""
        self._finish(conn, event, now, started, status="quarantined", reason=reason, parsed=parsed)
        return success("quarantined", QUARANTINE_MESSAGES[reason], reason=reason, eventId=event["id"], **fields)

    def _stored_lead(self, conn: sqlite3.Connection, lead: NormalizedLead, key: str, event: dict) -> dict | None:
        """The lead this delivery belongs to, if we already have it."""
        if lead.external_lead_id:
            # Same Angi lead id. The account has to agree only when both deliveries name
            # one: a retry that lost (or gained) its account id is still the same lead,
            # while one id under two different accounts is two leads. An exact account
            # match is preferred, then the oldest lead.
            account = lead.external_account_id
            row = conn.execute(
                """SELECT * FROM leads
                   WHERE source = ? AND external_lead_id = ?
                     AND (external_account_id IS NULL OR ? IS NULL OR external_account_id = ?)
                   ORDER BY external_account_id IS ? DESC, created_at
                   LIMIT 1""",
                (lead.source, lead.external_lead_id, account, account, account),
            ).fetchone()
        else:
            row = conn.execute("SELECT * FROM leads WHERE idempotency_key = ?", (key,)).fetchone()
        if row is None and event["lead_id"]:
            # This very event became (or updated) a lead on an earlier pass, under ids
            # the parser read differently then. A replay never creates a second lead.
            row = conn.execute("SELECT * FROM leads WHERE id = ?", (event["lead_id"],)).fetchone()
        return dict(row) if row else None

    def _cross_tenant_write(
        self,
        conn: sqlite3.Connection,
        event: dict,
        existing: dict,
        lead: NormalizedLead,
        parsed: ParseResult,
        now: datetime,
        started: float,
    ) -> IngestOutcome | None:
        """A request that names its tenant (a tenant URL, a crmKey, an operator's
        assignment) may only ever touch that tenant's own leads. If the lead id it
        carries already belongs to another tenant, nothing is changed: otherwise whoever
        holds one tenant's URL could rewrite another tenant's lead and have its emails
        sent to an address of their choosing. Returns the quarantine answer, or None
        when there is no such conflict."""
        claimed, how = explicit_tenant_id(
            conn,
            operator_tenant_id=event["operator_tenant_id"],
            url_tenant_id=event["url_tenant_id"],
            crm_key=lead.crm_key,
        )
        owner = existing["tenant_id"]
        if claimed is None or claimed == owner:
            return None
        names = {tid: (get_tenant(conn, tid) or {"name": tid})["name"] for tid in (claimed, owner)}
        raise_alert(
            conn,
            kind="tenant_conflict",
            key=f"tenant_conflict:{claimed}",
            severity="critical",
            title=f"A request addressed to {names[claimed]} carries a lead id that belongs to {names[owner]}; "
            f"it was quarantined and the lead was not changed",
            detail={
                "event_id": event["id"],
                "lead_id": existing["id"],
                "routed_tenant_id": claimed,
                "routed_by": how,
                "lead_tenant_id": owner,
                "external_account_id": lead.external_account_id,
            },
            now=now,
            count_occurrence=_first_pass(event),
        )
        return self._quarantine(
            conn, event, now, started, parsed, "tenant_conflict", angiAccountId=lead.external_account_id
        )

    def _redelivery(
        self,
        conn: sqlite3.Connection,
        event: dict,
        existing: dict,
        lead: NormalizedLead,
        parsed: ParseResult,
        now: datetime,
        started: float,
    ) -> IngestOutcome:
        """Angi sent a lead id we already have. Never a second lead, never a second intro."""
        ids = {"eventId": event["id"], "leadId": existing["id"]}
        applied = event["status"] == "processed" and event["lead_id"] == existing["id"]
        if existing["first_event_id"] == event["id"] or applied:
            # Re-processing the very event that created the lead, or one that already
            # updated it (manual replay / retry): it stays on record as what it was.
            self._finish(
                conn, event, now, started, status="processed",
                reason=event["status_reason"] if applied else None, parsed=parsed,
                tenant_id=existing["tenant_id"], lead_id=existing["id"],
            )
            return success("processed", "Already processed.", **ids)

        fresh = lead_columns(lead)
        changed = _changed_columns(fresh, existing)
        # Deliveries can be processed out of order (a quarantined event replayed after a
        # later correction was applied). The most recently *received* version wins.
        stale = event["received_at"] < (existing["last_delivery_at"] or existing["received_at"])
        if not changed or stale:
            add_lead_event(
                conn, existing["id"], existing["tenant_id"], EV_REDELIVERED, now,
                {"event_id": event["id"], "stale": stale},
            )
            self._finish(
                conn, event, now, started, status="duplicate_delivery",
                reason="older than the version already stored" if stale else "lead already received",
                parsed=parsed, tenant_id=existing["tenant_id"], lead_id=existing["id"],
            )
            return success(
                "duplicate_delivery",
                "This lead was already received; nothing was created or sent again.",
                **ids,
            )

        # Same lead id, different content: Angi corrected the lead. Update in place.
        updates = {col: fresh[col] for col in changed}
        if existing["email_normalized"] is None and fresh["email_normalized"]:
            # The lead was (at least partly) flagged for review because it could not be
            # emailed. Now it can: judge it again from this delivery.
            updates["needs_review"] = int(_needs_review(lead, parsed))
        assignments = ", ".join(f"{col} = ?" for col in updates)
        conn.execute(
            f"UPDATE leads SET {assignments}, last_delivery_at = ?, updated_at = ? WHERE id = ?",
            [*updates.values(), event["received_at"], iso(now), existing["id"]],
        )
        add_lead_event(
            conn, existing["id"], existing["tenant_id"], EV_UPDATED, now,
            {"event_id": event["id"], "changed": changed},
        )
        email_plan = self._email_after_correction(conn, existing, fresh, changed, now, event["id"])
        self._finish(
            conn, event, now, started, status="processed", reason="updated_existing_lead",
            parsed=parsed, tenant_id=existing["tenant_id"], lead_id=existing["id"],
        )
        return success(
            "updated",
            "Lead id already known; the stored lead was updated instead of creating a new one.",
            **ids,
            changed=changed,
            email=email_plan,
        )

    def _insert_lead(
        self,
        conn: sqlite3.Connection,
        lead: NormalizedLead,
        parsed: ParseResult,
        tenant: dict,
        routing: Routing,
        event: dict,
        key: str,
        duplicate: DuplicateMatch | None,
        now: datetime,
    ) -> dict:
        lead_id = new_id("lead")
        stamp = iso(now)
        columns = lead_columns(lead)
        fee_source = "payload" if lead.fee_cents is not None else None
        if lead.fee_cents is None and routing.mapping and routing.mapping.get("default_fee_cents") is not None:
            # The Angi Ads feed carries no price; use what this account pays per lead.
            columns["fee_cents"] = routing.mapping["default_fee_cents"]
            fee_source = "account_default"
        row: dict[str, Any] = {
            "id": lead_id,
            "tenant_id": tenant["id"],
            "source": lead.source,
            "source_format": lead.source_format,
            "idempotency_key": key,
            "external_lead_id": lead.external_lead_id,
            "external_request_id": lead.external_request_id,
            "external_account_id": lead.external_account_id,
            "external_account_name": lead.external_account_name,
            "routed_by": routing.routed_by,
            **columns,
            "fee_source": fee_source,
            "status": "new",
            "parse_mode": parsed.mode,
            "needs_review": int(_needs_review(lead, parsed)),
            "is_duplicate": 1 if duplicate else 0,
            "duplicate_of_lead_id": duplicate.original["id"] if duplicate else None,
            "duplicate_matched_on": dumps(duplicate.matched_on) if duplicate else None,
            "booking_token": new_token(),
            # The speed-to-lead clock starts when the request arrived, not when we got
            # round to processing it (which matters for replayed events).
            "received_at": event["received_at"],
            "last_delivery_at": event["received_at"],
            "first_event_id": event["id"],
            "created_at": stamp,
            "updated_at": stamp,
        }
        names = ", ".join(row)
        marks = ", ".join("?" * len(row))
        conn.execute(f"INSERT INTO leads ({names}) VALUES ({marks})", list(row.values()))
        add_lead_event(
            conn,
            lead_id,
            tenant["id"],
            EV_RECEIVED,
            now,
            {
                "event_id": event["id"],
                "format": parsed.detected_format,
                "parse_mode": parsed.mode,
                "routed_by": routing.routed_by,
                "issues": parsed.issues,
                # Released by a mapping, an assignment or a replay: say when it arrived,
                # or this entry's time (now) contradicts "Received" and the response time.
                "waited_in_quarantine_since": event["received_at"] if event["status"] == "quarantined" else None,
            },
        )
        return row

    def _email_after_correction(
        self,
        conn: sqlite3.Connection,
        existing: dict,
        fresh: dict[str, Any],
        changed: list[str],
        now: datetime,
        event_id: str,
    ) -> dict[str, Any]:
        """A corrected delivery changed the lead. Decide what that means for email:
        nothing, unless the address itself changed."""
        new_email = fresh["email_normalized"]
        if "email_normalized" not in changed or not new_email:
            return {"status": "unchanged"}
        if conn.execute(
            "SELECT 1 FROM outbox_messages WHERE lead_id = ? AND to_address = ? "
            "AND template IN ('intro', 'repeat_request') AND status IN ('sending', 'sent') LIMIT 1",
            (existing["id"], new_email),
        ).fetchone() is not None:
            # This address has had its first email (a late delivery flipping the address
            # back). One still queued for the address being replaced is dropped, not
            # re-addressed: re-addressed, it would be a second introduction here.
            outbox.cancel_pending(
                conn, existing["id"], now, reason="address_corrected", templates=("intro", "repeat_request")
            )
        # Whatever has not gone out yet now goes to the corrected address.
        conn.execute(
            "UPDATE outbox_messages SET to_address = ? WHERE lead_id = ? AND status = 'pending'",
            (new_email, existing["id"]),
        )
        tenant = get_tenant(conn, existing["tenant_id"])
        if tenant is None:
            return {"status": "unchanged"}
        if is_suppressed(conn, tenant["id"], new_email):
            if not (existing["first_contacted_at"] or existing["booked_at"] or existing["outcome"]):
                # As for a new lead whose address has opted out (_plan_emails): nobody will
                # hear from us by email, so a person has to pick this lead up.
                conn.execute("UPDATE leads SET needs_review = 1 WHERE id = ?", (existing["id"],))
            return {"status": "unchanged"}
        # No new introduction for a lead that is past that stage: it has booked, it was
        # closed, or the person asked through this lead's own link not to be emailed.
        opted_out = conn.execute(
            "SELECT 1 FROM lead_events WHERE lead_id = ? AND type = 'lead.unsubscribed' LIMIT 1", (existing["id"],)
        ).fetchone()
        if existing["booked_at"] or existing["outcome"]:
            return {"status": "unchanged"}
        if opted_out:
            # The opt-out was recorded for the address that has just been replaced (it may
            # be a mistyped one: somebody else's mailbox). Nobody at the corrected address
            # will hear from us by email either, so a person has to pick this lead up.
            add_lead_event(conn, existing["id"], tenant["id"], EV_EMAIL_SKIPPED, now, {"reason": "unsubscribed"})
            conn.execute("UPDATE leads SET needs_review = 1 WHERE id = ?", (existing["id"],))
            return {"status": "unchanged"}
        same_consumer = bool(existing["is_duplicate"])
        if existing["email_normalized"] is None:
            # The first usable address this lead has had: start the normal cadence.
            corrected = {**existing, **{col: fresh[col] for col in changed}}
            return self._plan_emails(conn, corrected, tenant, now, same_consumer=same_consumer)
        # One first touch per address. It may already exist: sent earlier (a stale
        # delivery flipping the address back), or still queued and just retargeted above.
        first_touch = conn.execute(
            "SELECT status FROM outbox_messages WHERE lead_id = ? AND to_address = ? "
            "AND template IN ('intro', 'repeat_request') AND status IN ('pending', 'sending', 'sent') LIMIT 1",
            (existing["id"], new_email),
        ).fetchone()
        if first_touch is not None:
            status = "already_sent" if first_touch["status"] == "sent" else "queued"
            return {"status": status, "to": new_email}
        acknowledge = same_consumer and self._already_introduced(conn, tenant["id"], new_email, existing)
        # Keyed by the delivery that made the correction: a key made from the address
        # could already be taken by a row that was re-addressed or cancelled since.
        outbox.enqueue(
            conn,
            lead_id=existing["id"],
            tenant_id=tenant["id"],
            template="repeat_request" if acknowledge else "intro",
            to_address=new_email,
            dedupe_key=f"{existing['id']}:first_touch:{event_id}",
            send_after=now,
            now=now,
            max_attempts=self.settings.outbox_max_attempts,
        )
        follow_ups = 0
        if not acknowledge and not existing["first_contacted_at"] and conn.execute(
            "SELECT 1 FROM outbox_messages WHERE lead_id = ? AND template = 'followup' "
            "AND status IN ('pending', 'sending', 'sent') LIMIT 1",
            (existing["id"],),
        ).fetchone() is None:
            # The lead was never reached (the first address bounced or had opted out) and
            # its follow-ups went with that: the corrected address gets the normal cadence.
            follow_ups = self._queue_follow_ups(conn, existing["id"], tenant, new_email, now, key_suffix=f":{event_id}")
        plan: dict[str, Any] = {"status": "queued", "to": new_email}
        if follow_ups:
            plan["followUpsScheduled"] = follow_ups
        return plan

    def _already_introduced(self, conn: sqlite3.Connection, tenant_id: str, address: str, lead_row: dict) -> bool:
        """Has this tenant's first email for another lead already gone to this address
        (or is it on its way) inside the duplicate window?"""
        window_start = parse_iso(lead_row["received_at"]) - timedelta(days=self.settings.duplicate_window_days)
        row = conn.execute(
            "SELECT 1 FROM outbox_messages WHERE tenant_id = ? AND to_address = ? AND lead_id != ? "
            "AND template IN ('intro', 'repeat_request') AND status IN ('pending', 'sending', 'sent') "
            "AND COALESCE(sent_at, created_at) >= ? LIMIT 1",
            (tenant_id, address, lead_row["id"], iso(window_start)),
        ).fetchone()
        return row is not None

    def _plan_emails(
        self,
        conn: sqlite3.Connection,
        lead_row: dict,
        tenant: dict,
        now: datetime,
        *,
        same_consumer: bool,
    ) -> dict[str, Any]:
        """Queue the first-touch email (and the follow-up cadence) in the same
        transaction as the lead, so "lead stored" always implies "email queued".
        ``same_consumer`` says the lead is one of a duplicate pair."""
        address = lead_row["email_normalized"]
        if not address:
            add_lead_event(conn, lead_row["id"], tenant["id"], EV_EMAIL_SKIPPED, now, {"reason": "no_valid_email"})
            return {"status": "skipped", "reason": "no_valid_email"}
        if is_suppressed(conn, tenant["id"], address):
            add_lead_event(conn, lead_row["id"], tenant["id"], EV_EMAIL_SKIPPED, now, {"reason": "unsubscribed"})
            # Nobody will hear from us by email: a person has to pick this lead up.
            conn.execute("UPDATE leads SET needs_review = 1 WHERE id = ?", (lead_row["id"],))
            return {"status": "skipped", "reason": "unsubscribed"}

        # A repeat request gets a short acknowledgement instead of the same introduction
        # and a second follow-up cadence - but "we got your new request" only makes sense
        # to someone who has our first email. A duplicate from a consumer we never
        # reached at this address (the earlier lead had another address, or none we
        # could use) gets the normal introduction and follow-ups. It is still a
        # duplicate for the credit claim.
        acknowledge = same_consumer and self._already_introduced(conn, tenant["id"], address, lead_row)
        template = "repeat_request" if acknowledge else "intro"
        outbox.enqueue(
            conn,
            lead_id=lead_row["id"],
            tenant_id=tenant["id"],
            template=template,
            to_address=address,
            dedupe_key=f"{lead_row['id']}:first_touch",
            send_after=now,
            now=now,
            max_attempts=self.settings.outbox_max_attempts,
        )
        follow_ups = 0 if acknowledge else self._queue_follow_ups(conn, lead_row["id"], tenant, address, now)
        return {"status": "queued", "template": template, "to": address, "followUpsScheduled": follow_ups}

    def _queue_follow_ups(
        self, conn: sqlite3.Connection, lead_id: str, tenant: dict, address: str, now: datetime, key_suffix: str = ""
    ) -> int:
        """Queue the follow-up cadence for a lead; returns how many were queued."""
        if tenant["is_test"]:
            # A test tenant's lead is the monitoring probe. It proves the first response
            # and gets no nudges: the canary posts the same address every few minutes, so
            # they could never go out (always "emailed minutes ago") and would pile up.
            return 0
        queued = 0
        for index, minutes in enumerate(self.settings.followup_delays, start=1):
            outbox.enqueue(
                conn,
                lead_id=lead_id,
                tenant_id=tenant["id"],
                template="followup",
                sequence=index,
                to_address=address,
                dedupe_key=f"{lead_id}:followup:{index}{key_suffix}",
                send_after=defer_past_quiet_hours(tenant, now + timedelta(minutes=minutes)),
                now=now,
                max_attempts=self.settings.outbox_max_attempts,
            )
            queued += 1
        return queued

    def _routing_alerts(
        self, conn: sqlite3.Connection, routing: Routing, lead: NormalizedLead, event: dict, now: datetime
    ) -> None:
        account = lead.external_account_id or "(none)"
        business = lead.external_account_name or "unknown business"
        first_pass = _first_pass(event)
        detail = {
            "event_id": event["id"],
            "external_account_id": lead.external_account_id,
            "external_account_name": lead.external_account_name,
        }
        if routing.reason == "mapping_conflict":
            conflict = routing.conflict or {}
            names = {
                side: (get_tenant(conn, conflict[side]) or {"name": conflict[side]})["name"]
                for side in ("routed_tenant_id", "mapped_tenant_id")
            }
            raise_alert(
                conn,
                kind="mapping_conflict",
                key=f"mapping_conflict:{account}",
                severity="critical",
                title=f"Lead quarantined: Angi account {account} is mapped to {names['mapped_tenant_id']} but a "
                f"lead for it arrived addressed to {names['routed_tenant_id']}",
                detail={**detail, **conflict, "reason": routing.reason},
                now=now,
                count_occurrence=first_pass,
            )
        elif routing.reason == "no_account_id":
            raise_alert(
                conn,
                kind="no_account_id",
                key="no_account_id",
                severity="critical",
                title=f"Lead quarantined: the payload names no Angi account ({business}); "
                f"an operator has to assign it to a tenant",
                detail={**detail, "reason": routing.reason},
                now=now,
                count_occurrence=first_pass,
            )
        elif routing.tenant is None:
            problem = (
                "belongs to a deactivated tenant"
                if routing.reason == "tenant_inactive"
                else "is not mapped to a tenant"
            )
            raise_alert(
                conn,
                kind="unmapped_account",
                key=f"unmapped_account:{account}",
                severity="critical",
                title=f"Lead quarantined: Angi account {account} ({business}) {problem}",
                detail={**detail, "reason": routing.reason},
                now=now,
                count_occurrence=first_pass,  # the counter is leads affected, not replays
            )
        elif routing.routed_by == "fallback":
            raise_alert(
                conn,
                kind="unmapped_account",
                key=f"unmapped_account:{account}",
                severity="warning",
                title=f"Angi account {account} has no tenant mapping; lead went to the fallback tenant",
                detail=detail,
                now=now,
                count_occurrence=first_pass,
            )
        elif lead.external_account_id:
            # The account routes properly now: earlier trouble with it is over.
            resolve_alert(conn, f"unmapped_account:{account}", now)
            # A conflict is over only when no lead of this account is still waiting on
            # it. (The event in hand may be one of them, being released right now.)
            if conn.execute(
                "SELECT 1 FROM webhook_events WHERE status = 'quarantined' AND status_reason = 'mapping_conflict' "
                "AND external_account_id = ? AND id != ? LIMIT 1",
                (lead.external_account_id, event["id"]),
            ).fetchone() is None:
                resolve_alert(conn, f"mapping_conflict:{account}", now)
        if routing.learned_mapping and routing.tenant is not None:
            # Convenient, but worth a human glance: one mis-addressed lead could
            # otherwise claim an Angi account for the wrong tenant unnoticed.
            raise_alert(
                conn,
                kind="mapping_learned",
                key=f"mapping_learned:{account}",
                severity="info",
                title=f"Learned that Angi account {account} belongs to {routing.tenant['name']} "
                f"(a lead for it arrived addressed to that tenant)",
                detail={**detail, "tenant_id": routing.tenant["id"], "routed_by": routing.routed_by},
                now=now,
            )

    def _record_shape(self, conn: sqlite3.Connection, parsed: ParseResult, event: dict, now: datetime) -> None:
        """Remember each distinct payload shape, and alert the first time a new
        deviation from the documented format shows up."""
        stamp = iso(now)
        first_pass = _first_pass(event)
        conn.execute(
            """INSERT INTO payload_shapes
                   (signature, source, detected_format, parse_mode, is_clean, detail_json,
                    first_seen_at, last_seen_at, seen_count, sample_event_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
               ON CONFLICT(signature) DO UPDATE SET
                   seen_count = seen_count + ?, last_seen_at = excluded.last_seen_at""",
            (
                parsed.signature,
                SOURCE_ANGI,
                parsed.detected_format,
                parsed.mode,
                1 if parsed.is_clean else 0,
                dumps(parsed.drift.as_dict()),
                stamp,
                stamp,
                event["id"],
                1 if first_pass else 0,  # a replay is not another lead of this shape
            ),
        )
        if parsed.is_clean or parsed.lead is None:
            return  # nothing to report, or reported as "unrecognised_payload" by the caller
        drift = parsed.drift
        if parsed.detected_format == UNKNOWN_FORMAT:
            severity = "critical"
            title = "Angi payload is in an unknown format (lead recovered by alias matching)"
        else:
            severity = drift.severity or "info"
            parts = []
            if drift.missing:
                parts.append(f"missing: {', '.join(drift.missing[:6])}")
            if drift.mistyped:
                parts.append(f"type changed: {', '.join(m.split(':')[0] for m in drift.mistyped[:6])}")
            if drift.recovered:
                parts.append(f"recovered by alias: {', '.join(drift.recovered[:6])}")
            if drift.unknown:
                parts.append(f"new fields: {', '.join(drift.unknown[:6])}")
            title = f"Angi payload format drift ({parsed.detected_format}) - {'; '.join(parts)}"
        raise_alert(
            conn,
            kind="schema_drift",
            key=f"schema_drift:{parsed.signature}",
            severity=severity,
            title=title[:300],
            detail={"event_id": event["id"], "format": parsed.detected_format, **drift.as_dict()},
            now=now,
            count_occurrence=first_pass,
        )

    def _finish(
        self,
        conn: sqlite3.Connection,
        event: dict,
        now: datetime,
        started: float,
        *,
        status: str,
        reason: str | None = None,
        parsed: ParseResult | None = None,
        tenant_id: str | None = None,
        lead_id: str | None = None,
    ) -> None:
        """Write the outcome of this pass onto the event row (and onto our copy of it,
        which the log line reports from)."""
        event.update(
            tenant_id=tenant_id,
            lead_id=lead_id,
            detected_format=parsed.detected_format if parsed else None,
            parse_mode=parsed.mode if parsed else None,
        )
        conn.execute(
            """UPDATE webhook_events SET
                   status = ?, status_reason = ?, detected_format = ?, parse_mode = ?, issues_json = ?,
                   external_account_id = ?,
                   tenant_id = ?, lead_id = ?, attempts = attempts + 1, processing_ms = ?, processed_at = ?
               WHERE id = ?""",
            (
                status,
                reason,
                event["detected_format"],
                event["parse_mode"],
                dumps({"issues": parsed.issues, "drift": parsed.drift.as_dict()}) if parsed else None,
                parsed.lead.external_account_id if parsed and parsed.lead else None,
                tenant_id,
                lead_id,
                round((time.perf_counter() - started) * 1000, 2),
                iso(now),
                event["id"],
            ),
        )
        if event["status"] == "quarantined" and (status, reason) != ("quarantined", event["status_reason"]):
            # It was waiting in quarantine and is not any more (or waits for something else now).
            resolve_quarantine_alert(conn, event["status_reason"], now)

    # ------------------------------------------------------------------ replay
    def replay_quarantined(
        self, reason: str | None = None, account_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Re-run events that are waiting on a fix (a new mapping, a parser update).
        ``account_id`` narrows it to one Angi account, so mapping that account does not
        churn through every other quarantined lead."""
        sql = "SELECT id FROM webhook_events WHERE status = 'quarantined'"
        params: list[Any] = []
        if reason:
            sql += " AND status_reason = ?"
            params.append(reason)
        if account_id:
            sql += " AND external_account_id = ?"
            params.append(account_id)
        with self.db.read() as conn:
            ids = [r["id"] for r in conn.execute(sql + " ORDER BY received_at", params)]
        results = []
        for event_id in ids:
            outcome = self.process_event(event_id)
            results.append({"eventId": event_id, "result": outcome.body.get("result")})
        return results

    def retry_unfinished(self, older_than_seconds: float = 30.0, max_attempts: int = 5) -> int:
        """Pick up events that failed, or were interrupted by a crash between "stored"
        and "processed". Called periodically by the background worker."""
        cutoff = iso(self.clock() - timedelta(seconds=older_than_seconds))
        with self.db.read() as conn:
            ids = [
                r["id"]
                for r in conn.execute(
                    "SELECT id FROM webhook_events WHERE status IN ('received', 'failed') "
                    "AND received_at < ? AND attempts < ? ORDER BY received_at LIMIT 50",
                    (cutoff, max_attempts),
                )
            ]
        for event_id in ids:
            self.process_event(event_id)
        return len(ids)


def _first_pass(event: dict) -> bool:
    """Alert counters and payload-shape counts say how many leads were affected, so only
    an event's first pass adds to them: a replay or a retry is the same lead again."""
    return event["attempts"] == 0


# Contact details are stored twice: as sent, and normalised. They move together.
_CONTACT_PAIRS = (
    ("email", "email_normalized"),
    ("phone", "phone_normalized"),
    ("secondary_phone", "secondary_phone_normalized"),
)


def _changed_columns(fresh: dict[str, Any], existing: dict[str, Any]) -> list[str]:
    """Which stored columns a corrected delivery should overwrite.

    Only values the new delivery actually carries are taken over: a sparser redelivery
    must never make the stored lead poorer.
    """
    changed = {
        col for col, value in fresh.items()
        if value is not None and value != existing[col] and col != "extras_json"
    }
    # Never replace a usable address or number with one we cannot use.
    for raw_col, normalized_col in _CONTACT_PAIRS:
        if fresh[normalized_col] is None and existing[normalized_col] is not None:
            changed.discard(raw_col)
    # Derived values: do not degrade them because one of their inputs went missing.
    if (fresh["first_name"] is None and existing["first_name"]) or (
        fresh["last_name"] is None and existing["last_name"]
    ):
        changed.discard("full_name")
    if fresh["market"] is None and existing["market"]:
        changed.discard("task_name")
    return sorted(changed)


def _warnings(parsed: ParseResult, routing: Routing, read_pair_by_pair: bool = False) -> list[str]:
    warnings = list(parsed.issues)
    drift = parsed.drift
    if "<malformed-json>" in drift.unknown and read_pair_by_pair:
        warnings.append(
            "the body was not valid JSON (<malformed-json>): its key/value pairs were salvaged one by one, so "
            "nesting is lost. Nested fields are listed below as missing or recovered by alias, and interview "
            "answers are not kept"
        )
    elif "<malformed-json>" in drift.unknown:
        warnings.append(
            "the body was not valid JSON (<malformed-json>): it was repaired as a whole (typographic quotes, "
            "a comma before a closing bracket or a raw line break in a text), so its nesting is intact"
        )
    if drift.missing:
        warnings.append(f"payload is missing documented fields: {', '.join(drift.missing)}")
    if drift.mistyped:
        warnings.append(f"unexpected field types: {'; '.join(drift.mistyped)}")
    if drift.recovered:
        warnings.append(f"fields recovered by alias: {', '.join(drift.recovered)}")
    if drift.unknown:
        warnings.append(f"fields not in the documented format: {', '.join(drift.unknown)}")
    warnings.extend(routing.notes)
    return warnings
