"""Tests for the lines ``initialize_auth()`` logs at startup.

They are the one place an operator is told how sign-in works after an upgrade or on a first start. Issue #263: a
user set ``WEB_AUTH_TOKEN`` but the generic banner made it look like the variable was ignored. These tests pin the
lines to what is really on disk and in the environment (first run, an existing account, the old token carried over
as the webhook secret, a retired token, ``AUTH_METHOD=external``), and pin that no line ever carries a full secret:
logs get pasted into bug reports.
"""

import json
from pathlib import Path

import pytest

from media_preview_generator.web.auth import get_webhook_secret, initialize_auth, webhook_secret_matches
from media_preview_generator.web.settings_manager import get_settings_manager, reset_settings_manager
from tests.auth_helpers import OWNER_PASSWORD, OWNER_USERNAME, ensure_owner_account

ENV_TOKEN = "env-secret-token-abcd1234"
FILE_TOKEN = "saved-token-wxyz-56789"
STORED_SECRET = "dedicated-secret-0123456789"

FIRST_RUN = "FIRST RUN: open the web interface and create your username and password."
CARRIED_OVER = "Carried the old sign-in token over as the webhook secret"
VARIABLE_BECAME_THE_SECRET = "WEB_AUTH_TOKEN is set; its value became the webhook secret"
VARIABLE_IGNORED = "WEB_AUTH_TOKEN is set but ignored"


