"""Owner UI and separately authenticated playback-client endpoints."""

import mimetypes
import re
from dataclasses import asdict, is_dataclass
from functools import wraps

from flask import Blueprint, Response, g, jsonify, request, stream_with_context

from ...reprise import acceleration
from ...reprise.api_auth import DEFAULT_SCOPES, SCOPES, bearer_secret, require_client
from ...reprise.models import AnalysisRefused, PlaybackIdentity, ReviewRefused
from ..auth import _check_token_headers, api_token_required

_ID_FIELDS = (
    "plex_machine_identifier",
    "rating_key",
    "media_id",
    "part_id",
    "part_key",
    "part_size_bytes",
    "duration_ms",
    "audio_stream_id",
)


def _json(value):
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, tuple | list):
        return [_json(item) for item in value]
    if isinstance(value, dict):
        return {key: _json(item) for key, item in value.items()}
    return value


def _body(allowed, required=()):
    value = request.get_json(silent=True)
    if not isinstance(value, dict) or set(value) - set(allowed) or not set(required) <= set(value):
        raise ValueError("Invalid request")
    return value


def _identity(values):
    if set(values) != set(_ID_FIELDS):
        raise ValueError("Invalid identity")
    result = {}
    for key in _ID_FIELDS:
        value = values[key]
        if key in ("part_size_bytes", "duration_ms"):
            if isinstance(value, bool) or not (isinstance(value, int) or isinstance(value, str) and value.isdecimal()):
                raise ValueError("Invalid identity")
            value = int(value)
            if value <= 0:
                raise ValueError("Invalid identity")
        elif not isinstance(value, str) or not value or len(value) > 2048:
            raise ValueError("Invalid identity")
        result[key] = value
    if "?" in result["part_key"] or "#" in result["part_key"]:
        raise ValueError("Invalid identity")
    return PlaybackIdentity(**result)


def _unresolved():
    return dict(
        schema_version=1,
        resolution="unresolved",
        analysis_status="missing",
        freshness="unavailable",
        media_version_id=None,
        analysis_id=None,
        result_revision=None,
        timebase="file_presentation_ms",
        duration_ms=None,
        eligible_skip_ranges=[],
        protected_keep_ranges=[],
        proposed_ranges=[],
        reasons=["incomplete_identity"],
    )


def _stream(file, version):
    size = version.size_bytes
    start, end, status = 0, size - 1, 200
    header = request.headers.get("Range")
    if header:
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip())
        if not match or not any(match.groups()) or size <= 0:
            file.close()
            return Response(status=416, headers={"Content-Range": f"bytes */{size}"})
        first, last = match.groups()
        if first:
            start = int(first)
            end = min(int(last), size - 1) if last else size - 1
        else:
            length = int(last)
            start = max(0, size - length)
        if start >= size or start > end or (not first and int(last) == 0):
            file.close()
            return Response(status=416, headers={"Content-Range": f"bytes */{size}"})
        status = 206
    length = max(0, end - start + 1)
    headers = {"Accept-Ranges": "bytes", "Content-Length": str(length)}
    if status == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    if request.method == "HEAD":
        file.close()
        return Response(
            status=status,
            headers=headers,
            content_type=mimetypes.guess_type(version.canonical_path)[0] or "application/octet-stream",
        )

    def chunks():
        try:
            file.seek(start)
            remaining = length
            while remaining:
                chunk = file.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk
        finally:
            file.close()

    response = Response(
        stream_with_context(chunks()),
        status=status,
        headers=headers,
        content_type=mimetypes.guess_type(version.canonical_path)[0] or "application/octet-stream",
        direct_passthrough=True,
    )
    response.call_on_close(file.close)
    return response


