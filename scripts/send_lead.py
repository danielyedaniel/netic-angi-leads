"""Send a sample lead to the webhook, the way Angi would. For the person running the demo.

Run from the project root (put your own address where it says you@example.com):

    # the spec example as a brand-new lead from a brand-new consumer, addressed to you
    uv run python scripts/send_lead.py --email you@example.com

    # another sample (another tenant), with a name of your choosing
    uv run python scripts/send_lead.py --sample angi_ads_emergency_hvac --email you@example.com \
        --first-name Dana --last-name Whitfield

    # the Angi Leads (camelCase) feed
    uv run python scripts/send_lead.py --sample angi_leads_example --email you@example.com

    # Angi retrying the very same lead: the second send answers "duplicate_delivery"
    uv run python scripts/send_lead.py --email you@example.com --same-id --repeat 2

    # the same consumer asking again (new lead id, same phone): the second send is flagged
    # as a duplicate lead and a credit claim is opened
    uv run python scripts/send_lead.py --email you@example.com --same-consumer --repeat 2

    # an Angi account nobody mapped to a tenant: stored and quarantined, not lost
    uv run python scripts/send_lead.py --account 000000

    # Angi's own example with its JSON defects (not valid JSON), bytes untouched
    uv run python scripts/send_lead.py --sample malformed_spec_example --raw

    # a tenant-specific webhook URL instead of the shared API key. The sample's Angi account
    # has to be that tenant's (or one that is not mapped yet, which the URL then claims): the
    # default sample is Sparkle Home Cleaning's, and under another tenant's key it is
    # quarantined as a mapping_conflict
    uv run python scripts/send_lead.py --tenant-key <webhook_key of Sparkle Home Cleaning>

    # through the public ngrok URL instead of localhost
    uv run python scripts/send_lead.py --url https://your-name.ngrok-free.dev --email you@example.com

The URL and the API key default to PUBLIC_BASE_URL and the first entry of
ANGI_WEBHOOK_API_KEYS, read from the environment or from ./.env. The key is never printed.

By default every send is a NEW lead from a NEW consumer:

* the sample's CorrelationId (Angi Ads) or leadOid + srOid (Angi Leads) is replaced with
  a fresh one, and
* the sample's phone number is replaced with a fresh fictional one (555-01xx), unless
  ``--phone`` is given. Without that, everybody who tries the same sample would share
  one phone number and be flagged as each other's duplicate.

``--same-consumer`` keeps the sample's phone (a repeat request from one homeowner).
``--same-id`` keeps the sample's lead id and its phone (Angi delivering one lead again).
The same ``--email`` on two leads for one Angi account is, rightly, the same consumer too.

It never emails a stranger: the samples' own addresses are @example.com (which the
service never hands to a real mail server), and a payload containing any other address
is refused unless that address is the one you gave with ``--email``.

Prints the HTTP status, the elapsed milliseconds and the JSON answer. The exit code is 0
only if every send was answered with HTTP 200, and 2 if nothing was sent because the
request made no sense.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import random
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

SAMPLES_DIR = Path(__file__).resolve().parent.parent / "samples"
DEFAULT_URL = "http://localhost:8000"
DEFAULT_SAMPLE = "angi_ads_spec_example"
WEBHOOK_PATH = "/webhooks/angi/leads"

ANGI_ADS = "angi_ads"
ANGI_LEADS = "angi_leads"

# Which payload key each command-line override sets, per format. The first spelling is
# the documented one; a second spelling is one our drift samples use for the same field.
ADS_KEYS = {
    "email": ("Email", "EmailAddress"),
    "first_name": ("FirstName",),
    "last_name": ("LastName",),
    "phone": ("PhoneNumber",),
    "account": ("ALAccountId",),
}
LEADS_KEYS = {
    "email": ("email",),
    "first_name": ("firstName",),
    "last_name": ("lastName",),
    "phone": ("primaryPhone",),
    "account": ("spEntityId",),
}

# Addresses under these domains (and their subdomains) can never reach a real person (RFC 2606).
RESERVED_DOMAINS = ("example.com", "example.org", "example.net", "example", "test", "invalid")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


# --- configuration -------------------------------------------------------------------


def read_dotenv(path: str | Path = ".env") -> dict[str, str]:
    """The KEY=VALUE lines of a .env file ({} if there is none)."""
    values: dict[str, str] = {}
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


def setting(name: str, default: str = "", dotenv_path: str | Path = ".env") -> str:
    """A setting from the environment, else from ./.env, else the default."""
    return os.environ.get(name) or read_dotenv(dotenv_path).get(name) or default


def first_api_key(value: str) -> str:
    """ANGI_WEBHOOK_API_KEYS is a comma-separated list (for key rotation); use the first."""
    keys = [part.strip() for part in value.split(",") if part.strip()]
    return keys[0] if keys else ""


# --- the sample ----------------------------------------------------------------------


def resolve_sample(name: str) -> Path:
    """``name`` is a file in samples/ without its extension, or a path to any file."""
    candidates = [Path(name), SAMPLES_DIR / name, SAMPLES_DIR / f"{name}.json", SAMPLES_DIR / f"{name}.txt"]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    available = ", ".join(sorted(p.stem for p in SAMPLES_DIR.iterdir() if p.suffix in (".json", ".txt")))
    raise FileNotFoundError(f"no sample named {name!r}. Available: {available}")


def find_key(payload: dict[str, Any], name: str) -> str | None:
    """The payload's own spelling of ``name`` (``correlationId`` for ``CorrelationId``)."""
    for key in payload:
        if key.lower() == name.lower():
            return key
    return None


