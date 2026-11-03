"""Sign-in, the owner account, the first-run tour and the webhook secret.

Everything here needs the browser session itself (``login_required``), never the webhook secret: a sender that knows
the secret must not be able to change the password or read the secret back.
"""

from flask import jsonify, request, session
from loguru import logger

from ..auth import (
    ONBOARDING_COMPLETED,
    ONBOARDING_DISMISSED,
    AccountError,
    change_password,
    get_auth_method,
    get_onboarding_state,
    get_webhook_secret,
    is_auth_external,
    is_authenticated,
    is_setup_required,
    login_required,
    mask_secret,
    regenerate_webhook_secret,
    set_onboarding_state,
    start_signed_in_session,
    verify_login,
)
from . import api
from ._helpers import limiter


def _json_object() -> dict:
    """The request's JSON body, or ``{}`` when it is empty, malformed or not an object."""
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _no_store(response):
    """Mark a response that carries a secret so no browser or proxy keeps a copy."""
    response.headers["Cache-Control"] = "no-store"
    return response


@api.route("/auth/status")
def auth_status():
    """Check authentication status and auth method."""
    return jsonify(
        {
            "authenticated": is_authenticated(),
            "auth_method": get_auth_method(),
            "setup_required": is_setup_required(),
        }
    )


@api.route("/auth/login", methods=["POST"])
@limiter.limit("10 per minute")
def api_login():
    """API login endpoint (``{username, password, remember}``). Rate limited to 10 requests per minute."""
    data = _json_object()

    if verify_login(data.get("username"), data.get("password")):
        start_signed_in_session(remember=bool(data.get("remember", True)))
        return jsonify({"success": True})

    return jsonify({"success": False, "error": "Wrong username or password"}), 401


@api.route("/auth/logout", methods=["POST"])
def api_logout():
    """API logout endpoint."""
    session.clear()
    return jsonify({"success": True})


@api.route("/account/password", methods=["POST"])
@login_required
@limiter.limit("5 per minute")
def change_account_password():
    """Change the owner's password (``{current_password, new_password}``).

    The current password is required even though the browser is signed in, so a borrowed, unlocked browser can't
    take the account over. Every other browser is signed out; this one stays in.
    """
    if is_auth_external():
        return jsonify({"success": False, "error": "Sign-in is handled by your proxy or VPN, not by Reprise."}), 409
    data = _json_object()
    try:
        epoch = change_password(data.get("current_password"), data.get("new_password"))
    except AccountError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except OSError:
        logger.exception("Could not save the new password")
        return (
            jsonify(
                {
                    "success": False,
                    "error": "Reprise couldn't save the new password. Check that its config folder is writable.",
                }
            ),
            500,
        )
    session["epoch"] = epoch
    return jsonify({"success": True})


@api.route("/account/onboarding", methods=["GET"])
@login_required
def get_onboarding():
    """Whether the tour opens by itself: ``pending`` (yes), ``dismissed`` or ``completed`` (no)."""
    return jsonify({"state": get_onboarding_state()})


@api.route("/account/onboarding", methods=["POST"])
@login_required
def set_onboarding():
    """Remember that the tour was skipped (``{"state": "dismissed"}``) or finished (``{"state": "completed"}``)."""
    state = _json_object().get("state")
    if state not in (ONBOARDING_DISMISSED, ONBOARDING_COMPLETED):
        return jsonify({"error": "state must be 'dismissed' or 'completed'."}), 400
    try:
        set_onboarding_state(state)
    except OSError:
        logger.exception("Could not save the tour's state")
        return jsonify({"error": "Reprise couldn't save this. Check that its config folder is writable."}), 500
    return jsonify({"state": state})


@api.route("/webhook-secret/reveal", methods=["POST"])
@login_required
def reveal_webhook_secret():
    """The webhook secret in full, for Show and Copy. Never part of a page, so it isn't in the HTML until asked for."""
    return _no_store(jsonify({"secret": get_webhook_secret()}))


@api.route("/webhook-secret/regenerate", methods=["POST"])
@login_required
def regenerate_secret():
    """Replace the webhook secret. Senders using the old one stop working until updated; Plex is re-registered here."""
    from ..settings_manager import get_settings_manager
    from .api_settings import _reregister_plex_webhooks_after_secret_rotation

    secret = regenerate_webhook_secret()
    _reregister_plex_webhooks_after_secret_rotation(get_settings_manager())
    return _no_store(jsonify({"success": True, "secret": secret, "masked": mask_secret(secret)}))
