"""Angi payload -> NormalizedLead, plus a report of how the payload deviates from the
documented format.

Parsing is deliberately forgiving, because the sender can change the format without
telling us and every minute a lead waits costs conversion:

1. **Detect** which known variant the payload is (by its distinctive keys).
2. **Strict mapping** using that variant's field table (``formats.py``).
3. **Tolerant recovery**: anything important still missing is searched for by alias
   among the keys the table did not recognise (renamed, re-cased or nested keys). The
   consumer's details are never taken from a part of the payload that describes the
   business (``company.email``), nor the account's from the consumer's part.
4. Only a payload with no recognisable format *and* no way to contact the consumer is
   given up on; it stays in ``webhook_events`` for a human, and can be replayed.

Every deviation is recorded in ``Drift`` so monitoring can alert on it.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, DecimalException
from typing import Any

from app.angi.formats import (
    ACCOUNT_TARGETS,
    ALIASES,
    ANGI_ADS,
    BUSINESS_WORDS,
    CONSUMER_TARGETS,
    CONSUMER_WORDS,
    FORMATS,
    FormatSpec,
    norm_key,
)
from app.models import InterviewAnswer, NormalizedLead
from app.util import clean_str, dumps, normalize_email, normalize_phone

MAX_DEPTH = 4
UNKNOWN_FORMAT = "unknown"
MAX_DOLLARS = 10**9  # anything larger is not a price, and would overflow the integer-cents column


@dataclass
class Drift:
    """How a payload differs from the documented shape of its format."""

    unknown: list[str] = field(default_factory=list)  # keys the format does not document
    missing: list[str] = field(default_factory=list)  # documented always-present keys that are absent
    mistyped: list[str] = field(default_factory=list)  # "key: expected string, got object"
    recovered: list[str] = field(default_factory=list)  # "email <- EmailAddress" (found by alias)

    @property
    def is_clean(self) -> bool:
        return not (self.unknown or self.missing or self.mistyped or self.recovered)

    @property
    def severity(self) -> str | None:
        if self.missing or self.mistyped or self.recovered:
            return "warning"  # the documented mapping no longer fits
        if self.unknown:
            return "info"  # new fields appeared; nothing broke
        return None

    def as_dict(self) -> dict[str, list[str]]:
        return {
            "unknown": self.unknown,
            "missing": self.missing,
            "mistyped": self.mistyped,
            "recovered": self.recovered,
        }


@dataclass
class ParseResult:
    detected_format: str  # angi_ads | angi_leads | unknown
    mode: str  # strict | tolerant | failed
    lead: NormalizedLead | None
    drift: Drift
    issues: list[str]  # data-quality problems with this particular lead
    signature: str  # stable id for this kind of deviation ("angi_ads:clean" when none)

    @property
    def is_clean(self) -> bool:
        return self.detected_format != UNKNOWN_FORMAT and self.drift.is_clean


# --- small value helpers -----------------------------------------------------------------


def json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def _is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, dict)):
        return not value
    return False


def _scalar_str(value: Any, max_len: int = 200) -> str | None:
    """Ids, phones and postal codes arrive as strings or numbers; make them strings."""
    if value is None or isinstance(value, (bool, dict, list)):
        return None
    if isinstance(value, float):
        if not math.isfinite(value):  # NaN / Infinity are never an id, phone or ZIP
            return None
        if value.is_integer():
            value = int(value)
    return clean_str(value, max_len)


def to_cents(value: Any) -> int | None:
    if value is None or isinstance(value, (bool, dict, list)):
        return None
    try:
        amount = Decimal(str(value).replace("$", "").replace(",", "").strip())
        if not amount.is_finite() or amount < 0 or amount > MAX_DOLLARS:
            return None
        return int((amount * 100).to_integral_value(rounding=ROUND_HALF_UP))
    except (DecimalException, ValueError):  # not a number, or one too extreme to do arithmetic on
        return None


def to_bool(value: Any) -> bool | None:
    """Angi sends this kind of flag as true, "true" and "false"; never trust truthiness
    (the string "false" is truthy)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("true", "yes", "y", "1"):
            return True
        if text in ("false", "no", "n", "0"):
            return False
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    return None


def _postal_code(value: Any) -> str | None:
    text = _scalar_str(value, 20)
    # A US ZIP sent as a JSON number loses its leading zeros (02134 -> 2134).
    if text and text.isdigit() and len(text) < 5 and isinstance(value, (int, float)):
        text = text.zfill(5) if value else None  # the number 0 is "no ZIP", not 00000
    return text


def _is_question_and_answer(item: Any) -> bool:
    return isinstance(item, dict) and {"question", "answer"} <= {norm_key(str(k)) for k in item}


