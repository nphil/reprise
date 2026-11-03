"""Tests for AUTH_METHOD=external (external authentication bypass)."""

import json

import pytest

from media_preview_generator.web.auth import (
    AUTH_METHOD_EXTERNAL,
    AUTH_METHOD_INTERNAL,
    get_auth_method,
    is_auth_external,
)
from media_preview_generator.web.settings_manager import get_settings_manager
from tests.auth_helpers import ensure_owner_account, sign_in


class TestGetAuthMethod:
    """Tests for get_auth_method() env var parsing."""

    def test_default_is_internal(self, monkeypatch):
        monkeypatch.delenv("AUTH_METHOD", raising=False)
        assert get_auth_method() == AUTH_METHOD_INTERNAL

    def test_external_lowercase(self, monkeypatch):
        monkeypatch.setenv("AUTH_METHOD", "external")
        assert get_auth_method() == AUTH_METHOD_EXTERNAL

    def test_external_uppercase(self, monkeypatch):
        monkeypatch.setenv("AUTH_METHOD", "EXTERNAL")
        assert get_auth_method() == AUTH_METHOD_EXTERNAL

    def test_external_mixed_case(self, monkeypatch):
        monkeypatch.setenv("AUTH_METHOD", "External")
        assert get_auth_method() == AUTH_METHOD_EXTERNAL

    def test_external_with_whitespace(self, monkeypatch):
        monkeypatch.setenv("AUTH_METHOD", "  external  ")
        assert get_auth_method() == AUTH_METHOD_EXTERNAL

    def test_internal_explicit(self, monkeypatch):
        monkeypatch.setenv("AUTH_METHOD", "internal")
        assert get_auth_method() == AUTH_METHOD_INTERNAL

    def test_invalid_value_falls_back_to_internal(self, monkeypatch):
        monkeypatch.setenv("AUTH_METHOD", "bogus")
        assert get_auth_method() == AUTH_METHOD_INTERNAL

    def test_empty_string_falls_back_to_internal(self, monkeypatch):
        monkeypatch.setenv("AUTH_METHOD", "")
        assert get_auth_method() == AUTH_METHOD_INTERNAL


class TestIsAuthExternal:
    """Tests for is_auth_external() helper."""

    def test_false_by_default(self, monkeypatch):
        monkeypatch.delenv("AUTH_METHOD", raising=False)
        assert is_auth_external() is False

    def test_true_when_external(self, monkeypatch):
        monkeypatch.setenv("AUTH_METHOD", "external")
        assert is_auth_external() is True

    def test_false_when_internal(self, monkeypatch):
        monkeypatch.setenv("AUTH_METHOD", "internal")
        assert is_auth_external() is False


class TestExternalAuthRouteBehavior:
    """Test that protected routes pass through when AUTH_METHOD=external."""

    @pytest.fixture(autouse=True)
    def _set_external(self, monkeypatch):
        monkeypatch.setenv("AUTH_METHOD", "external")

    def test_home_and_dashboard_accessible_without_token(self, client):
        """External auth allows the home redirect and both destination pages."""
        settings = get_settings_manager()
        settings.set("setup_complete", True)

        response = client.get("/", follow_redirects=False)
        assert response.status_code == 302
        assert response.headers["Location"].endswith("/reprise")
        assert client.get("/reprise").status_code == 200
        assert client.get("/dashboard").status_code == 200

    def test_api_jobs_accessible_without_token(self, client):
        """GET /api/jobs should succeed without any token."""
        settings = get_settings_manager()
        settings.set("setup_complete", True)

        response = client.get("/api/jobs")
        assert response.status_code == 200

    def test_api_settings_accessible_without_token(self, client):
        """GET /api/settings should succeed without any token."""
        settings = get_settings_manager()
        settings.set("setup_complete", True)

        response = client.get("/api/settings")
        assert response.status_code == 200

    def test_api_auth_status_reports_external(self, client):
        """GET /api/auth/status should report auth_method='external' and never ask for a first-run account."""
        response = client.get("/api/auth/status")
        assert response.status_code == 200
        data = response.get_json()
        assert data["authenticated"] is True
        assert data["auth_method"] == "external"
        assert data["setup_required"] is False

    def test_login_page_redirects_to_dashboard(self, client):
        """GET /login should redirect to dashboard when external auth is active."""
        settings = get_settings_manager()
        settings.set("setup_complete", True)

        response = client.get("/login", follow_redirects=False)
        assert response.status_code == 302
        assert response.headers["Location"].endswith("/")


