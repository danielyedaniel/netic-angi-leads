"""The pages a consumer sees (app/routes/public.py + app/templates/public): the booking
page behind the link in the email, and unsubscribe. Both are reached with nothing but
the per-lead token.
"""

from __future__ import annotations

import json
import re
from urllib.parse import quote

import pytest

from app.util import iso
from tests.conftest import (
    ADMIN,
    BASE_URL,
    START,
    SUMMIT_ADS_ACCOUNT,
    ads_payload,
    db_one,
    db_rows,
    leads_payload,
    post_lead,
)

SLOT_INPUT = re.compile(r'<input type="radio" name="slot" value="([^"]+)" required\s*(checked)?\s*>')
SCRIPT = "<script>alert(1)</script>"
ATTRIBUTE_BREAK = '" onmouseover="alert(1)'

# Sparkle's first open windows at START (Thu 2026-11-12 10:00 in Indianapolis, UTC-5).
THU_NOON = "2026-11-12T17:00:00.000Z"  # Thu 12:00 - 2:00 PM
THU_2PM = "2026-11-12T19:00:00.000Z"  # Thu 2:00 - 4:00 PM
FRI_8AM = "2026-11-13T13:00:00.000Z"  # Fri 8:00 - 10:00 AM


# --- helpers ---------------------------------------------------------------------------


def new_lead(client, ctx, **overrides) -> dict:
    """Post an Angi Ads lead for Sparkle ("Pat", pat@example.com unless overridden)."""
    payload = ads_payload(**{"Email": "pat@example.com", "FirstName": "Pat", "LastName": "Lee", **overrides})
    response = post_lead(client, payload)
    assert response.json()["result"] == "processed", response.text
    return lead_row(ctx, response.json()["leadId"])


def lead_row(ctx, lead_id: str) -> dict:
    return db_one(ctx, "SELECT * FROM leads WHERE id = ?", (lead_id,))


def lead_events(ctx, lead_id: str, event_type: str) -> list[dict]:
    rows = db_rows(
        ctx, "SELECT data_json FROM lead_events WHERE lead_id = ? AND type = ? ORDER BY id", (lead_id, event_type)
    )
    return [json.loads(row["data_json"] or "{}") for row in rows]


def messages(ctx, lead_id: str) -> list[dict]:
    return db_rows(ctx, "SELECT * FROM outbox_messages WHERE lead_id = ? ORDER BY sequence, created_at", (lead_id,))


def suppressions(ctx) -> list[dict]:
    return db_rows(ctx, "SELECT * FROM email_suppressions ORDER BY tenant_id, email_normalized")


def slot_inputs(html: str) -> list[tuple[str, bool]]:
    """(slot key, is it preselected?) for every radio button on the page, in page order."""
    return [(key, bool(checked)) for key, checked in SLOT_INPUT.findall(html)]


def book_url(lead: dict) -> str:
    return f"/book/{lead['booking_token']}"


def unsubscribe_url(lead: dict) -> str:
    return f"/unsubscribe/{lead['booking_token']}"


# --- GET /book/{token} -------------------------------------------------------------------


def test_booking_page_lists_open_windows_grouped_by_day(client, ctx):
    # Rule: the consumer sees who they are booking with, for what, and five days of windows.
    lead = new_lead(client, ctx)

    page = client.get(book_url(lead))

    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert "<title>Book with Sparkle Home Cleaning</title>" in page.text
    assert "Pick a time, Pat" in page.text
    assert "For your House Cleaning request in Indianapolis." in page.text
    assert re.findall(r"<h2>([^<]+)</h2>", page.text) == [
        "Thursday, November 12",
        "Friday, November 13",
        "Saturday, November 14",
        "Monday, November 16",
        "Tuesday, November 17",
    ]
    # Radios sit under their own day's heading: 2 left today, 4 on each of the other days.
    per_day = [len(SLOT_INPUT.findall(section)) for section in page.text.split("<h2>")[1:]]
    assert per_day == [2, 4, 4, 4, 4]
    inputs = slot_inputs(page.text)
    assert [key for key, _ in inputs[:3]] == [THU_NOON, THU_2PM, FRI_8AM]
    assert not any(checked for _, checked in inputs)
    assert len({key for key, _ in inputs}) == 18
    assert re.findall(r"<span>([^<]+)</span>", page.text)[:3] == ["12:00 - 2:00 PM", "2:00 - 4:00 PM", "8:00 - 10:00 AM"]
    assert f'<form method="post" action="{book_url(lead)}">' in page.text
    assert "Confirm appointment" in page.text
    assert "All times are Sparkle Home Cleaning's local time (EST)." in page.text
    assert "(317) 555-0142" in page.text
    assert '<meta name="robots" content="noindex">' in page.text


