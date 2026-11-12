"""Timing rules for outreach after the first response.

The first response always goes out immediately, at any hour: an email is unobtrusive
and speed is what wins the job. Follow-ups are different - a "just checking in" at
3 AM reads as a bot - so they are held until the tenant's local morning.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.booking import tenant_zone

QUIET_FROM_HOUR = 20  # 8 PM local
QUIET_UNTIL_HOUR = 8  # 8 AM local
FOLLOWUP_MIN_GAP = timedelta(minutes=30)  # never nudge someone we emailed moments ago


def defer_past_quiet_hours(tenant: dict, when: datetime) -> datetime:
    """``when`` itself if it falls in daytime for the tenant, else the next 8 AM."""
    local = when.astimezone(tenant_zone(tenant))
    if local.hour >= QUIET_FROM_HOUR:
        local = (local + timedelta(days=1)).replace(hour=QUIET_UNTIL_HOUR, minute=0, second=0, microsecond=0)
    elif local.hour < QUIET_UNTIL_HOUR:
        local = local.replace(hour=QUIET_UNTIL_HOUR, minute=0, second=0, microsecond=0)
    else:
        return when
    return local.astimezone(UTC)
