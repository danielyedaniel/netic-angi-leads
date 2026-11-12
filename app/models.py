"""The canonical lead: one shape for every lead, whatever payload variant Angi sent.

Everything downstream (tenant mapping, duplicate detection, email, analytics) works on
this model only, so supporting a new payload format means writing one more table in
``app/angi/formats.py`` and nothing else.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class InterviewAnswer(BaseModel):
    question: str
    answer: str


class NormalizedLead(BaseModel):
    source: str = "angi"
    source_format: str

    # Identifiers assigned by the lead source.
    external_lead_id: str | None = None  # unique per lead (CorrelationId / leadOid)
    external_request_id: str | None = None  # the consumer's service request (srOid)
    external_account_id: str | None = None  # which business the lead was sold to
    external_account_name: str | None = None
    crm_key: str | None = None  # routing value we gave Angi for this tenant, echoed back

    # Consumer contact details. *_normalized are the comparable forms
    # (lower-cased email, E.164 phone) and are None when the value is unusable.
    first_name: str | None = None
    last_name: str | None = None
    full_name: str | None = None
    email: str | None = None
    email_normalized: str | None = None
    phone: str | None = None
    phone_normalized: str | None = None
    phone_ext: str | None = None
    phone_is_masked: bool | None = None  # True = a temporary Angi proxy number, not the consumer's own
    secondary_phone: str | None = None
    secondary_phone_normalized: str | None = None

    address_line1: str | None = None
    address_line2: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None

    # What they want done.
    task_name: str | None = None  # the service requested
    market: str | None = None  # Angi's market for the request, when it names one
    comments: str | None = None
    urgency: str | None = None
    match_type: str | None = None
    lead_description: str | None = None
    lead_source_detail: str | None = None
    contact_status: str | None = None
    fee_cents: int | None = None
    interview: list[InterviewAnswer] = Field(default_factory=list)
    trusted_form_url: str | None = None
    # Did the consumer consent to automated calls / texts? None = Angi did not say.
    automated_contact_ok: bool | None = None

    # Anything in the payload we do not model yet, kept so it is never lost.
    extras: dict[str, Any] = Field(default_factory=dict)

    @property
    def display_name(self) -> str:
        return self.full_name or " ".join(p for p in (self.first_name, self.last_name) if p) or "Unknown"

    @property
    def is_contactable(self) -> bool:
        return bool(self.email_normalized or self.phone_normalized or self.secondary_phone_normalized)
