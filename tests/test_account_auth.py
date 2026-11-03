"""The owner account: first-run setup, sign-in, the session, the tour's memory, and the old token's migration.

Every test builds the real app in a fresh config folder (what a container's first start does) and drives it with a
test client; nothing about sign-in is mocked.
"""

import base64
import html
import json
import os
import re

import pytest

from tests.auth_helpers import OWNER_PASSWORD, OWNER_USERNAME, sign_in

PASSWORD = "correct horse battery"
OLD_TOKEN = "token-sonarr-and-radarr-already-use"
STALE_FILE_TOKEN = "auto-generated-token-the-env-var-was-hiding"


@pytest.fixture
def make_app(tmp_path, monkeypatch):
    """Build the app like a container's first start: ``settings``/``auth_json`` are what is already in /config."""
    from media_preview_generator.web import auth, settings_manager
    from media_preview_generator.web.app import create_app
    from media_preview_generator.web.routes import clear_gpu_cache, limiter

    def build(*, settings=None, auth_json=None, env=None, csrf=False):
        monkeypatch.setattr(auth, "AUTH_FILE", str(tmp_path / "auth.json"))
        monkeypatch.setattr(auth, "CONFIG_DIR", str(tmp_path))
        monkeypatch.delenv("WEB_AUTH_TOKEN", raising=False)
        monkeypatch.delenv("AUTH_METHOD", raising=False)
        for key, value in (env or {}).items():
            monkeypatch.setenv(key, value)
        settings_manager.reset_settings_manager()
        clear_gpu_cache()
        (tmp_path / "settings.json").write_text(json.dumps({"setup_complete": True, **(settings or {})}))
        if auth_json is not None:
            (tmp_path / "auth.json").write_text(json.dumps(auth_json))
        monkeypatch.setattr("media_preview_generator.web.app._prewarm_caches", lambda: None)
        app = create_app(config_dir=str(tmp_path))
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = csrf
        monkeypatch.setattr(limiter, "enabled", False)
        return app

    yield build
    settings_manager.reset_settings_manager()


@pytest.fixture
def app(make_app):
    """A brand-new install: nothing in /config yet."""
    return make_app()


@pytest.fixture
def client(app):
    return app.test_client()


def _auth_file(tmp_path) -> dict:
    return json.loads((tmp_path / "auth.json").read_text())


def _settings_file(tmp_path) -> dict:
    return json.loads((tmp_path / "settings.json").read_text())


def create_account(client, username="nitin", password=PASSWORD, confirm=None):
    return client.post(
        "/welcome",
        data={"username": username, "password": password, "confirm_password": password if confirm is None else confirm},
    )


def sign_in_with_form(client, username="nitin", password=PASSWORD, **extra):
    return client.post("/login", data={"username": username, "password": password, **extra})


def webhook(client, secret, *, header="X-Auth-Token"):
    return client.post("/api/webhooks/sonarr", json={"eventType": "Test"}, headers={header: secret})


@pytest.fixture
def owner(client):
    """The owner account exists and ``client`` is signed in as it."""
    assert create_account(client).status_code == 302
    return client


