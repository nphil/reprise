"""E2E: the Library page, Reprise's home.

Plain-language statuses, technical identifiers tucked behind "File details", every analysis outcome explained, and the
pick -> analyze -> progress -> result flow. The /api/reprise/* and /api/libraries calls are mocked, so no media or Plex
is needed; the shell, scripts and styles are the real ones served by the app.
"""

from __future__ import annotations

import copy
import json
import re
from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, Route, expect

from ._mocks import _fulfill_json

DURATION = 1_320_000


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
                    "Analysis stopped because this file can't be compared exactly.",
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

    def _handle(self, route: Route) -> None:
        request = route.request
        path, method = urlparse(request.url).path, request.method
        body = json.loads(request.post_data) if request.post_data else {}
        match = re.fullmatch(r"/api/reprise/media/([^/]+)/(result|stream|review)", path)
        if path == "/api/reprise/media":
            return _fulfill_json(
                route, {"items": copy.deepcopy(self.items), "next_cursor": None, "shows": ["Community"], "seasons": [1]}
            )
        if match and match[2] == "result":
            return _fulfill_json(route, copy.deepcopy(self.results[match[1]]))
        if match and match[2] == "stream":
            return route.fulfill(status=404, body="")
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
        expect(rows.nth(0)).to_contain_text("Community · S1 E1 · 22 min")
        for row, status in zip(
            range(6),
            ["2 to review", "Nothing found", "Analysis failed", "Not analyzed", "Not analyzed", "File changed"],
            strict=True,
        ):
            expect(rows.nth(row).locator(".rp-chip")).to_have_text(status)

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
        expect(outcome).to_contain_text("Analysis stopped because this file can't be compared exactly.")
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
        expect(authed_page.locator("#rpSaveBar")).to_be_hidden()

    def test_approving_a_section_and_saving_sends_only_that_decision(
        self, authed_page: Page, api: RepriseApi, app_url: str
    ) -> None:
        _open(authed_page, app_url)
        authed_page.locator(".rp-episode-open").first.click()
        first = authed_page.locator(".rp-range").first
        first.locator(":scope > summary").click()
        first.locator("label[data-value=approved]").click()
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
        headline = "This episode can't be compared exactly with the others, so nothing could be confirmed."
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
        job = authed_page.locator("#rpActivity .rp-job").first
        expect(job).to_contain_text("Analyzing 2 episodes")
        expect(authed_page.locator(".rp-episode-open").nth(3).locator(".rp-chip")).to_contain_text("Analyzing")
        api.jobs[0]["progress"] = {"percent": 40.0, "current_item": "Checking the intro: comparing with episode 2 of 6"}
        expect(job).to_contain_text("40%", timeout=10_000)
        expect(job).to_contain_text("comparing with episode 2 of 6")
        expect(job.get_by_role("button", name="Pause")).to_be_visible()
        api.finish_analysis()
        expect(authed_page.locator(".rp-episode-open").nth(3).locator(".rp-chip")).to_have_text(
            "Nothing found", timeout=10_000
        )
        expect(authed_page.locator("#rpActivity .rp-job").first).to_contain_text(
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
        expect(chip).to_contain_text("Plex publishing is on")

    def test_the_library_says_whether_analysis_uses_the_gpu_and_can_turn_it_off(
        self, authed_page: Page, api: RepriseApi, app_url: str
    ) -> None:
        _open(authed_page, app_url)
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
        authed_page.once("dialog", lambda dialog: dialog.accept())
        switch.check()
        expect(authed_page.locator("#rpSafety")).to_have_attribute("data-state", "on")
        assert api.settings_patches == [{"writes_enabled": True}]
