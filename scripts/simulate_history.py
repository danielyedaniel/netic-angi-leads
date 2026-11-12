"""Fill a database with realistic lead HISTORY, so the dashboard, analytics, credits and
monitoring pages have something to show in a demo.

    uv run python scripts/simulate_history.py                 # 160 leads over the last 14 days
    uv run python scripts/simulate_history.py --leads 300 --days 21 --seed 3
    uv run python scripts/simulate_history.py --database /tmp/demo.db

How it works
    Nothing is faked in the database: the script drives the real application code with
    a clock it controls. It starts the clock ``--days`` ago and steps it forward to
    "now", and at each step does what would really have happened at that moment:

    * a lead arrives         -> ``ctx.ingest.receive`` (the code behind the webhook)
    * an email falls due     -> ``ctx.worker.deliver_due`` (the code of the background worker)
    * a consumer opens the booking link -> ``booking.record_link_click``
    * ... and picks a time   -> ``booking.book_appointment``
    * a job is won or lost   -> ``queries.record_outcome``
    * a credit is requested  -> ``credits.set_status``
    * once an hour           -> ``ctx.worker.run_maintenance`` (the monitoring checks)

    So every row, timestamp, alert and analytics number is one the application produced
    itself.

It can never send a real email
    * It runs entirely in this process, with the in-memory email provider. No SMTP, no
      HTTP, no server.
    * Every address is @example.com.
    * At the very end every message still waiting in the outbox is cancelled (reason
      "simulation") and the script checks that none is left. When the real server later
      starts on this database, there is nothing queued for a simulated lead.

What it generates (``--seed`` makes a run reproducible)
    * about ``--leads`` leads for the three demo tenants, in both Angi formats, more
      during business hours than at night and fewer at weekends;
    * a realistic funnel: emailed within seconds, roughly 45% open the booking link,
      30% book, some jobs won ($150 - $9,000) and some lost;
    * about 6% repeat requests from the same consumer through the same Angi account
      (duplicate leads with credit claims, a few of them already submitted or
      credited). One in four of them comes from another email address of the same
      person: still a duplicate by phone, but answered with the normal introduction,
      because that address has never heard from the business;
    * two leads without an email address, one lead in a drifted payload format, and one
      lead from an Angi account nobody mapped, left in quarantine.

It refuses to touch a database that already contains leads unless ``--force`` is given.
``--force`` adds a second batch on top (use another ``--seed``, or the same people
come back).
"""

from __future__ import annotations

import argparse
import heapq
import json
import logging
import random
import re
import sys
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

# Allow "python scripts/simulate_history.py" from the project root to import the app.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import credits, outbox, queries  # noqa: E402
from app.booking import BookingError, available_slots, book_appointment, record_link_click  # noqa: E402
from app.config import Settings  # noqa: E402
from app.context import AppContext, build_context  # noqa: E402
from app.mailer.providers import MemoryEmailProvider  # noqa: E402
from app.repo import get_lead, get_tenant  # noqa: E402
from app.seed import seed_demo  # noqa: E402
from app.util import iso, parse_iso, utcnow  # noqa: E402

# --- how the simulated consumers behave --------------------------------------------------
CLICK_RATE = 0.45  # share of emailed leads who open the booking link
BOOK_RATE_AFTER_CLICK = 0.67  # ... of whom this share book (0.45 * 0.67 = 30% of leads)
WON_RATE_AFTER_BOOKING = 0.60
LOST_RATE_AFTER_BOOKING = 0.15  # the rest are still open
LOST_RATE_WITHOUT_BOOKING = 0.12
DUPLICATE_RATE = 0.06  # share of leads that are a repeat request from the same consumer
OTHER_ADDRESS_EVERY = 4  # one repeat request in this many comes from another address of the same person
ANGI_LEADS_SHARE = 0.45  # for tenants that buy both feeds: share arriving in the Angi Leads format
LEADS_WITHOUT_EMAIL = 2
EMAIL_HICCUPS = 2  # leads whose intro email needs one retry (a provider timeout)

# Relative number of leads per local hour of the day (business hours are the busiest)...
HOUR_WEIGHTS = (1, 1, 1, 1, 1, 2, 4, 8, 12, 14, 14, 13, 12, 12, 11, 10, 9, 9, 8, 7, 5, 4, 2, 1)
# ... and per weekday, Monday first (weekends are quieter).
WEEKDAY_WEIGHTS = (1.0, 1.0, 1.0, 1.0, 1.0, 0.5, 0.35)

