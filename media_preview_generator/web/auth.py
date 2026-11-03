"""Owner account, sign-in session and webhook secret for the web interface.

Two separate credentials, so neither doubles as the other:

* **The owner account** (one username and password) signs a person in through the browser. It lives in ``auth.json``
  in the config folder; the password is stored only as a salted scrypt hash. The browser session is a signed cookie
  that carries the account's ``epoch``, a random value replaced whenever the password changes or a new account is
  made, so a cookie from before that moment stops working. Deleting ``auth.json`` (or the CLI in
  ``account_cli.py``) returns the app to the first-run page, where a new account is made; no integration changes.
* **The webhook secret** (``webhook_secret`` in ``settings.json``) is what Sonarr, Radarr, Plex, Emby, Jellyfin and
  scripts send in ``X-Auth-Token`` / ``Authorization: Bearer`` (or Plex's ``?token=``). The first start after
  upgrading from the token login copies the old token into it unchanged, so those senders need no changes.
"""

import hmac
import json
import os
import secrets
import threading
import unicodedata
from datetime import UTC, datetime
from functools import wraps

from flask import jsonify, redirect, request, session, url_for
from loguru import logger
from werkzeug.security import check_password_hash, generate_password_hash

# Default config directory
CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")
AUTH_FILE = os.path.join(CONFIG_DIR, "auth.json")

# Auth method constants (mirrors *arr ecosystem AuthenticationMethod convention)
AUTH_METHOD_INTERNAL = "internal"
AUTH_METHOD_EXTERNAL = "external"

MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 256
MAX_USERNAME_LENGTH = 64

ONBOARDING_PENDING = "pending"
ONBOARDING_DISMISSED = "dismissed"
ONBOARDING_COMPLETED = "completed"
ONBOARDING_STATES = (ONBOARDING_PENDING, ONBOARDING_DISMISSED, ONBOARDING_COMPLETED)

_account_lock = threading.RLock()
_secret_lock = threading.RLock()
_warned_unreadable: set[tuple[str, str]] = set()
_dummy_hash: str | None = None


class AccountError(ValueError):
    """A username or password isn't acceptable; the message says why, in words for the person typing it."""


class AccountExistsError(RuntimeError):
    """First-run setup is closed: an owner account already exists."""


def get_config_dir() -> str:
    """Get the configuration directory path."""
    return CONFIG_DIR


def generate_secret() -> str:
    """Generate a cryptographically secure random secret."""
    return secrets.token_urlsafe(32)


# ---------------------------------------------------------------------------
# auth.json
# ---------------------------------------------------------------------------


def _read_auth_file() -> tuple[dict, bool]:
    """Read ``auth.json``: ``(contents, readable)``.

    A missing file is fine (nothing has been made yet). A file that exists but can't be read, or isn't a JSON object,
    is not: that is a damaged account, never an empty one (see ``is_setup_required``). Read afresh on every call (a few
    hundred bytes), so a change made by another process, such as the recovery command, applies at once.
    """
    path = AUTH_FILE
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
    except FileNotFoundError:
        return {}, True
    except (OSError, ValueError) as e:
        key = (path, f"{type(e).__name__}: {e}")
        if key not in _warned_unreadable:
            _warned_unreadable.add(key)
            logger.warning(
                "Could not read the saved sign-in account at {} ({}: {}). Nobody can sign in, and the first-run "
                "page stays closed so no one can take its place. Fix the file's permissions (did PUID or PGID "
                "change?) or restore it from a backup; to start over, run the account_cli reset-account command "
                "or delete the file.",
                path,
                type(e).__name__,
                e,
            )
        return {}, False
    return data, True


def load_auth_config() -> dict:
    """The contents of ``auth.json``: ``{}`` when it is missing or can't be read."""
    return _read_auth_file()[0]


def auth_file_unreadable() -> bool:
    """Whether ``auth.json`` exists but can't be read (permissions, damage): the account is there, just out of reach."""
    return not _read_auth_file()[1]


def save_auth_config(config: dict) -> None:
    """Write ``auth.json`` atomically (mode 0600).

    Raises:
        OSError: The config folder isn't writable; callers tell the person, never carry on as if it was saved.
    """
    from ..utils import atomic_json_save

    os.makedirs(os.path.dirname(AUTH_FILE), exist_ok=True)
    atomic_json_save(AUTH_FILE, config, permissions=0o600)


