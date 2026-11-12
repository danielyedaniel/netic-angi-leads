"""The operator dashboard: every page, every form, and the ways to break them.

All leads are created the way Angi creates them (through the webhook); the tests then
read the HTML the operator would see and the database rows behind it.
"""

from __future__ import annotations

import base64
import re
from datetime import date
from html.parser import HTMLParser
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import security
from app.config import Settings
from app.context import build_context
from app.mailer.providers import MemoryEmailProvider, PermanentEmailError
from app.main import create_app
from app.routes import dashboard
from app.tenants import valid_timezone
from tests.conftest import (
    ADMIN,
    ADMIN_TOKEN,
    API_KEY,
    BASE_URL,
    CANARY_ACCOUNT,
    FakeClock,
    ads_payload,
    db_one,
    db_rows,
    leads_payload,
    post_lead,
)

XSS = "<script>alert(1)</script>"
XSS_ESCAPED = "&lt;script&gt;alert(1)&lt;/script&gt;"
ATTRIBUTE_BREAKER = '"><img src=x onerror=alert(2)>'
UNMAPPED_ACCOUNT = "55500055"
EVIL = {"Origin": "https://evil.example"}

PAGES = ["/dashboard", "/dashboard/leads", "/dashboard/credits", "/dashboard/monitoring", "/dashboard/tenants"]


# --- helpers -------------------------------------------------------------------------------


def get(client: TestClient, path: str):
    return client.get(path, headers=ADMIN)


def post(client: TestClient, path: str, data: dict[str, str] | None = None, headers: dict[str, str] | None = None):
    """Submit a dashboard form the way a browser does, without following the redirect."""
    return client.post(path, data=data or {}, headers={**ADMIN, **(headers or {})}, follow_redirects=False)


def tile(html: str, label: str) -> str:
    """The big number on the stat tile with this label."""
    match = re.search(
        rf'<div class="tile-label">{re.escape(label)}</div>\s*<div class="tile-value">([^<]*)</div>', html
    )
    assert match, f"no tile labelled {label!r}"
    return match.group(1).strip()


def funnel_count(html: str, stage: str) -> int:
    match = re.search(rf'title="{stage}: (\d+)', html)
    assert match, f"no funnel bar for {stage!r}"
    return int(match.group(1))


def deliver(ctx, clock, seconds: float = 5) -> None:
    """Let ``seconds`` pass, then run one pass of the email worker."""
    clock.advance(seconds=seconds)
    ctx.worker.deliver_due()


def book_first_slot(client: TestClient, token: str) -> str:
    """The homeowner opens the booking link and takes the first slot offered."""
    page = client.get(f"/book/{token}")
    slot = re.search(r'name="slot" value="([^"]+)"', page.text).group(1)
    assert client.post(f"/book/{token}", data={"slot": slot}, follow_redirects=False).status_code == 303
    return slot


VOID_ELEMENTS = {"meta", "input", "br", "hr", "img", "link"}


class _Outline(HTMLParser):
    """Checks that tags are properly nested and closed, and collects ids."""

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[str] = []
        self.problems: list[str] = []
        self.ids: list[str] = []
        self.tags: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(self, tag, attrs):
        self._note(tag, attrs)
        if tag not in VOID_ELEMENTS:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):  # <line ... /> inside the SVG chart
        self._note(tag, attrs)

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1] != tag:
            self.problems.append(f"</{tag}> closes {self.stack[-1] if self.stack else 'nothing'}")
        else:
            self.stack.pop()

    def _note(self, tag, attrs):
        attributes = dict(attrs)
        self.tags.append((tag, attributes))
        if "id" in attributes:
            self.ids.append(attributes["id"])


def outline(html: str) -> _Outline:
    parser = _Outline()
    parser.feed(html)
    parser.close()
    return parser


@pytest.fixture
def scene(client, ctx, clock) -> dict[str, Any]:
    """A small, varied set of leads, all posted through the webhook:

    * a lead whose name is a script tag (Sparkle), later followed by
    * a duplicate of it from the same homeowner (so a credit claim exists),
    * a lead that books an appointment (Sparkle),
    * an Angi Leads format lead with interview answers (Summit),
    * a lead from an Angi account nobody has mapped (quarantined).
    """
    first = post_lead(
        client,
        ads_payload(FirstName=XSS, LastName="Lee", Email="xss@example.com", PhoneNumber="3175550101",
                    Description=ATTRIBUTE_BREAKER),
    ).json()
    deliver(ctx, clock)

    booked = post_lead(
        client, ads_payload(FirstName="Bea", LastName="Booker", Email="bea@example.com", PhoneNumber="3175550102")
    ).json()
    deliver(ctx, clock)
    booked_token = db_one(ctx, "SELECT booking_token FROM leads WHERE id = ?", (booked["leadId"],))["booking_token"]
    slot = book_first_slot(client, booked_token)
    ctx.worker.deliver_due()  # the booking confirmation

    jane = post_lead(client, leads_payload(trustedFormUrl="javascript:alert(3)")).json()
    deliver(ctx, clock)

    clock.advance(hours=5)
    duplicate = post_lead(
        client, ads_payload(FirstName=XSS, LastName="Lee", Email="xss@example.com", PhoneNumber="3175550101")
    ).json()
    deliver(ctx, clock)

    quarantined = post_lead(
        client,
        leads_payload(spEntityId=int(UNMAPPED_ACCOUNT), spCompanyName=f"{XSS} Plumbing", name="Quinn Quarantine",
                      firstName="Quinn", lastName="Quarantine", email="quinn@example.com", primaryPhone="3035550177"),
    ).json()

    assert [first["result"], booked["result"], jane["result"], duplicate["result"], quarantined["result"]] == [
        "processed", "processed", "processed", "processed", "quarantined",
    ]
    assert duplicate["duplicate"]["ofLeadId"] == first["leadId"]
    return {
        "message_id": db_one(ctx, "SELECT id FROM outbox_messages WHERE lead_id = ? AND template = 'intro'",
                             (booked["leadId"],))["id"],
        "xss_id": first["leadId"],
        "booked_id": booked["leadId"],
        "booked_token": booked_token,
        "slot": slot,
        "jane_id": jane["leadId"],
        "dup_id": duplicate["leadId"],
        "claim_id": duplicate["duplicate"]["creditClaimId"],
        "quarantined_event_id": quarantined["eventId"],
    }


def tenant_section(html: str, tenant_id: str) -> str:
    """The card of one tenant on the Tenants page."""
    section = html[html.index(f'id="tenant-{tenant_id}"'):]
    return section[:section.index("</section>")]


def claim_block(html: str, claim_id: str) -> str:
    """Everything the Credits page shows for one claim (its facts row and its actions row)."""
    block = html[html.index(f'<tbody id="claim-{claim_id}"'):]
    return block[:block.index("</tbody>")]


def all_pages(scene: dict[str, Any]) -> list[str]:
    return [*PAGES, f"/dashboard/leads/{scene['xss_id']}", f"/dashboard/leads/{scene['dup_id']}",
            f"/dashboard/leads/{scene['booked_id']}", f"/dashboard/leads/{scene['jane_id']}"]


def empty_app(tmp_path, *, seed: bool):
    """An application over a brand-new database, with or without the demo tenants."""
    settings = Settings(
        _env_file=None,
        database_path=str(tmp_path / "empty.db"),
        public_base_url=BASE_URL,
        angi_webhook_api_keys=API_KEY,
        admin_token=ADMIN_TOKEN,
        run_background_worker=False,
        seed_demo_data=seed,
    )
    ctx = build_context(settings, email_provider=MemoryEmailProvider(), clock=FakeClock())
    return ctx, create_app(context=ctx)


# --- access --------------------------------------------------------------------------------


@pytest.mark.parametrize("path", PAGES)
def test_page_needs_the_admin_token(client, path):
    # Rule: the dashboard shows customer data, so every page is behind the admin token.
    anonymous = client.get(path)
    assert anonymous.status_code == 401
    assert anonymous.headers["www-authenticate"].startswith("Basic")  # so a browser asks for the password
    assert client.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get(path, headers=ADMIN).status_code == 200


def test_lead_detail_needs_the_admin_token(client, scene):
    # Rule: a lead's detail page is as protected as the list it is linked from.
    path = f"/dashboard/leads/{scene['xss_id']}"
    assert client.get(path).status_code == 401
    assert get(client, path).status_code == 200


def test_browser_basic_auth_password_opens_the_dashboard(client):
    # Rule: a browser sends the admin token as the HTTP Basic password; any username works.
    credentials = base64.b64encode(f"operator:{ADMIN_TOKEN}".encode()).decode()
    assert client.get("/dashboard", headers={"Authorization": f"Basic {credentials}"}).status_code == 200


@pytest.mark.parametrize(
    "path",
    [
        "/dashboard/leads/lead_x/outcome",
        "/dashboard/messages/msg_x/retry",
        "/dashboard/credits/clm_x/status",
        "/dashboard/alerts/1/resolve",
        "/dashboard/events/evt_x/replay",
        "/dashboard/events/evt_x/assign",
        "/dashboard/events/evt_x/discard",
        "/dashboard/events/replay-quarantined",
        "/dashboard/tenants",
        "/dashboard/tenants/tnt_sparkle/lead-sources",
    ],
)
def test_form_posts_need_the_admin_token(client, ctx, path):
    # Rule: no dashboard action can be taken without the admin token.
    response = client.post(path, data={"slug": "nope", "name": "Nope", "external_account_id": "1"},
                           follow_redirects=False)
    assert response.status_code == 401
    assert db_one(ctx, "SELECT COUNT(*) AS n FROM tenants WHERE slug = 'nope'")["n"] == 0
    assert db_one(ctx, "SELECT COUNT(*) AS n FROM tenant_lead_sources WHERE external_account_id = '1'")["n"] == 0


# --- empty states --------------------------------------------------------------------------


@pytest.mark.parametrize("path", PAGES)
def test_page_renders_before_any_lead_has_arrived(client, path):
    # Rule: a fresh install (demo tenants, no leads yet) must show every page, not an error.
    response = get(client, path)
    assert response.status_code == 200
    assert outline(response.text).problems == []


@pytest.mark.parametrize("path", [*PAGES, "/dashboard?days=all&live=1", "/dashboard/leads?q=x&duplicates=1&review=1"])
def test_page_renders_on_a_completely_empty_database(tmp_path, path):
    # Rule: with no tenants and no leads at all, every page still renders.
    ctx, app = empty_app(tmp_path, seed=False)
    with TestClient(app) as empty_client:
        response = empty_client.get(path, headers=ADMIN)
    assert response.status_code == 200
    assert outline(response.text).problems == []


def test_empty_overview_says_so_instead_of_drawing_empty_charts(client):
    # Rule: with no leads the overview shows zeros and "nothing yet" messages, never a broken chart.
    html = get(client, "/dashboard").text
    assert tile(html, "Leads received") == "0"
    assert tile(html, "Lead to booked rate") == "—"
    assert tile(html, "Median speed to lead") == "—"
    assert tile(html, "Cost per booking") == "—"
    assert "<svg" not in html
    assert html.count("No leads in this period yet.") >= 3
    assert "No bookings in this period yet." in html


def test_empty_database_tenants_page_offers_to_create_the_first_tenant(tmp_path):
    # Rule: with no tenants the page explains what to do next.
    ctx, app = empty_app(tmp_path, seed=False)
    with TestClient(app) as empty_client:
        html = empty_client.get("/dashboard/tenants", headers=ADMIN).text
    assert "No tenants yet" in html
    assert 'action="/dashboard/tenants"' in html


# --- page hygiene --------------------------------------------------------------------------


def test_every_page_is_well_formed_html_with_unique_ids(client, scene):
    # Rule: pages are valid enough for any browser: tags nest and close, and ids are unique.
    for path in [*all_pages(scene), "/dashboard?live=1", "/dashboard/leads/missing"]:
        page = outline(get(client, path).text)
        assert page.problems == [], path
        assert page.stack == [], path
        assert len(page.ids) == len(set(page.ids)), path


def test_pages_load_nothing_from_anywhere_else(client, scene):
    # Rule: the demo runs offline through a tunnel, so no page may reference an external asset.
    for path in all_pages(scene):
        response = get(client, path)
        html = response.text
        tags = outline(html).tags
        assert not [tag for tag, _ in tags if tag in ("link", "img", "iframe", "object", "embed", "video", "audio")], path
        assert not [attrs for tag, attrs in tags if tag == "script" and "src" in attrs], path
        assert "@import" not in html and "url(" not in html, path
        for tag, attrs in tags:
            if tag == "a" and attrs.get("href"):
                assert attrs["href"].startswith(("/", "mailto:", "tel:")), (path, attrs["href"])
            if tag == "form":
                assert attrs["action"].startswith("/dashboard"), (path, attrs["action"])
        assert "default-src 'none'" in response.headers["content-security-policy"], path


def test_pages_are_not_cached_or_framed(client):
    # Rule: pages with customer data are never stored by the browser or embedded by another site.
    response = get(client, "/dashboard/leads")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-frame-options"] == "DENY"


def test_navigation_links_every_section_and_marks_the_current_one(client):
    # Rule: one shared navigation bar, with the current section marked.
    for path, label in zip(PAGES, ["Overview", "Leads", "Credits", "Monitoring", "Tenants"], strict=True):
        html = get(client, path).text
        for target in PAGES:
            assert f'href="{target}"' in html
        assert re.search(rf'<a href="{path}" aria-current="page">{label}', html)
        assert html.split("<body>")[1].count('aria-current="page"') == 1


# --- untrusted data ------------------------------------------------------------------------


def test_script_in_a_lead_name_is_never_sent_to_the_browser_unescaped(client, scene):
    # Rule: lead data is untrusted; a name that is a script tag is displayed, never executed.
    shown_on = 0
    for path in [*all_pages(scene), "/dashboard?tenant=tnt_sparkle&days=7", "/dashboard/leads?q=alert",
                 "/dashboard/leads?duplicates=1", "/dashboard/credits?status=detected",
                 "/dashboard/monitoring?status=quarantined", "/dashboard/monitoring?status=processed"]:
        html = get(client, path).text
        assert XSS not in html, path
        assert "<img" not in html, path
        shown_on += XSS_ESCAPED in html
    # ...and it really was on those pages (escaped), so the check above means something.
    assert shown_on >= 10


@pytest.mark.parametrize(
    "path",
    ["/dashboard", "/dashboard/leads", "/dashboard/credits", "/dashboard/monitoring", "/dashboard/tenants"],
)
def test_escaped_script_is_visible_on_each_section(client, scene, path):
    # Rule: untrusted text is shown as text on every section that lists it
    # (recent leads, lead list, claim evidence, alert titles, unmapped-account hint).
    html = get(client, path).text
    assert XSS_ESCAPED in html
    assert XSS not in html


def test_untrusted_values_cannot_break_out_of_an_attribute(client, scene):
    # Rule: a value containing quotes and angle brackets stays inside the element it was put in.
    html = get(client, f"/dashboard/leads/{scene['xss_id']}").text
    assert ATTRIBUTE_BREAKER not in html
    assert "&#34;&gt;&lt;img src=x onerror=alert(2)&gt;" in html
    search = get(client, "/dashboard/leads", ).text
    assert ATTRIBUTE_BREAKER not in search
    reflected = client.get("/dashboard/leads", params={"q": ATTRIBUTE_BREAKER}, headers=ADMIN).text
    assert ATTRIBUTE_BREAKER not in reflected
    assert 'value="&#34;&gt;&lt;img src=x onerror=alert(2)&gt;"' in reflected  # echoed back, escaped


def test_url_from_a_payload_is_never_turned_into_a_link(client, scene):
    # Rule: a URL supplied by the lead source is shown as text; "javascript:" must never become an href.
    html = get(client, f"/dashboard/leads/{scene['jane_id']}").text
    assert "javascript:alert(3)" in html
    assert 'href="javascript:' not in html
    for _tag, attrs in outline(html).tags:
        assert "javascript:" not in (attrs.get("href") or "")


def test_message_code_in_the_url_is_looked_up_never_echoed(client):
    # Rule: text from the query string is never displayed; only known message codes show a message.
    html = client.get("/dashboard/leads", params={"msg": XSS, "n": XSS, "ok": "-1"}, headers=ADMIN).text
    assert XSS not in html and XSS_ESCAPED not in html
    assert 'role="status"' not in html
    known = get(client, "/dashboard/leads?msg=replayed_all&n=3&ok=abc").text
    assert "Replayed 3 quarantined event(s); 0 became leads." in known


