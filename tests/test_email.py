"""Email content and email transport.

* rendering (app/mailer/render.py + app/templates/email): every template, for a fully
  populated lead and for a nearly empty one, with hostile lead data;
* MIME building and the providers (app/mailer/providers.py), with smtplib and
  httpx.post replaced by fakes so nothing leaves the machine.
"""

from __future__ import annotations

import re
import smtplib
import ssl
from email import message_from_bytes, policy
from email.message import EmailMessage
from urllib.parse import quote

import httpx
import pytest

from app.booking import generate_slots
from app.config import Settings
from app.mailer.providers import (
    ConsoleEmailProvider,
    MemoryEmailProvider,
    OutboundEmail,
    PermanentEmailError,
    ResendEmailProvider,
    SmtpEmailProvider,
    build_mime,
    build_provider,
)
from app.mailer.render import TEMPLATES, EmailRenderer, reflect
from app.util import iso
from tests.conftest import (
    BASE_URL,
    CANARY_ACCOUNT,
    START,
    ads_payload,
    db_one,
    db_rows,
    leads_payload,
    post_lead,
)

SCRIPT = "<script>alert(1)</script>"
ATTRIBUTE_BREAK = '" onmouseover="alert(1)'
MARKETING_TEMPLATES = ("intro", "repeat_request", "followup")
LINE_BREAKS = "\r\n\x0b\x0c\x1c\x1d\x1e\x85  "


# --- fixtures and helpers --------------------------------------------------------------


@pytest.fixture
def renderer() -> EmailRenderer:
    return EmailRenderer(BASE_URL)


def stored_lead_and_tenant(client, ctx, payload: dict) -> tuple[dict, dict]:
    """Run a payload through the real pipeline and return the stored lead + its tenant."""
    response = post_lead(client, payload)
    assert response.json()["result"] == "processed", response.text
    lead = db_one(ctx, "SELECT * FROM leads WHERE id = ?", (response.json()["leadId"],))
    tenant = db_one(ctx, "SELECT * FROM tenants WHERE id = ?", (lead["tenant_id"],))
    return lead, tenant


@pytest.fixture
def full(client, ctx) -> tuple[dict, dict]:
    """A fully populated lead (name, task, city, interview answers) for a tenant with a phone."""
    lead, tenant = stored_lead_and_tenant(client, ctx, leads_payload(email="jane@example.com"))
    assert lead["first_name"] == "Jane" and lead["task_name"] and lead["city"] and lead["interview_json"]
    assert tenant["phone"]
    return lead, tenant


@pytest.fixture
def sparse(client, ctx) -> tuple[dict, dict]:
    """No first name, no task, no city, no interview; the tenant has no phone."""
    payload = ads_payload(
        ALAccountId=CANARY_ACCOUNT, Email="someone@example.com", FirstName="", LastName="",
        Category="", Description="", Urgency="", PostalAddress={},
    )
    lead, tenant = stored_lead_and_tenant(client, ctx, payload)
    for column in ("first_name", "full_name", "task_name", "city", "interview_json", "address_line1"):
        assert lead[column] is None, column
    assert tenant["phone"] is None
    return lead, tenant


def appointment_for(tenant: dict, index: int = 0) -> dict:
    """An appointments row for one of the tenant's open slots (only the columns emails read)."""
    slot = generate_slots(tenant, START)[index]
    return {"starts_at": slot.key, "ends_at": iso(slot.ends_at), "timezone": tenant["timezone"]}


def make_email(**overrides) -> OutboundEmail:
    fields = {
        "to": "pat@example.com",
        "subject": "Your House Cleaning request: pick a time with Sparkle Home Cleaning",
        "text": "Hi Pat,\n\nPick a time: https://leads.example.test/book/abc\n",
        "html": "<p>Hi Pat,</p><p><a href=\"https://leads.example.test/book/abc\">Pick a time</a></p>\n",
        "from_name": "Sparkle Home Cleaning",
        "from_address": "leads@example.test",
    }
    fields.update(overrides)
    return OutboundEmail(**fields)


def on_the_wire(email: OutboundEmail) -> EmailMessage:
    """Serialise the message the way a provider sends it, then parse those bytes back."""
    return message_from_bytes(bytes(build_mime(email)), policy=policy.default)


def without_urls(body: str) -> str:
    """The body minus its links (their random tokens could spell anything)."""
    return re.sub(r"https://[^\s\"<]+", "", body)


def assert_clean_text(text: str) -> None:
    assert "{{" not in text and "}}" not in text and "{%" not in text and "%}" not in text
    assert "None" not in without_urls(text)
    assert "\n\n\n" not in text, "more than one blank line in a row"
    assert text == text.strip() + "\n", "leading blank lines or stray trailing whitespace"
    assert all(line == line.rstrip() for line in text.splitlines()), "a line ends in whitespace"


# --- every template, full and sparse ---------------------------------------------------


@pytest.mark.parametrize("template", TEMPLATES)
@pytest.mark.parametrize("which", ["full", "sparse"])
def test_every_template_renders_for_full_and_sparse_leads(request, renderer, template, which):
    # Rule: no lead, however incomplete, may break an email (templates use StrictUndefined).
    lead, tenant = request.getfixturevalue(which)
    slots = generate_slots(tenant, START)
    appointment = appointment_for(tenant) if template == "booking_confirmation" else None

    email = renderer.render(template, lead=lead, tenant=tenant, slots=slots, appointment=appointment)

    booking_url = f"{BASE_URL}/book/{lead['booking_token']}"
    unsubscribe_url = f"{BASE_URL}/unsubscribe/{lead['booking_token']}"
    assert booking_url in email.text
    assert f'href="{booking_url}"' in email.html
    if template == "booking_confirmation":
        assert unsubscribe_url not in email.text and unsubscribe_url not in email.html
    else:
        assert unsubscribe_url in email.text
        assert f'href="{unsubscribe_url}"' in email.html
    assert email.text.startswith("Hi Jane,\n" if which == "full" else "Hi there,\n")
    assert ("Hi Jane," if which == "full" else "Hi there,") in email.html
    assert_clean_text(email.text)
    assert "{{" not in email.html and "{%" not in email.html and "None" not in without_urls(email.html)
    assert tenant["name"] in email.text
    assert email.subject and email.subject == email.subject.strip()
    assert not any(ch in email.subject for ch in LINE_BREAKS)


@pytest.mark.parametrize("template", TEMPLATES)
def test_templates_render_from_nothing_but_a_token_and_a_tenant_name(renderer, template):
    # Rule: the renderer needs only the booking token; every other lead field is optional.
    email = renderer.render(template, lead={"booking_token": "tok123"}, tenant={"name": "Acme Plumbing"})

    assert f"{BASE_URL}/book/tok123" in email.text
    assert email.text.startswith("Hi there,\n")
    assert_clean_text(email.text)
    assert "Next available" not in email.text and "Next available" not in email.html  # no slots given
    assert "What we have from your request" not in email.text


def test_unknown_template_name_is_rejected(renderer):
    # Rule: a typo in a template name is an error, never a blank email.
    with pytest.raises(ValueError, match="unknown email template"):
        renderer.render("welcome", lead={"booking_token": "tok"}, tenant={"name": "Acme"})


