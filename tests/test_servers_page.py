"""Smoke test for the /servers HTML page."""

from __future__ import annotations

import pytest

from media_preview_generator.web.settings_manager import get_settings_manager
from tests.auth_helpers import ensure_owner_account, sign_in


@pytest.fixture
def mock_auth_config(tmp_path, monkeypatch):
    auth_file = str(tmp_path / "auth.json")
    monkeypatch.setattr("media_preview_generator.web.auth.AUTH_FILE", auth_file)
    from media_preview_generator.web.settings_manager import reset_settings_manager

    reset_settings_manager()
    from media_preview_generator.web.routes import clear_gpu_cache

    clear_gpu_cache()
    return str(tmp_path)


@pytest.fixture
def flask_app(tmp_path, mock_auth_config):
    from media_preview_generator.web.app import create_app

    app = create_app(config_dir=str(tmp_path))
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False  # CSRF has its own tests: tests/test_csrf.py
    return app


@pytest.fixture
def client(flask_app):
    return flask_app.test_client()


@pytest.fixture
def authenticated_client(flask_app):
    """A test client with a valid session cookie."""
    sm = get_settings_manager()
    sm.set("setup_complete", True)

    client = flask_app.test_client()
    sign_in(client)
    return client


class TestServersPage:
    def test_unauthenticated_redirects_to_login(self, client):
        ensure_owner_account()  # without an account every page sends you to /welcome instead
        response = client.get("/servers", follow_redirects=False)
        # Flask's redirect() defaults to 302 — pinning the status code
        # ensures we don't silently accept e.g. a 308 (permanent route change)
        # or a 303 (POST redirect) when neither is what the auth gate emits.
        assert response.status_code == 302
        assert "/login" in response.headers.get("Location", "")

    def test_authenticated_renders(self, authenticated_client):
        response = authenticated_client.get("/servers")
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        # Key landmarks of the rendered page.
        assert "Connections" in body
        assert "Add server" in body
        assert 'id="webhookSecretPanel"' in body
        # The webhook URL placeholder is now the per-server pinned form
        # (multi-server-safe). Either landmark proves the Webhook & Scanner
        # tab rendered.
        assert "/api/webhooks/server/" in body or "webhookUrl" in body
        assert 'data-type="plex"' in body
        assert 'data-type="emby"' in body
        assert 'data-type="jellyfin"' in body

    def test_sidebar_links_to_the_servers_page(self, authenticated_client):
        # Verify the rendered library page, not the root redirect.
        response = authenticated_client.get("/reprise")
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert "/servers" in body