# ---------------------------------------------------------------------------
# Owner account
# ---------------------------------------------------------------------------


_ACCOUNT_KEYS = ("username", "password_hash", "epoch")


def _valid_account(config: dict) -> dict | None:
    account = config.get("account")
    if isinstance(account, dict) and all(isinstance(account.get(k), str) and account[k] for k in _ACCOUNT_KEYS):
        return account
    return None


def _account() -> dict | None:
    return _valid_account(load_auth_config())


def account_exists() -> bool:
    """Whether the owner account has been created (first-run setup is closed once it has)."""
    return _account() is not None


def get_username() -> str | None:
    """The owner account's username, or None before it is created."""
    account = _account()
    return account["username"] if account else None


def session_epoch() -> str | None:
    """The value a signed-in session must carry; replaced when the password changes or the account is remade."""
    account = _account()
    return account["epoch"] if account else None


def normalize_username(raw: object) -> str:
    """Return the username as it will be stored.

    Raises:
        AccountError: Empty, too long, or holding control characters.
    """
    username = unicodedata.normalize("NFC", raw if isinstance(raw, str) else "").strip()
    if not username:
        raise AccountError("Choose a username.")
    if len(username) > MAX_USERNAME_LENGTH:
        raise AccountError(f"Use {MAX_USERNAME_LENGTH} characters or fewer for the username.")
    if any(unicodedata.category(ch).startswith("C") for ch in username):
        raise AccountError("The username can't contain control characters.")
    return username


def check_password_rules(password: object, username: str = "") -> None:
    """Raise AccountError unless ``password`` may be used (length, and not simply the username).

    Raises:
        AccountError: With the reason, worded for the person choosing the password.
    """
    if not isinstance(password, str) or not password:
        raise AccountError("Choose a password.")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise AccountError(f"Use at least {MIN_PASSWORD_LENGTH} characters for the password.")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise AccountError(f"Use {MAX_PASSWORD_LENGTH} characters or fewer for the password.")
    if "\x00" in password:
        raise AccountError("The password can't contain a null character.")
    if username and password.casefold() == username.casefold():
        raise AccountError("The password can't be the same as the username.")


def _hash_password(password: str) -> str:
    return generate_password_hash(password, method="scrypt")


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def create_account(username: str, password: str) -> None:
    """Create the one owner account (first-run setup) and start onboarding for it.

    Raises:
        AccountError: The username or password isn't acceptable.
        AccountExistsError: An account already exists; first-run setup is closed for good.
        OSError: auth.json couldn't be written.
    """
    username = normalize_username(username)
    check_password_rules(password, username)
    hashed = _hash_password(password)
    with _account_lock:
        config, readable = _read_auth_file()
        if not readable or _valid_account(config):
            raise AccountExistsError("An account already exists.")
        config["account"] = {
            "username": username,
            "password_hash": hashed,
            "epoch": secrets.token_hex(16),
            "created_at": _now(),
        }
        config["onboarding"] = {"state": ONBOARDING_PENDING}
        save_auth_config(config)
    logger.info("Owner account '{}' created", username)


def _dummy_password_hash() -> str:
    """A hash to check an unknown username against, so a wrong username costs the same time as a wrong password."""
    global _dummy_hash  # noqa: PLW0603
    if _dummy_hash is None:
        _dummy_hash = _hash_password(secrets.token_urlsafe(16))
    return _dummy_hash


def verify_login(username: object, password: object) -> bool:
    """Whether this username and password belong to the owner account (the hash check always runs)."""
    account = _account()
    supplied_ok = isinstance(username, str) and isinstance(password, str) and len(password) <= MAX_PASSWORD_LENGTH
    password_ok = check_password_hash(
        account["password_hash"] if account else _dummy_password_hash(),
        password if supplied_ok else "",
    )
    if not (account and supplied_ok and password_ok):
        return False
    wanted = unicodedata.normalize("NFC", account["username"]).casefold().encode()
    given = unicodedata.normalize("NFC", username.strip()).casefold().encode()  # type: ignore[union-attr]  # supplied_ok
    return hmac.compare_digest(wanted, given)