def _interview(value: Any) -> list[InterviewAnswer]:
    if not isinstance(value, list):
        return []
    answers: list[InterviewAnswer] = []
    for item in value[:50]:
        if not isinstance(item, dict):
            continue
        by_key = {norm_key(str(k)): v for k, v in item.items()}
        question = clean_str(by_key.get("question"), 500)
        answer = clean_str(by_key.get("answer"), 1000)
        if question and answer:
            answers.append(InterviewAnswer(question=question, answer=answer))
    return answers


# "Indianapolis – House Cleaning": the Angi Ads spec writes Category as market, dash, service.
_CATEGORY_DASH = re.compile(r"^(?P<market>.+?)\s+[\u2013\u2014]\s+(?P<task>.+)$")
_CATEGORY_HYPHEN = re.compile(r"^(?P<market>.+?)\s+-\s+(?P<task>.+)$")


def split_category(category: str | None, city: str | None) -> tuple[str | None, str | None]:
    """-> (market, service). A plain hyphen is only treated as the separator when the
    left side is the lead's own city, so "Heating - Repair" is left alone."""
    if not category:
        return None, None
    match = _CATEGORY_DASH.match(category)
    if match is None:
        match = _CATEGORY_HYPHEN.match(category)
        if match is None or not city or match["market"].strip().casefold() != city.strip().casefold():
            return None, category
    return match["market"].strip(), match["task"].strip()


# The spec's own example crams the whole address into the first line:
# "123 Main St. Indianapolis, IN 46203".
_ADDRESS_TAIL = re.compile(r"[,\s]+(?P<state>[A-Z]{2})\s+(?P<zip>\d{5})(?:-\d{4})?\s*$")


def split_crammed_address(line1: str | None) -> tuple[str | None, str | None]:
    """-> (state, postal_code) read from the end of a one-line address, if present."""
    if not line1:
        return None, None
    match = _ADDRESS_TAIL.search(line1)
    return (match["state"], match["zip"]) if match else (None, None)


# --- structure helpers -------------------------------------------------------------------


def flatten(obj: dict, prefix: str = "", depth: int = 0, leaves: frozenset[str] = frozenset()) -> dict[str, Any]:
    """``{"PostalAddress": {"City": "X"}, "interview": [...]}`` ->
    ``{"PostalAddress.City": "X", "interview": [...]}``. Lists are left whole, and so is
    anything at a path in ``leaves`` (a documented single-value field that arrived as an
    object must be reported as mistyped, not dissolved into its parts)."""
    flat: dict[str, Any] = {}
    for key, value in obj.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict) and value and depth < MAX_DEPTH and path not in leaves:
            flat.update(flatten(value, f"{path}.", depth + 1, leaves))
        else:
            flat[path] = value
    return flat


def detect_format(payload: dict) -> FormatSpec | None:
    """Pick the known variant sharing the most signature keys with the payload.
    Keys are compared case- and separator-insensitively so a casing change alone does
    not make the format unrecognisable. Two hits are required, at least one of them a
    key only that feed uses: ``{"urgency": .., "category": ..}`` from some other system
    is not an Angi Ads lead, and treating it as one would create an empty lead."""
    keys = {norm_key(str(k)) for k in payload}
    best: FormatSpec | None = None
    best_hits = 0
    for spec in FORMATS:
        hits = len(keys & spec.signature_keys)
        if hits > best_hits and keys & spec.distinctive_keys:
            best, best_hits = spec, hits
    return best if best_hits >= 2 else None


_WORDS = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+")  # "spContact" / "SP_contact" -> sp, contact


def _party(path: str) -> str | None:
    """Whose data a nested value is, going by the names of the objects it sits in:
    "business", "consumer", or None when they do not say. A business word anywhere
    wins, so ``spContact.email`` is the business's contact, not the consumer."""
    words = {word.lower() for segment in path.split(".")[:-1] for word in _WORDS.findall(segment)}
    if words & BUSINESS_WORDS:
        return "business"
    if words & CONSUMER_WORDS:
        return "consumer"
    return None


def _unwrap(payload: dict) -> tuple[dict, list[str]]:
    """``{"lead": {...}}`` -> the inner object, remembering the wrapper as drift."""
    wrappers: list[str] = []
    while len(payload) == 1 and len(wrappers) < MAX_DEPTH:
        ((key, value),) = payload.items()
        if isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict) and value[0]:
            wrappers.append(f"{key}[]")  # {"leads": [{...}]}
            payload = value[0]
            continue
        if not isinstance(value, dict) or not value:
            break
        wrappers.append(str(key))
        payload = value
    return payload, wrappers


# --- the parser --------------------------------------------------------------------------