def test_script_in_a_tenant_name_is_escaped_everywhere(client):
    # Rule: operator-entered text gets the same treatment as lead data.
    created = post(client, "/dashboard/tenants", {"slug": "evil-co", "name": XSS, "timezone": "America/Denver"})
    assert created.status_code == 303
    for path in PAGES:
        html = get(client, path).text
        assert XSS not in html, path
        assert XSS_ESCAPED in html, path


def test_unknown_lead_path_with_markup_gets_a_plain_404(client):
    # Rule: an id taken from the URL is never echoed onto the not-found page.
    response = get(client, "/dashboard/leads/%3Cscript%3Ealert(1)%3C%2Fscript%3E")
    assert response.status_code == 404
    assert XSS not in response.text and XSS_ESCAPED not in response.text


# --- 1. overview ---------------------------------------------------------------------------


def test_overview_shows_the_key_figures(client, scene):
    # Rule: the KPI tiles are the analytics summary: 4 billable leads (the quarantined one and
    # test tenants do not count), 1 booked, 1 duplicate worth its $28 fee back.
    html = get(client, "/dashboard").text
    assert tile(html, "Leads received") == "4"
    assert tile(html, "Lead to booked rate") == "25%"
    assert tile(html, "Median speed to lead") == "5.0 s"
    assert tile(html, "Contacted within 1 minute") == "100%"
    assert tile(html, "Spend on leads") == "$109.67"  # 3 x $28.00 account default + $25.67 from the payload
    assert tile(html, "Cost per booking") == "$109.67"
    assert tile(html, "Revenue from won jobs") == "$0.00"
    assert tile(html, "Duplicates found") == "1"
    assert tile(html, "Claimable credit") == "$28.00"


def test_overview_funnel_counts_each_stage_and_its_step_conversion(client, scene):
    # Rule: received -> contacted -> engaged -> booked -> won, each with the share of the stage before.
    html = get(client, "/dashboard").text
    assert [funnel_count(html, stage) for stage in ("Received", "Contacted", "Engaged", "Booked", "Won")] == [
        4, 4, 1, 1, 0,
    ]
    assert "100% of received" in html
    assert "25% of contacted" in html
    assert "100% of engaged" in html
    assert "0% of booked" in html
    # The widest bar is the largest stage; a stage with nobody in it draws no bar at all.
    assert html.count('class="funnel-bar') == 4
    assert 'funnel-bar stage-1" style="width: 100.0%"' in html
    assert 'funnel-bar stage-4" style="width: 25.0%"' in html


def test_overview_tables_break_the_numbers_down_by_service_and_tenant(client, scene):
    # Rule: "By service" and "By tenant" list received / booked / rate / spend / cost per booking.
    html = get(client, "/dashboard").text
    by_service = html[html.index("<h2>By service</h2>"):html.index("<h2>By tenant</h2>")]
    by_tenant = html[html.index("<h2>By tenant</h2>"):html.index("<h2>Recent leads</h2>")]
    assert re.search(r"<td>House Cleaning</td><td[^>]*>3</td><td[^>]*>1</td><td[^>]*>33.3%</td>"
                     r"<td[^>]*>\$84.00</td><td[^>]*>\$84.00</td>", by_service)
    assert re.search(r"<td>Central A/C - Repair</td><td[^>]*>1</td><td[^>]*>0</td><td[^>]*>0%</td>"
                     r"<td[^>]*>\$25.67</td><td[^>]*>—</td>", by_service)
    assert re.search(r"<td>Sparkle Home Cleaning</td><td[^>]*>3</td><td[^>]*>1</td>", by_tenant)
    assert re.search(r"<td>Summit Heating &amp; Air</td><td[^>]*>1</td><td[^>]*>0</td>", by_tenant)


def test_overview_lists_recent_leads_linking_to_their_detail(client, scene):
    # Rule: the newest leads are one click away from their detail page.
    html = get(client, "/dashboard").text
    recent = html[html.index("<h2>Recent leads</h2>"):]
    for key in ("xss_id", "booked_id", "jane_id", "dup_id"):
        assert f'href="/dashboard/leads/{scene[key]}"' in recent
    assert "Bea Booker" in recent
    assert recent.index(scene["dup_id"]) < recent.index(scene["xss_id"])  # newest first


def test_overview_recent_leads_is_capped_at_ten(client, ctx, clock):
    # Rule: "Recent leads" shows the ten newest, however many there are.
    for index in range(12):
        clock.advance(seconds=1)
        post_lead(client, ads_payload(Email=f"lead{index}@example.com", PhoneNumber=f"31755502{index:02d}",
                                      FirstName=f"Lead{index}", LastName="Recent"))
    html = get(client, "/dashboard").text
    recent = html[html.index("<h2>Recent leads</h2>"):]
    assert recent.count('href="/dashboard/leads/lead_') == 10
    assert "Lead11 Recent" in recent and "Lead2 Recent" in recent
    assert "Lead1 Recent" not in recent and "Lead0 Recent" not in recent


def test_overview_tenant_filter_scopes_every_number(client, scene):
    # Rule: picking a tenant narrows the tiles, the tables and the recent leads alike.
    html = get(client, "/dashboard?tenant=tnt_summit").text
    assert tile(html, "Leads received") == "1"
    assert tile(html, "Spend on leads") == "$25.67"
    assert tile(html, "Duplicates found") == "0"
    assert "Bea Booker" not in html
    assert f'href="/dashboard/leads/{scene["jane_id"]}"' in html
    assert '<option value="tnt_summit" selected>' in html


def test_overview_unknown_tenant_filter_falls_back_to_all_tenants(client, scene):
    # Rule: a tenant id that does not exist is ignored rather than showing an empty dashboard.
    html = get(client, "/dashboard?tenant=tnt_nobody").text
    assert tile(html, "Leads received") == "4"
    assert "selected>" not in html.split('name="tenant"')[1].split("</select>")[0]


def test_overview_period_filter_limits_leads_to_the_last_n_days(client, scene, clock):
    # Rule: "last 7 days" means received in the 7 days before now; "all" has no limit.
    clock.advance(days=10)
    assert tile(get(client, "/dashboard?days=7").text, "Leads received") == "0"
    assert "No leads in this period yet." in get(client, "/dashboard?days=7").text
    assert tile(get(client, "/dashboard?days=30").text, "Leads received") == "4"
    assert tile(get(client, "/dashboard?days=all").text, "Leads received") == "4"
    clock.advance(days=25)
    assert tile(get(client, "/dashboard").text, "Leads received") == "0"  # default period is 30 days
    assert tile(get(client, "/dashboard?days=90").text, "Leads received") == "4"


@pytest.mark.parametrize("days", ["", "0", "-7", "365", "seven", "7.5", "all "])
def test_overview_unknown_period_falls_back_to_thirty_days(client, scene, days):
    # Rule: only 7 / 30 / 90 / all are periods; anything else is treated as the default, 30 days.
    html = client.get("/dashboard", params={"days": days}, headers=ADMIN).text
    assert tile(html, "Leads received") == "4"
    assert '<option value="30" selected>' in html


def test_overview_leaves_test_tenants_out_unless_one_is_selected(client, ctx, clock):
    # Rule: canary (test tenant) leads never pollute business numbers, but can be looked at on purpose.
    post_lead(client, ads_payload(ALAccountId=CANARY_ACCOUNT, Email="canary@example.com"))
    post_lead(client, ads_payload(Email="real@example.com", PhoneNumber="3175550150"))
    assert tile(get(client, "/dashboard").text, "Leads received") == "1"
    canary = get(client, "/dashboard?tenant=tnt_canary").text
    assert tile(canary, "Leads received") == "1"
    assert "Synthetic Canary Co." in canary


def test_overview_bookings_by_touch_separates_intro_from_follow_up(client, ctx, clock):
    # Rule: a booking made before any follow-up went out is credited to the intro email;
    # one made after a follow-up was sent is credited to the follow-up.
    quick = post_lead(client, ads_payload(Email="quick@example.com", PhoneNumber="3175550160")).json()["leadId"]
    slow = post_lead(client, ads_payload(Email="slow@example.com", PhoneNumber="3175550161")).json()["leadId"]
    deliver(ctx, clock)
    tokens = {
        row["id"]: row["booking_token"]
        for row in db_rows(ctx, "SELECT id, booking_token FROM leads WHERE id IN (?, ?)", (quick, slow))
    }
    book_first_slot(client, tokens[quick])
    deliver(ctx, clock, seconds=61 * 60)  # the first follow-up goes out to the lead that has not booked
    assert db_one(
        ctx, "SELECT COUNT(*) AS n FROM outbox_messages WHERE lead_id = ? AND template = 'followup' AND status = 'sent'",
        (slow,),
    )["n"] == 1
    clock.advance(minutes=5)
    book_first_slot(client, tokens[slow])

    html = get(client, "/dashboard").text
    touch = html[html.index("<h2>Bookings by touch</h2>"):html.index("<h2>Leads per day</h2>")]
    assert re.search(r"Intro email only</td><td[^>]*><strong>1</strong></td><td[^>]*>50%", touch)
    assert re.search(r"After a follow-up</td><td[^>]*><strong>1</strong></td><td[^>]*>50%", touch)
    assert touch.count("flex-grow: 1") == 2


def test_overview_leads_per_day_chart_matches_its_table(client, scene):
    # Rule: the chart is inline SVG drawn on the server, and the same numbers are in a table view.
    html = get(client, "/dashboard?days=7").text
    chart = html[html.index("<h2>Leads per day</h2>"):html.index("<h2>By service</h2>")]
    assert chart.count("<svg") == 2 and "<script" not in chart
    assert "Thu Nov 12: 4 received, 1 booked" in chart  # the hover text of that day's column
    assert chart.count('class="hit"') == 7  # one hover target per day of the period, quiet days included
    assert chart.count('class="bar received"') == 1 and chart.count('class="bar booked"') == 1
    assert "<td>2026-11-12</td><td class=\"num\">4</td><td class=\"num\">1</td>" in chart
    assert "Received</li>" in chart and "Booked" in chart  # a legend, since there are two series


def test_live_mode_reloads_the_overview_and_keeps_the_filters(client, scene):
    # Rule: ?live=1 adds a 10-second refresh that preserves the other filters, shows an
    # indicator, and offers a link that turns it off.
    html = get(client, "/dashboard?tenant=tnt_sparkle&days=7&live=1").text
    assert '<meta http-equiv="refresh" content="10; url=/dashboard?tenant=tnt_sparkle&amp;days=7&amp;live=1">' in html
    assert "Live: reloading every 10 s" in html
    assert '<a href="/dashboard?tenant=tnt_sparkle&amp;days=7">turn off</a>' in html
    assert '<input type="hidden" name="live" value="1">' in html  # changing a filter stays live

    off = get(client, "/dashboard?tenant=tnt_sparkle&days=7").text
    assert "http-equiv" not in off
    assert 'href="/dashboard?tenant=tnt_sparkle&amp;days=7&amp;live=1"' in off


@pytest.mark.parametrize("value", ["0", "true", "yes", "2", ""])
def test_live_mode_is_only_switched_on_by_live_equals_one(client, value):
    # Rule: only live=1 turns auto-refresh on.
    html = client.get("/dashboard", params={"live": value}, headers=ADMIN).text
    assert "http-equiv" not in html


def test_live_refresh_does_not_repeat_a_one_off_message(client):
    # Rule: the message shown after an action must not come back on every automatic reload.
    html = get(client, "/dashboard/monitoring?live=1&status=quarantined&msg=alert_resolved").text
    assert "Alert resolved." in html
    assert 'content="10; url=/dashboard/monitoring?status=quarantined&amp;live=1"' in html


# --- 2. leads list -------------------------------------------------------------------------


def test_leads_list_shows_every_lead_with_its_columns(client, scene):
    # Rule: one row per stored lead: received, tenant, name, service, city/state, status, speed, fee, flags.
    html = get(client, "/dashboard/leads").text
    assert html.count('href="/dashboard/leads/lead_') == 4  # the quarantined request is not a lead yet
    for key in ("xss_id", "booked_id", "jane_id", "dup_id"):
        assert f'href="/dashboard/leads/{scene[key]}"' in html
    row = html[html.index(scene["booked_id"]):]
    row = row[:row.index("</tr>")]
    assert "Bea Booker" in row
    assert "House Cleaning" in row
    assert "Indianapolis, IN" in row
    assert '<span class="badge good">booked</span>' in row
    assert "5.0 s" in row
    assert "$28.00" in row
    jane = html[html.index(scene["jane_id"]):]
    assert "Denver, CO" in jane[:jane.index("</tr>")] and "$25.67" in jane[:jane.index("</tr>")]
    assert "2026-11-12 15:00" in html


def test_leads_list_flags_duplicates(client, scene):
    # Rule: a duplicate lead is marked as such in the list.
    html = get(client, "/dashboard/leads").text
    dup_row = html[html.index(scene["dup_id"]):]
    dup_row = dup_row[:dup_row.index("</tr>")]
    assert ">duplicate</span>" in dup_row
    original_row = html[html.index(scene["xss_id"]):]
    assert ">duplicate</span>" not in original_row[:original_row.index("</tr>")]


def test_leads_list_flags_tolerant_parses_and_leads_needing_review(client, ctx):
    # Rule: a lead recovered from a drifted payload, or one we cannot email, is flagged for a human.
    drifted = ads_payload(Email="drift@example.com", PhoneNumber="3175550170")
    drifted["EmailAddress"] = drifted.pop("Email")  # Angi renames a field without telling us
    assert post_lead(client, drifted).json()["result"] == "processed"
    lead = db_one(ctx, "SELECT id, parse_mode, needs_review FROM leads")
    assert (lead["parse_mode"], lead["needs_review"]) == ("tolerant", 1)
    html = get(client, "/dashboard/leads").text
    assert ">tolerant parse</span>" in html
    assert ">needs review</span>" in html
    assert f'href="/dashboard/leads/{lead["id"]}"' in get(client, "/dashboard/leads?review=1").text


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("tenant=tnt_summit", {"jane_id"}),
        ("tenant=tnt_sparkle", {"xss_id", "booked_id", "dup_id"}),
        ("tenant=tnt_blueline", set()),
        ("status=booked", {"booked_id"}),
        ("status=contacted", {"xss_id", "jane_id", "dup_id"}),
        ("status=won", set()),
        ("status=not-a-status", set()),
        ("duplicates=1", {"dup_id"}),
        ("duplicates=0", {"xss_id", "booked_id", "jane_id", "dup_id"}),
        ("review=1", set()),
        ("q=bea%40example.com", {"booked_id"}),
        ("q=BOOKER", {"booked_id"}),
        ("q=3035550123", {"jane_id"}),
        ("q=Central+A%2FC", {"jane_id"}),
        ("q=++Booker++", {"booked_id"}),
        ("q=nobody-by-this-name", set()),
        ("q=", {"xss_id", "booked_id", "jane_id", "dup_id"}),
        ("q=%27%3B+DROP+TABLE+leads%3B+--", set()),
        ("tenant=tnt_sparkle&status=booked&q=bea", {"booked_id"}),
        ("tenant=tnt_summit&status=booked", set()),
    ],
)
def test_leads_list_filters(client, scene, query, expected):
    # Rule: tenant, status, duplicates-only, needs-review and free text narrow the list, and combine.
    html = get(client, f"/dashboard/leads?{query}").text
    shown = {key for key in ("xss_id", "booked_id", "jane_id", "dup_id") if f"/dashboard/leads/{scene[key]}" in html}
    assert shown == expected
    if not expected:
        assert "No leads match." in html


def test_leads_list_search_by_lead_id_and_angi_id(client, ctx, scene):
    # Rule: an operator can paste our lead id or Angi's lead id into the search box.
    angi_id = db_one(ctx, "SELECT external_lead_id FROM leads WHERE id = ?", (scene["booked_id"],))["external_lead_id"]
    for term in (scene["booked_id"], angi_id):
        html = client.get("/dashboard/leads", params={"q": term}, headers=ADMIN).text
        assert html.count('href="/dashboard/leads/lead_') == 1
        assert "Bea Booker" in html


def test_leads_list_keeps_the_chosen_filters_in_the_form(client, scene):
    # Rule: after filtering, the form still shows what was chosen.
    html = get(client, "/dashboard/leads?tenant=tnt_sparkle&status=booked&duplicates=1&review=1&q=bea").text
    assert '<option value="tnt_sparkle" selected>' in html
    assert '<option value="booked" selected>' in html
    assert 'name="duplicates" value="1" checked' in html
    assert 'name="review" value="1" checked' in html
    assert 'name="q" value="bea"' in html


