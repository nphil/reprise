"""E2E tests for the /login page (additional coverage beyond test_webapp.py)."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from .conftest import E2E_USERNAME


@pytest.mark.e2e
class TestLoginPage:
    def test_username_input_is_autofocused(self, page: Page, app_url: str, session_cookie: dict) -> None:
        # ``session_cookie`` makes sure the owner account exists: with none, /login is sent to /welcome.
        page.goto(f"{app_url}/login")
        expect(page.locator("#username")).to_be_focused()

    def test_wrong_password_shows_error_alert(self, page: Page, app_url: str, session_cookie: dict) -> None:
        page.goto(f"{app_url}/login")
        page.locator("#username").fill(E2E_USERNAME)
        page.locator("#password").fill("definitely-not-the-real-password")
        page.locator('button[type="submit"]').click()
        expect(page.locator(".rp-message.error")).to_contain_text("isn't right", timeout=3000)
        # The username stays filled in so only the password needs retyping.
        expect(page.locator("#username")).to_have_value(E2E_USERNAME)

    def test_login_page_shows_title_and_concise_subtitle(self, page: Page, app_url: str, session_cookie: dict) -> None:
        page.goto(f"{app_url}/login")
        expect(page.locator("h1")).to_have_text("Sign in to Reprise")
        expect(page.locator(".auth-brand p")).to_have_text("Skip the repeat. Keep the story.")

    def test_password_field_and_reveal_button_are_touch_sized_when_viewport_is_phone(
        self, page: Page, app_url: str, session_cookie: dict
    ) -> None:
        page.set_viewport_size({"width": 390, "height": 844})
        page.goto(f"{app_url}/login")
        password = page.locator("#password").bounding_box()
        reveal = page.locator('[data-reveal="password"]').bounding_box()
        assert password["height"] >= 44
        assert reveal["width"] >= 44 and reveal["height"] >= 44