def parse_angi_payload(payload: dict, *, envelope: tuple[str, ...] = ()) -> ParseResult:
    """``envelope`` names anything the caller already had to undo to get a JSON object
    (e.g. "array" for a body of ``[{...}]``, "malformed-json" for a salvaged body), so
    it is reported as drift like any other deviation."""
    drift = Drift()
    drift.unknown.extend(f"<{name}>" for name in envelope)
    payload, wrappers = _unwrap(payload)
    drift.unknown.extend(f"<wrapper:{name}>" for name in wrappers)

    spec = detect_format(payload)
    known = spec.field_map() if spec else {}
    flat = flatten(payload, leaves=frozenset(known))
    values: dict[str, Any] = {}
    extras: dict[str, Any] = {}
    unrecognised: set[str] = set(flat)  # keys the format does not document
    leftovers: set[str] = set(flat)  # values nothing has used yet (searched by alias, then kept in extras)

    # 1. Strict mapping with the documented field table.
    if spec is not None:
        parents = {key.rsplit(".", 1)[0] for key in known if "." in key}
        for path, value in flat.items():
            kind = json_type(value)
            spec_field = known.get(path)
            if spec_field is None:
                if path in parents:  # a documented object such as "PostalAddress"
                    unrecognised.discard(path)
                    if _is_blank(value):  # null or {}: simply empty
                        leftovers.discard(path)
                    else:  # a string or list where an object belongs: keep it, report it
                        drift.mistyped.append(f"{path}: expected object, got {kind}")
                continue
            unrecognised.discard(path)
            if kind != "null" and kind not in spec_field.types:
                drift.mistyped.append(f"{path}: expected {'/'.join(spec_field.types)}, got {kind}")
                if kind in ("object", "array"):
                    continue  # unusable as a single value; stays in leftovers -> extras
            leftovers.discard(path)
            if spec_field.target is None:
                if not _is_blank(value):
                    extras[path] = value
            elif _is_blank(values.get(spec_field.target)):
                values[spec_field.target] = value
        present = set(flat)
        drift.missing.extend(f.key for f in spec.fields if f.required and f.key not in present)

    drift.unknown.extend(sorted(unrecognised))

    # 2. Tolerant recovery: look for still-missing fields by alias among the values the
    #    table did not use. A payload that matches the table never gets here. For a known
    #    format only the fields that format documents are searched for, so an unrelated
    #    new key (say a nested "Pro.Name") is never mistaken for the consumer's name.
    by_alias: dict[str, list[tuple[str, Any]]] = {}
    for path in sorted(leftovers, key=lambda p: (p.count("."), p)):  # shallowest first
        if not _is_blank(flat[path]) and not isinstance(flat[path], dict):
            by_alias.setdefault(norm_key(path.rsplit(".", 1)[-1]), []).append((path, flat[path]))
    recoverable = set(ALIASES) if spec is None else {f.target for f in spec.fields if f.target}
    recovered_paths: set[str] = set()
    for target, aliases in ALIASES.items():
        if target not in recoverable or not _is_blank(values.get(target)):
            continue
        # The consumer's details are never read from the part of the payload that
        # describes the business, nor the account's from the consumer's part.
        wanted = "consumer" if target in CONSUMER_TARGETS else "business" if target in ACCOUNT_TARGETS else None
        hits = [
            (path, value)
            for alias in aliases
            for path, value in by_alias.get(alias, ())
            if path not in recovered_paths
            and isinstance(value, list) == (target == "interview")  # the one field that is a list
            and (wanted is None or _party(path) in (wanted, None))
        ]
        if not hits:
            continue
        # A branch that names the right party beats one that names nobody; otherwise
        # the first alias listed wins, and for one alias the shallowest path.
        path, value = next((hit for hit in hits if wanted and _party(hit[0]) == wanted), hits[0])
        values[target] = value
        recovered_paths.add(path)
        drift.recovered.append(f"{target} <- {path}")

    for path in leftovers - recovered_paths:
        if not _is_blank(flat[path]):
            extras[path] = flat[path]

    # 3. The interview is a list whose items have a shape of their own. If that shape
    #    changes ("q" / "a" instead of "question" / "answer") the answers would vanish
    #    without a trace, so report it like any other type change and keep the raw list.
    interview = values.get("interview")
    if isinstance(interview, list) and not all(_is_question_and_answer(item) for item in interview):
        drift.mistyped.append("interview: expected a list of {question, answer} objects")
        extras.setdefault("interview", interview)

    detected = spec.name if spec else UNKNOWN_FORMAT
    lead, issues = _build_lead(detected, values, extras)

    if spec is None:
        # No known format: keep the lead only if we found a way to reach the consumer.
        mode = "tolerant" if lead.is_contactable else "failed"
    else:
        mode = "tolerant" if (drift.missing or drift.recovered) else "strict"

    return ParseResult(
        detected_format=detected,
        mode=mode,
        lead=lead if mode != "failed" else None,
        drift=drift,
        issues=issues,
        signature=_signature(detected, drift),
    )