# --- 3. lead detail ------------------------------------------------------------------------


def test_lead_detail_shows_contact_request_and_routing(client, scene):
    # Rule: everything Angi sent about the lead, and how it was routed, is on one page.
    html = get(client, f"/dashboard/leads/{scene['jane_id']}").text
    for expected in (
        "Jane Doe", 'href="mailto:janedoe@example.com"', 'href="tel:+13035550123"', "123 Main Street, Denver, CO, 80210",
        "Central A/C - Repair", "AC is blowing warm air.", "$25.67", "consented",
        "What kind of location is this?", "Home/Residence", "When do you need this work done?", "Within 1 week",
        "Summit Heating &amp; Air", "tnt_summit", "12345678", "account_id", "angi_leads", "strict",
    ):
        assert expected in html, expected


def test_lead_detail_shows_extra_payload_fields(client, ctx):
    # Rule: fields we do not model are kept and shown, so nothing Angi sends is invisible.
    lead_id = post_lead(client, ads_payload(Email="extra@example.com", PromoCode="SPRING-25")).json()["leadId"]
    html = get(client, f"/dashboard/leads/{lead_id}").text
    assert "Other fields in the payload" in html
    assert "PromoCode" in html and "SPRING-25" in html


def test_lead_detail_timeline_uses_readable_labels(client, scene):
    # Rule: the lifecycle log is shown in plain words, in order, with each event's data.
    html = get(client, f"/dashboard/leads/{scene['booked_id']}").text
    timeline = html[html.index("<h2>Timeline</h2>"):html.index("<h2>Messages</h2>")]
    labels = re.findall(r"<strong>([^<]+)</strong>", timeline)
    assert labels[0] == "Lead received from Angi"
    for label in ("Email queued", "Email sent", "Booking link opened", "Appointment booked", "Pending email cancelled"):
        assert label in labels, label
    assert labels.index("Email sent") < labels.index("Booking link opened") < labels.index("Appointment booked")
    assert "lead.received" not in timeline and "booking.appointment_booked" not in timeline  # no raw type names
    assert "seconds since received:</span> 5.0" in timeline
    assert "reason:</span> booked" in timeline


def test_every_lifecycle_event_type_has_a_label(client):
    # Rule: no event type the application can write is shown as a raw identifier.
    from app import repo

    types = {value for name, value in vars(repo).items() if name.startswith("EV_")}
    assert types and types <= set(dashboard.EVENT_LABELS)
    assert dashboard.event_label("something.new") == "something.new"  # unknown types degrade to the raw name


def test_lead_detail_shows_messages_with_the_audit_copy(client, scene):
    # Rule: each outbox row is listed with status and timing, and what was actually sent can be read.
    html = get(client, f"/dashboard/leads/{scene['booked_id']}").text
    messages = html[html.index("<h2>Messages</h2>"):html.index("<h2>Raw webhook payloads</h2>")]
    assert messages.count("<tr>") == 1 + 4  # header + intro, 2 follow-ups, confirmation
    assert messages.count('<span class="badge good">sent</span>') == 2
    assert messages.count('<span class="badge neutral">cancelled</span>') == 2
    assert "bea@example.com" in messages
    assert "1 / 5" in messages
    assert "booked" in messages  # why the follow-ups were cancelled (last_error)
    assert "<details>" in messages and f"/book/{scene['booked_token']}" in messages  # the body of the intro
    assert "You&#39;re booked" in messages


def test_lead_detail_shows_the_appointment_and_funnel_timestamps(client, ctx, scene):
    # Rule: the booked window is shown in the tenant's local time, alongside the funnel timestamps.
    from app.booking import slot_from_appointment

    appointment = db_one(ctx, "SELECT * FROM appointments WHERE lead_id = ?", (scene["booked_id"],))
    assert appointment["starts_at"] == scene["slot"]
    html = get(client, f"/dashboard/leads/{scene['booked_id']}").text
    assert slot_from_appointment(appointment).label in html  # e.g. "Thu, Nov 12, 2:00 - 4:00 PM"
    assert "Thu, Nov 12" in html
    assert "America/Indiana/Indianapolis" in html
    funnel = html[html.index("<h2>Funnel</h2>"):html.index("<h2>Record the outcome</h2>")]
    assert "2026-11-12 15:00:05 UTC" in funnel  # received
    assert "(5.0 s after it arrived)" in funnel
    assert funnel.count("2026-11-12 15:00:10") == 3  # contacted, engaged and booked within the same second
    unbooked = get(client, f"/dashboard/leads/{scene['jane_id']}").text
    assert "No appointment booked yet." in unbooked


def test_lead_detail_has_a_copyable_booking_link(client, scene):
    # Rule: the operator can copy the homeowner's booking link, built from the public base URL.
    html = get(client, f"/dashboard/leads/{scene['booked_id']}").text
    assert f'value="{BASE_URL}/book/{scene["booked_token"]}"' in html
    assert 'data-copy="booking-link"' in html and 'id="booking-link"' in html
    # It is text to copy, not a link to click: opening it would count as the homeowner engaging.
    assert f'href="{BASE_URL}/book/' not in html and 'href="/book/' not in html


def test_lead_detail_pretty_prints_the_raw_payload(client, scene):
    # Rule: the request exactly as Angi sent it is available, indented, inside a collapsed section.
    html = get(client, f"/dashboard/leads/{scene['jane_id']}").text
    raw = html[html.index("<h2>Raw webhook payloads</h2>"):]
    assert "<details>" in raw and "<pre>{\n" in raw
    assert '  &#34;taskName&#34;: &#34;Central A/C - Repair&#34;' in raw
    assert '    {\n      &#34;answer&#34;: &#34;Home/Residence&#34;' in raw
    assert "processed" in raw and "angi_leads, strict" in raw


def test_lead_detail_shows_every_delivery_of_the_same_lead(client, ctx):
    # Rule: when Angi delivers a lead twice, both raw requests are listed and no second lead exists.
    payload = ads_payload(Email="twice@example.com")
    lead_id = post_lead(client, payload).json()["leadId"]
    assert post_lead(client, payload).json()["result"] == "duplicate_delivery"
    html = get(client, f"/dashboard/leads/{lead_id}").text
    raw = html[html.index("<h2>Raw webhook payloads</h2>"):]
    assert raw.count("<details>") == 2
    assert "duplicate delivery (lead already received)" in raw
    assert "Angi delivered the same lead again" in html
    assert get(client, "/dashboard/leads").text.count('href="/dashboard/leads/lead_') == 1


def test_lead_detail_shows_a_non_json_salvaged_payload_as_received(client, ctx):
    # Rule: a body that was not valid JSON is shown verbatim rather than breaking the page.
    body = ('{“FirstName”: “Sal”, “LastName”: “Vage”, “Email”: sal@example.com, '
            '“PhoneNumber”: “3175550190”, “ALAccountId”: “123456”, “CorrelationId”: “abc-123”}')
    result = post_lead(client, body.encode("utf-8")).json()
    assert result["result"] == "processed"
    html = get(client, f"/dashboard/leads/{result['leadId']}").text
    assert "“FirstName”: “Sal”" in html
    assert ">tolerant parse</span>" in html


def test_duplicate_lead_links_to_its_original_and_its_claim(client, scene):
    # Rule: from a duplicate you can reach the lead it duplicates and the credit claim it produced.
    html = get(client, f"/dashboard/leads/{scene['dup_id']}").text
    section = html[html.index("<h2>Duplicates</h2>"):html.index("<h2>Timeline</h2>")]
    assert f'href="/dashboard/leads/{scene["xss_id"]}"' in section
    assert "same phone and email" in section
    assert f'href="/dashboard/credits?tenant=tnt_sparkle#claim-{scene["claim_id"]}"' in section
    assert "$28.00" in section and '<span class="badge warning">detected</span>' in section
    assert "Recognised as a duplicate of an earlier lead" in html
    # ...and the anchor it points at exists on the credits page.
    assert f'id="claim-{scene["claim_id"]}"' in get(client, "/dashboard/credits?tenant=tnt_sparkle").text


def test_original_lead_links_to_the_duplicates_of_it(client, scene):
    # Rule: from the original you can reach every later lead from the same homeowner.
    html = get(client, f"/dashboard/leads/{scene['xss_id']}").text
    section = html[html.index("<h2>Duplicates</h2>"):html.index("<h2>Timeline</h2>")]
    assert f'href="/dashboard/leads/{scene["dup_id"]}"' in section
    assert "<h2>Duplicates</h2>" not in get(client, f"/dashboard/leads/{scene['jane_id']}").text


@pytest.mark.parametrize("lead_id", ["lead_0000000000000000", "nope", "1", "lead_%", "%00", "lead_x%20OR%201=1"])
def test_unknown_lead_gives_a_404_page(client, scene, lead_id):
    # Rule: an unknown lead id is a friendly 404 page inside the dashboard, never a 500.
    response = get(client, f"/dashboard/leads/{lead_id}")
    assert response.status_code == 404
    assert "Lead not found" in response.text
    assert 'href="/dashboard/leads"' in response.text  # still has the navigation


# --- recording the outcome ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("typed", "cents"),
    [("1250", 125000), ("1,250.50", 125050), ("$980", 98000), (" 75.5 ", 7550), ("0", 0), ("0.01", 1),
     ("999999999.99", 99999999999), ("", None)],
)
def test_recording_a_won_job_stores_its_value_in_cents(client, ctx, scene, typed, cents):
    # Rule: "won" closes the lead and records what the job was worth, entered in dollars.
    lead_id = scene["booked_id"]
    response = post(client, f"/dashboard/leads/{lead_id}/outcome", {"outcome": "won", "job_value": typed})
    assert response.status_code == 303
    assert response.headers["location"] == f"/dashboard/leads/{lead_id}?msg=outcome_saved"
    lead = db_one(ctx, "SELECT status, outcome, job_value_cents, closed_at FROM leads WHERE id = ?", (lead_id,))
    assert (lead["status"], lead["outcome"], lead["job_value_cents"]) == ("won", "won", cents)
    assert lead["closed_at"] is not None


def test_recorded_win_shows_up_on_the_lead_and_in_the_revenue(client, ctx, scene):
    # Rule: after recording a win the operator lands back on the lead, sees it confirmed,
    # and the overview's revenue and return on spend reflect it.
    lead_id = scene["booked_id"]
    response = post(client, f"/dashboard/leads/{lead_id}/outcome", {"outcome": "won", "job_value": "1,250.50"})
    html = get(client, response.headers["location"]).text
    assert "Outcome recorded." in html
    assert "job value $1,250.50" in html
    assert '<span class="badge good">won</span>' in html
    assert "Outcome recorded</strong>" in html  # the timeline entry
    overview = get(client, "/dashboard").text
    assert tile(overview, "Revenue from won jobs") == "$1,250.50"
    assert tile(overview, "Return on spend") == "11.4x"  # $1,250.50 / $109.67
    assert funnel_count(overview, "Won") == 1


def test_recording_a_lost_job_stores_the_reason_and_stops_the_follow_ups(client, ctx, scene):
    # Rule: "lost" closes the lead with a reason and cancels the nudges still queued for it.
    lead_id = scene["jane_id"]
    assert db_one(ctx, "SELECT COUNT(*) AS n FROM outbox_messages WHERE lead_id = ? AND status = 'pending'",
                  (lead_id,))["n"] == 1
    response = post(client, f"/dashboard/leads/{lead_id}/outcome",
                    {"outcome": "lost", "reason": "  Went with a cheaper quote  ", "job_value": "500"})
    assert response.status_code == 303
    lead = db_one(ctx, "SELECT status, outcome, outcome_reason, job_value_cents FROM leads WHERE id = ?", (lead_id,))
    assert lead == {"status": "lost", "outcome": "lost", "outcome_reason": "Went with a cheaper quote",
                    "job_value_cents": None}  # a job value typed next to "lost" is ignored
    assert db_one(ctx, "SELECT COUNT(*) AS n FROM outbox_messages WHERE lead_id = ? AND status = 'pending'",
                  (lead_id,))["n"] == 0
    html = get(client, response.headers["location"]).text
    assert "Went with a cheaper quote" in html


@pytest.mark.parametrize("typed", ["abc", "-5", "1e30", "12.345", "NaN", "Infinity", "1.2.3", "5 dollars", "1" * 10, "٥٠"])
def test_job_value_that_is_not_a_dollar_amount_is_refused(client, ctx, scene, typed):
    # Rule: a job value must be a plain non-negative dollar amount; anything else changes nothing.
    lead_id = scene["booked_id"]
    response = post(client, f"/dashboard/leads/{lead_id}/outcome", {"outcome": "won", "job_value": typed})
    assert response.status_code == 303
    assert response.headers["location"] == f"/dashboard/leads/{lead_id}?msg=amount_invalid"
    lead = db_one(ctx, "SELECT status, outcome, job_value_cents FROM leads WHERE id = ?", (lead_id,))
    assert lead == {"status": "booked", "outcome": None, "job_value_cents": None}
    assert "not a valid dollar amount" in get(client, response.headers["location"]).text


@pytest.mark.parametrize("outcome", ["", "maybe", "WON", "won ", "booked"])
def test_outcome_must_be_won_or_lost(client, ctx, scene, outcome):
    # Rule: the only outcomes are won and lost.
    lead_id = scene["booked_id"]
    response = post(client, f"/dashboard/leads/{lead_id}/outcome", {"outcome": outcome, "job_value": "100"})
    assert response.status_code == 303
    assert response.headers["location"] == f"/dashboard/leads/{lead_id}?msg=outcome_invalid"
    assert db_one(ctx, "SELECT outcome FROM leads WHERE id = ?", (lead_id,))["outcome"] is None


def test_outcome_for_an_unknown_lead_is_a_404(client, scene):
    # Rule: posting to a lead that does not exist is a 404 page, whatever the form says.
    for data in ({"outcome": "won", "job_value": "100"}, {"outcome": "nonsense"}, {}):
        response = post(client, "/dashboard/leads/lead_missing/outcome", data)
        assert response.status_code == 404
        assert "Lead not found" in response.text


def test_outcome_can_be_corrected(client, ctx, scene):
    # Rule: a mistaken outcome can be overwritten; the latest entry wins.
    lead_id = scene["booked_id"]
    post(client, f"/dashboard/leads/{lead_id}/outcome", {"outcome": "lost", "reason": "typo"})
    post(client, f"/dashboard/leads/{lead_id}/outcome", {"outcome": "won", "job_value": "300"})
    lead = db_one(ctx, "SELECT status, outcome, job_value_cents FROM leads WHERE id = ?", (lead_id,))
    assert lead == {"status": "won", "outcome": "won", "job_value_cents": 30000}


# --- cross-site requests and redirects -------------------------------------------------------


def form_posts(scene: dict[str, Any], alert_id: int) -> list[tuple[str, dict[str, str]]]:
    return [
        (f"/dashboard/leads/{scene['booked_id']}/outcome", {"outcome": "won", "job_value": "100"}),
        (f"/dashboard/messages/{scene['message_id']}/retry", {}),
        (f"/dashboard/credits/{scene['claim_id']}/status", {"status": "submitted"}),
        (f"/dashboard/alerts/{alert_id}/resolve", {}),
        (f"/dashboard/events/{scene['quarantined_event_id']}/replay", {}),
        (f"/dashboard/events/{scene['quarantined_event_id']}/assign", {"tenant_id": "tnt_blueline"}),
        ("/dashboard/events/replay-quarantined", {}),
        (f"/dashboard/events/{scene['quarantined_event_id']}/discard", {}),
        ("/dashboard/tenants", {"slug": "csrf-co", "name": "CSRF Co"}),
        ("/dashboard/tenants/tnt_sparkle/lead-sources", {"external_account_id": UNMAPPED_ACCOUNT}),
    ]


def state(ctx) -> dict[str, Any]:
    """Everything a dashboard form can change."""
    return {
        "leads": db_rows(ctx, "SELECT id, status, outcome, job_value_cents FROM leads ORDER BY id"),
        "claims": db_rows(ctx, "SELECT id, status, credited_cents FROM credit_claims ORDER BY id"),
        "alerts": db_rows(ctx, "SELECT id, status, count FROM alerts ORDER BY id"),
        "events": db_rows(ctx, "SELECT id, status, attempts, operator_tenant_id FROM webhook_events ORDER BY id"),
        "messages": db_rows(ctx, "SELECT id, status, attempts, send_after FROM outbox_messages ORDER BY id"),
        "tenants": db_rows(ctx, "SELECT id FROM tenants ORDER BY id"),
        "sources": db_rows(ctx, "SELECT external_account_id, tenant_id FROM tenant_lead_sources ORDER BY 1"),
    }


