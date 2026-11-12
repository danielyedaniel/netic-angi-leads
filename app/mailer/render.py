"""Turns (template name, lead, tenant) into a subject + text + HTML email.

Lead data is untrusted input: HTML templates are auto-escaped, anything that goes
into a header is collapsed to one line, and the consumer's own words are only played
back with links removed (see ``reflect``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from app.booking import Slot, slot_from_appointment
from app.util import loads, normalize_phone, one_line

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates" / "email"
TEMPLATES = ("intro", "repeat_request", "followup", "booking_confirmation")
SLOTS_IN_EMAIL = 3
INTERVIEW_ANSWERS_IN_EMAIL = 3
DETAIL_MAX_LEN = 120

# What a mail client would turn into something clickable: scheme://..., www...., an
# email address, or a bare domain (word.tld, with or without a path).
_LINK_LIKE = re.compile(
    r"[a-z][a-z0-9+.-]*://\S*|www\.\S+|\S+@\S+|[\w-]+(?:\.[\w-]+)*\.[^\W\d_]{2,}(?![a-z0-9])(?:[/:]\S*)?",
    re.IGNORECASE,
)


@dataclass
class RenderedEmail:
    subject: str
    text: str
    html: str


def booking_url(base_url: str, token: str) -> str:
    return f"{base_url}/book/{token}"


def unsubscribe_url(base_url: str, token: str) -> str:
    return f"{base_url}/unsubscribe/{token}"


def reflect(value: object, max_len: int = DETAIL_MAX_LEN) -> str:
    """Consumer-supplied text, made safe to repeat in an email that the business signs.

    Anyone can file a request under somebody else's address, so whatever we play back
    arrives there as if the business had written it. Links and addresses are therefore
    removed and the length is capped: what is left cannot send the reader anywhere.
    It errs on the side of removing ("Mr.Smith" goes too); the cost is a missing word
    in a courtesy summary."""
    text = one_line(str(value), 2000) if value else ""
    return one_line(_LINK_LIKE.sub(" ", text), max_len).rstrip()  # the cut may land on a space


def first_name_of(lead: dict) -> str:
    """How to greet them. Falls back to "there" rather than risk "Hi Builder,": the
    first word of the full name is only used when we were not given a surname either."""
    name = lead.get("first_name")
    if not name and not lead.get("last_name"):
        name = (lead.get("full_name") or "").split(" ")[0]
    return reflect(name, 40) or "there"


def request_details(lead: dict) -> list[tuple[str, str]]:
    """The lead's own words, played back so the email is clearly about *their* job.
    A value with nothing left after ``reflect`` is left out."""
    place = ", ".join(p for p in (reflect(lead.get("city")), reflect(lead.get("state"))) if p)
    place = f"{place} {reflect(lead.get('postal_code'), 20)}".strip()
    details = [
        ("Service", reflect(lead.get("task_name"))),
        ("Location", reflect(place)),
        ("Timing", reflect(lead.get("urgency"))),
    ]
    for item in (loads(lead.get("interview_json"), []) or [])[:INTERVIEW_ANSWERS_IN_EMAIL]:
        details.append((reflect(item.get("question")).rstrip("?:"), reflect(item.get("answer"))))
    return [(label, value) for label, value in details if label and value]


class EmailRenderer:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.env = Environment(
            loader=FileSystemLoader(str(TEMPLATE_DIR)),
            autoescape=select_autoescape(enabled_extensions=("html",), default_for_string=False),
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
        )

    def render(
        self,
        template: str,
        *,
        lead: dict,
        tenant: dict,
        slots: list[Slot] | None = None,
        appointment: dict | None = None,
    ) -> RenderedEmail:
        if template not in TEMPLATES:
            raise ValueError(f"unknown email template {template!r}")

        book_url = booking_url(self.base_url, lead["booking_token"])
        task = reflect(lead.get("task_name"), 80) or None
        tenant_name = one_line(tenant["name"], 80)
        appointment_label = slot_from_appointment(appointment).label if appointment else None

        context = {
            "first_name": first_name_of(lead),
            "tenant_name": tenant_name,
            "tenant_phone": tenant.get("phone"),
            "tenant_phone_tel": normalize_phone(tenant.get("phone")) or tenant.get("phone"),
            "task": task,
            "city": reflect(lead.get("city"), 60) or None,
            "booking_url": book_url,
            "unsubscribe_url": unsubscribe_url(self.base_url, lead["booking_token"]),
            "slots": [
                {"label": slot.label, "url": f"{book_url}?slot={quote(slot.key)}"}
                for slot in (slots or [])[:SLOTS_IN_EMAIL]
            ],
            "details": request_details(lead),
            "appointment_label": appointment_label,
            "address": reflect(
                ", ".join(p for p in (lead.get("address_line1"), lead.get("city"), lead.get("state")) if p), 160
            )
            or None,
        }

        if template == "intro":
            subject = f"Your {task or 'service'} request: pick a time with {tenant_name}"
        elif template == "repeat_request":
            subject = f"We got your new request - {tenant_name}"
        elif template == "followup":
            subject = f"Still need help with your {task or 'project'}? - {tenant_name}"
        else:
            subject = f"You're booked: {appointment_label or 'appointment confirmed'} - {tenant_name}"

        return RenderedEmail(
            subject=one_line(subject, 180),
            text=self.env.get_template(f"{template}.txt").render(context).strip() + "\n",
            html=self.env.get_template(f"{template}.html").render(context),
        )
