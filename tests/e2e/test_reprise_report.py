"""E2E: the Plex report page.

The /api/reprise/* calls are mocked (the comparison itself is tested in tests/reprise/test_plex_report.py); the shell,
script and styles are the real ones served by the app. Checks the plain-language summary, the filter chips, the link
into an episode's review and that nothing overflows at phone, tablet and desktop widths.
"""

from __future__ import annotations

import os
import re
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import Page, Route, expect

from ._mocks import _fulfill_json

SIZES = {"phone": (390, 844), "tablet": (1024, 768), "desktop": (1440, 900)}


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


def _flag(code: str, severity: str, text: str, kind: str | None = None, seconds: float | None = None) -> dict:
    return {"code": code, "severity": severity, "kind": kind, "seconds": seconds, "text": text}


def _row(n: int, title: str, summary: str, verdict: str, flags: list, plex: list, skips: list, **extra) -> dict:
    return {
        "id": f"v{n}",
        "server_id": "s",
        "show_title": "Community",
        "season": 3,
        "episode": n,
        "title": title,
        "label": f"S3E{n:02d}",
        "duration_ms": 1_300_000,
        "verdict": verdict,
        "summary": summary,
        "flags": flags,
        "analysis": {
            "status": extra.pop("status", "complete"),
            "freshness": "current",
            "stale": None,
            "reasons": extra.pop("reasons", []),
        },
        "plex": {"status": "read", "markers": plex, "unknown_kinds": []},
        "reprise": {"skips": skips, "suggestions": [], "keeps": extra.pop("keeps", []), "to_review": 0, "approved": 0},
        "published": False,
        "published_revision": None,
    }


def _m(kind: str, start: int, end: int) -> dict:
    return {"kind": kind, "start_ms": start, "end_ms": end}


INTRO, CREDITS = _m("intro", 100_000, 130_000), _m("credits", 1_200_000, 1_280_000)
REPORT = {
    "schema_version": 1,
    "show": "Community",
    "season": 3,
    "generated_at": "2026-11-20T12:00:00Z",
    "tolerance_ms": 1000,
    "plex": {"status": "read", "message": None},
    "summary": {
        "episodes": 5,
        "analyzed": 3,
        "counts": {
            "plex_cuts_into_keep": 1,
            "plex_starts_early": 1,
            "plex_ends_late": 1,
            "plex_missing": 0,
            "unconfirmed": 0,
            "failed": 1,
            "stale": 0,
            "not_analyzed": 0,
            "published": 0,
        },
        "phrases": {},
        "headline": "1 episode where Plex skips into a scene Reprise protects; 1 episode where Plex's skip starts too early; 1 episode where the analysis failed",
        "differs": 2,
        "needs_attention": 1,
    },
    "episodes": [
        _row(1, "Biology 101", "Plex and Reprise agree.", "ok", [], [INTRO, CREDITS], [INTRO, CREDITS]),
        _row(
            2,
            "Competitive Ecology",
            "Plex's credits skip cuts 14 s out of a protected scene. Plex's credits skip ends 20 s after Reprise's.",
            "differs",
            [
                _flag(
                    "plex_cuts_into_keep",
                    "problem",
                    "Plex's credits skip cuts 14 s out of a protected scene",
                    "credits",
                    14.0,
                ),
                _flag("plex_ends_late", "problem", "Skips 20 s of story after the credits", "credits", 20.0),
            ],
            [INTRO, _m("credits", 1_200_000, 1_300_000)],
            [INTRO, CREDITS],
            keeps=[_m("credits", 1_236_000, 1_250_000)],
        ),
        _row(
            3,
            "Advanced Criminal Law",
            "Plex's intro skip starts 40 s before Reprise's.",
            "differs",
            [_flag("plex_starts_early", "problem", "Skips 40 s of story before the intro", "intro", 40.0)],
            [_m("intro", 60_000, 130_000), CREDITS],
            [INTRO, CREDITS],
        ),
        _row(
            4,
            "Advanced Gay",
            "Reprise has no result for this episode yet.",
            "needs_attention",
            [_flag("not_analyzed", "attention", "Not analyzed yet")],
            [INTRO],
            [],
            status="missing",
        ),
        _row(
            5,
            "Remedial Chaos Theory",
            "The analysis failed.",
            "needs_attention",
            [_flag("failed", "problem", "Analysis failed")],
            [INTRO],
            [],
            status="failed",
            reasons=["Reprise couldn't read this file well enough to compare it."],
        ),
    ],
}


class ReportApi:
    def __init__(self, page: Page) -> None:
        self.reports: list[dict] = []
        self.refusal: str | None = None
        page.route(re.compile(r".*/api/reprise/(media\?|report|settings|clients|jobs|publications).*"), self._handle)

    def _handle(self, route: Route) -> None:
        url = urlparse(route.request.url)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path == "/api/reprise/media":
            seasons = [1, 2, 3] if query.get("show") else []
            return _fulfill_json(
                route,
                {"items": [], "next_cursor": None, "shows": ["Community", "Parks and Recreation"], "seasons": seasons},
            )
        if url.path == "/api/reprise/report":
            self.reports.append(query)
            if self.refusal:
                return _fulfill_json(route, {"error": self.refusal}, 400)
            return _fulfill_json(route, REPORT)
        if url.path == "/api/reprise/settings":
            return _fulfill_json(route, {"writes_enabled": False, "publication_available": True})
        return _fulfill_json(route, {"items": []})


