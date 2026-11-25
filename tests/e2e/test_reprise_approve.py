"""E2E: the To approve screen.

One click for a season's sure skips (with Undo), the one-by-one review for the rest (buttons and keys, nudging a time,
Undo), the states where sending to Plex is off or a change went stale, and the theme picker. The /api/reprise/approvals*
and picture endpoints are answered by ``_approvals_api.ApprovalsApi``; the shell, scripts and styles are the real ones.
"""

from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import Page, expect

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


def choose(page: Page, name: str):
    """One of the three buttons of the one-by-one view (each carries its key as a small label)."""
    return page.locator(".ap-choose").get_by_role("button", name=re.compile(rf"^{name}\b"))


def start_review(page: Page) -> None:
    community(page).get_by_role("button", name="Review 2").click()
    expect(page.locator(".ap-card")).to_be_visible()


# ── the list ────────────────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.e2e
def test_the_queue_shows_each_season_and_what_you_can_do(authed_page: Page, app_url: str, errors: list[str]) -> None:
    page = authed_page
    open_page(page, app_url)

    expect(page.get_by_role("heading", name="To approve", level=1)).to_be_visible()
    expect(page.locator("#apSub")).to_have_text(
        "5 skips Reprise is sure about · 2 for you to look at · 1 episode approved, not sent yet"
    )
    shows = page.locator(".ap-show")
    expect(shows).to_have_count(2)
    expect(shows.nth(0)).to_contain_text("Community")  # the show that needs you most comes first
    expect(shows.nth(1)).to_contain_text("Parks and Recreation")

    season = community(page).locator(".ap-season")
    expect(season).to_contain_text("Season 2")
    expect(season).to_contain_text("3 sure ones")
    expect(season).to_contain_text("2 intros and 1 set of credits")
    expect(season.get_by_role("button", name="Approve & send 3 sure ones")).to_be_enabled()
    expect(season.get_by_role("button", name="Review 2")).to_be_enabled()
    expect(season.get_by_role("button", name="Send 1 episode to Plex")).to_be_enabled()
    # what a click would change in Plex, said in plain words, arrives after the list
    expect(season).to_contain_text("Intro skip is added on 2 episodes where Plex has none.")
    expect(page.locator('[data-tour="nav-approve"]')).to_have_attribute("aria-current", "page")
    assert errors == []


@pytest.mark.e2e
def test_nothing_waiting_says_so_and_a_failed_load_can_be_retried(authed_page: Page, app_url: str) -> None:
    page = authed_page
    api = ApprovalsApi(page, spans=[])
    page.goto(f"{app_url}/reprise/approve")
    expect(page.get_by_role("heading", name="Nothing is waiting for you")).to_be_visible()
    expect(page.get_by_role("link", name="Open the Library")).to_have_attribute("href", re.compile(r"/reprise$"))
    expect(page.locator("#apSub")).to_have_text("Nothing is waiting for you.")

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

    community(page).get_by_role("button", name="Approve & send 3 sure ones").click()
    sent = page.locator("#apSent")
    expect(sent).to_contain_text("Sent 3 skips to Plex")
    expect(sent).to_contain_text("Plex was backed up first")
    assert api.posts("/approvals/send") == [{"show": "Community", "season": 2, "scope": "sure", "to_plex": True}]
    # the season no longer offers them; the other show still does
    expect(page.get_by_role("button", name=re.compile(r"^Approve & send"))).to_have_count(1)

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
    expect(community(page).get_by_role("button", name="Approve & send 3 sure ones")).to_be_visible()
    expect(sent.get_by_role("button", name="Undo")).to_have_count(0)  # an undone change cannot be undone twice
    assert errors == []


