"""Runtime configuration.

Everything is read from environment variables (or a local ``.env`` file), so the same
code runs locally behind ngrok and in a real deployment. See ``.env.example`` for the
full list with explanations.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


def _csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- general -----------------------------------------------------------------
    # Anything other than an explicit development value is treated as production, so a
    # typo ("prod ", "staging") fails safe: the app refuses to start with auth disabled.
    app_env: str = "development"
    public_base_url: str = "http://localhost:8000"  # the ngrok https URL during the demo
    database_path: str = "data/leads.db"
    max_body_bytes: int = 256 * 1024
    # Create the demo tenants and Angi account mappings on first start (empty database).
    seed_demo_data: bool = True

    # --- inbound auth ------------------------------------------------------------
    # Shared secret(s) Angi sends with every lead. Comma-separated so a key can be
    # rotated without downtime (accept old + new, then drop the old one).
    angi_webhook_api_keys: str = ""
    # Protects the dashboard and the /admin API.
    admin_token: str = ""

    # --- tenant mapping ----------------------------------------------------------
    # If set, leads from an Angi account we have no mapping for are routed to this
    # tenant (and flagged) instead of being quarantined. Handy for a demo; leave
    # empty in production so we never email a consumer on behalf of the wrong business.
    fallback_tenant_slug: str = ""

    # --- outbound email ----------------------------------------------------------
    email_provider: str = "console"  # console | smtp | resend
    # Leave empty to send from SMTP_USERNAME (what Gmail requires anyway).
    email_from_address: str = ""
    email_reply_to: str = ""  # optional global Reply-To override
    # Safety valve for development: send every email to this address instead of the lead.
    email_redirect_all_to: str = ""
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 465
    smtp_security: str = "ssl"  # ssl | starttls | none
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_timeout_seconds: float = 15.0
    resend_api_key: str = ""

    # --- speed to lead / follow-up cadence ----------------------------------------
    # Minutes after the intro email at which to nudge a lead who has not booked yet.
    followup_delays_minutes: str = "60,1440"
    outbox_poll_seconds: float = 2.0
    outbox_max_attempts: int = 5
    run_background_worker: bool = True

    # --- duplicates / lead credits -------------------------------------------------
    # Angi: a duplicate is being "charged twice for the same lead/homeowner within 45 days".
    duplicate_window_days: int = 45
    # Angi Pro Terms: charge disputes must be raised within 30 days of the charge.
    credit_request_window_days: int = 30
    # Angi only considers a credit if the pro tried to contact the homeowner within 24h.
    credit_contact_sla_hours: int = 24

    # --- monitoring ----------------------------------------------------------------
    monitor_interval_seconds: float = 60.0
    # Speed-to-lead SLA: an email still unsent after this long raises an alert.
    email_sla_seconds: float = 120.0
    # Share of recent webhook events that failed/quarantined before we suspect a format change.
    error_rate_threshold: float = 0.2
    error_rate_min_events: int = 3
    error_rate_window_minutes: int = 60
    # "No leads at all" detection (dead man's switch). 0 disables the fixed threshold;
    # the rate-based check still runs for tenants with enough history.
    silence_alert_minutes: int = 0
    # How often scripts/canary.py is scheduled to run. 0 = not scheduled; otherwise a
    # canary that has not arrived for three intervals raises an alert.
    canary_interval_minutes: int = 0
    alert_slack_webhook_url: str = ""
    alert_email_to: str = ""

    # --- derived -------------------------------------------------------------------
    @property
    def webhook_api_keys(self) -> list[str]:
        return _csv(self.angi_webhook_api_keys)

    @property
    def followup_delays(self) -> list[int]:
        delays: list[int] = []
        for part in _csv(self.followup_delays_minutes):
            try:
                minutes = int(part)
            except ValueError:
                continue
            if minutes > 0:
                delays.append(minutes)
        return sorted(delays)

    @property
    def is_production(self) -> bool:
        return self.app_env.strip().lower() not in {"dev", "development", "local", "test"}

    @property
    def sender_address(self) -> str:
        return self.email_from_address or self.smtp_username or "leads@example.com"

    @property
    def base_url(self) -> str:
        return self.public_base_url.rstrip("/")
