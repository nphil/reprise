"""Owner UI and separately authenticated playback-client endpoints."""

import mimetypes
import re
from dataclasses import asdict, is_dataclass
from functools import wraps

from flask import Blueprint, Response, g, jsonify, request, stream_with_context
from loguru import logger

from ...reprise import acceleration, approvals
from ...reprise import setup_guide as guide
from ...reprise.api_auth import DEFAULT_SCOPES, SCOPES, bearer_secret, require_client
from ...reprise.models import AnalysisRefused, PlaybackIdentity, PublicationRefused, ReportRefused, ReviewRefused
from ...reprise.report import render_text
from ...reprise.store import NEEDS_OWNER_SPAN_ACKNOWLEDGMENT
from ..auth import (
    SETUP_GUIDE_COMPLETED,
    SETUP_GUIDE_DISMISSED,
    _check_token_headers,
    api_token_required,
    get_setup_guide_state,
    set_setup_guide_state,
)
from ..settings_manager import get_settings_manager

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


def _query_int(name, low, high, default=None):
    """A whole number from the query string between ``low`` and ``high`` (``ValueError`` when missing or outside)."""
    raw = request.args.get(name)
    if raw is None and default is not None:
        return default
    if raw is None or not re.fullmatch(r"\d{1,9}", raw) or not low <= int(raw) <= high:
        raise ValueError("Invalid request")
    return int(raw)