def test_booking_page_shows_times_in_the_tenants_own_timezone(client, ctx):
    # Rule: a Denver business's customer sees Denver times, and the page says whose clock it is
    # (the consumer may be reading it in another time zone).
    lead = new_lead(client, ctx, ALAccountId=SUMMIT_ADS_ACCOUNT)

    page = client.get(book_url(lead))

    assert "Summit Heating &amp; Air" in page.text
    assert "All times are Summit Heating &amp; Air's local time (MST)." in page.text
    # 15:00 UTC is 08:00 in Denver, so the first window with two hours' notice is 10:00 local.
    assert slot_inputs(page.text)[0] == ("2026-11-12T17:00:00.000Z", False)
    assert re.findall(r"<span>([^<]+)</span>", page.text)[0] == "10:00 AM - 12:00 PM"


def test_separate_visits_are_recorded_but_first_engagement_is_stamped_once(client, ctx, clock):
    # Rule: visits at different times are on the timeline; the funnel's "engaged" time is the first.
    lead = new_lead(client, ctx)
    ctx.worker.deliver_due()
    assert lead_row(ctx, lead["id"])["status"] == "contacted"
    clock.advance(minutes=3)

    client.get(book_url(lead), headers={"User-Agent": "Mozilla/5.0 (test)"})

    first = lead_row(ctx, lead["id"])
    assert first["status"] == "engaged"
    assert first["first_engaged_at"] == iso(clock())
    assert lead_events(ctx, lead["id"], "booking.link_clicked") == [
        {"user_agent": "Mozilla/5.0 (test)", "preselected_slot": None}
    ]

    clock.advance(hours=2)
    client.get(book_url(lead) + f"?slot={quote(FRI_8AM)}")

    second = lead_row(ctx, lead["id"])
    assert second["status"] == "engaged"
    assert second["first_engaged_at"] == first["first_engaged_at"]
    clicks = lead_events(ctx, lead["id"], "booking.link_clicked")
    assert len(clicks) == 2
    assert clicks[1]["preselected_slot"] == FRI_8AM


def test_hammering_the_booking_link_adds_one_timeline_event_not_fifty(client, ctx, clock):
    # Rule: reloading the page (or a script looping over it with a huge ?slot=) cannot grow
    # the database: one "link opened" event per lead per minute, and the slot is cut short.
    lead = new_lead(client, ctx)
    long_slot = "A" * 6000
    opened_at = iso(clock())

    for n in range(50):
        if n == 25:
            clock.advance(seconds=30)  # still inside the same minute
        assert client.get(book_url(lead), params={"slot": long_slot}).status_code == 200

    stored = db_rows(ctx, "SELECT data_json FROM lead_events WHERE lead_id = ? AND type = 'booking.link_clicked'", (lead["id"],))
    assert len(stored) == 1
    assert json.loads(stored[0]["data_json"])["preselected_slot"] == "A" * 40
    assert len(stored[0]["data_json"]) < 200
    row = lead_row(ctx, lead["id"])
    assert (row["status"], row["first_engaged_at"]) == ("engaged", opened_at)  # did not move

    clock.advance(seconds=30)  # a full minute after the first visit: a new visit
    client.get(book_url(lead))
    assert len(lead_events(ctx, lead["id"], "booking.link_clicked")) == 2
    assert lead_row(ctx, lead["id"])["first_engaged_at"] == opened_at


def test_an_oversized_user_agent_is_cut_short_on_the_timeline(client, ctx):
    # Rule: nothing the browser sends is stored without a bound.
    lead = new_lead(client, ctx)

    client.get(book_url(lead), headers={"User-Agent": "Mozilla/5.0 " + "x" * 5000})

    (click,) = lead_events(ctx, lead["id"], "booking.link_clicked")
    assert len(click["user_agent"]) == 200
    assert click["user_agent"].startswith("Mozilla/5.0 xxx")


