"""The sign-in, first-run and setup-wizard pages, exercised through the real app (their IDs are what the scripts and e2e tests select)."""

from __future__ import annotations

import html as htmllib
import json
import re

import pytest

from tests.auth_helpers import OWNER_PASSWORD, OWNER_USERNAME, ensure_owner_account, sign_in


@pytest.fixture
def make_app(tmp_path, monkeypatch):
    from media_preview_generator.web import auth
    from media_preview_generator.web.app import create_app
    from media_preview_generator.web.routes import clear_gpu_cache, limiter
    from media_preview_generator.web.settings_manager import reset_settings_manager

    def build(*, csrf=False, setup_complete=False):
        monkeypatch.setattr(auth, "AUTH_FILE", str(tmp_path / "auth.json"))
        monkeypatch.setattr(auth, "CONFIG_DIR", str(tmp_path))
        monkeypatch.delenv("WEB_AUTH_TOKEN", raising=False)
        monkeypatch.delenv("AUTH_METHOD", raising=False)
        reset_settings_manager()
        clear_gpu_cache()
        (tmp_path / "settings.json").write_text(json.dumps({"setup_complete": setup_complete}))
        monkeypatch.setattr("media_preview_generator.web.app._prewarm_caches", lambda: None)
        flask_app = create_app(config_dir=str(tmp_path))
        flask_app.config["TESTING"] = True
        flask_app.config["WTF_CSRF_ENABLED"] = csrf
        limiter.reset()  # the 5-per-minute sign-in limit is counted per client address
        return flask_app

    yield build
    reset_settings_manager()


@pytest.fixture
def app(make_app):
    return make_app()


@pytest.fixture
def login_html(app) -> str:
    """The sign-in page a signed-out visitor gets once the owner account exists."""
    ensure_owner_account()
    response = app.test_client().get("/login")
    assert response.status_code == 200
    return response.get_data(as_text=True)


@pytest.fixture
def welcome_html(app) -> str:
    response = app.test_client().get("/welcome")
    assert response.status_code == 200
    return response.get_data(as_text=True)


@pytest.fixture
def setup_html(app) -> str:
    response = sign_in(app.test_client()).get("/setup")
    assert response.status_code == 200
    return response.get_data(as_text=True)


def _input(html: str, name: str) -> str:
    match = re.search(rf'<input[^>]*name="{name}"[^>]*>', html)
    assert match, f"no <input name={name!r}> on the page"
    return match.group(0)


def _post_login(client, **fields):
    return client.post("/login", data={"username": OWNER_USERNAME, "password": OWNER_PASSWORD, **fields})