def _picture(data):
    """A JPEG the browser may keep for a few minutes: it is private to the owner and its address names the exact time."""
    response = Response(data, mimetype="image/jpeg")
    response.headers["Cache-Control"] = "private, max-age=300"
    return response


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
        if "Cache-Control" not in response.headers:
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

    @bp.errorhandler(ReportRefused)
    def report_refused(error):
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

    @bp.get("/api/reprise/report")
    @owner
    def report():
        """Plex's markers against Reprise's results for one show or season, computed here so one read answers it."""
        args = request.args
        if set(args) - {"show", "season", "format"} or any(len(args.getlist(k)) != 1 for k in args):
            raise ReportRefused("Use show, season and format, each at most once.")
        show = args.get("show", "").strip()
        if not show or len(show) > 200:
            raise ReportRefused("Say which show to report on (show=<title>).")
        season = args.get("season", "").strip()
        if season and not re.fullmatch(r"\d{1,4}", season):
            raise ReportRefused("The season must be a number.")
        if args.get("format", "json") not in ("json", "text"):
            raise ReportRefused("The format must be json or text.")
        data = service.report(show, int(season) if season else None)
        if args.get("format") == "text":
            return Response(render_text(data), mimetype="text/plain")
        return jsonify(data)

    @bp.post("/api/reprise/approve-local-model")
    @owner
    def approve_local_model():
        """Approve one season's high-confidence local-model suggestions as the owner's own skips. Nothing is sent."""
        data = _body({"show", "season"}, {"show", "season"})
        show, season = data["show"], data["season"]
        if not isinstance(show, str) or not show.strip() or len(show) > 200:
            raise ReportRefused("Say which show to approve (show=<title>).")
        if type(season) is not int or not 0 <= season <= 9999:
            raise ReportRefused("The season must be a number.")
        return jsonify(_json(service.approve_confident(show.strip(), season)))

    @bp.get("/api/reprise/approvals")
    @owner
    def approvals_queue():
        """Everything waiting for the owner, by show and season, from the store alone (Plex is not read)."""
        return jsonify(_json(approvals.queue(service)))

    @bp.get("/api/reprise/approvals/season")
    @owner
    def approvals_season():
        """One season in detail, set against what Plex has now, with the plain account of what sending would change."""
        show = request.args.get("show", "").strip()
        if not show or len(show) > 200:
            raise ReportRefused("Say which show (show=<title>).")
        return jsonify(_json(approvals.season_plan(service, show, _query_int("season", 0, 9999))))

    @bp.post("/api/reprise/approvals/send")
    @owner
    def approvals_send():
        """Approve one season's sure ones (``sure``) or take what the owner approved (``approved``) and, unless
        ``to_plex`` is false, send it to Plex with a backup first."""
        data = _body({"show", "season", "scope", "to_plex"}, {"show", "season", "scope"})
        if not isinstance(data.get("to_plex", True), bool):
            raise ValueError("Invalid request")
        show, season = data["show"], data["season"]
        if not isinstance(show, str) or not show.strip() or len(show) > 200:
            raise ReportRefused("Say which show (show=<title>).")
        if type(season) is not int or not 0 <= season <= 9999:
            raise ReportRefused("The season must be a number.")
        return jsonify(
            _json(approvals.send(service, show.strip(), season, data["scope"], to_plex=data.get("to_plex", True)))
        )

    @bp.post("/api/reprise/approvals/undo")
    @owner
    def approvals_undo():
        """Put Plex back as it was for the changes a send returned, and take back what that send approved."""
        data = _body({"changes"}, {"changes"})
        changes = data["changes"]
        if not isinstance(changes, list) or not 0 < len(changes) <= 500:
            raise ValueError("Invalid request")
        for change in changes:
            if (
                not isinstance(change, dict)
                or set(change) - {"media_version_id", "change_id", "span_ids", "label"}
                or not isinstance(change.get("media_version_id"), str)
                or not isinstance(change.get("change_id"), str | None)
                or not isinstance(change.get("span_ids", []), list)
                or any(not isinstance(i, str) for i in change.get("span_ids", []))
            ):
                raise ValueError("Invalid request")
        return jsonify(_json(approvals.undo(service, changes)))

    @bp.post("/api/reprise/approvals/decide")
    @owner
    def approvals_decide():
        """Skip (at the shown or a moved time), keep or restore one suggestion; every answer carries its own undo."""
        data = _body(
            {"media_version_id", "expected_revision", "kind", "decision", "start_ms", "end_ms", "span_id", "range_ids"},
            {"media_version_id", "expected_revision", "kind", "decision"},
        )
        revision = data["expected_revision"]
        ranges = data.get("range_ids", [])
        if (
            not isinstance(data["media_version_id"], str)
            or type(revision) is not int
            or not isinstance(data["kind"], str)
            or not isinstance(ranges, list)
            or any(not isinstance(i, str) for i in ranges)
            or not isinstance(data.get("span_id"), str | None)
        ):
            raise ValueError("Invalid request")
        return jsonify(
            _json(
                approvals.decide(
                    service,
                    data["media_version_id"],
                    revision,
                    data["kind"],
                    data["decision"],
                    start_ms=data.get("start_ms"),
                    end_ms=data.get("end_ms"),
                    span_id=data.get("span_id"),
                    range_ids=ranges,
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

    @bp.get("/api/reprise/media/<media_id>/frame")
    @owner
    def media_frame(media_id):
        """One still from the episode at ``t_ms`` (picture width ``w`` from a fixed set)."""
        frames, version = service.picture_source(media_id)
        at_ms = _query_int("t_ms", 0, max(version.duration_ms or 0, 1))
        try:
            return _picture(frames.still(version, at_ms, _query_int("w", 1, 4096, 480)))
        except OSError as exc:
            return jsonify(error=str(exc)), 502

    @bp.get("/api/reprise/media/<media_id>/strip")
    @owner
    def media_strip(media_id):
        """A row of ``count`` stills ``step_ms`` apart from ``start_ms`` as one picture (the thumbnail strip)."""
        frames, version = service.picture_source(media_id)
        start_ms = _query_int("start_ms", 0, max(version.duration_ms or 0, 1))
        try:
            return _picture(
                frames.strip(
                    version,
                    start_ms,
                    _query_int("step_ms", 1, 600000),
                    _query_int("count", 1, 200),
                    _query_int("w", 1, 4096, 160),
                )
            )
        except OSError as exc:
            return jsonify(error=str(exc)), 502

    @bp.get("/api/reprise/media/<media_id>/cuts")
    @owner
    def media_cuts(media_id):
        """Where the picture jumps to a different shot between ``start_ms`` and ``end_ms`` (milliseconds, ascending)."""
        frames, version = service.picture_source(media_id)
        top = max(version.duration_ms or 0, 1)
        try:
            cuts = frames.cuts(version, _query_int("start_ms", 0, top), _query_int("end_ms", 1, top))
        except OSError as exc:
            return jsonify(error=str(exc)), 502
        return jsonify({"cuts": cuts})

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
            "acknowledge_owner_span",
        }

        def names_a_range(edit):
            """An existing range by id, a new keep, or a new exact skip (it has no id yet)."""
            if edit["range_id"] is None:
                return edit.get("action") == "keep" or {"kind", "start_ms", "end_ms"} <= set(edit)
            return isinstance(edit["range_id"], str) and bool(edit["range_id"])

        if any(not isinstance(edit, dict) or set(edit) - allowed or "range_id" not in edit for edit in data["edits"]):
            raise ValueError("Invalid edits")
        if not all(names_a_range(edit) for edit in data["edits"]):
            raise ValueError("Invalid edits")
        if any(
            edit["range_id"] is None and edit.get("action") != "keep" and edit.get("acknowledge_owner_span") is not True
            for edit in data["edits"]
        ):
            raise ReviewRefused(NEEDS_OWNER_SPAN_ACKNOWLEDGMENT)
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

    no_autopilot = "Automatic work is not available right now."

    @bp.route("/api/reprise/settings", methods=["GET", "PATCH"])
    @owner
    def settings():
        autopilot = getattr(service, "autopilot", None)
        if request.method == "PATCH":
            data = _body(
                {
                    "writes_enabled",
                    "gpu_decode",
                    "online_lookups",
                    "gpu_vram_cap_mb",
                    "auto_send",
                    "backfill",
                    "alerts",
                    "local_vision",
                }
            )
            asked_vision = "local_vision" in data
            vision = data.pop("local_vision", None)
            asked_cap = "gpu_vram_cap_mb" in data
            cap = data.pop("gpu_vram_cap_mb", None)
            # The three automatic-work switches go to the autopilot; only the keys that were sent.
            automatic = {key: data.pop(key) for key in ("auto_send", "backfill", "alerts") if key in data}
            # The memory cap is a whole number of MiB inside the range the setting allows; the switches are booleans.
            low, high = acceleration.VRAM_CAP_RANGE_MB
            if not (data or asked_cap or automatic or asked_vision) or any(
                type(value) is not bool for value in (*data.values(), *automatic.values())
            ):
                raise ValueError("Invalid setting")
            if asked_cap and (type(cap) is not int or not low <= cap <= high):
                raise ValueError("Invalid setting")
            if asked_vision:
                current = service.local_vision()
                if type(vision) is not dict or not set(vision) <= {"enabled", "base_url", "model"}:
                    raise ValueError("Invalid setting")
                service.set_local_vision(
                    vision.get("enabled", current.enabled),
                    vision.get("base_url", current.base_url),
                    vision.get("model", current.model),
                )
            if automatic and autopilot is None:
                return jsonify(error=no_autopilot), 400
            if "writes_enabled" in data:
                try:
                    service.set_writes_enabled(data["writes_enabled"])
                except ValueError as refused:
                    # The switch refuses in plain words (what folder is missing, why no episode passed); the generic
                    # handler below would hide them behind "Invalid request or operation".
                    return jsonify(error=str(refused)), 400
            if "gpu_decode" in data:
                acceleration.set_enabled(data["gpu_decode"])
            if "online_lookups" in data:
                service.set_online_lookups_enabled(data["online_lookups"])
            if asked_cap:
                acceleration.set_vram_cap_mb(cap)
            if automatic:
                autopilot.update_settings(automatic)
        publisher = service.publisher
        return jsonify(
            publication_available=publisher is not None,
            writes_enabled=bool(getattr(publisher, "writes_enabled", False)),
            # What decodes analysis frames now: label (for display), active, per-device state and counters.
            acceleration=acceleration.status(),
            # The most video memory (MiB) the GPU comparison may hold at once; it also always leaves 1 GiB of the card free.
            gpu_vram_cap_mb=acceleration.vram_cap_mb(),
            # The owner's switch for asking public databases where an episode's intro and credits are.
            online_lookups=service.online_lookups_enabled(),
            # The optional check of unconfirmed stretches by a local vision model: off unless turned on, advice only.
            local_vision=asdict(service.local_vision()),
            # The background job that looks after the library by itself: its three switches and what it is doing now.
            autopilot=None if autopilot is None else {**autopilot.settings(), "status": autopilot.status()},
        )

    @bp.post("/api/reprise/alerts/test")
    @owner
    def alerts_test():
        autopilot = getattr(service, "autopilot", None)
        if autopilot is None:
            return jsonify(error=no_autopilot), 400
        return jsonify(_json(autopilot.send_test_alert()))

    @bp.route("/api/reprise/setup-guide", methods=["GET", "POST"])
    @owner
    def setup_guide():
        """The setup guide's seven steps with their live state (GET), and the owner's answer to it (POST).

        GET reads saved settings, a few folder checks, one indexed count and in-memory state: no Plex call, no walk of
        the catalogue, nothing written. POST remembers ``dismissed`` (stop opening it by itself) or ``completed``.
        """
        settings = get_settings_manager()
        if request.method == "POST":
            state = _body({"state"}, {"state"})["state"]
            if state not in (SETUP_GUIDE_DISMISSED, SETUP_GUIDE_COMPLETED):
                raise ValueError("Invalid state")
            try:
                set_setup_guide_state(state)
            except OSError:
                logger.exception("Could not save the setup guide's state")
                return jsonify(error="Reprise couldn't save this. Check that its config folder is writable."), 500
            return jsonify(state=state)
        return jsonify(
            guide.build_status(
                service,
                settings,
                base_url=request.host_url.rstrip("/"),
                guide_state=get_setup_guide_state(configured=settings.is_configured()),
                wizard_complete=settings.is_setup_complete(),
            )
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
        except PublicationRefused as error:
            return jsonify(error=str(error)), 409
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
