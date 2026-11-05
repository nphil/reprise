"""The setup guide as a browser meets it: where a new account lands, what the status API says, and what an install that
already works must never see.

Every test builds the real app in a fresh config folder (what a container's first start does) and drives it with a
test client; the status API itself is never mocked.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from media_preview_generator.reprise import acceleration
from media_preview_generator.reprise.identity import media_version
from media_preview_generator.reprise.models import PlaybackIdentity
from tests.auth_helpers import sign_in

PASSWORD = "correct horse battery"


@pytest.fixture
def make_app(tmp_path, monkeypatch):
    from media_preview_generator.web import auth, settings_manager
    from media_preview_generator.web.app import create_app
    from media_preview_generator.web.routes import clear_gpu_cache, limiter

    def build(*, settings=None):
        monkeypatch.setattr(auth, "AUTH_FILE", str(tmp_path / "auth.json"))
        monkeypatch.setattr(auth, "CONFIG_DIR", str(tmp_path))
        monkeypatch.delenv("WEB_AUTH_TOKEN", raising=False)
        monkeypatch.delenv("AUTH_METHOD", raising=False)
        settings_manager.reset_settings_manager()
        clear_gpu_cache()
        (tmp_path / "settings.json").write_text(json.dumps(settings if settings is not None else {}))
        monkeypatch.setattr("media_preview_generator.web.app._prewarm_caches", lambda: None)
        monkeypatch.setattr(
            acceleration,
            "status",
            lambda: {"enabled": True, "active": False, "label": "CPU (no GPU found)", "devices": []},
        )
        app = create_app(config_dir=str(tmp_path))
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        monkeypatch.setattr(limiter, "enabled", False)
        return app

    yield build
    settings_manager.reset_settings_manager()


def create_account(client, **request):
    data = {"username": "nitin", "password": PASSWORD, "confirm_password": PASSWORD}
    return client.post("/welcome", data=data, **request)


def auth_file(tmp_path) -> dict:
    return json.loads((tmp_path / "auth.json").read_text())


def plex_server(tmp_path, *, libraries=True, folders_exist=True, **output):
    """A connected Plex whose one library lives in a folder this container can (or cannot) see."""
    shows = tmp_path / "media" / "tv"
    if folders_exist:
        shows.mkdir(parents=True, exist_ok=True)
        (shows / "Show S01E01.mkv").write_bytes(b"x")
    return {
        "id": "plex-home",
        "name": "Home Plex",
        "type": "plex",
        "enabled": True,
        "url": "http://127.0.0.1:9",
        "auth": {"token": "saved-token"},
        "libraries": [
            {"id": "1", "name": "TV Shows", "remote_paths": [str(shows)], "enabled": libraries, "kind": "episode"}
        ]
        if libraries
        else [],
        "path_mappings": [],
        "output": output,
    }


def seed_servers(*servers):
    from media_preview_generator.web.settings_manager import get_settings_manager

    get_settings_manager().set("media_servers", list(servers))


def register_episode(app, tmp_path):
    path = tmp_path / "Show S01E01.mkv"
    path.write_bytes(b"0123456789")
    identity = PlaybackIdentity("machine", "1", "2", "3", "/library/parts/3/file", 10, 5000, "4")
    app.extensions["reprise"].store.register_media(
        media_version(str(path), 5000, "{}"), identity, server_id="plex-home", metadata={}
    )


def get_guide(client):
    response = client.get("/api/reprise/setup-guide")
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()


def step(guide, step_id):
    return next(s for s in guide["steps"] if s["id"] == step_id)


class TestANewInstall:
    def test_creating_the_account_lands_on_the_guide_not_the_wizard(self, make_app):
        client = make_app(settings={"setup_complete": False}).test_client()

        assert create_account(client).status_code == 302
        landed = client.get("/")

        assert landed.status_code == 302 and landed.headers["Location"].endswith("/setup-guide")
        page = client.get("/setup-guide")
        assert page.status_code == 200 and "Setup guide" in page.get_data(as_text=True)

    def test_the_guide_is_reachable_before_any_server_is_connected(self, make_app):
        client = make_app(settings={"setup_complete": False}).test_client()
        create_account(client)

        assert client.get("/setup-guide").status_code == 200
        assert client.get("/api/reprise/setup-guide").status_code == 200

    def test_the_sidebar_can_say_review_only_before_anything_is_connected(self, make_app):
        client = make_app(settings={"setup_complete": False}).test_client()
        create_account(client)

        answer = client.get("/api/reprise/settings")

        assert answer.status_code == 200 and answer.get_json()["writes_enabled"] is False

    def test_the_account_file_remembers_the_guide_is_pending(self, make_app, tmp_path):
        client = make_app().test_client()

        create_account(client)

        assert auth_file(tmp_path)["setup_guide"] == {"state": "pending"}

    def test_the_status_says_connect_plex_first_and_nothing_is_done(self, make_app):
        client = make_app(settings={"setup_complete": False}).test_client()
        create_account(client)

        guide = get_guide(client)

        assert (guide["state"], guide["auto_open"], guide["required_done"]) == ("pending", True, 0)
        assert step(guide, "connect_plex")["action"]["href"] == "/setup"
        assert [s["state"] for s in guide["steps"][:3]] == ["todo", "blocked", "blocked"]

    def test_the_webhook_addresses_use_the_address_the_owner_opened(self, make_app):
        client = make_app().test_client()
        opened_at = {"base_url": "http://nas.local:8080"}
        create_account(client, **opened_at)

        guide = client.get("/api/reprise/setup-guide", **opened_at).get_json()

        assert step(guide, "webhooks")["values"][0]["value"] == "http://nas.local:8080/api/webhooks/sonarr"

    def test_skipping_stops_the_guide_opening_by_itself(self, make_app, tmp_path):
        client = make_app(settings={"setup_complete": True}).test_client()
        create_account(client)

        saved = client.post("/api/reprise/setup-guide", json={"state": "dismissed"})

        assert saved.get_json() == {"state": "dismissed"}
        assert auth_file(tmp_path)["setup_guide"]["state"] == "dismissed"
        assert client.get("/").headers["Location"].endswith("/reprise")
        assert get_guide(client)["auto_open"] is False
        assert client.get("/setup-guide").status_code == 200, "still reachable from the sidebar"

    def test_a_skipped_guide_on_a_fresh_install_leaves_the_wizard_in_charge(self, make_app):
        client = make_app(settings={"setup_complete": False}).test_client()
        create_account(client)
        client.post("/api/reprise/setup-guide", json={"state": "dismissed"})

        assert client.get("/").headers["Location"].endswith("/setup")

    def test_the_tour_waits_for_the_library_instead_of_opening_over_the_guide(self, make_app):
        client = make_app(settings={"setup_complete": True}).test_client()
        create_account(client)

        guide_page = client.get("/setup-guide").get_data(as_text=True)
        library_page = client.get("/reprise").get_data(as_text=True)

        assert 'data-auto="false"' in guide_page
        assert 'data-auto="true"' in library_page

    def test_after_the_wizard_the_next_stop_is_still_the_guide_until_the_library_is_read(self, make_app, tmp_path):
        client = make_app(settings={"setup_complete": True}).test_client()
        create_account(client)
        seed_servers(plex_server(tmp_path))

        guide = get_guide(client)

        assert [s["state"] for s in guide["steps"][:3]] == ["done", "done", "todo"]
        assert client.get("/").headers["Location"].endswith("/setup-guide")

    def test_once_the_library_is_read_home_is_the_library_and_the_guide_can_finish(self, make_app, tmp_path):
        app = make_app(settings={"setup_complete": True})
        client = app.test_client()
        create_account(client)
        seed_servers(plex_server(tmp_path))
        register_episode(app, tmp_path)

        guide = get_guide(client)

        assert (guide["all_required_done"], guide["auto_open"]) == (True, False)
        assert client.get("/").headers["Location"].endswith("/reprise")
        assert client.post("/api/reprise/setup-guide", json={"state": "completed"}).get_json() == {"state": "completed"}
        assert auth_file(tmp_path)["setup_guide"]["state"] == "completed"

    def test_a_missing_media_mount_is_named_in_the_guide(self, make_app, tmp_path):
        client = make_app(settings={"setup_complete": True}).test_client()
        create_account(client)
        seed_servers(plex_server(tmp_path, folders_exist=False))

        folders = step(get_guide(client), "libraries_and_folders")

        assert folders["state"] == "todo"
        assert str(tmp_path / "media" / "tv") in folders["details"][0]
        assert folders["action"]["href"] == "/servers?edit=plex-home&tab=paths"


class TestAnInstallThatAlreadyWorks:
    """Nitin's install: an account from before the guide existed, Plex connected, libraries read, sending on."""

    @pytest.fixture
    def established(self, make_app, tmp_path):
        from media_preview_generator.web import auth

        app = make_app(settings={"setup_complete": True})
        client = app.test_client()
        create_account(client)
        seed_servers(plex_server(tmp_path))
        register_episode(app, tmp_path)
        # An account made before this feature: no saved guide choice, the tour long since dismissed.
        config = auth_file(tmp_path)
        config.pop("setup_guide")
        config["onboarding"] = {"state": "dismissed"}
        (tmp_path / "auth.json").write_text(json.dumps(config))
        app.extensions["reprise"].publisher.writes_enabled = True
        assert auth.get_setup_guide_state(configured=True) == "completed"
        return app, client

    def test_home_goes_straight_to_the_library(self, established):
        _app, client = established

        assert client.get("/").headers["Location"].endswith("/reprise")

    def test_the_status_is_completed_and_never_asks_to_open(self, established):
        _app, client = established

        guide = get_guide(client)

        assert (guide["state"], guide["auto_open"], guide["all_required_done"]) == ("completed", False, True)
        assert step(guide, "send_to_plex")["state"] == "done"
        assert step(guide, "connect_plex")["reason"] == "Connected to Home Plex."

    def test_nothing_is_written_or_reset_by_looking_at_it(self, established, tmp_path):
        _app, client = established
        files = ("auth.json", "settings.json")
        before = {name: hashlib.sha256((tmp_path / name).read_bytes()).hexdigest() for name in files}

        client.get("/")
        client.get("/setup-guide")
        get_guide(client)

        assert {name: hashlib.sha256((tmp_path / name).read_bytes()).hexdigest() for name in files} == before

    def test_even_with_the_library_folders_gone_it_does_not_reappear_by_itself(self, established, tmp_path):
        import shutil

        _app, client = established
        shutil.rmtree(tmp_path / "media")

        assert step(get_guide(client), "libraries_and_folders")["state"] == "todo"
        assert client.get("/").headers["Location"].endswith("/reprise")

    def test_an_old_account_with_no_server_yet_is_offered_it_because_setup_was_never_finished(self, make_app, tmp_path):
        client = make_app(settings={"setup_complete": False}).test_client()
        create_account(client)
        config = auth_file(tmp_path)
        config.pop("setup_guide")
        (tmp_path / "auth.json").write_text(json.dumps(config))

        assert get_guide(client)["state"] == "pending"
        assert client.get("/").headers["Location"].endswith("/setup-guide")