def payload_format(payload: dict[str, Any]) -> str | None:
    """Which of Angi's two feeds this payload is, judged by its id fields."""
    if find_key(payload, "leadOid") or find_key(payload, "srOid") or find_key(payload, "spEntityId"):
        return ANGI_LEADS
    if find_key(payload, "CorrelationId") or find_key(payload, "ALAccountId"):
        return ANGI_ADS
    return None


def _set(payload: dict[str, Any], spellings: tuple[str, ...], value: Any) -> None:
    """Set a field, keeping the key the sample already uses for it (so a re-cased or
    renamed drift sample stays drifted). A missing field is added under its documented name."""
    for spelling in spellings:
        existing = find_key(payload, spelling)
        if existing is not None:
            payload[existing] = value
            return
    payload[spellings[0]] = value


def apply_overrides(
    payload: dict[str, Any],
    *,
    email: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    phone: str | None = None,
    account: str | None = None,
) -> dict[str, Any]:
    """A copy of the payload with the given lead fields replaced, in whichever of the
    two formats the payload is. ``None`` leaves a field alone."""
    wanted = {"email": email, "first_name": first_name, "last_name": last_name, "phone": phone, "account": account}
    wanted = {name: value for name, value in wanted.items() if value is not None}
    result = copy.deepcopy(payload)
    if not wanted:
        return result

    kind = payload_format(result)
    if kind is None:
        raise ValueError("this sample is neither Angi Ads nor Angi Leads shaped, so its fields cannot be overridden")
    keys = ADS_KEYS if kind == ANGI_ADS else LEADS_KEYS
    for name, value in wanted.items():
        if kind == ANGI_LEADS and name == "account" and value.isdigit():
            value = int(value)  # Angi Leads sends spEntityId as a number
        _set(result, keys[name], value)

    if kind == ANGI_LEADS and ("first_name" in wanted or "last_name" in wanted):
        # Angi Leads also carries the full name; keep it consistent with its parts.
        parts = (result.get("firstName"), result.get("lastName"))
        result["name"] = " ".join(str(part) for part in parts if part)
    return result


def with_fresh_ids(payload: dict[str, Any]) -> dict[str, Any]:
    """A copy of the payload that Angi would consider a different lead: a new
    CorrelationId (Angi Ads) or a new leadOid + srOid (Angi Leads). Any other shape is
    returned unchanged."""
    result = copy.deepcopy(payload)
    kind = payload_format(result)
    if kind == ANGI_ADS:
        _set(result, ("CorrelationId",), str(uuid.uuid4()))
    elif kind == ANGI_LEADS:
        _set(result, ("leadOid",), random.randint(100_000_000, 999_999_999))
        _set(result, ("srOid",), random.randint(100_000_000, 999_999_999))
    return result


def fictional_phone() -> str:
    """A ten-digit phone number nobody owns: 555-0100 to 555-0199 is reserved for
    fiction in every North American area code. The area code is random but well-formed
    (not 0xx / 1xx, not a service code such as 911), so the number normalises like a real one."""
    while True:
        area = random.randint(200, 989)
        if area % 100 != 11 and area // 10 % 10 != 9:
            return f"{area}55501{random.randint(0, 99):02d}"