def change_password(current_password: object, new_password: object) -> str:
    """Set a new password for the owner account; every other browser is signed out (the epoch changes).

    Args:
        current_password: The password in use now.
        new_password: The password to use from now on.

    Returns:
        The account's new session epoch, for the caller to put in the session that made the change.

    Raises:
        AccountError: No account, the current password is wrong, or the new one isn't acceptable.
        OSError: auth.json couldn't be written.
    """
    with _account_lock:
        config = load_auth_config()
        account = _valid_account(config)
        if account is None:
            raise AccountError("There is no account to change.")
        if (
            not isinstance(current_password, str)
            or len(current_password) > MAX_PASSWORD_LENGTH
            or not check_password_hash(account["password_hash"], current_password)
        ):
            raise AccountError("That isn't your current password.")
        check_password_rules(new_password, account["username"])
        if new_password == current_password:
            raise AccountError("Choose a password you aren't using already.")
        account["password_hash"] = _hash_password(new_password)  # type: ignore[arg-type]  # checked above
        account["epoch"] = secrets.token_hex(16)
        save_auth_config(config)
    logger.info("Password changed for '{}'; other browsers were signed out", account["username"])
    return account["epoch"]


def reset_password(new_password: str) -> str:
    """Recovery: give the existing account a new password without knowing the old one; everyone is signed out.

    Raises:
        AccountError: No account yet, or the password isn't acceptable.
        OSError: auth.json couldn't be written.
    """
    with _account_lock:
        config, readable = _read_auth_file()
        account = _valid_account(config)
        if account is None:
            raise AccountError(
                "auth.json can't be read; use reset-account to start over."
                if not readable
                else "There is no account yet; open the web interface to create one."
            )
        check_password_rules(new_password, account["username"])
        account["password_hash"] = _hash_password(new_password)
        account["epoch"] = secrets.token_hex(16)
        save_auth_config(config)
        return account["username"]


def reset_account() -> bool:
    """Recovery: remove the account, so the first-run page asks for a new one. Webhook secret untouched.

    Returns:
        Whether there was an account to remove.
    """
    with _account_lock:
        config = load_auth_config()
        had = _valid_account(config) is not None
        config.pop("account", None)
        config.pop("onboarding", None)
        if config:
            save_auth_config(config)
        elif os.path.exists(AUTH_FILE):
            os.unlink(AUTH_FILE)
        return had


# ---------------------------------------------------------------------------
# Browser session
# ---------------------------------------------------------------------------


def get_auth_method() -> str:
    """Get the configured authentication method.

    Returns:
        'internal' (default) or 'external'.
        Set AUTH_METHOD=external when using a reverse proxy or VPN for auth.
    """
    method = os.environ.get("AUTH_METHOD", AUTH_METHOD_INTERNAL).lower().strip()
    if method == AUTH_METHOD_EXTERNAL:
        return AUTH_METHOD_EXTERNAL
    return AUTH_METHOD_INTERNAL


def is_auth_external() -> bool:
    """Check if authentication is handled externally (reverse proxy, VPN, etc.)."""
    return get_auth_method() == AUTH_METHOD_EXTERNAL


def is_setup_required() -> bool:
    """Whether the first-run page must come before anything else: built-in sign-in, and no account yet.

    A damaged ``auth.json`` is not "no account yet": the account is there, just out of reach, so setup stays closed
    instead of letting a stranger make a new one in its place.
    """
    if is_auth_external():
        return False
    config, readable = _read_auth_file()
    return readable and _valid_account(config) is None


def is_authenticated() -> bool:
    """Whether this browser is signed in as the current owner account.

    A cookie only counts while its ``epoch`` matches the account's, so one from before a password change, a
    recovery reset, or the token login this replaced (it has no epoch) is refused.
    """
    if is_auth_external():
        return True
    if not session.get("authenticated"):
        return False
    epoch = session_epoch()
    given = session.get("epoch")
    return bool(epoch) and isinstance(given, str) and hmac.compare_digest(epoch.encode(), given.encode())


def start_signed_in_session(*, remember: bool = True) -> None:
    """Mark this browser's session signed in, starting it afresh.

    Nothing from before the sign-in carries over, the CSRF secret above all: another app on this host (a different
    port is the same site for cookies) can plant a ``session`` cookie whose token it read from /login. Kept, that token
    would stay valid for the whole signed-in session.

    Args:
        remember: Keep the session across browser restarts (up to the session lifetime); otherwise it ends with the
            browser.
    """
    epoch = session_epoch()
    session.clear()
    session["authenticated"] = True
    session["epoch"] = epoch
    session.permanent = remember