@pytest.mark.parametrize("origin", ["https://evil.example", "http://testserver.evil.example", "null", "",
                                    "https://testserver:8443", "http://[bad"])
def test_cross_site_form_posts_are_refused_and_change_nothing(client, ctx, scene, origin):
    # Rule: a form posted from another site (Origin host differs from ours) is refused with 403.
    # The values are the ones a browser can send: an origin with its scheme, or "null".
    alert_id = db_one(ctx, "SELECT id FROM alerts WHERE status = 'open' ORDER BY id")["id"]
    before = state(ctx)
    for path, data in form_posts(scene, alert_id):
        response = post(client, path, data, headers={"Origin": origin})
        assert response.status_code == 403, path
    assert state(ctx) == before


def test_same_origin_form_posts_are_accepted(client, ctx, scene):
    # Rule: a form posted from the dashboard itself (Origin host equals the request Host) works,
    # and every action answers with a 303 redirect back into the dashboard.
    alert_id = db_one(ctx, "SELECT id FROM alerts WHERE status = 'open' ORDER BY id")["id"]
    for origin in ("http://testserver", "https://TESTSERVER"):
        for path, data in form_posts(scene, alert_id):
            if path == "/dashboard/tenants":
                data = {**data, "slug": f"csrf-{origin[:5].strip(':').lower()}"}
            response = post(client, path, data, headers={"Origin": origin})
            assert response.status_code == 303, (path, origin)
            assert response.headers["location"].startswith("/dashboard"), path
    assert state(ctx)["leads"] != []


def test_form_posts_without_an_origin_header_are_accepted(client, ctx, scene):
    # Rule: the Origin check only applies when the header is present (scripts and curl do not send it).
    response = post(client, f"/dashboard/credits/{scene['claim_id']}/status", {"status": "submitted"})
    assert response.status_code == 303
    assert db_one(ctx, "SELECT status FROM credit_claims WHERE id = ?", (scene["claim_id"],))["status"] == "submitted"


@pytest.mark.parametrize(
    "next_url",
    [
        "https://evil.example/dashboard",
        "//evil.example/dashboard",
        "/\\evil.example",
        "/admin/api/leads",
        "/dashboardevil",
        "/",
        "javascript:alert(1)",
        "dashboard/credits",
        "http://[bad",
        "",
    ],
)
def test_redirect_after_a_form_never_leaves_the_dashboard(client, scene, next_url):
    # Rule: the "return to" address in a form is only honoured when it is a dashboard page.
    response = post(client, f"/dashboard/credits/{scene['claim_id']}/status", {"status": "submitted", "next": next_url})
    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard/credits?msg=claim_updated"


def test_redirect_after_a_form_returns_to_the_filtered_page_it_came_from(client, scene):
    # Rule: after an action the operator is back on the page they were on, filters and live mode
    # included, with the old message replaced by the new one.
    response = post(
        client,
        f"/dashboard/credits/{scene['claim_id']}/status",
        {"status": "submitted", "next": "/dashboard/credits?tenant=tnt_sparkle&status=detected&msg=amount_invalid&n=9"},
    )
    assert response.headers["location"] == "/dashboard/credits?tenant=tnt_sparkle&status=detected&msg=claim_updated"
    replay = post(client, f"/dashboard/events/{scene['quarantined_event_id']}/replay",
                  {"next": "/dashboard/monitoring?status=quarantined&live=1"})
    assert replay.headers["location"] == (
        "/dashboard/monitoring?status=quarantined&live=1&msg=replay_quarantined_unmapped_account"
    )


def test_forms_carry_the_page_they_are_on(client, scene):
    # Rule: every action form on a filtered page knows where to send the operator back to.
    html = get(client, "/dashboard/credits?tenant=tnt_sparkle&msg=claim_updated").text
    assert '<input type="hidden" name="next" value="/dashboard/credits?tenant=tnt_sparkle">' in html
    monitoring = get(client, "/dashboard/monitoring?status=quarantined&live=1").text
    assert monitoring.count('name="next" value="/dashboard/monitoring?status=quarantined&amp;live=1"') >= 3


# --- 4. credits ----------------------------------------------------------------------------


def test_credits_page_lists_the_claim_with_its_evidence(client, ctx, scene):
    # Rule: each duplicate has a claim row with both leads, why they match, the amount, the
    # deadline (30 days after the charge), eligibility, and text ready to paste into Angi.
    html = get(client, "/dashboard/credits").text
    leads = {row["id"]: row for row in db_rows(ctx, "SELECT id, external_lead_id FROM leads")}
    row = claim_block(html, scene["claim_id"])
    assert "Sparkle Home Cleaning" in row
    assert f'href="/dashboard/leads/{scene["dup_id"]}"' in row and f'href="/dashboard/leads/{scene["xss_id"]}"' in row
    assert leads[scene["dup_id"]]["external_lead_id"] in row
    assert leads[scene["xss_id"]]["external_lead_id"] in row
    assert "phone, email" in row
    assert "5.0 h" in row  # how far apart the two leads arrived
    assert "high" in row
    assert "$28.00" in row
    assert "2026-12-12 20:00" in row and "30 d left" in row
    assert '<span class="badge good">eligible</span>' in row
    assert '<span class="badge warning">detected</span>' in row
    assert "Duplicate lead. Lead " in row and "Requesting a credit of $28.00." in row
    assert f'data-copy="evidence-{scene["claim_id"]}"' in row and f'id="evidence-{scene["claim_id"]}"' in row


def test_credits_summary_tiles(client, ctx, scene, clock):
    # Rule: the tiles add up what can be filed, what is with Angi, what came back and what is urgent.
    html = get(client, "/dashboard/credits").text
    assert tile(html, "Ready to file") == "$28.00"
    assert tile(html, "Submitted to Angi") == "$0.00"
    assert tile(html, "Recovered") == "$0.00"
    assert tile(html, "Expiring within 7 days") == "0"

    clock.advance(days=24)
    assert tile(get(client, "/dashboard/credits").text, "Expiring within 7 days") == "1"

    post(client, f"/dashboard/credits/{scene['claim_id']}/status", {"status": "submitted"})
    html = get(client, "/dashboard/credits").text
    assert tile(html, "Ready to file") == "$0.00"
    assert tile(html, "Submitted to Angi") == "$28.00"
    assert tile(html, "Expiring within 7 days") == "0"  # filed, so no longer urgent

    post(client, f"/dashboard/credits/{scene['claim_id']}/status", {"status": "credited", "credited": "20"})
    html = get(client, "/dashboard/credits").text
    assert tile(html, "Recovered") == "$20.00"
    assert tile(html, "Submitted to Angi") == "$0.00"
    assert "credited $20.00" in html


def test_credits_highlights_claims_whose_deadline_is_within_a_week(client, scene, clock):
    # Rule: an unfiled claim with 7 days or less left is highlighted; with more time it is not.
    def claim_row() -> str:
        return claim_block(get(client, "/dashboard/credits").text, scene["claim_id"])

    assert 'class="attention"' not in claim_row()
    clock.advance(days=22)  # 8 days left
    row = claim_row()
    assert 'class="attention"' not in row and "8 d left" in row and "<strong>" not in row
    clock.advance(days=1)  # 7 days left: inside the warning window
    row = claim_row()
    assert 'class="attention"' in row and "<strong>7 d left</strong>" in row
    clock.advance(days=6, hours=20)
    assert "<strong>under a day left</strong>" in claim_row()
    # Once it has been filed, the deadline no longer needs attention.
    post(client, f"/dashboard/credits/{scene['claim_id']}/status", {"status": "submitted"})
    assert 'class="attention"' not in claim_row()


def test_credits_shows_why_a_claim_is_not_eligible(client, ctx, scene, clock):
    # Rule: Angi's conditions are checked per claim; a missed deadline is spelled out as a blocker.
    clock.advance(days=31)
    html = get(client, "/dashboard/credits").text
    assert '<span class="badge warning">not eligible yet</span>' in html
    assert "the deadline for requesting this credit has passed" in html
    assert "overdue" in html
    assert tile(html, "Ready to file") == "$0.00"


def test_credits_flags_a_duplicate_that_was_never_contacted(client, ctx, clock):
    # Rule: Angi expects a contact attempt within 24 hours; a duplicate with none is not eligible yet.
    post_lead(client, ads_payload(Email="first@example.com", PhoneNumber="3175550180"))
    clock.advance(hours=1)
    duplicate = post_lead(client, ads_payload(Email="", PhoneNumber="3175550180")).json()  # no address to email
    assert duplicate["duplicate"]["matchedOn"] == ["phone"]
    html = get(client, "/dashboard/credits").text
    assert "no contact attempt has been recorded for this lead yet" in html
    assert '<span class="badge warning">not eligible yet</span>' in html


def test_credits_page_explains_angis_rules_from_the_settings(client):
    # Rule: the page states the rules as implemented (45-day duplicate window, 30 days to ask,
    # contact within 24 hours, filed by hand because there is no API).
    html = get(client, "/dashboard/credits").text
    text = " ".join(html.split())
    assert "same homeowner charged twice within 45 days" in text
    assert "within 30 days of the charge" in text
    assert "within 24 hours" in text
    assert "Angi for Pros app (there is no API)" in text


def test_credits_export_link_carries_the_filters_and_works(client, scene):
    # Rule: "Export CSV" downloads exactly the claims being looked at.
    html = get(client, "/dashboard/credits?tenant=tnt_sparkle&status=detected").text
    assert 'href="/admin/api/claims/export.csv?tenant_id=tnt_sparkle&amp;status=detected">Export CSV</a>' in html
    assert 'href="/admin/api/claims/export.csv">Export CSV</a>' in get(client, "/dashboard/credits").text
    export = get(client, "/admin/api/claims/export.csv?tenant_id=tnt_sparkle&status=detected")
    assert export.status_code == 200
    assert export.headers["content-type"].startswith("text/csv")
    assert scene["claim_id"] in export.text
    assert scene["claim_id"] not in get(client, "/admin/api/claims/export.csv?tenant_id=tnt_summit").text


@pytest.mark.parametrize(
    ("query", "shown"),
    [("", True), ("tenant=tnt_sparkle", True), ("tenant=tnt_summit", False), ("status=detected", True),
     ("status=submitted", False), ("status=bogus", True), ("tenant=tnt_sparkle&status=credited", False)],
)
def test_credits_filters(client, scene, query, shown):
    # Rule: claims can be narrowed by tenant and status; an unknown status is ignored.
    html = get(client, f"/dashboard/credits?{query}").text
    assert (f'id="claim-{scene["claim_id"]}"' in html) is shown
    if not shown:
        assert "No credit claims match these filters" in html


@pytest.mark.parametrize(
    ("form", "expected"),
    [
        ({"status": "submitted"}, {"status": "submitted", "credited_cents": None, "submitted": True, "resolved": False}),
        ({"status": "credited"}, {"status": "credited", "credited_cents": 2800, "submitted": True, "resolved": True}),
        ({"status": "credited", "credited": "14.00"},
         {"status": "credited", "credited_cents": 1400, "submitted": True, "resolved": True}),
        ({"status": "credited", "credited": "0"},
         {"status": "credited", "credited_cents": 0, "submitted": True, "resolved": True}),
        ({"status": "denied", "credited": "14.00"},
         {"status": "denied", "credited_cents": None, "submitted": True, "resolved": True}),
        ({"status": "dismissed"}, {"status": "dismissed", "credited_cents": None, "submitted": False, "resolved": True}),
        ({"status": "submitted", "credited": "not a number"},  # the amount only matters for "credited"
         {"status": "submitted", "credited_cents": None, "submitted": True, "resolved": False}),
    ],
)
def test_claim_status_can_be_moved_through_angis_process(client, ctx, scene, form, expected):
    # Rule: a claim moves to submitted / credited (optionally with the amount actually credited,
    # default the full fee) / denied / dismissed, and the timestamps follow.
    response = post(client, f"/dashboard/credits/{scene['claim_id']}/status", form)
    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard/credits?msg=claim_updated"
    claim = db_one(ctx, "SELECT * FROM credit_claims WHERE id = ?", (scene["claim_id"],))
    assert claim["status"] == expected["status"]
    assert claim["credited_cents"] == expected["credited_cents"]
    assert (claim["submitted_at"] is not None) is expected["submitted"]
    assert (claim["resolved_at"] is not None) is expected["resolved"]
    page = get(client, response.headers["location"]).text
    assert "Claim updated." in page
    assert f'<span class="badge {dashboard.badge_tone(expected["status"])}">{expected["status"]}</span>' in page


def test_dismissed_claim_can_be_reopened(client, ctx, scene):
    # Rule: a claim dismissed by mistake can be put back on the worklist.
    post(client, f"/dashboard/credits/{scene['claim_id']}/status", {"status": "dismissed"})
    post(client, f"/dashboard/credits/{scene['claim_id']}/status", {"status": "detected"})
    claim = db_one(ctx, "SELECT status, resolved_at FROM credit_claims WHERE id = ?", (scene["claim_id"],))
    assert claim == {"status": "detected", "resolved_at": None}
    assert tile(get(client, "/dashboard/credits").text, "Ready to file") == "$28.00"


@pytest.mark.parametrize(
    ("form", "message"),
    [
        ({}, "claim_invalid"),
        ({"status": ""}, "claim_invalid"),
        ({"status": "paid"}, "claim_invalid"),
        ({"status": "CREDITED"}, "claim_invalid"),
        ({"status": "credited", "credited": "lots"}, "amount_invalid"),
        ({"status": "credited", "credited": "-1"}, "amount_invalid"),
        ({"status": "credited", "credited": "1e9"}, "amount_invalid"),
    ],
)
def test_invalid_claim_update_changes_nothing(client, ctx, scene, form, message):
    # Rule: an unknown status or a malformed amount is refused with a message; the claim is untouched.
    response = post(client, f"/dashboard/credits/{scene['claim_id']}/status", form)
    assert response.status_code == 303
    assert response.headers["location"] == f"/dashboard/credits?msg={message}"
    claim = db_one(ctx, "SELECT status, credited_cents FROM credit_claims WHERE id = ?", (scene["claim_id"],))
    assert claim == {"status": "detected", "credited_cents": None}
    assert 'class="notice critical"' in get(client, response.headers["location"]).text


def test_claim_update_for_an_unknown_claim_is_a_404(client, scene):
    # Rule: an unknown claim id is a 404 page, whatever the form says.
    for form in ({"status": "submitted"}, {"status": "bogus"}, {}):
        response = post(client, "/dashboard/credits/clm_missing/status", form)
        assert response.status_code == 404
        assert "Claim not found" in response.text


# --- 5. monitoring -------------------------------------------------------------------------


def test_monitoring_banner_reports_all_clear_when_nothing_is_wrong(client):
    # Rule: with no problems the banner says so and no alert count is shown in the navigation.
    html = get(client, "/dashboard/monitoring").text
    assert '<div class="notice good" role="status">' in html
    assert "all checks pass" in html and "Every health check passes." in html
    assert 'class="count"' not in html
    assert "No open alerts." in html and "No payloads received yet." in html and "No webhook events yet." in html


def test_monitoring_lists_every_health_check(client, scene):
    # Rule: each health check is listed with its status and a one-line summary.
    html = get(client, "/dashboard/monitoring").text
    checks = html[html.index("<h2>Health checks</h2>"):html.index("<h2>Open alerts</h2>")]
    for name in ("ingest_errors", "unprocessed", "quarantine", "email_backlog", "email_failures", "canary",
                 "lead_silence:sparkle-home-cleaning"):
        assert f'<td class="mono">{name}</td>' in checks, name
    quarantine = checks[checks.index(">quarantine<"):]
    quarantine = quarantine[:quarantine.index("</tr>")]
    assert '<span class="badge warning">warning</span>' in quarantine
    assert "1 lead(s) are quarantined and waiting for action (1 unmapped_account)" in quarantine
    # Both machine endpoints sit behind the admin token, and the page says so.
    assert 'href="/metrics"' in checks and 'href="/admin/api/health"' in checks
    assert "with the admin token" in checks