def test_base_url_trailing_slash_does_not_double_up_in_links():
    # Rule: PUBLIC_BASE_URL may be configured with or without a trailing slash.
    email = EmailRenderer(BASE_URL + "/").render("intro", lead={"booking_token": "tok"}, tenant={"name": "Acme"})

    assert f"{BASE_URL}/book/tok" in email.text
    assert "//book" not in email.text and "//unsubscribe" not in email.text


@pytest.mark.parametrize(
    ("lead_fields", "greeting"),
    [
        ({"first_name": "Pat", "full_name": "Pat Lee"}, "Hi Pat,"),
        ({"first_name": None, "full_name": "Jane Q. Doe"}, "Hi Jane,"),
        ({"first_name": None, "full_name": None}, "Hi there,"),
        ({"first_name": "   ", "full_name": ""}, "Hi there,"),
        ({"first_name": "Mary\r\nAnn", "full_name": None}, "Hi Mary Ann,"),
    ],
)
def test_greeting_uses_the_first_name_and_falls_back_to_there(renderer, lead_fields, greeting):
    # Rule: greet by first name when we have one, otherwise "Hi there,".
    lead = {"booking_token": "tok", **lead_fields}

    email = renderer.render("intro", lead=lead, tenant={"name": "Acme"})

    assert email.text.splitlines()[0] == greeting
    assert f">{greeting}</p>" in email.html


def test_greeting_does_not_use_the_last_name_when_there_is_no_first_name(client, ctx, mailer):
    # Rule: no first name -> "Hi there,", even when Angi did send a last name.
    post_lead(client, ads_payload(Email="pat@example.com", FirstName="", LastName="Builder"))

    ctx.worker.deliver_due()

    assert mailer.sent[0].text.splitlines()[0] == "Hi there,"


def test_subjects_name_the_task_and_the_tenant(renderer, full):
    # Rule: the subject says what the email is about and who it is from.
    lead, tenant = full
    appointment = appointment_for(tenant)

    def subject(template: str, **kwargs) -> str:
        return renderer.render(template, lead=lead, tenant=tenant, **kwargs).subject

    assert subject("intro") == "Your Central A/C - Repair request: pick a time with Summit Heating & Air"
    assert subject("repeat_request") == "We got your new request - Summit Heating & Air"
    assert subject("followup") == "Still need help with your Central A/C - Repair? - Summit Heating & Air"
    assert subject("booking_confirmation", appointment=appointment) == (
        "You're booked: Thu, Nov 12, 10:00 AM - 12:00 PM - Summit Heating & Air"
    )
    assert subject("booking_confirmation") == "You're booked: appointment confirmed - Summit Heating & Air"


def test_subjects_without_a_task_use_generic_wording(renderer, sparse):
    # Rule: a missing task never leaves a hole ("Your  request") in the subject.
    lead, tenant = sparse

    assert renderer.render("intro", lead=lead, tenant=tenant).subject == (
        "Your service request: pick a time with Synthetic Canary Co."
    )
    assert renderer.render("followup", lead=lead, tenant=tenant).subject == (
        "Still need help with your project? - Synthetic Canary Co."
    )


def test_booking_confirmation_states_when_what_and_where(renderer, full):
    # Rule: the confirmation repeats the arrival window in the tenant's local time, the job and the address.
    lead, tenant = full

    email = renderer.render("booking_confirmation", lead=lead, tenant=tenant, appointment=appointment_for(tenant, 1))

    assert "  When:  Thu, Nov 12, 12:00 - 2:00 PM\n" in email.text
    assert "  What:  Central A/C - Repair\n" in email.text
    assert "  Where: 123 Main Street, Denver, CO\n" in email.text
    assert "call us at (303) 555-0117" in email.text
    assert "Thu, Nov 12, 12:00 - 2:00 PM" in email.html
    assert 'href="tel:+13035550117"' in email.html


def test_tenant_without_a_phone_gets_no_call_us_wording(renderer, sparse):
    # Rule: never print "call us at" with nothing after it.
    lead, tenant = sparse

    for template in TEMPLATES:
        email = renderer.render(template, lead=lead, tenant=tenant)
        assert "call us" not in email.text, template
        assert "call us" not in email.html and "tel:" not in email.html, template


# --- slots and request details ---------------------------------------------------------


@pytest.mark.parametrize("template", MARKETING_TEMPLATES)
def test_at_most_three_slots_are_listed_and_each_links_to_its_own_time(renderer, full, template):
    # Rule: the email offers the next three windows; each link preselects that window.
    lead, tenant = full
    slots = generate_slots(tenant, START)
    assert len(slots) > 3

    email = renderer.render(template, lead=lead, tenant=tenant, slots=slots)

    booking_url = f"{BASE_URL}/book/{lead['booking_token']}"
    labels = {slot.label for slot in slots}
    listed = [line[4:] for line in email.text.splitlines() if line.startswith("  - ") and line[4:] in labels]
    assert listed == [slot.label for slot in slots[:3]]
    links = re.findall(r'href="([^"]*\?slot=[^"]*)"', email.html)
    assert links == [f"{booking_url}?slot={quote(slot.key)}" for slot in slots[:3]]
    assert all("%3A" in link and ":" not in link.split("?slot=")[1] for link in links)
    assert slots[3].label not in email.text and slots[3].label not in email.html


def test_slot_link_in_the_email_preselects_that_slot_on_the_booking_page(client, ctx, mailer):
    # Rule: one click in the email lands on the booking page with that window already chosen.
    post_lead(client, ads_payload(Email="pat@example.com"))
    ctx.worker.deliver_due()
    links = re.findall(r'href="([^"]*\?slot=[^"]*)"', mailer.sent[0].html)
    assert len(links) == 3

    page = client.get(links[1].removeprefix(BASE_URL))

    assert page.status_code == 200
    checked = re.findall(r'name="slot" value="([^"]+)" required checked', page.text)
    assert len(checked) == 1
    assert links[1].endswith(quote(checked[0]))


def test_no_slot_section_when_the_tenant_has_no_open_times(renderer, full):
    # Rule: an empty calendar must not print an empty "Next available" heading.
    lead, tenant = full

    email = renderer.render("intro", lead=lead, tenant=tenant, slots=[])

    assert "Next available" not in email.text and "Next available" not in email.html
    assert "?slot=" not in email.html
    assert_clean_text(email.text)


def test_interview_answers_are_capped_at_three(client, ctx, renderer):
    # Rule: play back a few of the consumer's own answers, not the whole questionnaire.
    interview = [{"question": f"Question {n}?", "answer": f"Answer {n}"} for n in range(1, 6)]
    lead, tenant = stored_lead_and_tenant(client, ctx, leads_payload(email="jane@example.com", interview=interview))

    email = renderer.render("intro", lead=lead, tenant=tenant)

    for n in (1, 2, 3):
        assert f"  - Question {n}: Answer {n}\n" in email.text
        assert f">Answer {n}</td>" in email.html
    for n in (4, 5):
        assert f"Answer {n}" not in email.text and f"Answer {n}" not in email.html