def login_required(f):
    """Decorator to require a signed-in browser session for a route."""

    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not is_authenticated():
            if request.is_json or request.path.startswith("/api/"):
                return jsonify({"error": "Authentication required"}), 401
            if is_setup_required():
                return redirect(url_for("main.first_run"))
            return redirect(url_for("main.login"))
        return f(*args, **kwargs)

    return decorated_function


# ---------------------------------------------------------------------------
# Webhook secret
# ---------------------------------------------------------------------------


def _stored_secret() -> str:
    from .settings_manager import get_settings_manager

    return str(get_settings_manager().get("webhook_secret") or "").strip()


def _env_legacy_token() -> str | None:
    """``WEB_AUTH_TOKEN`` from the environment: the sign-in token before accounts, imported once as the secret."""
    raw = os.environ.get("WEB_AUTH_TOKEN")
    stripped = (raw or "").strip()
    return stripped or None


def _legacy_token() -> str | None:
    """The token the app accepted before accounts: the environment variable when set (it always won), else the file's."""
    env = _env_legacy_token()
    if env:
        return env
    saved = load_auth_config().get("token")
    return saved if isinstance(saved, str) and saved else None


def get_webhook_secret() -> str:
    """The webhook secret, made first if there is none.

    The first one is the token the app used before accounts (so Sonarr, Radarr, Plex and scripts keep working
    unchanged), else a new random one.
    """
    from .settings_manager import get_settings_manager

    with _secret_lock:
        secret = _stored_secret()
        if secret:
            return secret
        legacy = _legacy_token()
        secret = legacy or generate_secret()
        get_settings_manager().set("webhook_secret", secret)
        if legacy:
            logger.info(
                "Carried the old sign-in token over as the webhook secret, unchanged: Sonarr, Radarr, Plex and "
                "scripts that used it keep working. Sign-in is now a username and password."
            )
        else:
            logger.info("Made a webhook secret; see Settings > Connections.")
        return secret


def regenerate_webhook_secret() -> str:
    """Replace the webhook secret with a new random one; senders using the old one stop working."""
    from .settings_manager import get_settings_manager

    with _secret_lock:
        secret = generate_secret()
        get_settings_manager().set("webhook_secret", secret)
    logger.info("Webhook secret regenerated (hidden)")
    return secret


def webhook_secret_matches(candidate: object) -> bool:
    """Whether ``candidate`` is the webhook secret (compares bytes: a non-ASCII ``str`` would raise otherwise).

    A settings.json that lost the secret (a restored backup, a reset) gets one made here, as at startup, so the
    owner can copy it from Settings > Connections without restarting; the caller is refused until then.
    """
    if not isinstance(candidate, str) or not candidate:
        return False
    try:
        stored = get_webhook_secret()
    except Exception:
        logger.exception("Could not read or make the webhook secret")
        return False
    return secrets.compare_digest(candidate.encode(), stored.encode())


def mask_secret(secret: str) -> str:
    """The secret as the page shows it until asked: 8 dots, then its last 4 characters (short enough for a phone)."""
    return "\u2022" * 8 + (secret[-4:] if len(secret) > 8 else "")


def _check_token_headers() -> bool:
    """Validate Authorization Bearer or X-Auth-Token headers against the webhook secret.

    Returns:
        True if a valid secret was found in the request headers.

    """
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
        if token and webhook_secret_matches(token):
            return True

    x_token = request.headers.get("X-Auth-Token", "").strip()
    if x_token and webhook_secret_matches(x_token):
        return True

    return False


def api_token_required(f):
    """Decorator to require a signed-in session or the webhook secret (script access) for API routes.

    Accepts session cookies, Authorization Bearer, or X-Auth-Token headers.
    """

    @wraps(f)
    def decorated_function(*args, **kwargs):
        if is_authenticated():
            return f(*args, **kwargs)

        if _check_token_headers():
            return f(*args, **kwargs)

        return jsonify({"error": "Authentication required"}), 401

    return decorated_function


def setup_or_auth_required(f):
    """Decorator that allows unauthenticated access during setup, requires auth after.

    If the setup wizard has not been completed, the request is allowed without
    authentication (the wizard needs open access to configure the app).
    Once setup is complete, falls through to the same logic as
    ``api_token_required`` -- checking session, Bearer token, and X-Auth-Token.
    """

    @wraps(f)
    def decorated_function(*args, **kwargs):
        from .settings_manager import get_settings_manager

        settings = get_settings_manager()
        if not settings.is_setup_complete():
            return f(*args, **kwargs)

        if is_authenticated():
            return f(*args, **kwargs)

        if _check_token_headers():
            return f(*args, **kwargs)

        return jsonify({"error": "Authentication required"}), 401

    return decorated_function