@pytest.mark.e2e
def test_what_reprise_sent_by_itself_is_listed_and_can_be_undone_or_dismissed(
    authed_page: Page, app_url: str, errors: list[str]
) -> None:
    page = authed_page
    api = open_page(
        page,
        app_url,
        automatic=[
            automatic_entry("v201", 1, sent_at=1_700_000_100),
            automatic_entry("v202", 2, kinds=("intro", "credits"), sent_at=1_700_000_000),
        ],
    )
    sent = page.locator("#apSent")

    expect(sent).to_contain_text("Sent 1 ending to Plex automatically")
    expect(sent).to_contain_text("Community · Season 2 · E1")
    expect(sent).to_contain_text("Sent 1 intro and 1 ending to Plex automatically")
    assert [q for m, p, q, _ in api.calls if p == "/approvals/automatic"][0] == {"days": "7"}

    sent.locator(".ap-sent-row").first.get_by_role("button", name="Undo").click()
    expect(sent).to_contain_text("Undone")
    assert api.posts("/approvals/undo") == [
        {
            "changes": [
                {
                    "media_version_id": "v201",
                    "change_id": "auto-1",
                    "span_ids": ["owner-v201-credits"],
                    "label": "S02E01",
                }
            ]
        }
    ]
    expect(sent.locator(".ap-sent-row")).to_have_count(2)  # the undone one stays, as "Undone", until dismissed

    sent.locator(".ap-sent-row").nth(1).get_by_role("button", name="Dismiss").click()
    expect(sent.locator(".ap-sent-row")).to_have_count(1)
    assert json.loads(page.evaluate("localStorage.getItem('repriseAutoDismissed')")) == ["auto-2"]
    page.reload()  # a dismissed row stays gone, and the undone one is no longer sent
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

    community(page).get_by_role("button", name="Approve & send 3 sure ones").click()
    sent = page.locator("#apSent")
    expect(sent).to_contain_text("Sent 2 skips to Plex")
    expect(sent).to_contain_text("S02E02: Plex's data did not allow it: the episode moved.")
    # the one that did not go is approved and waits (with the one that already waited)
    expect(community(page)).to_contain_text("2 episodes approved, not sent yet")


@pytest.mark.e2e
def test_with_sending_off_it_only_approves(authed_page: Page, app_url: str) -> None:
    page = authed_page
    api = open_page(page, app_url, sending="off")

    expect(page.locator("#apNotice")).to_contain_text("Sending to Plex is turned off")
    expect(page.get_by_role("link", name="Open Sharing")).to_be_visible()
    expect(community(page).get_by_role("button", name="Send 1 episode to Plex")).to_be_disabled()
    expect(page.get_by_role("button", name=re.compile(r"Approve & send"))).to_have_count(0)

    community(page).get_by_role("button", name="Approve 3 sure ones").click()
    expect(page.locator("#apSent")).to_contain_text("Approved 3 skips")
    expect(page.locator("#apSent")).to_contain_text("sending to Plex is turned off")
    assert api.posts("/approvals/send") == [{"show": "Community", "season": 2, "scope": "sure", "to_plex": False}]
    expect(community(page).get_by_role("button", name="Send 3 episodes to Plex")).to_be_disabled()


@pytest.mark.e2e
def test_sending_a_season_you_approved_earlier(authed_page: Page, app_url: str) -> None:
    page = authed_page
    api = open_page(page, app_url)

    community(page).get_by_role("button", name="Send 1 episode to Plex").click()
    expect(page.locator("#apSent")).to_contain_text("Sent 1 skip to Plex")
    assert api.posts("/approvals/send") == [{"show": "Community", "season": 2, "scope": "approved", "to_plex": True}]
    page.locator("#apSent").get_by_role("button", name="Undo").click()
    expect(page.locator("#apSent")).to_contain_text("Undone")
    assert api.posts("/approvals/undo")[0]["changes"] == [
        {"media_version_id": "v204", "change_id": "c1", "span_ids": [], "label": "S02E04"}
    ]
    expect(community(page).get_by_role("button", name="Send 1 episode to Plex")).to_be_visible()


# ── one by one ──────────────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.e2e
def test_review_one_by_one_with_the_keyboard(authed_page: Page, app_url: str, errors: list[str]) -> None:
    page = authed_page
    api = open_page(page, app_url)
    start_review(page)

    # credits come first within an episode; the card says what it is, how sure Reprise is, and what Plex has
    card = page.locator(".ap-card")
    expect(card.get_by_role("heading", name="S02E03 · Basic Genealogy")).to_be_visible()
    expect(page.locator(".ap-top")).to_contain_text("Choice 1 of 2")
    expect(card).to_contain_text("A guess")
    expect(card).to_contain_text("Plex has it at 20:50 to 22:00. This moves the start 10 s earlier.")
    expect(page.locator("#apTimes")).to_have_text("20:40 → 22:00 · 1 min 20 s")
    expect(page.locator(".ap-choose")).to_contain_text("Skip these credits?")

    # A opens the nudge controls; the arrow keys move the picked time by a second
    page.keyboard.press("a")
    expect(choose(page, "Adjust")).to_have_attribute("aria-pressed", "true")
    expect(page.get_by_role("button", name="Skip starts: 1 second later")).to_be_visible()
    page.keyboard.press("ArrowRight")
    expect(page.locator("#apTimes")).to_have_text("20:41 → 22:00 · 1 min 19 s")
    expect(card.locator(".lu-clip").first).to_contain_text("20:41 · was 20:40")

    page.keyboard.press("s")
    expect(page.locator(".ap-top")).to_contain_text("Choice 2 of 2")
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
    expect(page.locator(".ap-choose")).to_contain_text("Skip this intro?")
    expect(card).to_contain_text("Plex has no intro skip here yet.")

    page.keyboard.press("k")
    expect(page.get_by_role("heading", name="All done with Season 2")).to_be_visible()
    expect(page.locator(".ap-done")).to_contain_text("You chose to skip 1 and keep 1.")
    assert api.posts("/approvals/decide")[1] == {
        "media_version_id": "v203",
        "expected_revision": 2,
        "kind": "intro",
        "decision": "keep",
    }

    # what you approved here and earlier waits to be sent
    expect(page.locator(".ap-done")).to_contain_text("2 episodes are approved and not in Plex yet")
    page.get_by_role("button", name="Send 2 episodes to Plex").click()
    expect(page.locator("#apQueue")).to_be_visible()
    expect(page.locator("#apSent")).to_contain_text("Sent 2 skips to Plex")
    assert api.posts("/approvals/send") == [{"show": "Community", "season": 2, "scope": "approved", "to_plex": True}]
    assert errors == []