def test_request_details_play_back_service_location_and_timing(client, ctx, mailer):
    # Rule: the intro shows the consumer what we understood of their request.
    post_lead(client, ads_payload(Email="pat@example.com"))

    ctx.worker.deliver_due()

    text = mailer.sent[0].text
    assert "What we have from your request:\n" in text
    assert "  - Service: House Cleaning\n" in text
    assert "  - Location: Indianapolis, IN 46203\n" in text
    assert "  - Timing: This Week\n" in text


# --- untrusted lead data ---------------------------------------------------------------


@pytest.fixture
def hostile(client, ctx) -> tuple[dict, dict]:
    """A lead whose every free-text field tries to inject markup."""
    payload = leads_payload(
        email="mallory@example.com",
        firstName=SCRIPT,
        lastName=ATTRIBUTE_BREAK,
        name=f"{SCRIPT} {ATTRIBUTE_BREAK}",
        taskName=f"Roof {ATTRIBUTE_BREAK} {SCRIPT}",
        city=f"{SCRIPT}{ATTRIBUTE_BREAK}",
        address=f"1 Main St {SCRIPT}{ATTRIBUTE_BREAK}",
        comments=f"{SCRIPT} {ATTRIBUTE_BREAK}",
        interview=[
            {"question": f"{SCRIPT}{ATTRIBUTE_BREAK}?", "answer": f"{ATTRIBUTE_BREAK}{SCRIPT}"},
            {"question": "Anything else?", "answer": SCRIPT},
        ],
    )
    return stored_lead_and_tenant(client, ctx, payload)


@pytest.mark.parametrize("template", TEMPLATES)
def test_html_part_escapes_hostile_lead_data(renderer, hostile, template):
    # Rule: lead data is untrusted; it must never become markup in the HTML email.
    lead, tenant = hostile
    assert lead["first_name"] == SCRIPT  # stored verbatim; escaping happens at render time

    email = renderer.render(
        template, lead=lead, tenant=tenant, slots=generate_slots(tenant, START), appointment=appointment_for(tenant)
    )

    assert "<script" not in email.html.lower()
    assert '" onmouseover=' not in email.html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in email.html
    assert "&#34; onmouseover=&#34;alert(1)" in email.html
    # The tenant's own name is escaped as well.
    assert "Summit Heating &amp; Air" in email.html and "Heating & Air" not in email.html
    # Plain text is not HTML: the same strings appear as typed.
    assert SCRIPT in email.text


def test_hostile_lead_is_escaped_in_the_email_actually_sent(client, ctx, mailer, hostile):
    # Rule: same guarantee end to end, through the worker (intro incl. the request-details table).
    ctx.worker.deliver_due()

    (email,) = mailer.sent
    assert "<script" not in email.html.lower()
    assert '" onmouseover=' not in email.html
    assert email.html.count("&lt;script&gt;alert(1)&lt;/script&gt;") >= 4  # name, task, city, interview
    assert "&#34; onmouseover=" in email.html


@pytest.mark.parametrize("template", TEMPLATES)
def test_line_breaks_in_task_or_tenant_name_never_reach_the_subject(renderer, full, template):
    # Rule: anything that goes into a header is collapsed to one line (no header injection).
    lead, tenant = full
    lead = {**lead, "task_name": "Roof\r\nBcc: attacker@example.com\tRepair\n X"}
    tenant = {**tenant, "name": "Acme\r\nBcc: attacker@example.com\tPlumbing\x0b"}

    email = renderer.render(template, lead=lead, tenant=tenant, appointment=appointment_for(tenant))

    assert not any(ch in email.subject for ch in LINE_BREAKS + "\t")
    assert len(email.subject) <= 180
    parsed = on_the_wire(make_email(subject=email.subject, text=email.text, html=email.html))
    assert parsed["Bcc"] is None
    assert str(parsed["Subject"]) == email.subject


def test_very_long_task_name_is_truncated_in_the_subject(renderer, full):
    # Rule: subjects stay a sane length whatever Angi sends.
    lead, tenant = full

    email = renderer.render("intro", lead={**lead, "task_name": "Gutter cleaning " * 40}, tenant=tenant)

    assert len(email.subject) <= 180
    assert email.subject.endswith("pick a time with Summit Heating & Air")


def test_header_injection_through_the_webhook_payload_adds_no_headers(client, ctx, mailer):
    # Rule: CR/LF smuggled in through Angi's payload cannot add a Bcc (or any header) to the email.
    post_lead(
        client,
        ads_payload(
            Email="pat@example.com",
            Category="Roofing\r\nBcc: attacker@example.com\r\n\r\nInjected body",
            FirstName="Pat\r\nBcc: attacker@example.com",
        ),
    )
    ctx.worker.deliver_due()
    (email,) = mailer.sent

    parsed = on_the_wire(email)  # build_mime succeeds

    assert not any(ch in email.subject for ch in LINE_BREAKS)
    assert parsed["Bcc"] is None and parsed["Cc"] is None
    assert parsed["To"] == "pat@example.com"
    assert str(parsed["Subject"]) == email.subject
    assert "Injected body" in str(parsed["Subject"])  # kept, but only as subject text
    assert parsed.get_content_type() == "multipart/alternative"


def test_tenant_name_with_a_line_break_still_produces_a_sendable_message(client, ctx, mailer):
    # Rule: the tenant name is collapsed to one line in the From header just as it is in the subject.
    with ctx.db.transaction() as conn:
        conn.execute("UPDATE tenants SET name = ? WHERE id = 'tnt_sparkle'", ("Sparkle Home\r\nCleaning",))
    post_lead(client, ads_payload(Email="pat@example.com"))
    ctx.worker.deliver_due()
    (email,) = mailer.sent
    assert not any(ch in email.subject for ch in LINE_BREAKS)  # the subject is already safe

    parsed = on_the_wire(email)  # what the SMTP and console providers have to build

    sender = parsed["From"].addresses[0]
    assert sender.addr_spec == "leads@example.test"
    assert "Sparkle Home" in sender.display_name and "Cleaning" in sender.display_name
    assert not any(ch in sender.display_name for ch in LINE_BREAKS)


# --- consumer text played back in our email ---------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "Home/Residence", "Within 1 week", "St. Louis", "approx. 1500 sq. ft.", "No. 5", "2.5 baths", "3.5in pipe",
        "Central A/C - Repair", "Indianapolis – House Cleaning", "9 a.m. - 5 p.m.", "Call me @ 5pm", "$1,200.50",
        "Jean-Luc", "O'Brien", "José", "J.", "李",
    ],
)
def test_ordinary_request_text_is_played_back_unchanged(value):
    # Rule: stripping links must not eat the things consumers normally write.
    assert reflect(value) == value


@pytest.mark.parametrize(
    ("value", "kept"),
    [
        ("Your card was declined. Update it at https://evil.example/pay within 24 hours",
         "Your card was declined. Update it at within 24 hours"),
        ("Refund due: evil.example/claim now", "Refund due: now"),
        ("see www.evil.example today", "see today"),
        ("go to Evil.Com:8080/x ok", "go to ok"),
        ("write to victim@example.com please", "write to please"),
        ("HTTP://EVIL.EXAMPLE", ""),
        ("bit.ly/abc", ""),
        ("ftp://files.evil.example/a.exe", ""),
        ("evil\x00.com", ""),  # a control character must not hide the dot from the check
        (None, ""),
        ("", ""),
    ],
)
def test_anything_that_looks_like_a_link_or_an_address_is_removed(value, kept):
    # Rule: text a stranger typed into Angi must not carry a link in an email the business signs.
    assert reflect(value) == kept


