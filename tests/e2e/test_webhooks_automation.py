"""E2E tests for /automation (webhooks/triggers) page.

Regression coverage for the copy cleanup pass — the patronising
"you don't have to open this app every time" line must be gone, and
the new tighter copy must be present.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


@pytest.mark.e2e
class TestAutomationCopy:
    def test_old_patronising_line_is_gone(self, authed_page: Page, app_url: str) -> None:
        authed_page.goto(f"{app_url}/automation")
        authed_page.wait_for_load_state("domcontentloaded")
        # Regression for the user-flagged copy.
        assert "you don't have to open this app every time" not in authed_page.content()

    def test_overview_intro_is_concise(self, authed_page: Page, app_url: str) -> None:
        authed_page.goto(f"{app_url}/automation")
        authed_page.wait_for_load_state("domcontentloaded")
        # The first card leads with the address to paste and explains "webhook" once, in plain words.
        expect(authed_page.locator("#section-webhooks-sonarr-radarr")).to_contain_text(
            "a message one app sends another"
        )


@pytest.mark.e2e
class TestAutomationSections:
    def test_decision_list_renders(self, authed_page: Page, app_url: str) -> None:
        authed_page.goto(f"{app_url}/automation")
        authed_page.wait_for_load_state("domcontentloaded")
        # "Other ways to start previews" lists Custom, Plex, Emby and Jellyfin.
        rows = authed_page.locator("#section-webhooks-overview .decision-row")
        assert rows.count() == 4

    def test_arr_section_link_present(self, authed_page: Page, app_url: str) -> None:
        authed_page.goto(f"{app_url}/automation")
        authed_page.wait_for_load_state("domcontentloaded")
        # The Sonarr/Radarr card comes first, and the other-ways list links on to the Custom webhook card.
        expect(authed_page.locator("#section-webhooks-sonarr-radarr")).to_be_visible()
        expect(authed_page.locator('a[href="#section-webhooks-custom"]').first).to_be_visible()