def test_viewing_the_page_before_the_intro_went_out_also_counts_as_engaged(client, ctx):
    # Rule: new -> engaged as well as contacted -> engaged.
    lead = new_lead(client, ctx)
    assert lead["status"] == "new"

    client.get(book_url(lead))

    assert lead_row(ctx, lead["id"])["status"] == "engaged"


def test_viewing_the_page_never_downgrades_a_booked_lead(client, ctx, clock):
    # Rule: status only moves forward; looking at the booking again does not undo it.
    lead = new_lead(client, ctx)
    assert client.post(book_url(lead), data={"slot": THU_NOON}, follow_redirects=False).status_code == 303
    booked = lead_row(ctx, lead["id"])
    assert booked["status"] == "booked"
    clock.advance(minutes=30)

    assert client.get(book_url(lead)).status_code == 200

    after = lead_row(ctx, lead["id"])
    assert after["status"] == "booked"
    assert after["first_engaged_at"] == booked["first_engaged_at"]
    assert after["booked_at"] == booked["booked_at"]
    assert len(lead_events(ctx, lead["id"], "booking.link_clicked")) == 1


@pytest.mark.parametrize("outcome", ["won", "lost"])
def test_viewing_the_page_never_reopens_a_closed_lead(client, ctx, outcome):
    # Rule: a recorded outcome is final as far as page views are concerned.
    lead = new_lead(client, ctx)
    recorded = client.post(f"/admin/api/leads/{lead['id']}/outcome", json={"outcome": outcome}, headers=ADMIN)
    assert recorded.status_code == 200, recorded.text

    assert client.get(book_url(lead)).status_code == 200

    assert lead_row(ctx, lead["id"])["status"] == outcome


def test_slot_query_parameter_preselects_that_window(client, ctx):
    # Rule: the "?slot=" links in the email arrive with that radio button already chosen.
    lead = new_lead(client, ctx)

    page = client.get(book_url(lead), params={"slot": FRI_8AM})

    assert page.status_code == 200
    assert [key for key, checked in slot_inputs(page.text) if checked] == [FRI_8AM]
    assert len(slot_inputs(page.text)) == 18


@pytest.mark.parametrize(
    "slot",
    [
        "2026-11-15T15:00:00.000Z",  # a Sunday
        "2026-11-13T13:00:00Z",  # a real window in a different spelling
        "garbage",
        "",
        '"><script>alert(1)</script>',
    ],
)
def test_unknown_slot_query_parameter_is_ignored(client, ctx, slot):
    # Rule: a stale or forged ?slot= shows the normal page with nothing chosen (and is not echoed).
    lead = new_lead(client, ctx)

    page = client.get(book_url(lead), params={"slot": slot})

    assert page.status_code == 200
    inputs = slot_inputs(page.text)
    assert len(inputs) == 18
    assert not any(checked for _, checked in inputs)
    assert "<script>alert(1)" not in page.text


def test_slot_someone_else_has_taken_is_not_preselected(client, ctx):
    # Rule: an email link to a window that has since been booked falls back to "pick a time".
    first = new_lead(client, ctx)
    second = new_lead(client, ctx, Email="sam@example.com", PhoneNumber="3175551234", FirstName="Sam")
    assert client.post(book_url(first), data={"slot": THU_NOON}, follow_redirects=False).status_code == 303

    page = client.get(book_url(second), params={"slot": THU_NOON})

    inputs = slot_inputs(page.text)
    assert THU_NOON not in [key for key, _ in inputs]
    assert not any(checked for _, checked in inputs)
    assert "Pick a time, Sam" in page.text


def test_after_booking_the_page_confirms_the_window(client, ctx):
    # Rule: once booked, the page says so, with the window, and offers a change of time.
    lead = new_lead(client, ctx)

    page = client.post(book_url(lead), data={"slot": THU_NOON})  # follows the 303 back to the page

    assert page.status_code == 200
    assert page.url.path == book_url(lead)
    assert "You're booked, Pat." in page.text
    assert "Thu, Nov 12, 12:00 - 2:00 PM" in page.text
    assert "within that window at 123 Main St. A confirmation is on its way to your inbox.</p>" in page.text
    assert "Pick a time, Pat" not in page.text
    assert "Need a different time?" in page.text
    assert "Change my appointment" in page.text
    assert THU_NOON not in [key for key, _ in slot_inputs(page.text)]  # their own window is not re-offered
    assert len(slot_inputs(page.text)) == 17


