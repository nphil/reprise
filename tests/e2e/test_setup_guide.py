"""E2E: the Setup guide (``/setup-guide``), a checklist for a new install, and the two deep links its buttons use.

``GET``/``POST /api/reprise/setup-guide`` are mocked with the payload shapes from the contract, so the tests cover what the
page does with each answer (the groups, the one primary button, blocked and working steps, copyable values, the calm
"everything is done" note, errors, Skip for now, the silent ``completed`` save), and never what the service decides. The
shell, scripts and styles are the real ones served by the app. The deep links (``/servers?edit=..&tab=..`` and
``/reprise?refresh=1``) are tested on their own pages with the API they call mocked.
"""

from __future__ import annotations

import copy
import json
import re
from urllib.parse import urlparse

import pytest
from playwright.sync_api import BrowserContext, Page, Route, expect

from ._mocks import _fulfill_json

pytestmark = pytest.mark.e2e

GPU_HELP = {"label": "How to add a graphics card", "href": "https://github.com/nphil/reprise#optional-gpu-decoding"}
FOLDER_HELP = {
    "label": "How to share media folders",
    "href": "https://github.com/nphil/reprise#share-your-media-folders-with-reprise",
}
SEND_HELP = {"label": "Read the safe steps", "href": "https://github.com/nphil/reprise#turn-on-sending-to-plex"}
SONARR = "http://192.168.1.50:8080/api/webhooks/sonarr"
RADARR = "http://192.168.1.50:8080/api/webhooks/radarr"
ONLINE_REASON = (
    "On, which is the default. Reprise asks the public databases IntroDB and SkipDB where intros and credits are. It "
    "sends only the show's id, the season and episode numbers and the file's length. Their answers are only "
    "suggestions: nothing is skipped until you approve it."
)
WEBHOOK_REASON = (
    "Not connected. Skip this if you don't use Sonarr or Radarr: after new episodes arrive, press Refresh library "
    "instead. If you do connect them, copy the addresses below and put the webhook secret (Settings, then "
    "Connections, then Webhook secret) in the Password field. Today these webhooks only drive Plex preview "
    "thumbnails, not intro and credits analysis, so for review only leave their import and upgrade events off."
)


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


# ---------------------------------------------------------------------------
# Payloads (the shapes the service emits)
# ---------------------------------------------------------------------------


def _step(number: int, step_id: str, title: str, state: str, reason: str, **extra) -> dict:
    return {
        "id": step_id,
        "number": number,
        "title": title,
        "required": number <= 3,
        "state": state,
        "done": state == "done",
        "reason": reason,
        "details": [],
        "values": [],
        "action": None,
        "help": None,
        "progress": None,
        "data": {},
        **extra,
    }


def _guide(steps: list[dict], state: str = "pending") -> dict:
    required = [step for step in steps if step["required"]]
    done = sum(1 for step in required if step["done"])
    return {
        "state": state,
        "auto_open": state == "pending" and done < len(required),
        "required_total": len(required),
        "required_done": done,
        "all_required_done": done == len(required),
        "steps": steps,
    }


def fresh() -> dict:
    """Just created the account: nothing is connected yet."""
    return _guide(
        [
            _step(
                1,
                "connect_plex",
                "Connect Plex",
                "todo",
                "Reprise isn't connected to a Plex server yet. You sign in with your Plex account; it takes about a minute.",
                action={"label": "Connect Plex", "href": "/setup"},
            ),
            _step(
                2,
                "libraries_and_folders",
                "Choose libraries and check your media folders",
                "blocked",
                "Connect Plex first.",
            ),
            _step(3, "refresh_library", "Refresh the library", "blocked", "Finish the steps above first."),
            _step(
                4,
                "gpu",
                "Use a graphics card (GPU)",
                "todo",
                "No graphics card was found, so analysis uses your processor (CPU). That works fine; a graphics card "
                "only makes the video decoding a little faster.",
                help=GPU_HELP,
            ),
            _step(
                5,
                "online_lookup",
                "Look up intro times online",
                "done",
                ONLINE_REASON,
                action={"label": "Change in Library", "href": "/reprise"},
            ),
            _step(
                6,
                "webhooks",
                "Connect Sonarr and Radarr",
                "todo",
                WEBHOOK_REASON,
                values=[
                    {"label": "Sonarr address", "value": SONARR},
                    {"label": "Radarr address", "value": RADARR},
                ],
                action={"label": "Open Automation", "href": "/automation#section-webhooks-sonarr-radarr"},
            ),
            _step(7, "send_to_plex", "Send results to Plex", "blocked", "Connect Plex first.", help=SEND_HELP),
        ]
    )


def partly() -> dict:
    """Plex is connected, one media folder is not visible."""
    guide = fresh()
    steps = guide["steps"]
    steps[0].update(state="done", done=True, reason="Connected to Home Plex.", action=None)
    steps[1].update(
        state="todo",
        reason=(
            "Reprise can't see 1 of the folders your libraries use. It only reads your files, so each folder Plex "
            "uses has to be shared with it."
        ),
        details=[
            "TV Shows: Plex calls this folder /data/tv. Reprise looks for it at /media/tv and finds nothing there. "
            "Share the folder that holds it with Reprise at /media/tv, read-only."
        ],
        action={"label": "Fix media folders", "href": "/servers?edit=plex-home&tab=paths"},
        help=FOLDER_HELP,
    )
    steps[6].update(reason="Finish the steps above first.")
    return _guide(steps)


def refreshing(percent: int = 42) -> dict:
    """Libraries are chosen and the refresh is running."""
    guide = partly()
    steps = guide["steps"]
    steps[1].update(
        state="done",
        done=True,
        reason="2 libraries chosen: TV Shows, Anime. Reprise can see all 3 of their folders.",
        details=[],
        action=None,
        help=None,
    )
    steps[2].update(
        state="working",
        reason="Reading your library from Plex now. This can take a few minutes.",
        progress={"percent": percent},
    )
    return _guide(steps)


def required_done(state: str = "pending") -> dict:
    """The three required steps are done; the optional ones are in the states a real install shows."""
    guide = refreshing()
    steps = guide["steps"]
    steps[2].update(state="done", done=True, reason="Reprise has read 4,258 episode files from Plex.", progress=None)
    steps[6].update(
        state="todo",
        reason=(
            "Review only is the default, so Reprise changes nothing in Plex. To turn sending on, Reprise has to see "
            "Plex's own database, and it can't yet."
        ),
        details=[
            "Home Plex: /plex exists, but Plex's database isn't in it. Only part of Plex's folder is shared (for "
            "example just Media/localhost). Share the whole \u201cPlex Media Server\u201d folder with Reprise, read-write."
        ],
        values=[
            {"label": "Share Plex's whole config folder at", "value": "/plex"},
            {"label": "Environment variable", "value": "PLEX_CONFIG_FOLDER=/plex"},
        ],
        action={"label": "Open Sharing", "href": "/reprise/sharing"},
        help=SEND_HELP,
    )
    return _guide(steps, state)