class TestFirstRun:
    @pytest.mark.parametrize("path", ["/", "/reprise", "/settings", "/servers", "/login", "/logout", "/setup"])
    def test_every_page_sends_you_to_the_welcome_page(self, client, path):
        resp = client.get(path)

        assert resp.status_code == 302
        assert resp.headers["Location"].endswith("/welcome")

    def test_welcome_page_asks_for_a_username_and_a_password_twice(self, client):
        html = client.get("/welcome").get_data(as_text=True)

        assert 'name="username"' in html
        assert 'name="password"' in html
        assert 'name="confirm_password"' in html
        assert 'minlength="8"' in html
        assert 'name="token"' not in html

    def test_api_routes_answer_for_themselves_instead_of_redirecting(self, client):
        resp = client.get("/api/jobs")

        assert resp.status_code == 401
        assert resp.get_json() == {"error": "Authentication required"}

    def test_creating_the_account_signs_you_in_and_opens_the_library(self, client, tmp_path):
        resp = create_account(client)

        assert resp.status_code == 302
        assert resp.headers["Location"].endswith("/")
        assert client.get("/reprise").status_code == 200

    def test_the_password_is_stored_only_as_a_hash(self, client, tmp_path):
        create_account(client)

        raw = (tmp_path / "auth.json").read_text()
        account = json.loads(raw)["account"]
        assert PASSWORD not in raw
        assert account["username"] == "nitin"
        assert account["password_hash"].startswith("scrypt:")

    def test_the_same_password_hashes_differently_each_time(self, client, tmp_path, app):
        from media_preview_generator.web import auth

        create_account(client)
        first = _auth_file(tmp_path)["account"]["password_hash"]
        auth.reset_account()
        create_account(client)

        assert _auth_file(tmp_path)["account"]["password_hash"] != first

    @pytest.mark.parametrize(
        ("username", "password", "confirm", "message"),
        [
            ("nitin", "short", "short", "at least 8 characters"),
            ("nitin", PASSWORD, "something else entirely", "don't match"),
            ("   ", PASSWORD, PASSWORD, "Choose a username"),
            ("nitin-the-owner", "NITIN-THE-OWNER", "NITIN-THE-OWNER", "same as the username"),
        ],
    )
    def test_a_bad_username_or_password_is_refused_with_the_reason(
        self, client, tmp_path, username, password, confirm, message
    ):
        resp = create_account(client, username, password, confirm)

        assert resp.status_code == 400
        assert message in html.unescape(resp.get_data(as_text=True))
        assert not (tmp_path / "auth.json").exists()
        assert client.get("/reprise").status_code == 302  # still not signed in


class TestSetupClosesForGood:
    def test_a_second_setup_changes_nothing(self, owner, app, tmp_path):
        stranger = app.test_client()

        resp = create_account(stranger, "intruder", "intruder-password")

        assert resp.status_code == 403
        assert sign_in_with_form(app.test_client(), "intruder", "intruder-password").status_code == 401
        assert sign_in_with_form(app.test_client()).status_code == 302
        assert _auth_file(tmp_path)["account"]["username"] == "nitin"

    def test_the_welcome_page_is_gone_once_an_account_exists(self, owner, app):
        assert owner.get("/welcome").status_code == 302
        assert owner.get("/welcome").headers["Location"].endswith("/")
        assert app.test_client().get("/welcome").status_code == 302

    def test_the_sign_in_page_replaces_it_for_everyone_else(self, owner, app):
        resp = app.test_client().get("/login")

        assert resp.status_code == 200
        assert 'name="username"' in resp.get_data(as_text=True)

    def test_a_damaged_account_file_keeps_setup_closed_instead_of_inviting_a_new_account(self, owner, app, tmp_path):
        from media_preview_generator.web import account_cli

        (tmp_path / "auth.json").write_text("{ this is not json")  # e.g. unreadable after a PUID change
        stranger = app.test_client()

        assert stranger.get("/reprise").headers["Location"].endswith("/login"), "not the welcome page"
        assert stranger.get("/welcome").status_code == 302
        assert create_account(stranger, "intruder", "intruder-password").status_code == 403
        assert "can't read its account file" in html.unescape(stranger.get("/login").get_data(as_text=True))
        assert (tmp_path / "auth.json").read_text() == "{ this is not json", "nothing was overwritten"

        assert account_cli.main(["reset-account"]) == 0  # the documented way out
        assert app.test_client().get("/reprise").headers["Location"].endswith("/welcome")


