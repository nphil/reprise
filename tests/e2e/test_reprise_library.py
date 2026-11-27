"""E2E: the Library page, Reprise's home.

Plain-language statuses, technical identifiers tucked behind "File details", every analysis outcome explained, and the
pick -> analyze -> progress -> result flow. The /api/reprise/* and /api/libraries calls are mocked, so no media or Plex
is needed; the shell, scripts and styles are the real ones served by the app.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import Page, Route, expect

from ._mocks import _fulfill_json
from .conftest import accept_app_confirm

DURATION = 1_320_000
TEST_IMAGE = '<svg xmlns="http://www.w3.org/2000/svg" width="2" height="3"><rect width="2" height="3"/></svg>'


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


def _identity(n: int) -> dict:
    return {
        "plex_machine_identifier": "machine",
        "rating_key": str(100 + n),
        "media_id": str(30000 + n),
        "part_id": str(33000 + n),
        "part_key": f"/library/parts/{33000 + n}/file.mkv",
        "part_size_bytes": 1_220_000,
        "duration_ms": DURATION,
        "audio_stream_id": str(110000 + n),
    }


def _item(n: int, title: str, status: str = "missing", freshness: str = "current", ranges=None) -> dict:
    return {
        "id": f"v{n}",
        "server_id": "s",
        "title": title,
        "show_title": "Community",
        "season": 1,
        "episode": n,
        "duration_ms": DURATION,
        "analysis_status": status,
        "freshness": freshness,
        "identities": [_identity(n)],
        "ranges": ranges,
        "reasons": [],
    }


def _range(rid: str, kind: str, action: str, start: int, end: int, status: str = "proposed") -> dict:
    return {
        "id": rid,
        "kind": kind,
        "action": action,
        "start_ms": start,
        "end_ms": end,
        "review_status": status,
        "locked": False,
        "origin": "detector",
        "reason_code": "native_signal_match",
        "reason": "Picture and sound repeat in 3 other episodes of this season.",
        "evidence_ids": [],
        "audio_stream_ids": [],
    }


class RepriseApi:
    """The Reprise API as the Library page sees it, with a few episodes in every analysis state."""

    def __init__(self, page: Page) -> None:
        found_ranges = [
            _range("r-credits", "credits", "skip", 1_240_000, DURATION),
            _range("r-keep", "credits", "keep", 1_250_000, 1_262_000),
            _range("r-intro", "intro", "skip", 0, 32_000),
        ]
        self.items = [
            _item(1, "Pilot", "complete", ranges={"skip": 2, "keep": 1, "proposed": 3, "approved": 0, "to_review": 2}),
            _item(
                2,
                "Spanish 101",
                "complete",
                ranges={"skip": 0, "keep": 0, "proposed": 0, "approved": 0, "to_review": 0},
            ),
            _item(3, "Social Psychology", "failed"),
            _item(4, "Advanced Criminal Law"),
            _item(5, "Anthropology 101"),
            _item(6, "Accounting for Lawyers", freshness="stale"),
        ]
        base = {
            "schema_version": 1,
            "resolution": "resolved",
            "timebase": "file_presentation_ms",
            "duration_ms": DURATION,
            "evidence": [],
        }
        self.results = {
            "v1": {
                **base,
                "analysis_status": "complete",
                "freshness": "current",
                "result_revision": 3,
                "reasons": ["unapproved_or_unverified"],
                "ranges": found_ranges,
                "evidence": [
                    {
                        "id": "e1",
                        "start_ms": 0,
                        "end_ms": 32_000,
                        "modality": "audiovisual",
                        "verdict": "match",
                        "reason_code": "native_signal_match",
                        "reason": "Matches episode 2.",
                        "reference_version_id": "v2",
                        "reference_start_ms": 0,
                        "reference_end_ms": 32_000,
                        "metrics": {},
                    }
                ],
            },
            "v2": {
                **base,
                "analysis_status": "complete",
                "freshness": "current",
                "result_revision": 2,
                "ranges": [],
                "reasons": [
                    "Compared with 6 other episodes of this season, but no repeating intro or credits could be confirmed.",
                    "Plex has no intro marker for this episode, so only its first 90 seconds were checked.",
                ],
            },
            "v3": {
                **base,
                "analysis_status": "failed",
                "freshness": "current",
                "result_revision": 1,
                "ranges": [],
                "reasons": [
                    "Reprise can't compare this file with other episodes, so the analysis stopped.",
                    "audio timestamps drift by 1 ms",
                ],
            },
            "v4": {
                **base,
                "analysis_status": "missing",
                "freshness": "current",
                "result_revision": None,
                "reasons": [],
            },
            "v5": {
                **base,
                "analysis_status": "missing",
                "freshness": "current",
                "result_revision": None,
                "reasons": [],
            },
            "v6": {
                **base,
                "analysis_status": "missing",
                "freshness": "stale",
                "result_revision": None,
                "reasons": [
                    "This file changed on disk after the library was last refreshed. Refresh the library, then analyze it again."
                ],
            },
        }
        for n in range(1, 7):
            self.results[f"v{n}"]["identities"] = [_identity(n)]
        self.jobs: list[dict] = []
        self.analyses: list[dict] = []
        self.reviews: list[dict] = []
        self.refreshes: list[dict] = []
        self.settings: dict = {
            "publication_available": False,
            "writes_enabled": False,
            "online_lookups": True,
            "acceleration": {
                "enabled": True,
                "active": True,
                "label": "GPU: NVIDIA Tesla P40 (NVDEC decode)",
                "devices": [],
                "windows": {},
                "last_problem": None,
            },
        }
        self.frames: list[str] = []
        self.settings_patches: list[dict] = []
        self.libraries = [
            {"server_id": "s", "server_type": "plex", "server_name": "Home Plex", "id": 1, "name": "TV Shows"}
        ]
        page.route(re.compile(r".*/api/(reprise/|libraries).*"), self._handle)

    def start_analysis(self, ids: list[str], percent: float = 40.0) -> dict:
        job = {
            "id": "job-1",
            "status": "running",
            "operation": "analysis",
            "created_at": "2026-11-03T12:00:00+00:00",
            "pause_requested": False,
            "error": None,
            "media_version_ids": ids,
            "message": None,
            "progress": {"percent": percent, "current_item": "Checking the intro: comparing with episode 2 of 6"},
        }
        self.jobs.insert(0, job)
        return job

    def finish_analysis(self) -> None:
        job = self.jobs[0]
        job.update(
            status="completed",
            progress={"percent": 100.0, "current_item": ""},
            outcome={
                "state": "nothing_found",
                "message": "Finished: nothing repeating was confirmed in 2 episodes.",
                "per_media": [],
            },
        )
        for item in self.items:
            if item["id"] in job["media_version_ids"]:
                item.update(
                    analysis_status="complete",
                    ranges={"skip": 0, "keep": 0, "proposed": 0, "approved": 0, "to_review": 0},
                )
                self.results[item["id"]].update(
                    analysis_status="complete",
                    result_revision=1,
                    ranges=[],
                    reasons=[
                        "Compared with 6 other episodes of this season, but no repeating intro or credits could be confirmed."
                    ],
                )

    def list_media(self, query: dict) -> dict:
        """The list as the real service answers it: a show is a substring, a season is exact, a search looks in the title and the show."""
        show, season, search = ((query.get(key) or [""])[0].lower() for key in ("show", "season", "search"))
        items = [
            item
            for item in self.items
            if show in item["show_title"].lower()
            and (not season or str(item["season"]) == season)
            and search in f"{item['title']} {item['show_title']}".lower()
        ]
        shows, seasons = sorted({i["show_title"] for i in self.items}), sorted({i["season"] for i in self.items})
        return {"items": copy.deepcopy(items), "next_cursor": None, "shows": shows, "seasons": seasons}

    def _handle(self, route: Route) -> None:
        request = route.request
        path, method = urlparse(request.url).path, request.method
        body = json.loads(request.post_data) if request.post_data else {}
        match = re.fullmatch(r"/api/reprise/media/([^/]+)/(result|stream|frame|poster|review)", path)
        if path == "/api/reprise/media":
            return _fulfill_json(route, self.list_media(parse_qs(urlparse(request.url).query)))
        if match and match[2] == "result":
            return _fulfill_json(route, copy.deepcopy(self.results[match[1]]))
        if match and match[2] == "stream":
            return route.fulfill(status=404, body="")
        if match and match[2] == "frame":
            self.frames.append(request.url)
            return route.fulfill(content_type="image/svg+xml", body=TEST_IMAGE)
        if match and match[2] == "poster":
            return route.fulfill(content_type="image/svg+xml", body=TEST_IMAGE)
        if match and match[2] == "review" and method == "PATCH":
            self.reviews.append(body)
            return _fulfill_json(route, self.results[match[1]])
        if path == "/api/reprise/jobs":
            return _fulfill_json(route, {"items": copy.deepcopy(self.jobs)})
        if path == "/api/reprise/analyses" and method == "POST":
            self.analyses.append(body)
            return _fulfill_json(route, self.start_analysis(body["media_version_ids"], percent=0.0), 202)
        if path == "/api/reprise/refresh" and method == "POST":
            self.refreshes.append(body)
            job = {
                "id": "job-r",
                "status": "running",
                "operation": "refresh",
                "created_at": "2026-11-03T12:01:00+00:00",
                "pause_requested": False,
                "error": None,
                "progress": {"percent": 0.0, "current_item": ""},
            }
            self.jobs.insert(0, job)
            return _fulfill_json(route, job, 202)
        if path == "/api/reprise/settings":
            if method == "PATCH":
                self.settings_patches.append(body)
                if "writes_enabled" in body:
                    self.settings["writes_enabled"] = body["writes_enabled"]
                if "online_lookups" in body:
                    self.settings["online_lookups"] = body["online_lookups"]
                for key in ("auto_send", "auto_send_endings", "auto_send_intros", "backfill", "alerts"):
                    if key in body and self.settings.get("autopilot"):
                        self.settings["autopilot"][key] = body[key]
                if "gpu_decode" in body:
                    on = body["gpu_decode"]
                    self.settings["acceleration"].update(
                        enabled=on,
                        active=on,
                        label="GPU: NVIDIA Tesla P40 (NVDEC decode)" if on else "CPU (GPU decode is turned off)",
                    )
            return _fulfill_json(route, self.settings)
        if path == "/api/libraries":
            return _fulfill_json(route, {"libraries": self.libraries})
        if path in ("/api/reprise/clients", "/api/reprise/publications"):
            return _fulfill_json(route, {"items": []})
        return route.continue_()


@pytest.fixture
def api(authed_page: Page) -> RepriseApi:
    return RepriseApi(authed_page)


def _open(page: Page, app_url: str) -> None:
    page.goto(f"{app_url}/reprise")
    expect(page.locator(".rp-episode-open").first).to_be_visible()


@pytest.mark.e2e
class TestEpisodeList:
    def test_rows_say_what_happened_in_plain_words(self, authed_page: Page, api: RepriseApi, app_url: str) -> None:
        _open(authed_page, app_url)
        rows = authed_page.locator(".rp-episode-open")
        expect(rows).to_have_count(6)
        expect(rows.nth(0)).to_contain_text("Pilot")
        expect(authed_page.locator(".rp-season-head h3")).to_have_text("Community · Season 1")
        expect(rows.nth(0).locator(".rp-episode-context")).to_have_text("E1 · 22 min")
        expect(rows.nth(0)).to_have_accessible_name("Community · S1 E1 · Pilot, 2 to review")
        for row, status in zip(
            range(6),
            ["2 to review", "Nothing found", "Analysis failed", "Not analyzed", "Not analyzed", "File changed"],
            strict=True,
        ):
            expect(rows.nth(row).locator(".rp-chip")).to_have_text(status)

    def test_one_lazy_poster_per_season_uses_plex_artwork_not_episode_frames(
        self, authed_page: Page, api: RepriseApi, app_url: str
    ) -> None:
        api.items[1]["poster_url"] = "/api/reprise/media/v2/poster"
        api.items[2]["poster_url"] = "/api/reprise/media/v3/poster"
        _more_episodes(api, 2, show="Andor")
        _open(authed_page, app_url)
        groups = authed_page.locator(".rp-season")
        expect(groups).to_have_count(2)
        poster = groups.filter(has_text="Community · Season 1").locator(".rp-season-poster")
        expect(poster).to_have_count(1)
        expect(poster).to_have_attribute("loading", "lazy")
        expect(poster).to_have_attribute("alt", "")
        expect(poster).to_have_attribute("src", "/api/reprise/media/v2/poster")
        expect(poster).to_have_js_property("naturalWidth", 2)
        expect(groups.filter(has_text="Andor · Season 1").locator(".rp-season-poster")).to_have_count(0)
        assert api.frames == []

    @pytest.mark.parametrize("width", [390, 1440])
    def test_only_the_open_episode_gets_a_frame_and_back_returns_to_its_row(
        self, authed_page: Page, api: RepriseApi, app_url: str, width: int
    ) -> None:
        api.items[1]["ranges"].update(skip=2, proposed=2, suggested=2)
        authed_page.set_viewport_size({"width": width, "height": 900})
        _open(authed_page, app_url)
        pilot = authed_page.locator(".rp-episode-open").first
        expect(pilot).to_be_visible()
        assert api.frames == []
        status = authed_page.locator(".rp-episode-open .rp-chip").nth(1)
        expect(status).to_have_text("2 suggestions")
        line_count = status.evaluate(
            """chip => {
                const text = document.createRange();
                text.selectNodeContents(chip);
                return text.getClientRects().length;
            }"""
        )
        assert line_count == 1
        pilot.click()
        artwork = authed_page.locator("#rpEpisodeStill")
        expect(artwork).to_be_visible()
        assert {urlparse(url).path for url in api.frames} == {"/api/reprise/media/v1/frame"}
        assert parse_qs(urlparse(api.frames[0]).query) == {"t_ms": ["1000"], "w": ["640"]}
        assert authed_page.evaluate("document.documentElement.scrollWidth") <= width
        authed_page.get_by_role("button", name="Back to episodes").click()
        expect(authed_page.locator("#rpReview")).to_be_hidden()
        expect(pilot).to_be_focused()

    def test_technical_identifiers_stay_hidden_until_file_details_is_opened(
        self, authed_page: Page, api: RepriseApi, app_url: str
    ) -> None:
        _open(authed_page, app_url)
        page_text = authed_page.locator("#repriseApp").inner_text()
        for technical in ("30001", "33001", "110001", "Plex item", "machine"):
            assert technical not in page_text
        authed_page.locator(".rp-episode-open").first.click()
        expect(authed_page.locator("#rpReviewHeading")).to_have_text("Pilot")
        assert authed_page.locator("#rpFileDetails").evaluate("e => e.open") is False
        assert "30001" not in authed_page.locator("#rpReview").inner_text()
        authed_page.locator("#rpFileDetails summary").click()
        expect(authed_page.locator("#rpFacts")).to_contain_text("30001 / 33001 / 110001")

    def test_selecting_episodes_offers_one_clear_next_step(
        self, authed_page: Page, api: RepriseApi, app_url: str
    ) -> None:
        _open(authed_page, app_url)
        expect(authed_page.locator("#rpSelectBar")).to_be_hidden()
        authed_page.locator(".rp-episode input[type=checkbox]").nth(3).check()
        expect(authed_page.locator("#rpAnalyze")).to_have_text("Analyze episode")
        authed_page.locator(".rp-episode input[type=checkbox]").nth(4).check()
        expect(authed_page.locator("#rpSelectCount")).to_have_text("2 episodes selected")
        expect(authed_page.locator("#rpAnalyze")).to_have_text("Analyze 2 episodes")
        authed_page.locator("#rpSelectClear").click()
        expect(authed_page.locator("#rpSelectBar")).to_be_hidden()

    def test_empty_library_points_to_connections_when_nothing_is_connected(
        self, authed_page: Page, api: RepriseApi, app_url: str
    ) -> None:
        api.items, api.libraries = [], []
        authed_page.goto(f"{app_url}/reprise")
        state = authed_page.locator("#rpLibraryState")
        expect(state).to_contain_text("No episodes yet")
        expect(state.get_by_role("link", name="Open Connections")).to_have_attribute("href", "/servers")

    def test_empty_library_offers_a_refresh_when_a_library_is_connected(
        self, authed_page: Page, api: RepriseApi, app_url: str
    ) -> None:
        api.items = []
        authed_page.goto(f"{app_url}/reprise")
        authed_page.locator("#rpLibraryState").get_by_role("button", name="Refresh library").click()
        expect(authed_page.locator("#rpMessage")).to_contain_text("Refreshing TV Shows")
        assert api.refreshes == [{"server_id": "s", "library_id": "1"}]


@pytest.mark.e2e
class TestReviewingAnEpisode:
    @pytest.mark.parametrize(
        ("row", "headline", "button"),
        [
            (3, "This episode hasn't been analyzed yet.", "Analyze"),
            (2, "The analysis could not finish.", "Try again"),
            (1, "No repeated intro or credits found.", "Analyze again"),
        ],
    )
    def test_each_outcome_is_explained_with_a_next_step(
        self, authed_page: Page, api: RepriseApi, app_url: str, row: int, headline: str, button: str
    ) -> None:
        _open(authed_page, app_url)
        authed_page.locator(".rp-episode-open").nth(row).click()
        outcome = authed_page.locator("#rpOutcome")
        expect(outcome).to_contain_text(headline)
        expect(authed_page.locator("#rpAnalyzeOne")).to_have_text(button)

    def test_a_failed_analysis_shows_the_reason_and_hides_the_technical_line(
        self, authed_page: Page, api: RepriseApi, app_url: str
    ) -> None:
        _open(authed_page, app_url)
        authed_page.locator(".rp-episode-open").nth(2).click()
        outcome = authed_page.locator("#rpOutcome")
        expect(outcome).to_contain_text("Reprise can't compare this file with other episodes, so the analysis stopped.")
        technical = outcome.get_by_text("audio timestamps drift by 1 ms")
        expect(technical).to_be_hidden()
        outcome.get_by_text("Details").click()
        expect(technical).to_be_visible()

    def test_nothing_found_says_what_was_compared(self, authed_page: Page, api: RepriseApi, app_url: str) -> None:
        _open(authed_page, app_url)
        authed_page.locator(".rp-episode-open").nth(1).click()
        outcome = authed_page.locator("#rpOutcome")
        expect(outcome).to_contain_text("Compared with 6 other episodes of this season")
        expect(outcome).to_contain_text("only its first 90 seconds were checked")

    def test_a_changed_file_asks_for_a_refresh_instead_of_offering_analyze(
        self, authed_page: Page, api: RepriseApi, app_url: str
    ) -> None:
        _open(authed_page, app_url)
        authed_page.locator(".rp-episode-open").nth(5).click()
        expect(authed_page.locator("#rpOutcome")).to_contain_text("This file has changed since Reprise last read it.")
        expect(authed_page.locator("#rpOutcome").get_by_role("button", name="Refresh library")).to_be_visible()
        expect(authed_page.locator("#rpAnalyzeOne")).to_be_hidden()

    def test_findings_are_listed_in_time_order_with_decisions_after_the_summary(
        self, authed_page: Page, api: RepriseApi, app_url: str
    ) -> None:
        _open(authed_page, app_url)
        authed_page.locator(".rp-episode-open").first.click()
        expect(authed_page.locator("#rpOutcome")).to_contain_text(
            "Reprise found 2 sections it can skip and 1 protected scene."
        )
        names = authed_page.locator(".rp-range-name")
        expect(names).to_have_text(["Skip intro", "Skip credits", "Keep credits"])
        timeline = authed_page.locator("#rpTimeline")
        expect(timeline.locator(".rp-timeline-segment.suggested")).to_have_count(2)
        expect(timeline.locator(".rp-timeline-segment.skip")).to_have_count(0)
        expect(timeline.locator(".rp-timeline-segment.keep")).to_have_count(1)
        expect(authed_page.locator("#rpSaveBar")).to_be_hidden()

    def test_approving_a_section_and_saving_sends_only_that_decision(
        self, authed_page: Page, api: RepriseApi, app_url: str
    ) -> None:
        _open(authed_page, app_url)
        authed_page.locator(".rp-episode-open").first.click()
        first = authed_page.locator(".rp-range").first
        first.locator(":scope > summary").click()
        first.locator("label[data-value=approved]").click()
        expect(authed_page.locator("#rpTimeline .rp-timeline-segment.skip")).to_have_count(1)
        expect(authed_page.locator("#rpSaveBar")).to_be_visible()
        expect(authed_page.locator("#rpSaveState")).to_have_text("You have unsaved changes.")
        authed_page.locator("#rpSave").click()
        expect(authed_page.locator("#rpMessage")).to_have_text("Review saved. Nothing was sent to Plex.")
        edits = {edit["range_id"]: edit["review_status"] for edit in api.reviews[0]["edits"]}
        assert api.reviews[0]["expected_revision"] == 3
        assert edits == {"r-intro": "approved", "r-keep": "proposed", "r-credits": "proposed"}


def _plex(rid: str, kind: str, start: int, end: int, status: str = "proposed") -> dict:
    return {
        **_range(rid, kind, "skip", start, end, status),
        "origin": "plex",
        "reason_code": "plex_suggestion",
        "reason": "Suggested by Plex — not confirmed by Reprise.",
        "suggested_by": ["Plex"],
    }


def _online(rid: str, kind: str, start: int, end: int, status: str = "proposed") -> dict:
    return {
        **_range(rid, kind, "skip", start, end, status),
        "origin": "online",
        "reason_code": "online_suggestion",
        "reason": "Suggested by IntroDB — not confirmed by Reprise.",
        "suggested_by": ["IntroDB"],
    }


def _suggest(api: RepriseApi, *ranges: dict, reasons: tuple[str, ...] = ()) -> None:
    """Spanish 101 now holds only what Plex or an online database suggested."""
    open_ranges = [r for r in ranges if r["review_status"] == "proposed"]
    api.items[1]["ranges"] = {
        "skip": len(ranges),
        "keep": 0,
        "proposed": len(open_ranges),
        "approved": len(ranges) - len(open_ranges),
        "to_review": 0,
        "suggested": len(open_ranges),
        "suggested_by": list(dict.fromkeys(name for r in open_ranges for name in r["suggested_by"])),
    }
    api.results["v2"].update(result_revision=4, ranges=list(ranges), reasons=list(reasons))


@pytest.mark.e2e
class TestPlexSuggestions:
    def test_a_plex_suggestion_is_counted_and_badged_apart_from_what_reprise_found(
        self, authed_page: Page, api: RepriseApi, app_url: str
    ) -> None:
        _suggest(api, _plex("p-intro", "intro", 0, 45_000), _plex("p-credits", "credits", 1_200_000, DURATION))
        _open(authed_page, app_url)
        expect(authed_page.locator(".rp-episode-open").nth(1).locator(".rp-chip")).to_have_text("2 Plex suggestions")
        authed_page.locator(".rp-episode-open").nth(1).click()
        expect(authed_page.locator("#rpOutcome")).to_contain_text(
            "Plex suggested 2 sections to skip, but Reprise could not confirm them."
        )
        expect(authed_page.locator(".rp-range .rp-chip", has_text="Plex suggestion")).to_have_count(2)

    def test_approving_a_plex_suggestion_asks_first_and_sends_the_acknowledgement_for_that_range_only(
        self, authed_page: Page, api: RepriseApi, app_url: str
    ) -> None:
        _suggest(api, _plex("p-intro", "intro", 0, 45_000), _plex("p-credits", "credits", 1_200_000, DURATION))
        _open(authed_page, app_url)
        authed_page.locator(".rp-episode-open").nth(1).click()
        first = authed_page.locator(".rp-range").first
        first.locator(":scope > summary").click()
        first.locator("label[data-value=approved]").click()
        expect(first.locator(".rp-plex-confirm")).to_contain_text("Plex found this, Reprise could not confirm it.")
        expect(authed_page.locator("#rpSaveBar")).to_be_hidden()
        first.get_by_role("button", name="Approve it").click()
        expect(first.locator(".rp-chip", has_text="Plex suggestion · approved by you")).to_be_visible()
        authed_page.locator("#rpSave").click()
        expect(authed_page.locator("#rpMessage")).to_have_text("Review saved. Nothing was sent to Plex.")
        edits = {edit["range_id"]: edit for edit in api.reviews[0]["edits"]}
        assert edits["p-intro"]["review_status"] == "approved"
        assert edits["p-intro"]["acknowledge_suggestion"] is True
        assert "acknowledge_suggestion" not in edits["p-credits"]
        assert all("acknowledge_plex_suggestion" not in edit for edit in edits.values())

    def test_a_failed_comparison_still_shows_plex_suggestions_with_the_plain_headline(
        self, authed_page: Page, api: RepriseApi, app_url: str
    ) -> None:
        headline = "Reprise can't compare this episode with the others, so it couldn't confirm anything."
        technical = "Native audio timestamps are discontinuous"
        _suggest(api, _plex("p-intro", "intro", 0, 45_000), reasons=(headline, technical))
        _open(authed_page, app_url)
        expect(authed_page.locator(".rp-episode-open").nth(1).locator(".rp-chip")).to_have_text("1 Plex suggestion")
        authed_page.locator(".rp-episode-open").nth(1).click()
        outcome = authed_page.locator("#rpOutcome")
        expect(outcome).to_contain_text("Plex suggested 1 section to skip, but Reprise could not confirm it.")
        expect(outcome).to_contain_text(headline)
        hidden_line = outcome.get_by_text(technical)
        expect(hidden_line).to_be_hidden()
        outcome.get_by_text("Details").click()
        expect(hidden_line).to_be_visible()

    def test_not_now_leaves_a_plex_suggestion_undecided(self, authed_page: Page, api: RepriseApi, app_url: str) -> None:
        _suggest(api, _plex("p-intro", "intro", 0, 45_000))
        _open(authed_page, app_url)
        authed_page.locator(".rp-episode-open").nth(1).click()
        range_box = authed_page.locator(".rp-range").first
        range_box.locator(":scope > summary").click()
        range_box.locator("label[data-value=approved]").click()
        range_box.get_by_role("button", name="Not now").click()
        expect(range_box.locator(".rp-plex-confirm")).to_be_hidden()
        expect(range_box.locator("input[value=proposed]")).to_be_checked()
        expect(authed_page.locator("#rpSaveBar")).to_be_hidden()

    def test_an_already_approved_plex_range_is_resent_without_the_flag_when_untouched(
        self, authed_page: Page, api: RepriseApi, app_url: str
    ) -> None:
        _suggest(
            api, _plex("p-intro", "intro", 0, 45_000, "approved"), _plex("p-credits", "credits", 1_200_000, DURATION)
        )
        _open(authed_page, app_url)
        authed_page.locator(".rp-episode-open").nth(1).click()
        expect(authed_page.locator(".rp-range .rp-chip", has_text="Plex suggestion · approved by you")).to_have_count(1)
        second = authed_page.locator(".rp-range").nth(1)
        second.locator(":scope > summary").click()
        second.locator("label[data-value=rejected]").click()
        authed_page.locator("#rpSave").click()
        expect(authed_page.locator("#rpMessage")).to_have_text("Review saved. Nothing was sent to Plex.")
        edits = {edit["range_id"]: edit for edit in api.reviews[0]["edits"]}
        assert edits["p-credits"]["review_status"] == "rejected"
        assert all("acknowledge_suggestion" not in edit for edit in edits.values())

    def test_an_online_suggestion_is_named_asks_first_and_sends_the_acknowledgement(
        self, authed_page: Page, api: RepriseApi, app_url: str
    ) -> None:
        _suggest(api, _online("o-intro", "intro", 0, 45_000))
        _open(authed_page, app_url)
        expect(authed_page.locator(".rp-episode-open").nth(1).locator(".rp-chip")).to_have_text("1 suggestion")
        authed_page.locator(".rp-episode-open").nth(1).click()
        expect(authed_page.locator("#rpOutcome")).to_contain_text(
            "IntroDB suggested 1 section to skip, but Reprise could not confirm it."
        )
        box = authed_page.locator(".rp-range").first
        expect(box.locator(".rp-chip", has_text="IntroDB suggestion")).to_have_count(1)
        box.locator(":scope > summary").click()
        box.locator("label[data-value=approved]").click()
        expect(box.locator(".rp-plex-confirm")).to_contain_text(
            "IntroDB found this, Reprise could not confirm it. Approve it only if it is right."
        )
        box.get_by_role("button", name="Approve it").click()
        expect(box.locator(".rp-chip", has_text="IntroDB suggestion · approved by you")).to_be_visible()
        authed_page.locator("#rpSave").click()
        expect(authed_page.locator("#rpMessage")).to_have_text("Review saved. Nothing was sent to Plex.")
        edits = {edit["range_id"]: edit for edit in api.reviews[0]["edits"]}
        assert edits["o-intro"]["acknowledge_suggestion"] is True


def _owned(rid: str, kind: str, start: int, end: int, status: str = "approved") -> dict:
    return {
        **_range(rid, kind, "skip", start, end, status),
        "locked": True,
        "origin": "owner",
        "reason_code": "owner_span",
        "reason": "Set by you",
    }


def _open_pilot(page: Page, app_url: str) -> None:
    _open(page, app_url)
    page.locator(".rp-episode-open").first.click()
    expect(page.locator("#rpSetSkip")).to_be_hidden()
    page.locator("#rpEditRanges > summary").click()
    expect(page.locator("#rpSetSkip")).to_be_enabled()


def _set_skip(page: Page, kind: str, start: int, end: int) -> None:
    page.locator("#rpSetSkip").click()
    page.locator("#rpSpanKind").select_option(kind)
    page.locator("#rpSpanStart").fill(str(start))
    page.locator("#rpSpanEnd").fill(str(end))
    page.get_by_role("button", name="Add this skip").click()


@pytest.mark.e2e
class TestSettingASkipExactly:
    def test_the_button_opens_the_form_and_cancel_closes_it(
        self, authed_page: Page, api: RepriseApi, app_url: str
    ) -> None:
        _open_pilot(authed_page, app_url)
        form, button = authed_page.locator("#rpSpanForm"), authed_page.locator("#rpSetSkip")
        expect(form).to_be_hidden()
        button.click()
        expect(form).to_be_visible()
        expect(button).to_have_attribute("aria-expanded", "true")
        expect(authed_page.locator("#rpSpanStart")).to_be_focused()
        form.get_by_role("button", name="Cancel").click()
        expect(form).to_be_hidden()
        expect(button).to_have_attribute("aria-expanded", "false")
        expect(button).to_be_focused()

    def test_a_skip_set_exactly_replaces_the_others_of_its_type_and_is_sent_with_the_yes(
        self, authed_page: Page, api: RepriseApi, app_url: str
    ) -> None:
        _open_pilot(authed_page, app_url)
        _set_skip(authed_page, "intro", 5_000, 40_000)
        expect(authed_page.locator("#rpSpanForm")).to_be_hidden()
        expect(authed_page.locator("#rpOutcome")).to_contain_text("You set the intro skip yourself.")
        expect(authed_page.locator(".rp-range .rp-chip", has_text="Set by you")).to_have_count(1)
        expect(authed_page.locator(".rp-range .rp-chip", has_text="Replaced by your skip")).to_have_count(1)
        expect(authed_page.locator("#rpSaveBar")).to_be_visible()
        authed_page.locator("#rpSave").click()
        expect(authed_page.locator("#rpMessage")).to_have_text("Review saved. Nothing was sent to Plex.")
        edits = api.reviews[0]["edits"]
        assert [edit for edit in edits if edit["range_id"] is None] == [
            {
                "range_id": None,
                "kind": "intro",
                "start_ms": 5_000,
                "end_ms": 40_000,
                "review_status": "approved",
                "acknowledge_owner_span": True,
            }
        ]
        others = {edit["range_id"]: edit for edit in edits if edit["range_id"]}
        assert {rid: edit["review_status"] for rid, edit in others.items()} == {
            "r-intro": "proposed",
            "r-keep": "proposed",
            "r-credits": "proposed",
        }
        assert all("acknowledge_owner_span" not in edit for edit in others.values())

    def test_both_types_can_be_set_and_a_second_skip_of_a_type_replaces_the_first(
        self, authed_page: Page, api: RepriseApi, app_url: str
    ) -> None:
        _open_pilot(authed_page, app_url)
        _set_skip(authed_page, "intro", 5_000, 40_000)
        _set_skip(authed_page, "intro", 6_000, 41_000)
        _set_skip(authed_page, "credits", 1_200_000, 1_300_000)
        expect(authed_page.locator("#rpOutcome")).to_contain_text(
            "You set the intro skip and the credits skip yourself."
        )
        expect(authed_page.locator(".rp-range .rp-chip", has_text="Set by you")).to_have_count(2)
        authed_page.locator("#rpSave").click()
        expect(authed_page.locator("#rpMessage")).to_have_text("Review saved. Nothing was sent to Plex.")
        new = [(e["kind"], e["start_ms"], e["end_ms"]) for e in api.reviews[0]["edits"] if e["range_id"] is None]
        assert new == [("intro", 6_000, 41_000), ("credits", 1_200_000, 1_300_000)]

    @pytest.mark.parametrize(("start", "end"), [(40_000, 5_000), (5_000, 5_000), (5_000, DURATION + 1)])
    def test_times_that_do_not_fit_the_episode_are_refused_in_words_and_nothing_is_added(
        self, authed_page: Page, api: RepriseApi, app_url: str, start: int, end: int
    ) -> None:
        _open_pilot(authed_page, app_url)
        _set_skip(authed_page, "intro", start, end)
        expect(authed_page.locator("#rpMessage")).to_have_text(
            "The skip needs a start before its end, inside the episode."
        )
        expect(authed_page.locator("#rpSpanForm")).to_be_visible()
        expect(authed_page.locator("#rpSaveBar")).to_be_hidden()
        expect(authed_page.locator(".rp-range-name")).to_have_count(3)

    def test_remove_takes_back_a_skip_that_is_not_saved_yet(
        self, authed_page: Page, api: RepriseApi, app_url: str
    ) -> None:
        _open_pilot(authed_page, app_url)
        _set_skip(authed_page, "intro", 5_000, 40_000)
        expect(authed_page.locator(".rp-range-name")).to_have_count(4)
        expect(authed_page.locator("input[name^=decision-]")).to_have_count(9)  # the new skip has nothing to decide
        authed_page.locator(".rp-range").last.get_by_role("button", name="Remove").click()
        expect(authed_page.locator(".rp-range-name")).to_have_count(3)
        expect(authed_page.locator(".rp-range .rp-chip", has_text="Replaced by your skip")).to_have_count(0)
        expect(authed_page.locator("#rpOutcome")).to_contain_text("Reprise found 2 sections it can skip")

    def test_a_saved_skip_is_badged_and_leaves_only_approve_and_reject(
        self, authed_page: Page, api: RepriseApi, app_url: str
    ) -> None:
        api.results["v1"]["ranges"].append(_owned("o-intro", "intro", 5_000, 40_000))
        _open_pilot(authed_page, app_url)
        expect(authed_page.locator("#rpOutcome")).to_contain_text("You set the intro skip yourself.")
        expect(authed_page.locator(".rp-range .rp-chip", has_text="Replaced by your skip")).to_have_count(1)
        box = authed_page.locator(".rp-range", has=authed_page.locator(".rp-chip", has_text="Set by you"))
        expect(box).to_have_count(1)
        box.locator(":scope > summary").click()
        expect(box).to_contain_text("You set this skip yourself.")
        expect(box.locator("label[data-value]")).to_have_text(["Approve", "Reject"])
        box.locator(".rp-fine > summary").click()
        expect(box.get_by_label("What to do")).to_have_count(0)
        expect(box.get_by_label("Lock this section so a new analysis never changes it")).to_have_count(0)
        expect(box.get_by_label("Type").locator("option")).to_have_text(["Intro", "Credits"])

    def test_rejecting_a_saved_skip_hands_its_type_back_and_sends_no_yes(
        self, authed_page: Page, api: RepriseApi, app_url: str
    ) -> None:
        api.results["v1"]["ranges"].append(_owned("o-intro", "intro", 5_000, 40_000))
        _open_pilot(authed_page, app_url)
        box = authed_page.locator(".rp-range", has=authed_page.locator(".rp-chip", has_text="Set by you"))
        box.locator(":scope > summary").click()
        box.locator("label[data-value=rejected]").click()
        expect(authed_page.locator("#rpOutcome")).not_to_contain_text("You set the intro skip yourself.")
        expect(authed_page.locator(".rp-range .rp-chip", has_text="Replaced by your skip")).to_have_count(0)
        authed_page.locator("#rpSave").click()
        expect(authed_page.locator("#rpMessage")).to_have_text("Review saved. Nothing was sent to Plex.")
        sent = {edit["range_id"]: edit for edit in api.reviews[0]["edits"]}
        assert sent["o-intro"]["review_status"] == "rejected"
        assert "acknowledge_owner_span" not in sent["o-intro"]

    def test_a_saved_skip_is_sent_again_with_the_yes_when_something_else_is_saved(
        self, authed_page: Page, api: RepriseApi, app_url: str
    ) -> None:
        api.results["v1"]["ranges"].append(_owned("o-intro", "intro", 5_000, 40_000))
        _open_pilot(authed_page, app_url)
        credits = authed_page.locator(".rp-range", has_text="Skip credits")
        credits.locator(":scope > summary").click()
        credits.locator("label[data-value=rejected]").click()
        authed_page.locator("#rpSave").click()
        expect(authed_page.locator("#rpMessage")).to_have_text("Review saved. Nothing was sent to Plex.")
        sent = {edit["range_id"]: edit for edit in api.reviews[0]["edits"]}
        assert sent["o-intro"] == {
            "range_id": "o-intro",
            "action": "skip",
            "kind": "intro",
            "start_ms": 5_000,
            "end_ms": 40_000,
            "review_status": "approved",
            "locked": True,
            "reason": "Set by you",
            "acknowledge_owner_span": True,
        }

    def test_a_changed_file_cannot_have_a_skip_set(self, authed_page: Page, api: RepriseApi, app_url: str) -> None:
        _open(authed_page, app_url)
        authed_page.locator(".rp-episode-open").nth(5).click()
        expect(authed_page.locator("#rpOutcome")).to_contain_text("This file has changed since Reprise last read it.")
        expect(authed_page.locator("#rpSetSkip")).to_be_disabled()
        expect(authed_page.locator("#rpEditRanges")).to_be_hidden()

    def test_fix_link_previews_typed_boundaries_without_saving(
        self, authed_page: Page, api: RepriseApi, app_url: str
    ) -> None:
        requests: list[str] = []
        clip = Path(__file__).resolve().parents[2] / "media_preview_generator/gpu/assets/test_video.mp4"

        def preview(route: Route) -> None:
            requests.append(route.request.url)
            route.fulfill(path=clip, content_type="video/mp4")

        authed_page.route(re.compile(r"/jump-preview\?"), preview)
        authed_page.goto(f"{app_url}/reprise?show=Community&season=1&open=v1&kind=credits#fix")
        expect(authed_page.locator("#rpSpanKind")).to_have_value("credits")
        expect(authed_page.locator("#rpSpanStart")).to_have_value("1240000")
        expect(authed_page.locator("#rpSpanEnd")).to_have_value(str(DURATION))
        video = authed_page.locator("#rpJumpVideo")
        expect(video).to_be_hidden()
        assert requests == []
        authed_page.locator("#rpSpanStart").fill("1263000")
        authed_page.locator("#rpSpanJump").click()
        expect(video).to_be_visible()
        expect(video).to_have_js_property("controls", True)
        assert f"/media/v1/jump-preview?start_ms=1263000&end_ms={DURATION}" in requests[0]
        authed_page.locator("#rpSpanEnd").fill("1319000")
        expect(video).to_be_hidden()
        expect(video).not_to_have_attribute("src", re.compile("."))
        expect(authed_page.locator("#rpSaveBar")).to_be_hidden()
        assert api.reviews == []

    def test_jump_for_a_missing_skip_keeps_editing_available_if_the_preview_fails(
        self, authed_page: Page, api: RepriseApi, app_url: str
    ) -> None:
        requests: list[str] = []

        def unavailable(route: Route) -> None:
            requests.append(route.request.url)
            route.fulfill(status=503)

        authed_page.route(re.compile(r"/jump-preview\?"), unavailable)
        authed_page.goto(f"{app_url}/reprise?show=Community&season=1&open=v2#fix")
        expect(authed_page.locator("#rpSpanStart")).to_have_value("")
        expect(authed_page.locator("#rpSpanEnd")).to_have_value("")
        authed_page.locator("#rpSpanJump").click()
        expect(authed_page.locator("#rpJumpStatus")).to_contain_text("Enter a start and end")
        assert requests == []
        authed_page.locator("#rpSpanStart").fill("5000")
        authed_page.locator("#rpSpanEnd").fill("40000")
        authed_page.locator("#rpSpanJump").click()
        expect(authed_page.locator("#rpJumpStatus")).to_contain_text("Jump preview unavailable.")
        expect(authed_page.locator("#rpJumpVideo")).to_be_hidden()
        assert "/media/v2/jump-preview?start_ms=5000&end_ms=40000" in requests[0]
        expect(authed_page.locator("#rpSpanStart")).to_be_editable()
        assert api.reviews == []

    def test_the_form_fits_a_phone(self, authed_page: Page, api: RepriseApi, app_url: str) -> None:
        authed_page.set_viewport_size({"width": 390, "height": 844})
        _open_pilot(authed_page, app_url)
        authed_page.locator("#rpSetSkip").click()
        expect(authed_page.locator("#rpSpanForm")).to_be_visible()
        assert authed_page.evaluate("document.documentElement.scrollWidth") <= 390


@pytest.mark.e2e
class TestAnalysisProgress:
    def test_analyze_selected_shows_progress_and_then_the_result(
        self, authed_page: Page, api: RepriseApi, app_url: str
    ) -> None:
        _open(authed_page, app_url)
        authed_page.locator(".rp-episode input[type=checkbox]").nth(3).check()
        authed_page.locator(".rp-episode input[type=checkbox]").nth(4).check()
        authed_page.locator("#rpAnalyze").click()
        assert api.analyses == [{"media_version_ids": ["v4", "v5"]}]
        summary = authed_page.locator("#rpActivityDetails > summary")
        expect(summary).to_contain_text("1 active")
        expect(authed_page.locator("#rpQueue")).to_be_hidden()
        summary.click()
        expect(authed_page.locator("#notificationBellBtn")).to_have_attribute("aria-expanded", "false")
        expect(authed_page.locator("#rpActivity")).to_be_hidden()
        job = authed_page.locator("#rpLibraryActivity .rp-job").first
        expect(job).to_contain_text("Analyzing 2 episodes")
        expect(authed_page.locator(".rp-episode-open").nth(3).locator(".rp-chip")).to_contain_text("Analyzing")
        pause = job.get_by_role("button", name="Pause")
        pause.focus()
        api.jobs[0]["progress"] = {"percent": 40.0, "current_item": "Checking the intro: comparing with episode 2 of 6"}
        expect(job).to_contain_text("40%", timeout=10_000)
        expect(job).to_contain_text("comparing with episode 2 of 6")
        expect(pause).to_be_focused()
        api.finish_analysis()
        expect(authed_page.locator(".rp-episode-open").nth(3).locator(".rp-chip")).to_have_text(
            "Nothing found", timeout=10_000
        )
        expect(authed_page.locator("#rpLibraryActivity .rp-job").first).to_contain_text(
            "Finished: nothing repeating was confirmed in 2 episodes."
        )

    def test_the_open_episode_shows_its_own_progress_and_picks_up_the_result(
        self, authed_page: Page, api: RepriseApi, app_url: str
    ) -> None:
        api.start_analysis(["v4"], percent=40.0)
        _open(authed_page, app_url)
        authed_page.locator(".rp-episode-open").nth(3).click()
        outcome = authed_page.locator("#rpOutcome")
        expect(outcome).to_contain_text("Analyzing this episode")
        expect(outcome.get_by_role("progressbar")).to_have_attribute("aria-valuenow", "40")
        api.finish_analysis()
        expect(outcome).to_contain_text("No repeated intro or credits found.", timeout=12_000)

    def test_a_refresh_that_could_not_read_episodes_lists_each_with_its_reason_and_is_not_red(
        self, authed_page: Page, api: RepriseApi, app_url: str
    ) -> None:
        api.jobs.insert(
            0,
            {
                "id": "refresh-1",
                "status": "completed",
                "operation": "refresh",
                "created_at": "2026-11-04T19:38:00+00:00",
                "pause_requested": False,
                "error": None,
                "warning": "2 episodes could not be read and kept their earlier entries",
                "message": None,
                "progress": {"percent": 100.0, "current_item": ""},
                "refresh": {
                    "registered": 4249,
                    "skipped": 2,
                    "removed": 124,
                    "skipped_items": [
                        {
                            "episode": "Atlanta S03E08 New Jazz",
                            "reason": "Plex and FFprobe can't read its audio/video streams",
                        },
                        {"episode": "Peep Show S04E05 Holiday", "reason": "Plex needs to scan it again"},
                    ],
                },
            },
        )
        _open(authed_page, app_url)
        authed_page.locator("#rpActivityDetails > summary").click()
        job = authed_page.locator("#rpLibraryActivity .rp-job").first
        expect(job).to_contain_text("Refreshing the library")
        expect(job).to_contain_text("2 episodes could not be read and kept their earlier entries")
        job.locator("details > summary").click()
        warnings = job.locator(".rp-job-warn li")
        expect(warnings).to_have_count(2)
        expect(warnings.first).to_have_text(
            "Atlanta S03E08 New Jazz: Plex and FFprobe can't read its audio/video streams."
        )
        expect(job).not_to_contain_text("Failed")


@pytest.mark.e2e
class TestShellStatus:
    def test_the_sidebar_says_review_only_while_publication_is_off(
        self, authed_page: Page, api: RepriseApi, app_url: str
    ) -> None:
        _open(authed_page, app_url)
        chip = authed_page.locator("#rpSafety")
        expect(chip).to_have_attribute("data-state", "off")
        expect(chip).to_contain_text("Review only")

    def test_the_sidebar_warns_when_publication_is_on(self, authed_page: Page, api: RepriseApi, app_url: str) -> None:
        api.settings["writes_enabled"] = True
        _open(authed_page, app_url)
        chip = authed_page.locator("#rpSafety")
        expect(chip).to_have_attribute("data-state", "on")
        expect(chip).to_contain_text("Sending to Plex is on")

    def test_the_library_says_whether_analysis_uses_the_gpu_and_can_turn_it_off(
        self, authed_page: Page, api: RepriseApi, app_url: str
    ) -> None:
        _open(authed_page, app_url)
        authed_page.locator("#rpLibraryTools > summary").click()
        authed_page.locator("#rpAnalysisSettings > summary").click()
        hardware = authed_page.locator("#rpHardware")
        expect(hardware).to_contain_text("Analysis uses GPU: NVIDIA Tesla P40 (NVDEC decode).")
        authed_page.locator("#rpHardwareToggle").click()
        expect(hardware).to_contain_text("Analysis uses CPU (GPU decode is turned off).")
        expect(authed_page.locator("#rpHardwareToggle")).to_have_text("Use GPU when available")
        assert api.settings_patches == [{"gpu_decode": False}]

    def test_the_library_has_a_switch_for_looking_up_intro_times_online(
        self, authed_page: Page, api: RepriseApi, app_url: str
    ) -> None:
        _open(authed_page, app_url)
        authed_page.locator("#rpLibraryTools > summary").click()
        authed_page.locator("#rpAnalysisSettings > summary").click()
        switch = authed_page.locator("#rpOnlineToggle")
        expect(authed_page.locator("#rpOnline")).to_contain_text("Look up intro times online (IntroDB, SkipDB)")
        expect(switch).to_be_checked()
        switch.uncheck()
        expect(authed_page.locator("#rpMessage")).to_have_text("Intro times will no longer be looked up online.")
        expect(switch).not_to_be_checked()
        assert api.settings_patches == [{"online_lookups": False}]

    def test_the_sharing_page_flips_the_plex_switch_and_updates_the_sidebar(
        self, authed_page: Page, api: RepriseApi, app_url: str
    ) -> None:
        api.settings["publication_available"] = True
        authed_page.goto(f"{app_url}/reprise/sharing")
        switch = authed_page.locator("#rpWritesEnabled")
        expect(switch).to_be_enabled()
        expect(authed_page.locator("#rpWriteState")).to_contain_text("Sending to Plex is off")
        switch.check()
        accept_app_confirm(authed_page)
        expect(authed_page.locator("#rpSafety")).to_have_attribute("data-state", "on")
        assert api.settings_patches == [{"writes_enabled": True}]

    def test_the_endings_switch_is_off_until_turned_on_and_the_intro_switch_is_not_there(
        self, authed_page: Page, api: RepriseApi, app_url: str
    ) -> None:
        api.settings["autopilot"] = {
            "auto_send": True,
            "backfill": True,
            "alerts": True,
            "auto_send_endings": False,
            "status": {"queue_length": 0, "last_reconcile": None, "last_alert": None, "auto_send_blocked": None},
        }
        authed_page.goto(f"{app_url}/reprise/sharing")
        endings = authed_page.locator("#rpAutoSendEndings")
        expect(endings).to_be_visible()
        expect(endings).not_to_be_checked()
        expect(authed_page.locator("#rpAutoSendEndingsHelp")).to_contain_text("sure where the end credits start")
        expect(authed_page.locator("#rpAutoSendIntros")).to_have_count(0)
        endings.check()
        expect(endings).to_be_checked()
        assert api.settings_patches == [{"auto_send_endings": True}]
        endings.uncheck()
        expect(endings).not_to_be_checked()
        assert api.settings_patches[-1] == {"auto_send_endings": False}

    def test_the_intro_switch_appears_only_when_the_server_offers_it(
        self, authed_page: Page, api: RepriseApi, app_url: str
    ) -> None:
        api.settings["autopilot"] = {
            "auto_send": True,
            "backfill": True,
            "alerts": True,
            "auto_send_endings": False,
            "auto_send_intros": False,
            "status": {"queue_length": 0, "last_reconcile": None, "last_alert": None, "auto_send_blocked": None},
        }
        authed_page.goto(f"{app_url}/reprise/sharing")
        intros = authed_page.locator("#rpAutoSendIntros")
        expect(intros).to_be_visible()
        expect(authed_page.get_by_label("Send the sure intros to Plex automatically")).not_to_be_checked()
        intros.check()
        expect(intros).to_be_checked()
        assert api.settings_patches == [{"auto_send_intros": True}]


def _more_episodes(api: RepriseApi, count: int, show: str = "Community", first: int = 7) -> None:
    """Add listed episodes (no results: they are only listed), by default more of the same season."""
    for n in range(first, first + count):
        api.items.append({**_item(n, f"{show} episode {n}"), "show_title": show})


@pytest.mark.e2e
class TestListLayout:
    def test_rows_stay_readable_beside_activity_and_the_selection_bar_in_a_short_window(
        self, authed_page: Page, api: RepriseApi, app_url: str
    ) -> None:
        # A tablet window is about 700 px tall. A running job, finished jobs and ticked rows used to squash the list to
        # nothing and push Activity and "Analyze" out of the pane.
        authed_page.set_viewport_size({"width": 1280, "height": 720})
        _more_episodes(api, 34)
        api.start_analysis(["v4"])
        for n in range(4):
            api.jobs.append(
                {**api.jobs[0], "id": f"old-{n}", "status": "completed", "created_at": f"2026-11-02T0{n}:00:00+00:00"}
            )
        _open(authed_page, app_url)
        authed_page.locator("#rpActivityDetails > summary").click()
        authed_page.locator(".rp-episode-open").first.click()
        for n in range(3):
            authed_page.locator(".rp-episode input[type=checkbox]").nth(n).check()
        authed_page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        measured = authed_page.evaluate(
            """() => {
                const pane = document.querySelector('.rp-library').getBoundingClientRect();
                const spill = [...document.querySelector('.rp-library').children]
                    .filter(c => c.offsetParent && c.getBoundingClientRect().bottom > pane.bottom + 1).map(c => c.id || c.className);
                return { list: document.querySelector('#rpScroll').clientHeight, spill, bar: document.querySelector('#rpSelectBar').getBoundingClientRect().bottom, view: innerHeight };
            }"""
        )
        assert measured["spill"] == []
        assert measured["list"] >= 150  # at least two and a half rows
        assert measured["bar"] <= measured["view"]  # "Analyze" is reachable without leaving the pane

    def test_ticking_a_row_does_not_move_the_rows_below_it(
        self, authed_page: Page, api: RepriseApi, app_url: str
    ) -> None:
        _open(authed_page, app_url)
        rows = authed_page.locator(".rp-episode")
        before = rows.nth(1).bounding_box()["y"]
        authed_page.locator(".rp-episode input[type=checkbox]").first.check()
        expect(authed_page.locator("#rpSelectBar")).to_be_visible()
        assert rows.nth(1).bounding_box()["y"] == before

    def test_messages_stay_in_view_when_the_page_is_scrolled(
        self, authed_page: Page, api: RepriseApi, app_url: str
    ) -> None:
        authed_page.set_viewport_size({"width": 1280, "height": 720})
        _more_episodes(api, 34)
        _open(authed_page, app_url)
        authed_page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        authed_page.locator("#rpRefresh").dispatch_event("click")
        message = authed_page.locator("#rpMessage")
        expect(message).to_contain_text("Refreshing TV Shows")
        assert 0 <= message.bounding_box()["y"] < 100  # stuck near the top of the window, not left above the fold


@pytest.mark.e2e
class TestListFollowsFilters:
    def test_an_episode_the_filters_leave_out_is_closed_and_the_page_says_so(
        self, authed_page: Page, api: RepriseApi, app_url: str
    ) -> None:
        _more_episodes(api, 2, show="Andor")
        _open(authed_page, app_url)
        authed_page.locator(".rp-episode-open").first.click()
        expect(authed_page.locator("#rpReviewHeading")).to_have_text("Pilot")
        authed_page.select_option("#rpShow", "Andor")
        expect(authed_page.locator("#rpReview")).to_be_hidden()
        expect(authed_page.locator("#rpLibraryList")).to_be_visible()
        expect(authed_page.locator("#rpMessage")).to_contain_text("Closed Pilot")
        expect(authed_page.locator(".rp-episode-open[aria-current=true]")).to_have_count(0)

    def test_an_episode_that_is_still_in_the_filtered_list_stays_open(
        self, authed_page: Page, api: RepriseApi, app_url: str
    ) -> None:
        _more_episodes(api, 2, show="Andor")
        _open(authed_page, app_url)
        authed_page.locator(".rp-episode-open").first.click()
        authed_page.select_option("#rpShow", "Community")
        authed_page.select_option("#rpSeason", "1")
        expect(authed_page.locator(".rp-episode-open")).to_have_count(6)
        expect(authed_page.locator("#rpReviewHeading")).to_have_text("Pilot")
        expect(authed_page.locator("#rpOffList")).to_be_hidden()

    def test_unsaved_changes_keep_an_episode_open_and_the_page_says_why(
        self, authed_page: Page, api: RepriseApi, app_url: str
    ) -> None:
        _more_episodes(api, 2, show="Andor")
        _open(authed_page, app_url)
        authed_page.locator(".rp-episode-open").first.click()
        first = authed_page.locator(".rp-range").first
        first.locator(":scope > summary").click()
        first.locator("label[data-value=approved]").click()
        authed_page.select_option("#rpShow", "Andor")
        expect(authed_page.locator("#rpReviewHeading")).to_have_text("Pilot")
        expect(authed_page.locator("#rpOffList")).to_contain_text("unsaved changes")
        authed_page.locator("#rpOffListClear").click()
        expect(authed_page.locator("#rpOffList")).to_be_hidden()
        expect(authed_page.locator("#rpShow")).to_have_value("")
        expect(authed_page.locator(".rp-episode-open[aria-current=true]")).to_contain_text("Pilot")


@pytest.mark.e2e
class TestSelectingEpisodes:
    @staticmethod
    def _changed_files(api: RepriseApi) -> None:
        refresh = {
            "code": "file_missing",
            "label": "File missing",
            "message": "This file is no longer where Reprise last saw it.",
            "next": "Refresh the library, then analyze it again.",
            "action": "refresh",
        }
        again = {
            "code": "needs_reanalysis",
            "label": "Analyze again",
            "message": "This result is out of date.",
            "next": "Analyze it again to see the current result.",
            "action": "analyze",
        }
        for n, stale in ((4, refresh), (5, again)):
            api.items[n - 1].update(freshness="stale", stale=stale)
            api.results[f"v{n}"].update(freshness="stale", stale=stale)

    def test_select_all_ticks_every_episode_that_can_be_analyzed_now(
        self, authed_page: Page, api: RepriseApi, app_url: str
    ) -> None:
        self._changed_files(api)  # v4 needs a refresh, v6 is the plain "file changed" case: both are left out
        _open(authed_page, app_url)
        authed_page.locator("#rpSelectAll").click()
        expect(authed_page.locator("#rpAnalyze")).to_have_text("Analyze 4 episodes")
        expect(authed_page.locator("#rpSelectAll")).to_have_text("Deselect all")
        authed_page.locator("#rpAnalyze").click()
        assert api.analyses == [{"media_version_ids": ["v1", "v2", "v3", "v5"]}]

    def test_deselect_all_empties_the_selection(self, authed_page: Page, api: RepriseApi, app_url: str) -> None:
        _open(authed_page, app_url)
        authed_page.locator("#rpSelectAll").click()
        authed_page.locator("#rpSelectAll").click()
        expect(authed_page.locator("#rpSelectBar")).to_be_hidden()

    def test_the_status_and_next_step_of_a_changed_file_follow_the_reason_the_service_gives(
        self, authed_page: Page, api: RepriseApi, app_url: str
    ) -> None:
        self._changed_files(api)
        _open(authed_page, app_url)
        rows = authed_page.locator(".rp-episode-open")
        expect(rows.nth(3).locator(".rp-chip")).to_have_text("File missing")
        expect(rows.nth(4).locator(".rp-chip")).to_have_text("Analyze again")
        outcome = authed_page.locator("#rpOutcome")
        rows.nth(3).click()
        expect(outcome).to_contain_text("This file is no longer where Reprise last saw it.")
        expect(outcome.get_by_role("button", name="Refresh library")).to_be_visible()
        expect(authed_page.locator("#rpAnalyzeOne")).to_be_hidden()
        rows.nth(4).click()
        expect(outcome).to_contain_text("Analyze it again to see the current result.")
        expect(outcome.get_by_role("button", name="Refresh library")).to_have_count(0)
        expect(authed_page.locator("#rpAnalyzeOne")).to_be_visible()

    def test_only_an_actionable_season_links_to_its_review(
        self, authed_page: Page, api: RepriseApi, app_url: str
    ) -> None:
        _more_episodes(api, 2, show="Andor")
        _open(authed_page, app_url)
        expect(authed_page.get_by_role("link", name="Review season 1 of Community")).to_have_attribute(
            "href", "/reprise/approve?show=Community&season=1"
        )
        expect(authed_page.get_by_role("link", name=re.compile("Review season .* of Andor"))).to_have_count(0)
        expect(authed_page.locator("#rpReview")).to_be_hidden()