@pytest.fixture
def api(authed_page: Page) -> ReportApi:
    return ReportApi(authed_page)


@pytest.mark.e2e
class TestReport:
    def test_the_report_opens_from_the_library_and_says_what_differs_in_plain_words(
        self, authed_page: Page, api: ReportApi, app_url: str
    ) -> None:
        authed_page.goto(f"{app_url}/reprise/report?show=Community&season=3")
        expect(authed_page.locator("#rpReportHeadline")).to_contain_text(
            "1 episode where Plex skips into a scene Reprise protects"
        )
        expect(authed_page.locator(".rp-report-row")).to_have_count(5)
        row = authed_page.locator(".rp-report-row").nth(2)
        expect(row).to_contain_text("Skips 40 s of story before the intro")
        expect(row).to_contain_text("Intro\u00a01:00\u2060\u2013\u20602:10")
        expect(authed_page.locator("#rpReportSub")).to_contain_text("5 episodes \u00b7 3 analyzed")
        assert api.reports == [{"show": "Community", "season": "3"}]
        expect(authed_page.locator("[data-tour=nav-library]")).to_have_attribute("aria-current", "page")

    def test_a_filter_chip_narrows_the_list_and_can_be_cleared(
        self, authed_page: Page, api: ReportApi, app_url: str
    ) -> None:
        authed_page.goto(f"{app_url}/reprise/report?show=Community&season=3")
        authed_page.get_by_role("button", name=re.compile("Starts too early")).click()
        expect(authed_page.locator(".rp-report-row")).to_have_count(1)
        expect(authed_page.get_by_role("button", name=re.compile("Starts too early"))).to_have_attribute(
            "aria-pressed", "true"
        )
        authed_page.get_by_role("button", name="Show every episode").click()
        expect(authed_page.locator(".rp-report-row")).to_have_count(5)

    def test_a_row_opens_that_episodes_review_link(self, authed_page: Page, api: ReportApi, app_url: str) -> None:
        authed_page.goto(f"{app_url}/reprise/report?show=Community&season=3")
        href = authed_page.locator(".rp-report-row").nth(1).get_attribute("href")
        assert urlparse(href).path == "/reprise" and parse_qs(urlparse(href).query) == {
            "show": ["Community"],
            "season": ["3"],
            "open": ["v2"],
        }

    def test_a_failed_analysis_shows_its_plain_reason(self, authed_page: Page, api: ReportApi, app_url: str) -> None:
        authed_page.goto(f"{app_url}/reprise/report?show=Community&season=3")
        expect(authed_page.locator(".rp-report-row").nth(4)).to_contain_text(
            "Reprise couldn't read this file well enough to compare it."
        )

    def test_choosing_a_show_runs_the_report_and_a_refusal_is_a_plain_sentence(
        self, authed_page: Page, api: ReportApi, app_url: str
    ) -> None:
        authed_page.goto(f"{app_url}/reprise/report")
        expect(authed_page.locator("#rpReportState")).to_contain_text("Choose a show")
        authed_page.select_option("#rpReportShow", "Community")
        expect(authed_page.locator(".rp-report-row")).to_have_count(5)
        assert api.reports[-1] == {"show": "Community"}
        api.refusal = "Reprise has no season 9 of Community. It has: 3."
        authed_page.select_option("#rpReportSeason", "2")
        expect(authed_page.locator("#rpMessage")).to_have_text("Reprise has no season 9 of Community. It has: 3.")

    @pytest.mark.parametrize("size", SIZES)
    def test_nothing_overflows_at_phone_tablet_and_desktop_widths(
        self, authed_page: Page, api: ReportApi, app_url: str, size: str
    ) -> None:
        width, height = SIZES[size]
        authed_page.set_viewport_size({"width": width, "height": height})
        authed_page.goto(f"{app_url}/reprise/report?show=Community&season=3")
        expect(authed_page.locator(".rp-report-row")).to_have_count(5)
        assert authed_page.evaluate("document.documentElement.scrollWidth") <= width
        shots = os.environ.get("REPORT_SHOTS")
        if shots:
            authed_page.screenshot(path=f"{shots}/report-{width}.png", full_page=True)

    def test_the_library_links_to_the_report_for_the_show_and_season_picked(
        self, authed_page: Page, api: ReportApi, app_url: str
    ) -> None:
        authed_page.goto(f"{app_url}/reprise?show=Community&season=3")
        link = authed_page.locator("#rpReportLink")
        expect(link).to_have_attribute("href", "/reprise/report?show=Community&season=3")
        assert api.reports == []