class TestLoginPage:
    def test_asks_for_a_username_and_a_password_and_has_no_token_field(self, login_html):
        username = _input(login_html, "username")
        password = _input(login_html, "password")

        assert 'id="username"' in username and "required" in username and "autofocus" in username
        assert 'type="password"' in password and 'id="password"' in password and "required" in password
        assert 'name="token"' not in login_html
        assert 'id="token"' not in login_html
        assert 'name="csrf_token"' in login_html
        assert login_html.count('type="submit"') == 1

    def test_keep_me_signed_in_is_an_opt_out_checkbox_sending_remember_1(self, login_html):
        remember = _input(login_html, "remember")

        assert 'type="checkbox"' in remember
        assert 'value="1"' in remember
        assert "checked" in remember

    def test_reveal_toggle_is_a_non_submit_button_for_the_password_field(self, login_html):
        button = re.search(r'<button[^>]*data-reveal="password"[^>]*>', login_html).group(0)

        assert 'type="button"' in button
        assert 'aria-pressed="false"' in button

    def test_the_forgotten_password_help_is_collapsed_and_names_the_recovery_commands(self, login_html):
        details = re.search(r'<details[^>]*class="auth-help"[^>]*>', login_html).group(0)

        assert " open" not in details
        assert "account_cli reset-password" in login_html
        assert "account_cli reset-account" in login_html

    def test_wrong_credentials_give_a_401_danger_alert_and_mark_both_fields_invalid(self, app):
        ensure_owner_account()

        response = _post_login(app.test_client(), password="not-the-password")
        html = response.get_data(as_text=True)

        assert response.status_code == 401
        assert "isn't right" in html
        assert re.search(r'class="rp-message error"[^>]*role="alert"', html)
        assert 'aria-invalid="true"' in _input(html, "username")
        assert 'aria-invalid="true"' in _input(html, "password")
        assert re.search(r'<details[^>]*class="auth-help"[^>]* open', html)

    def test_a_failed_attempt_keeps_the_typed_username_but_never_the_password(self, app):
        ensure_owner_account()

        html = _post_login(app.test_client(), password="not-the-password").get_data(as_text=True)

        assert f'value="{OWNER_USERNAME}"' in _input(html, "username")
        assert "not-the-password" not in html

    def test_the_page_throttles_repeated_attempts_with_a_warning_not_the_invalid_state(self, app):
        ensure_owner_account()
        client = app.test_client()

        responses = [_post_login(client, password="not-the-password") for _ in range(6)]

        assert [r.status_code for r in responses[:5]] == [401] * 5
        assert responses[5].status_code == 429
        html = responses[5].get_data(as_text=True)
        assert "Too many attempts" in html
        assert "isn't right" not in html
        assert "aria-invalid" not in html
        assert re.search(r'<details[^>]*class="auth-help"[^>]* open', html)

    def test_a_form_posted_without_its_page_token_says_the_page_expired(self, make_app):
        client = make_app(csrf=True).test_client()
        ensure_owner_account()

        response = _post_login(client)

        assert response.status_code == 400
        assert "This page expired" in response.get_data(as_text=True)
        assert client.get("/reprise").status_code == 302

    def test_the_right_credentials_sign_in_and_the_page_then_goes_to_the_library(self, make_app):
        client = make_app(setup_complete=True).test_client()
        ensure_owner_account()

        response = _post_login(client)

        assert response.status_code == 302
        assert client.get("/login").status_code == 302
        assert client.get("/reprise").status_code == 200

    def test_setup_is_closed_note_replaces_the_form_error_when_welcome_is_posted_after_signup(self, app):
        ensure_owner_account()

        response = app.test_client().post(
            "/welcome", data={"username": "other", "password": "another-pass-1", "confirm_password": "another-pass-1"}
        )

        assert response.status_code == 403
        assert "Setup is already done" in response.get_data(as_text=True)


class TestWelcomePage:
    def test_asks_for_a_username_a_password_and_the_password_again(self, welcome_html):
        username = _input(welcome_html, "username")
        password = _input(welcome_html, "password")
        confirm = _input(welcome_html, "confirm_password")

        assert "required" in username and "autofocus" in username
        assert 'type="password"' in password and 'minlength="8"' in password
        assert 'type="password"' in confirm and "required" in confirm
        assert 'autocomplete="new-password"' in password and 'autocomplete="new-password"' in confirm
        assert 'name="csrf_token"' in welcome_html
        assert welcome_html.count('type="submit"') == 1

    def test_has_no_token_or_remember_field(self, welcome_html):
        assert 'name="token"' not in welcome_html
        assert 'name="remember"' not in welcome_html

    def test_one_reveal_toggle_covers_both_password_fields(self, welcome_html):
        button = re.search(r'<button[^>]*data-reveal="[^"]*"[^>]*>', welcome_html).group(0)

        assert 'data-reveal="password confirm_password"' in button
        assert 'type="button"' in button

    def test_a_mismatch_shows_the_error_keeps_the_username_and_makes_no_account(self, app, tmp_path):
        response = app.test_client().post(
            "/welcome",
            data={"username": "nitin", "password": "first-password-1", "confirm_password": "second-password-2"},
        )
        html = response.get_data(as_text=True)

        assert response.status_code == 400
        assert "don't match" in htmllib.unescape(html)
        assert 'value="nitin"' in _input(html, "username")
        assert "first-password-1" not in html
        assert not (tmp_path / "auth.json").exists()

    def test_creating_the_account_signs_you_in_and_closes_the_page(self, make_app):
        client = make_app(setup_complete=True).test_client()

        response = client.post(
            "/welcome",
            data={"username": "nitin", "password": "first-password-1", "confirm_password": "first-password-1"},
        )

        assert response.status_code == 302
        assert client.get("/reprise").status_code == 200
        assert client.get("/welcome").status_code == 302

    def test_a_form_posted_without_its_page_token_says_the_page_expired(self, make_app, tmp_path):
        client = make_app(csrf=True).test_client()

        response = client.post(
            "/welcome",
            data={"username": "nitin", "password": "first-password-1", "confirm_password": "first-password-1"},
        )

        assert response.status_code == 400
        assert "This page expired" in response.get_data(as_text=True)
        assert not (tmp_path / "auth.json").exists()