MIN_LEADS, MAX_LEADS = 10, 400  # the 555-01xx phone range holds 200 numbers per tenant
# The sample payloads use the same fictional range. A simulated consumer must not share a
# number with one of them, or a sample sent after "make simulate" (in the demo: step 2)
# would be flagged as a repeat request from a simulated stranger.
SAMPLES_DIR = Path(__file__).resolve().parent.parent / "samples"
SAMPLE_PHONES = frozenset(
    re.sub(r"\D", "", number)  # "(317) 555-0184" and 3175550184 are the same number
    for sample in SAMPLES_DIR.glob("*.*")
    if sample.suffix in (".json", ".txt")
    for number in re.findall(r"\(?\b\d{3}\)?[ .-]?555[ .-]?01\d{2}\b", sample.read_text(encoding="utf-8"))
)
UNMAPPED_ACCOUNT_ID = "000000"
# What the stored request looks like it came from (203.0.113.0/24 is reserved for examples).
ANGI_HEADERS = {"content-type": "application/json", "user-agent": "Angi-Lead-Feed/1.0 (simulated)"}
ANGI_ADDRESS = "203.0.113.10"


class SimulationRefused(Exception):
    """The simulation will not run against this database."""


# --- reference data: who the tenants are and what their customers ask for ---------------


@dataclass(frozen=True)
class Service:
    name: str
    job_low: int  # what a won job is worth, in dollars
    job_high: int
    requests: tuple[str, ...]  # things a consumer writes when asking for it


@dataclass(frozen=True)
class TenantProfile:
    tenant_id: str
    company: str
    share: float  # share of all simulated leads
    ads_account: str  # ALAccountId (Angi Ads feed)
    leads_account: int | None  # spEntityId (Angi Leads feed); None = buys Angi Ads only
    leads_fee: tuple[float, float]  # price range of an Angi Leads lead, in dollars
    city: str
    state: str
    timezone: str
    area_codes: tuple[str, str]
    zips: tuple[str, ...]
    streets: tuple[str, ...]
    services: tuple[Service, ...]


TENANTS = (
    TenantProfile(
        tenant_id="tnt_sparkle",
        company="Sparkle Home Cleaning",
        share=0.30,
        ads_account="123456",
        leads_account=None,
        leads_fee=(0, 0),
        city="Indianapolis",
        state="IN",
        timezone="America/Indiana/Indianapolis",
        area_codes=("317", "463"),
        zips=("46202", "46203", "46205", "46208", "46220", "46240"),
        streets=("N Meridian St", "E 56th St", "Broadway St", "N College Ave", "Carrollton Ave", "E 46th St"),
        services=(
            Service("House Cleaning", 150, 420, (
                "Looking for recurring house cleaning every two weeks. Three bedrooms, two baths.",
                "We need a regular cleaner for a 1,800 sq ft home. We have one cat.",
            )),
            Service("Deep Cleaning", 220, 650, (
                "One-time deep clean before family visits, including inside the oven and fridge.",
                "The house has not had a proper clean in a while. Kitchen and bathrooms need the most work.",
            )),
            Service("Move-Out Cleaning", 250, 700, (
                "Moving out at the end of the month and need the apartment cleaned for the walkthrough.",
                "Empty four bedroom house needs a full move-out clean, including the garage.",
            )),
            Service("Carpet Cleaning", 150, 480, (
                "Three rooms of carpet and a staircase. There are a few pet stains.",
                "Steam cleaning for the living room and hallway carpet.",
            )),
            Service("Window Cleaning", 180, 520, (
                "Inside and outside windows on a two story house, about 22 windows.",
                "Exterior windows and screens before winter.",
            )),
        ),
    ),
    TenantProfile(
        tenant_id="tnt_summit",
        company="Summit Heating & Air",
        share=0.38,
        ads_account="200001",
        leads_account=12345678,
        leads_fee=(28.0, 75.0),
        city="Denver",
        state="CO",
        timezone="America/Denver",
        area_codes=("303", "720"),
        zips=("80205", "80206", "80210", "80211", "80220", "80230"),
        streets=("Larimer St", "Gaylord St", "S Pearl St", "Zuni St", "E 17th Ave", "Tennyson St"),
        services=(
            Service("Heating & Furnace Repair", 180, 1400, (
                "Furnace runs but blows cold air. It was last serviced about three years ago.",
                "The furnace keeps shutting off after a few minutes and the house will not get above 62.",
            )),
            Service("Central A/C - Repair", 200, 1600, (
                "The AC runs all day but the house never cools down. The outdoor unit is buzzing.",
                "Water is dripping from the air handler in the attic.",
            )),
            Service("Furnace Installation", 3800, 9000, (
                "Replacing a 20 year old furnace before winter. Would like a quote for a high-efficiency unit.",
                "The inspector said the heat exchanger is cracked. We need a replacement furnace.",
            )),
            Service("Thermostat Installation", 150, 450, (
                "Want a smart thermostat installed. I already bought the unit.",
                "The old thermostat stopped responding. Please replace it with a programmable one.",
            )),
            Service("Duct Cleaning", 350, 900, (
                "Just finished a remodel and there is drywall dust in every vent.",
                "The ducts have not been cleaned in ten years and we have allergies.",
            )),
        ),
    ),
    TenantProfile(
        tenant_id="tnt_blueline",
        company="BlueLine Plumbing",
        share=0.32,
        ads_account="200002",
        leads_account=567890123,
        leads_fee=(22.0, 60.0),
        city="Austin",
        state="TX",
        timezone="America/Chicago",
        area_codes=("512", "737"),
        zips=("78702", "78704", "78723", "78745", "78751", "78757"),
        streets=("Bouldin Ave", "Canterbury St", "Duval St", "Manchaca Rd", "E 12th St", "Burnet Rd"),
        services=(
            Service("Water Heater Repair", 220, 900, (
                "No hot water since this morning. The pilot light will not stay lit.",
                "The water heater is making a popping noise and the water is only lukewarm.",
            )),
            Service("Water Heater Installation", 1400, 3800, (
                "The 12 year old water heater is leaking from the bottom. Looking to replace it.",
                "Want to switch from a tank to a tankless water heater.",
            )),
            Service("Drain Clearing", 150, 480, (
                "Kitchen sink drains very slowly and the disposal hums but does not spin.",
                "The main bathroom tub backs up whenever the washing machine drains.",
            )),
            Service("Leak Repair", 190, 1100, (
                "There is a wet spot on the ceiling under the upstairs bathroom.",
                "The outdoor faucet leaks inside the wall when it is turned on.",
            )),
            Service("Toilet Repair", 150, 420, (
                "The toilet runs constantly and rocks a little at the base.",
                "Second bathroom toilet will not flush properly.",
            )),
            Service("Sewer Line Repair", 2500, 8500, (
                "Sewage smell in the yard and slow drains everywhere. A camera inspection found roots.",
                "The city says the blockage is on our side of the sewer line.",
            )),
        ),
    ),
)
PROFILES = {profile.tenant_id: profile for profile in TENANTS}