class TestSignIn:
    @pytest.fixture(autouse=True)
    def _account(self, owner):
        owner.post("/logout")

    def test_the_wrong_password_is_rejected(self, app):
        browser = app.test_client()

        resp = sign_in_with_form(browser, password="correct horse staple")

        assert resp.status_code == 401
        assert "isn't right" in html.unescape(resp.get_data(as_text=True))
        assert browser.get("/reprise").status_code == 302

    def test_after_a_miss_the_username_is_kept_and_the_password_gets_the_cursor(self, app):
        page = sign_in_with_form(app.test_client(), password="correct horse staple").get_data(as_text=True)

        username_input = re.search(r'<input id="username"[^>]*>', page, re.S).group(0)
        password_input = re.search(r'<input id="password"[^>]*>', page, re.S).group(0)
        assert 'value="nitin"' in username_input and "autofocus" not in username_input
        assert "autofocus" in password_input

    @pytest.mark.parametrize("username", ["somebody-else", "", "nitin "])
    def test_only_the_owners_username_gets_in(self, app, username):
        browser = app.test_client()

        resp = sign_in_with_form(browser, username)

        assert (resp.status_code == 302) == (username.strip() == "nitin")
        assert (browser.get("/reprise").status_code == 200) == (username.strip() == "nitin")

    def test_the_right_password_signs_in(self, app):
        browser = app.test_client()

        resp = sign_in_with_form(browser)

        assert resp.status_code == 302
        assert browser.get("/reprise").status_code == 200

    def test_the_username_ignores_capitals(self, app):
        assert sign_in_with_form(app.test_client(), "NITIN").status_code == 302

    def test_signing_out_ends_the_session(self, app):
        browser = app.test_client()
        sign_in_with_form(browser)

        browser.post("/logout")

        assert browser.get("/reprise").status_code == 302

    def test_keep_me_signed_in_makes_a_cookie_that_outlives_the_browser(self, app):
        kept = sign_in_with_form(app.test_client(), remember="1")
        not_kept = sign_in_with_form(app.test_client())

        def session_cookie(resp):
            return next(c for c in resp.headers.getlist("Set-Cookie") if c.startswith("reprise_session="))

        assert "Expires=" in session_cookie(kept)
        assert "Expires=" not in session_cookie(not_kept)

    def test_the_session_cookie_has_a_name_no_other_app_on_the_host_uses(self, app):
        # Cookies are per host, not per port: a plain "session" would be replaced by any other Flask app's.
        cookies = sign_in_with_form(app.test_client()).headers.getlist("Set-Cookie")

        assert any(c.startswith("reprise_session=") for c in cookies)
        assert not any(c.startswith("session=") for c in cookies)

    def test_the_json_login_takes_the_same_credentials(self, app):
        browser = app.test_client()

        wrong = browser.post("/api/auth/login", json={"username": "nitin", "password": "nope-nope-nope"})
        right = browser.post("/api/auth/login", json={"username": "nitin", "password": PASSWORD})

        assert wrong.status_code == 401
        assert wrong.get_json()["success"] is False
        assert right.get_json() == {"success": True}
        assert browser.get("/api/auth/status").get_json()["authenticated"] is True

    def test_a_cookie_from_the_old_token_login_is_refused(self, app):
        browser = app.test_client()
        with browser.session_transaction() as sess:
            sess["authenticated"] = True  # what the token login stored: no account epoch

        assert browser.get("/reprise").status_code == 302
        assert browser.get("/api/jobs").status_code == 401

    def test_the_sign_in_page_no_longer_asks_for_a_token(self, app):
        html = app.test_client().get("/login").get_data(as_text=True)

        assert 'name="token"' not in html
        assert "WEB_AUTH_TOKEN" not in html
        assert "auth.json" in html  # the forgotten-password help names the file to delete