def long_text() -> dict:
    """A phone must cope with unbroken paths and addresses and with a long title."""
    guide = partly()
    path = "/mnt/user/media/" + "a-very-long-folder-name-without-any-spaces-" * 4 + "end"
    steps = guide["steps"]
    steps[1].update(
        title="Choose libraries and check every one of your media folders, including the very long ones",
        details=[f"TV Shows: Plex calls this folder {path}. Reprise looks for it at {path} and finds nothing there."],
    )
    steps[5]["values"] = [{"label": "Sonarr address", "value": "http://a-long-host.example.internal:8080/" + "x" * 140}]
    return _guide(steps)


class GuideApi:
    """``/api/reprise/setup-guide`` answered from ``payload``; every request is recorded."""

    def __init__(self, page: Page, payload: dict) -> None:
        self.payload = payload
        self.gets = 0
        self.posts: list[dict] = []
        self.fail_gets = 0
        self.fail_posts = False
        page.route("**/api/reprise/setup-guide", self._handle)

    def _handle(self, route: Route) -> None:
        request = route.request
        if request.method == "POST":
            body = json.loads(request.post_data or "{}")
            self.posts.append(body)  # recorded before the answer: a test that waits for the response then sees it
            if self.fail_posts:
                return _fulfill_json(route, {"error": "could not save"}, 500)
            return _fulfill_json(route, {"state": body["state"]})
        self.gets += 1
        if self.fail_gets:
            self.fail_gets -= 1
            return _fulfill_json(route, {"error": "boom"}, 500)
        return _fulfill_json(route, copy.deepcopy(self.payload))


class LibraryApi:
    """What the Library page asks for on load: an empty list, the Plex libraries, no jobs, and the refresh call."""

    def __init__(self, page: Page, libraries: list[dict] | None = None) -> None:
        self.libraries = (
            libraries
            if libraries is not None
            else [{"server_id": "s", "server_type": "plex", "server_name": "Home Plex", "id": 1, "name": "TV Shows"}]
        )
        self.refreshes: list[dict] = []
        page.route(re.compile(r".*/api/(reprise/|libraries).*"), self._handle)

    def _handle(self, route: Route) -> None:
        request = route.request
        path = urlparse(request.url).path
        if path == "/api/reprise/media":
            return _fulfill_json(route, {"items": [], "next_cursor": None, "shows": [], "seasons": []})
        if path == "/api/reprise/jobs":
            return _fulfill_json(route, {"items": []})
        if path == "/api/reprise/refresh" and request.method == "POST":
            self.refreshes.append(json.loads(request.post_data or "{}"))
            job = {
                "id": "job-r",
                "status": "running",
                "operation": "refresh",
                "created_at": "2026-11-04T12:00:00+00:00",
            }
            return _fulfill_json(route, {**job, "progress": {"percent": 0.0, "current_item": ""}}, 202)
        if path == "/api/reprise/settings":
            return _fulfill_json(route, {"publication_available": False, "writes_enabled": False})
        if path == "/api/libraries":
            return _fulfill_json(route, {"libraries": self.libraries})
        return route.fallback()  # anything else (the guide's own endpoint) goes to the mock registered before this one


def _open(page: Page, app_url: str) -> None:
    page.goto(f"{app_url}/setup-guide")
    expect(page.locator("#sgGroups")).not_to_have_attribute("aria-busy", "true")


def _row(page: Page, step_id: str):
    return page.locator(f'#repriseSetupGuide li[data-step-id="{step_id}"]')


def _weak(ratios: list) -> list:
    """The rows of a CONTRAST_JS answer that are missing or under the 4.5:1 minimum for text."""
    return [row for row in ratios if not isinstance(row[1], int | float) or row[1] < 4.5]


OVERFLOW_JS = """() => ({
    page: document.documentElement.scrollWidth - window.innerWidth,
    wide: [...document.querySelectorAll('#repriseSetupGuide *')]
        .filter(e => !e.closest('.rp-sr') && e.getBoundingClientRect().width > 0
            && (e.getBoundingClientRect().right > window.innerWidth + 0.5 || e.getBoundingClientRect().left < -0.5))
        .map(e => e.tagName.toLowerCase() + '.' + e.className),
})"""

FOCUS_JS = """() => {
    const e = document.activeElement, row = e.closest('li[data-step-id]');
    return row ? row.dataset.stepId + ':' + (e.dataset.focusKey || e.tagName) : (e.id || e.tagName);
}"""

# Text colour against whatever is painted behind it (the layers are composited from the page up), as the WCAG ratio.
CONTRAST_JS = """(targets) => {
    const paint = document.createElement('canvas').getContext('2d', { willReadFrequently: true });
    const rgba = css => { paint.clearRect(0, 0, 1, 1); paint.fillStyle = '#000'; paint.fillStyle = css; paint.fillRect(0, 0, 1, 1);
        const d = paint.getImageData(0, 0, 1, 1).data; return [d[0], d[1], d[2], d[3] / 255]; };
    const over = (top, base) => [0, 1, 2].map(i => top[i] * top[3] + base[i] * (1 - top[3])).concat([1]);
    const behind = el => { const layers = []; for (let e = el; e; e = e.parentElement) { const c = rgba(getComputedStyle(e).backgroundColor); if (c[3] > 0) layers.push(c); }
        return layers.reverse().reduce((under, layer) => over(layer, under), [0, 0, 0, 1]); };
    const lum = ([r, g, b]) => { const f = v => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; }; return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b); };
    return targets.map(([selector, label]) => {
        const el = document.querySelector(selector);
        if (!el) return [label, 'missing'];
        const bg = behind(el), a = lum(over(rgba(getComputedStyle(el).color), bg)), b = lum(bg);
        return [label, Math.round(((Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05)) * 100) / 100];
    });
}"""

# Text that must reach 4.5:1 in each state of the page (selector, what it is).
CONTRAST_ROWS = [
    ("#sgProgress", "progress"),
    ("#sgChecked", "time of the last check"),
    ("#sgRecheck", "Check again"),
    ("#sgSkip", "Skip for now"),
    ("#repriseSetupGuide h1", "page title"),
    ("#sgRequiredTitle", "group title"),
    ("#sgRequiredTitle + p", "group sentence"),
    ('li[data-step-id="connect_plex"] .sg-mark', "check in a filled circle"),
    ('li[data-step-id="connect_plex"] .rp-chip', "Done chip"),
    ('li[data-step-id="libraries_and_folders"] .sg-mark', "number of the current step"),
    ('li[data-step-id="libraries_and_folders"] .sg-title', "title"),
    ('li[data-step-id="libraries_and_folders"] .rp-chip', "Not done chip"),
    ('li[data-step-id="libraries_and_folders"] .sg-reason', "reason"),
    ('li[data-step-id="libraries_and_folders"] .sg-details li', "detail line"),
    ('li[data-step-id="libraries_and_folders"] .sg-help', "help link"),
    ('li[data-step-id="libraries_and_folders"] .sg-action', "primary button"),
    ('li[data-step-id="refresh_library"] .sg-mark', "number of a waiting step"),
    ('li[data-step-id="refresh_library"] .sg-title', "title of a waiting step"),
    ('li[data-step-id="refresh_library"] .rp-chip', "Waiting chip"),
    ('li[data-step-id="online_lookup"] .sg-action', "normal button"),
    ('li[data-step-id="webhooks"] .sg-value label', "value label"),
    ('li[data-step-id="webhooks"] .sg-value input', "value field"),
    ('li[data-step-id="webhooks"] .sg-copy .rp-btn', "Copy button"),
]