class TestOwnerOnlyAndCheap:
    def test_a_stranger_gets_401(self, make_app):
        client = make_app().test_client()
        create_account(client)

        stranger = make_app().test_client()

        assert stranger.get("/api/reprise/setup-guide").status_code == 401
        assert stranger.post("/api/reprise/setup-guide", json={"state": "dismissed"}).status_code == 401

    def test_a_playback_client_key_is_refused_with_403(self, make_app):
        app = make_app()
        client = app.test_client()
        create_account(client)
        secret = app.extensions["reprise"].store.create_client("Marquee")["secret"]

        for call in (
            lambda: client.get("/api/reprise/setup-guide", headers={"Authorization": f"Bearer {secret}"}),
            lambda: client.post(
                "/api/reprise/setup-guide", json={"state": "dismissed"}, headers={"Authorization": f"Bearer {secret}"}
            ),
        ):
            assert call().status_code == 403

    def test_only_dismissed_and_completed_can_be_saved(self, make_app):
        client = make_app().test_client()
        create_account(client)

        for body in ({"state": "pending"}, {"state": "whatever"}, {}, {"state": "dismissed", "extra": 1}, [], None):
            assert client.post("/api/reprise/setup-guide", json=body).status_code == 400

    def test_the_page_needs_a_signed_in_browser(self, make_app):
        client = make_app().test_client()
        create_account(client)

        assert make_app().test_client().get("/setup-guide").status_code == 302

    def test_looking_leaves_plex_s_database_folder_untouched(self, make_app, tmp_path):
        client = make_app(settings={"setup_complete": True}).test_client()
        create_account(client)
        plex = tmp_path / "plexcfg"
        (plex / "Plug-in Support" / "Databases").mkdir(parents=True)
        database = plex / "Plug-in Support" / "Databases" / "com.plexapp.plugins.library.db"
        database.write_bytes(b"not a real database, and it must stay that way")
        (plex / "Preferences.xml").write_text('<Preferences ProcessedMachineIdentifier="m"/>')
        seed_servers(plex_server(tmp_path, plex_config_folder=str(plex)))
        before = (sorted(p.name for p in database.parent.iterdir()), database.read_bytes(), database.stat().st_mtime_ns)

        sending = step(get_guide(client), "send_to_plex")

        assert sending["data"]["servers"][0]["problem"] is None
        assert (
            sorted(p.name for p in database.parent.iterdir()),
            database.read_bytes(),
            database.stat().st_mtime_ns,
        ) == before

    def test_a_probe_that_fails_does_not_make_the_endpoint_fail(self, make_app, monkeypatch):
        client = make_app().test_client()
        create_account(client)

        def boom():
            raise RuntimeError("probe hung")

        monkeypatch.setattr(acceleration, "status", boom)

        guide = get_guide(client)

        assert step(guide, "gpu")["state"] == "todo" and len(guide["steps"]) == 7

    def test_it_is_not_cached(self, make_app):
        client = make_app().test_client()
        create_account(client)

        assert client.get("/api/reprise/setup-guide").headers["Cache-Control"] == "no-store"


