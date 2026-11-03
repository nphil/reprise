"""Smoke test for the /nodes HTML page and its place in the Settings tabs."""

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
    app.config["WTF_CSRF_ENABLED"] = False
    return app


@pytest.fixture
def client(flask_app):
    return flask_app.test_client()


@pytest.fixture
def authenticated_client(flask_app):
    get_settings_manager().set("setup_complete", True)
    client = flask_app.test_client()
    sign_in(client)
    return client


class TestNodesPage:
    def test_unauthenticated_redirects_to_login(self, client):
        ensure_owner_account()  # without an account every page sends you to /welcome instead
        response = client.get("/nodes", follow_redirects=False)
        assert response.status_code == 302
        assert "/login" in response.headers.get("Location", "")

    def test_authenticated_renders_the_page_and_its_dialogs(self, authenticated_client):
        response = authenticated_client.get("/nodes")
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert "<title>Nodes · Reprise</title>" in body
        for landmark in (
            'id="nodeList"',
            'id="nodesCheckNow"',
            'id="nodesAddBtn"',
            'id="addNodeModal"',
            'id="addNodeUrl"',
            'placeholder="http://host.lan:9494"',
            'id="nodeModal"',
            'data-bs-target="#node-tab-health"',
            'data-bs-target="#node-tab-log"',
            "js/nodes.js",
            "css/pages/nodes.css",
        ):
            assert landmark in body, landmark
        # The key field never shows what was typed.
        assert 'type="password" class="form-control" id="addNodeKey"' in body