class TestChangePassword:
    def test_it_needs_a_signed_in_browser_not_the_webhook_secret(self, owner, app):
        from media_preview_generator.web.auth import get_webhook_secret

        resp = app.test_client().post(
            "/api/account/password",
            json={"current_password": PASSWORD, "new_password": "a brand new password"},
            headers={"X-Auth-Token": get_webhook_secret()},
        )

        assert resp.status_code == 401

    def test_the_current_password_must_be_right(self, owner):
        resp = owner.post(
            "/api/account/password", json={"current_password": "not it at all", "new_password": "a brand new password"}
        )

        assert resp.status_code == 400
        assert "current password" in resp.get_json()["error"]

    def test_the_new_password_follows_the_same_rules(self, owner):
        resp = owner.post("/api/account/password", json={"current_password": PASSWORD, "new_password": "short"})

        assert resp.status_code == 400
        assert "at least 8" in resp.get_json()["error"]

    def test_the_new_password_works_the_old_one_does_not_and_other_browsers_are_signed_out(self, owner, app):
        other = app.test_client()
        sign_in_with_form(other)
        assert other.get("/reprise").status_code == 200

        resp = owner.post(
            "/api/account/password", json={"current_password": PASSWORD, "new_password": "a brand new password"}
        )

        assert resp.get_json() == {"success": True}
        assert owner.get("/reprise").status_code == 200, "the browser that changed it stays signed in"
        assert other.get("/reprise").status_code == 302, "every other browser is signed out"
        assert sign_in_with_form(app.test_client()).status_code == 401
        assert sign_in_with_form(app.test_client(), password="a brand new password").status_code == 302

    def test_the_panel_is_in_settings_and_the_token_card_is_gone(self, owner):
        html = owner.get("/settings").get_data(as_text=True)

        assert 'id="changePasswordForm"' in html
        assert "data-tour-open" in html
        assert "regenerateToken" not in html
        assert "customAuthToken" not in html


class TestOldTokenBecomesTheWebhookSecret:
    def test_what_sonarr_and_radarr_send_today_still_works_after_the_upgrade(self, make_app, tmp_path):
        # The live install: WEB_AUTH_TOKEN is set (so it won) and auth.json holds a different, never-used token.
        app = make_app(auth_json={"token": STALE_FILE_TOKEN}, env={"WEB_AUTH_TOKEN": OLD_TOKEN})
        client = app.test_client()

        assert webhook(client, OLD_TOKEN).status_code == 200
        assert (
            webhook(
                client, "Basic " + base64.b64encode(f":{OLD_TOKEN}".encode()).decode(), header="Authorization"
            ).status_code
            == 200
        )  # Sonarr: blank Username, token as Password
        assert (
            client.post(
                "/api/webhooks/radarr", json={"eventType": "Test"}, headers={"Authorization": f"Bearer {OLD_TOKEN}"}
            ).status_code
            == 200
        )
        assert client.post("/api/webhooks/radarr?token=" + OLD_TOKEN, json={"eventType": "Test"}).status_code == 200
        assert webhook(client, STALE_FILE_TOKEN).status_code == 401
        assert webhook(client, "some-other-token").status_code == 401
        assert _settings_file(tmp_path)["webhook_secret"] == OLD_TOKEN
        assert not (tmp_path / "auth.json").exists() or "token" not in _auth_file(tmp_path)

    def test_the_token_in_auth_json_is_used_when_no_variable_was_set(self, make_app, tmp_path):
        app = make_app(auth_json={"token": STALE_FILE_TOKEN})

        assert webhook(app.test_client(), STALE_FILE_TOKEN).status_code == 200
        assert _settings_file(tmp_path)["webhook_secret"] == STALE_FILE_TOKEN

    def test_webhooks_work_before_anyone_has_made_an_account(self, make_app):
        app = make_app(auth_json={"token": STALE_FILE_TOKEN})
        client = app.test_client()

        assert client.get("/").status_code == 302  # the pages ask for an account...
        assert webhook(client, STALE_FILE_TOKEN).status_code == 200  # ...but the live webhooks are not blocked

    def test_a_webhook_secret_that_was_already_set_wins_and_the_old_token_is_retired(self, make_app, tmp_path):
        app = make_app(settings={"webhook_secret": "dedicated-secret-1234"}, auth_json={"token": STALE_FILE_TOKEN})
        client = app.test_client()

        assert webhook(client, "dedicated-secret-1234").status_code == 200
        assert webhook(client, STALE_FILE_TOKEN).status_code == 401
        assert not (tmp_path / "auth.json").exists() or "token" not in _auth_file(tmp_path)

    def test_a_new_install_gets_a_random_secret(self, app, tmp_path):
        secret = _settings_file(tmp_path)["webhook_secret"]

        assert len(secret) >= 32
        assert webhook(app.test_client(), secret).status_code == 200

    def test_it_happens_once_a_later_start_keeps_the_secret(self, make_app, tmp_path):
        make_app(env={"WEB_AUTH_TOKEN": OLD_TOKEN})
        first = _settings_file(tmp_path)["webhook_secret"]

        again = make_app(env={"WEB_AUTH_TOKEN": "a-different-value-set-later"}, settings={"webhook_secret": first})

        assert webhook(again.test_client(), first).status_code == 200
        assert webhook(again.test_client(), "a-different-value-set-later").status_code == 401

    def test_a_settings_file_that_lost_the_secret_gets_a_new_one_without_a_restart(self, app, tmp_path):
        from media_preview_generator.web.settings_manager import get_settings_manager

        get_settings_manager().delete("webhook_secret")  # a restored backup from before the secret existed

        assert webhook(app.test_client(), "whatever-the-sender-has").status_code == 401
        remade = _settings_file(tmp_path)["webhook_secret"]
        assert len(remade) >= 32, "so the owner can copy it from Settings > Connections"
        assert webhook(app.test_client(), remade).status_code == 200

    def test_scripts_keep_using_it_as_the_api_header(self, app, tmp_path):
        secret = _settings_file(tmp_path)["webhook_secret"]

        assert app.test_client().get("/api/jobs", headers={"X-Auth-Token": secret}).status_code == 200
        assert app.test_client().get("/api/jobs", headers={"X-Auth-Token": "wrong"}).status_code == 401

    def test_plex_gets_the_same_secret_in_the_url_it_registers(self, app, tmp_path):
        from media_preview_generator.web.routes.api_plex_webhook import _plex_webhook_auth_token

        assert _plex_webhook_auth_token() == _settings_file(tmp_path)["webhook_secret"]


