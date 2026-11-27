"""The same settings shortcuts stay reachable on a tablet and on the narrowest phone."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from ._mocks import mock_dashboard_defaults

TABS = ["Connections", "Sharing", "Automation", "Advanced"]
TOOLS = ["Jobs", "Logs", "Inspector", "Library health"]


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


def _fits_viewport(page: Page, selector: str) -> bool:
    return page.locator(selector).evaluate(
        """el => {
            const r = el.getBoundingClientRect();
            return r.width > 0 && r.height > 0 && r.left >= 0 && r.right <= document.documentElement.clientWidth;
        }"""
    )


@pytest.mark.e2e
class TestSettingsTabStrip:
    @pytest.mark.parametrize("width", [320, 390, 1024])
    def test_settings_show_four_sections_and_group_tools_without_clipping(
        self, authed_page: Page, app_url: str, width: int
    ) -> None:
        mock_dashboard_defaults(authed_page)
        authed_page.set_viewport_size({"width": width, "height": 800})
        authed_page.goto(f"{app_url}/library-health")
        nav = authed_page.get_by_role("navigation", name="Settings", exact=True)
        expect(nav.get_by_role("link")).to_have_text(TABS)
        assert _fits_viewport(authed_page, ".rp-subnav")

        tools = nav.get_by_role("button", name="Tools")
        expect(tools).to_have_attribute("data-current", "")
        tools.press("Enter")
        panel = authed_page.locator("#rpTools")
        expect(panel.get_by_role("link")).to_have_text(TOOLS)
        expect(panel.get_by_role("link", name="Library health")).to_be_focused()
        expect(panel.get_by_role("link", name="Library health")).to_have_attribute("aria-current", "page")
        assert _fits_viewport(authed_page, "#rpTools")
        authed_page.keyboard.press("Escape")
        expect(panel).to_be_hidden()
        expect(tools).to_be_focused()

    def test_phone_bottom_navigation_opens_settings_in_one_tap(self, authed_page: Page, app_url: str) -> None:
        mock_dashboard_defaults(authed_page)
        authed_page.set_viewport_size({"width": 390, "height": 800})
        authed_page.goto(f"{app_url}/reprise")
        nav = authed_page.get_by_role("navigation", name="Main", exact=True)
        expect(nav.get_by_role("link")).to_have_text(["Library", "Review", "Settings"])
        expect(nav).to_have_css("position", "fixed")
        assert nav.evaluate("el => Math.abs(el.getBoundingClientRect().bottom - innerHeight) <= 1")
        nav.get_by_role("link", name="Settings").click()
        expect(authed_page).to_have_url(f"{app_url}/servers")

    def test_phone_plex_status_reports_failure_and_links_to_sharing(self, authed_page: Page, app_url: str) -> None:
        mock_dashboard_defaults(authed_page)
        authed_page.route("**/api/reprise/settings", lambda route: route.fulfill(status=503, json={}))
        authed_page.set_viewport_size({"width": 390, "height": 800})
        authed_page.goto(f"{app_url}/dashboard")
        chip = authed_page.get_by_role("link", name="Plex status unavailable")
        expect(chip).to_have_attribute("title", "Plex status unavailable. Open Sharing to check.")
        expect(chip).to_have_attribute("href", "/reprise/sharing")

    def test_outside_click_closes_help_without_activating_the_page(self, authed_page: Page, app_url: str) -> None:
        mock_dashboard_defaults(authed_page)
        authed_page.goto(f"{app_url}/dashboard")
        help_button = authed_page.locator("#rpHelpBtn")
        help_button.click()
        expect(authed_page.locator("#rpHelp")).to_be_visible()
        library = authed_page.get_by_role("navigation", name="Main", exact=True).get_by_role("link", name="Library")
        bounds = library.bounding_box()
        assert bounds
        authed_page.mouse.click(bounds["x"] + bounds["width"] / 2, bounds["y"] + bounds["height"] / 2)
        expect(authed_page.locator("#rpHelp")).to_be_hidden()
        expect(help_button).to_be_focused()
        expect(authed_page).to_have_url(f"{app_url}/dashboard")
