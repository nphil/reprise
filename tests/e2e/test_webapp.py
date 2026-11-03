"""
E2E tests for the web application.

Tests core functionality:
- Login page
- Authentication flow
- Page navigation
"""

import pytest
import requests
from playwright.sync_api import Page, expect

from .conftest import E2E_PASSWORD, E2E_USERNAME

# Use the requests library for API-contract checks rather than
# Playwright's page.request — the Python↔Node IPC stalls under
# pytest-xdist `-n auto` (playwright#26739). Same pattern as the
# canary fix in test_journey_schedule_lifecycle.py.
_API_TIMEOUT = 30


def _sign_in_through_form(page: Page, app_url: str) -> None:
    """Fill the real /login form with the e2e owner account and wait for the redirect away from it.

    The ``session_cookie`` fixture (a dependency of every caller) makes sure the owner account exists first: with no
    account /login sends the browser to the first-run page instead.
    """
    page.goto(f"{app_url}/login")
    page.locator("#username").fill(E2E_USERNAME)
    page.locator("#password").fill(E2E_PASSWORD)
    page.locator('button[type="submit"]').click()
    # The redirect may go to /, /setup, or /servers depending on setup-complete state, so match any non-/login URL.
    page.wait_for_url(lambda url: "/login" not in url, timeout=10000)


@pytest.mark.e2e
class TestAuthentication:
    """Test authentication flow."""

    def test_valid_credentials_redirect_away_from_login(self, page: Page, app_url: str, session_cookie: dict):
        """Verify the owner's username and password grant access and redirect."""
        _sign_in_through_form(page, app_url)
        assert "/login" not in page.url, f"Should redirect away from login, got: {page.url}"

    def test_authenticated_user_can_access_protected_pages(self, page: Page, app_url: str, session_cookie: dict):
        """Verify authenticated user can access the app."""
        # Login first, waiting for the redirect to land before navigating away.
        _sign_in_through_form(page, app_url)

        # After login, navigate to settings.
        page.goto(f"{app_url}/settings")
        # Pin the actual destination — accept either /settings (setup
        # complete) or /setup (the redirect when setup isn't done).
        # A regression returning 500 or 404 would fall through neither
        # branch and fail visibly.
        page.wait_for_load_state("domcontentloaded")
        assert page.url.endswith("/settings") or page.url.endswith("/setup"), (
            f"Expected /settings or /setup after login; got {page.url!r}"
        )


@pytest.mark.e2e
class TestSetupWizard:
    """Test setup wizard accessibility."""

    def test_setup_page_accessible_after_login(self, page: Page, app_url: str, session_cookie: dict):
        """Verify setup page is accessible after authentication."""
        _sign_in_through_form(page, app_url)

        # Navigate to setup.
        page.goto(f"{app_url}/setup")
        page.wait_for_load_state("domcontentloaded")

        # Setup page either renders the wizard (when setup isn't
        # complete) or 302s elsewhere. Pin: the destination URL must
        # NOT be /login. A regression that 500'd or auth-bounced
        # would land back on /login and fail here.
        assert "/login" not in page.url, f"Should access setup, got: {page.url}"


@pytest.mark.e2e
class TestAPIEndpoints:
    """Test API endpoint accessibility."""

    def test_health_check_endpoint(self, app_url: str):
        """Verify health check endpoint is accessible without auth."""
        # Use requests (not page.request) to avoid the Playwright
        # Python↔Node IPC stall under -n auto.
        response = requests.get(f"{app_url}/api/health", timeout=_API_TIMEOUT)

        # Health check should return 200.
        assert response.status_code == 200

        # Pin the actual response shape: {"status": "healthy"} (the
        # documented contract). Earlier accepted either "status" in
        # data OR "ok" in str(data) — too permissive; a regression
        # that swapped the field name would have silently passed.
        data = response.json()
        assert data.get("status") == "healthy", f"Health check body must be {{'status': 'healthy'}}; got {data!r}"

    def test_auth_status_endpoint(self, app_url: str):
        """Verify auth status endpoint is accessible."""
        response = requests.get(f"{app_url}/api/auth/status", timeout=_API_TIMEOUT)

        # Endpoint reachable without auth (200 even when unauthenticated).
        assert response.status_code == 200


@pytest.mark.e2e
class TestSetupWizardSteps:
    """Test the setup wizard's four steps (the last one finishes setup)."""

    def test_setup_wizard_has_4_steps(self, page: Page, app_url: str, session_cookie: dict):
        """Verify setup wizard has 4 progress steps."""
        # Setup page is accessible without login and avoids auth/session state
        # leakage from prior E2E tests. ``session_cookie`` only makes sure the
        # owner account exists (with none, every page sends the browser to /welcome).
        page.goto(f"{app_url}/setup")
        # Deterministic: wait for the first progress step to render
        # instead of a hardcoded sleep.
        expect(page.locator(".progress-step").first).to_be_visible(timeout=5000)

        progress_steps = page.locator(".progress-step")
        assert progress_steps.count() == 4
        expect(page.locator("#setupProgressCurrent")).to_have_text("Step 1 of 4 · Server type")

    def test_step4_is_labeled_options(self, page: Page, app_url: str, session_cookie: dict):
        """Verify the last step is labeled 'Options' (the old 'Security' step is gone)."""
        page.goto(f"{app_url}/setup")
        expect(page.locator('.progress-step[data-step="4"]')).to_contain_text("Options", timeout=5000)
        assert page.locator('.progress-step[data-step="5"]').count() == 0

    def test_wizard_has_no_access_token_form(self, page: Page, app_url: str, session_cookie: dict):
        """Verify the wizard no longer asks for an access token: the account was made on the first-run page."""
        page.goto(f"{app_url}/setup")
        expect(page.locator(".progress-step").first).to_be_visible(timeout=5000)

        assert page.locator("#newToken").count() == 0
        assert page.locator("#confirmToken").count() == 0
        assert page.locator("#finishSetup").count() == 0

    def test_step4_has_finish_setup_button(self, page: Page, app_url: str, session_cookie: dict):
        """Verify step 4's button says 'Finish setup' (it saves the options and completes setup)."""
        page.goto(f"{app_url}/setup")
        expect(page.locator(".progress-step").first).to_be_visible(timeout=5000)

        finish_btn = page.locator("#step4Next")
        assert finish_btn.count() == 1
        assert finish_btn.inner_text().strip() == "Finish setup"