# ---------------------------------------------------------------------------
# What the page shows
# ---------------------------------------------------------------------------


class TestFreshInstall:
    def test_two_groups_and_one_clear_first_step(self, authed_page: Page, app_url: str) -> None:
        GuideApi(authed_page, fresh())
        _open(authed_page, app_url)
        guide = authed_page.locator("#repriseSetupGuide")
        expect(guide.get_by_role("heading", level=1)).to_have_text("Setup guide")
        expect(guide.get_by_role("heading", level=2)).to_have_text(["Required", "Optional"])
        expect(authed_page.locator("#sgProgress")).to_have_text("0 of 3 required steps done")
        expect(authed_page.locator("#sgRequired > li")).to_have_count(3)
        expect(authed_page.locator("#sgOptional > li")).to_have_count(4)
        expect(authed_page.locator("#sgOptional")).to_have_attribute("start", "4")
        expect(authed_page.locator("#sgRequired > li[data-required=true]")).to_have_count(3)
        expect(authed_page.locator("#sgOptional > li[data-required=false]")).to_have_count(4)
        expect(guide.locator("section[aria-labelledby]")).to_have_count(2)

    def test_step_one_has_the_only_primary_button_and_opens_the_wizard(self, authed_page: Page, app_url: str) -> None:
        GuideApi(authed_page, fresh())
        _open(authed_page, app_url)
        first = _row(authed_page, "connect_plex")
        expect(first).to_have_attribute("data-state", "todo")
        expect(first).to_have_attribute("aria-current", "step")
        expect(first.get_by_role("heading", level=3)).to_have_text("Connect Plex")
        expect(first.locator(".rp-chip")).to_have_text("Not done")
        expect(first).to_contain_text("Reprise isn't connected to a Plex server yet.")
        button = first.get_by_role("link", name="Connect Plex")
        expect(button).to_have_attribute("href", "/setup")
        expect(button).to_have_class(re.compile(r"\bprimary\b"))
        expect(authed_page.locator("#sgGroups .rp-btn.primary")).to_have_count(1)
        expect(authed_page.locator("#sgSkip")).to_be_hidden()  # Skip for now only appears once Plex is connected

    def test_blocked_steps_wait_without_a_button(self, authed_page: Page, app_url: str) -> None:
        GuideApi(authed_page, fresh())
        _open(authed_page, app_url)
        for step_id, reason in (
            ("libraries_and_folders", "Connect Plex first."),
            ("refresh_library", "Finish the steps above first."),
        ):
            row = _row(authed_page, step_id)
            expect(row).to_have_attribute("data-state", "blocked")
            expect(row.locator(".rp-chip")).to_have_text("Waiting")
            expect(row).to_contain_text(reason)
            expect(row.locator("a, button")).to_have_count(0)
        expect(authed_page.locator("#sgGroups li[data-state=blocked] .rp-btn")).to_have_count(0)

    def test_the_marker_is_a_number_until_a_step_is_done(self, authed_page: Page, app_url: str) -> None:
        GuideApi(authed_page, fresh())
        _open(authed_page, app_url)
        expect(_row(authed_page, "connect_plex").locator(".sg-mark")).to_have_text("1")
        expect(_row(authed_page, "refresh_library").locator(".sg-mark")).to_have_text("3")
        done = _row(authed_page, "online_lookup")
        expect(done.locator(".sg-mark .bi-check-lg")).to_have_count(1)
        expect(done.locator(".rp-chip")).to_have_text("Done")
        expect(done.locator(".sg-mark")).to_have_attribute("aria-hidden", "true")

    def test_text_from_the_service_is_shown_as_text_and_unsafe_links_are_dropped(
        self, authed_page: Page, app_url: str
    ) -> None:
        guide = fresh()
        guide["steps"][0]["reason"] = "<b>Bold</b> & <img src=x onerror=window.pwned=1>"
        guide["steps"][0]["action"] = {"label": "Go", "href": "javascript:window.pwned=1"}
        guide["steps"][3]["help"] = {"label": "Docs", "href": "javascript:window.pwned=1"}
        GuideApi(authed_page, guide)
        _open(authed_page, app_url)
        first = _row(authed_page, "connect_plex")
        expect(first).to_contain_text("<b>Bold</b> & <img src=x onerror=window.pwned=1>")
        expect(first.locator("b, img")).to_have_count(0)
        expect(first.locator("a, button")).to_have_count(0)
        expect(_row(authed_page, "gpu").locator("a")).to_have_count(0)
        assert authed_page.evaluate("window.pwned") is None

    def test_no_placeholder_text_leaks_onto_the_page(self, authed_page: Page, app_url: str) -> None:
        GuideApi(authed_page, fresh())
        _open(authed_page, app_url)
        text = authed_page.locator("#repriseSetupGuide").inner_text()
        for leak in ("undefined", "null", "NaN", "[object", "{{"):
            assert leak not in text, leak


class TestPartlySetUp:
    def test_missing_folders_are_listed_and_the_button_opens_the_paths_tab(
        self, authed_page: Page, app_url: str
    ) -> None:
        GuideApi(authed_page, partly())
        _open(authed_page, app_url)
        expect(authed_page.locator("#sgProgress")).to_have_text("1 of 3 required steps done")
        connected = _row(authed_page, "connect_plex")
        expect(connected).to_have_attribute("data-state", "done")
        expect(connected.locator(".rp-chip")).to_have_text("Done")
        expect(connected).to_contain_text("Connected to Home Plex.")
        expect(connected.locator("a, button")).to_have_count(0)
        folders = _row(authed_page, "libraries_and_folders")
        expect(folders).to_have_attribute("data-state", "todo")
        expect(folders).to_have_attribute("aria-current", "step")
        expect(folders.locator(".sg-details li")).to_have_count(1)
        expect(folders.locator(".sg-details li")).to_contain_text("TV Shows: Plex calls this folder /data/tv.")
        expect(folders.locator(".sg-details li")).to_contain_text("Share the folder that holds it with Reprise")
        action = folders.get_by_role("link", name="Fix media folders")
        expect(action).to_have_attribute("href", "/servers?edit=plex-home&tab=paths")
        expect(action).to_have_class(re.compile(r"\bprimary\b"))
        expect(authed_page.locator("#sgGroups .rp-btn.primary")).to_have_count(1)
        expect(_row(authed_page, "refresh_library")).to_have_attribute("data-state", "blocked")

    def test_the_help_link_opens_in_a_new_tab_safely(self, authed_page: Page, app_url: str) -> None:
        GuideApi(authed_page, partly())
        _open(authed_page, app_url)
        help_link = _row(authed_page, "libraries_and_folders").get_by_role(
            "link", name=re.compile("How to share media")
        )
        expect(help_link).to_have_attribute("href", FOLDER_HELP["href"])
        expect(help_link).to_have_attribute("target", "_blank")
        expect(help_link).to_have_attribute("rel", re.compile("noopener"))
        expect(help_link).to_contain_text("(opens in a new tab)")

    def test_skip_for_now_shows_once_plex_is_connected(self, authed_page: Page, app_url: str) -> None:
        GuideApi(authed_page, partly())
        _open(authed_page, app_url)
        expect(authed_page.get_by_role("button", name="Skip for now")).to_be_visible()
        expect(authed_page.get_by_role("button", name="Check again")).to_be_visible()