FIRST_NAMES = (
    "Aaron", "Alicia", "Andre", "Bianca", "Caleb", "Carmen", "Daniel", "Deborah", "Elena", "Eric",
    "Fatima", "Gabriel", "Grace", "Hector", "Imani", "Isaac", "Jasmine", "Joel", "Karen", "Kevin",
    "Laura", "Liam", "Monica", "Nathan", "Nina", "Omar", "Paula", "Quentin", "Rachel", "Samuel",
    "Tanya", "Tyler", "Uma", "Victor", "Wendy", "Xavier", "Yolanda", "Zachary", "Sofia", "Marcus",
)
LAST_NAMES = (
    "Adams", "Baker", "Castillo", "Dawson", "Ellis", "Foster", "Garcia", "Hughes", "Ibarra", "Jensen",
    "Kim", "Lopez", "Mitchell", "Novak", "Ortiz", "Patel", "Quinn", "Rivera", "Schultz", "Turner",
    "Underwood", "Vargas", "Walker", "Xu", "Young", "Zimmerman", "Bennett", "Coleman", "Delgado", "Fisher",
    "Grant", "Hoffman", "Iverson", "Jordan", "Keller", "Larson", "Morales", "Nguyen", "Owens", "Price",
)
URGENCIES = ("Emergency", "Within 48 Hours", "This Week", "Within 2 Weeks", "Flexible")
URGENCY_WEIGHTS = (1, 3, 5, 2, 2)
LOST_AFTER_BOOKING = (
    "Customer cancelled the appointment",
    "Estimate given, customer chose another company",
    "Nobody home at the appointment",
)
LOST_WITHOUT_BOOKING = (
    "No response after follow-ups",
    "Hired another company",
    "Outside our service area",
    "Only wanted a price over the phone",
)


# --- the plan: who arrives when, and what they go on to do -------------------------------


@dataclass
class Person:
    first_name: str
    last_name: str
    email: str
    phone: str
    address: str
    postal_code: str


@dataclass
class PlannedLead:
    at: datetime  # when Angi delivers it
    profile: TenantProfile
    person: Person
    service: Service
    payload: dict[str, Any]
    # What this consumer goes on to do (None = does not happen).
    clicks_after: timedelta | None = None  # opens the booking link this long after arriving
    books_after: timedelta | None = None  # ... and books this long after opening it
    closes_as: str | None = None  # won | lost
    closes_after: timedelta | None = None  # after the appointment (booked) or after arriving (not booked)
    close_reason: str | None = None
    job_value_cents: int | None = None
    is_repeat: bool = False  # the same consumer asking again: Angi bills it as a new lead
    claim_steps: list[tuple[timedelta, str, str]] = field(default_factory=list)  # (after arriving, status, note)
    email_hiccup: bool = False
    lead_id: str | None = None  # filled in when the application has stored it


