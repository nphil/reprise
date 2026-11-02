"""Authentication for Reprise's independently scoped playback clients."""
from functools import wraps

from flask import g, jsonify, request

SCOPES = frozenset({"results:read", "jobs:read", "analysis:request"})
DEFAULT_SCOPES = ("results:read", "jobs:read")


def bearer_secret():
    value = request.headers.get("Authorization", "")
    parts = value.split()
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
        return None
    return parts[1]


def require_client(store, scope):
    """Never consult session or the legacy external-auth bypass."""
    def decorate(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            secret = bearer_secret()
            principal = store.authenticate_client(secret) if secret else None
            if principal is None:
                return jsonify(error="Authentication required"), 401
            if scope not in principal.scopes:
                return jsonify(error="Insufficient scope"), 403
            g.reprise_principal = principal
            return view(*args, **kwargs)
        return wrapped
    return decorate