class TestWorkingStep:
    def test_a_running_refresh_shows_its_percent_and_has_no_button(self, authed_page: Page, app_url: str) -> None:
        GuideApi(authed_page, refreshing(42))
        _open(authed_page, app_url)
        row = _row(authed_page, "refresh_library")
        expect(row).to_have_attribute("data-state", "working")
        expect(row.locator(".rp-chip")).to_have_text("Working 42%")
        expect(row).to_contain_text("Reading your library from Plex now.")
        expect(row.get_by_role("progressbar")).to_have_attribute("aria-valuenow", "42")
        expect(row.locator("a, button")).to_have_count(0)
        expect(authed_page.locator("#sgProgress")).to_have_text("2 of 3 required steps done")
        expect(authed_page.locator("#sgGroups .rp-btn.primary")).to_have_count(0)

    def test_it_checks_again_by_itself_only_while_something_is_working(self, authed_page: Page, app_url: str) -> None:
        authed_page.clock.install()
        api = GuideApi(authed_page, refreshing(42))
        _open(authed_page, app_url)
        assert api.gets == 1
        api.payload = refreshing(80)
        authed_page.clock.fast_forward(3100)
        expect(_row(authed_page, "refresh_library").locator(".rp-chip")).to_have_text("Working 80%")
        assert api.gets == 2
        api.payload = required_done()  # the refresh finished: nothing is working, so the polling stops
        authed_page.clock.fast_forward(3100)
        expect(_row(authed_page, "refresh_library").locator(".rp-chip")).to_have_text("Done")
        gets = api.gets
        authed_page.clock.fast_forward(20_000)
        authed_page.wait_for_timeout(300)
        assert api.gets == gets

    def test_nothing_is_polled_when_nothing_is_working(self, authed_page: Page, app_url: str) -> None:
        authed_page.clock.install()
        api = GuideApi(authed_page, partly())
        _open(authed_page, app_url)
        authed_page.clock.fast_forward(30_000)
        authed_page.wait_for_timeout(300)
        assert api.gets == 1


class TestRequiredStepsDone:
    def test_a_calm_note_and_open_library_replace_the_primary_step_button(
        self, authed_page: Page, app_url: str
    ) -> None:
        GuideApi(authed_page, required_done())
        _open(authed_page, app_url)
        note = authed_page.locator("#sgDone")
        expect(note).to_be_visible()
        expect(note).to_contain_text("Everything Reprise needs is done. The optional steps below are up to you.")
        library = note.get_by_role("link", name="Open Library")
        expect(library).to_have_attribute("href", "/reprise")
        expect(library).to_have_class(re.compile(r"\bprimary\b"))
        expect(authed_page.locator("#sgProgress")).to_have_text("3 of 3 required steps done")
        expect(authed_page.locator("#sgRequired > li[data-state=done]")).to_have_count(3)
        expect(authed_page.locator("#sgRequired > li[aria-current=step]")).to_have_count(0)
        expect(authed_page.locator("#sgGroups .rp-btn.primary")).to_have_count(0)
        expect(authed_page.locator("#sgSkip")).to_be_hidden()
        expect(authed_page.locator("#sgDismissed")).to_be_hidden()

    def test_the_optional_steps_say_what_they_found(self, authed_page: Page, app_url: str) -> None:
        GuideApi(authed_page, required_done())
        _open(authed_page, app_url)
        gpu = _row(authed_page, "gpu")
        expect(gpu).to_have_attribute("data-state", "todo")
        expect(gpu).to_contain_text("No graphics card was found, so analysis uses your processor (CPU).")
        expect(gpu.get_by_role("link", name=re.compile("How to add a graphics card"))).to_have_attribute(
            "href", GPU_HELP["href"]
        )
        expect(gpu.locator(".sg-action")).to_have_count(0)
        online = _row(authed_page, "online_lookup")
        expect(online).to_have_attribute("data-state", "done")
        expect(online.get_by_role("link", name="Change in Library")).to_have_attribute("href", "/reprise")
        webhooks = _row(authed_page, "webhooks")
        expect(webhooks.get_by_label("Sonarr address", exact=True)).to_have_value(SONARR)
        expect(webhooks.get_by_label("Radarr address", exact=True)).to_have_value(RADARR)
        expect(webhooks.get_by_label("Sonarr address", exact=True)).to_have_attribute("readonly", "")
        expect(webhooks.get_by_role("link", name="Open Automation")).to_have_attribute(
            "href", "/automation#section-webhooks-sonarr-radarr"
        )
        # Optional steps never get the primary button, whatever state they are in.
        expect(webhooks.get_by_role("link", name="Open Automation")).not_to_have_class(re.compile(r"\bprimary\b"))

    def test_sending_to_plex_that_cannot_work_yet_explains_why_and_what_to_share(
        self, authed_page: Page, app_url: str
    ) -> None:
        GuideApi(authed_page, required_done())
        _open(authed_page, app_url)
        send = _row(authed_page, "send_to_plex")
        expect(send).to_have_attribute("data-state", "todo")
        expect(send).to_contain_text("Review only is the default, so Reprise changes nothing in Plex.")
        expect(send.locator(".sg-details li")).to_contain_text("Plex's database isn't in it")
        expect(send.get_by_label("Share Plex's whole config folder at", exact=True)).to_have_value("/plex")
        expect(send.get_by_label("Environment variable", exact=True)).to_have_value("PLEX_CONFIG_FOLDER=/plex")
        expect(send.get_by_role("link", name="Open Sharing")).to_have_attribute("href", "/reprise/sharing")
        expect(send.get_by_role("link", name=re.compile("Read the safe steps"))).to_have_attribute(
            "href", SEND_HELP["href"]
        )

    def test_dismissed_says_where_the_guide_lives_now(self, authed_page: Page, app_url: str) -> None:
        guide = partly()
        guide["state"] = "dismissed"
        GuideApi(authed_page, guide)
        _open(authed_page, app_url)
        expect(authed_page.locator("#sgDismissed")).to_have_text(
            "This guide won't open by itself now. It stays under Setup guide in the sidebar."
        )
        expect(authed_page.locator("#sgSkip")).to_be_hidden()
        expect(authed_page.locator("#rpSetupGuideLink")).to_have_attribute("aria-current", "page")


# ---------------------------------------------------------------------------
# Copy buttons
# ---------------------------------------------------------------------------