def test_unknown_token_gets_a_plain_404_page(client, ctx):
    # Rule: a wrong link reveals nothing: no lead data, no stack trace, no slots.
    lead = new_lead(client, ctx, FirstName="Zebediah")

    for url in ("/book/not-a-real-token", book_url(lead) + "x", book_url(lead)[:-1], "/book/" + "a" * 500):
        page = client.get(url)
        assert page.status_code == 404, url
        assert page.headers["content-type"].startswith("text/html")
        assert "This link is no longer valid" in page.text
        assert "Traceback" not in page.text and "sqlite" not in page.text.lower()
        assert "Zebediah" not in page.text and "Sparkle" not in page.text
        assert slot_inputs(page.text) == []
    assert db_rows(ctx, "SELECT id FROM lead_events WHERE type = 'booking.link_clicked'") == []
    assert lead_row(ctx, lead["id"])["status"] == "new"


def test_token_lookup_is_case_sensitive(client, ctx):
    # Rule: the token is a secret, compared exactly.
    lead = new_lead(client, ctx)
    wrong_case = lead["booking_token"].swapcase()
    assert wrong_case != lead["booking_token"]

    assert client.get(f"/book/{wrong_case}").status_code == 404
    assert client.get(f"/unsubscribe/{wrong_case}").status_code == 404


def test_posting_to_an_unknown_token_is_a_404_and_books_nothing(client, ctx):
    # Rule: the form cannot be used without a valid link, with or without a slot.
    new_lead(client, ctx)

    with_slot = client.post("/book/not-a-real-token", data={"slot": THU_NOON}, follow_redirects=False)
    without_slot = client.post("/book/not-a-real-token", follow_redirects=False)

    assert with_slot.status_code == 404
    assert without_slot.status_code == 404
    assert "This link is no longer valid" in with_slot.text
    assert db_rows(ctx, "SELECT id FROM appointments") == []


@pytest.mark.parametrize("form", [None, {"slot": ""}, {"other": "x"}])
def test_posting_without_choosing_a_slot_is_a_400_with_a_message(client, ctx, form):
    # Rule: submitting the form empty asks the consumer to choose, and books nothing.
    lead = new_lead(client, ctx)

    response = client.post(book_url(lead), data=form, follow_redirects=False)

    assert response.status_code == 400
    assert "Please choose a time first." in response.text
    assert 'role="alert"' in response.text
    assert len(slot_inputs(response.text)) == 18  # the form is shown again
    assert db_rows(ctx, "SELECT id FROM appointments") == []
    assert lead_row(ctx, lead["id"])["booked_at"] is None


def test_booking_page_escapes_hostile_lead_data(client, ctx):
    # Rule: the lead's name, task, city and address are untrusted and must never become markup.
    response = post_lead(
        client,
        leads_payload(
            email="mallory@example.com",
            firstName=SCRIPT,
            name=f"{SCRIPT} Doe",
            taskName=f"Roof {ATTRIBUTE_BREAK} {SCRIPT}",
            city=f"{SCRIPT}{ATTRIBUTE_BREAK}",
            address=f"1 Main St {SCRIPT}{ATTRIBUTE_BREAK}",
        ),
    )
    lead = lead_row(ctx, response.json()["leadId"])
    assert lead["first_name"] == SCRIPT

    before = client.get(book_url(lead))
    first_slot = slot_inputs(before.text)[0][0]
    after = client.post(book_url(lead), data={"slot": first_slot})  # the booked view shows the address

    for page in (before, after):
        assert page.status_code == 200
        assert "<script>alert(1)" not in page.text
        assert '" onmouseover=' not in page.text
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page.text
    assert "&#34; onmouseover=&#34;alert(1)" in before.text  # task and city
    assert "&#34; onmouseover=&#34;alert(1)" in after.text  # address
    assert "You're booked, &lt;script&gt;" in after.text


def test_booking_tokens_are_long_url_safe_and_unique_per_lead(client, ctx):
    # Rule: the token is the only credential, so it must be unguessable and never shared.
    for n in range(30):
        new_lead(client, ctx, Email=f"consumer{n}@example.com", PhoneNumber=f"317555{1000 + n:04d}")

    tokens = [row["booking_token"] for row in db_rows(ctx, "SELECT booking_token FROM leads")]

    assert len(tokens) == 30
    assert len(set(tokens)) == 30
    assert all(re.fullmatch(r"[A-Za-z0-9_-]{20,}", token) for token in tokens)
    lead_ids = [row["id"] for row in db_rows(ctx, "SELECT id FROM leads")]
    assert not any(lead_id.split("_")[1] in token for lead_id in lead_ids for token in tokens)