def test_monitoring_banner_and_badge_follow_the_checks_and_alerts(client, ctx, scene):
    # Rule: the banner shows the worst check status, and the navigation badge counts open alerts.
    html = get(client, "/dashboard/monitoring").text
    open_alerts = db_one(ctx, "SELECT COUNT(*) AS n FROM alerts WHERE status = 'open'")["n"]
    assert open_alerts >= 1
    assert '<div class="notice warning" role="status">' in html
    assert "needs attention" in html
    assert "1 of " in html and "health checks are not passing" in html
    assert f"{open_alerts} open alert(s) below still need a look." in html
    for path in PAGES:  # the badge is on every page
        assert f'<span class="count" title="{open_alerts} open alert(s)">{open_alerts}</span>' in get(client, path).text


def test_monitoring_banner_turns_critical_when_events_keep_failing(client, ctx):
    # Rule: a burst of events that cannot become leads is a critical condition (possible format change).
    for _ in range(3):
        post_lead(client, b"this is not json")
    html = get(client, "/dashboard/monitoring").text
    assert '<div class="notice critical" role="status">' in html
    assert "action needed" in html
    assert '<span class="badge critical">critical</span>' in html
    assert html.count('<span class="badge critical">rejected</span>') == 3
    assert "this is not json" in html  # the body preview


def test_monitoring_shows_open_alerts_with_detail_and_a_resolve_button(client, ctx, scene):
    # Rule: an open alert shows severity, title, count, first/last seen, its detail, and can be resolved.
    alert = db_one(ctx, "SELECT * FROM alerts WHERE kind = 'unmapped_account'")
    html = get(client, "/dashboard/monitoring").text
    alerts = html[html.index("<h2>Open alerts</h2>"):html.index("<h2>Payload shapes seen</h2>")]
    assert '<span class="badge critical">critical</span>' in alerts
    assert f"Lead quarantined: Angi account {UNMAPPED_ACCOUNT} ({XSS_ESCAPED} Plumbing) is not mapped to a tenant" in alerts
    assert f'action="/dashboard/alerts/{alert["id"]}/resolve"' in alerts
    assert f"&#34;external_account_id&#34;: &#34;{UNMAPPED_ACCOUNT}&#34;" in alerts  # the detail JSON, indented
    assert "2026-11-12 20:00" in alerts


def test_resolving_an_alert_moves_it_to_recently_resolved(client, ctx, scene):
    # Rule: Resolve closes the alert, the badge count drops, and it stays visible under "recently resolved".
    alert = db_one(ctx, "SELECT * FROM alerts WHERE kind = 'unmapped_account'")
    before = db_one(ctx, "SELECT COUNT(*) AS n FROM alerts WHERE status = 'open'")["n"]
    response = post(client, f"/dashboard/alerts/{alert['id']}/resolve")
    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard/monitoring?msg=alert_resolved"
    row = db_one(ctx, "SELECT status, resolved_at FROM alerts WHERE id = ?", (alert["id"],))
    assert row["status"] == "resolved" and row["resolved_at"] is not None

    html = get(client, response.headers["location"]).text
    assert "Alert resolved." in html
    assert before == 1
    assert html.count("No open alerts.") == 2  # in the banner and in the (now empty) list
    assert 'class="count"' not in html  # the navigation badge is gone
    open_section = html[html.index("<h2>Open alerts</h2>"):html.index("Recently resolved")]
    assert UNMAPPED_ACCOUNT not in open_section
    resolved_section = html[html.index("Recently resolved (1)"):html.index("<h2>Payload shapes seen</h2>")]
    assert UNMAPPED_ACCOUNT in resolved_section
    assert "/resolve" not in resolved_section  # nothing left to do on a resolved alert


def test_resolving_an_alert_twice_is_harmless(client, ctx, scene):
    # Rule: a second click on Resolve (stale page, double click) is acknowledged, not an error.
    alert_id = db_one(ctx, "SELECT id FROM alerts WHERE kind = 'unmapped_account'")["id"]
    post(client, f"/dashboard/alerts/{alert_id}/resolve")
    again = post(client, f"/dashboard/alerts/{alert_id}/resolve")
    assert again.status_code == 303
    assert again.headers["location"] == "/dashboard/monitoring?msg=alert_already_resolved"
    assert "already resolved" in get(client, again.headers["location"]).text


@pytest.mark.parametrize("alert_id", ["999999", "0", "abc", "-1", "1.5", "99999999999999999999999999", "²", "1%20OR%201=1"])
def test_resolving_an_unknown_alert_is_a_404(client, ctx, scene, alert_id):
    # Rule: an alert id that does not exist (or is not a number at all) is a 404 page; nothing is resolved.
    response = post(client, f"/dashboard/alerts/{alert_id}/resolve")
    assert response.status_code == 404
    assert "Alert not found" in response.text
    assert db_one(ctx, "SELECT COUNT(*) AS n FROM alerts WHERE status = 'resolved'")["n"] == 0


def test_monitoring_shows_payload_shapes_clean_and_drifted(client, ctx, scene):
    # Rule: every distinct payload shape is listed; a drifted one names the fields that differ.
    drifted = ads_payload(Email="shape@example.com", PhoneNumber="3175550175", LoyaltyTier="gold")
    del drifted["Urgency"]
    post_lead(client, drifted)
    html = get(client, "/dashboard/monitoring").text
    shapes = html[html.index("<h2>Payload shapes seen</h2>"):html.index("Recent webhook events")]
    assert '<span class="badge good">clean</span>' in shapes
    assert '<span class="badge warning">drifted</span>' in shapes
    assert "angi_ads" in shapes and "angi_leads" in shapes
    assert "New fields:</span> LoyaltyTier" in shapes
    stored = db_rows(ctx, "SELECT signature, seen_count FROM payload_shapes")
    for shape in stored:
        assert shape["signature"] in shapes
    clean_ads = next(s for s in stored if s["signature"].startswith("angi_ads") and s["seen_count"] == 3)
    row = shapes[shapes.index(clean_ads["signature"]):]
    assert re.search(r'<td class="num">3</td>', row[:row.index("</tr>")])


def test_monitoring_lists_recent_webhook_events(client, ctx, scene):
    # Rule: every stored request is listed with status, reason, format, parse mode, timing, tenant and lead.
    html = get(client, "/dashboard/monitoring").text
    events = html[html.index("Recent webhook events"):]
    assert events.count('<span class="badge good">processed</span>') == 4
    assert events.count('<span class="badge warning">quarantined</span>') == 1
    # The reason code the health check uses, and under it what the event is waiting for, in words.
    assert "<td>unmapped_account</td>" in events
    assert "Its Angi account is not mapped to a tenant. Add the mapping on the Tenants page." in events
    assert "angi_ads" in events and "angi_leads" in events and "strict" in events
    assert re.search(r"\d+\.\d ms", events)
    assert "Sparkle Home Cleaning" in events and "Summit Heating &amp; Air" in events
    for key in ("xss_id", "booked_id", "jane_id", "dup_id"):
        assert f'href="/dashboard/leads/{scene[key]}"' in events
    assert "Quinn Quarantine" in events  # the body preview of the quarantined request


def test_monitoring_offers_replay_and_discard_only_where_they_make_sense(client, ctx, scene):
    # Rule: Replay and Discard are offered on quarantined / failed / rejected events, not on
    # processed ones (a processed event is a lead; there is nothing to redo or to throw away).
    post_lead(client, b"[1, 2, 3]")  # rejected: not a JSON object
    html = get(client, "/dashboard/monitoring").text
    replay_forms = re.findall(r'action="/dashboard/events/(evt_[0-9a-f]+)/replay"', html)
    discard_forms = re.findall(r'action="/dashboard/events/(evt_[0-9a-f]+)/discard"', html)
    waiting = {row["id"] for row in db_rows(ctx, "SELECT id FROM webhook_events WHERE status IN ('quarantined', 'rejected')")}
    assert set(replay_forms) == waiting and len(replay_forms) == 2
    assert set(discard_forms) == waiting and len(discard_forms) == 2
    assert "Replay all quarantined (1)" in html
    # The unmapped account can be fixed with a mapping, so nobody is asked to pick a tenant by hand.
    assert "/assign" not in html


@pytest.mark.parametrize(
    ("status", "count"),
    [("quarantined", 1), ("processed", 4), ("failed", 0), ("rejected", 0), ("duplicate_delivery", 0), ("discarded", 0),
     ("bogus", 5), ("", 5)],
)
def test_monitoring_event_status_filter(client, scene, status, count):
    # Rule: the event list can be narrowed to one status; an unknown status shows everything.
    html = client.get("/dashboard/monitoring", params={"status": status}, headers=ADMIN).text
    events = html[html.index("Recent webhook events"):]
    assert events.count("<summary>Preview</summary>") == count
    if count == 0:
        assert "No webhook events with this status yet." in events


def test_monitoring_supports_live_mode(client, scene):
    # Rule: the monitoring page can reload itself too, keeping its status filter.
    html = get(client, "/dashboard/monitoring?status=quarantined&live=1").text
    assert '<meta http-equiv="refresh" content="10; url=/dashboard/monitoring?status=quarantined&amp;live=1">' in html
    assert '<a href="/dashboard/monitoring?status=quarantined">turn off</a>' in html
    assert "http-equiv" not in get(client, "/dashboard/monitoring").text


def test_replaying_a_quarantined_event_without_a_fix_leaves_it_quarantined(client, ctx, scene):
    # Rule: replay re-runs the stored request; if the cause is not fixed it stays quarantined and says so.
    event_id = scene["quarantined_event_id"]
    response = post(client, f"/dashboard/events/{event_id}/replay")
    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard/monitoring?msg=replay_quarantined_unmapped_account"
    event = db_one(ctx, "SELECT status, attempts FROM webhook_events WHERE id = ?", (event_id,))
    assert event == {"status": "quarantined", "attempts": 2}
    assert db_one(ctx, "SELECT COUNT(*) AS n FROM leads")["n"] == 4
    html = get(client, response.headers["location"]).text
    assert "Event replayed, but it is still quarantined: its Angi account is not mapped to a tenant." in html


def test_replaying_a_quarantined_event_after_the_mapping_exists_creates_the_lead(client, ctx, scene):
    # Rule: once the Angi account is mapped, replaying the stored request turns it into a lead
    # whose speed-to-lead clock started when the request first arrived.
    mapped = client.post("/admin/api/tenants/tnt_blueline/lead-sources",
                         json={"external_account_id": "000"}, headers=ADMIN)  # an unrelated mapping: replays nothing useful
    assert mapped.status_code == 201
    with ctx.db.transaction() as conn:
        from app.tenants import add_mapping

        add_mapping(conn, tenant_id="tnt_blueline", source="angi", external_account_id=UNMAPPED_ACCOUNT,
                    external_account_name=None, now=ctx.now())
    received_at = db_one(ctx, "SELECT received_at FROM webhook_events WHERE id = ?", (scene["quarantined_event_id"],))
    response = post(client, f"/dashboard/events/{scene['quarantined_event_id']}/replay")
    assert response.headers["location"] == "/dashboard/monitoring?msg=replay_processed"
    lead = db_one(ctx, "SELECT tenant_id, full_name, received_at FROM leads WHERE email = 'quinn@example.com'")
    assert lead == {"tenant_id": "tnt_blueline", "full_name": "Quinn Quarantine", "received_at": received_at["received_at"]}
    assert "it is now a lead" in get(client, response.headers["location"]).text


def test_replaying_a_processed_event_does_nothing(client, ctx, scene):
    # Rule: only events that still need work are replayed; a processed one is left exactly as it is.
    event = db_one(ctx, "SELECT id, attempts FROM webhook_events WHERE lead_id = ?", (scene["booked_id"],))
    before = state(ctx)
    lead_events = db_one(ctx, "SELECT COUNT(*) AS n FROM lead_events")["n"]
    response = post(client, f"/dashboard/events/{event['id']}/replay")
    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard/monitoring?msg=replay_not_needed"
    assert state(ctx) == before
    assert db_one(ctx, "SELECT COUNT(*) AS n FROM lead_events")["n"] == lead_events


def test_replaying_a_rejected_event_reports_that_it_is_still_unreadable(client, ctx):
    # Rule: a body that is not JSON cannot be fixed by replaying it; the operator is told so.
    event_id = post_lead(client, b"<xml>not json</xml>").json()["eventId"]
    response = post(client, f"/dashboard/events/{event_id}/replay")
    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard/monitoring?msg=replay_rejected"
    assert db_one(ctx, "SELECT status FROM webhook_events WHERE id = ?", (event_id,))["status"] == "rejected"
    html = get(client, response.headers["location"]).text
    assert "still cannot be read as a JSON object" in html
    assert "&lt;xml&gt;not json&lt;/xml&gt;" in html and "<xml>" not in html


@pytest.mark.parametrize("event_id", ["evt_missing", "1", "replay", "%27"])
def test_replaying_an_unknown_event_is_a_404(client, scene, event_id):
    # Rule: an unknown webhook event id is a 404 page.
    response = post(client, f"/dashboard/events/{event_id}/replay")
    assert response.status_code == 404
    assert "Webhook event not found" in response.text


def test_replay_all_quarantined_processes_what_can_now_be_processed(client, ctx, scene):
    # Rule: "Replay all quarantined" re-runs every quarantined event and reports how many became leads.
    post_lead(client, ads_payload(ALAccountId="424242", Email="other@example.com", PhoneNumber="3175550185"))
    with ctx.db.transaction() as conn:
        from app.tenants import add_mapping

        add_mapping(conn, tenant_id="tnt_blueline", source="angi", external_account_id=UNMAPPED_ACCOUNT,
                    external_account_name=None, now=ctx.now())
    response = post(client, "/dashboard/events/replay-quarantined", {"next": "/dashboard/monitoring?status=quarantined"})
    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard/monitoring?status=quarantined&msg=replayed_all&n=2&ok=1"
    statuses = sorted(row["status"] for row in db_rows(ctx, "SELECT status FROM webhook_events"))
    assert statuses.count("quarantined") == 1 and statuses.count("processed") == 5
    html = get(client, response.headers["location"]).text
    assert "Replayed 2 quarantined event(s); 1 became leads." in html
    assert "Replay all quarantined (1)" in html


def test_replay_all_quarantined_with_nothing_to_do(client, ctx):
    # Rule: with an empty quarantine the action is a harmless no-op, and the button is disabled.
    assert "disabled>Replay all quarantined (0)" in get(client, "/dashboard/monitoring").text
    response = post(client, "/dashboard/events/replay-quarantined")
    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard/monitoring?msg=replayed_all&n=0&ok=0"


# --- operator actions: retry a failed email, discard an event, assign a tenant -----------------


def failed_intro(client, ctx, mailer, clock) -> dict[str, Any]:
    """A lead whose intro email the mail server refused for good (so we gave up on it)."""
    mailer.fail_with = PermanentEmailError("550 mailbox unavailable")
    mailer.fail_times = 1
    lead_id = post_lead(client, ads_payload(Email="bounced@example.com", PhoneNumber="3175550111")).json()["leadId"]
    deliver(ctx, clock)
    message = db_one(ctx, "SELECT * FROM outbox_messages WHERE lead_id = ? AND template = 'intro'", (lead_id,))
    assert message["status"] == "failed" and mailer.sent == []
    return message


def test_failed_email_can_be_retried_from_the_lead_page(client, ctx, mailer, clock):
    # Rule: an email we gave up on shows a Retry button; pressing it queues the message again
    # with a fresh set of attempts, and the worker sends it.
    message = failed_intro(client, ctx, mailer, clock)
    path = f"/dashboard/leads/{message['lead_id']}"
    html = get(client, path).text
    assert '<span class="badge critical">failed</span>' in html and "550 mailbox unavailable" in html
    assert html.count(f'action="/dashboard/messages/{message["id"]}/retry"') == 1

    response = post(client, f"/dashboard/messages/{message['id']}/retry", {"next": path})
    assert response.status_code == 303
    assert response.headers["location"] == f"{path}?msg=message_requeued"
    queued = db_one(ctx, "SELECT status, attempts, last_error, send_after FROM outbox_messages WHERE id = ?", (message["id"],))
    assert queued == {"status": "pending", "attempts": 0, "last_error": None, "send_after": "2026-11-12T15:00:05.000Z"}
    html = get(client, response.headers["location"]).text
    assert "Email queued again; it is sent within a few seconds." in html
    assert "/retry" not in html  # nothing failed any more, so nothing to retry

    deliver(ctx, clock, seconds=1)  # the mail server accepts it this time
    assert [email.to for email in mailer.sent] == ["bounced@example.com"]
    sent = db_one(ctx, "SELECT status, attempts FROM outbox_messages WHERE id = ?", (message["id"],))
    assert sent == {"status": "sent", "attempts": 1}
    timeline = get(client, path).text
    assert "requeued by operator:</span> yes" in timeline  # who put it back in the queue is on the record