class TestCopy:
    def test_copy_puts_the_address_on_the_clipboard(
        self, authed_page: Page, context: BrowserContext, app_url: str
    ) -> None:
        context.grant_permissions(["clipboard-read", "clipboard-write"])
        GuideApi(authed_page, fresh())
        _open(authed_page, app_url)
        button = authed_page.get_by_role("button", name="Copy Sonarr address")
        expect(button).to_have_text("Copy")
        button.click()
        expect(button).to_have_text("Copied")
        assert authed_page.evaluate("navigator.clipboard.readText()") == SONARR
        expect(authed_page.locator("#sgLive")).to_have_text("Sonarr address copied.")
        authed_page.get_by_role("button", name="Copy Radarr address").click()
        assert authed_page.evaluate("navigator.clipboard.readText()") == RADARR

    def test_on_a_plain_http_address_it_selects_the_text_and_uses_the_older_copy_command(
        self, authed_page: Page, context: BrowserContext, app_url: str
    ) -> None:
        context.grant_permissions(["clipboard-read", "clipboard-write"])
        authed_page.add_init_script("Object.defineProperty(window, 'isSecureContext', { value: false })")
        GuideApi(authed_page, fresh())
        _open(authed_page, app_url)
        authed_page.get_by_role("button", name="Copy Radarr address").click()
        expect(authed_page.get_by_role("button", name="Copy Radarr address")).to_have_text("Copied")
        assert authed_page.evaluate("navigator.clipboard.readText()") == RADARR
        selected = authed_page.get_by_label("Radarr address", exact=True).evaluate(
            "e => [e.selectionStart, e.selectionEnd]"
        )
        assert selected == [0, len(RADARR)]

    def test_when_copying_is_refused_the_text_stays_selected_and_the_page_says_so(
        self, authed_page: Page, app_url: str
    ) -> None:
        authed_page.add_init_script(
            "Object.defineProperty(window, 'isSecureContext', { value: false }); document.execCommand = () => false;"
        )
        GuideApi(authed_page, fresh())
        _open(authed_page, app_url)
        authed_page.get_by_role("button", name="Copy Sonarr address").click()
        row = _row(authed_page, "webhooks")
        expect(row.locator(".sg-copy-note").first).to_have_text("Selected. Press Ctrl+C (or Cmd+C) to copy it.")
        expect(authed_page.get_by_role("button", name="Copy Sonarr address")).to_have_text("Copy")
        selection = authed_page.get_by_label("Sonarr address", exact=True).evaluate(
            "e => [e.selectionStart, e.selectionEnd]"
        )
        assert selection == [0, len(SONARR)]

    def test_landing_in_a_value_field_selects_all_of_it(self, authed_page: Page, app_url: str) -> None:
        GuideApi(authed_page, fresh())
        _open(authed_page, app_url)
        field = authed_page.get_by_label("Sonarr address", exact=True)
        field.focus()
        assert field.evaluate("e => [e.selectionStart, e.selectionEnd]") == [0, len(SONARR)]


# ---------------------------------------------------------------------------
# Loading, errors and the three saves
# ---------------------------------------------------------------------------


class TestStatesAndSaves:
    def test_a_slow_answer_shows_rows_in_the_shape_of_the_real_ones(self, authed_page: Page, app_url: str) -> None:
        held: list[Route] = []
        authed_page.route("**/api/reprise/setup-guide", lambda route: held.append(route))
        authed_page.goto(f"{app_url}/setup-guide")
        expect(authed_page.locator("#sgRequired .sg-skel")).to_have_count(3)
        expect(authed_page.locator("#sgOptional .sg-skel")).to_have_count(4)
        expect(authed_page.locator("#sgGroups")).to_have_attribute("aria-busy", "true")
        expect(authed_page.locator("#sgRequired .sg-skel").first).to_have_attribute("aria-hidden", "true")
        expect(authed_page.get_by_role("heading", level=2, name="Required")).to_be_visible()
        assert held, "the page never asked for the guide"
        _fulfill_json(held[0], fresh())
        expect(authed_page.locator("#sgRequired .sg-step")).to_have_count(3)
        expect(authed_page.locator(".sg-skel")).to_have_count(0)

    def test_an_error_says_so_in_plain_words_and_try_again_recovers(self, authed_page: Page, app_url: str) -> None:
        api = GuideApi(authed_page, fresh())
        api.fail_gets = 1
        authed_page.goto(f"{app_url}/setup-guide")
        error = authed_page.locator("#sgError")
        expect(error).to_be_visible()
        expect(error).to_contain_text("Reprise couldn't check your setup just now.")
        expect(authed_page.locator("#sgGroups")).to_be_hidden()
        expect(authed_page.locator("#sgLive")).to_have_text("Reprise couldn't check your setup just now.")
        authed_page.get_by_role("button", name="Try again").click()
        expect(authed_page.locator("#sgRequired .sg-step")).to_have_count(3)
        expect(error).to_be_hidden()
        expect(authed_page.locator("#sgRecheck")).to_be_focused()

    def test_a_failed_recheck_keeps_what_was_already_on_screen(self, authed_page: Page, app_url: str) -> None:
        api = GuideApi(authed_page, partly())
        _open(authed_page, app_url)
        api.fail_gets = 1
        authed_page.get_by_role("button", name="Check again").click()
        expect(authed_page.locator("#sgError")).to_contain_text("Reprise couldn't check your setup just now.")
        expect(authed_page.locator("#sgRequired .sg-step")).to_have_count(3)
        authed_page.get_by_role("button", name="Try again").click()
        expect(authed_page.locator("#sgError")).to_be_hidden()

    def test_skip_for_now_saves_dismissed_then_opens_the_library(self, authed_page: Page, app_url: str) -> None:
        api = GuideApi(authed_page, partly())
        LibraryApi(authed_page)
        _open(authed_page, app_url)
        authed_page.get_by_role("button", name="Skip for now").click()
        expect(authed_page).to_have_url(f"{app_url}/reprise")
        assert api.posts == [{"state": "dismissed"}]

    def test_skip_for_now_stays_put_and_says_so_when_it_cannot_be_saved(self, authed_page: Page, app_url: str) -> None:
        api = GuideApi(authed_page, partly())
        api.fail_posts = True
        _open(authed_page, app_url)
        skip = authed_page.get_by_role("button", name="Skip for now")
        skip.click()
        expect(authed_page.locator("#sgError")).to_contain_text("Reprise couldn't save that.")
        expect(authed_page.locator("#sgRetry")).to_be_hidden()
        expect(skip).to_be_enabled()
        assert authed_page.url == f"{app_url}/setup-guide"
        assert api.posts == [{"state": "dismissed"}]

    def test_finishing_the_required_steps_is_saved_once_and_silently(self, authed_page: Page, app_url: str) -> None:
        api = GuideApi(authed_page, required_done("pending"))
        with authed_page.expect_response(
            lambda r: r.url.endswith("/api/reprise/setup-guide") and r.request.method == "POST"
        ):
            _open(authed_page, app_url)
        assert api.posts == [{"state": "completed"}]
        expect(authed_page.locator("#sgError")).to_be_hidden()
        expect(authed_page.locator("#sgDismissed")).to_be_hidden()
        expect(authed_page.locator("#sgLive")).to_have_text("")
        authed_page.get_by_role("button", name="Check again").click()
        expect(authed_page.locator("#sgLive")).to_have_text("Checked. 3 of 3 required steps done.")
        assert api.posts == [{"state": "completed"}], "it must be sent once, not on every check"

    @pytest.mark.parametrize("saved", ["completed", "dismissed"])
    def test_a_guide_that_already_has_an_answer_is_not_saved_again(
        self, authed_page: Page, app_url: str, saved: str
    ) -> None:
        api = GuideApi(authed_page, required_done(saved))
        _open(authed_page, app_url)
        authed_page.wait_for_timeout(400)
        assert api.posts == []

    def test_open_steps_never_trigger_the_completed_save(self, authed_page: Page, app_url: str) -> None:
        api = GuideApi(authed_page, partly())
        _open(authed_page, app_url)
        authed_page.wait_for_timeout(400)
        assert api.posts == []


