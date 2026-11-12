"""Authentication for the two kinds of caller.

* **Angi** proves itself with a shared API key (header, bearer token, basic-auth
  password, or - for senders that can only be given a URL - a query parameter), or by
  posting to a tenant-specific secret URL.
* **Operators** reach the dashboard and ``/admin`` API with the admin token, sent as a
  bearer token (scripts) or as the HTTP Basic password (browsers prompt for it).

Comparisons are constant-time so the key cannot be guessed byte by byte from timing.
"""

from __future__ import annotations

import base64
import binascii
import hmac

from fastapi import HTTPException, Request

from app.config import Settings

API_KEY_HEADERS = ("x-api-key", "api-key", "x-angi-api-key", "x-webhook-key")
API_KEY_QUERY_PARAMS = ("api_key", "apikey", "key")


def _matches(presented: str | None, expected: str) -> bool:
    if not presented or not expected:
        return False
    return hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))


def _from_authorization(header: str | None) -> list[str]:
    """Credentials carried by an Authorization header: the bearer token, or the
    basic-auth password (and username, since some senders put the key there)."""
    if not header:
        return []
    scheme, _, value = header.partition(" ")
    value = value.strip()
    if not value:
        return [scheme]  # "Authorization: <key>" with no scheme at all
    if scheme.lower() == "bearer":
        return [value] if value else []
    if scheme.lower() == "basic":
        try:
            decoded = base64.b64decode(value, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return []
        username, _, password = decoded.partition(":")
        return [c for c in (password, username) if c]
    return []


def presented_api_keys(request: Request) -> list[str]:
    candidates = [request.headers.get(name) for name in API_KEY_HEADERS]
    candidates.extend(_from_authorization(request.headers.get("authorization")))
    candidates.extend(request.query_params.get(name) for name in API_KEY_QUERY_PARAMS)
    return [c.strip() for c in candidates if c and c.strip()]


def webhook_auth_ok(settings: Settings, request: Request) -> bool:
    keys = settings.webhook_api_keys
    if not keys:
        # No key configured: open in development (with a startup warning), closed in production.
        return not settings.is_production
    return any(_matches(candidate, key) for candidate in presented_api_keys(request) for key in keys)


def admin_auth_ok(settings: Settings, request: Request) -> bool:
    token = settings.admin_token
    if not token:
        return not settings.is_production
    candidates = _from_authorization(request.headers.get("authorization"))
    return any(_matches(candidate, token) for candidate in candidates)


def require_admin(request: Request) -> None:
    """FastAPI dependency for the dashboard and the admin API."""
    settings: Settings = request.app.state.ctx.settings
    if not admin_auth_ok(settings, request):
        raise HTTPException(
            status_code=401,
            detail="Admin token required (HTTP Basic password or Bearer token).",
            headers={"WWW-Authenticate": 'Basic realm="Angi lead integration", charset="UTF-8"'},
        )


def require_same_origin(request: Request) -> None:
    """FastAPI dependency for state-changing operator routes. A browser attaches the
    cached Basic credentials to cross-site form posts too, so a request that names an
    Origin must name ours. Scripts and curl send no Origin header and pass."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    origin = request.headers.get("origin")
    if origin is None:
        return
    host = request.headers.get("host", "")
    if origin.split("://", 1)[-1].rstrip("/").lower() != host.lower():
        raise HTTPException(status_code=403, detail="Cross-site request refused.")


def client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return request.client.host if request.client else None