class TestWebhookSecretPanel:
    def test_the_connections_page_shows_it_masked_and_never_in_the_page_source(self, owner, tmp_path):
        secret = _settings_file(tmp_path)["webhook_secret"]

        html = owner.get("/servers").get_data(as_text=True)

        assert 'id="webhookSecretPanel"' in html
        assert secret not in html
        assert "\u2022" * 8 + secret[-4:] in html

    def test_reveal_needs_a_signed_in_browser(self, owner, app, tmp_path):
        secret = _settings_file(tmp_path)["webhook_secret"]

        by_script = app.test_client().post("/api/webhook-secret/reveal", headers={"X-Auth-Token": secret})
        by_browser = owner.post("/api/webhook-secret/reveal")

        assert by_script.status_code == 401
        assert by_browser.get_json() == {"secret": secret}
        assert by_browser.headers["Cache-Control"] == "no-store"

    def test_regenerate_replaces_it_everywhere_and_re_registers_plex(self, owner, app, tmp_path, monkeypatch):
        old = _settings_file(tmp_path)["webhook_secret"]
        reregistered = []
        monkeypatch.setattr(
            "media_preview_generator.web.routes.api_settings._reregister_plex_webhooks_after_secret_rotation",
            lambda settings: reregistered.append(settings.get("webhook_secret")),
        )

        body = owner.post("/api/webhook-secret/regenerate").get_json()

        new = body["secret"]
        assert new != old
        assert body["masked"].endswith(new[-4:])
        assert reregistered == [new], "Plex's registered webhook must carry the new secret"
        assert webhook(app.test_client(), new).status_code == 200
        assert webhook(app.test_client(), old).status_code == 401
        assert _settings_file(tmp_path)["webhook_secret"] == new

    def test_regenerate_needs_a_signed_in_browser(self, owner, app, tmp_path):
        secret = _settings_file(tmp_path)["webhook_secret"]

        resp = app.test_client().post("/api/webhook-secret/regenerate", headers={"X-Auth-Token": secret})

        assert resp.status_code == 401
        assert _settings_file(tmp_path)["webhook_secret"] == secret