def with_fresh_phone(payload: dict[str, Any]) -> dict[str, Any]:
    """A copy of the payload whose consumer has a phone number of their own, so that two
    people sending the same sample are not taken for one homeowner. The field keeps its
    spelling and its type (one drift sample sends the phone as a number on purpose); a
    payload of another shape, or one with no phone at all, is returned unchanged."""
    result = copy.deepcopy(payload)
    kind = payload_format(result)
    if kind is None:
        return result
    key = find_key(result, (ADS_KEYS if kind == ANGI_ADS else LEADS_KEYS)["phone"][0])
    if key is not None and result[key]:
        number = fictional_phone()
        result[key] = int(number) if isinstance(result[key], int) else number
    return result


def lead_id_of(payload: dict[str, Any]) -> str | None:
    """Angi's id for the lead, for the progress line."""
    for name in ("CorrelationId", "leadOid"):
        key = find_key(payload, name)
        if key is not None:
            return str(payload[key])
    return None


def build_body(
    raw: bytes,
    *,
    send_raw: bool = False,
    same_id: bool = False,
    same_consumer: bool = False,
    overrides: dict[str, str | None] | None = None,
) -> tuple[bytes, str | None]:
    """-> (the bytes to POST, Angi's lead id if the body has one).

    By default the body is a new lead (fresh id) from a new consumer (fresh phone).
    ``same_consumer`` keeps the sample's phone; ``same_id`` keeps its lead id and, since
    a redelivery is the same request again, its phone as well; a phone given as an
    override is used as it is.

    A sample that is not valid JSON can only be sent untouched, like ``--raw``; asking
    to override its fields is an error."""
    overrides = {name: value for name, value in (overrides or {}).items() if value is not None}
    if send_raw:
        if overrides:
            raise ValueError("--raw sends the file untouched, so it cannot be combined with field overrides")
        return raw, None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        if overrides:
            raise ValueError(f"the sample is not valid JSON ({exc}), so its fields cannot be overridden") from exc
        return raw, None
    if not isinstance(payload, dict):
        if overrides:
            raise ValueError("the sample is not a JSON object, so its fields cannot be overridden")
        return raw, None
    payload = apply_overrides(payload, **overrides)
    if not same_id:
        payload = with_fresh_ids(payload)
        if not same_consumer and "phone" not in overrides:
            payload = with_fresh_phone(payload)
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"), lead_id_of(payload)


def is_reserved_address(address: str) -> bool:
    domain = address.rsplit("@", 1)[-1].lower()
    return any(domain == reserved or domain.endswith("." + reserved) for reserved in RESERVED_DOMAINS)


def real_looking_addresses(body: bytes) -> list[str]:
    """Email addresses anywhere in the body that might belong to a real person. The
    whole text is searched, not one field: the service finds an address under renamed
    and nested keys too, and a body that is not valid JSON has no fields to look at."""
    found = _EMAIL.findall(body.decode("utf-8", errors="replace"))
    return sorted({address for address in found if not is_reserved_address(address)})


# --- sending -------------------------------------------------------------------------


def webhook_url(base_url: str, tenant_key: str | None = None) -> str:
    url = base_url.rstrip("/") + WEBHOOK_PATH
    return f"{url}/{tenant_key}" if tenant_key else url


def request_headers(api_key: str, tenant_key: str | None = None) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if api_key and not tenant_key:  # a tenant-keyed URL is its own credential
        headers["X-API-KEY"] = api_key
    return headers


def post_lead(client: Any, url: str, body: bytes, headers: dict[str, str]) -> tuple[int, float, Any]:
    """POST one lead. -> (HTTP status, elapsed milliseconds, parsed JSON or the raw text).
    ``client`` is an ``httpx.Client`` (or anything with the same ``post``)."""
    started = time.perf_counter()
    response = client.post(url, content=body, headers=headers)
    elapsed_ms = (time.perf_counter() - started) * 1000
    try:
        answer: Any = response.json()
    except ValueError:
        answer = response.text
    return response.status_code, elapsed_ms, answer


