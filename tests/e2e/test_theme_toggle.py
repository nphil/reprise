"""E2E: one picker applies real palettes to every page and follows the device on demand."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from ._mocks import mock_dashboard_defaults


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


@pytest.mark.e2e
class TestThemePicker:
    def test_all_42_palettes_are_available_and_searchable(self, authed_page: Page, app_url: str) -> None:
        mock_dashboard_defaults(authed_page)
        authed_page.goto(f"{app_url}/dashboard")
        trigger = authed_page.get_by_role("button", name="Theme", exact=True)
        trigger.click()
        picker = authed_page.get_by_role("region", name="Theme", exact=True)
        expect(picker.get_by_role("radio")).to_have_count(42)
        picker.get_by_role("searchbox", name="Search themes").fill("catppuccin")
        expect(picker.get_by_role("radio")).to_have_count(4)
        picker.get_by_role("radio", name="Catppuccin Latte").click()
        expect(picker.get_by_role("radio", name="Catppuccin Latte")).to_have_attribute("aria-checked", "true")
        expect(authed_page.locator("html")).to_have_attribute("data-theme", "catppuccin-latte")
        expect(authed_page.locator("html")).to_have_attribute("data-bs-theme", "light")
        authed_page.keyboard.press("Escape")
        expect(picker).to_be_hidden()
        expect(trigger).to_be_focused()

    def test_the_same_selected_palette_applies_to_other_pages(self, authed_page: Page, app_url: str) -> None:
        mock_dashboard_defaults(authed_page)
        authed_page.goto(f"{app_url}/dashboard")
        authed_page.locator("#rpThemeBtn").click()
        picker = authed_page.locator("#rpTheme")
        picker.get_by_role("searchbox").fill("nord")
        picker.get_by_role("radio", name="Nord", exact=True).click()
        authed_page.goto(f"{app_url}/reprise/approve")
        expect(authed_page.locator("html")).to_have_attribute("data-theme", "nord")
        expect(authed_page.locator("html")).to_have_attribute("data-bs-theme", "dark")
        expect(authed_page.locator("#rpThemeBtn")).to_have_count(1)

    def test_phone_follows_the_two_chosen_device_palettes(self, authed_page: Page, app_url: str) -> None:
        mock_dashboard_defaults(authed_page)
        authed_page.set_viewport_size({"width": 390, "height": 800})
        authed_page.emulate_media(color_scheme="dark")
        authed_page.goto(f"{app_url}/dashboard")
        authed_page.locator("#rpThemeBtn").click()
        picker = authed_page.locator("#rpTheme")
        switch = picker.get_by_role("switch", name="Follow my device")
        switch.click()
        expect(switch).to_have_attribute("aria-checked", "true")
        search = picker.get_by_role("searchbox")
        search.fill("nord")
        picker.get_by_role("radio", name="Nord", exact=True).click()
        search.fill("latte")
        picker.get_by_role("radio", name="Catppuccin Latte", exact=True).click()
        expect(authed_page.locator("html")).to_have_attribute("data-theme", "nord")
        authed_page.emulate_media(color_scheme="light")
        expect(authed_page.locator("html")).to_have_attribute("data-theme", "catppuccin-latte")
        expect(authed_page.locator("html")).to_have_attribute("data-bs-theme", "light")
