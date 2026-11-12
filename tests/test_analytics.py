"""Conversion analytics (app/analytics.py) and /admin/api/analytics/summary.

Leads are put into known states the way real ones get there: posted to the webhook,
emailed by the worker, clicked and booked through the public booking page, closed through
the admin API. The clock only moves when a test moves it, so every number is exact.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest

from app import analytics
from app.analytics import percentile, ratio
from app.mailer.providers import PermanentEmailError
from tests.conftest import (
    ADMIN,
    BLUELINE_ACCOUNT,
    CANARY_ACCOUNT,
    START,
    SUMMIT_ADS_ACCOUNT,
    ads_payload,
    db_one,
    leads_payload,
    post_lead,
)


def send(client, payload) -> dict:
    response = post_lead(client, payload)
    assert response.status_code == 200, response.text
    return response.json()


def consumer(n: int) -> dict:
    """Contact details of the n-th distinct made-up homeowner, so leads are not duplicates."""
    return {"Email": f"homeowner{n}@example.com", "PhoneNumber": f"317555{n:04d}"}


def new_lead(client, n: int, **overrides) -> str:
    """Post a distinct Angi Ads lead (Sparkle unless ALAccountId is overridden); returns its id."""
    body = send(client, ads_payload(**{**consumer(n), **overrides}))
    assert body["result"] == "processed", body
    return body["leadId"]


def deliver(ctx, clock, seconds: float = 1) -> None:
    """Advance the clock and let the worker send whatever is due."""
    clock.advance(seconds=seconds)
    ctx.worker.deliver_due()


def booking_token(ctx, lead_id: str) -> str:
    return db_one(ctx, "SELECT booking_token FROM leads WHERE id = ?", (lead_id,))["booking_token"]


def click(client, ctx, lead_id: str) -> str:
    """The consumer opens the booking link from the email. Returns the page HTML."""
    page = client.get(f"/book/{booking_token(ctx, lead_id)}")
    assert page.status_code == 200
    return page.text


def book(client, ctx, lead_id: str) -> None:
    """The consumer opens the link and books the first slot still on offer."""
    html = click(client, ctx, lead_id)
    slot = re.search(r'name="slot" value="([^"]+)"', html).group(1)
    response = client.post(f"/book/{booking_token(ctx, lead_id)}", data={"slot": slot}, follow_redirects=False)
    assert response.status_code == 303, response.text


def close(client, lead_id: str, outcome: str, **fields) -> None:
    response = client.post(
        f"/admin/api/leads/{lead_id}/outcome", headers=ADMIN, json={"outcome": outcome, **fields}
    )
    assert response.status_code == 200, response.text


def summary(ctx, **filters) -> dict:
    with ctx.db.read() as conn:
        return analytics.summary(conn, **filters)


# --- percentile / ratio ------------------------------------------------------------------


def test_percentile_of_nothing_is_none():
    # No data means "unknown", never zero.
    assert percentile([], 50) is None
    assert percentile([], 0) is None
    assert percentile([], 100) is None


@pytest.mark.parametrize("pct", [0, 50, 90, 100])
def test_percentile_of_a_single_value_is_that_value(pct):
    # One observation is every percentile of itself.
    assert percentile([7.5], pct) == 7.5


def test_percentile_endpoints_are_min_and_max():
    # p0 is the smallest value and p100 the largest.
    values = [5.0, 1.0, 4.0, 2.0, 3.0]
    assert percentile(values, 0) == 1.0
    assert percentile(values, 100) == 5.0


def test_percentile_median_of_odd_and_even_counts():
    # The median is the middle value, or the mean of the two middle values.
    assert percentile([1, 2, 3, 4, 5], 50) == 3
    assert percentile([1, 2, 3, 4], 50) == 2.5
    assert percentile([10, 20], 50) == 15


def test_percentile_interpolates_linearly_between_neighbours():
    # p90 of 1..5 sits 60% of the way from 4 to 5; p25 of 1..4 sits 75% of the way from 1 to 2.
    assert percentile([1, 2, 3, 4, 5], 90) == pytest.approx(4.6)
    assert percentile([1, 2, 3, 4], 25) == pytest.approx(1.75)
    assert percentile([2, 10, 400], 90) == pytest.approx(322.0)
    assert percentile([0, 100], 90) == pytest.approx(90.0)


def test_percentile_sorts_a_copy_and_leaves_the_input_alone():
    # Order of the input does not matter and the caller's list is not reordered.
    values = [400.0, 2.0, 10.0]
    assert percentile(values, 50) == 10.0
    assert values == [400.0, 2.0, 10.0]


def test_percentile_with_repeated_values():
    # Ties are fine.
    assert percentile([3, 3, 3, 3], 90) == 3
    assert percentile([1, 1, 2, 2], 50) == 1.5


def test_ratio_with_a_zero_denominator_is_none():
    # "Out of nothing" is unknown, not 0% and not a ZeroDivisionError.
    assert ratio(0, 0) is None
    assert ratio(5, 0) is None


def test_ratio_rounds_to_four_decimals():
    # Rates are stored as fractions with four decimals.
    assert ratio(1, 3) == 0.3333
    assert ratio(2, 3) == 0.6667
    assert ratio(0, 5) == 0.0
    assert ratio(5, 5) == 1.0


# --- empty database ----------------------------------------------------------------------


def test_empty_database_gives_zero_counts_and_none_rates(client, ctx):
    # A brand-new install must render a dashboard, not crash on a division by zero.
    result = summary(ctx)

    assert result["filters"] == {"tenant_id": None, "since": None, "until": None}
    assert result["funnel"] == {
        "received": 0,
        "unique": 0,
        "contacted": 0,
        "engaged": 0,
        "booked": 0,
        "won": 0,
        "lost": 0,
        "contact_rate": None,
        "engagement_rate": None,
        "booking_rate": None,
        "win_rate": None,
    }
    assert result["speed"] == {
        "speed_to_lead_p50_seconds": None,
        "speed_to_lead_p90_seconds": None,
        "speed_to_lead_max_seconds": None,
        "contacted_within_1_min": None,
        "contacted_within_5_min": None,
        "time_to_book_p50_minutes": None,
    }
    assert result["money"] == {
        "spend_cents": 0,
        "unpriced_leads": 0,
        "credited_cents": 0,
        "net_spend_cents": 0,
        "cost_per_lead_cents": None,
        "cost_per_booking_cents": None,
        "cost_per_won_job_cents": None,
        "revenue_cents": 0,
        "return_on_spend": None,
    }
    assert result["duplicates"] == {
        "count": 0,
        "rate": None,
        "open_claims": 0,
        "claimable_cents": 0,
        "credited_cents": 0,
    }
    assert result["attribution"] == {"booked_after_intro_only": 0, "booked_after_followup": 0}
    assert result["by_task"] == result["by_tenant"] == result["by_postal_code"] == result["by_format"] == []
    assert result["by_day"] == []
    assert result["by_hour"] == [{"hour": hour, "received": 0, "booked": 0} for hour in range(24)]


def test_empty_database_over_http(client):
    # The endpoint answers 200 with the same zero/None shape.
    response = client.get("/admin/api/analytics/summary", headers=ADMIN)
    assert response.status_code == 200
    body = response.json()
    assert body["funnel"]["received"] == 0
    assert body["funnel"]["booking_rate"] is None
    assert body["money"]["return_on_spend"] is None


# --- the funnel --------------------------------------------------------------------------


def test_funnel_counts_and_rates_from_leads_in_known_states(client, ctx, mailer, clock):
    # One lead in each state; every count and rate must follow from the funnel timestamps.
    contacted = new_lead(client, 1)
    clicked = new_lead(client, 2)
    booked = new_lead(client, 3)
    won = new_lead(client, 4)
    lost = new_lead(client, 5)
    booked_then_lost = new_lead(client, 6)
    clock.advance(minutes=1)
    duplicate = new_lead(client, 1)  # same consumer as `contacted`: flagged, still contacted
    deliver(ctx, clock)  # the seven leads so far get their first email

    clock.advance(minutes=5)
    click(client, ctx, clicked)
    book(client, ctx, booked)
    book(client, ctx, won)
    book(client, ctx, booked_then_lost)
    clock.advance(days=2)
    close(client, won, "won", job_value=450)
    close(client, lost, "lost", reason="went with a competitor")
    close(client, booked_then_lost, "lost", reason="cancelled the visit")
    received_only = new_lead(client, 8)  # arrives last; the worker has not run since

    funnel = summary(ctx)["funnel"]
    assert funnel["received"] == 8
    assert funnel["unique"] == 7  # the duplicate is not a new consumer
    assert funnel["contacted"] == 7  # everyone except the lead that just arrived
    assert funnel["engaged"] == 4  # clicked + the three who booked
    assert funnel["booked"] == 3  # including the one later marked lost
    assert funnel["won"] == 1
    assert funnel["lost"] == 2
    assert funnel["contact_rate"] == 0.875  # 7 / 8 received
    assert funnel["engagement_rate"] == 0.5714  # 4 / 7 contacted
    assert funnel["booking_rate"] == 0.375  # 3 / 8 received
    assert funnel["win_rate"] == 0.3333  # 1 / 3 booked

    statuses = {
        lead_id: db_one(ctx, "SELECT status FROM leads WHERE id = ?", (lead_id,))["status"]
        for lead_id in (received_only, contacted, clicked, booked, won, lost, booked_then_lost, duplicate)
    }
    assert statuses == {
        received_only: "new",
        contacted: "contacted",
        clicked: "engaged",
        booked: "booked",
        won: "won",
        lost: "lost",
        booked_then_lost: "lost",
        duplicate: "contacted",
    }


def test_lead_that_booked_and_was_later_lost_still_counts_as_booked(client, ctx, clock):
    # The funnel is computed from timestamps, so losing the job later does not un-book it.
    lead = new_lead(client, 1)
    deliver(ctx, clock)
    book(client, ctx, lead)
    clock.advance(days=1)
    close(client, lead, "lost", reason="no-show")

    funnel = summary(ctx)["funnel"]
    assert (funnel["booked"], funnel["lost"], funnel["won"]) == (1, 1, 0)
    assert funnel["booking_rate"] == 1.0
    assert funnel["win_rate"] == 0.0


def test_lead_with_no_email_is_received_but_never_contacted(client, ctx, clock):
    # No usable address means no first touch; it must drag the contact rate down, not vanish.
    new_lead(client, 1)
    new_lead(client, 2, Email="")
    deliver(ctx, clock)

    funnel = summary(ctx)["funnel"]
    assert (funnel["received"], funnel["contacted"]) == (2, 1)
    assert funnel["contact_rate"] == 0.5


def test_lead_whose_email_bounced_is_not_counted_as_contacted(client, ctx, mailer, clock):
    # "Contacted" means an email actually left, not that we tried.
    new_lead(client, 1)
    mailer.fail_with = PermanentEmailError("550 no such mailbox")
    mailer.fail_times = -1
    deliver(ctx, clock)

    result = summary(ctx)
    assert result["funnel"]["contacted"] == 0
    assert result["funnel"]["contact_rate"] == 0.0
    assert result["speed"]["speed_to_lead_p50_seconds"] is None


def test_clicking_the_link_twice_counts_one_engaged_lead(client, ctx, clock):
    # Engagement is per lead, not per page view.
    lead = new_lead(client, 1)
    deliver(ctx, clock)
    click(client, ctx, lead)
    clock.advance(minutes=3)
    click(client, ctx, lead)

    funnel = summary(ctx)["funnel"]
    assert funnel["engaged"] == 1
    assert funnel["engagement_rate"] == 1.0


def test_unique_and_duplicate_counts(client, ctx, clock):
    # Three leads from two consumers: two unique, one duplicate, with its open claim.
    new_lead(client, 1)
    new_lead(client, 2)
    clock.advance(hours=1)
    new_lead(client, 1)

    result = summary(ctx)
    assert result["funnel"]["received"] == 3
    assert result["funnel"]["unique"] == 2
    assert result["duplicates"] == {
        "count": 1,
        "rate": 0.3333,
        "open_claims": 1,
        "claimable_cents": 2800,
        "credited_cents": 0,
    }


def test_win_rate_is_never_above_one(client, ctx, clock):
    # A job that was won was booked, by phone if not through our link. So a won lead counts
    # as booked, and jobs won after a phone call cannot push the win rate past 100%.
    booked_online = new_lead(client, 1)
    phoned_one = new_lead(client, 2)
    phoned_two = new_lead(client, 3)
    deliver(ctx, clock)
    book(client, ctx, booked_online)
    close(client, phoned_one, "won", job_value=300)
    close(client, phoned_two, "won", job_value=300)

    funnel = summary(ctx)["funnel"]
    assert (funnel["booked"], funnel["won"]) == (3, 2)
    assert funnel["win_rate"] == 0.6667  # 2 won of 3 booked
    assert funnel["booking_rate"] == 1.0


def test_funnel_is_cumulative_so_each_stage_is_no_bigger_than_the_one_before(client, ctx, clock):
    # A lead counts at every stage it reached or passed. Here one job is won by phone
    # without any email going out (no address), one is booked through the link, and one
    # is only contacted: the bars must still shrink from left to right.
    won_by_phone = new_lead(client, 1, Email="")  # never emailed, never clicked, never booked online
    booked_online = new_lead(client, 2)
    new_lead(client, 3)  # contacted only
    new_lead(client, 4, Email="")  # received only
    deliver(ctx, clock)
    book(client, ctx, booked_online)
    close(client, won_by_phone, "won", job_value=900)

    stored = db_one(
        ctx, "SELECT first_contacted_at, first_engaged_at, booked_at FROM leads WHERE id = ?", (won_by_phone,)
    )
    assert stored == {"first_contacted_at": None, "first_engaged_at": None, "booked_at": None}
    funnel = summary(ctx)["funnel"]
    stages = [funnel[stage] for stage in ("received", "contacted", "engaged", "booked", "won")]
    assert stages == [4, 3, 2, 2, 1]
    assert stages == sorted(stages, reverse=True)
    assert funnel["contact_rate"] == 0.75
    assert funnel["engagement_rate"] == 0.6667
    assert funnel["booking_rate"] == 0.5
    assert funnel["win_rate"] == 0.5


def test_a_job_won_by_phone_counts_as_booked_in_every_number(client, ctx, clock):
    # One definition of "booked" everywhere: headline, money, the slices, by day and by hour.
    won_by_phone = new_lead(client, 1)
    new_lead(client, 2)
    deliver(ctx, clock)
    close(client, won_by_phone, "won", job_value=500)

    result = summary(ctx)
    assert result["funnel"]["booked"] == 1
    assert result["money"]["cost_per_booking_cents"] == 5600  # 2 x $28 for one booked job
    assert result["money"]["cost_per_won_job_cents"] == 5600
    for section in ("by_task", "by_tenant", "by_postal_code", "by_format"):
        row = result[section][0]
        assert (row["booked"], row["booking_rate"], row["cost_per_booking_cents"]) == (1, 0.5, 5600), section
    assert result["by_day"] == [{"day": "2026-11-12", "received": 2, "booked": 1}]
    assert result["by_hour"][10] == {"hour": 10, "received": 2, "booked": 1}
    # The two numbers that need a booking TIME keep using real online bookings.
    assert result["speed"]["time_to_book_p50_minutes"] is None
    assert result["attribution"] == {"booked_after_intro_only": 0, "booked_after_followup": 0}


# --- speed to lead -----------------------------------------------------------------------


def post_and_deliver_after(client, ctx, clock, n: int, seconds: float) -> str:
    """Post a lead and have its intro go out exactly ``seconds`` later."""
    lead_id = new_lead(client, n)
    deliver(ctx, clock, seconds)
    return lead_id


def test_speed_to_lead_percentiles_from_three_known_delays(client, ctx, mailer, clock):
    # Intros delivered 2 s, 10 s and 400 s after receipt: median 10 s, worst 400 s.
    for n, delay in ((1, 2), (2, 10), (3, 400)):
        lead_id = post_and_deliver_after(client, ctx, clock, n, delay)
        row = db_one(ctx, "SELECT received_at, first_contacted_at FROM leads WHERE id = ?", (lead_id,))
        assert analytics.seconds_between(row["received_at"], row["first_contacted_at"]) == delay
    assert len(mailer.sent) == 3

    speed = summary(ctx)["speed"]
    assert speed["speed_to_lead_p50_seconds"] == 10
    assert speed["speed_to_lead_p90_seconds"] == 322.0
    assert speed["speed_to_lead_max_seconds"] == 400
    assert speed["contacted_within_1_min"] == 0.6667  # 2 of 3
    assert speed["contacted_within_5_min"] == 0.6667  # 400 s is over five minutes
    assert speed["time_to_book_p50_minutes"] is None


def test_lead_never_contacted_counts_against_the_within_n_minutes_ratios(client, ctx, clock):
    # The denominators are ALL leads: one we never reached lowers both shares.
    for n, delay in ((1, 2), (2, 10), (3, 400)):
        post_and_deliver_after(client, ctx, clock, n, delay)
    new_lead(client, 4, Email="")  # no address: never contacted

    speed = summary(ctx)["speed"]
    assert speed["contacted_within_1_min"] == 0.5  # 2 of 4
    assert speed["contacted_within_5_min"] == 0.5
    # The percentiles describe the leads that were contacted, so they do not move.
    assert speed["speed_to_lead_p50_seconds"] == 10
    assert speed["speed_to_lead_max_seconds"] == 400


def test_within_one_and_five_minute_boundaries_are_inclusive(client, ctx, clock):
    # Exactly 60 s counts as "within 1 minute"; exactly 300 s as "within 5 minutes".
    post_and_deliver_after(client, ctx, clock, 1, 60)
    post_and_deliver_after(client, ctx, clock, 2, 61)
    post_and_deliver_after(client, ctx, clock, 3, 300)
    post_and_deliver_after(client, ctx, clock, 4, 301)

    speed = summary(ctx)["speed"]
    assert speed["contacted_within_1_min"] == 0.25
    assert speed["contacted_within_5_min"] == 0.75


def test_speed_to_lead_is_measured_to_the_first_email_only(client, ctx, mailer, clock):
    # A follow-up an hour later does not change when the lead was FIRST contacted.
    post_and_deliver_after(client, ctx, clock, 1, 5)
    deliver(ctx, clock, 61 * 60)  # follow-up 1 goes out
    assert len(mailer.sent) == 2

    speed = summary(ctx)["speed"]
    assert speed["speed_to_lead_p50_seconds"] == 5
    assert speed["speed_to_lead_max_seconds"] == 5


def test_time_to_book_p50_minutes(client, ctx, clock):
    # Bookings 30 and 90 minutes after receipt: the median time to book is 60 minutes.
    first = new_lead(client, 1)
    second = new_lead(client, 2)
    never_books = new_lead(client, 3)
    deliver(ctx, clock)
    clock.advance(minutes=30, seconds=-1)
    book(client, ctx, first)
    clock.advance(minutes=60)
    book(client, ctx, second)

    speed = summary(ctx)["speed"]
    assert speed["time_to_book_p50_minutes"] == 60.0
    assert db_one(ctx, "SELECT booked_at FROM leads WHERE id = ?", (never_books,))["booked_at"] is None


def test_time_to_book_for_a_single_booking(client, ctx, clock):
    # One booking 45 minutes after the lead arrived.
    lead = new_lead(client, 1)
    deliver(ctx, clock)
    clock.advance(minutes=45, seconds=-1)
    book(client, ctx, lead)
    assert summary(ctx)["speed"]["time_to_book_p50_minutes"] == 45.0


# --- money -------------------------------------------------------------------------------


def test_spend_uses_default_fees_for_angi_ads_and_payload_fees_for_angi_leads(client, ctx, clock):
    # Angi Ads carries no price (the account default applies); Angi Leads states the fee per lead.
    new_lead(client, 1)  # Sparkle, $28.00 default
    new_lead(client, 2, ALAccountId=SUMMIT_ADS_ACCOUNT)  # Summit, $45.00 default
    send(client, leads_payload(fee=25.67))  # Summit via Angi Leads, fee in the payload

    money = summary(ctx)["money"]
    assert money["spend_cents"] == 2800 + 4500 + 2567
    assert money["credited_cents"] == 0
    assert money["net_spend_cents"] == 9867
    assert money["cost_per_lead_cents"] == 3289  # 9867 / 3
    assert money["cost_per_booking_cents"] is None  # nothing booked yet
    assert money["cost_per_won_job_cents"] is None
    assert money["revenue_cents"] == 0
    assert money["return_on_spend"] == 0.0


def test_lead_with_unknown_fee_adds_nothing_to_spend(client, ctx, clock):
    # No fee in the payload and no default on the mapping: spend is not guessed.
    send(client, leads_payload(fee=None))
    money = summary(ctx)["money"]
    assert money["spend_cents"] == 0
    assert money["unpriced_leads"] == 1  # ...but the reader is told the $0 is not a real price
    assert money["cost_per_lead_cents"] == 0
    assert money["return_on_spend"] is None  # nothing spent: return is undefined, not infinite


def test_unpriced_leads_counts_only_leads_whose_fee_is_unknown(client, ctx, clock):
    # A free lead (fee 0) has a known price; only a missing fee is "unpriced".
    new_lead(client, 1)  # $28 account default
    send(client, leads_payload(fee=0))
    send(client, leads_payload(fee=None))
    send(client, leads_payload(fee=None))

    money = summary(ctx)["money"]
    assert money["unpriced_leads"] == 2
    assert money["spend_cents"] == 2800


def test_credited_claim_reduces_net_spend_and_unit_costs(client, ctx, clock):
    # A duplicate was charged ($28) and then credited back: net spend drops by the credit.
    original = new_lead(client, 1)
    other = new_lead(client, 2)
    clock.advance(hours=1)
    duplicate = send(client, ads_payload(**consumer(1)))
    deliver(ctx, clock)
    book(client, ctx, original)
    book(client, ctx, other)

    before = summary(ctx)["money"]
    assert before["spend_cents"] == 8400
    assert before["net_spend_cents"] == 8400
    assert before["cost_per_lead_cents"] == 2800
    assert before["cost_per_booking_cents"] == 4200

    credited = client.post(
        f"/admin/api/claims/{duplicate['duplicate']['creditClaimId']}/status",
        headers=ADMIN,
        json={"status": "credited"},
    )
    assert credited.status_code == 200

    after = summary(ctx)
    assert after["money"]["spend_cents"] == 8400  # what was charged does not change
    assert after["money"]["credited_cents"] == 2800
    assert after["money"]["net_spend_cents"] == 5600
    assert after["money"]["cost_per_lead_cents"] == 1867  # 5600 / 3
    assert after["money"]["cost_per_booking_cents"] == 2800  # 5600 / 2
    assert after["duplicates"]["credited_cents"] == 2800
    assert after["duplicates"]["open_claims"] == 0
    assert after["duplicates"]["claimable_cents"] == 0
    # The slices use the same net figure as the headline, so the tables agree with the tiles.
    for section in ("by_tenant", "by_task"):
        row = after[section][0]
        assert (row["spend_cents"], row["net_spend_cents"], row["cost_per_booking_cents"]) == (8400, 5600, 2800)


def test_partial_credit_reduces_net_spend_by_the_amount_actually_credited(client, ctx, clock):
    # Angi credited $10 of a $28 fee: only $10 comes off.
    new_lead(client, 1)
    clock.advance(hours=1)
    duplicate = send(client, ads_payload(**consumer(1)))
    client.post(
        f"/admin/api/claims/{duplicate['duplicate']['creditClaimId']}/status",
        headers=ADMIN,
        json={"status": "credited", "credited": 10},
    )

    money = summary(ctx)["money"]
    assert money["credited_cents"] == 1000
    assert money["net_spend_cents"] == 5600 - 1000


@pytest.mark.parametrize("status", ["submitted", "denied", "dismissed"])
def test_claim_that_is_not_credited_does_not_reduce_spend(client, ctx, clock, status):
    # Only money actually returned counts; a filed or refused claim changes nothing.
    new_lead(client, 1)
    clock.advance(hours=1)
    duplicate = send(client, ads_payload(**consumer(1)))
    client.post(
        f"/admin/api/claims/{duplicate['duplicate']['creditClaimId']}/status", headers=ADMIN, json={"status": status}
    )

    result = summary(ctx)
    assert result["money"]["credited_cents"] == 0
    assert result["money"]["net_spend_cents"] == 5600
    assert result["duplicates"]["open_claims"] == (1 if status == "submitted" else 0)


def test_revenue_and_return_on_spend_come_only_from_won_leads(client, ctx, clock):
    # A booked lead is not revenue until the job is won; a lost lead is never revenue.
    won_big = new_lead(client, 1)
    won_small = new_lead(client, 2)
    lost = new_lead(client, 3)
    still_booked = new_lead(client, 4)
    deliver(ctx, clock)
    for lead in (won_big, won_small, lost, still_booked):
        book(client, ctx, lead)
    close(client, won_big, "won", job_value=1200.50)
    close(client, won_small, "won", job_value=450)
    close(client, lost, "lost", job_value=999, reason="price")  # a value on a lost job is ignored

    money = summary(ctx)["money"]
    assert money["spend_cents"] == 4 * 2800
    assert money["revenue_cents"] == 120050 + 45000
    assert money["return_on_spend"] == round(165050 / 11200, 4)
    assert money["cost_per_booking_cents"] == 2800  # 11200 / 4 booked
    assert money["cost_per_won_job_cents"] == 5600  # 11200 / 2 won


def test_won_lead_without_a_job_value_adds_no_revenue(client, ctx, clock):
    # "Won" with no amount recorded is a win with unknown revenue, counted as zero.
    lead = new_lead(client, 1)
    close(client, lead, "won")
    result = summary(ctx)
    assert result["funnel"]["won"] == 1
    assert result["money"]["revenue_cents"] == 0
    assert result["money"]["return_on_spend"] == 0.0


def test_money_ratios_are_none_where_the_denominator_is_zero(client, ctx, clock):
    # Leads but no bookings and no wins: per-booking and per-win costs are undefined.
    new_lead(client, 1)
    new_lead(client, 2)
    money = summary(ctx)["money"]
    assert money["cost_per_lead_cents"] == 2800
    assert money["cost_per_booking_cents"] is None
    assert money["cost_per_won_job_cents"] is None


# --- attribution -------------------------------------------------------------------------


def test_attribution_separates_bookings_after_a_follow_up_from_intro_only(client, ctx, mailer, clock):
    # Which touch produced the booking: the intro alone, or the nudge an hour later?
    from_intro = new_lead(client, 1)
    after_nudge = new_lead(client, 2)
    deliver(ctx, clock)
    clock.advance(minutes=5)
    book(client, ctx, from_intro)

    deliver(ctx, clock, 61 * 60)  # follow-up 1 is now due for the lead that has not booked
    followups = [m for m in mailer.sent if m.subject.startswith("Still need help")]
    assert [m.to for m in followups] == ["homeowner2@example.com"]
    clock.advance(minutes=10)
    book(client, ctx, after_nudge)

    result = summary(ctx)
    assert result["funnel"]["booked"] == 2
    assert result["attribution"] == {"booked_after_intro_only": 1, "booked_after_followup": 1}


def test_follow_up_queued_but_not_sent_before_booking_is_not_credited(client, ctx, clock):
    # Only a follow-up that actually went out BEFORE the booking can take the credit.
    lead = new_lead(client, 1)
    deliver(ctx, clock)
    clock.advance(minutes=30)  # the 60-minute follow-up is still pending
    book(client, ctx, lead)

    assert summary(ctx)["attribution"] == {"booked_after_intro_only": 1, "booked_after_followup": 0}


def test_attribution_counts_a_lead_once_even_after_two_follow_ups(client, ctx, mailer, clock):
    # Two nudges before booking is still one booking "after follow-up".
    lead = new_lead(client, 1)
    deliver(ctx, clock)
    deliver(ctx, clock, 61 * 60)
    deliver(ctx, clock, 24 * 3600)
    assert len([m for m in mailer.sent if m.subject.startswith("Still need help")]) == 2
    clock.advance(minutes=10)
    book(client, ctx, lead)

    assert summary(ctx)["attribution"] == {"booked_after_intro_only": 0, "booked_after_followup": 1}


def test_attribution_ignores_leads_that_have_not_booked(client, ctx, clock):
    # A follow-up sent to someone who never booked is not a booking of either kind.
    new_lead(client, 1)
    deliver(ctx, clock)
    deliver(ctx, clock, 61 * 60)
    assert summary(ctx)["attribution"] == {"booked_after_intro_only": 0, "booked_after_followup": 0}


# --- breakdowns --------------------------------------------------------------------------


def test_by_task_groups_by_requested_service(client, ctx, clock):
    # Each service gets its own row with volume, bookings, spend and revenue.
    cleaning_one = new_lead(client, 1, Category="Indianapolis – House Cleaning")
    new_lead(client, 2, Category="Indianapolis – House Cleaning")
    new_lead(client, 3, Category="Indianapolis – Window Cleaning")
    deliver(ctx, clock)
    book(client, ctx, cleaning_one)
    close(client, cleaning_one, "won", job_value=450)

    assert summary(ctx)["by_task"] == [
        {
            "key": "House Cleaning",
            "received": 2,
            "booked": 1,
            "booking_rate": 0.5,
            "spend_cents": 5600,
            "net_spend_cents": 5600,
            "cost_per_booking_cents": 5600,
            "revenue_cents": 45000,
        },
        {
            "key": "Window Cleaning",
            "received": 1,
            "booked": 0,
            "booking_rate": 0.0,
            "spend_cents": 2800,
            "net_spend_cents": 2800,
            "cost_per_booking_cents": None,
            "revenue_cents": 0,
        },
    ]


def test_breakdown_rows_are_ordered_by_volume_then_name(client, ctx, clock):
    # Biggest group first; ties in alphabetical order so the table does not jump around.
    new_lead(client, 1, Category="Zebra Care")
    new_lead(client, 2, Category="Apple Picking")
    new_lead(client, 3, Category="Mowing")
    new_lead(client, 4, Category="Mowing")

    assert [row["key"] for row in summary(ctx)["by_task"]] == ["Mowing", "Apple Picking", "Zebra Care"]


def test_missing_group_value_is_labelled_not_provided(client, ctx, clock):
    # A lead without a service still shows up, under an explicit label.
    new_lead(client, 1, Category="")
    assert [row["key"] for row in summary(ctx)["by_task"]] == ["(not provided)"]


def test_by_tenant_groups_by_business_name(client, ctx, clock):
    # One row per tenant, with that tenant's own spend.
    new_lead(client, 1)
    new_lead(client, 2)
    new_lead(client, 3, ALAccountId=SUMMIT_ADS_ACCOUNT)
    new_lead(client, 4, ALAccountId=BLUELINE_ACCOUNT)

    rows = {row["key"]: row for row in summary(ctx)["by_tenant"]}
    assert set(rows) == {"Sparkle Home Cleaning", "Summit Heating & Air", "BlueLine Plumbing"}
    assert (rows["Sparkle Home Cleaning"]["received"], rows["Sparkle Home Cleaning"]["spend_cents"]) == (2, 5600)
    assert (rows["Summit Heating & Air"]["received"], rows["Summit Heating & Air"]["spend_cents"]) == (1, 4500)
    assert (rows["BlueLine Plumbing"]["received"], rows["BlueLine Plumbing"]["spend_cents"]) == (1, 3900)
    assert summary(ctx)["by_tenant"][0]["key"] == "Sparkle Home Cleaning"  # most leads first


def address(postal_code) -> dict:
    return {
        "AddressFirstLine": "1 Test St.",
        "AddressSecondLine": "",
        "City": "Indianapolis",
        "State": "IN",
        "PostalCode": postal_code,
    }


def test_by_postal_code_groups_by_zip(client, ctx, clock):
    # Where the demand is: one row per ZIP code.
    new_lead(client, 1, PostalAddress=address("46203"))
    new_lead(client, 2, PostalAddress=address("46203"))
    new_lead(client, 3, PostalAddress=address("46220"))

    rows = summary(ctx)["by_postal_code"]
    assert [(row["key"], row["received"]) for row in rows] == [("46203", 2), ("46220", 1)]


def test_by_postal_code_is_limited_to_the_top_ten(client, ctx, clock):
    # The ZIP table is a top-10; the busiest ZIP must survive the cut.
    for n in range(12):
        new_lead(client, n + 1, PostalAddress=address(f"462{n:02d}"))
    new_lead(client, 99, PostalAddress=address("46211"))  # a second lead for one ZIP

    rows = summary(ctx)["by_postal_code"]
    assert len(rows) == 10
    assert rows[0] == {
        "key": "46211",
        "received": 2,
        "booked": 0,
        "booking_rate": 0.0,
        "spend_cents": 5600,
        "net_spend_cents": 5600,
        "cost_per_booking_cents": None,
        "revenue_cents": 0,
    }


def test_by_format_separates_the_two_angi_feeds(client, ctx, clock):
    # Angi Ads and Angi Leads are reported side by side.
    new_lead(client, 1)
    new_lead(client, 2)
    send(client, leads_payload())

    rows = {row["key"]: row["received"] for row in summary(ctx)["by_format"]}
    assert rows == {"angi_ads": 2, "angi_leads": 1}


# --- by_day / by_hour in the tenant's local time --------------------------------------------


def test_by_day_and_by_hour_use_the_tenants_local_time_in_winter(client, ctx, clock):
    # 03:30 UTC on Nov 13 is 20:30 on Nov 12 in Denver (MST, UTC-7): previous local day, hour 20.
    clock.set(datetime(2026, 11, 13, 3, 30, tzinfo=UTC))
    new_lead(client, 1, ALAccountId=SUMMIT_ADS_ACCOUNT)

    result = summary(ctx)
    assert result["by_day"] == [{"day": "2026-11-12", "received": 1, "booked": 0}]
    busy = [row for row in result["by_hour"] if row["received"]]
    assert busy == [{"hour": 20, "received": 1, "booked": 0}]


def test_by_day_and_by_hour_use_the_tenants_local_time_in_summer(client, ctx, clock):
    # The same UTC time of day in July is 21:30 in Denver (MDT, UTC-6): hour 21.
    clock.set(datetime(2026, 7, 15, 3, 30, tzinfo=UTC))
    new_lead(client, 1, ALAccountId=SUMMIT_ADS_ACCOUNT)

    result = summary(ctx)
    assert result["by_day"] == [{"day": "2026-07-14", "received": 1, "booked": 0}]
    busy = [row for row in result["by_hour"] if row["received"]]
    assert busy == [{"hour": 21, "received": 1, "booked": 0}]


def test_each_lead_is_bucketed_in_its_own_tenants_timezone(client, ctx, clock):
    # Three tenants, one instant (03:30 UTC): 22:30 in Indianapolis, 21:30 in Chicago, 20:30 in Denver.
    clock.set(datetime(2026, 11, 13, 3, 30, tzinfo=UTC))
    new_lead(client, 1)  # Sparkle, America/Indiana/Indianapolis
    new_lead(client, 2, ALAccountId=BLUELINE_ACCOUNT)  # BlueLine, America/Chicago
    new_lead(client, 3, ALAccountId=SUMMIT_ADS_ACCOUNT)  # Summit, America/Denver

    result = summary(ctx)
    assert result["by_day"] == [{"day": "2026-11-12", "received": 3, "booked": 0}]
    assert {row["hour"]: row["received"] for row in result["by_hour"] if row["received"]} == {22: 1, 21: 1, 20: 1}
    assert len(result["by_hour"]) == 24
    assert [row["hour"] for row in result["by_hour"]] == list(range(24))


def test_by_day_is_sorted_and_counts_bookings_on_the_day_the_lead_arrived(client, ctx, clock):
    # Days appear in order; a booking is credited to the day (and hour) its lead was received.
    first_day = new_lead(client, 1)  # Thu Nov 12, 10:00 Indianapolis
    deliver(ctx, clock)
    clock.advance(days=1, hours=2)
    new_lead(client, 2)  # Fri Nov 13, 12:00
    new_lead(client, 3)
    clock.advance(hours=1)
    book(client, ctx, first_day)  # booked on Friday, but the lead is Thursday's

    result = summary(ctx)
    assert result["by_day"] == [
        {"day": "2026-11-12", "received": 1, "booked": 1},
        {"day": "2026-11-13", "received": 2, "booked": 0},
    ]
    assert result["by_hour"][10] == {"hour": 10, "received": 1, "booked": 1}
    assert result["by_hour"][12] == {"hour": 12, "received": 2, "booked": 0}


# --- filters -----------------------------------------------------------------------------


def test_tenant_id_filter_limits_every_section(client, ctx, clock):
    # A tenant's report contains only that tenant's leads, money and claims.
    sparkle = new_lead(client, 1)
    new_lead(client, 2, ALAccountId=SUMMIT_ADS_ACCOUNT)
    clock.advance(hours=1)
    new_lead(client, 2, ALAccountId=SUMMIT_ADS_ACCOUNT)  # a duplicate, for Summit only
    deliver(ctx, clock)
    book(client, ctx, sparkle)

    sparkle_only = summary(ctx, tenant_id="tnt_sparkle")
    assert sparkle_only["filters"]["tenant_id"] == "tnt_sparkle"
    assert sparkle_only["funnel"]["received"] == 1
    assert sparkle_only["funnel"]["booked"] == 1
    assert sparkle_only["money"]["spend_cents"] == 2800
    assert sparkle_only["duplicates"]["count"] == 0
    assert sparkle_only["duplicates"]["open_claims"] == 0
    assert [row["key"] for row in sparkle_only["by_tenant"]] == ["Sparkle Home Cleaning"]

    summit_only = summary(ctx, tenant_id="tnt_summit")
    assert summit_only["funnel"]["received"] == 2
    assert summit_only["funnel"]["booked"] == 0
    assert summit_only["money"]["spend_cents"] == 9000
    assert summit_only["duplicates"]["count"] == 1
    assert summit_only["duplicates"]["claimable_cents"] == 4500
    assert summit_only["attribution"] == {"booked_after_intro_only": 0, "booked_after_followup": 0}

    assert summary(ctx, tenant_id="tnt_blueline")["funnel"]["received"] == 0
    assert summary(ctx)["funnel"]["received"] == 3


def test_since_is_inclusive_and_until_is_exclusive(client, ctx, clock):
    # A reporting period is [since, until): a lead exactly at `since` is in, exactly at `until` is out.
    new_lead(client, 1)  # at START
    clock.advance(days=1)
    new_lead(client, 2)  # at START + 1 day
    clock.advance(days=1)
    new_lead(client, 3)  # at START + 2 days

    assert summary(ctx, since=START)["funnel"]["received"] == 3
    assert summary(ctx, since=START + timedelta(seconds=1))["funnel"]["received"] == 2
    assert summary(ctx, until=START + timedelta(days=2))["funnel"]["received"] == 2
    assert summary(ctx, since=START + timedelta(days=1), until=START + timedelta(days=2))["funnel"]["received"] == 1
    result = summary(ctx, since=START + timedelta(days=1), until=START + timedelta(days=2))
    assert result["filters"] == {
        "tenant_id": None,
        "since": "2026-11-13T15:00:00.000Z",
        "until": "2026-11-14T15:00:00.000Z",
    }


def test_days_filter_over_http_looks_back_from_now(client, ctx, clock):
    # ?days=7 means "leads received in the last seven days".
    new_lead(client, 1)
    clock.advance(days=10)
    new_lead(client, 2)
    new_lead(client, 3)

    def received(**params) -> int:
        response = client.get("/admin/api/analytics/summary", params=params, headers=ADMIN)
        assert response.status_code == 200
        return response.json()["funnel"]["received"]

    assert received() == 3
    assert received(days=7) == 2
    assert received(days=30) == 3
    assert received(days=10) == 3  # exactly ten days old is still inside (inclusive)
    assert received(days=9) == 2


def test_days_filter_also_limits_credits_and_claims(client, ctx, clock):
    # A credit belongs to the period in which its duplicate lead was received.
    new_lead(client, 1)
    clock.advance(hours=1)
    duplicate = send(client, ads_payload(**consumer(1)))
    client.post(
        f"/admin/api/claims/{duplicate['duplicate']['creditClaimId']}/status",
        headers=ADMIN,
        json={"status": "credited"},
    )
    clock.advance(days=10)
    new_lead(client, 2)

    recent = client.get("/admin/api/analytics/summary", params={"days": 7}, headers=ADMIN).json()
    assert recent["funnel"]["received"] == 1
    assert recent["money"]["credited_cents"] == 0
    assert recent["money"]["net_spend_cents"] == 2800
    everything = client.get("/admin/api/analytics/summary", headers=ADMIN).json()
    assert everything["money"]["credited_cents"] == 2800
    assert everything["money"]["net_spend_cents"] == 3 * 2800 - 2800


def test_tenant_id_filter_over_http(client, ctx, clock):
    # ?tenant_id= is passed through to the report.
    new_lead(client, 1)
    new_lead(client, 2, ALAccountId=SUMMIT_ADS_ACCOUNT)

    body = client.get("/admin/api/analytics/summary", params={"tenant_id": "tnt_summit"}, headers=ADMIN).json()
    assert body["filters"]["tenant_id"] == "tnt_summit"
    assert body["funnel"]["received"] == 1
    assert body["money"]["spend_cents"] == 4500


def test_unknown_tenant_id_gives_an_empty_report(client, ctx, clock):
    # Filtering by a tenant that does not exist is an empty result, not an error.
    new_lead(client, 1)
    body = client.get("/admin/api/analytics/summary", params={"tenant_id": "tnt_nobody"}, headers=ADMIN).json()
    assert body["funnel"]["received"] == 0
    assert body["funnel"]["contact_rate"] is None


def test_canary_tenant_is_excluded_by_default_and_included_on_request(client, ctx, clock):
    # Synthetic probe leads must not inflate real conversion numbers.
    new_lead(client, 1)
    canary = send(client, ads_payload(**consumer(2), ALAccountId=CANARY_ACCOUNT))
    assert canary["tenant"]["id"] == "tnt_canary"
    deliver(ctx, clock)

    default = summary(ctx)
    assert default["funnel"]["received"] == 1
    assert default["funnel"]["contacted"] == 1
    assert [row["key"] for row in default["by_tenant"]] == ["Sparkle Home Cleaning"]

    with_test = summary(ctx, include_test=True)
    assert with_test["funnel"]["received"] == 2
    assert with_test["funnel"]["contacted"] == 2
    assert {row["key"] for row in with_test["by_tenant"]} == {"Sparkle Home Cleaning", "Synthetic Canary Co."}

    over_http = client.get("/admin/api/analytics/summary", headers=ADMIN).json()
    assert over_http["funnel"]["received"] == 1


def test_test_tenant_duplicates_and_claims_are_excluded_by_default(client, ctx, clock):
    # Whatever a test tenant has on record (here: a duplicate and its claim) is not a real
    # credit opportunity. (Intake no longer flags the probe's own leads as duplicates, so the
    # pair is created for a normal tenant, which is then marked as a test tenant.)
    new_lead(client, 1)
    clock.advance(minutes=5)
    assert send(client, ads_payload(**consumer(1)))["duplicate"] is not None
    with ctx.db.transaction() as conn:  # there is no API to turn a tenant into a test tenant
        conn.execute("UPDATE tenants SET is_test = 1 WHERE id = 'tnt_sparkle'")

    assert summary(ctx)["duplicates"]["count"] == 0
    assert summary(ctx)["duplicates"]["open_claims"] == 0
    assert summary(ctx, include_test=True)["duplicates"]["count"] == 1
    assert summary(ctx, include_test=True)["duplicates"]["open_claims"] == 1


# --- the endpoint ------------------------------------------------------------------------


def test_summary_endpoint_structure(client, ctx, clock):
    # The dashboard depends on these sections being present.
    new_lead(client, 1)
    response = client.get("/admin/api/analytics/summary", headers=ADMIN)
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "filters",
        "funnel",
        "speed",
        "money",
        "duplicates",
        "attribution",
        "by_task",
        "by_tenant",
        "by_postal_code",
        "by_format",
        "by_day",
        "by_hour",
    }
    assert len(body["by_hour"]) == 24


def test_summary_endpoint_requires_the_admin_token(client):
    # Revenue and conversion numbers are not public.
    assert client.get("/admin/api/analytics/summary").status_code == 401


def test_summary_endpoint_rejects_a_non_numeric_days_value(client):
    # ?days=abc is a client error.
    assert client.get("/admin/api/analytics/summary", params={"days": "abc"}, headers=ADMIN).status_code == 422


@pytest.mark.parametrize("days", [3651, 99999999, -1, 10**30])
def test_summary_endpoint_rejects_a_days_value_outside_zero_to_ten_years(client, days):
    # ?days=99999999 reaches back before year 1 and a negative one looks into the future:
    # both are bad requests, never a server error.
    response = client.get("/admin/api/analytics/summary", params={"days": days}, headers=ADMIN)
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["query", "days"]


def test_summary_endpoint_days_zero_means_all_time_and_ten_years_is_allowed(client, ctx, clock):
    # 0 is "no filter" (what the dashboard's "all time" sends); 3650 is the largest window.
    new_lead(client, 1)
    clock.advance(days=400)
    new_lead(client, 2)

    def received(days) -> int:
        response = client.get("/admin/api/analytics/summary", params={"days": days}, headers=ADMIN)
        assert response.status_code == 200
        return response.json()["funnel"]["received"]

    assert received(0) == 2
    assert received(3650) == 2
    assert received(30) == 1
