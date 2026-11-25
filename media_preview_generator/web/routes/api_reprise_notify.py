"""Phone notifications: the buttons on them, and the owner's settings for them.

``POST /api/reprise/notify/act/<token>`` is what a button on the owner's phone calls. It has no session and no login
header, so it carries its own proof instead: a signed, single-use, short-lived token (see ``reprise.nudges``). That is
why it is a webhook receiver (exempt from the browser CSRF check, like the other endpoints that authenticate on their
own). ``GET``/``PATCH /api/reprise/notifications`` are the owner's settings and use the usual owner sign-in.
"""

from functools import wraps

from flask import Blueprint, jsonify, request
from loguru import logger

from ...reprise.api_auth import bearer_secret
from ..auth import _check_token_headers, api_token_required
from ._helpers import limiter

NOT_AVAILABLE = "Phone alerts are not available right now."


def create_notify_blueprint(service):
    bp = Blueprint("reprise_notify", __name__)

    @bp.after_request
    def no_store(response):
        response.headers["Cache-Control"] = "no-store"
        return response

    def owner(view):
        @wraps(view)
        @api_token_required
        def wrapped(*args, **kwargs):
            secret = bearer_secret()
            if secret and service.store.authenticate_client(secret) is not None:
                return jsonify(error="Owner access required"), 403
            if (
                request.headers.get("Authorization") or request.headers.get("X-Auth-Token")
            ) and not _check_token_headers():
                return jsonify(error="Owner access required"), 403
            return view(*args, **kwargs)

        return wrapped

    def notifier():
        return getattr(getattr(service, "autopilot", None), "notifier", None)

    @bp.post("/api/reprise/notify/act/<token>")
    @limiter.limit("30 per minute")
    def act(token):
        found = notifier()
        if found is None:
            return jsonify(ok=False, message=NOT_AVAILABLE), 503
        status, body = found.act(token)
        return jsonify(body), status

    # The button's own token is the proof, so the browser CSRF check (which protects session cookies) does not apply.
    act.is_webhook_receiver = True

    @bp.route("/api/reprise/notifications", methods=["GET", "PATCH"])
    @owner
    def notifications():
        found = notifier()
        if found is None:
            return jsonify(error=NOT_AVAILABLE), 400
        if request.method == "PATCH":
            data = request.get_json(silent=True)
            try:
                found.update_settings(data)
            except ValueError as refused:
                logger.debug("Phone alert settings refused: {}", refused)
                return jsonify(error=str(refused)), 400
        return jsonify(found.status())

    return bp