def test_reflected_text_is_capped_and_kept_on_one_line():
    # Rule: a consumer cannot use the summary to deliver an essay (or a second paragraph).
    assert reflect("word " * 200) == ("word " * 24).strip()
    assert len(reflect("x" * 5000)) == 120
    assert reflect("first line\r\nsecond line") == "first line second line"
    assert reflect("abcdefghij", 4) == "abcd"


def test_links_in_a_request_never_reach_the_email(client, ctx, mailer):
    # Rule: someone files a request under a victim's address; our email to the victim must
    # not carry the attacker's link, in the greeting, the subject, the first sentence or the summary.
    payload = leads_payload(
        email="victim@example.com",
        firstName="Refund due: evil.example/claim now",
        name="Refund due: evil.example/claim now Doe",
        taskName="Roof repair - details at http://evil.example/roof",
        city="evil.example/city",
        interview=[
            {"question": "Security notice", "answer": "Your card was declined. Update it at https://evil.example/pay within 24 hours"},
            {"question": "Contact", "answer": "billing@evil.example"},
            {"question": "See www.evil.example?", "answer": "Yes"},
        ],
    )
    assert post_lead(client, payload).json()["result"] == "processed"

    ctx.worker.deliver_due()

    (email,) = mailer.sent
    assert email.to == "victim@example.com"
    for part in (email.subject, email.text, email.html):
        assert "evil" not in part.lower()
    assert email.subject == "Your Roof repair - details at request: pick a time with Summit Heating & Air"
    assert email.text.splitlines()[0] == "Hi Refund due: now,"
    assert "Thanks for your request on Angi - Roof repair - details at. This is Summit Heating & Air" in email.text
    assert "  - Security notice: Your card was declined. Update it at within 24 hours\n" in email.text
    assert "  - See: Yes\n" in email.text
    # A value with nothing left is not listed at all.
    assert "Contact" not in email.text and "Contact" not in email.html
    # Our own links are the only ones in the email.
    assert set(re.findall(r"https?://[^/\s\"<]+", email.text + email.html)) == {BASE_URL}


def test_a_detail_that_is_nothing_but_a_link_is_left_out(renderer):
    # Rule: no empty "Timing:" line, and a task that was only a link falls back to the generic wording.
    lead = {
        "booking_token": "tok",
        "task_name": "https://evil.example/task",
        "city": "www.evil.example",
        "state": "CO",
        "postal_code": "80210",
        "urgency": "evil.example/now",
        "interview_json": '[{"question": "https://evil.example/q", "answer": "Home/Residence"}]',
    }

    email = renderer.render("intro", lead=lead, tenant={"name": "Acme"})

    assert email.subject == "Your service request: pick a time with Acme"
    assert "Thanks for your request on Angi. This is Acme" in email.text
    details = email.text.split("What we have from your request:\n")[1].split("\n\n")[0]
    assert details == "  - Location: CO 80210"
    assert "evil" not in email.text and "evil" not in email.html
    assert_clean_text(email.text)


def test_long_interview_answers_are_cut_short(client, ctx, renderer):
    # Rule: each played-back value is capped, so the summary stays a summary.
    interview = [{"question": "Anything else?", "answer": "very " * 100 + "long"}]
    lead, tenant = stored_lead_and_tenant(client, ctx, leads_payload(email="jane@example.com", interview=interview))

    email = renderer.render("intro", lead=lead, tenant=tenant)

    line = next(line for line in email.text.splitlines() if line.startswith("  - Anything else: "))
    assert line == "  - Anything else: " + ("very " * 24).strip()


# --- build_mime ------------------------------------------------------------------------


def test_build_mime_is_multipart_alternative_with_text_then_html():
    # Rule: mail clients show the last alternative they understand, so plain text comes first.
    email = make_email()

    parsed = on_the_wire(email)

    assert parsed.get_content_type() == "multipart/alternative"
    parts = list(parsed.iter_parts())
    assert [part.get_content_type() for part in parts] == ["text/plain", "text/html"]
    assert parts[0].get_content() == email.text
    assert parts[1].get_content() == email.html
    assert parsed["MIME-Version"] == "1.0"


def test_build_mime_without_html_is_a_plain_text_message():
    # Rule: alert emails have no HTML part and must still be valid.
    parsed = on_the_wire(make_email(html=None))

    assert parsed.get_content_type() == "text/plain"
    assert not parsed.is_multipart()
    assert parsed.get_content() == make_email().text


def test_build_mime_sets_the_headers_mailbox_providers_expect():
    # Rule: From shows the business name; To, Subject, Date and a Message-ID are always present.
    email = make_email()

    parsed = on_the_wire(email)

    sender = parsed["From"].addresses[0]
    assert (sender.display_name, sender.addr_spec) == ("Sparkle Home Cleaning", "leads@example.test")
    assert [address.addr_spec for address in parsed["To"].addresses] == ["pat@example.com"]
    assert str(parsed["Subject"]) == email.subject
    assert parsed["Date"].datetime.utcoffset().total_seconds() == 0
    assert re.fullmatch(r"<[^<>@\s]+@example\.test>", str(parsed["Message-ID"]))
    assert parsed["Reply-To"] is None
    assert parsed["Bcc"] is None and parsed["Cc"] is None


def test_build_mime_gives_every_message_its_own_message_id():
    # Rule: Message-IDs must be unique or mail clients thread unrelated emails together.
    ids = {str(build_mime(make_email())["Message-ID"]) for _ in range(20)}

    assert len(ids) == 20