def create_reprise_blueprint(service):
    bp = Blueprint("reprise_api", __name__)
    store = service.store

    @bp.after_request
    def no_store(response):
        response.headers["Cache-Control"] = "no-store"
        return response

    def owner(view):
        @wraps(view)
        @api_token_required
        def wrapped(*args, **kwargs):
            secret = bearer_secret()
            if secret and store.authenticate_client(secret) is not None:
                return jsonify(error="Owner access required"), 403
            if (
                request.headers.get("Authorization") or request.headers.get("X-Auth-Token")
            ) and not _check_token_headers():
                return jsonify(error="Owner access required"), 403
            return view(*args, **kwargs)

        return wrapped

    @bp.errorhandler(KeyError)
    def missing(_error):
        return jsonify(error="Not found"), 404

    @bp.errorhandler(FileNotFoundError)
    def missing_file(_error):
        return jsonify(error="Not found"), 404

    @bp.errorhandler(AnalysisRefused)
    def refused(error):
        return jsonify(error=str(error)), 409

    @bp.errorhandler(ReviewRefused)
    def review_refused(error):
        return jsonify(error=str(error)), 400

    @bp.errorhandler(ValueError)
    def invalid(_error):
        return jsonify(error="Invalid request or operation"), 400

    @bp.errorhandler(PermissionError)
    def forbidden(_error):
        return jsonify(error="Operation not permitted"), 403

    @bp.errorhandler(Exception)
    def error(exc):
        from werkzeug.exceptions import HTTPException

        if isinstance(exc, HTTPException):
            return jsonify(error=exc.name), exc.code
        if type(exc).__name__ in {"RevisionConflict", "PublicationConflict", "ConflictError"}:
            return jsonify(error="Conflict; refresh and try again"), 409
        return jsonify(error="Operation failed"), 500

    @bp.get("/api/v1/results")
    @require_client(store, "results:read")
    def client_result():
        if set(request.args) != set(_ID_FIELDS) or any(len(request.args.getlist(k)) != 1 for k in request.args):
            return jsonify(_unresolved())
        try:
            identity = _identity(dict(request.args))
        except ValueError:
            return jsonify(_unresolved())
        return jsonify(_json(service.lookup(identity)))

    @bp.get("/api/v1/jobs/<job_id>")
    @require_client(store, "jobs:read")
    def client_job(job_id):
        return jsonify(_json(service.get_job(job_id, g.reprise_principal)))

    @bp.post("/api/v1/analyses")
    @require_client(store, "analysis:request")
    def client_analysis():
        return _enqueue(g.reprise_principal)

    # Existing global CSRF hook exempts views that authenticate without cookies.
    client_analysis.is_webhook_receiver = True

    def _enqueue(principal):
        data = _body({"media_version_ids", "idempotency_key"}, {"media_version_ids"})
        ids = data["media_version_ids"]
        key = data.get("idempotency_key")
        if (
            not isinstance(ids, list)
            or not ids
            or len(ids) > 100
            or any(not isinstance(i, str) or not i or len(i) > 256 for i in ids)
            or (key is not None and (not isinstance(key, str) or not key or len(key) > 256))
        ):
            raise ValueError("Invalid analysis request")
        return jsonify(_json(service.enqueue_analysis(ids, principal=principal, idempotency_key=key))), 202

    @bp.get("/api/reprise/media")
    @owner
    def media():
        allowed = {"server_id", "cursor", "limit", "search", "show", "season"}
        if set(request.args) - allowed or any(len(request.args.getlist(k)) != 1 for k in request.args):
            raise ValueError("Invalid filters")
        limit = request.args.get("limit", "50")
        if not limit.isdecimal() or not 1 <= int(limit) <= 200:
            raise ValueError("Invalid limit")
        return jsonify(
            _json(
                service.list_media(
                    server_id=request.args.get("server_id"),
                    cursor=request.args.get("cursor"),
                    limit=int(limit),
                    search=request.args.get("search"),
                    show=request.args.get("show"),
                    season=request.args.get("season"),
                )
            )
        )

    @bp.post("/api/reprise/refresh")
    @owner
    def refresh():
        data = _body({"server_id", "library_id"}, {"server_id", "library_id"})
        if any(not isinstance(data[k], str) or not data[k] for k in data):
            raise ValueError("Invalid library")
        return jsonify(_json(service.enqueue_refresh(data["server_id"], data["library_id"], principal="owner"))), 202

    @bp.get("/api/reprise/media/<media_id>/result")
    @owner
    def result(media_id):
        value = service.get_result(media_id)
        if value is None:
            raise KeyError(media_id)
        return jsonify(_json(value))

    @bp.route("/api/reprise/media/<media_id>/stream", methods=["GET", "HEAD"])
    @owner
    def media_stream(media_id):
        file, version = service.open_stream(media_id)
        return _stream(file, version)

    @bp.post("/api/reprise/media/<media_id>/preview")
    @owner
    def preview(media_id):
        data = _body({"start_ms", "end_ms", "audio_stream_id"}, {"start_ms", "end_ms", "audio_stream_id"})
        if (
            type(data["start_ms"]) is not int
            or type(data["end_ms"]) is not int
            or data["start_ms"] < 0
            or data["end_ms"] <= data["start_ms"]
            or not isinstance(data["audio_stream_id"], str)
            or not data["audio_stream_id"]
        ):
            raise ValueError("Invalid preview request")
        return jsonify(
            _json(
                service.enqueue_preview(
                    media_id, data["start_ms"], data["end_ms"], data["audio_stream_id"], principal="owner"
                )
            )
        ), 202

    @bp.route("/api/reprise/previews/<job_id>/stream", methods=["GET", "HEAD"])
    @owner
    def preview_stream(job_id):
        file, artifact = service.open_preview(job_id)
        return _stream(file, artifact)

    @bp.post("/api/reprise/analyses")
    @owner
    def analysis():
        return _enqueue("owner")

    @bp.patch("/api/reprise/media/<media_id>/review")
    @owner
    def review(media_id):
        data = _body({"expected_revision", "edits"}, {"expected_revision", "edits"})
        if (
            type(data["expected_revision"]) is not int
            or data["expected_revision"] < 0
            or not isinstance(data["edits"], list)
        ):
            raise ValueError("Invalid review")
        allowed = {
            "range_id",
            "action",
            "kind",
            "start_ms",
            "end_ms",
            "review_status",
            "locked",
            "reason",
            "acknowledge_suggestion",
            "acknowledge_plex_suggestion",
        }
        if any(
            not isinstance(edit, dict)
            or set(edit) - allowed
            or "range_id" not in edit
            or not (
                isinstance(edit["range_id"], str)
                and edit["range_id"]
                or edit["range_id"] is None
                and edit.get("action") == "keep"
            )
            for edit in data["edits"]
        ):
            raise ValueError("Invalid edits")
        return jsonify(_json(service.review(media_id, data["expected_revision"], data["edits"], "owner")))

    @bp.get("/api/reprise/jobs")
    @owner
    def jobs():
        return jsonify(_json(service.list_jobs()))

    @bp.get("/api/reprise/jobs/<job_id>")
    @owner
    def owner_job(job_id):
        return jsonify(_json(service.get_job(job_id)))

    @bp.post("/api/reprise/jobs/<job_id>/<action>")
    @owner
    def control_job(job_id, action):
        if action not in {"pause", "resume", "cancel"}:
            raise KeyError(action)
        return jsonify(_json(service.control_job(job_id, action)))

    @bp.route("/api/reprise/settings", methods=["GET", "PATCH"])
    @owner
    def settings():
        if request.method == "PATCH":
            data = _body({"writes_enabled", "gpu_decode", "online_lookups"})
            if not data or any(type(value) is not bool for value in data.values()):
                raise ValueError("Invalid setting")
            if "writes_enabled" in data:
                service.set_writes_enabled(data["writes_enabled"])
            if "gpu_decode" in data:
                acceleration.set_enabled(data["gpu_decode"])
            if "online_lookups" in data:
                service.set_online_lookups_enabled(data["online_lookups"])
        publisher = service.publisher
        return jsonify(
            publication_available=publisher is not None,
            writes_enabled=bool(getattr(publisher, "writes_enabled", False)),
            # What decodes analysis frames now: label (for display), active, per-device state and counters.
            acceleration=acceleration.status(),
            # The owner's switch for asking public databases where an episode's intro and credits are.
            online_lookups=service.online_lookups_enabled(),
        )

    @bp.route("/api/reprise/clients", methods=["GET", "POST"])
    @owner
    def clients():
        if request.method == "GET":
            return jsonify(_json(store.list_clients()))
        data = _body({"name", "scopes"}, {"name"})
        name = data["name"]
        scopes = data.get("scopes", list(DEFAULT_SCOPES))
        if (
            not isinstance(name, str)
            or not name.strip()
            or len(name) > 128
            or not isinstance(scopes, list)
            or not scopes
            or any(not isinstance(s, str) or s not in SCOPES for s in scopes)
            or len(set(scopes)) != len(scopes)
        ):
            raise ValueError("Invalid client")
        return jsonify(_json(store.create_client(name.strip(), scopes))), 201

    @bp.delete("/api/reprise/clients/<client_id>")
    @owner
    def revoke(client_id):
        store.revoke_client(client_id)
        return "", 204

    @bp.post("/api/reprise/clients/<client_id>/rotate")
    @owner
    def rotate(client_id):
        return jsonify(_json(store.rotate_client(client_id)))

    @bp.get("/api/reprise/publications")
    @owner
    def publications():
        if service.publisher is None:
            return jsonify(error="Publication is unavailable"), 503
        if (
            set(request.args) != {"media_version_id"}
            or not request.args["media_version_id"]
            or len(request.args.getlist("media_version_id")) != 1
        ):
            raise ValueError("Invalid publication query")
        return jsonify(items=_json(service.publisher.list_changes(request.args["media_version_id"])))

    @bp.post("/api/reprise/publications/prepare")
    @owner
    def prepare():
        if service.publisher is None:
            return jsonify(error="Publication is unavailable"), 503
        data = _body(
            {"media_version_id", "expected_revision", "identity"}, {"media_version_id", "expected_revision", "identity"}
        )
        if (
            not isinstance(data["media_version_id"], str)
            or type(data["expected_revision"]) is not int
            or not isinstance(data["identity"], dict)
        ):
            raise ValueError("Invalid publication")
        identity = _identity(data["identity"])
        try:
            result = service.publisher.prepare(data["media_version_id"], data["expected_revision"], identity, "owner")
        except ValueError:
            return jsonify(error="Publication conflict; refresh and try again"), 409
        return jsonify(_json(result))

    @bp.post("/api/reprise/publications/<change_id>/<action>")
    @owner
    def publication(change_id, action):
        if service.publisher is None:
            return jsonify(error="Publication is unavailable"), 503
        if action not in {"apply", "undo", "recover"}:
            raise KeyError(action)
        try:
            result = getattr(service.publisher, action)(change_id, "owner")
        except ValueError:
            return jsonify(error="Publication conflict; refresh and try again"), 409
        return jsonify(_json(result))

    return bp