class TestCheckingAgain:
    def test_check_again_picks_up_what_changed_and_says_so(self, authed_page: Page, app_url: str) -> None:
        api = GuideApi(authed_page, partly())
        _open(authed_page, app_url)
        api.payload = refreshing(10)
        authed_page.get_by_role("button", name="Check again").click()
        expect(_row(authed_page, "libraries_and_folders").locator(".rp-chip")).to_have_text("Done")
        expect(_row(authed_page, "refresh_library").locator(".rp-chip")).to_have_text("Working 10%")
        expect(authed_page.locator("#sgLive")).to_contain_text("Checked. 2 of 3 required steps done.")
        expect(authed_page.locator("#sgLive")).to_contain_text("Choose libraries and check your media folders is done.")
        expect(authed_page.locator("#sgChecked")).to_contain_text("Checked")

    def test_coming_back_to_the_tab_checks_without_being_asked(self, authed_page: Page, app_url: str) -> None:
        api = GuideApi(authed_page, partly())
        _open(authed_page, app_url)
        api.payload = refreshing(10)
        authed_page.evaluate("() => document.dispatchEvent(new Event('visibilitychange'))")
        expect(_row(authed_page, "libraries_and_folders").locator(".rp-chip")).to_have_text("Done")
        expect(authed_page.locator("#sgLive")).to_have_text(
            "Choose libraries and check your media folders is done. Refresh the library is in progress."
        )

    def test_a_page_restored_from_the_back_button_checks_again(self, authed_page: Page, app_url: str) -> None:
        api = GuideApi(authed_page, partly())
        _open(authed_page, app_url)
        api.payload = refreshing(10)
        authed_page.evaluate("() => window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true }))")
        expect(_row(authed_page, "libraries_and_folders").locator(".rp-chip")).to_have_text("Done")

    def test_a_check_never_takes_keyboard_focus_away(self, authed_page: Page, app_url: str) -> None:
        api = GuideApi(authed_page, refreshing(42))
        _open(authed_page, app_url)
        copy_button = authed_page.get_by_role("button", name="Copy Sonarr address")
        copy_button.focus()
        api.payload = refreshing(60)  # only the refresh row changes: the focused row is left alone
        authed_page.evaluate("() => document.dispatchEvent(new Event('visibilitychange'))")
        expect(_row(authed_page, "refresh_library").locator(".rp-chip")).to_have_text("Working 60%")
        expect(copy_button).to_be_focused()
        changed = refreshing(60)
        # The focused row itself is rebuilt this time: focus must follow to the same control in the new row.
        changed["steps"][5]["reason"] = "Not connected yet."
        api.payload = changed
        authed_page.evaluate("() => document.dispatchEvent(new Event('visibilitychange'))")
        expect(_row(authed_page, "webhooks")).to_contain_text("Not connected yet.")
        expect(authed_page.get_by_role("button", name="Copy Sonarr address")).to_be_focused()

    def test_a_slow_check_says_it_is_checking_and_goes_back_when_done(self, authed_page: Page, app_url: str) -> None:
        GuideApi(authed_page, partly())
        _open(authed_page, app_url)
        held: list[Route] = []
        authed_page.route("**/api/reprise/setup-guide", lambda route: held.append(route))
        authed_page.get_by_role("button", name="Check again").click()
        busy = authed_page.get_by_role("button", name="Checking…")
        expect(busy).to_have_attribute("aria-busy", "true")
        assert len(held) == 1
        _fulfill_json(held[0], partly())
        expect(authed_page.get_by_role("button", name="Check again")).not_to_have_attribute("aria-busy", "true")
        expect(authed_page.locator("#sgLive")).to_have_text("Checked. 1 of 3 required steps done.")


# ---------------------------------------------------------------------------
# Layout and keyboard
# ---------------------------------------------------------------------------


class TestLayout:
    @pytest.mark.parametrize("width", [390, 1024, 1440])
    @pytest.mark.parametrize("payload", [fresh, partly, refreshing, required_done, long_text])
    def test_nothing_overflows_sideways(self, authed_page: Page, app_url: str, width: int, payload) -> None:
        GuideApi(authed_page, payload())
        authed_page.set_viewport_size({"width": width, "height": 900})
        _open(authed_page, app_url)
        found = authed_page.evaluate(OVERFLOW_JS)
        assert found["page"] <= 0, found
        assert found["wide"] == [], found

    def test_the_button_sits_under_the_text_on_a_phone_and_at_the_right_edge_from_a_tablet_up(
        self, authed_page: Page, app_url: str
    ) -> None:
        GuideApi(authed_page, partly())
        geometry = """() => {
            const row = document.querySelector('li[data-step-id="libraries_and_folders"]');
            const body = row.querySelector('.sg-body').getBoundingClientRect(), action = row.querySelector('.sg-action').getBoundingClientRect();
            return { below: action.top >= body.bottom - 1, beside: action.left >= body.right - 1, width: action.width, body: body.width };
        }"""
        authed_page.set_viewport_size({"width": 390, "height": 900})
        _open(authed_page, app_url)
        phone = authed_page.evaluate(geometry)
        assert phone["below"] and not phone["beside"], phone
        assert phone["width"] >= phone["body"] - 1, "on a phone the button takes the full width of the text column"
        for width in (1024, 1440):
            authed_page.set_viewport_size({"width": width, "height": 900})
            wide = authed_page.evaluate(geometry)
            assert wide["beside"] and not wide["below"], (width, wide)

    def test_every_control_is_a_full_size_target(self, authed_page: Page, app_url: str) -> None:
        GuideApi(authed_page, required_done())
        authed_page.set_viewport_size({"width": 390, "height": 900})
        _open(authed_page, app_url)
        small = authed_page.evaluate(
            """() => [...document.querySelectorAll('#repriseSetupGuide a[href], #repriseSetupGuide button, #repriseSetupGuide input')]
                .filter(e => e.getClientRects().length && !e.closest('[hidden]'))
                .map(e => [e.id || e.textContent.trim().slice(0, 24), e.getBoundingClientRect().height])
                .filter(([, height]) => height < 43.5)"""
        )
        assert small == [], small

    @pytest.mark.parametrize("theme", ["dark", "light"])
    def test_the_checklist_text_reaches_4_5_to_1_in_both_themes(
        self, authed_page: Page, app_url: str, theme: str
    ) -> None:
        GuideApi(authed_page, partly())
        _open(authed_page, app_url)
        authed_page.evaluate("(theme) => document.documentElement.setAttribute('data-bs-theme', theme)", theme)
        assert _weak(authed_page.evaluate(CONTRAST_JS, [list(row) for row in CONTRAST_ROWS])) == []

    @pytest.mark.parametrize("theme", ["dark", "light"])
    def test_the_notes_and_the_error_box_reach_4_5_to_1_in_both_themes(
        self, authed_page: Page, app_url: str, theme: str
    ) -> None:
        done = required_done()
        done["state"] = "dismissed"  # the calm note and the quiet "won't open by itself" line are both on screen
        api = GuideApi(authed_page, done)
        _open(authed_page, app_url)
        authed_page.evaluate("(theme) => document.documentElement.setAttribute('data-bs-theme', theme)", theme)
        notes = [["#sgDone p", "calm note"], ["#sgOpenLibrary", "Open Library"], ["#sgDismissed", "quiet note"]]
        assert _weak(authed_page.evaluate(CONTRAST_JS, notes)) == []
        api.fail_gets = 1
        authed_page.get_by_role("button", name="Check again").click()
        expect(authed_page.locator("#sgError")).to_be_visible()
        errors = [["#sgErrorText", "error sentence"], ["#sgRetry", "Try again"]]
        assert _weak(authed_page.evaluate(CONTRAST_JS, errors)) == []

    def test_the_working_ring_and_dot_stand_still_when_reduced_motion_is_asked_for(
        self, authed_page: Page, app_url: str
    ) -> None:
        GuideApi(authed_page, refreshing())
        _open(authed_page, app_url)
        ring = "() => getComputedStyle(document.querySelector('li[data-step-id=refresh_library] .sg-mark'), '::after').animationName"
        dot = "() => getComputedStyle(document.querySelector('li[data-step-id=refresh_library] .rp-chip'), '::before').animationName"
        assert authed_page.evaluate(ring) == "sg-spin"
        assert authed_page.evaluate(dot) == "rp-pulse"
        authed_page.emulate_media(reduced_motion="reduce")
        assert authed_page.evaluate(ring) == "none"
        assert authed_page.evaluate(dot) == "none"