@pytest.mark.e2e
def test_review_buttons_decide_and_the_back_button_leaves(authed_page: Page, app_url: str) -> None:
    page = authed_page
    api = open_page(page, app_url)
    start_review(page)

    choose(page, "Keep it").click()
    expect(page.locator(".ap-top")).to_contain_text("Choice 2 of 2")
    assert [(p["kind"], p["decision"]) for p in api.posts("/approvals/decide")] == [("credits", "keep")]

    page.go_back()  # the browser's own Back goes to the list, not out of the page
    expect(page.locator("#apQueue")).to_be_visible()
    expect(page.locator(".ap-card")).to_have_count(0)
    expect(community(page)).to_contain_text("1 to look at")


@pytest.mark.e2e
def test_undo_brings_back_the_choice_you_made(authed_page: Page, app_url: str) -> None:
    page = authed_page
    api = open_page(page, app_url)
    start_review(page)

    page.keyboard.press("a")
    page.keyboard.press("ArrowRight")
    page.keyboard.press("s")
    expect(page.locator(".ap-top")).to_contain_text("Choice 2 of 2")
    page.keyboard.press("u")

    expect(page.locator(".ap-top")).to_contain_text("Choice 1 of 2")
    assert api.posts("/approvals/decide")[1] == {
        "media_version_id": "v203",
        "expected_revision": 2,
        "decision": "restore",
        "kind": "credits",
        "span_id": "owner-v203-credits",
        "range_ids": [],
    }
    # the time you moved is still there to change again
    expect(choose(page, "Adjust")).to_have_attribute("aria-pressed", "true")
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
    expect(page.locator(".ap-top")).to_contain_text("Choice 2 of 2")
    page.wait_for_timeout(300)
    assert len(api.posts("/approvals/decide")) == 1


@pytest.mark.e2e
def test_snapping_to_a_cut_moves_the_picked_time(authed_page: Page, app_url: str) -> None:
    page = authed_page
    open_page(page, app_url)
    start_review(page)
    choose(page, "Keep it").click()  # on to the intro: 0:02 to 0:34, cuts at 0:31, 0:32 and 0:33
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
        "This changed while you were looking at it. Getting what is left."
    )
    expect(page.locator(".ap-top")).to_contain_text("Choice 1 of 2")
    expect(page.locator(".ap-card")).to_be_visible()
    # what is asked now carries the new revision
    page.keyboard.press("s")
    expect(page.locator(".ap-top")).to_contain_text("Choice 2 of 2")
    assert api.posts("/approvals/decide")[-1]["expected_revision"] == 2


# ── the theme, and the phone ────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.e2e
def test_the_theme_picker_changes_and_remembers_the_theme(authed_page: Page, app_url: str) -> None:
    page = authed_page
    open_page(page, app_url)
    html = page.locator("html")
    expect(page.get_by_role("button", name="Colour theme")).to_be_visible()

    page.get_by_role("button", name="Colour theme").click()
    dialog = page.get_by_role("dialog", name="Theme")
    expect(dialog).to_be_visible()
    dialog.get_by_role("radio", name="Dracula", exact=True).click()
    expect(html).to_have_attribute("data-theme", "dracula")
    expect(html).to_have_attribute("data-scheme", "dark")

    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    page.reload()
    expect(html).to_have_attribute("data-theme", "dracula")  # remembered, and applied before the page paints
    page.get_by_role("button", name="Colour theme").click()
    page.get_by_role("dialog", name="Theme").get_by_role("radio", name="Lucent").first.click()


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
    # the three choices stay reachable without scrolling
    for name in ("Skip it", "Keep it", "Adjust"):
        expect(choose(page, name)).to_be_in_viewport()