class TestOnboarding:
    def test_a_new_account_is_offered_the_tour_straight_away(self, owner):
        html = owner.get("/reprise").get_data(as_text=True)

        assert 'id="repriseTour"' in html
        assert 'data-auto="true"' in html

    def test_the_tour_has_one_way_out_and_it_is_skip(self, owner):
        page = owner.get("/reprise").get_data(as_text=True)

        assert "data-tour-skip" in page
        assert "data-tour-never" not in page and "Don't show again" not in html.unescape(page)

    def test_skipping_is_remembered_by_the_server_for_every_browser(self, owner, app, tmp_path):
        assert owner.post("/api/account/onboarding", json={"state": "dismissed"}).get_json() == {"state": "dismissed"}

        assert _auth_file(tmp_path)["onboarding"]["state"] == "dismissed"
        elsewhere = app.test_client()
        sign_in_with_form(elsewhere)
        html = elsewhere.get("/reprise").get_data(as_text=True)
        assert 'data-auto="false"' in html
        assert 'id="repriseTour"' in html, "still there to re-open"

    def test_it_stays_dismissed_after_a_restart(self, owner, make_app, tmp_path):
        owner.post("/api/account/onboarding", json={"state": "dismissed"})

        restarted = make_app(auth_json=_auth_file(tmp_path), settings={"webhook_secret": "kept-secret-12345678"})
        browser = restarted.test_client()
        sign_in_with_form(browser)

        assert 'data-auto="false"' in browser.get("/reprise").get_data(as_text=True)

    def test_finishing_the_tour_also_stops_it_opening_by_itself(self, owner):
        owner.post("/api/account/onboarding", json={"state": "completed"})

        assert 'data-auto="false"' in owner.get("/reprise").get_data(as_text=True)
        assert owner.get("/api/account/onboarding").get_json() == {"state": "completed"}

    def test_it_can_be_opened_again_from_settings(self, owner):
        owner.post("/api/account/onboarding", json={"state": "dismissed"})

        assert "data-tour-open" in owner.get("/settings").get_data(as_text=True)

    def test_only_two_answers_are_accepted_and_only_from_a_signed_in_browser(self, owner, app):
        assert owner.post("/api/account/onboarding", json={"state": "whatever"}).status_code == 400
        assert owner.post("/api/account/onboarding", json={"state": "pending"}).status_code == 400
        assert app.test_client().post("/api/account/onboarding", json={"state": "dismissed"}).status_code == 401

    def test_it_can_be_saved_before_the_server_wizard_is_finished(self, make_app):
        browser = make_app(settings={"setup_complete": False}).test_client()  # the gate sends pages to /setup
        assert create_account(browser).status_code == 302

        resp = browser.post("/api/account/onboarding", json={"state": "dismissed"})

        assert resp.status_code == 200
        assert resp.get_json() == {"state": "dismissed"}

    def test_a_new_account_starts_the_tour_again(self, owner, tmp_path):
        from media_preview_generator.web import auth

        owner.post("/api/account/onboarding", json={"state": "dismissed"})
        auth.reset_account()
        create_account(owner)

        assert 'data-auto="true"' in owner.get("/reprise").get_data(as_text=True)

    def test_the_server_wizard_no_longer_has_a_token_step(self, owner):
        html = owner.get("/setup?rerun=1").get_data(as_text=True)

        assert 'data-step="4"' in html
        assert 'data-step="5"' not in html
        assert "newToken" not in html


