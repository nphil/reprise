"""E2E: the Settings tab strip shows every tab on a tablet and keeps the current tab in view on a phone.

Regression: at 1024 px the eight tabs (with icons) did not fit beside the sidebar, and the script that scrolled the
current tab into view *centred* it, which pushed the first tabs ("Connections") out of sight under the sidebar.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page

from ._mocks import mock_dashboard_defaults

TABS = ["Connections", "Sharing", "Automation", "Advanced", "Jobs", "Logs", "Inspector", "Library health"]


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


def _strip(page: Page) -> dict:
    return page.evaluate(
        """() => {
            const strip = document.querySelector('.rp-subnav-scroll');
            const tabs = [...strip.querySelectorAll('a')];
            const box = strip.getBoundingClientRect();
            return {
                scrollLeft: strip.scrollLeft,
                overflow: strip.scrollWidth - strip.clientWidth,
                labels: tabs.map(a => a.textContent.trim()),
                clipped: tabs.filter(a => { const r = a.getBoundingClientRect(); return r.left < box.left - 1 || r.right > box.right + 1; })
                             .map(a => a.textContent.trim()),
                current: (strip.querySelector('a.active') || {}).textContent || '',
                fade: strip.dataset.fade || '',
            };
        }"""
    )


@pytest.mark.e2e
class TestSettingsTabStrip:
    def test_every_tab_is_visible_on_a_tablet(self, authed_page: Page, app_url: str) -> None:
        mock_dashboard_defaults(authed_page)
        authed_page.set_viewport_size({"width": 1024, "height": 800})
        for path in ("/dashboard", "/library-health"):  # a middle tab and the last tab
            authed_page.goto(f"{app_url}{path}")
            authed_page.wait_for_load_state("domcontentloaded")
            strip = _strip(authed_page)
            assert strip["labels"] == TABS
            assert strip["scrollLeft"] == 0, "the strip must not be scrolled away from its first tab"
            assert strip["clipped"] == [] and strip["overflow"] <= 0, strip

    def test_current_tab_is_in_view_on_a_phone(self, authed_page: Page, app_url: str) -> None:
        mock_dashboard_defaults(authed_page)
        authed_page.set_viewport_size({"width": 390, "height": 800})
        authed_page.goto(f"{app_url}/library-health")
        authed_page.wait_for_load_state("domcontentloaded")
        strip = _strip(authed_page)
        assert strip["current"].strip() == "Library health"
        assert "Library health" not in strip["clipped"]
        assert strip["fade"] in ("start", "both"), "the cut-off start of the strip should fade"

    def test_collapsed_sidebar_keeps_the_safety_note_as_a_tooltip(self, authed_page: Page, app_url: str) -> None:
        mock_dashboard_defaults(authed_page)
        authed_page.set_viewport_size({"width": 1280, "height": 800})
        authed_page.goto(f"{app_url}/dashboard")
        authed_page.wait_for_load_state("domcontentloaded")
        authed_page.locator("#rpRailToggle").click()
        authed_page.wait_for_function("document.getElementById('rpSafety').title !== ''")
        title = authed_page.locator("#rpSafety").get_attribute("title") or ""
        assert "Plex" in title