def test_retry_falls_back_to_the_lead_page_and_wakes_the_worker(client, ctx, mailer, clock, monkeypatch):
    # Rule: without a "return to" address the operator lands on the message's lead, and the
    # worker is woken so the email leaves now, not at the next poll.
    message = failed_intro(client, ctx, mailer, clock)
    woken = []
    monkeypatch.setattr(ctx.worker, "wake", lambda: woken.append(True))
    response = post(client, f"/dashboard/messages/{message['id']}/retry")
    assert response.headers["location"] == f"/dashboard/leads/{message['lead_id']}?msg=message_requeued"
    assert woken == [True]


def test_only_failed_emails_offer_retry_and_a_second_click_sends_nothing_twice(client, ctx, mailer, clock, scene):
    # Rule: Retry exists for failed messages only. Posting it for a sent, pending or cancelled
    # message (a stale page, a double click) changes nothing and says so.
    html = get(client, f"/dashboard/leads/{scene['booked_id']}").text
    assert "/retry" not in html  # sent and cancelled messages only
    before = state(ctx)
    sent_before = len(mailer.sent)
    for message in db_rows(ctx, "SELECT id, lead_id FROM outbox_messages"):
        response = post(client, f"/dashboard/messages/{message['id']}/retry")
        assert response.status_code == 303
        assert response.headers["location"] == f"/dashboard/leads/{message['lead_id']}?msg=message_not_failed"
    assert state(ctx) == before
    deliver(ctx, clock)
    assert len(mailer.sent) == sent_before
    assert "not in the failed state" in get(client, f"/dashboard/leads/{scene['booked_id']}?msg=message_not_failed").text


@pytest.mark.parametrize("message_id", ["msg_missing", "1", "%27", "retry"])
def test_retrying_an_unknown_message_is_a_404(client, scene, message_id):
    # Rule: an unknown message id is a 404 page.
    response = post(client, f"/dashboard/messages/{message_id}/retry")
    assert response.status_code == 404
    assert "Message not found" in response.text


def test_discarding_a_quarantined_event_takes_it_out_of_the_backlog(client, ctx, scene):
    # Rule: Discard says "this stored request will never be a lead". It leaves the quarantine
    # (the health check clears), keeps the raw request, and shows as a neutral "discarded" row.
    event_id = scene["quarantined_event_id"]
    response = post(client, f"/dashboard/events/{event_id}/discard", {"next": "/dashboard/monitoring?status=quarantined"})
    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard/monitoring?status=quarantined&msg=event_discarded"
    event = db_one(ctx, "SELECT status, raw_body, lead_id FROM webhook_events WHERE id = ?", (event_id,))
    assert event["status"] == "discarded" and event["lead_id"] is None
    assert "Quinn Quarantine" in event["raw_body"]  # still on record
    html = get(client, response.headers["location"]).text
    assert "Event discarded. The stored request stays on record." in html
    assert "No webhook events with this status yet." in html and "Replay all quarantined (0)" in html

    monitoring = get(client, "/dashboard/monitoring").text
    checks = monitoring[monitoring.index("<h2>Health checks</h2>"):monitoring.index("<h2>Open alerts</h2>")]
    assert "nothing in quarantine" in checks
    discarded = get(client, "/dashboard/monitoring?status=discarded").text
    events = discarded[discarded.index("Recent webhook events"):]
    assert '<option value="discarded" selected>discarded</option>' in events
    assert events.count("<summary>Preview</summary>") == 1 and "Quinn Quarantine" in events
    assert '<span class="badge neutral">discarded</span>' in events
    # Dealt with: no more buttons on it, and no instructions about a mapping.
    assert "/replay\"" not in events and "/discard" not in events and "Add the mapping" not in events


def test_discarded_event_is_not_replayed(client, ctx, scene):
    # Rule: a discarded event stays discarded: neither its own Replay (from a stale page) nor
    # "Replay all quarantined" nor a new mapping for its account turns it into a lead.
    event_id = scene["quarantined_event_id"]
    post(client, f"/dashboard/events/{event_id}/discard")
    replay = post(client, f"/dashboard/events/{event_id}/replay")
    assert replay.headers["location"] == "/dashboard/monitoring?msg=replay_discarded"
    assert "was discarded, so it is not replayed" in get(client, replay.headers["location"]).text
    everything = post(client, "/dashboard/events/replay-quarantined")
    assert everything.headers["location"] == "/dashboard/monitoring?msg=replayed_all&n=0&ok=0"
    mapped = post(client, "/dashboard/tenants/tnt_blueline/lead-sources", {"external_account_id": UNMAPPED_ACCOUNT})
    assert mapped.headers["location"] == "/dashboard/tenants?msg=mapping_added&n=0&ok=0"
    assert db_one(ctx, "SELECT status, attempts FROM webhook_events WHERE id = ?", (event_id,)) == {
        "status": "discarded", "attempts": 1,
    }
    assert db_one(ctx, "SELECT COUNT(*) AS n FROM leads WHERE email = 'quinn@example.com'")["n"] == 0


def test_rejected_event_can_be_discarded(client, ctx):
    # Rule: a body that is not JSON (a ping, a scanner) is the typical thing to discard.
    event_id = post_lead(client, b"ping").json()["eventId"]
    response = post(client, f"/dashboard/events/{event_id}/discard")
    assert response.headers["location"] == "/dashboard/monitoring?msg=event_discarded"
    assert db_one(ctx, "SELECT status FROM webhook_events WHERE id = ?", (event_id,))["status"] == "discarded"


def test_processed_event_cannot_be_discarded(client, ctx, scene):
    # Rule: an event that became a lead is never thrown away; a second Discard is harmless too.
    event = db_one(ctx, "SELECT id FROM webhook_events WHERE lead_id = ?", (scene["booked_id"],))
    before = state(ctx)
    response = post(client, f"/dashboard/events/{event['id']}/discard")
    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard/monitoring?msg=discard_not_possible"
    assert state(ctx) == before
    assert 'class="notice warning"' in get(client, response.headers["location"]).text
    post(client, f"/dashboard/events/{scene['quarantined_event_id']}/discard")
    again = post(client, f"/dashboard/events/{scene['quarantined_event_id']}/discard")
    assert again.headers["location"] == "/dashboard/monitoring?msg=discard_not_possible"


@pytest.mark.parametrize("action", ["discard", "assign"])
@pytest.mark.parametrize("event_id", ["evt_missing", "1", "%27"])
def test_discarding_or_assigning_an_unknown_event_is_a_404(client, ctx, scene, action, event_id):
    # Rule: an unknown webhook event id is a 404 page, whatever the form says.
    before = state(ctx)
    response = post(client, f"/dashboard/events/{event_id}/{action}", {"tenant_id": "tnt_sparkle"})
    assert response.status_code == 404
    assert "Webhook event not found" in response.text
    assert state(ctx) == before


def lead_without_account_id(client, ctx) -> str:
    """A lead whose payload names no Angi account, waiting in quarantine. -> its event id.

    Intake records such an event with the reason "no_account_id" (asserted by the first
    test below, through the webhook). The row is pinned to that reason here so the tests
    of the dashboard's own form do not depend on anything but the dashboard."""
    payload = ads_payload(FirstName="Nora", LastName="Noaccount", Email="nora@example.com", PhoneNumber="3175550133")
    del payload["ALAccountId"]
    result = post_lead(client, payload).json()
    assert result["result"] == "quarantined"
    with ctx.db.transaction() as conn:
        conn.execute("UPDATE webhook_events SET status_reason = 'no_account_id' WHERE id = ?", (result["eventId"],))
    return result["eventId"]


def test_lead_without_an_account_id_is_quarantined_and_can_be_assigned_on_the_monitoring_page(client, ctx, mailer, clock):
    # Rule, end to end through the webhook: a payload with no Angi account id cannot be routed
    # by any mapping. It waits in quarantine with the reason "no_account_id"; the Monitoring
    # page offers a tenant picker on its row; assigning a tenant turns it into that tenant's lead.
    payload = ads_payload(FirstName="Nora", LastName="Noaccount", Email="nora@example.com", PhoneNumber="3175550133")
    del payload["ALAccountId"]
    result = post_lead(client, payload).json()
    assert (result["result"], result["reason"]) == ("quarantined", "no_account_id")

    html = get(client, "/dashboard/monitoring").text
    assert f'action="/dashboard/events/{result["eventId"]}/assign"' in html
    assert "The payload names no Angi account, so no mapping can route it. Assign a tenant to it." in html
    assert "Leads are waiting for a mapping." not in get(client, "/dashboard/tenants").text  # a mapping cannot help

    response = post(client, f"/dashboard/events/{result['eventId']}/assign", {"tenant_id": "tnt_summit"})
    assert response.headers["location"] == "/dashboard/monitoring?msg=replay_processed"
    lead = db_one(ctx, "SELECT tenant_id, routed_by FROM leads WHERE email = 'nora@example.com'")
    assert lead == {"tenant_id": "tnt_summit", "routed_by": "operator"}
    deliver(ctx, clock)
    assert [(email.to, email.from_name) for email in mailer.sent] == [("nora@example.com", "Summit Heating & Air")]


def test_assign_form_is_offered_only_for_an_event_without_an_account_id(client, ctx, scene):
    # Rule: the tenant picker appears on exactly the rows that need it, lists the active
    # tenants, and sits next to Replay and Discard.
    event_id = lead_without_account_id(client, ctx)
    with ctx.db.transaction() as conn:
        conn.execute("UPDATE tenants SET is_active = 0 WHERE id = 'tnt_blueline'")
    html = get(client, "/dashboard/monitoring?status=quarantined").text
    assert re.findall(r'action="/dashboard/events/(evt_[0-9a-f]+)/assign"', html) == [event_id]
    row = html[html.index(f"/dashboard/events/{event_id}/replay"):]
    row = row[:row.index("</tbody>")]
    assert "Assign and replay</button>" in row and f"/dashboard/events/{event_id}/discard" in row
    assert '<select name="tenant_id" required>' in row
    options = re.findall(r'<option value="(tnt_\w+)">', row)
    assert options == ["tnt_sparkle", "tnt_summit", "tnt_canary"]  # not the deactivated tenant
    assert row.count('name="next" value="/dashboard/monitoring?status=quarantined"') == 3
    assert "The payload names no Angi account" in html and "<td>no_account_id</td>" in html
    assert outline(html).problems == []


def test_assigning_a_tenant_processes_the_event_for_that_tenant(client, ctx, mailer, clock, scene):
    # Rule: "Assign and replay" records who the operator said the lead is for and runs the
    # stored request through the pipeline for that tenant: a lead, an intro email under that
    # business's name, and the speed-to-lead clock still starting when Angi sent it.
    event_id = lead_without_account_id(client, ctx)
    received_at = db_one(ctx, "SELECT received_at FROM webhook_events WHERE id = ?", (event_id,))["received_at"]
    clock.advance(minutes=20)
    response = post(client, f"/dashboard/events/{event_id}/assign",
                    {"tenant_id": "tnt_blueline", "next": "/dashboard/monitoring?status=quarantined"})
    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard/monitoring?status=quarantined&msg=replay_processed"

    event = db_one(ctx, "SELECT status, operator_tenant_id, tenant_id, lead_id FROM webhook_events WHERE id = ?", (event_id,))
    assert (event["status"], event["operator_tenant_id"], event["tenant_id"]) == ("processed", "tnt_blueline", "tnt_blueline")
    lead = db_one(ctx, "SELECT * FROM leads WHERE id = ?", (event["lead_id"],))
    assert (lead["tenant_id"], lead["full_name"], lead["routed_by"]) == ("tnt_blueline", "Nora Noaccount", "operator")
    assert lead["received_at"] == received_at
    sent_before = len(mailer.sent)
    deliver(ctx, clock)
    assert [(email.to, email.from_name) for email in mailer.sent[sent_before:]] == [("nora@example.com", "BlueLine Plumbing")]
    html = get(client, response.headers["location"]).text
    assert "Event replayed: it is now a lead." in html
    assert f"/dashboard/events/{event_id}/assign" not in get(client, "/dashboard/monitoring").text
    assert "operator" in get(client, f"/dashboard/leads/{lead['id']}").text  # "Routed by" on the lead page


@pytest.mark.parametrize("tenant_id", ["", "tnt_missing", "sparkle-home-cleaning", "tnt_blueline"])
def test_assigning_needs_an_existing_active_tenant(client, ctx, scene, tenant_id):
    # Rule: no tenant chosen, an unknown one, or a deactivated one: nothing is processed and
    # nothing is recorded on the event, so the operator can simply choose again.
    event_id = lead_without_account_id(client, ctx)
    with ctx.db.transaction() as conn:
        conn.execute("UPDATE tenants SET is_active = 0 WHERE id = 'tnt_blueline'")
    before = state(ctx)
    response = post(client, f"/dashboard/events/{event_id}/assign", {"tenant_id": tenant_id})
    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard/monitoring?msg=assign_invalid"
    assert state(ctx) == before
    html = get(client, response.headers["location"]).text
    assert "Choose the (active) tenant this lead is for." in html and 'class="notice critical"' in html
    assert f'action="/dashboard/events/{event_id}/assign"' in html  # still there to try again


def test_assigning_is_refused_for_events_a_mapping_or_nothing_can_fix(client, ctx, scene):
    # Rule: routing by hand is for the one case no mapping can solve. An unmapped account is
    # fixed by adding its mapping (which also routes its future leads); a processed event
    # needs nothing. Posting the form for either changes nothing.
    processed = db_one(ctx, "SELECT id FROM webhook_events WHERE lead_id = ?", (scene["booked_id"],))["id"]
    before = state(ctx)
    for event_id in (scene["quarantined_event_id"], processed):
        response = post(client, f"/dashboard/events/{event_id}/assign", {"tenant_id": "tnt_blueline"})
        assert response.status_code == 303
        assert response.headers["location"] == "/dashboard/monitoring?msg=assign_not_needed"
    assert state(ctx) == before
    assert "for want of an Angi account id" in get(client, "/dashboard/monitoring?msg=assign_not_needed").text


def test_lead_sent_to_one_tenants_url_for_another_tenants_account_is_quarantined_and_explained(client, ctx, mailer, clock):
    # Rule: the URL does not win. Account 123456 is mapped to Sparkle; a lead for it posted to
    # BlueLine's private URL is quarantined (reason "mapping_conflict"), nobody is emailed, and
    # both the row and the message after a replay say what to correct.
    webhook_key = db_one(ctx, "SELECT webhook_key FROM tenants WHERE id = 'tnt_blueline'")["webhook_key"]
    result = post_lead(client, ads_payload(Email="conflict@example.com"), headers={},
                       path=f"/webhooks/angi/leads/{webhook_key}").json()
    assert (result["result"], result["reason"]) == ("quarantined", "mapping_conflict")
    deliver(ctx, clock)
    assert mailer.sent == [] and db_one(ctx, "SELECT COUNT(*) AS n FROM leads")["n"] == 0

    html = get(client, "/dashboard/monitoring").text
    assert "It arrived addressed to one tenant (by webhook URL or crmKey) but its Angi account is mapped to another." in html
    assert f'action="/dashboard/events/{result["eventId"]}/assign"' not in html  # correcting the mapping is the fix
    replay = post(client, f"/dashboard/events/{result['eventId']}/replay")
    assert replay.headers["location"] == "/dashboard/monitoring?msg=replay_quarantined_mapping_conflict"
    assert "still quarantined: it arrived addressed to one tenant" in get(client, replay.headers["location"]).text