class TestKeyboard:
    def test_tab_visits_check_again_skip_for_now_then_each_step_in_order(self, authed_page: Page, app_url: str) -> None:
        GuideApi(authed_page, partly())
        _open(authed_page, app_url)
        authed_page.locator("#main-content").focus()
        trail = []
        for _ in range(12):
            authed_page.keyboard.press("Tab")
            trail.append(authed_page.evaluate(FOCUS_JS))
        assert trail == [
            "sgRecheck",
            "sgSkip",
            "libraries_and_folders:help",
            "libraries_and_folders:action",
            "gpu:help",
            "online_lookup:action",
            "webhooks:value-0",
            "webhooks:copy-0",
            "webhooks:value-1",
            "webhooks:copy-1",
            "webhooks:action",
            "send_to_plex:help",
        ]

    def test_the_actions_are_reached_in_step_order_and_enter_follows_one(self, authed_page: Page, app_url: str) -> None:
        GuideApi(authed_page, fresh())
        authed_page.route(
            "**/setup", lambda route: route.fulfill(status=200, content_type="text/html", body="<title>wizard</title>")
        )
        _open(authed_page, app_url)
        authed_page.locator("#sgRecheck").focus()
        authed_page.keyboard.press("Tab")
        expect(_row(authed_page, "connect_plex").get_by_role("link", name="Connect Plex")).to_be_focused()
        authed_page.keyboard.press("Enter")
        expect(authed_page).to_have_url(f"{app_url}/setup")

    def test_focus_is_drawn(self, authed_page: Page, app_url: str) -> None:
        GuideApi(authed_page, fresh())
        _open(authed_page, app_url)
        authed_page.locator("#sgRecheck").focus()
        authed_page.keyboard.press("Tab")
        outline = authed_page.evaluate("() => getComputedStyle(document.activeElement).outlineStyle")
        assert outline == "solid"


class TestPageFrame:
    def test_title_and_sidebar(self, authed_page: Page, app_url: str) -> None:
        GuideApi(authed_page, fresh())
        _open(authed_page, app_url)
        expect(authed_page).to_have_title("Setup guide · Reprise")
        link = authed_page.locator("#rpSetupGuideLink")
        expect(link).to_have_text("Setup guide")
        expect(link).to_have_attribute("aria-current", "page")
        expect(link).to_have_attribute("href", "/setup-guide")
        for destination in ("Library", "Settings"):
            expect(authed_page.locator(".rp-rail-nav").get_by_role("link", name=destination)).not_to_have_attribute(
                "aria-current", re.compile(".")
            )

    def test_the_tour_does_not_open_over_the_guide(self, authed_page: Page, app_url: str) -> None:
        GuideApi(authed_page, fresh())
        _open(authed_page, app_url)
        expect(authed_page.locator("#repriseTour")).to_have_attribute("data-auto", "false")
        expect(authed_page.locator("#repriseTour")).not_to_have_attribute("open", re.compile(".*"))

    @pytest.mark.parametrize(("path", "says_it"), [("/setup-guide", False), ("/servers", True)])
    def test_missing_folder_cards_are_not_said_twice(
        self, authed_page: Page, app_url: str, path: str, says_it: bool
    ) -> None:
        """Step 2 already names every folder Reprise cannot see, so the page-top cards for them stay off the guide only."""
        GuideApi(authed_page, partly())
        authed_page.route(
            "**/api/system/config-health",
            lambda route: _fulfill_json(
                route,
                {
                    "config": {
                        "writable": True,
                        "warnings": [{"kind": "network_fs", "message": "On a network share."}],
                    },
                    "media_mount_issues": [{"path": "/media/tv", "issue": "missing"}],
                },
            ),
        )
        authed_page.goto(f"{app_url}{path}")
        banner = authed_page.locator("#configHealthBanner")
        expect(banner).to_contain_text("On a network share.")  # the check ran and its other cards still show
        if says_it:
            expect(banner).to_contain_text("/media/tv")
        else:
            expect(banner).not_to_contain_text("Media mount")


# ---------------------------------------------------------------------------
# The two deep links the guide's buttons use
# ---------------------------------------------------------------------------


def _plex_server(server_id: str = "plex-home") -> dict:
    return {
        "id": server_id,
        "type": "plex",
        "name": "Home Plex",
        "enabled": True,
        "url": "http://plex.invalid:32400",
        "verify_ssl": True,
        "timeout": 30,
        "auth": {"method": "token"},
        "libraries": [{"id": "2", "name": "TV Shows", "kind": "show", "enabled": True, "remote_paths": []}],
        "path_mappings": [
            {
                "remote_prefix": "/data/tv",
                "plex_prefix": "/data/tv",
                "local_prefix": "/media/tv",
                "webhook_prefixes": [],
            }
        ],
        "exclude_paths": [],
        "output": {"adapter": "plex_bundle", "plex_config_folder": "/tmp", "frame_interval": 10},
        "markers": {"enabled": False, "library_ids": None, "plex": {"on_plex_redetect": "restore"}},
        "loudness": {"enabled": False, "library_ids": None},
    }


