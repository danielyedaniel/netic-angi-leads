"""Small shared helpers: ids, timestamps, and contact-detail normalisation."""

from __future__ import annotations

import json
import re
import secrets
from datetime import UTC, datetime
from typing import Any

from email_validator import EmailNotValidError, validate_email

# --- ids -----------------------------------------------------------------------------


def new_id(prefix: str) -> str:
    """Prefixed random id, e.g. ``lead_9f2c4e1ab03d77c5``. The prefix makes ids
    self-describing in logs and URLs."""
    return f"{prefix}_{secrets.token_hex(8)}"


def new_token() -> str:
    """Unguessable URL token: booking / unsubscribe links and tenant webhook keys.

    It never starts with "-". Such a value typed after a command-line option
    (``send_lead.py --tenant-key -Xy...``) would be read as another option."""
    while True:
        token = secrets.token_urlsafe(18)
        if not token.startswith("-"):
            return token


# --- time ----------------------------------------------------------------------------
# All timestamps are stored as fixed-width UTC ISO-8601 strings ("2026-11-11T22:31:05.123Z").
# Fixed width means plain string comparison in SQL is also chronological comparison.


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    if dt.tzinfo is None:  # a naive value is taken to be UTC, never the machine's local time
        dt = dt.replace(tzinfo=UTC)
    dt = dt.astimezone(UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def seconds_between(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    return (parse_iso(end) - parse_iso(start)).total_seconds()


# --- contact normalisation -------------------------------------------------------------
# Normalised forms are what we match on for duplicate detection; the original values
# are stored too so nothing the lead source sent is lost.

_NON_DIGITS = re.compile(r"[^0-9]+")  # ASCII digits only: \D would let other scripts' digits through
# Characters that cannot be stored or shown safely: unpaired UTF-16 surrogates (what is
# left when a sender truncates text in the middle of an emoji; SQLite cannot encode
# them) and control characters other than tab / newline.
_UNPAIRED_SURROGATES = re.compile(r"[\ud800-\udfff]")
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def scrub(text: str) -> str:
    return _CONTROL_CHARS.sub("", _UNPAIRED_SURROGATES.sub("\ufffd", text))


# "555-433-2646 x123", "... ext. 12", "... #12": an extension written after the number.
_EXTENSION = re.compile(r"\s*(?:extension|ext\.?|x|#)\s*\d{1,6}\s*$", re.IGNORECASE)


def normalize_phone(value: Any) -> str | None:
    """North-American numbers -> E.164 (``+13035551212``). Returns None when the
    value cannot be a real NANP number, so junk never matches other junk."""
    if value is None:
        return None
    # An extension is not part of the number: left in, its digits would make a good
    # number look invalid and the lead would lose its phone for duplicate matching.
    digits = _NON_DIGITS.sub("", _EXTENSION.sub("", str(value)))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return None
    # NANP: area code and exchange cannot start with 0 or 1.
    if digits[0] in "01" or digits[3] in "01":
        return None
    return f"+1{digits}"


def normalize_email(value: Any) -> str | None:
    """Lower-cased, syntax-checked address, or None if it is not a usable address."""
    if value is None:
        return None
    candidate = str(value).strip()
    if not candidate or "=?" in candidate:
        # "=?utf-8?q?...?=" is an RFC 2047 encoded word: the mail library would decode
        # it and deliver to a different mailbox than the one stored here.
        return None
    try:
        result = validate_email(candidate, check_deliverability=False)
    except EmailNotValidError:
        return None
    # The ASCII form (an internationalised domain becomes punycode). It is None when
    # the part before the @ is not ASCII, which plain SMTP cannot deliver to.
    return result.ascii_email.lower() if result.ascii_email else None


RESERVED_EXAMPLE_DOMAINS = ("example.com", "example.org", "example.net")

# The consumer in the example payload printed in Angi's feed documentation. Anyone
# testing an integration posts that example sooner or later, and the address may be a
# real person's mailbox: somebody who never asked any business here for anything.
ANGI_DOCUMENTATION_ADDRESSES = frozenset({"bob.builder@gmail.com"})


def undeliverable_example_reason(address: str) -> str | None:
    """Why an address must never be handed to a real mail server, or None if it may be.

    Two kinds: addresses on the domains reserved for documentation (RFC 2606), where
    nobody receives mail and a real server would only bounce it, and the address in
    Angi's own example payload, which is not a lead's but may be a stranger's."""
    cleaned = address.strip().lower()
    if cleaned in ANGI_DOCUMENTATION_ADDRESSES:
        return "address is the one printed in Angi's example payload; nothing was sent"
    domain = cleaned.rsplit("@", 1)[-1]
    if any(domain == reserved or domain.endswith("." + reserved) for reserved in RESERVED_EXAMPLE_DOMAINS):
        return "address is on a reserved example domain; nothing was sent"
    return None


def is_example_address(address: str) -> bool:
    """True for an address that is only ever an example (see undeliverable_example_reason)."""
    return undeliverable_example_reason(address) is not None


def clean_str(value: Any, max_len: int = 2000) -> str | None:
    """Trimmed string or None. Lead sources send "", " " and null interchangeably."""
    if value is None or isinstance(value, (bool, dict, list)):
        return None
    try:
        text = str(value)
    except ValueError:  # e.g. an integer too long for Python to print
        return None
    text = scrub(text).strip()
    if not text:
        return None
    return text[:max_len]


def one_line(value: str | None, max_len: int = 120) -> str:
    """Collapse whitespace so untrusted text is safe to use in an email header."""
    if not value:
        return ""
    return " ".join(scrub(str(value)).split())[:max_len]


# --- json ----------------------------------------------------------------------------


def dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, default=str)


def loads(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def format_cents(cents: int | None) -> str:
    if cents is None:
        return "—"
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents) / 100:,.2f}"