def _build_lead(format_name: str, values: dict[str, Any], extras: dict[str, Any]) -> tuple[NormalizedLead, list[str]]:
    issues: list[str] = []

    first = clean_str(values.get("first_name"), 100)
    last = clean_str(values.get("last_name"), 100)
    full = clean_str(values.get("full_name"), 200)
    if not full and (first or last):
        full = " ".join(part for part in (first, last) if part)
    if full and not first and not last:
        parts = full.split(None, 1)
        first, last = parts[0], (parts[1] if len(parts) > 1 else None)

    email_raw = clean_str(values.get("email"), 320)
    email_normalized = normalize_email(email_raw)
    if not email_raw:
        issues.append("no email address in the payload")
    elif not email_normalized:
        issues.append("email is not a valid address")

    phone_raw = _scalar_str(values.get("phone"), 40)
    phone_normalized = normalize_phone(phone_raw)
    if phone_raw and not phone_normalized:
        issues.append("primary phone is not a valid US/Canada number")
    secondary_raw = _scalar_str(values.get("secondary_phone"), 40)
    secondary_normalized = normalize_phone(secondary_raw)

    fee_raw = values.get("fee")
    fee_cents = to_cents(fee_raw)
    if not _is_blank(fee_raw) and fee_cents is None:
        issues.append("fee is not a valid amount")

    address_line1 = clean_str(values.get("address_line1"), 300)
    city = clean_str(values.get("city"), 120)
    state = clean_str(values.get("state"), 60)
    postal_code = _postal_code(values.get("postal_code"))
    if not state and not postal_code:
        state, postal_code = split_crammed_address(address_line1)

    task_name = clean_str(values.get("task_name"), 300)
    market = None
    if format_name == ANGI_ADS.name and task_name:
        market, service = split_category(task_name, city)
        if market:
            extras.setdefault("Category", task_name)  # keep Angi's original wording
            task_name = service

    lead = NormalizedLead(
        source_format=format_name,
        external_lead_id=_scalar_str(values.get("external_lead_id")),
        external_request_id=_scalar_str(values.get("external_request_id")),
        external_account_id=_scalar_str(values.get("external_account_id")),
        external_account_name=clean_str(values.get("external_account_name"), 200),
        crm_key=_scalar_str(values.get("crm_key"), 200),
        first_name=first,
        last_name=last,
        full_name=full,
        email=email_raw,
        email_normalized=email_normalized,
        phone=phone_raw,
        phone_normalized=phone_normalized,
        phone_ext=_scalar_str(values.get("phone_ext"), 20),
        phone_is_masked=to_bool(values.get("phone_is_masked")),
        secondary_phone=secondary_raw,
        secondary_phone_normalized=secondary_normalized,
        address_line1=address_line1,
        address_line2=clean_str(values.get("address_line2"), 300),
        city=city,
        state=state,
        postal_code=postal_code,
        task_name=task_name,
        market=market,
        comments=clean_str(values.get("comments"), 5000),
        urgency=clean_str(values.get("urgency"), 200),
        match_type=clean_str(values.get("match_type"), 100),
        lead_description=clean_str(values.get("lead_description"), 200),
        lead_source_detail=clean_str(values.get("lead_source_detail"), 200),
        contact_status=clean_str(values.get("contact_status"), 200),
        fee_cents=fee_cents,
        interview=_interview(values.get("interview")),
        trusted_form_url=clean_str(values.get("trusted_form_url"), 1000),
        automated_contact_ok=to_bool(values.get("automated_contact_ok")),
        extras=extras,
    )
    if not lead.is_contactable:
        issues.append("no usable contact details (no valid email or phone)")
    return lead, issues


def _signature(format_name: str, drift: Drift) -> str:
    """One stable id per *kind* of deviation, so a format change raises one alert with
    a counter rather than one alert per lead."""
    if format_name != UNKNOWN_FORMAT and drift.is_clean:
        return f"{format_name}:clean"
    # Built from key names only. `recovered` is left out: it follows from unknown +
    # missing, and whether a value happened to be blank on this lead must not change it.
    basis = {
        "unknown": sorted(drift.unknown),
        "missing": sorted(drift.missing),
        "mistyped": sorted(item.split(":", 1)[0] for item in drift.mistyped),
    }
    digest = hashlib.sha256(dumps(basis).encode("utf-8")).hexdigest()[:12]
    return f"{format_name}:{digest}"
