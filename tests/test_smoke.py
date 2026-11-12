"""The happy path, end to end: Angi posts a lead -> stored -> mapped -> emailed -> booked."""

from __future__ import annotations

import re

from tests.conftest import ADMIN, SPARKLE_ACCOUNT, ads_payload, db_one, post_lead


def test_lead_is_stored_mapped_emailed_and_booked(client, ctx, mailer, clock):
    payload = ads_payload(Email="pat@example.com", FirstName="Pat", LastName="Lee")

    response = post_lead(client, payload)

    # Angi's contract: HTTP 200 and a body whose status is "success".
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["result"] == "processed"
    assert body["tenant"]["id"] == "tnt_sparkle"
    assert body["tenant"]["routedBy"] == "account_id"

    # Stored in one canonical shape, attached to the tenant mapped from ALAccountId.
    lead = db_one(ctx, "SELECT * FROM leads WHERE id = ?", (body["leadId"],))
    assert lead["tenant_id"] == "tnt_sparkle"
    assert lead["external_account_id"] == SPARKLE_ACCOUNT
    assert lead["external_lead_id"] == payload["CorrelationId"]
    assert lead["email_normalized"] == "pat@example.com"
    assert lead["phone_normalized"] == "+15554332646"
    assert lead["task_name"] == "House Cleaning"
    assert lead["market"] == "Indianapolis"
    assert lead["status"] == "new"

    # The intro email is queued in the same transaction and goes out on the next worker pass.
    assert mailer.sent == []
    clock.advance(seconds=1)
    ctx.worker.deliver_due()
    assert len(mailer.sent) == 1
    email = mailer.sent[0]
    assert email.to == "pat@example.com"
    assert email.from_name == "Sparkle Home Cleaning"
    assert "House Cleaning" in email.subject
    assert f"/book/{lead['booking_token']}" in email.text
    assert email.html and "Pick a time" in email.html

    lead = db_one(ctx, "SELECT * FROM leads WHERE id = ?", (body["leadId"],))
    assert lead["status"] == "contacted"
    assert lead["first_contacted_at"] is not None

    # The consumer follows the link and books a slot.
    page = client.get(f"/book/{lead['booking_token']}")
    assert page.status_code == 200
    slot = re.search(r'name="slot" value="([^"]+)"', page.text).group(1)
    booked = client.post(f"/book/{lead['booking_token']}", data={"slot": slot}, follow_redirects=False)
    assert booked.status_code == 303

    lead = db_one(ctx, "SELECT * FROM leads WHERE id = ?", (body["leadId"],))
    assert lead["status"] == "booked"
    assert lead["booked_at"] is not None
    appointment = db_one(ctx, "SELECT * FROM appointments WHERE lead_id = ?", (lead["id"],))
    assert appointment["starts_at"] == slot

    # Booking cancels the follow-up nudges and sends a confirmation instead.
    ctx.worker.deliver_due()
    assert mailer.sent[-1].subject.startswith("You're booked")
    pending = db_one(
        ctx, "SELECT COUNT(*) AS n FROM outbox_messages WHERE lead_id = ? AND status = 'pending'", (lead["id"],)
    )
    assert pending["n"] == 0

    # And the funnel reflects it.
    summary = client.get("/admin/api/analytics/summary", headers=ADMIN).json()
    assert summary["funnel"]["received"] == 1
    assert summary["funnel"]["booked"] == 1