def new_person(rng: random.Random, profile: TenantProfile, phones: list[str], used_emails: set[str]) -> Person:
    first, last = rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)
    email = f"{first}.{last}@example.com".lower()
    number = 2
    while email in used_emails:  # two different people can share a name
        email = f"{first}.{last}{number}@example.com".lower()
        number += 1
    used_emails.add(email)
    if not phones:
        raise SimulationRefused(f"ran out of 555-01xx phone numbers for {profile.company}; use fewer --leads")
    return Person(
        first_name=first,
        last_name=last,
        email=email,
        phone=phones.pop(),
        address=f"{rng.randint(100, 9899)} {rng.choice(profile.streets)}",
        postal_code=rng.choice(profile.zips),
    )


def ads_payload(rng: random.Random, profile: TenantProfile, person: Person, service: Service) -> dict[str, Any]:
    """An Angi Ads lead (PascalCase, nested address, no price)."""
    return {
        "FirstName": person.first_name,
        "LastName": person.last_name,
        "PhoneNumber": person.phone,
        "PostalAddress": {
            "AddressFirstLine": person.address,
            "AddressSecondLine": "",
            "City": profile.city,
            "State": profile.state,
            "PostalCode": person.postal_code,
        },
        "Email": person.email,
        "Source": "Angi Ads Quote Request",
        "Description": rng.choice(service.requests),
        "Category": f"{profile.city} – {service.name}",
        "Urgency": rng.choices(URGENCIES, URGENCY_WEIGHTS)[0],
        "CorrelationId": str(uuid.UUID(int=rng.getrandbits(128), version=4)),
        "ALAccountId": profile.ads_account,
    }


def leads_payload(rng: random.Random, profile: TenantProfile, person: Person, service: Service) -> dict[str, Any]:
    """An Angi Leads lead (camelCase, numeric ids, carries its own price)."""
    low, high = profile.leads_fee
    return {
        "name": f"{person.first_name} {person.last_name}",
        "firstName": person.first_name,
        "lastName": person.last_name,
        "address": person.address,
        "city": profile.city,
        "stateProvince": profile.state,
        "postalCode": person.postal_code,
        "primaryPhone": person.phone,
        "phoneExt": "",
        "secondaryPhone": None,
        "secondaryPhoneExt": "",
        "email": person.email,
        "srOid": rng.randint(100_000_000, 999_999_999),
        "leadOid": rng.randint(100_000_000, 999_999_999),
        "fee": round(rng.uniform(low, high), 2),
        "taskName": service.name,
        "comments": rng.choice(service.requests),
        "matchType": rng.choice(("Lead", "Lead", "Exact Match")),
        "leadDescription": "Standard",
        "spEntityId": profile.leads_account,
        "spCompanyName": profile.company,
        "leadSource": rng.choice(("Angi", "HomeAdvisor")),
        "trustedFormUrl": f"https://cert.trustedform.com/{rng.getrandbits(160):040x}",
        "primaryPhoneDetails": {"maskedNumber": False},
        "automatedContactCompliant": rng.random() < 0.8,
        "interview": [
            {"question": "What kind of location is this?", "answer": "Home/Residence"},
            {
                "question": "When do you need this work done?",
                "answer": rng.choice(("Timing is flexible", "Within 1 week", "1 - 2 weeks", "As soon as possible")),
            },
        ],
    }


def random_arrival(rng: random.Random, zone: ZoneInfo, start: datetime, end: datetime) -> datetime:
    """A moment between start and end, following the weekly and daily rhythm in the
    tenant's own timezone (people ask for a plumber at 10 AM their time, not UTC)."""
    first_day: date = start.astimezone(zone).date()
    days = [first_day + timedelta(days=n) for n in range((end.astimezone(zone).date() - first_day).days + 1)]
    day_weights = [WEEKDAY_WEIGHTS[day.weekday()] for day in days]
    for _ in range(100):
        day = rng.choices(days, day_weights)[0]
        hour = rng.choices(range(24), HOUR_WEIGHTS)[0]
        local = datetime(day.year, day.month, day.day, hour, rng.randrange(60), rng.randrange(60), tzinfo=zone)
        moment = local.astimezone(UTC)
        if start <= moment < end:
            return moment
    return start + (end - start) * rng.random()  # a window too short for the rhythm to matter


def plan_behaviour(rng: random.Random, lead: PlannedLead) -> None:
    """Decide up front what this consumer will do after getting the intro email."""
    if rng.random() < CLICK_RATE:
        # Most people who click do so within the hour; the rest come back after a follow-up.
        minutes = rng.uniform(2, 50) if rng.random() < 0.65 else rng.uniform(62, 30 * 60)
        lead.clicks_after = timedelta(minutes=minutes)
        if rng.random() < BOOK_RATE_AFTER_CLICK:
            lead.books_after = timedelta(minutes=rng.uniform(1, 12))
            roll = rng.random()
            if roll < WON_RATE_AFTER_BOOKING:
                lead.closes_as = "won"
                dollars = rng.uniform(lead.service.job_low, lead.service.job_high)
                lead.job_value_cents = int(round(dollars / 5) * 5) * 100  # invoices end in 0 or 5
                lead.closes_after = timedelta(hours=rng.uniform(1, 30))
            elif roll < WON_RATE_AFTER_BOOKING + LOST_RATE_AFTER_BOOKING:
                lead.closes_as = "lost"
                lead.close_reason = rng.choice(LOST_AFTER_BOOKING)
                lead.closes_after = timedelta(hours=rng.uniform(0.5, 20))
            return
    if rng.random() < LOST_RATE_WITHOUT_BOOKING:
        lead.closes_as = "lost"
        lead.close_reason = rng.choice(LOST_WITHOUT_BOOKING)
        lead.closes_after = timedelta(days=rng.uniform(2, 6))


