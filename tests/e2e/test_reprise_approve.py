"""E2E: the Review season queue, Fix editor, Plex send gates and Undo."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from playwright.sync_api import Page, Route, expect

from ._approvals_api import ApprovalsApi, automatic_entry, span


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


@pytest.fixture
def errors(authed_page: Page) -> list[str]:
    """Anything the page throws; the tests that matter end by asserting this is empty."""
    seen: list[str] = []
    authed_page.on("pageerror", lambda error: seen.append(str(error)))
    return seen


def open_page(page: Page, app_url: str, **options) -> ApprovalsApi:
    api = ApprovalsApi(page, **options)
    page.goto(f"{app_url}/reprise/approve")
    return api


def community(page: Page):
    return page.locator(".ap-show", has_text="Community")


def open_season(page: Page) -> None:
    community(page).get_by_role("link", name="Review season: Community, Season 2").click()
    expect(page.locator(".ap-season-work")).to_be_visible()


def choose(page: Page, name: str):
    """One of the three buttons of the one-by-one view (each carries its key as a small label)."""
    return page.locator(".ap-choose").get_by_role("button", name=re.compile(rf"^{name}\b"))


def start_review(page: Page) -> None:
    open_season(page)
    page.get_by_role("link", name="Review 2 skips", exact=True).click()
    expect(page.locator(".ap-card")).to_be_visible()


# ── the list ────────────────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.e2e
def test_the_queue_shows_each_season_and_what_you_can_do(authed_page: Page, app_url: str, errors: list[str]) -> None:
    page = authed_page
    open_page(page, app_url)

    expect(page.get_by_role("heading", name="Review", level=1)).to_be_visible()
    expect(page.locator("#apSub")).to_have_text("5 high-confidence skips · 2 skips to check · 1 episode ready to send")
    shows = page.locator(".ap-show")
    expect(shows).to_have_count(2)
    expect(shows.nth(0)).to_contain_text("Community")  # the show that needs you most comes first
    expect(shows.nth(1)).to_contain_text("Parks and Recreation")

    season = community(page).locator(".ap-season")
    expect(season).to_contain_text("Season 2")
    expect(season).to_contain_text("3 high-confidence skips · 2 skips to check · 1 episode ready to send")
    expect(season.get_by_role("button")).to_have_count(0)
    expect(season.get_by_role("link")).to_have_count(1)
    open_season(page)
    workspace = page.locator(".ap-season-work")
    expect(workspace.locator(".lu-primary")).to_have_count(1)
    expect(workspace.get_by_role("button", name="Approve & send 3 high-confidence skips")).to_be_enabled()
    expect(workspace.get_by_role("button", name="Send skips for 1 episode to Plex")).to_be_enabled()
    workspace.get_by_text("What changes in Plex", exact=True).first.click()
    expect(workspace).to_contain_text("Intro skip is added on 2 episodes where Plex has none.")
    expect(page.locator('[data-tour="nav-approve"]')).to_have_attribute("aria-current", "page")
    assert errors == []


@pytest.mark.e2e
def test_nothing_waiting_says_so_and_a_failed_load_can_be_retried(authed_page: Page, app_url: str) -> None:
    page = authed_page
    api = ApprovalsApi(page, spans=[])
    page.goto(f"{app_url}/reprise/approve")
    expect(page.get_by_role("heading", name="Nothing to review")).to_be_visible()
    expect(page.get_by_role("link", name="Open Library")).to_have_attribute("href", re.compile(r"/reprise$"))

    api.fail[("GET", "/approvals")] = (500, {"error": "Operation failed"})
    page.reload()
    expect(page.get_by_role("button", name="Try again")).to_be_visible()
    api.spans.extend([span("v301", "intro", "sure", 0, 25_000)])
    page.get_by_role("button", name="Try again").click()
    expect(page.locator(".ap-show")).to_contain_text("Parks and Recreation")


# ── one click ───────────────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.e2e
def test_one_click_sends_the_sure_ones_and_undo_puts_them_back(
    authed_page: Page, app_url: str, errors: list[str]
) -> None:
    page = authed_page
    api = open_page(page, app_url)

    open_season(page)
    page.get_by_role("button", name="Approve & send 3 high-confidence skips").click()
    sent = page.locator("#apSent")
    expect(sent).to_contain_text("Sent skips for 2 episodes to Plex")
    expect(sent).to_contain_text("Plex was backed up first")
    assert api.posts("/approvals/send") == [{"show": "Community", "season": 2, "scope": "sure", "to_plex": True}]
    # This season no longer has high-confidence suggestions waiting.
    expect(page.get_by_role("button", name=re.compile(r"^Approve & send"))).to_have_count(0)

    sent.get_by_role("button", name="Undo").click()
    expect(sent).to_contain_text("Undone")
    assert api.posts("/approvals/undo") == [
        {
            "changes": [
                {
                    "media_version_id": "v201",
                    "change_id": "c1",
                    "span_ids": ["owner-v201-intro", "owner-v201-credits"],
                    "label": "S02E01",
                },
                {"media_version_id": "v202", "change_id": "c2", "span_ids": ["owner-v202-intro"], "label": "S02E02"},
            ]
        }
    ]
    expect(page.get_by_role("button", name="Approve & send 3 high-confidence skips")).to_be_visible()
    expect(sent.get_by_role("button", name="Undo")).to_have_count(0)  # an undone change cannot be undone twice
    assert errors == []


def three_automatic() -> list[dict]:
    """What Reprise sent by itself, newest first: two Community episodes and one Parks and Recreation episode."""
    return [
        automatic_entry("v201", 1, sent_at=1_700_000_100),
        automatic_entry("v301", 3, kinds=("intro",), sent_at=1_700_000_050),
        automatic_entry("v202", 2, kinds=("intro", "credits"), sent_at=1_700_000_000),
    ]


@pytest.mark.e2e
def test_what_reprise_sent_by_itself_is_one_notice_per_season_and_can_be_undone_or_cleared(
    authed_page: Page, app_url: str, errors: list[str]
) -> None:
    page = authed_page
    api = open_page(page, app_url, automatic=three_automatic())
    sent = page.locator("#apSent")
    rows = sent.locator(".ap-sent-row")

    expect(rows).to_have_count(2)  # three episodes, two seasons: two notices
    expect(rows.first).to_contain_text("Sent 1 intro and 2 endings to Plex automatically")
    expect(rows.first).to_contain_text("Community · Season 2 · 2 episodes")
    expect(rows.nth(1)).to_contain_text("Sent 1 intro to Plex automatically")
    expect(rows.nth(1)).to_contain_text("Parks and Recreation · Season 1")
    expect(sent).to_contain_text("2 notices · 2 new")
    assert [q for m, p, q, _ in api.calls if p == "/approvals/automatic"][0] == {}

    rows.first.get_by_role("button", name="Undo").click()  # one Undo for the whole season
    expect(rows.first).to_contain_text("Undone")
    assert api.posts("/approvals/undo") == [
        {
            "changes": [
                {
                    "media_version_id": "v201",
                    "change_id": "auto-1",
                    "span_ids": ["owner-v201-credits"],
                    "label": "S02E01",
                },
                {
                    "media_version_id": "v202",
                    "change_id": "auto-2",
                    "span_ids": ["owner-v202-intro", "owner-v202-credits"],
                    "label": "S02E02",
                },
            ]
        }
    ]
    expect(rows).to_have_count(2)  # the undone one stays, as "Undone", until cleared

    rows.nth(1).get_by_role("button", name="Clear this notice").click()
    expect(rows).to_have_count(1)
    assert api.posts("/approvals/automatic/notices") == [{"action": "dismiss", "change_ids": ["auto-3"]}]
    page.reload()  # a cleared notice stays gone, and the undone one is no longer sent
    expect(page.locator("#apQueue")).to_be_visible()
    expect(sent).to_be_hidden()
    assert errors == []


@pytest.mark.e2e
def test_mark_all_as_read_and_clear_all_work_on_a_phone(authed_page: Page, app_url: str, errors: list[str]) -> None:
    page = authed_page
    page.set_viewport_size({"width": 390, "height": 800})
    api = open_page(page, app_url, automatic=three_automatic())
    sent = page.locator("#apSent")
    rows = sent.locator(".ap-sent-row")
    expect(sent.locator(".ap-new")).to_have_count(2)

    sent.get_by_role("button", name="Mark all as read").click()
    expect(sent.locator(".ap-new")).to_have_count(0)
    expect(sent.get_by_role("button", name="Mark all as read")).to_have_count(0)
    assert api.posts("/approvals/automatic/notices") == [{"action": "read", "all": True}]
    box = sent.get_by_role("button", name="Clear all").bounding_box()
    assert box is not None and box["x"] >= 0 and box["x"] + box["width"] <= 390
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.reload()  # read stays read
    expect(rows).to_have_count(2)
    expect(sent.locator(".ap-new")).to_have_count(0)

    sent.get_by_role("button", name="Clear all").click()
    expect(sent).to_be_hidden()
    expect(page.locator("#apToasts")).to_contain_text("Cleared 2 notices. Nothing was changed in Plex.")
    assert api.posts("/approvals/automatic/notices")[-1] == {"action": "dismiss", "all": True}
    page.reload()
    expect(page.locator("#apQueue")).to_be_visible()
    expect(sent).to_be_hidden()
    assert errors == []


@pytest.mark.e2e
def test_a_send_made_by_reprise_while_the_page_is_open_shows_up_when_you_come_back(
    authed_page: Page, app_url: str
) -> None:
    page = authed_page
    api = open_page(page, app_url)
    expect(page.locator("#apSent")).to_be_hidden()

    api.automatic.append(automatic_entry("v203", 3))
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")

    expect(page.locator("#apSent")).to_contain_text("Sent 1 ending to Plex automatically")


@pytest.mark.e2e
def test_a_failed_episode_is_listed_and_stays_unsent(authed_page: Page, app_url: str) -> None:
    page = authed_page
    api = open_page(page, app_url)
    api.send_fails["v202"] = "Plex's data did not allow it: the episode moved."

    open_season(page)
    page.get_by_role("button", name="Approve & send 3 high-confidence skips").click()
    sent = page.locator("#apSent")
    expect(sent).to_contain_text("Sent skips for 1 episode to Plex")
    expect(sent).to_contain_text("S02E02: Plex's data did not allow it: the episode moved.")
    # the one that did not go is approved and waits (with the one that already waited)
    expect(page.get_by_role("button", name="Send skips for 2 episodes to Plex")).to_be_enabled()


@pytest.mark.e2e
@pytest.mark.parametrize("sending", ["off", "missing"])
def test_with_sending_disabled_it_only_approves(authed_page: Page, app_url: str, sending: str) -> None:
    page = authed_page
    page.set_viewport_size({"width": 390, "height": 844})
    api = open_page(page, app_url, sending=sending)
    notice = page.locator("#apNotice")
    sharing = notice.get_by_role("link", name="Sharing")
    if sending == "off":
        expect(notice).to_contain_text("Review only · Approvals stay in Reprise.")
        expect(sharing).to_be_visible()
    else:
        expect(notice).to_have_text("Review only · Sending to Plex is unavailable on this install.")
        expect(sharing).to_have_count(0)
    expect(notice.locator(".lu-warn")).to_have_count(0)
    open_season(page)
    expect(page.get_by_role("button", name="Send skips for 1 episode to Plex")).to_be_disabled()
    expect(page.get_by_role("button", name=re.compile(r"Approve & send"))).to_have_count(0)

    page.get_by_role("button", name="Approve 3 high-confidence skips").click()
    expect(page.locator("#apSent")).to_contain_text("Approved 3 skips")
    expect(page.locator("#apSent")).to_contain_text("Not sent · Review only")
    assert api.posts("/approvals/send") == [{"show": "Community", "season": 2, "scope": "sure", "to_plex": False}]
    expect(page.get_by_role("button", name="Send skips for 3 episodes to Plex")).to_be_disabled()


@pytest.mark.e2e
def test_sending_a_season_you_approved_earlier(authed_page: Page, app_url: str) -> None:
    page = authed_page
    api = open_page(page, app_url)

    open_season(page)
    page.get_by_role("button", name="Send skips for 1 episode to Plex").click()
    expect(page.locator("#apSent")).to_contain_text("Sent skips for 1 episode to Plex")
    assert api.posts("/approvals/send") == [{"show": "Community", "season": 2, "scope": "approved", "to_plex": True}]
    page.locator("#apSent").get_by_role("button", name="Undo").click()
    expect(page.locator("#apSent")).to_contain_text("Undone")
    assert api.posts("/approvals/undo")[0]["changes"] == [
        {"media_version_id": "v204", "change_id": "c1", "span_ids": [], "label": "S02E04"}
    ]
    expect(page.get_by_role("button", name="Send skips for 1 episode to Plex")).to_be_visible()


# ── one by one ──────────────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.e2e
def test_review_one_by_one_with_the_keyboard(authed_page: Page, app_url: str, errors: list[str]) -> None:
    page = authed_page
    api = open_page(page, app_url)
    start_review(page)

    # credits come first within an episode; the card says what it is, how sure Reprise is, and what Plex has
    card = page.locator(".ap-card")
    expect(card.get_by_role("heading", name="S02E03 · Basic Genealogy")).to_be_visible()
    expect(page.locator(".ap-top")).to_contain_text("Skip 1 of 2")
    expect(card).to_contain_text("Low confidence")
    expect(card).to_contain_text("Plex now skips 20:50 to 22:00. Keeping Plex unchanged won’t remove that skip.")
    expect(page.locator("#apTimes")).to_have_text("20:40 → 22:00 · 1 min 20 s")
    expect(page.locator(".ap-choose")).to_contain_text("Credits skip")

    # A opens the nudge controls; the arrow keys move the picked time by a second
    page.keyboard.press("a")
    expect(choose(page, "Adjust timings")).to_have_attribute("aria-pressed", "true")
    expect(page.get_by_role("button", name="Skip starts: 1 second later")).to_be_visible()
    page.keyboard.press("ArrowRight")
    expect(page.locator("#apTimes")).to_have_text("20:41 → 22:00 · 1 min 19 s")
    expect(card.locator(".lu-clip").first).to_contain_text("20:41 · was 20:40")

    page.keyboard.press("s")
    expect(page.locator(".ap-top")).to_contain_text("Skip 2 of 2")
    assert api.posts("/approvals/decide") == [
        {
            "media_version_id": "v203",
            "expected_revision": 1,
            "kind": "credits",
            "decision": "skip",
            "start_ms": 1_241_000,
            "end_ms": 1_320_000,
        }
    ]
    expect(page.locator(".ap-choose")).to_contain_text("Intro skip")
    expect(card).to_contain_text("Plex has no intro skip.")

    page.keyboard.press("k")
    expect(page.get_by_role("heading", name="Timing review complete")).to_be_visible()
    expect(page.locator(".ap-done")).to_contain_text("1 skip approved · 1 suggestion dismissed")
    assert api.posts("/approvals/decide")[1] == {
        "media_version_id": "v203",
        "expected_revision": 2,
        "kind": "intro",
        "decision": "keep",
    }

    # Approval didn't send. Go back to the same season, then choose to publish explicitly.
    assert api.posts("/approvals/send") == []
    page.get_by_role("link", name="Back to season").click()
    expect(page.locator(".ap-season-work")).to_be_visible()
    page.get_by_role("button", name="Send skips for 2 episodes to Plex").click()
    expect(page.locator("#apSent")).to_contain_text("Sent skips for 2 episodes to Plex")
    assert api.posts("/approvals/send") == [{"show": "Community", "season": 2, "scope": "approved", "to_plex": True}]
    assert errors == []


@pytest.mark.e2e
def test_review_buttons_decide_and_the_back_button_leaves(authed_page: Page, app_url: str) -> None:
    page = authed_page
    api = open_page(page, app_url)
    start_review(page)

    choose(page, "Keep Plex unchanged").click()
    expect(page.locator(".ap-top")).to_contain_text("Skip 2 of 2")
    assert [(p["kind"], p["decision"]) for p in api.posts("/approvals/decide")] == [("credits", "keep")]

    credits = next(s for s in api.spans if s["mid"] == "v203" and s["kind"] == "credits")
    assert credits["plex"] == (1_250_000, 1_320_000)
    assert api.posts("/approvals/send") == []
    page.go_back()
    expect(page.locator(".ap-season-work")).to_be_visible()
    expect(page.locator(".ap-card")).to_have_count(0)
    expect(page.get_by_role("link", name="Review 1 skip", exact=True)).to_be_visible()
    page.go_back()
    expect(page.locator("#apQueue")).to_be_visible()
    expect(community(page)).to_contain_text("1 skip to check")


@pytest.mark.e2e
def test_undo_brings_back_the_choice_you_made(authed_page: Page, app_url: str) -> None:
    page = authed_page
    api = open_page(page, app_url)
    start_review(page)

    page.keyboard.press("a")
    page.keyboard.press("ArrowRight")
    page.keyboard.press("s")
    expect(page.locator(".ap-top")).to_contain_text("Skip 2 of 2")
    page.keyboard.press("u")

    expect(page.locator(".ap-top")).to_contain_text("Skip 1 of 2")
    assert api.posts("/approvals/decide")[1] == {
        "media_version_id": "v203",
        "expected_revision": 2,
        "decision": "restore",
        "kind": "credits",
        "span_id": "owner-v203-credits",
        "range_ids": [],
    }
    # the time you moved is still there to change again
    expect(choose(page, "Adjust timings")).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#apTimes")).to_have_text("20:41 → 22:00 · 1 min 19 s")


@pytest.mark.e2e
def test_a_held_key_decides_only_once(authed_page: Page, app_url: str) -> None:
    page = authed_page
    api = open_page(page, app_url)
    start_review(page)

    page.keyboard.down("s")
    page.keyboard.down("s")  # the key repeating while held
    page.keyboard.down("s")
    page.keyboard.up("s")
    expect(page.locator(".ap-top")).to_contain_text("Skip 2 of 2")
    assert len(api.posts("/approvals/decide")) == 1


@pytest.mark.e2e
def test_snapping_to_a_cut_moves_the_picked_time(authed_page: Page, app_url: str) -> None:
    page = authed_page
    open_page(page, app_url)
    start_review(page)
    choose(page, "Keep Plex unchanged").click()  # on to the intro: 0:02 to 0:34, cuts at 0:31, 0:32 and 0:33
    expect(page.locator("#apTimes")).to_have_text("0:02 → 0:34 · 32 s")

    page.keyboard.press("a")
    page.keyboard.press("2")  # the end
    page.keyboard.press("c")
    expect(page.locator("#apTimes")).to_have_text("0:02 → 0:33 · 31 s")
    expect(page.locator(".lu-strip[data-state='ready']").first).to_be_visible()


@pytest.mark.e2e
def test_a_changed_episode_reloads_what_is_left(authed_page: Page, app_url: str) -> None:
    page = authed_page
    api = open_page(page, app_url)
    start_review(page)

    api.revisions["v203"] += 1  # someone else decided it while this page was open
    page.keyboard.press("s")
    expect(page.locator("#apToasts")).to_contain_text(
        "This changed while you were looking at it. Reloading this review."
    )
    expect(page.locator(".ap-top")).to_contain_text("Skip 1 of 2")
    expect(page.locator(".ap-card")).to_be_visible()
    # what is asked now carries the new revision
    page.keyboard.press("s")
    expect(page.locator(".ap-top")).to_contain_text("Skip 2 of 2")
    assert api.posts("/approvals/decide")[-1]["expected_revision"] == 2


@pytest.mark.e2e
@pytest.mark.parametrize(
    ("show", "season", "kind", "action"),
    [
        ("Parks%20and%20Recreation", 1, "sure", "Approve & send 1 high-confidence skip"),
        ("Community", 2, "ready", "Send skips for 1 episode to Plex"),
    ],
)
def test_direct_season_link_works_without_manual_review(
    authed_page: Page, app_url: str, show: str, season: int, kind: str, action: str
) -> None:
    page = authed_page
    mid = "v301" if kind == "sure" else "v204"
    api = ApprovalsApi(page, spans=[span(mid, "intro", kind, 0, 25_000)])
    page.goto(f"{app_url}/reprise/approve?show={show}&season={season}")
    expect(page.get_by_role("button", name=action)).to_be_enabled()
    expect(page.locator(".ap-card")).to_have_count(0)
    assert api.posts("/approvals/send") == []
    page.get_by_role("link", name="All seasons").click()
    expect(page.locator("#apQueue .ap-season")).to_be_visible()


@pytest.mark.e2e
def test_direct_fix_opens_the_exact_episode_and_kind_in_edit_mode(authed_page: Page, app_url: str) -> None:
    page = authed_page
    api = ApprovalsApi(page)
    target = f"{app_url}/reprise/approve?show=Community&season=2&episode_id=v201&kind=intro#fix"
    page.goto(target)
    expect(page.get_by_role("heading", name="S02E01 · Anthropology 101")).to_be_visible()
    expect(page.locator(".ap-top")).to_contain_text("Skip 1 of 1")
    expect(choose(page, "Adjust timings")).to_have_attribute("aria-pressed", "true")
    page.get_by_role("button", name="Skip ends: 1 second earlier").click()
    choose(page, "Approve skip").click()
    expect(page.get_by_role("heading", name="Episode review complete")).to_be_visible()
    assert api.posts("/approvals/decide") == [
        {
            "media_version_id": "v201",
            "kind": "intro",
            "decision": "skip",
            "expected_revision": 1,
            "start_ms": 0,
            "end_ms": 31_000,
        }
    ]
    assert api.posts("/approvals/send") == []
    page.get_by_role("button", name="Undo the last choice").click()
    expect(page.locator("#apTimes")).to_have_text("0:00 → 0:31 · 31 s")
    expect(choose(page, "Adjust timings")).to_have_attribute("aria-pressed", "true")
    page.get_by_role("link", name="Back to season").click()
    expect(page).to_have_url(re.compile(r"\?show=Community&season=2$"))
    expect(page.get_by_role("link", name="Review 2 skips")).to_be_visible()


@pytest.mark.e2e
def test_direct_fix_without_a_pending_skip_links_to_the_exact_library_episode(authed_page: Page, app_url: str) -> None:
    page = authed_page
    ApprovalsApi(page)
    page.goto(f"{app_url}/reprise/approve?show=Community&season=2&episode_id=v204#fix")
    expect(page.locator(".ap-season-work")).to_contain_text("No pending skip matches this episode.")
    expect(page.get_by_role("link", name="Fix in Library")).to_have_attribute(
        "href", "/reprise?show=Community&season=2&open=v204#fix"
    )
    expect(page.locator(".ap-card")).to_have_count(0)


@pytest.mark.e2e
def test_jump_playback_requests_the_edited_edges_on_demand(authed_page: Page, app_url: str) -> None:
    page = authed_page
    ApprovalsApi(page)
    clip = Path(__file__).resolve().parents[2] / "media_preview_generator/gpu/assets/test_video.mp4"
    requests: list[str] = []

    def preview(route: Route) -> None:
        requests.append(route.request.url)
        route.fulfill(path=clip, content_type="video/mp4")

    page.route(re.compile(r"/jump-preview\?"), preview)
    page.goto(f"{app_url}/reprise/approve?show=Community&season=2&episode_id=v203&kind=intro#fix")
    expect(page.get_by_role("button", name="Play the jump")).to_be_visible()
    video = page.locator(".ap-jump-video")
    expect(video).to_be_hidden()
    assert requests == []
    page.get_by_role("button", name="Skip ends: 1 second earlier").click()
    page.get_by_role("button", name="Play the jump").click()
    expect(video).to_be_visible()
    expect(video).to_have_js_property("controls", True)
    assert "/media/v203/jump-preview?start_ms=2000&end_ms=33000" in requests[0]
    page.get_by_role("button", name="Skip ends: 1 second later").click()
    expect(video).to_be_hidden()
    expect(page.get_by_role("button", name="Play the jump")).to_be_visible()


@pytest.mark.e2e
def test_jump_preview_failure_does_not_claim_playback(authed_page: Page, app_url: str) -> None:
    page = authed_page
    ApprovalsApi(page)
    page.route(
        re.compile(r"/jump-preview\?"), lambda route: route.fulfill(status=503, body="Video temporarily unavailable")
    )
    page.goto(f"{app_url}/reprise/approve?show=Community&season=2&episode_id=v203&kind=intro")
    page.get_by_role("button", name="Play the jump").click()
    expect(page.locator(".ap-jump-status")).to_have_text(
        "Jump preview unavailable. You can still check the timings below."
    )
    expect(page.locator(".ap-jump-video")).to_be_hidden()
    expect(choose(page, "Adjust timings")).to_be_visible()


@pytest.mark.e2e
def test_recent_changes_link_focuses_activity_and_keeps_undo_reachable(authed_page: Page, app_url: str) -> None:
    page = authed_page
    ApprovalsApi(page, automatic=three_automatic())
    page.goto(f"{app_url}/reprise/approve#automatic-sends")
    expect(page.locator("#automatic-sends")).to_be_focused()
    expect(page.locator("#apSent")).to_be_visible()
    expect(page.locator("#apSent").get_by_role("button", name="Undo")).to_have_count(2)
    page.locator("#apSent").get_by_text("Fix an episode", exact=True).first.click()
    expect(page.locator("#apSent").get_by_role("link", name="S02E01")).to_have_attribute(
        "href", "/reprise?show=Community&season=2&open=v201&kind=credits#fix"
    )


# ── phone layout ────────────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.e2e
def test_the_phone_layout_does_not_scroll_sideways(authed_page: Page, app_url: str) -> None:
    page = authed_page
    page.set_viewport_size({"width": 390, "height": 844})
    open_page(page, app_url)
    expect(community(page)).to_contain_text("Season 2")

    def overflow() -> int:
        return page.evaluate("document.documentElement.scrollWidth - window.innerWidth")

    assert overflow() <= 0
    start_review(page)
    page.keyboard.press("a")
    expect(page.get_by_role("button", name="Skip starts: 1 second later")).to_be_visible()
    assert overflow() <= 0
    # The three choices stay reachable at phone width.
    for name in ("Approve skip", "Keep Plex unchanged", "Adjust timings"):
        expect(choose(page, name)).to_be_in_viewport()