def format_answer(answer: Any) -> str:
    if isinstance(answer, str):
        return answer
    return json.dumps(answer, indent=2, ensure_ascii=False)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Send a sample lead to the Angi webhook.",
        epilog="See the top of scripts/send_lead.py for examples.",
    )
    parser.add_argument("--url", help=f"base URL of the service (default: $PUBLIC_BASE_URL or {DEFAULT_URL})")
    parser.add_argument("--api-key", help="webhook API key (default: first entry of $ANGI_WEBHOOK_API_KEYS)")
    parser.add_argument("--tenant-key", help=f"post to {WEBHOOK_PATH}/<key> instead of using the API key")
    parser.add_argument(
        "--sample", default=DEFAULT_SAMPLE,
        help=f"a name in samples/ without extension, or a path (default: {DEFAULT_SAMPLE})",
    )
    parser.add_argument("--email", help="send the lead's intro email to this address")
    parser.add_argument("--first-name", help="the consumer's first name")
    parser.add_argument("--last-name", help="the consumer's last name")
    parser.add_argument("--phone", help="the consumer's phone number (default: a fresh fictional one per send)")
    parser.add_argument("--account", help="Angi account id (ALAccountId / spEntityId), i.e. which tenant")
    parser.add_argument(
        "--same-id", action="store_true",
        help="keep the sample's lead id and phone, so the send is a redelivery (default: a fresh id, i.e. a new lead)",
    )
    parser.add_argument(
        "--same-consumer", action="store_true",
        help="keep the sample's phone, so a second send is a duplicate lead (default: a fresh fictional phone)",
    )
    parser.add_argument("--repeat", type=int, default=1, metavar="N", help="send N times (default: 1)")
    parser.add_argument("--raw", action="store_true", help="send the file's bytes untouched (for the malformed sample)")
    args = parser.parse_args(argv)
    if args.repeat < 1:
        parser.error("--repeat must be at least 1")
    return args


def main(argv: list[str] | None = None, client: Any = None) -> int:
    """``client`` lets a test pass FastAPI's TestClient; normally a real httpx.Client is made."""
    args = parse_args(argv)
    base_url = args.url or setting("PUBLIC_BASE_URL", DEFAULT_URL)
    api_key = args.api_key or first_api_key(setting("ANGI_WEBHOOK_API_KEYS"))
    overrides = {
        "email": args.email,
        "first_name": args.first_name,
        "last_name": args.last_name,
        "phone": args.phone,
        "account": args.account,
    }
    def body_for_this_send() -> tuple[bytes, str | None]:
        return build_body(
            raw, send_raw=args.raw, same_id=args.same_id, same_consumer=args.same_consumer, overrides=overrides
        )

    try:
        sample = resolve_sample(args.sample)
        raw = sample.read_bytes()
        body, _ = body_for_this_send()  # fail before sending anything
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    # Never email a stranger. The samples only carry @example.com addresses; anything else
    # has to be the address the person running this asked for.
    strangers = [address for address in real_looking_addresses(body) if address.lower() != (args.email or "").lower()]
    if strangers:
        print(
            f"error: this payload contains {', '.join(strangers)}, which may be a real person's mailbox, "
            "and a server with a real email provider would write to it. Nothing was sent. Pass --email "
            "with your own address, or change the address in the file to one @example.com.",
            file=sys.stderr,
        )
        return 2

    url = webhook_url(base_url, args.tenant_key)
    shown_url = webhook_url(base_url, "<tenant key>" if args.tenant_key else None)
    headers = request_headers(api_key, args.tenant_key)
    if not api_key and not args.tenant_key:
        print("note: no API key found (ANGI_WEBHOOK_API_KEYS); sending without one", file=sys.stderr)

    owns_client = client is None
    if owns_client:
        client = httpx.Client(timeout=30.0)
    all_ok = True
    try:
        for number in range(1, args.repeat + 1):
            body, lead_id = body_for_this_send()
            label = f"[{number}/{args.repeat}] " if args.repeat > 1 else ""
            print(f"{label}POST {shown_url}  sample={sample.name}  lead id={lead_id or '(as in the file)'}")
            try:
                status, elapsed_ms, answer = post_lead(client, url, body, headers)
            except httpx.HTTPError as exc:
                print(f"error: could not reach {base_url}: {type(exc).__name__}: {exc}", file=sys.stderr)
                return 1
            print(f"HTTP {status} in {elapsed_ms:.0f} ms")
            print(format_answer(answer))
            all_ok = all_ok and status == 200
    finally:
        if owns_client:
            client.close()
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