def test_build_mime_adds_reply_to_and_passes_extra_headers_through():
    # Rule: replies go where the tenant wants them; unsubscribe headers arrive untouched.
    email = make_email(
        reply_to="office@sparkle.example",
        headers={
            "List-Unsubscribe": "<https://leads.example.test/unsubscribe/abc>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        },
    )

    parsed = on_the_wire(email)

    assert parsed["Reply-To"] == "office@sparkle.example"
    assert parsed["List-Unsubscribe"] == "<https://leads.example.test/unsubscribe/abc>"
    assert parsed["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"


def test_long_list_unsubscribe_url_is_written_literally_not_as_an_encoded_word():
    # Rule: List-Unsubscribe must contain the URL in angle brackets exactly (RFC 2369 / RFC 8058).
    url = "https://unduly-prime-mongoose.ngrok-free.app/unsubscribe/q1w2e3r4t5y6u7i8o9p0a1s2"
    email = make_email(
        headers={"List-Unsubscribe": f"<{url}>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}
    )

    # compat32 hands back the header exactly as written on the wire (no decoding).
    raw = message_from_bytes(bytes(build_mime(email)), policy=policy.compat32)["List-Unsubscribe"]
    unfolded = re.sub(r"\r?\n[ \t]+", "", raw)

    assert "=?" not in unfolded
    assert unfolded.strip() == f"<{url}>"


def test_build_mime_round_trips_non_ascii_subject_names_and_bodies():
    # Rule: accents and non-Latin scripts survive encoding (RFC 2047 headers, UTF-8 bodies).
    email = make_email(
        subject="Tu cita está confirmada — ¡gracias, José! 日本語のテスト",
        from_name="Café Niño & Söhne Plomería",
        text="Hola José,\n\nNos vemos mañana — 8:00.\n",
        html="<p>Hola José — nos vemos mañana ✓</p>\n",
    )

    wire = bytes(build_mime(email))
    parsed = message_from_bytes(wire, policy=policy.default)

    header_block = wire.split(b"\n\n", 1)[0]
    assert header_block.isascii()  # headers are encoded, never raw 8-bit
    assert str(parsed["Subject"]) == email.subject
    assert parsed["From"].addresses[0].display_name == email.from_name
    assert parsed["From"].addresses[0].addr_spec == "leads@example.test"
    text_part, html_part = parsed.iter_parts()
    assert text_part.get_content() == email.text
    assert html_part.get_content() == email.html
    assert text_part.get_content_charset() == "utf-8"


@pytest.mark.parametrize(
    "from_name",
    ['Smith, Jones & "Bob" Co.', "O'Brien <Heating>", "A.B. Plumbing (Denver)", "Semi;colon: Inc."],
)
def test_build_mime_quotes_special_characters_in_the_from_name(from_name):
    # Rule: commas, quotes and brackets in a business name must not split or forge the From address.
    parsed = on_the_wire(make_email(from_name=from_name))

    assert len(parsed["From"].addresses) == 1
    assert parsed["From"].addresses[0].display_name == from_name
    assert parsed["From"].addresses[0].addr_spec == "leads@example.test"


def test_build_mime_round_trips_long_subjects_and_very_long_body_lines():
    # Rule: folding and transfer encoding are lossless (SMTP limits lines to 998 characters).
    email = make_email(
        subject=("Still need help with your gutter cleaning and repair? " * 4)[:180].strip(),
        text="x" * 2500 + "\nshort line\n",
        html="<p>" + "y" * 5000 + "</p>\n",
    )

    wire = bytes(build_mime(email))
    parsed = message_from_bytes(wire, policy=policy.default)

    assert max(len(line) for line in wire.splitlines()) <= 998
    assert str(parsed["Subject"]) == email.subject
    text_part, html_part = parsed.iter_parts()
    assert text_part.get_content() == email.text
    assert html_part.get_content() == email.html


def test_build_mime_refuses_a_line_break_in_a_header_value():
    # Rule: the MIME layer itself is the last line of defence against header injection.
    with pytest.raises(ValueError):
        build_mime(make_email(subject="Hello\r\nBcc: attacker@example.com"))


# --- providers ---------------------------------------------------------------------------


def test_console_provider_writes_a_parseable_eml_file(tmp_path):
    # Rule: in development nothing is sent; each email lands on disk where it can be opened.
    out_dir = tmp_path / "nested" / "outbox"  # created on demand
    provider = ConsoleEmailProvider(str(out_dir))
    email = make_email(reply_to="office@sparkle.example", headers={"List-Unsubscribe-Post": "List-Unsubscribe=One-Click"})

    result = provider.send(email)

    files = list(out_dir.iterdir())
    assert len(files) == 1 and files[0].suffix == ".eml"
    assert "pat_example_com" in files[0].name
    parsed = message_from_bytes(files[0].read_bytes(), policy=policy.default)
    assert parsed["To"] == "pat@example.com"
    assert str(parsed["Subject"]) == email.subject
    assert parsed["Reply-To"] == "office@sparkle.example"
    assert parsed["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert [part.get_content_type() for part in parsed.iter_parts()] == ["text/plain", "text/html"]
    assert parsed.get_body(preferencelist=("plain",)).get_content() == email.text
    assert result.provider == "console"
    assert result.message_id == str(parsed["Message-ID"])


def test_console_provider_without_a_directory_only_logs(tmp_path, monkeypatch):
    # Rule: file output is optional; sending still "succeeds" without it.
    monkeypatch.chdir(tmp_path)

    result = ConsoleEmailProvider(None).send(make_email())

    assert result.provider == "console" and result.message_id
    assert list(tmp_path.iterdir()) == []


def test_console_provider_file_name_is_safe_for_a_hostile_address(tmp_path):
    # Rule: the recipient address never becomes a path (no "../" escapes from the outbox folder).
    provider = ConsoleEmailProvider(str(tmp_path))

    provider.send(make_email(to="../../etc/passwd@example.com"))

    (written,) = tmp_path.iterdir()
    assert written.parent == tmp_path
    assert "/" not in written.name and ".." not in written.name


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("console", ConsoleEmailProvider),
        ("smtp", SmtpEmailProvider),
        ("resend", ResendEmailProvider),
        ("SMTP", SmtpEmailProvider),
        ("Resend", ResendEmailProvider),
    ],
)
def test_build_provider_picks_the_configured_provider(name, expected):
    # Rule: EMAIL_PROVIDER selects the transport (case-insensitively).
    provider = build_provider(Settings(_env_file=None, email_provider=name))

    assert isinstance(provider, expected)
    assert provider.name == name.lower()


@pytest.mark.parametrize("name", ["sendgrid", "", "smtp2"])
def test_build_provider_rejects_an_unknown_provider(name):
    # Rule: a typo in EMAIL_PROVIDER stops the app at start-up instead of silently dropping email.
    with pytest.raises(ValueError, match="EMAIL_PROVIDER"):
        build_provider(Settings(_env_file=None, email_provider=name))


def test_only_providers_that_really_send_say_so():
    # Rule: the worker keeps example.com addresses away from providers that deliver for real.
    settings = Settings(_env_file=None)

    assert SmtpEmailProvider(settings).delivers_externally is True
    assert ResendEmailProvider(settings).delivers_externally is True
    assert not getattr(ConsoleEmailProvider(None), "delivers_externally", False)
    assert not getattr(MemoryEmailProvider(), "delivers_externally", False)


def test_memory_provider_fail_times_semantics():
    # Rule (test double): positive = fail that many sends then succeed; -1 = forever; 0 = never.
    provider = MemoryEmailProvider()
    provider.fail_with = RuntimeError("down")

    provider.fail_times = 0
    assert provider.send(make_email()).provider == "memory"

    provider.fail_times = 2
    for _ in range(2):
        with pytest.raises(RuntimeError):
            provider.send(make_email())
    assert provider.send(make_email()).message_id == "<memory-2@test>"

    provider.fail_times = -1
    for _ in range(3):
        with pytest.raises(RuntimeError):
            provider.send(make_email())
    assert len(provider.sent) == 2


# --- SMTP (smtplib replaced by a fake) -------------------------------------------------


class FakeSmtpServer:
    """Stands in for smtplib.SMTP / smtplib.SMTP_SSL and records what the provider did."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.connect_kwargs: dict = {}
        self.starttls_context = None
        self.delivered: list[EmailMessage] = []
        self.connect_error: Exception | None = None
        self.login_error: Exception | None = None
        self.send_error: Exception | None = None  # raised by every send_message
        self.send_errors: list[Exception] = []  # raised once each, in order, before send_error applies
        self.noop_error: Exception | None = None
        self.quit_error: Exception | None = None
        self.refused: dict = {}

    def connection_class(self, kind: str) -> type:
        server = self

        class Connection:
            def __init__(self, host, port, timeout=None, context=None):
                server.calls.append((kind, host, port))
                server.connect_kwargs = {"timeout": timeout, "context": context}
                if server.connect_error is not None:
                    raise server.connect_error

            def noop(self):
                server.calls.append(("noop",))
                if server.noop_error is not None:
                    raise server.noop_error
                return (250, b"2.0.0 OK")

            def quit(self):
                server.calls.append(("quit",))
                if server.quit_error is not None:
                    raise server.quit_error

            def close(self):
                server.calls.append(("close",))

            def starttls(self, context=None):
                server.calls.append(("starttls",))
                server.starttls_context = context

            def login(self, username, password):
                server.calls.append(("login", username, password))
                if server.login_error is not None:
                    raise server.login_error

            def send_message(self, msg):
                server.calls.append(("send_message",))
                if server.send_errors:
                    raise server.send_errors.pop(0)
                if server.send_error is not None:
                    raise server.send_error
                server.delivered.append(msg)
                return server.refused

        return Connection


@pytest.fixture
def smtp_server(monkeypatch) -> FakeSmtpServer:
    server = FakeSmtpServer()
    monkeypatch.setattr(smtplib, "SMTP_SSL", server.connection_class("SMTP_SSL"))
    monkeypatch.setattr(smtplib, "SMTP", server.connection_class("SMTP"))
    return server


def smtp_provider(**overrides) -> SmtpEmailProvider:
    fields = {
        "smtp_host": "smtp.test",
        "smtp_port": 465,
        "smtp_security": "ssl",
        "smtp_username": "mailer@example.test",
        "smtp_password": "app-password",
        "smtp_timeout_seconds": 15.0,
    }
    fields.update(overrides)
    return SmtpEmailProvider(Settings(_env_file=None, **fields))


@pytest.mark.parametrize("security", ["ssl", "SSL"])
def test_smtp_ssl_connects_with_implicit_tls_and_never_calls_starttls(smtp_server, security):
    # Rule: port 465 style: TLS from the first byte, verified against a CA bundle.
    result = smtp_provider(smtp_security=security).send(make_email())

    assert smtp_server.calls == [
        ("SMTP_SSL", "smtp.test", 465),
        ("login", "mailer@example.test", "app-password"),
        ("send_message",),
    ]  # and no quit: the connection is kept for the next lead
    assert isinstance(smtp_server.connect_kwargs["context"], ssl.SSLContext)
    assert smtp_server.connect_kwargs["context"].verify_mode == ssl.CERT_REQUIRED
    assert smtp_server.connect_kwargs["timeout"] == 15.0
    (delivered,) = smtp_server.delivered
    assert delivered["To"] == "pat@example.com"
    assert delivered["Subject"] == make_email().subject
    assert result.provider == "smtp"
    assert result.message_id == str(delivered["Message-ID"])


@pytest.mark.parametrize("security", ["starttls", "StartTLS"])
def test_smtp_starttls_upgrades_the_connection_before_logging_in(smtp_server, security):
    # Rule: port 587 style: the password is only sent after the connection is encrypted.
    smtp_provider(smtp_security=security, smtp_port=587).send(make_email())

    assert smtp_server.calls == [
        ("SMTP", "smtp.test", 587),
        ("starttls",),
        ("login", "mailer@example.test", "app-password"),
        ("send_message",),
    ]
    assert isinstance(smtp_server.starttls_context, ssl.SSLContext)
    assert smtp_server.starttls_context.verify_mode == ssl.CERT_REQUIRED


def test_smtp_security_none_uses_a_plain_connection(smtp_server):
    # Rule: "none" (a local relay) means no TLS at all.
    smtp_provider(smtp_security="none", smtp_port=25).send(make_email())

    assert smtp_server.calls == [
        ("SMTP", "smtp.test", 25),
        ("login", "mailer@example.test", "app-password"),
        ("send_message",),
    ]


def test_smtp_does_not_log_in_without_a_username(smtp_server):
    # Rule: an unauthenticated relay is used as is; login is only attempted with credentials.
    smtp_provider(smtp_username="", smtp_password="").send(make_email())

    assert smtp_server.calls == [("SMTP_SSL", "smtp.test", 465), ("send_message",)]


def test_smtp_recipient_refused_is_a_permanent_error(smtp_server):
    # Rule: the mail server said this mailbox does not exist; retrying cannot help.
    smtp_server.send_error = smtplib.SMTPRecipientsRefused({"pat@example.com": (550, b"5.1.1 no such user")})

    provider = smtp_provider()
    with pytest.raises(PermanentEmailError, match="recipient refused"):
        provider.send(make_email())

    # The server answered, so the connection is fine: the next lead reuses it.
    smtp_server.send_error = None
    provider.send(make_email())
    assert [call[0] for call in smtp_server.calls].count("SMTP_SSL") == 1


@pytest.mark.parametrize(
    "error",
    [
        smtplib.SMTPDataError(550, b"5.4.5 Daily user sending limit exceeded"),
        smtplib.SMTPDataError(550, b"5.7.1 Our system has detected that this message is likely unsolicited mail"),
        smtplib.SMTPDataError(552, b"5.2.3 Your message exceeded Google's message size limits"),
        smtplib.SMTPDataError(554, b"5.7.0 Too many unauthenticated commands"),
        smtplib.SMTPDataError(551, b"rejected"),
        smtplib.SMTPDataError(553, b"rejected"),
        smtplib.SMTPSenderRefused(550, b"5.7.60 not permitted to send as this sender", "leads@example.test"),
        smtplib.SMTPHeloError(554, b"go away"),
    ],
)
def test_smtp_5xx_that_is_not_about_the_recipient_is_retried(smtp_server, error):
    # Rule: only a refused RECIPIENT is permanent. A 5xx after the message body is how Gmail
    # reports its daily sending limit and policy blocks, and a refused sender is our own
    # misconfiguration: retried and alerted on, never one silently failed lead per attempt.
    smtp_server.send_error = error

    with pytest.raises(type(error)) as raised:
        smtp_provider().send(make_email())

    assert raised.value is error
    assert not isinstance(raised.value, PermanentEmailError)


@pytest.mark.parametrize("code", [535, 534, 550])
def test_smtp_authentication_failure_is_not_permanent(smtp_server, code):
    # Rule: bad credentials are our problem and fixable: retry (and alert), do not drop the lead.
    smtp_server.login_error = smtplib.SMTPAuthenticationError(code, b"5.7.8 bad credentials")

    with pytest.raises(smtplib.SMTPAuthenticationError) as raised:
        smtp_provider().send(make_email())

    assert not isinstance(raised.value, PermanentEmailError)
    assert ("send_message",) not in smtp_server.calls


@pytest.mark.parametrize(
    "error",
    [
        smtplib.SMTPDataError(451, b"4.3.0 try again later"),
        smtplib.SMTPDataError(421, b"4.7.0 too many connections"),
        smtplib.SMTPSenderRefused(452, b"4.5.3 too many recipients", "leads@example.test"),
        smtplib.SMTPServerDisconnected("connection unexpectedly closed"),
    ],
)
def test_smtp_temporary_failures_are_not_permanent(smtp_server, error):
    # Rule: 4xx answers and dropped connections clear up on their own; the worker retries them.
    smtp_server.send_error = error

    with pytest.raises(type(error)) as raised:
        smtp_provider().send(make_email())

    assert raised.value is error
    assert not isinstance(raised.value, PermanentEmailError)


def test_smtp_connection_failure_is_not_permanent(smtp_server):
    # Rule: an unreachable mail server is an outage, not a bad address.
    smtp_server.connect_error = ConnectionRefusedError("connection refused")

    with pytest.raises(ConnectionRefusedError):
        smtp_provider().send(make_email())


def test_smtp_partial_refusal_reported_by_send_message_is_a_permanent_error(smtp_server):
    # Rule: smtplib returns (does not raise) per-recipient refusals; that is still "not delivered".
    smtp_server.refused = {"pat@example.com": (550, b"5.1.1 user unknown")}

    with pytest.raises(PermanentEmailError, match="recipient refused"):
        smtp_provider().send(make_email())


def test_smtp_temporary_partial_refusal_reported_by_send_message_is_retried(smtp_server):
    # Rule: the same 4xx / 5xx split applies to refusals that smtplib returns instead of raising.
    smtp_server.refused = {"pat@example.com": (452, b"4.2.2 mailbox full, try again later")}

    with pytest.raises(RuntimeError, match="temporarily refused") as raised:
        smtp_provider().send(make_email())

    assert not isinstance(raised.value, PermanentEmailError)


def test_smtp_error_while_saying_goodbye_does_not_undo_an_accepted_message(smtp_server):
    # Rule: once the server accepted the message it is sent; a failed QUIT must not be
    # reported as a failure, or the worker would send the same email a second time.
    smtp_server.quit_error = smtplib.SMTPResponseException(421, b"4.7.0 closing connection")
    provider = smtp_provider()

    result = provider.send(make_email())
    provider.close()  # the goodbye fails; that must stay invisible

    assert len(smtp_server.delivered) == 1
    assert result.provider == "smtp"
    assert result.message_id == str(smtp_server.delivered[0]["Message-ID"])
    assert smtp_server.calls[-2:] == [("quit",), ("close",)]


def test_smtp_error_before_the_message_was_accepted_is_still_an_error(smtp_server):
    # Rule: ...but only then. Nothing was accepted here, so the attempt failed.
    smtp_server.login_error = smtplib.SMTPAuthenticationError(535, b"5.7.8 bad credentials")
    smtp_server.quit_error = smtplib.SMTPResponseException(421, b"4.7.0 closing connection")

    with pytest.raises(smtplib.SMTPException):
        smtp_provider().send(make_email())

    assert smtp_server.delivered == []


def test_smtp_temporary_recipient_refusal_is_retried_not_treated_as_permanent(smtp_server):
    # Rule: "4xx problems are not permanent" also applies when the 4xx is the answer to RCPT TO.
    smtp_server.send_error = smtplib.SMTPRecipientsRefused(
        {"pat@example.com": (450, b"4.2.0 greylisted, try again in 5 minutes")}
    )

    try:
        smtp_provider().send(make_email())
    except PermanentEmailError:
        pytest.fail("a temporary 4xx refusal was reported as permanent")
    except smtplib.SMTPException:
        pass  # any non-permanent error makes the worker retry
    else:
        pytest.fail("the refusal was swallowed")


def test_smtp_connection_is_kept_open_and_reused_for_the_next_message(smtp_server):
    # Rule: connection set-up (TLS, login, and Gmail's occasional ten-second greeting delay)
    # is paid once, not by every lead. A second send only checks the connection is alive.
    provider = smtp_provider()

    provider.send(make_email())
    provider.send(make_email())

    assert smtp_server.calls == [
        ("SMTP_SSL", "smtp.test", 465),
        ("login", "mailer@example.test", "app-password"),
        ("send_message",),
        ("noop",),
        ("send_message",),
    ]
    assert len(smtp_server.delivered) == 2


def test_smtp_warm_up_opens_the_connection_before_any_lead_needs_it(smtp_server):
    # Rule: the worker calls warm_up at start and every minute, so a lead's email never
    # waits for a handshake.
    provider = smtp_provider()

    provider.warm_up()
    assert smtp_server.calls == [("SMTP_SSL", "smtp.test", 465), ("login", "mailer@example.test", "app-password")]

    provider.warm_up()  # already open: only a liveness check
    provider.send(make_email())
    assert smtp_server.calls[2:] == [("noop",), ("noop",), ("send_message",)]


def test_smtp_connection_dropped_while_idle_is_reopened_without_failing_the_send(smtp_server):
    # Rule: mail servers hang up on idle clients. That is noticed by the liveness check and
    # costs a reconnect, never a failed attempt.
    provider = smtp_provider()
    provider.send(make_email())
    smtp_server.noop_error = smtplib.SMTPServerDisconnected("connection unexpectedly closed")

    provider.send(make_email())

    assert [call[0] for call in smtp_server.calls] == [
        "SMTP_SSL", "login", "send_message",  # first message
        "noop", "quit",  # the old connection turns out to be dead and is dropped
        "SMTP_SSL", "login", "send_message",  # second message on a fresh one
    ]
    assert len(smtp_server.delivered) == 2


@pytest.mark.parametrize(
    "hangup",
    [
        smtplib.SMTPServerDisconnected("connection unexpectedly closed"),
        smtplib.SMTPDataError(421, b"4.7.0 try again later, closing connection"),
    ],
)
def test_smtp_server_hanging_up_during_a_send_gets_one_retry_on_a_fresh_connection(smtp_server, hangup):
    # Rule: if the server closes the kept connection just as we use it, try once more
    # straight away instead of making the lead wait for the retry schedule.
    provider = smtp_provider()
    smtp_server.send_errors = [hangup]

    result = provider.send(make_email())

    assert result.provider == "smtp"
    assert len(smtp_server.delivered) == 1
    assert [call[0] for call in smtp_server.calls].count("SMTP_SSL") == 2


def test_smtp_broken_connection_is_not_reused(smtp_server):
    # Rule: after a timeout the state of the connection is unknown; the next send starts clean.
    provider = smtp_provider()
    smtp_server.send_errors = [TimeoutError("timed out")]

    with pytest.raises(TimeoutError):
        provider.send(make_email())
    provider.send(make_email())

    assert [call[0] for call in smtp_server.calls].count("SMTP_SSL") == 2
    assert ("noop",) not in smtp_server.calls  # nothing was left to check
    assert len(smtp_server.delivered) == 1


def test_smtp_burst_uses_extra_connections_instead_of_queueing_behind_the_standing_one(smtp_server):
    # Rule: one message at a time per connection. While the standing connection is busy,
    # another message gets a connection of its own (closed afterwards) rather than waiting.
    provider = smtp_provider()
    provider.send(make_email())
    assert provider._lock.acquire(blocking=False)  # another thread is mid-send
    try:
        provider.send(make_email())
    finally:
        provider._lock.release()

    assert [call[0] for call in smtp_server.calls] == [
        "SMTP_SSL", "login", "send_message",  # standing connection
        "SMTP_SSL", "login", "send_message", "quit",  # one-off connection for the overlap
    ]


def test_smtp_close_says_goodbye_and_a_later_send_reconnects(smtp_server):
    provider = smtp_provider()
    provider.send(make_email())

    provider.close()
    provider.close()  # nothing open any more: harmless
    provider.send(make_email())

    assert [call[0] for call in smtp_server.calls] == [
        "SMTP_SSL", "login", "send_message", "quit", "SMTP_SSL", "login", "send_message",
    ]


def test_worker_warms_the_mail_connection_and_survives_a_server_that_is_down(ctx, smtp_server, caplog):
    # Rule: keeping the connection warm is an optimisation. If the mail server cannot be
    # reached the worker says so and carries on; sending has its own retries and alerts.
    ctx.worker.email_provider = smtp_provider()

    ctx.worker._warm_up_mail()
    assert [call[0] for call in smtp_server.calls] == ["SMTP_SSL", "login"]

    ctx.worker.email_provider.close()
    smtp_server.connect_error = ConnectionRefusedError("connection refused")
    with caplog.at_level("WARNING", logger="app.worker"):
        ctx.worker._warm_up_mail()  # must not raise
    assert "could not open the mail connection ahead of time" in caplog.text


def test_smtp_unknown_security_mode_is_rejected_instead_of_sending_in_the_clear(smtp_server):
    # Rule: only ssl | starttls | none are valid; a typo must not downgrade to plain text.
    with pytest.raises(ValueError):
        smtp_provider(smtp_security="tls", smtp_port=587).send(make_email())

    assert not any(call[0] == "login" for call in smtp_server.calls)


def test_a_sending_limit_delays_every_lead_and_raises_the_alert_instead_of_failing_them(
    client, ctx, clock, smtp_server
):
    # Rule: end to end through the worker. Gmail answers its daily limit with 550 after the
    # message body; no lead may be written off for that. They wait, we are told, they go out.
    ctx.worker.email_provider = smtp_provider()  # the real SMTP provider on the fake server
    smtp_server.send_error = smtplib.SMTPDataError(550, b"5.4.5 Daily user sending limit exceeded")
    for n in range(3):
        payload = ads_payload(Email=f"consumer{n}@gmail.com", PhoneNumber=f"317555{1000 + n:04d}")
        assert post_lead(client, payload).json()["result"] == "processed"

    def intros() -> list[tuple]:
        rows = db_rows(ctx, "SELECT status, attempts FROM outbox_messages WHERE template = 'intro'")
        return [(row["status"], row["attempts"]) for row in rows]

    assert ctx.worker.deliver_due() == 3

    assert intros() == [("pending", 1)] * 3
    alert = db_one(ctx, "SELECT status, severity, count, title FROM alerts WHERE dedupe_key = 'email_send_error'")
    assert (alert["status"], alert["severity"], alert["count"]) == ("open", "warning", 3)
    assert "Daily user sending limit exceeded" in alert["title"]
    assert smtp_server.delivered == []

    smtp_server.send_error = None  # the limit has reset
    clock.advance(seconds=5)
    assert ctx.worker.deliver_due() == 3

    assert intros() == [("sent", 2)] * 3
    assert sorted(msg["To"] for msg in smtp_server.delivered) == [f"consumer{n}@gmail.com" for n in range(3)]
    assert db_one(ctx, "SELECT status FROM alerts WHERE dedupe_key = 'email_send_error'")["status"] == "resolved"


# --- Resend (httpx.post replaced by a fake) --------------------------------------------


class FakeResendApi:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.status = 200
        self.body: dict = {"id": "re_123"}

    def post(self, url, **kwargs):
        self.requests.append({"url": url, **kwargs})
        return httpx.Response(self.status, json=self.body, request=httpx.Request("POST", url))


@pytest.fixture
def resend_api(monkeypatch) -> FakeResendApi:
    api = FakeResendApi()
    monkeypatch.setattr(httpx, "post", api.post)
    return api


def resend_provider() -> ResendEmailProvider:
    return ResendEmailProvider(Settings(_env_file=None, resend_api_key="re_test_key", smtp_timeout_seconds=15.0))


def test_resend_posts_the_documented_payload_with_a_bearer_token(resend_api):
    # Rule: one POST to Resend's /emails with the API key as a bearer token.
    email = make_email(
        reply_to="office@sparkle.example",
        headers={"List-Unsubscribe": "<https://leads.example.test/unsubscribe/abc>"},
    )

    result = resend_provider().send(email)

    (request,) = resend_api.requests
    assert request["url"] == "https://api.resend.com/emails"
    assert request["headers"] == {"Authorization": "Bearer re_test_key"}
    assert request["timeout"] == 15.0
    assert request["json"] == {
        "from": "Sparkle Home Cleaning <leads@example.test>",
        "to": ["pat@example.com"],
        "subject": email.subject,
        "text": email.text,
        "html": email.html,
        "reply_to": "office@sparkle.example",
        "headers": {"List-Unsubscribe": "<https://leads.example.test/unsubscribe/abc>"},
    }
    assert (result.provider, result.message_id) == ("resend", "re_123")


def test_resend_payload_omits_optional_fields_that_are_not_set(resend_api):
    # Rule: no html / reply_to / headers keys when the email has none (Resend rejects nulls).
    resend_provider().send(make_email(html=None))

    assert set(resend_api.requests[0]["json"]) == {"from", "to", "subject", "text"}


@pytest.mark.parametrize(
    ("body", "message_id"),
    [({"id": 12345}, "12345"), ({"unexpected": True}, None), (["unexpected"], None), ("accepted", None)],
)
def test_resend_success_with_an_unexpected_body_is_still_a_success(resend_api, body, message_id):
    # Rule: a 2xx means Resend took the message. An answer we cannot read must not be
    # reported as a failure (the email would be sent again); we just have no id to keep.
    resend_api.body = body

    result = resend_provider().send(make_email())

    assert (result.provider, result.message_id) == ("resend", message_id)
    assert len(resend_api.requests) == 1


@pytest.mark.parametrize("status", [400, 422])
def test_resend_validation_error_is_permanent(resend_api, status):
    # Rule: Resend says the message itself is invalid (e.g. a bad address); do not retry.
    resend_api.status = status
    resend_api.body = {"name": "validation_error", "message": "Invalid `to` field."}

    with pytest.raises(PermanentEmailError, match="Invalid `to` field"):
        resend_provider().send(make_email())


@pytest.mark.parametrize("status", [500, 502, 503, 429, 401, 403])
def test_resend_server_and_auth_errors_are_transient(resend_api, status):
    # Rule: outages, rate limits and a wrong API key are retried, never blamed on the lead.
    resend_api.status = status
    resend_api.body = {"message": "nope"}

    with pytest.raises(httpx.HTTPStatusError) as raised:
        resend_provider().send(make_email())

    assert not isinstance(raised.value, PermanentEmailError)
    assert raised.value.response.status_code == status


def test_resend_network_error_is_transient(monkeypatch):
    # Rule: a timeout talking to Resend is retried.
    def unreachable(url, **kwargs):
        raise httpx.ConnectTimeout("timed out")

    monkeypatch.setattr(httpx, "post", unreachable)

    with pytest.raises(httpx.ConnectTimeout):
        resend_provider().send(make_email())