def plan_leads(rng: random.Random, count: int, start: datetime, end: datetime) -> list[PlannedLead]:
    """Everything that will arrive, in order of arrival. Pure: touches no database."""
    repeats = round(count * DUPLICATE_RATE)
    regular = count - repeats - 1  # one more is the lead from the unmapped account
    phones: dict[str, list[str]] = {}  # unused 555-01xx numbers, per tenant
    for profile in TENANTS:
        numbers = [f"{area}55501{n:02d}" for area in profile.area_codes for n in range(100)]
        numbers = [number for number in numbers if number not in SAMPLE_PHONES]
        rng.shuffle(numbers)
        phones[profile.tenant_id] = numbers
    used_emails: set[str] = set()

    def new_lead(profile: TenantProfile, at: datetime, use_leads_feed: bool) -> PlannedLead:
        person = new_person(rng, profile, phones[profile.tenant_id], used_emails)
        service = rng.choice(profile.services)
        build = leads_payload if use_leads_feed else ads_payload
        return PlannedLead(at=at, profile=profile, person=person, service=service,
                           payload=build(rng, profile, person, service))

    # 1. Ordinary leads.
    leads: list[PlannedLead] = []
    for _ in range(regular):
        profile = rng.choices(TENANTS, [p.share for p in TENANTS])[0]
        at = random_arrival(rng, ZoneInfo(profile.timezone), start, end)
        use_leads_feed = profile.leads_account is not None and rng.random() < ANGI_LEADS_SHARE
        lead = new_lead(profile, at, use_leads_feed)
        plan_behaviour(rng, lead)
        leads.append(lead)

    # 2. A few special cases among them.
    ads_leads = [lead for lead in leads if "CorrelationId" in lead.payload]
    specials = rng.sample(ads_leads, min(len(ads_leads), LEADS_WITHOUT_EMAIL + 1))
    if specials:
        # Angi renames a field and adds one without telling anyone: the lead is still answered.
        drifted = specials[0].payload
        drifted["EmailAddress"] = drifted.pop("Email")
        drifted["PreferredContactTime"] = "Evenings after 6 PM"
    for lead in specials[1:]:
        # No email address: stored and flagged for a phone call, nothing to send or click.
        lead.payload["Email"] = ""
        lead.clicks_after = lead.books_after = lead.closes_as = lead.closes_after = None
    special_ids = {id(lead) for lead in specials}
    ordinary = [lead for lead in leads if id(lead) not in special_ids]
    for lead in rng.sample(ordinary, min(len(ordinary), EMAIL_HICCUPS)):
        lead.email_hiccup = True

    # 3. Repeat requests: the same consumer, for the same business, as a new (billed) lead.
    #    It comes through the same feed as the original, hence the same Angi account: a
    #    duplicate is only claimable from the account that was charged twice.
    early_enough = [lead for lead in ordinary if lead.at < end - timedelta(hours=2)]
    for number, original in enumerate(rng.sample(early_enough, min(len(early_enough), repeats)), start=1):
        gap = rng.choice((
            timedelta(minutes=rng.uniform(20, 180)),  # impatient: asked again the same day
            timedelta(hours=rng.uniform(6, 48)),
            timedelta(days=rng.uniform(3, 9)),
        ))
        gap = min(gap, (end - original.at) * rng.uniform(0.3, 0.9))  # but never in the future
        service = original.service if rng.random() < 0.75 else rng.choice(original.profile.services)
        build = leads_payload if "leadOid" in original.payload else ads_payload
        repeat = PlannedLead(
            at=original.at + gap, profile=original.profile, person=original.person, service=service,
            payload=build(rng, original.profile, original.person, service), is_repeat=True,
        )
        if number % OTHER_ADDRESS_EVERY == 2:
            # Asked again from their work address. The phone still gives them away as the
            # same homeowner, but this address has never been written to, so it gets the
            # introduction rather than the short "we got your new request" note.
            email_key = "email" if "leadOid" in repeat.payload else "Email"
            repeat.payload[email_key] = original.person.email.replace("@", ".work@")
        # Someone at the tenant works through the credit list: most claims get filed, and
        # Angi has answered some of them by now.
        if rng.random() < 0.6:
            filed = timedelta(hours=rng.uniform(4, 30))
            repeat.claim_steps.append((filed, "submitted", "Requested in the Angi pro portal"))
            answer = rng.random()
            if answer < 0.5:
                repeat.claim_steps.append((filed + timedelta(days=rng.uniform(2, 5)), "credited", "Credit approved by Angi"))
            elif answer < 0.65:
                repeat.claim_steps.append((filed + timedelta(days=rng.uniform(2, 5)), "denied", "Angi: treated as a separate project"))
        leads.append(repeat)

    # 4. One lead for an Angi account nobody has mapped to a tenant yet (a new location).
    profile = PROFILES["tnt_blueline"]
    unmapped = new_lead(profile, random_arrival(rng, ZoneInfo(profile.timezone), start + (end - start) / 2, end), False)
    unmapped.payload["ALAccountId"] = UNMAPPED_ACCOUNT_ID
    leads.append(unmapped)

    return sorted(leads, key=lambda lead: lead.at)