class TestSetupPage:
    def test_the_wizard_has_four_steps_and_no_security_step(self, setup_html):
        labels = re.findall(
            r'class="progress-step[^"]*" data-step="(\d)">\s*<div class="step-number">\d</div>\s*<span[^>]*>([^<]+)</span>',
            setup_html,
        )
        panels = re.findall(r'<div class="setup-step[^"]*" data-step="(\d)">', setup_html)

        assert labels == [("1", "Server type"), ("2", "Connect"), ("3", "Paths"), ("4", "Options")]
        assert panels == ["1", "2", "3", "4"]
        assert 'aria-valuemax="4"' in setup_html
        assert "Step 1 of 4" in setup_html
        assert "Security" not in setup_html

    def test_there_is_no_token_step_or_token_inputs(self, setup_html):
        for gone in ("newToken", "confirmToken", "step5Back", "step5Next", "finishSetup", "set-token", "token-info"):
            assert gone not in setup_html, gone

    def test_the_last_step_ends_with_a_primary_finish_setup_button(self, setup_html):
        button = re.search(r'<button[^>]*id="step4Next"[^>]*>(.*?)</button>', setup_html, re.S)

        assert button
        assert "btn-primary" in button.group(0)
        assert "btn-success" not in button.group(0)
        assert "Finish setup" in button.group(1)

    def test_skip_lives_inside_the_step_one_card_as_a_secondary_button(self, setup_html):
        step1 = setup_html[setup_html.index('data-step="1">\n') : setup_html.index("<!-- Step 2")]

        button = re.search(r'<button[^>]*id="skipSetupBtn"[^>]*>', step1).group(0)
        assert "btn-outline-secondary" in button
        assert "Skip for now" in step1
        assert step1.index("setup-card-footer") < step1.index('id="skipSetupBtn"')

    @pytest.mark.parametrize("vendor", ["plex", "emby", "jellyfin"])
    def test_vendor_cards_are_neutral_buttons_when_page_renders(self, setup_html, vendor):
        button = re.search(rf'<button[^>]*data-vendor="{vendor}"[^>]*>', setup_html).group(0)

        assert "wizard-vendor-btn" in button
        assert "btn-outline" not in button

    def test_every_wizard_control_id_survives_the_redesign(self, setup_html):
        ids = [
            *(f"step{n}Back" for n in range(2, 5)),
            *(f"step{n}Next" for n in range(1, 5)),
            "vendorPickerBackBtn",
            "manualPlexTestBtn",
            "serverUrlApply",
            "wizardPlexConfigFolder",
            "wizardPlexConfigFolderBrowseBtn",
            "setupAddPathMappingBtn",
            "setupCheckFile",
            "setupSamplePath",
            "setupAddExcludePathBtn",
            "libraryGrid",
            "librariesCard",
        ]

        missing = [i for i in ids if f'id="{i}"' not in setup_html]
        assert missing == []

    def test_path_mappings_and_excludes_sit_in_the_advanced_disclosure(self, setup_html):
        advanced = setup_html[setup_html.index('id="setupAdvancedDetails"') :]

        for control in ("setupAddPathMappingBtn", "setupAddExcludePathBtn"):
            assert f'id="{control}"' in advanced
        assert setup_html.index('id="setupCheckFile"') < setup_html.index('id="setupAdvancedDetails"')

    def test_slim_top_bar_replaces_the_app_navbar_during_setup(self, setup_html):
        assert 'id="setupLogoutBtn"' in setup_html
        assert 'id="setupThemeBtn"' in setup_html

    def test_the_wizard_needs_the_account_first(self, make_app):
        response = make_app().test_client().get("/setup")

        assert response.status_code == 302
        assert response.headers["Location"].endswith("/welcome")