class TestInternalAuthUnchanged:
    """Verify default (internal) auth still requires a signed-in browser or the webhook secret."""

    @pytest.fixture(autouse=True)
    def _set_internal(self, monkeypatch):
        monkeypatch.setenv("AUTH_METHOD", "internal")

    def test_api_jobs_requires_auth(self, client):
        """GET /api/jobs should return 401 without credentials."""
        settings = get_settings_manager()
        settings.set("setup_complete", True)

        response = client.get("/api/jobs")
        assert response.status_code == 401

    def test_api_jobs_with_the_webhook_secret_succeeds(self, client, auth_headers):
        """GET /api/jobs should succeed with the webhook secret as the script header."""
        settings = get_settings_manager()
        settings.set("setup_complete", True)

        response = client.get("/api/jobs", headers=auth_headers)
        assert response.status_code == 200

    def test_api_jobs_with_a_signed_in_browser_succeeds(self, client):
        """GET /api/jobs should succeed for a browser signed in as the owner."""
        settings = get_settings_manager()
        settings.set("setup_complete", True)

        response = sign_in(client).get("/api/jobs")
        assert response.status_code == 200

    def test_api_auth_status_reports_internal(self, client):
        """GET /api/auth/status should report auth_method='internal'."""
        response = client.get("/api/auth/status")
        data = response.get_json()
        assert data["auth_method"] == "internal"

    def test_api_auth_status_says_setup_is_required_until_the_owner_account_exists(self, client):
        """No account yet: the first-run page is still ahead. Once the account exists it is closed."""
        assert client.get("/api/auth/status").get_json()["setup_required"] is True

        ensure_owner_account()

        assert client.get("/api/auth/status").get_json()["setup_required"] is False


class TestWebhookAuthNotBypassed:
    """Webhook auth uses its own decorator and must NOT be bypassed by external auth."""

    @pytest.fixture(autouse=True)
    def _set_external(self, monkeypatch):
        monkeypatch.setenv("AUTH_METHOD", "external")

    def test_radarr_webhook_requires_the_webhook_secret(self, client):
        """POST /api/webhooks/radarr still requires the webhook secret when external."""
        response = client.post(
            "/api/webhooks/radarr",
            data=json.dumps({"eventType": "Test"}),
            content_type="application/json",
        )
        assert response.status_code == 401

    def test_sonarr_webhook_requires_the_webhook_secret(self, client):
        """POST /api/webhooks/sonarr still requires the webhook secret when external."""
        response = client.post(
            "/api/webhooks/sonarr",
            data=json.dumps({"eventType": "Test"}),
            content_type="application/json",
        )
        assert response.status_code == 401

    def test_custom_webhook_not_auto_authenticated(self, client):
        """POST /api/webhooks/custom is not bypassed by external auth.

        The acceptable rejection codes are enumerated explicitly: a bare
        ``!= 200`` would let a 500 crash pass.

        The custom endpoint checks its own webhook secret and needs no
        CSRF token, so it answers 401; 302 and 403 would reject too.
        A 500 is NOT.
        """
        response = client.post(
            "/api/webhooks/custom",
            data=json.dumps({"eventType": "Test"}),
            content_type="application/json",
        )
        assert response.status_code in (
            302,
            401,
            403,
        ), (
            f"custom webhook must reject unauthenticated POST with 302/401/403; "
            f"got {response.status_code} (a 500 indicates a crash, not a security boundary)"
        )

    def test_radarr_webhook_succeeds_with_the_webhook_secret(self, client, auth_headers):
        """POST /api/webhooks/radarr succeeds with the webhook secret even when external."""
        response = client.post(
            "/api/webhooks/radarr",
            data=json.dumps({"eventType": "Test"}),
            content_type="application/json",
            headers=auth_headers,
        )
        assert response.status_code == 200


class TestExternalAuthReenableInternal:
    """Test that unsetting AUTH_METHOD re-enables internal auth."""

    def test_switch_from_external_to_internal(self, client, monkeypatch):
        """Removing AUTH_METHOD should require credentials again."""
        settings = get_settings_manager()
        settings.set("setup_complete", True)

        monkeypatch.setenv("AUTH_METHOD", "external")
        response = client.get("/api/jobs")
        assert response.status_code == 200

        monkeypatch.setenv("AUTH_METHOD", "internal")
        response = client.get("/api/jobs")
        assert response.status_code == 401

    def test_switch_from_external_to_internal_with_no_account_asks_for_one(self, client, monkeypatch):
        """No account was ever made behind the proxy: the pages go to the first-run page, not a sign-in page."""
        settings = get_settings_manager()
        settings.set("setup_complete", True)

        monkeypatch.setenv("AUTH_METHOD", "external")
        assert client.get("/reprise").status_code == 200

        monkeypatch.setenv("AUTH_METHOD", "internal")
        response = client.get("/reprise", follow_redirects=False)

        assert response.status_code == 302
        assert response.headers["Location"].endswith("/welcome")