@pytest.fixture
def config_dir(mock_auth_config, monkeypatch):
    """An empty config folder: auth.json and settings.json are made in it, and no variable from outside leaks in."""
    monkeypatch.delenv("WEB_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("AUTH_METHOD", raising=False)
    get_settings_manager(mock_auth_config)  # the webhook secret is saved in this folder's settings.json
    yield Path(mock_auth_config)
    reset_settings_manager()


def _settings(config_dir: Path) -> dict:
    return json.loads((config_dir / "settings.json").read_text())


def _auth_json(config_dir: Path) -> dict:
    return json.loads((config_dir / "auth.json").read_text())


def _write_auth_json(config_dir: Path, **extra) -> None:
    path = config_dir / "auth.json"
    current = json.loads(path.read_text()) if path.exists() else {}
    path.write_text(json.dumps({**current, **extra}))


def _start(loguru_caplog) -> str:
    """Run the startup step and return everything it logged as one string."""
    loguru_caplog.clear()
    initialize_auth()
    return loguru_caplog.text


def _messages(loguru_caplog, level: str) -> list[str]:
    return [r.getMessage() for r in loguru_caplog.records if r.levelname == level]


def _assert_no_secret_logged(out: str, config_dir: Path, *seeded: str) -> None:
    """No startup line carries a secret in full: not the one stored at the end, nor any that started the test."""
    stored = _settings(config_dir).get("webhook_secret")
    assert stored, "the startup step leaves a webhook secret behind"
    for secret in {stored, *seeded}:
        assert secret not in out


class TestFirstRun:
    def test_no_account_yet_says_to_create_one_in_the_web_interface(self, config_dir, loguru_caplog):
        out = _start(loguru_caplog)

        assert FIRST_RUN in out
        assert "Sign in as" not in out
        assert "Made a webhook secret" in out, "a new install gets a secret of its own"
        assert not _messages(loguru_caplog, "WARNING")
        _assert_no_secret_logged(out, config_dir)


class TestExistingAccount:
    def test_it_names_the_username_to_sign_in_as(self, config_dir, loguru_caplog):
        ensure_owner_account()

        out = _start(loguru_caplog)

        assert f"Sign in as '{OWNER_USERNAME}'" in out
        assert FIRST_RUN not in out
        assert not _messages(loguru_caplog, "WARNING")
        _assert_no_secret_logged(out, config_dir, OWNER_PASSWORD, _auth_json(config_dir)["account"]["password_hash"])


class TestOldTokenCarriedOver:
    def test_the_variable_becomes_the_webhook_secret_and_can_be_removed(self, config_dir, loguru_caplog, monkeypatch):
        monkeypatch.setenv("WEB_AUTH_TOKEN", ENV_TOKEN)

        out = _start(loguru_caplog)

        assert _settings(config_dir)["webhook_secret"] == ENV_TOKEN, "Sonarr, Radarr and scripts keep working"
        assert CARRIED_OVER in out
        assert VARIABLE_BECAME_THE_SECRET in out
        assert "remove the variable" in out
        assert VARIABLE_IGNORED not in out
        _assert_no_secret_logged(out, config_dir, ENV_TOKEN)

    def test_a_whitespace_padded_variable_is_trimmed(self, config_dir, loguru_caplog, monkeypatch):
        """A trailing newline/space in WEB_AUTH_TOKEN must not break matching.

        Headers are stripped on the way in, so the stored secret must be too —
        otherwise the env var silently 'doesn't work' (issue #263 shape).
        """
        monkeypatch.setenv("WEB_AUTH_TOKEN", "  padded-token-1234\n")

        out = _start(loguru_caplog)

        assert _settings(config_dir)["webhook_secret"] == "padded-token-1234"
        assert get_webhook_secret() == "padded-token-1234"
        assert webhook_secret_matches("padded-token-1234") is True
        assert VARIABLE_BECAME_THE_SECRET in out
        _assert_no_secret_logged(out, config_dir, "padded-token-1234")

    def test_a_blank_variable_counts_as_unset(self, config_dir, loguru_caplog, monkeypatch):
        """A blank/whitespace WEB_AUTH_TOKEN must not make the webhook secret blank or unusable."""
        _write_auth_json(config_dir, token=FILE_TOKEN)
        monkeypatch.setenv("WEB_AUTH_TOKEN", "   ")

        out = _start(loguru_caplog)

        assert _settings(config_dir)["webhook_secret"] == FILE_TOKEN, "falls back to the saved token"
        assert CARRIED_OVER in out
        assert "WEB_AUTH_TOKEN" not in out
        _assert_no_secret_logged(out, config_dir, FILE_TOKEN)

    def test_a_token_only_in_auth_json_is_carried_over_and_the_old_file_goes(self, config_dir, loguru_caplog):
        _write_auth_json(config_dir, token=FILE_TOKEN)

        out = _start(loguru_caplog)

        assert _settings(config_dir)["webhook_secret"] == FILE_TOKEN
        assert CARRIED_OVER in out
        assert "WEB_AUTH_TOKEN" not in out
        assert not (config_dir / "auth.json").exists(), "nothing else was in it, so the old token file is removed"
        assert not _messages(loguru_caplog, "WARNING")
        _assert_no_secret_logged(out, config_dir, FILE_TOKEN)

    def test_the_account_stays_in_auth_json_when_the_old_token_is_removed(self, config_dir, loguru_caplog):
        ensure_owner_account()
        _write_auth_json(config_dir, token=FILE_TOKEN)

        out = _start(loguru_caplog)

        left = _auth_json(config_dir)
        assert "token" not in left
        assert left["account"]["username"] == OWNER_USERNAME
        assert f"Sign in as '{OWNER_USERNAME}'" in out
        _assert_no_secret_logged(out, config_dir, FILE_TOKEN)

    def test_the_variable_wins_over_a_different_token_in_the_file_and_nobody_is_warned(
        self, config_dir, loguru_caplog, monkeypatch
    ):
        # The live install: the variable was set (so it always won) and auth.json holds a different, never-used token.
        _write_auth_json(config_dir, token=FILE_TOKEN)
        monkeypatch.setenv("WEB_AUTH_TOKEN", ENV_TOKEN)

        out = _start(loguru_caplog)

        assert _settings(config_dir)["webhook_secret"] == ENV_TOKEN, "the secret is what senders already use"
        assert VARIABLE_BECAME_THE_SECRET in out
        assert not _messages(loguru_caplog, "WARNING"), "the file's token was never in use, so nothing was lost"
        _assert_no_secret_logged(out, config_dir, ENV_TOKEN, FILE_TOKEN)


class TestRetiredToken:
    def test_a_token_in_auth_json_is_retired_with_a_warning_once_a_secret_was_set(self, config_dir, loguru_caplog):
        get_settings_manager().set("webhook_secret", STORED_SECRET)
        _write_auth_json(config_dir, token=FILE_TOKEN)

        out = _start(loguru_caplog)

        (warning,) = _messages(loguru_caplog, "WARNING")
        assert "retired" in warning
        assert str(config_dir / "auth.json") in warning, "the operator is told which file held the old token"
        assert "webhook secret was already set" in warning
        assert _settings(config_dir)["webhook_secret"] == STORED_SECRET, "the dedicated secret stays"
        assert not (config_dir / "auth.json").exists()
        _assert_no_secret_logged(out, config_dir, STORED_SECRET, FILE_TOKEN)

    def test_a_token_equal_to_the_stored_secret_is_removed_without_a_warning(self, config_dir, loguru_caplog):
        # Left behind by an earlier start that carried it over: nothing is lost by removing it.
        get_settings_manager().set("webhook_secret", STORED_SECRET)
        _write_auth_json(config_dir, token=STORED_SECRET)

        out = _start(loguru_caplog)

        assert not _messages(loguru_caplog, "WARNING")
        assert not (config_dir / "auth.json").exists()
        _assert_no_secret_logged(out, config_dir, STORED_SECRET)

    def test_the_variable_is_ignored_once_a_secret_was_set(self, config_dir, loguru_caplog, monkeypatch):
        get_settings_manager().set("webhook_secret", STORED_SECRET)
        monkeypatch.setenv("WEB_AUTH_TOKEN", ENV_TOKEN)

        out = _start(loguru_caplog)

        assert VARIABLE_IGNORED in out
        assert "remove the variable" in out
        assert VARIABLE_BECAME_THE_SECRET not in out
        assert _settings(config_dir)["webhook_secret"] == STORED_SECRET
        _assert_no_secret_logged(out, config_dir, STORED_SECRET, ENV_TOKEN)


class TestExternalAuth:
    @pytest.fixture(autouse=True)
    def _behind_a_proxy(self, config_dir, monkeypatch):
        monkeypatch.setenv("AUTH_METHOD", "external")

    def test_it_says_sign_in_is_off_and_never_mentions_an_account(self, config_dir, loguru_caplog, monkeypatch):
        """AUTH_METHOD=external: no account, no sign-in instructions, no word about the old variable."""
        monkeypatch.setenv("WEB_AUTH_TOKEN", ENV_TOKEN)

        out = _start(loguru_caplog)

        assert "AUTH_METHOD=external" in out
        assert "authentication DISABLED" in out
        assert {r.levelname for r in loguru_caplog.records if "DISABLED" in r.getMessage()} == {"WARNING"}
        assert FIRST_RUN not in out
        assert "Sign in as" not in out
        assert "WEB_AUTH_TOKEN is set" not in out
        _assert_no_secret_logged(out, config_dir, ENV_TOKEN)

    def test_webhook_senders_still_get_the_old_token_as_their_secret(self, config_dir, loguru_caplog, monkeypatch):
        # Webhooks keep their own secret behind a proxy, so the upgrade carries the token over here too.
        monkeypatch.setenv("WEB_AUTH_TOKEN", ENV_TOKEN)

        out = _start(loguru_caplog)

        assert _settings(config_dir)["webhook_secret"] == ENV_TOKEN
        assert CARRIED_OVER in out
        _assert_no_secret_logged(out, config_dir, ENV_TOKEN)