def test_a_token_only_ever_shows_its_own_lead(client, ctx):
    # Rule: one link, one request: another consumer's name never appears on your page.
    first = new_lead(client, ctx, FirstName="Patricia")
    second = new_lead(client, ctx, Email="sam@example.com", PhoneNumber="3175551234", FirstName="Samantha")

    first_page = client.get(book_url(first)).text
    second_page = client.get(book_url(second)).text

    assert "Pick a time, Patricia" in first_page and "Samantha" not in first_page
    assert "Pick a time, Samantha" in second_page and "Patricia" not in second_page
    assert second["booking_token"] not in first_page and first["booking_token"] not in second_page


def test_lead_without_a_name_or_task_gets_generic_wording(client, ctx):
    # Rule: a sparse lead still gets a sensible page.
    lead = new_lead(client, ctx, FirstName="", LastName="", Category="", PostalAddress={})

    page = client.get(book_url(lead))

    assert page.status_code == 200
    assert "Pick a time, there" in page.text
    assert "For the request you sent us through Angi." in page.text
    assert "None" not in page.text.replace(lead["booking_token"], "")


def test_deactivated_tenant_shows_no_times(client, ctx):
    # Rule: a switched-off business offers nothing to book, but the link still explains itself.
    lead = new_lead(client, ctx)
    with ctx.db.transaction() as conn:
        conn.execute("UPDATE tenants SET is_active = 0 WHERE id = 'tnt_sparkle'")

    page = client.get(book_url(lead))

    assert page.status_code == 200
    assert slot_inputs(page.text) == []
    assert "We don't have any open times online right now" in page.text
    assert "<form" not in page.text


# --- unsubscribe -------------------------------------------------------------------------


def test_unsubscribe_get_asks_for_confirmation_and_changes_nothing(client, ctx):
    # Rule: mail scanners follow links; a GET must never opt anyone out.
    lead = new_lead(client, ctx)
    ctx.worker.deliver_due()
    before = lead_row(ctx, lead["id"])

    for _ in range(3):
        page = client.get(unsubscribe_url(lead))
        assert page.status_code == 200

    assert "Stop emails from Sparkle Home Cleaning?" in page.text
    assert "Sparkle Home Cleaning will no longer email you" in page.text
    assert f'<form method="post" action="{unsubscribe_url(lead)}">' in page.text
    assert f'<form method="get" action="{book_url(lead)}">' in page.text
    assert "You're unsubscribed" not in page.text
    assert suppressions(ctx) == []
    assert [m["status"] for m in messages(ctx, lead["id"])] == ["sent", "pending", "pending"]
    assert lead_events(ctx, lead["id"], "lead.unsubscribed") == []
    assert lead_row(ctx, lead["id"]) == before


def test_unsubscribe_post_suppresses_the_address_and_cancels_pending_email(client, ctx, mailer, clock):
    # Rule: confirming stops everything still queued for this request and records the opt-out.
    lead = new_lead(client, ctx)
    ctx.worker.deliver_due()
    clock.advance(minutes=10)

    page = client.post(unsubscribe_url(lead))

    assert page.status_code == 200
    assert "You're unsubscribed" in page.text
    assert "Stop emails from Sparkle Home Cleaning?" not in page.text
    assert suppressions(ctx) == [
        {
            "tenant_id": "tnt_sparkle",
            "email_normalized": "pat@example.com",
            "reason": "unsubscribe_link",
            "created_at": iso(clock()),
        }
    ]
    assert [(m["template"], m["status"], m["last_error"]) for m in messages(ctx, lead["id"])] == [
        ("intro", "sent", None),
        ("followup", "cancelled", "unsubscribed"),
        ("followup", "cancelled", "unsubscribed"),
    ]
    assert lead_events(ctx, lead["id"], "lead.unsubscribed") == [{}]
    assert lead_events(ctx, lead["id"], "email.cancelled") == [
        {"reason": "unsubscribed", "count": 2, "templates": ["followup"]}
    ]

    clock.advance(days=2)
    assert ctx.worker.deliver_due() == 0
    assert len(mailer.sent) == 1  # only the intro, sent before the opt-out