# --- the run: step the clock and let the application do the work --------------------------


class SimClock:
    """The application's clock. It only ever moves forward."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def move_to(self, moment: datetime) -> None:
        self.now = max(self.now, moment)


class Simulation:
    def __init__(self, ctx: AppContext, clock: SimClock, mailer: MemoryEmailProvider, rng: random.Random, end: datetime) -> None:
        self.ctx = ctx
        self.clock = clock
        self.mailer = mailer
        self.rng = rng
        self.end = end
        self._events: list[tuple[datetime, int, str, Any]] = []  # a heap ordered by time
        self._counter = 0

    def schedule(self, at: datetime, kind: str, data: Any = None) -> None:
        if at < self.end:  # history stops at "now"
            self._counter += 1  # keeps events at the same instant in the order they were scheduled
            heapq.heappush(self._events, (at, self._counter, kind, data))

    def run(self, plan: list[PlannedLead]) -> None:
        for lead in plan:
            self.schedule(lead.at, "arrive", lead)
        tick = self.clock.now + timedelta(hours=1)
        while tick < self.end:
            self.schedule(tick, "tick")
            tick += timedelta(hours=1)

        handlers = {
            "arrive": self.arrive,
            "click": self.click,
            "book": self.book,
            "close": self.close,
            "claim": self.update_claim,
            "tick": self.tick,
        }
        while self._events:
            at, _, kind, data = heapq.heappop(self._events)
            self.deliver_until(at)  # first send whatever fell due before this moment
            self.clock.move_to(at)
            handlers[kind](data)
        self.deliver_until(self.end)
        self.clock.move_to(self.end)
        self.tick()

    def deliver_until(self, moment: datetime) -> None:
        """Send every queued email that falls due up to ``moment``, each at the time it
        falls due plus the second or two the background worker needs to pick it up
        (but never later than ``moment``, so nothing is stamped after the history ends)."""
        for _ in range(100_000):
            due = outbox.next_due_at(self.ctx.db)
            if due is None or parse_iso(due) > moment:
                return
            pickup = parse_iso(due) + timedelta(seconds=self.rng.uniform(0.8, 3.5))
            self.clock.move_to(min(pickup, moment))
            if self.ctx.worker.deliver_due() == 0 and outbox.next_due_at(self.ctx.db) == due:
                return  # the worker is holding that message back for now; time has to pass first
        raise RuntimeError("the outbox never drained; a message keeps coming back")

    # -- what can happen at a moment in time --
    def arrive(self, lead: PlannedLead) -> None:
        if lead.email_hiccup:
            # The email provider times out once; the worker retries a few seconds later.
            self.mailer.fail_with = TimeoutError("simulated: the email provider timed out")
            self.mailer.fail_times = 1
        outcome = self.ctx.ingest.receive(
            raw_body=json.dumps(lead.payload, ensure_ascii=False).encode("utf-8"),
            headers=ANGI_HEADERS,
            remote_addr=ANGI_ADDRESS,
        )
        body = outcome.body
        if body.get("result") != "processed":
            return  # quarantined (the unmapped account): no lead, so nothing follows
        lead.lead_id = body["leadId"]
        now = self.clock.now
        if lead.clicks_after is not None:
            self.schedule(now + lead.clicks_after, "click", lead)
        if lead.closes_as is not None and lead.books_after is None:
            self.schedule(now + lead.closes_after, "close", lead)  # lost without ever booking
        claim_id = (body.get("duplicate") or {}).get("creditClaimId")
        if claim_id:
            for after, status, note in lead.claim_steps:
                self.schedule(now + after, "claim", (claim_id, status, note))

    def click(self, lead: PlannedLead) -> None:
        """The consumer opens the booking link: the same call GET /book/{token} makes
        (app/routes/public.py), so it records the first engagement and a timeline event."""
        now = self.clock.now
        with self.ctx.db.transaction() as conn:
            record_link_click(conn, get_lead(conn, lead.lead_id), now, user_agent="simulated")
        if lead.books_after is not None:
            self.schedule(now + lead.books_after, "book", lead)

    def book(self, lead: PlannedLead) -> None:
        """The consumer picks one of the offered arrival windows, usually an early one."""
        now = self.clock.now
        try:
            with self.ctx.db.transaction() as conn:
                row = get_lead(conn, lead.lead_id)
                tenant = get_tenant(conn, row["tenant_id"])
                slots = available_slots(conn, tenant, now)
                if not slots:
                    return
                slot = slots[min(int(self.rng.expovariate(1 / 3)), len(slots) - 1)]
                appointment = book_appointment(conn, lead=row, tenant=tenant, slot_key=slot.key, now=now)
        except BookingError:
            return  # the window was taken in the meantime; this consumer gives up
        if lead.closes_as is not None:
            # The job is won or lost some time after the visit.
            self.schedule(parse_iso(appointment["ends_at"]) + lead.closes_after, "close", lead)

    def close(self, lead: PlannedLead) -> None:
        with self.ctx.db.transaction() as conn:
            queries.record_outcome(
                conn, lead.lead_id, outcome=lead.closes_as, now=self.clock.now,
                job_value_cents=lead.job_value_cents, reason=lead.close_reason,
            )

    def update_claim(self, step: tuple[str, str, str]) -> None:
        claim_id, status, note = step
        with self.ctx.db.transaction() as conn:
            credits.set_status(conn, claim_id, status, self.clock.now, notes=note)

    def tick(self, _: Any = None) -> None:
        """Once an hour, what the background worker does every minute: retry anything
        unfinished, run the monitoring checks, announce new alerts (here: to the log only)."""
        self.ctx.worker.run_maintenance()
        self.ctx.notifier.dispatch(self.ctx.db, self.clock.now)


def cancel_unsent(ctx: AppContext, now: datetime) -> int:
    """Cancel every message still waiting to be sent, then prove that none is left.
    This is what makes it safe to start the real server on the simulated database."""
    with ctx.db.transaction() as conn:
        waiting = [row["lead_id"] for row in conn.execute(
            "SELECT DISTINCT lead_id FROM outbox_messages WHERE status = 'pending' ORDER BY lead_id"
        )]
        cancelled = sum(outbox.cancel_pending(conn, lead_id, now, reason="simulation") for lead_id in waiting)
    with ctx.db.read() as conn:
        left = conn.execute(
            "SELECT COUNT(*) AS n FROM outbox_messages WHERE status IN ('pending', 'sending')"
        ).fetchone()["n"]
    assert left == 0, f"{left} outbox message(s) are still pending or sending after the simulation"
    return cancelled


def summarize(ctx: AppContext) -> dict[str, Any]:
    """Counts read back from the database (what the application stored, not what was planned)."""

    def count(sql: str) -> int:
        return conn.execute(f"SELECT COUNT(*) AS n FROM {sql}").fetchone()["n"]

    with ctx.db.read() as conn:
        by_tenant = {
            row["tenant_id"]: row["n"]
            for row in conn.execute("SELECT tenant_id, COUNT(*) AS n FROM leads GROUP BY tenant_id ORDER BY tenant_id")
        }
        revenue = conn.execute(
            "SELECT COALESCE(SUM(job_value_cents), 0) AS cents FROM leads WHERE outcome = 'won'"
        ).fetchone()["cents"]
        return {
            "webhook_events": count("webhook_events"),
            "leads": count("leads"),
            "leads_by_tenant": by_tenant,
            "contacted": count("leads WHERE first_contacted_at IS NOT NULL"),
            "clicked": count("leads WHERE first_engaged_at IS NOT NULL"),
            "booked": count("leads WHERE booked_at IS NOT NULL"),
            "won": count("leads WHERE outcome = 'won'"),
            "lost": count("leads WHERE outcome = 'lost'"),
            "revenue_cents": revenue,
            "duplicates": count("leads WHERE is_duplicate = 1"),
            "credit_claims": count("credit_claims"),
            "quarantined": count("webhook_events WHERE status = 'quarantined'"),
            "open_alerts": count("alerts WHERE status = 'open'"),
            "emails_sent": count("outbox_messages WHERE status = 'sent'"),
            "emails_cancelled": count("outbox_messages WHERE status = 'cancelled'"),
            "pending_outbox": count("outbox_messages WHERE status IN ('pending', 'sending')"),
        }


@dataclass
class SimulationResult:
    summary: dict[str, Any]
    mailer: MemoryEmailProvider  # every email the simulation "sent" ended up here, nowhere else
    database: str
    start: datetime
    end: datetime


def simulate(
    *,
    database: str | None = None,
    leads: int = 160,
    days: int = 14,
    seed: int = 7,
    force: bool = False,
    now: datetime | None = None,
    settings: Settings | None = None,
) -> SimulationResult:
    """Run the whole simulation. ``now`` is where the history ends (default: this moment);
    ``settings`` defaults to what the server itself would load from the environment / .env."""
    if not MIN_LEADS <= leads <= MAX_LEADS:
        raise SimulationRefused(f"--leads must be between {MIN_LEADS} and {MAX_LEADS}")
    if days < 1:
        raise SimulationRefused("--days must be at least 1")
    end = (now or utcnow()).astimezone(UTC)
    start = end - timedelta(days=days)

    # The server's own configuration (public URL for the links, follow-up cadence,
    # duplicate window), with everything that could reach the outside world switched off.
    settings = (settings or Settings()).model_copy(update={
        "run_background_worker": False,
        "seed_demo_data": False,  # seeded below, dated at the start of the history
        "email_provider": "console",  # unused: the in-memory provider is passed explicitly
        "email_redirect_all_to": "",
        "alert_slack_webhook_url": "",
        "alert_email_to": "",
        "fallback_tenant_slug": "",
    })
    if database:
        settings = settings.model_copy(update={"database_path": database})

    clock = SimClock(start)
    mailer = MemoryEmailProvider()
    ctx = build_context(settings, email_provider=mailer, clock=clock)

    with ctx.db.read() as conn:
        existing = conn.execute("SELECT COUNT(*) AS n FROM leads").fetchone()["n"]
        queued = conn.execute(
            "SELECT COUNT(*) AS n FROM outbox_messages WHERE status IN ('pending', 'sending')"
        ).fetchone()["n"]
    if existing and not force:
        raise SimulationRefused(
            f"{settings.database_path} already contains {existing} lead(s). Use an empty database "
            "(make reset-db), or pass --force to add simulated history on top."
        )
    if queued:
        # Stepping the clock would "send" them to the in-memory provider: real leads would never be emailed.
        raise SimulationRefused(
            f"{settings.database_path} has {queued} email(s) waiting to be sent, which the simulation "
            "would swallow. Let the server send them first, or use an empty database."
        )

    with ctx.db.transaction() as conn:
        seed_demo(conn, start)  # does nothing if the database already has tenants
        missing = [p.tenant_id for p in TENANTS if get_tenant(conn, p.tenant_id) is None]
    if missing:
        raise SimulationRefused(
            f"{settings.database_path} has tenants, but not the demo tenants ({', '.join(missing)}). "
            "Use an empty database."
        )

    rng = random.Random(seed)
    plan = plan_leads(rng, leads, start, end)
    Simulation(ctx, clock, mailer, rng, end).run(plan)
    cancel_unsent(ctx, end)
    return SimulationResult(summary=summarize(ctx), mailer=mailer, database=settings.database_path, start=start, end=end)


def format_summary(result: SimulationResult, seed: int) -> str:
    s = result.summary
    days = (result.end - result.start).days
    tenants = ", ".join(f"{tenant_id} {n}" for tenant_id, n in s["leads_by_tenant"].items())
    lines = [
        f"Simulated {days} days of history ending {iso(result.end)} in {result.database} (seed {seed})",
        f"  webhook events  {s['webhook_events']}",
        f"  leads           {s['leads']}  ({tenants})",
        f"  contacted       {s['contacted']}",
        f"  clicked         {s['clicked']}",
        f"  booked          {s['booked']}",
        f"  won             {s['won']}  (${s['revenue_cents'] / 100:,.0f} revenue)",
        f"  lost            {s['lost']}",
        f"  duplicates      {s['duplicates']}  ({s['credit_claims']} credit claims)",
        f"  quarantined     {s['quarantined']}",
        f"  open alerts     {s['open_alerts']}",
        f"  emails          {s['emails_sent']} sent to the in-memory provider (none left this machine), "
        f"{s['emails_cancelled']} cancelled",
        f"  pending outbox  {s['pending_outbox']}",
    ]
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fill a database with realistic lead history for the demo.")
    parser.add_argument("--database", help="SQLite file to fill (default: DATABASE_PATH, i.e. data/leads.db)")
    parser.add_argument("--leads", type=int, default=160, help="about how many leads to generate (default: 160)")
    parser.add_argument("--days", type=int, default=14, help="how many days of history (default: 14)")
    parser.add_argument("--seed", type=int, default=7, help="random seed; the same seed gives the same history (default: 7)")
    parser.add_argument("--force", action="store_true", help="run even if the database already contains leads")
    parser.add_argument("--now", help="where the history ends, as an ISO timestamp (default: the current time)")
    parser.add_argument("--verbose", action="store_true", help="show the application's warnings (alerts) as it runs")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.WARNING if args.verbose else logging.ERROR)
    try:
        now = parse_iso(args.now) if args.now else None
    except ValueError:
        print(f"error: --now must be an ISO timestamp like 2026-11-11T18:00:00Z, not {args.now!r}", file=sys.stderr)
        return 2
    try:
        result = simulate(
            database=args.database, leads=args.leads, days=args.days, seed=args.seed, force=args.force, now=now
        )
    except SimulationRefused as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(format_summary(result, args.seed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