class TestSignedInTestClientsSkipIt:
    def test_the_shared_helper_marks_the_guide_skipped(self, make_app, tmp_path):
        client = sign_in(make_app().test_client())

        assert auth_file(tmp_path)["setup_guide"]["state"] == "dismissed"
        assert client.get("/").status_code == 302


class TestTheSharingSwitchSaysWhatIsMissing:
    """The refusal text is the guide's sentence, and it reaches the page instead of "Invalid request or operation"."""

    def test_the_refusal_reaches_the_client_in_plain_words(self, make_app, tmp_path):
        client = make_app(settings={"setup_complete": True}).test_client()
        create_account(client)
        seed_servers(plex_server(tmp_path))
        sentence = step(get_guide(client), "send_to_plex")["details"][0].removeprefix("Home Plex: ")

        answer = client.patch("/api/reprise/settings", json={"writes_enabled": True})

        assert answer.status_code == 400
        assert (
            answer.get_json()["error"] == f"Sending to Plex needs Plex's database, and Reprise can't see it: {sentence}"
        )
        assert client.get("/api/reprise/settings").get_json()["writes_enabled"] is False

    def test_other_bad_requests_keep_the_generic_answer(self, make_app):
        client = make_app(settings={"setup_complete": True}).test_client()
        create_account(client)

        answer = client.patch("/api/reprise/settings", json={"writes_enabled": "yes"})

        assert (answer.status_code, answer.get_json()) == (400, {"error": "Invalid request or operation"})