def test_unsubscribing_before_the_intro_went_out_stops_the_intro_too(client, ctx, mailer):
    # Rule: every pending message for the lead is cancelled, not only follow-ups.
    lead = new_lead(client, ctx)

    assert client.post(unsubscribe_url(lead)).status_code == 200

    assert [m["status"] for m in messages(ctx, lead["id"])] == ["cancelled"] * 3
    assert ctx.worker.deliver_due() == 0
    assert mailer.sent == []


def test_unsubscribing_twice_is_harmless(client, ctx):
    # Rule: a second click (or a provider retrying the one-click POST) changes nothing more.
    lead = new_lead(client, ctx)

    first = client.post(unsubscribe_url(lead))
    second = client.post(unsubscribe_url(lead))

    assert (first.status_code, second.status_code) == (200, 200)
    assert "You're unsubscribed" in second.text
    assert len(suppressions(ctx)) == 1
    assert len(lead_events(ctx, lead["id"], "email.cancelled")) == 1


def test_one_click_unsubscribe_post_from_a_mailbox_provider_works(client, ctx, mailer):
    # Rule (RFC 8058): the provider POSTs "List-Unsubscribe=One-Click" to the header's URL.
    lead = new_lead(client, ctx)
    ctx.worker.deliver_due()
    header = mailer.sent[0].headers["List-Unsubscribe"]
    assert header == f"<{BASE_URL}{unsubscribe_url(lead)}>"
    assert mailer.sent[0].headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"

    response = client.post(
        header.strip("<>").removeprefix(BASE_URL),
        content="List-Unsubscribe=One-Click",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 200
    assert [row["email_normalized"] for row in suppressions(ctx)] == ["pat@example.com"]
    assert [m["status"] for m in messages(ctx, lead["id"])] == ["sent", "cancelled", "cancelled"]
    assert len(lead_events(ctx, lead["id"], "lead.unsubscribed")) == 1


def test_one_click_unsubscribe_as_multipart_form_data_works(client, ctx):
    # Rule: RFC 8058 allows the same body as multipart/form-data.
    lead = new_lead(client, ctx)

    response = client.post(unsubscribe_url(lead), files={"List-Unsubscribe": (None, "One-Click")})

    assert response.status_code == 200
    assert len(suppressions(ctx)) == 1


def test_after_unsubscribing_a_new_lead_for_the_same_tenant_is_stored_but_not_emailed(client, ctx, mailer):
    # Rule: the opt-out covers the address for that business, however the email is capitalised.
    lead = new_lead(client, ctx)
    ctx.worker.deliver_due()
    client.post(unsubscribe_url(lead))
    sent_before = len(mailer.sent)

    response = post_lead(client, ads_payload(Email="PAT@Example.COM", FirstName="Pat"))

    body = response.json()
    assert response.status_code == 200
    assert body["result"] == "processed"
    assert body["email"] == {"status": "skipped", "reason": "unsubscribed"}
    stored = lead_row(ctx, body["leadId"])
    assert stored["email_normalized"] == "pat@example.com"
    assert stored["tenant_id"] == "tnt_sparkle"
    assert messages(ctx, stored["id"]) == []
    assert lead_events(ctx, stored["id"], "email.skipped") == [{"reason": "unsubscribed"}]
    assert ctx.worker.deliver_due() == 0
    assert len(mailer.sent) == sent_before


def test_unsubscribing_from_one_tenant_does_not_silence_another(client, ctx, mailer):
    # Rule: opt-outs are per business; a different tenant may still answer this consumer.
    lead = new_lead(client, ctx)
    ctx.worker.deliver_due()
    client.post(unsubscribe_url(lead))

    response = post_lead(client, ads_payload(Email="pat@example.com", ALAccountId=SUMMIT_ADS_ACCOUNT))

    body = response.json()
    assert body["tenant"]["id"] == "tnt_summit"
    assert body["email"]["status"] == "queued"
    assert ctx.worker.deliver_due() == 1
    assert (mailer.sent[-1].to, mailer.sent[-1].from_name) == ("pat@example.com", "Summit Heating & Air")
    assert [row["tenant_id"] for row in suppressions(ctx)] == ["tnt_sparkle"]


def test_an_unsubscribed_consumer_who_books_anyway_still_gets_the_confirmation(client, ctx, mailer):
    # Rule: "stop emails" stops our outreach. A confirmation of a booking the person has just
    # made themselves is not outreach: it is sent, and the pages say so.
    lead = new_lead(client, ctx)
    ctx.worker.deliver_due()
    unsubscribed = client.post(unsubscribe_url(lead))
    assert "if you book a time yourself, we'll confirm it by email" in unsubscribed.text

    page = client.post(book_url(lead), data={"slot": THU_NOON})  # follows the redirect to the booked page
    assert "A confirmation is on its way to your inbox." in page.text
    ctx.worker.deliver_due()

    assert lead_row(ctx, lead["id"])["status"] == "booked"
    booked = [email for email in mailer.sent if email.subject.startswith("You're booked")]
    assert [(email.to, email.subject) for email in booked] == [
        ("pat@example.com", "You're booked: Thu, Nov 12, 12:00 - 2:00 PM - Sparkle Home Cleaning")
    ]
    assert "List-Unsubscribe" not in booked[0].headers  # transactional: nothing to unsubscribe from
    assert [(m["template"], m["status"], m["last_error"]) for m in messages(ctx, lead["id"])] == [
        ("intro", "sent", None),
        ("booking_confirmation", "sent", None),
        ("followup", "cancelled", "unsubscribed"),
        ("followup", "cancelled", "unsubscribed"),
    ]
    assert len(mailer.sent) == 2  # the intro (before the opt-out) and the confirmation, nothing else
    assert len(suppressions(ctx)) == 1  # the opt-out itself still stands


def test_booked_page_does_not_promise_a_confirmation_email_without_an_address(client, ctx, mailer):
    # Rule: the page only says what will happen. No usable email -> no confirmation -> no promise.
    lead = new_lead(client, ctx, Email="not-an-email")

    page = client.post(book_url(lead), data={"slot": THU_NOON})

    assert "You're booked, Pat." in page.text
    assert "Your technician will arrive within that window at 123 Main St.</p>" in page.text
    assert "confirmation" not in page.text.lower()
    assert ctx.worker.deliver_due() == 0
    assert mailer.sent == []


def test_unsubscribe_for_a_lead_without_a_valid_email_records_the_request_only(client, ctx):
    # Rule: nothing to suppress, but the click is still acknowledged and logged.
    lead = new_lead(client, ctx, Email="not-an-email")

    page = client.post(unsubscribe_url(lead))

    assert page.status_code == 200
    assert "You're unsubscribed" in page.text
    assert suppressions(ctx) == []
    assert len(lead_events(ctx, lead["id"], "lead.unsubscribed")) == 1


def test_unsubscribe_with_an_unknown_token_is_a_404_and_writes_nothing(client, ctx):
    # Rule: only the link from the email can opt an address out.
    lead = new_lead(client, ctx, FirstName="Zebediah")

    get = client.get("/unsubscribe/not-a-real-token")
    post = client.post("/unsubscribe/not-a-real-token")

    for response in (get, post):
        assert response.status_code == 404
        assert "This link is no longer valid" in response.text
        assert "Zebediah" not in response.text and "Sparkle" not in response.text
        assert "Traceback" not in response.text
    assert suppressions(ctx) == []
    assert db_rows(ctx, "SELECT id FROM lead_events WHERE type = 'lead.unsubscribed'") == []
    assert [m["status"] for m in messages(ctx, lead["id"])] == ["pending", "pending", "pending"]


def test_unsubscribe_page_escapes_the_tenant_name(client, ctx):
    # Rule: tenant names with markup characters render as text.
    lead = new_lead(client, ctx, ALAccountId=SUMMIT_ADS_ACCOUNT)

    page = client.get(unsubscribe_url(lead))

    assert "Summit Heating &amp; Air" in page.text
    assert "Heating & Air" not in page.text


# --- root --------------------------------------------------------------------------------


def test_root_redirects_to_the_dashboard(client):
    # Rule: the bare domain sends an operator to the dashboard (which asks for the admin token).
    response = client.get("/", follow_redirects=False)

    assert response.status_code in (302, 307)
    assert response.headers["location"] == "/dashboard"


def test_start_constant_matches_the_slot_constants_used_here():
    # Guard: the slot keys above assume the suite's fixed start time (Thu 2026-11-12 15:00 UTC).
    assert iso(START) == "2026-11-12T15:00:00.000Z"