# ---------------------------------------------------------------------------
# Onboarding (the first-run tour): remembered on the server, with the account
# ---------------------------------------------------------------------------


def get_onboarding_state() -> str:
    """``pending`` (offer the tour), ``dismissed`` (they chose Skip) or ``completed`` (they finished it)."""
    state = (load_auth_config().get("onboarding") or {}).get("state")
    return state if state in ONBOARDING_STATES else ONBOARDING_PENDING


def set_onboarding_state(state: str) -> None:
    """Remember the tour's state.

    Raises:
        ValueError: ``state`` isn't one of ONBOARDING_STATES.
        OSError: auth.json couldn't be written.
    """
    if state not in ONBOARDING_STATES:
        raise ValueError(f"Unknown onboarding state: {state!r}")
    with _account_lock:
        config = load_auth_config()
        config["onboarding"] = {"state": state, "updated_at": _now()}
        save_auth_config(config)


# ---------------------------------------------------------------------------
# Startup and app wiring
# ---------------------------------------------------------------------------


def initialize_auth() -> None:
    """Startup: carry the old sign-in token over to the webhook secret, retire it, and say how to sign in.

    Never raises: a config folder that can't be written is reported by its own startup check, and the app still
    starts (webhooks then refuse every call until a secret can be saved).
    """
    secret = ""
    try:
        had_secret = bool(_stored_secret())
        secret = get_webhook_secret()
        config = load_auth_config()
        file_token = config.get("token")
        if isinstance(file_token, str) and file_token:
            if had_secret and file_token != secret and not _env_legacy_token():
                logger.warning(
                    "The old sign-in token in {} is retired and no longer works for webhooks or scripts: a webhook "
                    "secret was already set, so that one stays. Update any sender still using the old token.",
                    AUTH_FILE,
                )
            config.pop("token", None)
            if config:
                save_auth_config(config)
            else:
                os.unlink(AUTH_FILE)
    except Exception:
        logger.exception("Could not finish setting up sign-in and the webhook secret")

    if is_auth_external():
        logger.warning("=" * 60)
        logger.warning("AUTH_METHOD=external — built-in authentication DISABLED")
        logger.warning("Ensure a reverse proxy, VPN, or other external")
        logger.warning("mechanism is protecting access to this application.")
        logger.warning("=" * 60)
        return

    env_token = _env_legacy_token()
    if env_token and env_token == secret:
        logger.info(
            "WEB_AUTH_TOKEN is set; its value became the webhook secret when you upgraded. Sign-in no longer uses "
            "it, so you can remove the variable."
        )
    elif env_token:
        logger.info(
            "WEB_AUTH_TOKEN is set but ignored: sign-in no longer uses it and a webhook secret was already set. "
            "You can remove the variable."
        )
    username = get_username()
    if username:
        logger.info("Sign in as '{}'. Forgot the password? See 'Sign in and your account' in the README.", username)
    else:
        logger.info("=" * 60)
        logger.info("FIRST RUN: open the web interface and create your username and password.")
        logger.info("=" * 60)


def install_auth_hooks(app) -> None:
    """Send every page to the first-run page until an account exists; give templates the sign-in facts.

    Only pages (the ``main`` blueprint) are redirected. API and webhook routes answer for themselves: a webhook
    carries its own secret and keeps working before anyone has made an account.
    """

    @app.before_request
    def _first_run_gate():
        endpoint = request.endpoint
        if not endpoint or not endpoint.startswith("main.") or endpoint == "main.first_run":
            return None
        if is_setup_required():
            return redirect(url_for("main.first_run"))
        return None

    @app.context_processor
    def _auth_context() -> dict:
        signed_in = is_authenticated()
        return {
            "auth_ui": {
                "external": is_auth_external(),
                "username": get_username() if signed_in else None,
                # The tour opens by itself only for a signed-in owner who hasn't dismissed or finished it.
                "onboarding_auto": signed_in and get_onboarding_state() == ONBOARDING_PENDING,
            },
            # Called only by the Connections page's secret card; the secret itself never goes into a page.
            "webhook_secret_masked": lambda: mask_secret(get_webhook_secret()),
        }