def _mock_servers(page: Page, servers: list[dict]) -> None:
    """The Connections page's calls, answered locally (the same set test_server_modal_redesign.py mocks)."""
    by_id = {server["id"]: server for server in servers}
    page.route("**/api/servers", lambda route: _fulfill_json(route, {"servers": servers}))

    def single(
        server_id: str,
    ):  # a factory: Playwright passes a second argument to a handler that has a second parameter
        return lambda route: _fulfill_json(route, by_id[server_id])

    for server_id in by_id:
        page.route(f"**/api/servers/{server_id}", single(server_id))
    page.route(
        "**/api/servers/*/test-connection",
        lambda route: _fulfill_json(route, {"ok": True, "message": "Connected", "version": "Plex 1.43.4"}),
    )
    page.route(
        "**/api/servers/*/previews-readiness",
        lambda route: _fulfill_json(route, {"vendor": "plex", "overall_ok": True, "sections": []}),
    )
    page.route("**/api/servers/*/refresh-libraries", lambda route: _fulfill_json(route, {"ok": True}))
    page.route(
        "**/api/markers/servers/*/status*",
        lambda route: _fulfill_json(
            route,
            {
                "server_id": "plex-home",
                "server_type": "plex",
                "enabled": False,
                "settings": {"enabled": False, "library_ids": None},
                "capability": {"state": "ready", "message": "", "details": {}, "warning": ""},
                "can_show": ["intro", "credits"],
                "libraries": [],
            },
        ),
    )
    page.route(
        "**/api/settings/plex_webhook/status*",
        lambda route: _fulfill_json(
            route, {"registered_in_plex": False, "has_plex_pass": True, "default_url": "http://localhost/hook"}
        ),
    )
    page.route(
        "**/api/settings/*_webhook/info*",
        lambda route: _fulfill_json(
            route,
            {
                "webhook_url_per_server": "http://localhost/api/webhooks/server/x",
                "auth_header_name": "X-Auth-Token",
                "plugin": {"plugin_name": "Webhook plugin", "install_url": "https://example.com", "config_steps": []},
            },
        ),
    )
    page.route("**/api/schedules", lambda route: _fulfill_json(route, {"schedules": []}))
    page.route("**/api/settings/validate-*", lambda route: _fulfill_json(route, {"exists": True, "detail": "valid"}))


class TestConnectionsDeepLink:
    @pytest.mark.parametrize(
        ("tab", "pane"),
        [
            ("paths", "edit-tab-paths"),
            ("libraries", "edit-tab-libraries"),
            ("connection", "edit-tab-general"),
            ("health", "edit-tab-health"),
        ],
    )
    def test_the_edit_dialog_opens_on_the_tab_the_guide_names(
        self, authed_page: Page, app_url: str, tab: str, pane: str
    ) -> None:
        _mock_servers(authed_page, [_plex_server()])
        authed_page.goto(f"{app_url}/servers?edit=plex-home&tab={tab}")
        expect(authed_page.locator("#editServerModal")).to_be_visible(timeout=10000)
        expect(authed_page.locator("#editServerName")).to_have_text("Home Plex")
        expect(authed_page.locator(f"#{pane}")).to_be_visible()
        expect(authed_page.locator(f'#editServerModal [data-bs-target="#{pane}"]')).to_have_class(
            re.compile(r"\bactive\b")
        )
        expect(authed_page.locator("#editServerSectionSelect")).to_have_value(pane)
        assert authed_page.url == f"{app_url}/servers", "the query is removed once it has been used"

    def test_the_path_mappings_tab_shows_the_server_folders(self, authed_page: Page, app_url: str) -> None:
        _mock_servers(authed_page, [_plex_server()])
        authed_page.goto(f"{app_url}/servers?edit=plex-home&tab=paths")
        expect(authed_page.locator("#edit-tab-paths")).to_be_visible(timeout=10000)
        expect(authed_page.locator("#edit-tab-paths .pm-local")).to_have_value("/media/tv")

    def test_without_a_tab_the_dialog_opens_on_connection(self, authed_page: Page, app_url: str) -> None:
        _mock_servers(authed_page, [_plex_server()])
        authed_page.goto(f"{app_url}/servers?edit=plex-home")
        expect(authed_page.locator("#edit-tab-general")).to_be_visible(timeout=10000)

    def test_a_server_that_is_not_listed_does_nothing_visible(self, authed_page: Page, app_url: str) -> None:
        _mock_servers(authed_page, [_plex_server()])
        authed_page.goto(f"{app_url}/servers?edit=gone&tab=paths")
        expect(authed_page.locator(".edit-server-btn[data-id='plex-home']")).to_be_visible(timeout=10000)
        authed_page.wait_for_timeout(400)
        expect(authed_page.locator("#editServerModal")).to_be_hidden()
        expect(authed_page.locator("#toastNotification.show")).to_have_count(0)
        assert authed_page.url == f"{app_url}/servers"

    def test_a_plain_visit_opens_no_dialog(self, authed_page: Page, app_url: str) -> None:
        _mock_servers(authed_page, [_plex_server()])
        authed_page.goto(f"{app_url}/servers")
        expect(authed_page.locator(".edit-server-btn[data-id='plex-home']")).to_be_visible(timeout=10000)
        authed_page.wait_for_timeout(400)
        expect(authed_page.locator("#editServerModal")).to_be_hidden()


class TestLibraryDeepLink:
    def test_the_refresh_link_starts_one_refresh_and_clears_the_address(self, authed_page: Page, app_url: str) -> None:
        library = LibraryApi(authed_page)
        authed_page.goto(f"{app_url}/reprise?refresh=1")
        expect(authed_page.locator("#rpMessage")).to_contain_text("Refreshing TV Shows")
        assert library.refreshes == [{"server_id": "s", "library_id": "1"}]
        assert authed_page.url == f"{app_url}/reprise"
        authed_page.wait_for_timeout(300)
        assert len(library.refreshes) == 1

    def test_with_several_libraries_it_asks_which_one(self, authed_page: Page, app_url: str) -> None:
        library = LibraryApi(
            authed_page,
            [
                {"server_id": "s", "server_type": "plex", "server_name": "Home Plex", "id": 1, "name": "TV Shows"},
                {"server_id": "s", "server_type": "plex", "server_name": "Home Plex", "id": 2, "name": "Anime"},
            ],
        )
        authed_page.goto(f"{app_url}/reprise?refresh=1")
        expect(authed_page.locator("#rpRefreshForm")).to_be_visible()
        expect(authed_page.locator("#rpLibraryChoice option")).to_have_text(
            ["Home Plex · TV Shows", "Home Plex · Anime"]
        )
        assert library.refreshes == []
        assert authed_page.url == f"{app_url}/reprise"

    def test_a_plain_visit_never_refreshes_by_itself(self, authed_page: Page, app_url: str) -> None:
        library = LibraryApi(authed_page)
        authed_page.goto(f"{app_url}/reprise")
        expect(authed_page.locator("#rpLibraryState")).to_contain_text("No episodes yet")
        authed_page.wait_for_timeout(300)
        assert library.refreshes == []

    @pytest.mark.parametrize("connected", [True, False])
    def test_the_empty_library_points_to_the_guide(self, authed_page: Page, app_url: str, connected: bool) -> None:
        LibraryApi(authed_page, None if connected else [])
        authed_page.goto(f"{app_url}/reprise")
        state = authed_page.locator("#rpLibraryState")
        expect(state).to_contain_text("No episodes yet")
        link = state.get_by_role("link", name="Setup guide")
        expect(link).to_have_attribute("href", "/setup-guide")
        expect(link).to_have_class(re.compile(r"\bquiet\b"))
        main_action = "Refresh library" if connected else "Open Connections"
        expect(state.get_by_role("button" if connected else "link", name=main_action)).to_be_visible()
