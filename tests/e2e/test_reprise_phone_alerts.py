"""E2E: the "Phone alerts" panel on the Sharing page (message switch and quiet hours).

The /api/reprise/* calls are mocked; the page, scripts and styles are the real ones served by the app.
"""

from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import Page, Route, expect

from ._mocks import _fulfill_json


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


class NotifyApi:
    def __init__(self, page: Page, *, available: bool = True) -> None:
        self.available = available
        self.state = {
            "enabled": True,
            "quiet_from": "22:00",
            "quiet_until": "08:00",
            "last": None,
            "waiting": 0,
            "waiting_reason": None,
        }
        self.patches: list[dict] = []
        page.route(re.compile(r".*/api/reprise/"), self._handle)

    def _handle(self, route: Route) -> None:
        request = route.request
        path = request.url.split("?")[0].split("/api/reprise", 1)[1]
        if path == "/notifications":
            if not self.available:
                return _fulfill_json(route, {"error": "Automatic work is not running."}, 400)
            if request.method == "PATCH":
                body = json.loads(request.post_data or "{}")
                self.patches.append(body)
                self.state.update(body)
            return _fulfill_json(route, self.state)
        if path == "/settings":
            return _fulfill_json(route, {"writes_enabled": False, "publication_available": False, "autopilot": None})
        if path in ("/clients", "/publications"):
            return _fulfill_json(route, {"items": []})
        return route.continue_()


@pytest.mark.e2e
class TestPhoneAlertsPanel:
    def test_the_switch_and_quiet_hours_are_shown_and_saved(self, authed_page: Page, app_url: str) -> None:
        api = NotifyApi(authed_page)
        api.state.update(
            last={"title": "Community S2: 3 sure ones", "at": "2020-01-01T00:00:00Z"}, waiting=2, waiting_reason="limit"
        )
        authed_page.goto(f"{app_url}/reprise/sharing")

        expect(authed_page.locator("#rpNotifyPanel")).to_be_visible()
        expect(authed_page.locator("#rpNotifyOn")).to_be_checked()
        expect(authed_page.locator("#rpQuietFrom")).to_have_value("22:00")
        expect(authed_page.locator("#rpQuietUntil")).to_have_value("08:00")
        expect(authed_page.locator("#rpNotifyStatus")).to_contain_text("Last message: Community S2: 3 sure ones")
        expect(authed_page.locator("#rpNotifyStatus")).to_contain_text(
            "2 waiting, held back by the three-an-hour limit"
        )

        authed_page.locator("#rpQuietUntil").fill("06:30")
        expect(authed_page.locator("#rpQuietUntil")).to_have_value("06:30")
        authed_page.locator("#rpNotifyOn").uncheck()
        expect(authed_page.locator("#rpNotifyStatus")).to_have_text("Phone alerts are off.")
        expect(authed_page.locator("#rpQuietFrom")).to_be_disabled()
        assert api.patches == [{"quiet_until": "06:30"}, {"enabled": False}]

    def test_the_panel_stays_hidden_when_there_is_no_automatic_work(self, authed_page: Page, app_url: str) -> None:
        NotifyApi(authed_page, available=False)
        authed_page.goto(f"{app_url}/reprise/sharing")

        expect(authed_page.locator("#rpClientForm, #rpWritesEnabled").first).to_be_attached()
        expect(authed_page.locator("#rpNotifyPanel")).to_be_hidden()