@pytest.mark.parametrize(
    ("reason", "words"),
    [
        ("unmapped_account", "its Angi account is not mapped to a tenant"),
        ("no_account_id", "the payload names no Angi account"),
        ("tenant_inactive", "the tenant it belongs to is deactivated"),
        ("mapping_conflict", "its Angi account is mapped to another"),
        ("tenant_conflict", "a lead that belongs to a different tenant"),
        ("unrecognised_payload", "matches no known Angi format"),
    ],
)
def test_every_quarantine_reason_is_explained_in_words(client, ctx, scene, reason, words):
    # Rule: each reason intake can give for a quarantine has a plain-language label, shown on
    # the event's row and in the message after a replay that did not release it.
    assert set(dashboard.QUARANTINE_REASONS) == {
        "unmapped_account", "no_account_id", "tenant_inactive", "mapping_conflict", "tenant_conflict", "unrecognised_payload",
    }
    flash = get(client, f"/dashboard/monitoring?msg=replay_quarantined_{reason}").text
    assert "Event replayed, but it is still quarantined: " in flash and words in flash
    assert 'class="notice warning"' in flash
    with ctx.db.transaction() as conn:
        conn.execute("UPDATE webhook_events SET status_reason = ? WHERE id = ?", (reason, scene["quarantined_event_id"]))
    row = get(client, "/dashboard/monitoring?status=quarantined").text
    sentence = dashboard.quarantine_reason(reason)
    assert words.lower() in sentence.lower() and sentence in row
    assert f"<td>{reason}</td>" in row


def test_reason_that_is_not_a_quarantine_code_is_shown_as_it_is(client, ctx):
    # Rule: other reasons (an error message, "lead already received") are shown verbatim, escaped;
    # a replay outcome nobody wrote a sentence for still gets a truthful generic message.
    assert dashboard.quarantine_reason("lead already received") is None
    assert dashboard.quarantine_reason(None) is None
    assert dashboard.quarantine_reason("no_account_id").endswith("Assign a tenant to it.")
    assert "still quarantined: the cause has not been fixed yet" in get(client, "/dashboard/monitoring?msg=replay_quarantined").text
    post_lead(client, b"<b>not json</b>")
    html = get(client, "/dashboard/monitoring").text
    assert "request body is not valid JSON" in html and "<b>not json" not in html


def test_every_dashboard_post_is_checked_for_its_origin_by_the_shared_dependency(client):
    # Rule: one cross-site check for the whole operator surface (app.security), applied to the
    # dashboard router itself, so a form added later cannot forget it.
    assert dashboard.require_same_origin is security.require_same_origin
    posts = [route for route in dashboard.router.routes if "POST" in route.methods]
    assert len(posts) == 10
    for route in posts:
        path = re.sub(r"\{\w+\}", "x", route.path)
        assert post(client, path, headers=EVIL).status_code == 403, route.path
        assert client.post(path, headers=EVIL).status_code == 401, route.path  # the token is asked for first


def test_overview_points_out_leads_that_carry_no_price(client, ctx, scene):
    # Rule: a lead with no fee in the payload and none on its Angi account counts as $0, which
    # flatters the spend and cost figures; the overview says how many such leads there are.
    priced = get(client, "/dashboard").text
    assert "have no fee" not in priced  # every lead in the scene has a price
    webhook_key = db_one(ctx, "SELECT webhook_key FROM tenants WHERE id = 'tnt_blueline'")["webhook_key"]
    for index in range(2):  # a new Angi account, learned from the tenant's own URL: no default fee yet
        unpriced = post_lead(client, ads_payload(ALAccountId="31337", Email=f"free{index}@example.com",
                                                 PhoneNumber=f"512555014{index}"),
                             headers={}, path=f"/webhooks/angi/leads/{webhook_key}").json()
        assert db_one(ctx, "SELECT fee_cents FROM leads WHERE id = ?", (unpriced["leadId"],))["fee_cents"] is None
    html = get(client, "/dashboard").text
    assert tile(html, "Leads received") == "6" and tile(html, "Spend on leads") == "$109.67"
    assert "2 lead(s) in this period have no fee" in " ".join(html.split())
    assert 'href="/dashboard/tenants">Tenants</a>' in html  # where to set the account's default fee
    assert "have no fee" not in get(client, "/dashboard?tenant=tnt_sparkle").text  # scoped like every other number


# --- 6. tenants ----------------------------------------------------------------------------


def test_tenants_page_shows_each_tenant_with_its_mappings_and_routing_details(client, ctx, scene):
    # Rule: for each tenant: its Angi accounts (id, name, default fee), its private webhook URL,
    # the crmKey to give Angi (the tenant id), and how many leads it has.
    html = get(client, "/dashboard/tenants").text
    tenants = db_rows(ctx, "SELECT * FROM tenants")
    assert len(tenants) == 4
    for tenant in tenants:
        section = tenant_section(html, tenant["id"])
        assert f'value="{BASE_URL}/webhooks/angi/leads/{tenant["webhook_key"]}"' in section
        assert f'<span class="mono" id="crm-{tenant["id"]}">{tenant["id"]}</span>' in section
        assert tenant["timezone"] in section
        assert f'action="/dashboard/tenants/{tenant["id"]}/lead-sources"' in section
    sparkle = tenant_section(html, "tnt_sparkle")
    assert re.search(r'<td class="mono">123456</td>\s*<td>Sparkle Home Cleaning</td>\s*<td class="num">\$28.00</td>', sparkle)
    assert '<a class="small" href="/dashboard/leads?tenant=tnt_sparkle">3 lead(s)</a>' in sparkle
    assert "added by hand" in sparkle
    summit = tenant_section(html, "tnt_summit")
    assert "12345678" in summit and "200001" in summit and "$45.00" in summit and "1 lead(s)" in summit
    assert '<span class="flag">test</span>' in tenant_section(html, "tnt_canary")
    assert '<span class="flag">test</span>' not in sparkle


def test_tenant_webhook_url_shown_on_the_page_really_routes_to_that_tenant(client, ctx):
    # Rule: the tenant-specific URL on the Tenants page accepts a lead for that tenant with no API key,
    # and the Angi account it came from is learned and shown as such.
    html = get(client, "/dashboard/tenants").text
    blueline = tenant_section(html, "tnt_blueline")
    url = re.search(r'value="' + re.escape(BASE_URL) + r'(/webhooks/angi/leads/[^"]+)"', blueline).group(1)
    result = post_lead(client, ads_payload(ALAccountId="31337", Email="urlkey@example.com"), headers={}, path=url).json()
    assert result["result"] == "processed"
    assert result["tenant"] == {"id": "tnt_blueline", "name": "BlueLine Plumbing", "routedBy": "url_key"}
    blueline = tenant_section(get(client, "/dashboard/tenants").text, "tnt_blueline")
    row = blueline[blueline.index("31337"):]
    assert ">learned</span>" in row[:row.index("</tr>")]


def test_tenants_page_names_the_accounts_waiting_for_a_mapping(client, ctx, scene):
    # Rule: when leads are quarantined for an unmapped Angi account, the page says which account
    # id to add and how many leads wait for it. The hint is read from the quarantine itself, so
    # closing the alert about the account does not hide leads that are still waiting.
    post_lead(client, leads_payload(spEntityId=int(UNMAPPED_ACCOUNT), spCompanyName=f"{XSS} Plumbing",
                                    email="second@example.com", primaryPhone="3035550178"))
    html = get(client, "/dashboard/tenants").text
    assert "Leads are waiting for a mapping." in html
    assert f'<span class="mono">{UNMAPPED_ACCOUNT}</span> ({XSS_ESCAPED} Plumbing): 2 lead(s) waiting' in html
    alert_id = db_one(ctx, "SELECT id FROM alerts WHERE kind = 'unmapped_account'")["id"]
    post(client, f"/dashboard/alerts/{alert_id}/resolve")
    assert f'<span class="mono">{UNMAPPED_ACCOUNT}</span>' in get(client, "/dashboard/tenants").text
    # Discarding the waiting requests (or mapping the account) is what empties the list.
    for event in db_rows(ctx, "SELECT id FROM webhook_events WHERE status = 'quarantined'"):
        post(client, f"/dashboard/events/{event['id']}/discard")
    assert "Leads are waiting for a mapping." not in get(client, "/dashboard/tenants").text


def test_adding_a_mapping_processes_the_quarantined_leads_for_that_account(client, ctx, scene, clock):
    # Rule: mapping an Angi account to a tenant immediately replays the leads quarantined for it,
    # applies the account's default fee, and tells the operator how many were processed.
    clock.advance(minutes=10)
    response = post(
        client,
        "/dashboard/tenants/tnt_blueline/lead-sources",
        {"external_account_id": f" {UNMAPPED_ACCOUNT} ", "external_account_name": "Quinn's Plumbing", "default_fee": "31.50"},
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard/tenants?msg=mapping_added&n=1&ok=1"

    mapping = db_one(ctx, "SELECT * FROM tenant_lead_sources WHERE external_account_id = ?", (UNMAPPED_ACCOUNT,))
    assert (mapping["tenant_id"], mapping["external_account_name"], mapping["default_fee_cents"], mapping["learned"]) == (
        "tnt_blueline", "Quinn's Plumbing", 3150, 0,
    )
    event = db_one(ctx, "SELECT status, lead_id, received_at FROM webhook_events WHERE id = ?",
                   (scene["quarantined_event_id"],))
    assert event["status"] == "processed"
    lead = db_one(ctx, "SELECT * FROM leads WHERE id = ?", (event["lead_id"],))
    assert (lead["tenant_id"], lead["full_name"], lead["email_normalized"]) == (
        "tnt_blueline", "Quinn Quarantine", "quinn@example.com",
    )
    assert lead["received_at"] == event["received_at"]  # the clock started when Angi sent it, not at replay
    assert db_one(ctx, "SELECT COUNT(*) AS n FROM outbox_messages WHERE lead_id = ? AND template = 'intro'",
                  (lead["id"],))["n"] == 1  # the homeowner finally gets the intro email

    html = get(client, response.headers["location"]).text
    assert "Mapping saved. Replayed 1 quarantined event(s) of that account; 1 became leads." in html
    blueline = tenant_section(html, "tnt_blueline")
    assert UNMAPPED_ACCOUNT in blueline and "Quinn&#39;s Plumbing" in blueline and "$31.50" in blueline
    assert "1 lead(s)" in blueline
    assert "Leads are waiting for a mapping." not in html  # nothing is waiting any more


def test_adding_a_mapping_leaves_the_leads_of_other_accounts_untouched(client, ctx, scene):
    # Rule: mapping one account replays that account's quarantined leads only. Another account's
    # lead is not let through, and is not even re-run (its attempt count and alert stay as they were).
    before = db_one(ctx, "SELECT status, attempts FROM webhook_events WHERE id = ?", (scene["quarantined_event_id"],))
    alert = db_one(ctx, "SELECT count FROM alerts WHERE kind = 'unmapped_account'")
    response = post(client, "/dashboard/tenants/tnt_blueline/lead-sources", {"external_account_id": "424242"})
    assert response.headers["location"] == "/dashboard/tenants?msg=mapping_added&n=0&ok=0"
    after = db_one(ctx, "SELECT status, attempts FROM webhook_events WHERE id = ?", (scene["quarantined_event_id"],))
    assert after == before == {"status": "quarantined", "attempts": 1}
    assert db_one(ctx, "SELECT count FROM alerts WHERE kind = 'unmapped_account'") == alert
    assert db_one(ctx, "SELECT COUNT(*) AS n FROM leads WHERE email = 'quinn@example.com'")["n"] == 0
    assert "Replayed 0 quarantined event(s) of that account; 0 became leads." in get(client, response.headers["location"]).text


def test_adding_a_mapping_without_a_fee_leaves_the_fee_unset(client, ctx):
    # Rule: the name and the default fee are optional.
    response = post(client, "/dashboard/tenants/tnt_summit/lead-sources", {"external_account_id": "808080"})
    assert response.headers["location"] == "/dashboard/tenants?msg=mapping_added&n=0&ok=0"
    mapping = db_one(ctx, "SELECT tenant_id, external_account_name, default_fee_cents FROM tenant_lead_sources "
                          "WHERE external_account_id = '808080'")
    assert mapping == {"tenant_id": "tnt_summit", "external_account_name": None, "default_fee_cents": None}


def test_remapping_an_account_from_another_tenant_is_called_out(client, ctx):
    # Rule: an Angi account belongs to one tenant; pointing it at another is allowed but never silent.
    response = post(client, "/dashboard/tenants/tnt_blueline/lead-sources", {"external_account_id": "123456"})
    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard/tenants?msg=mapping_moved&n=0&ok=0"
    owner = db_one(ctx, "SELECT tenant_id, default_fee_cents FROM tenant_lead_sources WHERE external_account_id = '123456'")
    assert owner == {"tenant_id": "tnt_blueline", "default_fee_cents": 2800}
    html = get(client, response.headers["location"]).text
    assert "was mapped to a different tenant before" in html
    assert 'class="notice warning"' in html
    # Re-adding it to the tenant that already owns it is not a move.
    again = post(client, "/dashboard/tenants/tnt_blueline/lead-sources", {"external_account_id": "123456"})
    assert again.headers["location"] == "/dashboard/tenants?msg=mapping_added&n=0&ok=0"


@pytest.mark.parametrize(
    ("form", "message"),
    [
        ({}, "mapping_invalid"),
        ({"external_account_id": "   "}, "mapping_invalid"),
        ({"external_account_id": "909090", "default_fee": "free"}, "amount_invalid"),
        ({"external_account_id": "909090", "default_fee": "-3"}, "amount_invalid"),
        ({"external_account_id": "909090", "default_fee": "3.999"}, "amount_invalid"),
    ],
)
def test_invalid_mapping_is_refused_and_nothing_is_replayed(client, ctx, scene, form, message):
    # Rule: a mapping needs an account id and, if given, a proper fee; otherwise nothing changes.
    before = state(ctx)
    response = post(client, "/dashboard/tenants/tnt_sparkle/lead-sources", form)
    assert response.status_code == 303
    assert response.headers["location"] == f"/dashboard/tenants?msg={message}"
    assert state(ctx) == before
    assert 'class="notice critical"' in get(client, response.headers["location"]).text


@pytest.mark.parametrize("tenant_id", ["tnt_missing", "sparkle-home-cleaning", "1", "%20"])
def test_adding_a_mapping_to_an_unknown_tenant_is_a_404(client, ctx, scene, tenant_id):
    # Rule: mappings can only be added to a tenant that exists (by id); otherwise a 404 page.
    before = state(ctx)
    for form in ({"external_account_id": UNMAPPED_ACCOUNT}, {}):
        response = post(client, f"/dashboard/tenants/{tenant_id}/lead-sources", form)
        assert response.status_code == 404
        assert "Tenant not found" in response.text
    assert state(ctx) == before


def test_creating_a_tenant(client, ctx):
    # Rule: a tenant needs a slug and a name; it gets an id (its crmKey) and a private webhook URL.
    response = post(client, "/dashboard/tenants",
                    {"slug": " Acme-Roofing ", "name": " Acme Roofing ", "timezone": "America/Denver", "phone": "(303) 555-0199"})
    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard/tenants?msg=tenant_created"
    tenant = db_one(ctx, "SELECT * FROM tenants WHERE slug = 'acme-roofing'")
    assert (tenant["name"], tenant["timezone"], tenant["phone"], tenant["is_test"], tenant["is_active"]) == (
        "Acme Roofing", "America/Denver", "(303) 555-0199", 0, 1,
    )
    assert tenant["id"].startswith("tnt_") and len(tenant["webhook_key"]) >= 20
    html = get(client, response.headers["location"]).text
    assert "Tenant created." in html
    section = tenant_section(html, tenant["id"])
    assert "Acme Roofing" in section and "No Angi account is mapped to this tenant yet." in section
    assert f'{BASE_URL}/webhooks/angi/leads/{tenant["webhook_key"]}' in section


def test_creating_a_tenant_with_only_the_required_fields_uses_the_defaults(client, ctx):
    # Rule: timezone and phone are optional; the timezone defaults to Pacific time.
    assert post(client, "/dashboard/tenants", {"slug": "min-co", "name": "Minimal Co"}).status_code == 303
    tenant = db_one(ctx, "SELECT timezone, phone FROM tenants WHERE slug = 'min-co'")
    assert tenant == {"timezone": "America/Los_Angeles", "phone": None}


def test_new_tenant_can_be_mapped_and_receive_a_lead(client, ctx):
    # Rule: onboarding end to end from the dashboard: create the tenant, map its Angi account, leads flow.
    post(client, "/dashboard/tenants", {"slug": "fresh-co", "name": "Fresh Co", "timezone": "America/Chicago"})
    tenant_id = db_one(ctx, "SELECT id FROM tenants WHERE slug = 'fresh-co'")["id"]
    post(client, f"/dashboard/tenants/{tenant_id}/lead-sources", {"external_account_id": "616161", "default_fee": "19"})
    result = post_lead(client, ads_payload(ALAccountId="616161", Email="fresh@example.com")).json()
    assert result["tenant"]["id"] == tenant_id
    assert db_one(ctx, "SELECT fee_cents FROM leads WHERE id = ?", (result["leadId"],))["fee_cents"] == 1900
    assert tile(get(client, f"/dashboard?tenant={tenant_id}").text, "Leads received") == "1"


@pytest.mark.parametrize(
    ("form", "problem"),
    [
        ({"slug": "", "name": "No Slug"}, "Slug: 2 to 60 characters"),
        ({"slug": "a", "name": "Too Short"}, "Slug: 2 to 60 characters"),
        ({"slug": "-leading", "name": "Bad Start"}, "Slug: 2 to 60 characters"),
        ({"slug": "has space", "name": "Space"}, "Slug: 2 to 60 characters"),
        ({"slug": "under_score", "name": "Underscore"}, "Slug: 2 to 60 characters"),
        ({"slug": "x" * 61, "name": "Too Long"}, "Slug: 2 to 60 characters"),
        ({"slug": "ünïcode", "name": "Unicode"}, "Slug: 2 to 60 characters"),
        ({"slug": "ok-slug", "name": ""}, "Name: required"),
        ({"slug": "ok-slug", "name": "   "}, "Name: required"),
        ({"slug": "ok-slug", "name": "n" * 121}, "Name: required"),
        ({"slug": "ok-slug", "name": "Ok", "timezone": "Mars/Olympus_Mons"}, "Timezone: use an IANA name"),
        ({"slug": "ok-slug", "name": "Ok", "timezone": "America"}, "Timezone: use an IANA name"),
        ({"slug": "ok-slug", "name": "Ok", "timezone": "../../etc/passwd"}, "Timezone: use an IANA name"),
        ({"slug": "ok-slug", "name": "Ok", "timezone": "z" * 300}, "Timezone: use an IANA name"),
        ({"slug": "ok-slug", "name": "Ok", "phone": "5" * 41}, "Phone: at most 40 characters"),
        ({"slug": "sparkle-home-cleaning", "name": "Copycat"}, "a tenant with this slug already exists"),
    ],
)
def test_invalid_tenant_is_not_created_and_the_problem_is_shown(client, ctx, form, problem):
    # Rule: a tenant that fails validation is not created; the form comes back with the problem
    # and what was typed, so nothing has to be entered twice.
    response = post(client, "/dashboard/tenants", form)
    assert response.status_code == 422
    assert db_one(ctx, "SELECT COUNT(*) AS n FROM tenants")["n"] == 4
    html = response.text
    assert "The tenant was not created." in html
    assert problem in html
    assert outline(html).problems == []
    typed_name = form["name"].strip()
    if typed_name:
        assert f'name="name" value="{typed_name}"' in html


def test_invalid_tenant_form_reports_every_problem_at_once(client):
    # Rule: all problems are listed together, not one per attempt.
    html = post(client, "/dashboard/tenants", {"slug": "Bad Slug!", "name": "", "timezone": "Nowhere/None"}).text
    assert "Slug: 2 to 60 characters" in html and "Name: required" in html and "Timezone: use an IANA name" in html
    assert 'name="slug" value="bad slug!"' in html and 'name="timezone" value="Nowhere/None"' in html


# --- formatting ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(None, "—"), (0, "0.0 s"), (0.44, "0.4 s"), (9.94, "9.9 s"), (9.96, "10.0 s"), (10, "10 s"), (59.4, "59 s"),
     (59.6, "1 min 00 s"), (60, "1 min 00 s"), (125, "2 min 05 s"), (3599.6, "1 h 00 min"), (3600, "1 h 00 min"),
     (7800, "2 h 10 min"), (86399.7, "1 d 0 h"), (86400, "1 d 0 h"), (273600, "3 d 4 h"), (-5, "—"),
     ("12", "—"), (True, "—")],
)
def test_duration_is_formatted_in_the_two_most_useful_units(seconds, text):
    # Rule: speed-to-lead is shown the way a person would say it, never as a raw number of seconds.
    assert dashboard.format_duration(seconds) == text