class TestRecoveryFromAForgottenPassword:
    def test_reset_account_returns_to_the_welcome_page_and_leaves_the_webhook_secret_alone(self, owner, app, tmp_path):
        from media_preview_generator.web import account_cli

        secret = _settings_file(tmp_path)["webhook_secret"]

        assert account_cli.main(["reset-account"]) == 0

        assert owner.get("/reprise").headers["Location"].endswith("/welcome"), "the old session no longer counts"
        assert create_account(app.test_client(), "newname", "a new password!").status_code == 302
        assert webhook(app.test_client(), secret).status_code == 200

    def test_deleting_auth_json_does_the_same(self, owner, app, tmp_path):
        os.unlink(tmp_path / "auth.json")

        assert app.test_client().get("/reprise").headers["Location"].endswith("/welcome")

    def test_reset_password_keeps_the_username_and_signs_every_browser_out(self, owner, app, monkeypatch, tmp_path):
        from media_preview_generator.web import account_cli

        monkeypatch.setattr("getpass.getpass", lambda prompt="": "the new password")

        assert account_cli.main(["reset-password"]) == 0

        assert owner.get("/reprise").status_code == 302
        assert sign_in_with_form(app.test_client()).status_code == 401
        assert sign_in_with_form(app.test_client(), password="the new password").status_code == 302
        assert _auth_file(tmp_path)["account"]["username"] == "nitin"

    def test_reset_password_refuses_a_mismatch_and_a_missing_account(self, owner, app, monkeypatch, capsys):
        from media_preview_generator.web import account_cli, auth

        answers = iter(["one password", "another password"])
        monkeypatch.setattr("getpass.getpass", lambda prompt="": next(answers))
        assert account_cli.main(["reset-password"]) == 1
        assert sign_in_with_form(app.test_client(), password=PASSWORD).status_code == 302, "nothing was changed"

        auth.reset_account()
        monkeypatch.setattr("getpass.getpass", lambda prompt="": "some new password")
        assert account_cli.main(["reset-password"]) == 1
        assert "no account" in capsys.readouterr().err


class TestExternalAuth:
    def test_a_proxy_in_front_means_no_account_and_no_welcome_page(self, make_app):
        client = make_app(env={"AUTH_METHOD": "external"}).test_client()

        assert client.get("/reprise").status_code == 200
        assert client.get("/welcome").status_code == 302
        assert client.get("/welcome").headers["Location"].endswith("/")

    def test_there_is_no_password_to_change(self, make_app):
        client = make_app(env={"AUTH_METHOD": "external"}).test_client()

        assert (
            client.post("/api/account/password", json={"current_password": "x", "new_password": "y" * 9}).status_code
            == 409
        )
        assert "changePasswordForm" not in client.get("/settings").get_data(as_text=True)


class TestFormsNeedThePagesToken:
    def test_the_welcome_form_without_its_token_is_refused_and_creates_nothing(self, make_app, tmp_path):
        client = make_app(csrf=True).test_client()

        resp = create_account(client)

        assert resp.status_code == 400
        assert "expired" in resp.get_data(as_text=True)
        assert not (tmp_path / "auth.json").exists()

    def test_the_sign_in_form_without_its_token_is_refused_and_signs_nobody_in(self, make_app):
        from media_preview_generator.web import auth

        client = make_app(csrf=True).test_client()
        auth.create_account("nitin", PASSWORD)

        resp = client.post("/login", data={"username": "nitin", "password": PASSWORD})

        assert resp.status_code == 400
        assert "expired" in resp.get_data(as_text=True)
        assert client.get("/reprise").status_code == 302


class TestHelpers:
    def test_sign_in_helper_makes_a_browser_the_app_accepts(self, app):
        browser = sign_in(app.test_client())

        assert browser.get("/reprise").status_code == 200
        from media_preview_generator.web import auth

        assert auth.get_username() == OWNER_USERNAME
        assert auth.verify_login(OWNER_USERNAME, OWNER_PASSWORD)