@pytest.mark.parametrize(
    ("ratio", "text"),
    [(None, "—"), (0, "0%"), (0.25, "25%"), (0.3333, "33.3%"), (0.005, "0.5%"), (1, "100%"), (1.5, "150%"), ("x", "—")],
)
def test_ratio_is_formatted_as_a_percentage(ratio, text):
    # Rule: rates are percentages with at most one decimal; "no data" is a dash, not 0%.
    assert dashboard.format_pct(ratio) == text


@pytest.mark.parametrize(
    ("value", "minutes", "seconds"),
    [("2026-11-12T15:00:05.123Z", "2026-11-12 15:00", "2026-11-12 15:00:05"), (None, "—", "—"), ("", "—", "—"), (5, "—", "—")],
)
def test_timestamps_are_formatted_without_the_iso_noise(value, minutes, seconds):
    # Rule: timestamps read as "YYYY-MM-DD HH:MM" (UTC), and a missing one is a dash.
    assert dashboard.format_dt(value) == minutes
    assert dashboard.format_dt_seconds(value) == seconds


@pytest.mark.parametrize(
    ("days", "text"),
    [(None, "—"), (29.8, "29 d left"), (7.0, "7 d left"), (1.0, "1 d left"), (0.9, "under a day left"),
     (0.0, "under a day left"),
     (-0.5, "overdue"), (-1.0, "overdue by 1 d"), (-12.4, "overdue by 12 d")],
)
def test_days_left_reads_naturally(days, text):
    # Rule: a deadline is shown as time left, or as overdue once it has passed.
    assert dashboard.format_days_left(days) == text


@pytest.mark.parametrize(
    ("hours", "text"), [(None, "—"), (0.0, "0.0 h"), (5.02, "5.0 h"), (47.9, "47.9 h"), (48.0, "2.0 d"), (240.0, "10.0 d")]
)
def test_gap_between_leads_is_in_hours_then_days(hours, text):
    # Rule: the time between a duplicate and its original is hours up to two days, then days.
    assert dashboard.format_gap(hours) == text


def test_pretty_json_never_produces_markup(client):
    # Rule: JSON shown on a page is plain text that the template escapes; it is never pre-marked as safe.
    text = dashboard.pretty_json('{"name": "<b>x</b>"}')
    assert text == '{\n  "name": "<b>x</b>"\n}'
    assert type(text) is str  # not markupsafe.Markup
    assert dashboard.pretty_json("not json <b>") == "not json <b>"
    assert dashboard.pretty_json({"b": 1, "a": [1, 2]}) == '{\n  "a": [\n    1,\n    2\n  ],\n  "b": 1\n}'
    templates = client.app.state.templates
    assert templates.env.autoescape  # autoescaping is on for every dashboard template


@pytest.mark.parametrize(
    ("typed", "cents"),
    [("", None), ("   ", None), ("0", 0), ("12", 1200), ("12.3", 1230), ("12.34", 1234), ("$1,234.50", 123450),
     ("007", 700), ("999999999.99", 99999999999)],
)
def test_dollars_typed_by_an_operator_become_cents(typed, cents):
    # Rule: money is entered in dollars and stored as whole cents, with no floating point in between.
    assert dashboard._dollars_to_cents(typed) == cents


@pytest.mark.parametrize("typed", ["-1", "1.234", "1e5", "abc", ".", ".5", "5.", "1 2", "1234567890", "0x10", "١٢", "1,2.3.4"])
def test_malformed_dollar_amounts_are_rejected(typed):
    # Rule: anything that is not a plain non-negative amount with at most two decimals is an error.
    with pytest.raises(ValueError):
        dashboard._dollars_to_cents(typed)


# --- chart geometry --------------------------------------------------------------------------


def test_daily_chart_fills_quiet_days_and_scales_to_the_busiest(client):
    # Rule: every day of the period gets a slot (so a quiet day is a visible gap) and no column
    # is taller than the plot.
    chart = dashboard.daily_chart(
        [{"day": "2026-11-03", "received": 7, "booked": 2}, {"day": "2026-11-05", "received": 1, "booked": 1}],
        date(2026, 11, 1), date(2026, 11, 7),
    )
    assert len(chart["columns"]) == 7
    assert [tick["value"] for tick in chart["ticks"]] == [0, 2, 4, 6, 8]
    drawn = [column for column in chart["columns"] if column["received"]]
    assert len(drawn) == 2
    assert chart["columns"][0]["received"] is None and chart["columns"][0]["booked"] is None
    top = min(tick["y"] for tick in chart["ticks"])
    for column in drawn:
        for bar in (column["received"], column["booked"]):
            assert top <= bar["y"] < chart["baseline"]
            assert bar["y"] + bar["height"] == pytest.approx(chart["baseline"] + chart["corner"])
            assert 0 <= bar["x"] and bar["x"] + chart["bar_width"] <= 100
    # The pair of columns for one day never overlaps: received ends where booked starts.
    busiest = chart["columns"][2]
    assert busiest["received"]["x"] + chart["bar_width"] == pytest.approx(busiest["booked"]["x"], abs=0.01)
    assert chart["bar_width"] <= 2.0  # thin columns even when there are only a few days


def test_daily_chart_is_capped_at_ninety_days_and_says_so(client):
    # Rule: a very long period is drawn as its most recent 90 days; the table still has every day.
    by_day = [{"day": "2025-01-01", "received": 50, "booked": 5}, {"day": "2026-11-10", "received": 2, "booked": 0}]
    chart = dashboard.daily_chart(by_day, None, date(2026, 11, 12))
    assert len(chart["columns"]) == 90 and chart["truncated"] is True
    assert chart["table"] == by_day
    assert [tick["value"] for tick in chart["ticks"]] == [0, 1, 2]  # scaled to what is drawn, not to the old spike
    labels = [column["label"] for column in chart["columns"] if column["label"]]
    assert 2 <= len(labels) <= 7
    assert chart["columns"][-1]["label"] is not None and chart["columns"][-1]["anchor"] == "end"


def test_daily_chart_handles_one_day_and_no_days(client):
    # Rule: one day of data is still a chart; no data is no chart (the page says "no leads yet").
    assert dashboard.daily_chart([], None, date(2026, 11, 12)) is None
    chart = dashboard.daily_chart([{"day": "2026-11-12", "received": 1, "booked": 0}], None, date(2026, 11, 12))
    assert len(chart["columns"]) == 1
    assert chart["columns"][0]["label"] == "Nov 12" and chart["columns"][0]["anchor"] == "middle"
    assert chart["columns"][0]["booked"] is None
    # A lead dated "tomorrow" in a tenant ahead of UTC is not dropped.
    ahead = dashboard.daily_chart([{"day": "2026-11-13", "received": 1, "booked": 0}], date(2026, 11, 6), date(2026, 11, 12))
    assert len(ahead["columns"]) == 8 and ahead["columns"][-1]["received"] is not None


@pytest.mark.parametrize(
    ("largest", "step"), [(1, 1), (4, 1), (5, 2), (8, 2), (9, 5), (20, 5), (21, 10), (40, 10), (41, 20), (399, 100), (5000, 2000)]
)
def test_axis_gridlines_use_round_steps(largest, step):
    # Rule: the count axis uses 1 / 2 / 5 x 10^n steps and never needs more than four of them.
    assert dashboard.axis_step(largest) == step
    assert largest <= 4 * step


def test_funnel_rows_handle_an_empty_funnel(client):
    # Rule: with no leads there is nothing to divide by; rates are "no data", widths are zero.
    rows = dashboard.funnel_rows({"received": 0, "contacted": 0, "engaged": 0, "booked": 0, "won": 0})
    assert [row["width"] for row in rows] == [0, 0, 0, 0, 0]
    assert [row["step_rate"] for row in rows] == [None] * 5


# --- behaviour the dashboard relies on from the rest of the application -----------------------


def test_unmapped_account_alert_closes_once_the_account_is_mapped(client, ctx, scene):
    # Rule: an alert describes a problem that still exists; fixing the mapping should close it.
    post(client, "/dashboard/tenants/tnt_blueline/lead-sources", {"external_account_id": UNMAPPED_ACCOUNT})
    assert db_one(ctx, "SELECT status FROM webhook_events WHERE id = ?", (scene["quarantined_event_id"],)) == {
        "status": "processed"
    }
    ctx.worker.run_maintenance()
    still_open = db_rows(ctx, "SELECT title FROM alerts WHERE kind = 'unmapped_account' AND status = 'open'")
    assert still_open == []


def test_funnel_never_shows_more_won_than_booked(client, ctx, scene):
    # Rule: the funnel is cumulative. A job recorded as won counts as booked, even when it was
    # arranged by phone and nobody used the booking link (2 such wins + the 1 online booking).
    post(client, f"/dashboard/leads/{scene['jane_id']}/outcome", {"outcome": "won", "job_value": "400"})
    post(client, f"/dashboard/leads/{scene['xss_id']}/outcome", {"outcome": "won", "job_value": "400"})
    html = get(client, "/dashboard").text
    assert funnel_count(html, "Won") == 2
    assert funnel_count(html, "Booked") == 3
    assert tile(html, "Lead to booked rate") == "75%"
    assert "A job recorded as won counts as booked" in html  # the page says how it counts


def test_lead_won_without_an_online_booking_says_how_it_is_counted(client, scene):
    # Rule: the lead's own page agrees with the overview: no booking time to show, but the win
    # counts as a booking in the numbers. A lead that did book online needs no such remark.
    post(client, f"/dashboard/leads/{scene['jane_id']}/outcome", {"outcome": "won", "job_value": "400"})
    remark = "a won job counts as booked in the numbers"
    assert remark in get(client, f"/dashboard/leads/{scene['jane_id']}").text
    post(client, f"/dashboard/leads/{scene['booked_id']}/outcome", {"outcome": "won", "job_value": "400"})
    assert remark not in get(client, f"/dashboard/leads/{scene['booked_id']}").text
    assert remark not in get(client, f"/dashboard/leads/{scene['dup_id']}").text  # not won at all


def test_timezone_that_is_a_directory_name_is_simply_invalid(client):
    # Rule: validating a timezone answers yes or no; it never raises.
    assert valid_timezone("America") is False


def test_admin_api_rejects_a_directory_name_as_timezone(client):
    # Rule: an invalid timezone is a validation error for the caller, not a server error.
    response = client.post("/admin/api/tenants", json={"slug": "tz-co", "name": "TZ Co", "timezone": "America"},
                           headers=ADMIN)
    assert response.status_code == 422


def test_lead_with_an_absurd_fee_is_still_stored(client, ctx):
    # Rule: a bad value in one optional field must never cost us the lead.
    result = post_lead(client, leads_payload(fee=1e30)).json()
    assert result["result"] == "processed"
    assert db_one(ctx, "SELECT COUNT(*) AS n FROM leads")["n"] == 1


def test_replaying_an_old_delivery_does_not_undo_a_newer_correction(client, ctx, clock):
    # Rule: replay is idempotent; it must never move a lead back to an older version of itself.
    # (Deliveries are ordered by when they arrived, so each one here arrives a second after the last.)
    payload = ads_payload(Email="versions@example.com")
    lead_id = post_lead(client, payload).json()["leadId"]
    clock.advance(seconds=1)
    second = post_lead(client, {**payload, "Description": "second version"}).json()
    clock.advance(seconds=1)
    post_lead(client, {**payload, "Description": "third version"})
    assert db_one(ctx, "SELECT comments FROM leads WHERE id = ?", (lead_id,))["comments"] == "third version"
    client.post(f"/admin/api/events/{second['eventId']}/replay", headers=ADMIN)
    assert db_one(ctx, "SELECT comments FROM leads WHERE id = ?", (lead_id,))["comments"] == "third version"


def test_crediting_more_than_the_fee_is_refused_with_a_message_not_a_server_error(client, ctx, clock):
    # A credit can never exceed what the lead cost; the form says so instead of crashing.
    first = ads_payload(Email="dup.fee@example.com", PhoneNumber="3175550177")
    post_lead(client, first)
    clock.advance(hours=2)
    post_lead(client, ads_payload(Email="dup.fee@example.com", PhoneNumber="3175550177"))
    claim = db_one(ctx, "SELECT id, amount_cents FROM credit_claims")
    assert claim["amount_cents"] == 2800

    response = client.post(
        f"/dashboard/credits/{claim['id']}/status",
        data={"status": "credited", "credited": "99.00"},
        headers=ADMIN,
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "msg=credit_too_large" in response.headers["location"]
    assert db_one(ctx, "SELECT status FROM credit_claims")["status"] == "detected"
    page = client.get(response.headers["location"], headers=ADMIN)
    assert "cannot be larger than the fee" in page.text
