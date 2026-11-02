"""Intro & Credits API."""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from typing import Any, cast

from flask import jsonify, request
from loguru import logger

from ...scan_filters import FILTER_CONFIG_KEYS, normalize_scan_filter_config, pick_filter_config
from ..auth import api_token_required
from ..jobs import PRIORITY_FROM_LABEL, PRIORITY_LABELS, PRIORITY_LOW
from . import api
from ._helpers import MEDIA_ROOT, _param_to_bool, _safe_resolve_within
from .api_bif import _validate_path_under_any_server
from .api_jobs import _check_servers_answer, _config_unwritable_response

_ONLINE_SOURCE_IDS = ("theintrodb", "introdb", "skipdb")
_AUTH_SECRET_KEYS = ("token", "api_key", "password", "access_token")
_MASK = "****"
# Item ids go into the media server's URL path: bare Plex rating keys, Jellyfin/Emby GUIDs (with or without dashes) or
# Emby's numeric ids. Anything else is refused before the server is asked. ASCII only: ``\d`` would also take other
# scripts' digits.
_PLEX_ITEM_ID_RE = re.compile(r"[0-9]+")
_EMBYISH_ITEM_ID_RE = re.compile(r"[0-9a-fA-F][0-9a-fA-F-]{0,35}|[0-9]+")


def _parse_job_priority(raw: object) -> int | None:
    """A priority from 1/2/3 or high/normal/low; None when it is anything else (bools included)."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw in PRIORITY_LABELS else None
    if isinstance(raw, str):
        return PRIORITY_FROM_LABEL.get(raw.strip().lower())
    return None


def job_title(label: str, library_name: str | None, libraries: list, resolved_paths: list) -> str:
    """A started job's title: the one sent, else ``<label>: N files``, ``<label>: N libraries`` or all libraries."""
    if (library_name or "").strip():
        return cast(str, library_name).strip()
    if resolved_paths:
        count = len(resolved_paths)
        return f"{label}: {count} file{'' if count == 1 else 's'}"
    if libraries:
        count = len(libraries)
        return f"{label}: {count} librar{'y' if count == 1 else 'ies'}"
    return f"{label}: all libraries"


def _optional_json_object() -> tuple[dict | None, tuple | None]:
    """Read a request body that may be empty: ``({}, None)`` when empty, else the JSON object.

    Returns:
        ``(data, None)``, or ``(None, error_response)`` for a body that isn't a JSON object.
    """
    data = request.get_json(silent=True)
    if data is None:
        # get_json answers None both for no body and for a body it can't read (no JSON content type, a trailing
        # comma); only an empty body means "no options".
        if request.content_length or request.get_data(cache=True):
            return None, (jsonify({"error": "The request body must be JSON"}), 400)
        return {}, None
    if not isinstance(data, dict):
        return None, (jsonify({"error": "The request body must be a JSON object"}), 400)
    return data, None


def parse_job_request():
    """Read a "start a job" body: ``libraries`` or ``file_paths``, ``priority`` and ``library_name``.

    Returns:
        ``((libraries, resolved_paths, priority, library_name, data), None)``, or ``(None, response)`` for an invalid
        body or an unwritable config directory.
    """
    blocked = _config_unwritable_response()
    if blocked is not None:
        return None, blocked
    data, bad_body = _optional_json_object()
    if bad_body is not None:
        return None, bad_body
    libraries = data.get("libraries") or []
    file_paths = data.get("file_paths") or []
    if not isinstance(libraries, list) or not all(
        isinstance(x, dict) and str(x.get("server_id") or "").strip() and str(x.get("library_id") or "").strip()
        for x in libraries
    ):
        return None, (jsonify({"error": "libraries must be a list of {server_id, library_id}"}), 400)
    if not isinstance(file_paths, list) or not all(isinstance(p, str) and p.strip() for p in file_paths):
        return None, (jsonify({"error": "file_paths must be a list of paths"}), 400)
    if libraries and file_paths:
        return None, (jsonify({"error": "Choose libraries or file_paths, not both"}), 400)
    priority = _parse_job_priority(data["priority"]) if "priority" in data else PRIORITY_LOW
    if priority is None:
        return None, (jsonify({"error": "priority must be 1, 2, 3, high, normal or low"}), 400)
    library_name = data.get("library_name")
    if library_name is not None and not isinstance(library_name, str):
        return None, (jsonify({"error": "library_name must be a string"}), 400)
    resolved_paths = []
    for raw in file_paths:
        resolved = _safe_resolve_within(raw.strip(), MEDIA_ROOT)
        if resolved is None:
            return None, (jsonify({"error": f"Path is outside allowed media root: {raw.strip()}"}), 400)
        resolved_paths.append(resolved)
    libraries = [{"server_id": str(x["server_id"]), "library_id": str(x["library_id"])} for x in libraries]
    return (libraries, resolved_paths, priority, library_name, data), None


def parse_scan_filters(data: dict) -> tuple[dict | None, Any]:
    """Read the optional ``scan_filters`` object of a "start a job" body.

    Returns:
        ``(filters, None)`` with the validated filter keys (empty when none were sent), or ``(None, response)`` for
        an invalid object.
    """
    raw = data.get("scan_filters")
    if raw is None:
        return {}, None
    if not isinstance(raw, dict):
        return None, (jsonify({"error": "scan_filters must be an object"}), 400)
    unknown = sorted(set(raw) - FILTER_CONFIG_KEYS)
    if unknown:
        return None, (jsonify({"error": f"Unknown scan_filters keys: {', '.join(unknown)}"}), 400)
    try:
        return pick_filter_config(normalize_scan_filter_config(raw, full_scan=True)), None
    except ValueError as exc:
        return None, (jsonify({"error": str(exc)}), 400)


@api.route("/markers/jobs", methods=["POST"])
@api_token_required
def create_marker_job():
    """Start an Intro & Credits job for chosen libraries, chosen files, or every library with markers turned on.

    Body: ``libraries`` (``[{"server_id", "library_id"}]``) or ``file_paths`` (files or folders inside
    ``MEDIA_ROOT``) — neither means every library Intro & Credits goes to; ``priority`` (1-3 or high/normal/low,
    default low); ``force`` (re-detect files already done); ``library_name`` (job title); ``scan_filters`` (added
    date, seasons, release year: which library files to list).

    Returns:
        201 with the job, 400 for an invalid body, 503 when the config directory isn't writable.
    """
    from ...markers.triggers import create_intro_credits_job

    parsed, error = parse_job_request()
    if error is not None:
        return error
    libraries, resolved_paths, priority, library_name, data = parsed
    scan_filters, bad_filters = parse_scan_filters(data)
    if bad_filters is not None:
        return bad_filters
    job = create_intro_credits_job(
        library_name=job_title("Intro & Credits", library_name, libraries, resolved_paths),
        priority=priority,
        source="manual",
        libraries=libraries,
        file_paths=resolved_paths,
        force=_param_to_bool(data.get("force"), False),
        scan_filters=scan_filters,
    )
    return jsonify(job.to_dict()), 201


@api.route("/markers/reconcile", methods=["POST"])
@api_token_required
def marker_reconcile():
    """Queue Intro & Credits · Check servers now: read back what servers show and publish again where they changed.

    Body (optional): ``priority`` (1-3 or high/normal/low, default low).

    Returns:
        202 with ``{"job_id", "already_queued"}`` (a new job, or the one already queued or running, whatever priority
        was asked for; ``"paused": true`` when that one is paused); 200 with ``{"job_id": null, "reason"}`` when
        Intro & Credits is off on every server; 400 for an invalid body; 503 when the config directory isn't writable.
    """
    from ...markers.reconcile import run_markers_reconcile

    blocked = _config_unwritable_response()
    if blocked is not None:
        return blocked
    data, bad_body = _optional_json_object()
    if bad_body is not None:
        return bad_body
    priority = _parse_job_priority(data["priority"]) if "priority" in data else PRIORITY_LOW
    if priority is None:
        return jsonify({"error": "priority must be 1, 2, 3, high, normal or low"}), 400
    return _check_servers_answer(run_markers_reconcile(priority=priority))


def _registry(*, timeout_s: int | None = None) -> Any:
    """The configured servers.

    Args:
        timeout_s: Cap every server's request timeout at this many seconds. The editor's publish runs inside a web
            request, so it shortens the transport instead of inheriting the 30 s a job can afford; a
            server already configured below the cap keeps its own value.

    Returns:
        A ``ServerRegistry``.
    """
    from ...servers import ServerRegistry
    from ..settings_manager import get_settings_manager

    entries = list(get_settings_manager().get("media_servers") or [])
    if timeout_s is not None:
        entries = [{**entry, "timeout": _capped_timeout(entry.get("timeout"), timeout_s)} for entry in entries]
    return ServerRegistry.from_settings(entries)


def _capped_timeout(stored: object, cap: int) -> int:
    """``stored`` seconds, never above ``cap``; the cap alone when settings.json holds something that isn't a number."""
    try:
        return min(int(cast(Any, stored) or 30), cap)
    except (TypeError, ValueError):
        return cap


def _without_secrets(value: Any, registry: Any) -> Any:
    """``value`` with every configured server credential replaced by ``****`` (error text can echo a request)."""
    secrets = sorted(
        {
            str(cfg.auth.get(key))
            for cfg in registry.configs()
            for key in _AUTH_SECRET_KEYS
            if isinstance(cfg.auth, dict) and cfg.auth.get(key)
        },
        key=len,
        reverse=True,
    )

    def scrub(v: Any) -> Any:
        if isinstance(v, str):
            for secret in secrets:
                v = v.replace(secret, _MASK)
            return v
        if isinstance(v, dict):
            return {k: scrub(x) for k, x in v.items()}
        if isinstance(v, list | tuple):
            return [scrub(x) for x in v]
        return v

    return scrub(value) if secrets else value


def _library_roots(registry: Any) -> list[str]:
    """Local folders of every library on every enabled server, whatever the library's preview opt-in."""
    from ...servers.ownership import apply_path_mappings

    roots: list[str] = []
    for cfg in registry.configs():
        if not cfg.enabled:
            continue
        for lib in cfg.libraries:
            for remote in lib.remote_paths:
                if (remote or "").strip():
                    roots.extend(r for r in apply_path_mappings(remote, cfg.path_mappings or []) if (r or "").strip())
    return roots


def _library_file(path: object, registry: Any) -> str | None:
    """A file inside a server library (and the media root), normalised; None for anything else."""
    if not isinstance(path, str) or not path.strip():
        return None
    safe = _validate_path_under_any_server(path, _library_roots(registry))
    if safe is None or not os.path.isfile(safe) or _safe_resolve_within(safe, MEDIA_ROOT) is None:
        return None
    return safe


def _server_off_response(cfg: Any) -> tuple[Any, int]:
    message = f"{cfg.name or 'This server'} is disabled. Re-enable it on the Servers page first."
    return jsonify({"ok": False, "error": message}), 409


@api.route("/markers/servers/<server_id>/status", methods=["GET"])
@api_token_required
def marker_server_status(server_id: str):
    """Intro & Credits status for a server's Edit dialog.

    Query: ``refresh=1`` drops this server's cached answer first — the Plex marker agent's "Check again" button,
    where the point is to ask the agent again rather than read a few-seconds-old answer.

    Returns:
        200 with ``markers.inspect.server_status_payload`` (capability checked as if Intro & Credits were on, state
        ``unknown`` when the check failed; a server turned off on the Servers page isn't contacted), 404 for an unknown
        server, 500 with a JSON error when the status can't be built.
    """
    from ...markers import inspect

    registry = _registry()
    cfg = registry.get_config(server_id)
    if cfg is None:
        return jsonify({"error": "server not found"}), 404
    if _param_to_bool(request.args.get("refresh"), False):
        inspect.forget_capability(server_id)
    try:
        payload = inspect.server_status_payload(registry.get(server_id), cfg)
    except Exception as exc:
        logger.warning("Intro & Credits status for {} failed: {}", cfg.name, type(exc).__name__)
        return jsonify({"error": "Couldn't check this server's Intro & Credits status"}), 500
    return jsonify(_without_secrets(payload, registry))


@api.route("/markers/sources/usage", methods=["GET"])
@api_token_required
def marker_source_usage():
    """Today's (UTC) lookups per online source; TheIntroDB's limit and remaining come from its response headers.

    Returns:
        200 with ``{source_id: {"day", "used", "limit", "remaining", "has_key", "low_priority_exhausted",
        "resets_at"}}``. ``low_priority_exhausted`` is true once a backfill (LOW priority) lookup would be refused
        right now; ``resets_at`` is always the daily budget's day boundary in the user's words, never the source's
        own (untrustworthy) reset header. The TheIntroDB key itself is never returned, only whether one is stored.
    """
    from ...markers.settings import get_global_settings
    from ...markers.sources.ratelimit import RESET_TIME_LABEL, get_limiter, low_priority_exhausted
    from ...markers.store import get_marker_store

    settings = get_global_settings()
    store = get_marker_store()
    out = {}
    for source_id in _ONLINE_SOURCE_IDS:
        limiter = get_limiter(source_id)
        live = limiter.usage()
        day = live["day"]
        # A limiter starts from today's stored row when it is created (after a restart); the stored row still counts
        # when that read-back failed.
        stored = store.source_usage(source_id, day) or {}
        source = settings.source(source_id)
        limit = live["limit"] if live.get("limit") is not None else stored.get("limit")
        remaining = live["remaining"] if live.get("remaining") is not None else stored.get("remaining")
        out[source_id] = {
            "day": day,
            "used": max(live.get("used") or 0, stored.get("used") or 0),
            "limit": limit,
            "remaining": remaining,
            "has_key": bool(source_id == "theintrodb" and source is not None and source.api_key),
            # This source's own share, not the module default: mirrors what its live limiter would actually do.
            "low_priority_exhausted": low_priority_exhausted(
                limit=limit, remaining=remaining, reserve_fraction=limiter.reserve_fraction
            ),
            "resets_at": RESET_TIME_LABEL,
        }
    return jsonify(out)


def _resolve_item_path(get: Callable[[str], Any], registry: Any) -> tuple[str | None, tuple[Any, int] | None]:
    """A local file path from ``path``, or from ``server_id`` + ``item_id`` (+ ``version_file``).

    Args:
        get: Reads one request field (``request.args.get``, or a JSON body's ``get``).
        registry: The ``ServerRegistry``.

    Returns:
        ``(path, None)``, or ``(None, response)`` with the refusal to return as-is. The path is the server's answer,
        not yet checked against the library roots: the caller still runs it through ``_library_file``.
    """
    from ...markers import inspect
    from ...servers.base import ServerType

    path = get("path")
    if isinstance(path, str) and path.strip():
        return path, None
    server_id, item_id = get("server_id"), get("item_id")
    if not isinstance(server_id, str) or not isinstance(item_id, str) or not server_id or not item_id:
        return None, (jsonify({"error": "Give path, or server_id and item_id"}), 400)
    cfg = registry.get_config(server_id)
    server = registry.get(server_id)
    if cfg is None or server is None:
        return None, (jsonify({"error": "server not found"}), 404)
    item_id_re = _PLEX_ITEM_ID_RE if cfg.type is ServerType.PLEX else _EMBYISH_ITEM_ID_RE
    if not item_id_re.fullmatch(item_id):
        return None, (jsonify({"error": "item_id isn't an item id this server uses"}), 400)
    if not cfg.enabled:
        return None, _server_off_response(cfg)
    version_file = get("version_file")
    try:
        resolved = inspect.resolve_local_path(
            server, cfg, item_id, version_file=version_file if isinstance(version_file, str) else None
        )
    except inspect.VersionNotHereError:
        return None, (jsonify({"error": inspect.VERSION_NOT_HERE, "reason": "version_not_here"}), 404)
    if not resolved:
        return None, (jsonify({"error": "No file on this app's disk for that item"}), 404)
    return resolved, None


@api.route("/markers/item", methods=["GET"])
@api_token_required
def marker_item():
    """Inspector data for one file, by ``path`` or by ``server_id`` + ``item_id`` (+ ``version_file``, the version's file
    as the search row gave it: ``markers.inspect.resolve_local_path`` never opens another version in its place).

    Returns:
        200 with ``markers.inspect.item_payload`` (a server whose state can't be read gets a degraded row); 400 when the
        path isn't a file inside a server library, the query is incomplete or ``item_id`` isn't shaped like that
        server's ids; 404 for an unknown server or an item with no file here (``reason: version_not_here`` when the
        server has the version asked for but its file isn't on this disk); 409 when the server is off; 500 with a
        JSON error when the file's data can't be built.
    """
    from ...markers import inspect
    from ...markers.store import get_marker_store

    registry = _registry()
    path, refused = _resolve_item_path(request.args.get, registry)
    if refused is not None:
        return refused
    safe = _library_file(path, registry)
    if safe is None:
        return jsonify({"error": "Path is not a file inside any server library"}), 400
    try:
        payload = inspect.item_payload(safe, registry=registry, store=get_marker_store())
    except Exception as exc:
        # One server's failure only degrades its row; this is the file-level part (e.g. markers.db unreadable).
        logger.warning("Inspector data for a file failed: {}", type(exc).__name__)
        return jsonify({"error": "Couldn't build the Intro & Credits data for this file"}), 500
    return jsonify(_without_secrets(payload, registry))


@api.route("/markers/item/redetect", methods=["POST"])
@api_token_required
def marker_item_redetect():
    """Run Intro & Credits again for one file, asking every source again.

    Body: ``{"path"}``.

    Returns:
        202 with ``{"job_id"}`` (a HIGH priority, forced, single-file job; this file's re-detect still queued or
        running when there is one); 400 when the path isn't a file inside a server library; 503 when the config
        directory isn't writable.
    """
    from ...markers.triggers import submit_redetect

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "The request body must be a JSON object with a path"}), 400
    safe = _library_file(data.get("path"), _registry())
    if safe is None:
        return jsonify({"error": "Path is not a file inside any server library"}), 400
    blocked = _config_unwritable_response()
    if blocked is not None:
        return blocked
    return jsonify({"job_id": submit_redetect(safe)}), 202


def _marker_dict(marker: Any, locked_at: str | None = None) -> dict:
    return {
        "type": marker.type.value,
        "start_ms": marker.start_ms,
        "end_ms": marker.end_ms,
        "locked": marker.locked,
        "locked_at": locked_at,
    }


def _stored_markers(store: Any, file_id: int) -> dict[str, dict]:
    dates = store.locked_at(file_id)
    return {m.type.value: _marker_dict(m, dates.get(m.type)) for m in store.get_markers(file_id).values()}


def _marker_types(raw: object) -> tuple[list[Any] | None, str]:
    """The ``types`` field as ``MarkerType``s, or the reason it isn't a list of them."""
    from ...markers.models import MarkerType

    names = [t.value for t in MarkerType]
    if not isinstance(raw, list) or not raw:
        return None, f"types must be a non-empty list of {', '.join(names)}"
    out = []
    for value in raw:
        if not isinstance(value, str) or value not in names:
            return None, f"types must be a non-empty list of {', '.join(names)}"
        out.append(MarkerType(value))
    return out, ""


def _user_markers(raw: object, duration_ms: int) -> tuple[list[Any] | None, str]:
    """The ``markers`` field as user markers, or the reason it can't be saved.

    Only the two bounds that a user's own marker keeps are enforced: inside the
    file, and ending after it starts. The 3 s minimum, the intro caps and the first-35 % / last-25 % windows exist to
    catch a source that is wrong, and a user marking a 2 s intro is not wrong — the editor warns, it doesn't refuse.
    """
    from ...markers.decide import EOF_CLAMP_MS
    from ...markers.models import Marker, MarkerType, Source

    names = [t.value for t in MarkerType]
    if not isinstance(raw, list) or not raw:
        return None, "markers must be a non-empty list of {type, start_ms, end_ms}"
    out: list[Any] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            return None, "markers must be a non-empty list of {type, start_ms, end_ms}"
        mtype = entry.get("type")
        if mtype not in names:
            return None, f"type must be one of {', '.join(names)}"
        if mtype in seen:
            return None, f"markers has {mtype} twice"
        seen.add(mtype)
        start = entry.get("start_ms")
        if isinstance(start, bool) or not isinstance(start, int):
            return None, f"{mtype}: start_ms must be a whole number of milliseconds"
        end = entry.get("end_ms")
        # No end (or null) means "runs to the end of the file", the way the editor's right-hand handle reads.
        if end is None:
            end = duration_ms
        elif isinstance(end, bool) or not isinstance(end, int):
            return None, f"{mtype}: end_ms must be a whole number of milliseconds, or null for the end of the file"
        elif duration_ms < end <= duration_ms + EOF_CLAMP_MS:
            end = duration_ms  # the same clamp every candidate gets: a couple of seconds over is the file's end
        if start < 0 or start >= duration_ms or end > duration_ms:
            return None, f"{mtype}: the marker has to be inside the file (0 to {duration_ms} ms)"
        if end <= start:
            return None, f"{mtype}: the marker has to end after it starts"
        out.append(Marker(MarkerType(mtype), start, end, (Source.USER.value,)))
    return out, ""


@api.route("/markers/item/markers", methods=["POST"])
@api_token_required
def marker_item_save():
    """Save and lock legacy proposal markers without publishing to a server.

    Reprise review and publication are separate operations. These legacy rows
    are retained for discovery and never authorize publication.
    """
    from ...markers.pipeline import identity_changed
    from ...markers.settings import get_global_settings
    from ...markers.store import get_marker_store

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "The request body must be a JSON object"}), 400
    # Bound server lookups made while resolving an item.
    registry = _registry(timeout_s=8)
    path, refused = _resolve_item_path(data.get, registry)
    if refused is not None:
        return refused
    safe = _library_file(path, registry)
    if safe is None:
        return jsonify({"error": "Path is not a file inside any server library"}), 400
    blocked = _config_unwritable_response()
    if blocked is not None:
        return blocked

    store = get_marker_store()
    rec = store.get_file(safe)
    if rec is None or not rec.duration_ms:
        return jsonify(
            {"error": "This file hasn't been analysed yet. Run Re-detect first.", "reason": "not_analysed"}
        ), 409
    if identity_changed(rec):
        # The stored duration describes the old file, so every bound below would be checked against the wrong length.
        return jsonify(
            {"error": "This file changed on disk since it was analysed; re-detect it first.", "reason": "file_changed"}
        ), 409
    saved, problem = _user_markers(data.get("markers"), rec.duration_ms)
    if saved is None:
        return jsonify({"error": problem}), 400

    store.save_user_markers(rec.id, saved, settings_fingerprint=get_global_settings().detection_fingerprint())
    server_rows = []
    payload = {
        "canonical_path": safe,
        "duration_ms": rec.duration_ms,
        "markers": _stored_markers(store, rec.id),
        "servers": server_rows,
        "queued_job_id": None,
    }
    return jsonify(_without_secrets(payload, registry))


@api.route("/markers/item/markers", methods=["DELETE"])
@api_token_required
def marker_item_unlock():
    """Drop the user's lock on one or more marker types for a file.

    Body: ``path`` (or ``server_id`` + ``item_id``, and optionally ``version_file``) and ``types``
    (``["intro", "credits", "recap", "preview"]``). Nothing is published: the type is left undecided and the
    next Intro & Credits run decides and publishes it again.

    Returns:
        200 with ``unlocked`` (the types that were locked), the file's remaining ``markers`` and the ``decisions`` of
        the unlocked types; 400 for a body this can't be read or a path outside every server library; 404 for an
        unknown server or item; 409 when the server is off or the file was never analysed; 503 when the config
        directory isn't writable.
    """
    from ...markers.store import get_marker_store

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "The request body must be a JSON object"}), 400
    registry = _registry()
    path, refused = _resolve_item_path(data.get, registry)
    if refused is not None:
        return refused
    safe = _library_file(path, registry)
    if safe is None:
        return jsonify({"error": "Path is not a file inside any server library"}), 400
    types, problem = _marker_types(data.get("types"))
    if types is None:
        return jsonify({"error": problem}), 400
    blocked = _config_unwritable_response()
    if blocked is not None:
        return blocked
    store = get_marker_store()
    rec = store.get_file(safe)
    if rec is None:
        return jsonify(
            {"error": "This file hasn't been analysed yet. Run Re-detect first.", "reason": "not_analysed"}
        ), 409
    unlocked = store.unlock_markers(rec.id, types)
    decisions = store.get_decisions(rec.id)
    return jsonify(
        {
            "canonical_path": safe,
            "unlocked": [t.value for t in types if t in unlocked],
            "markers": _stored_markers(store, rec.id),
            "decisions": {
                t.value: {"status": decisions[t].status.value, "reason": decisions[t].reason}
                for t in types
                if t in decisions
            },
        }
    )


def _library_episode(path: object, registry: Any) -> tuple[str | None, tuple[Any, int] | None]:
    """A TV episode inside a server library, or the 400 response saying why it isn't one."""
    from ...markers.external_ids import ids_from_path

    safe = _library_file(path, registry)
    if safe is None:
        return None, (jsonify({"error": "Path is not a file inside any server library"}), 400)
    if not ids_from_path(safe).is_episode:
        return None, (jsonify({"error": "Not a TV episode"}), 400)
    return safe, None


@api.route("/markers/season", methods=["GET"])
@api_token_required
def marker_season():
    """Season view data for the season of one episode, from markers.db only (no server is contacted).

    Query: ``path`` (an episode file inside a server library).

    Returns:
        200 with ``markers.inspect.season_payload``; 400 when the path isn't a file inside a server library or isn't a
        TV episode; 500 with a JSON error when the data can't be built.
    """
    from ...markers import inspect
    from ...markers.store import get_marker_store

    registry = _registry()
    safe, refused = _library_episode(request.args.get("path"), registry)
    if refused is not None:
        return refused
    try:
        payload = inspect.season_payload(safe, registry=registry, store=get_marker_store())
    except Exception as exc:
        logger.warning("Season view data failed: {}", type(exc).__name__)
        return jsonify({"error": "Couldn't build the Season view for this file"}), 500
    return jsonify(_without_secrets(payload, registry))


@api.route("/markers/season/publish", methods=["POST"])
@api_token_required
def marker_season_publish():
    """Queue the Season view's "Publish": a NORMAL-priority Intro & Credits job over the episodes the Season view lists.

    Body: ``{"path"}`` (an episode of the season).

    Returns:
        202 with ``{"job_id"}`` (the Publish of the same episodes still queued or running when there is one); 400 when
        the path isn't a TV episode inside a server library; 503 when the config directory isn't writable.
    """
    from ...markers.triggers import submit_season_publish

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "The request body must be a JSON object with a path"}), 400
    safe, refused = _library_episode(data.get("path"), _registry())
    if refused is not None:
        return refused
    blocked = _config_unwritable_response()
    if blocked is not None:
        return blocked
    return jsonify({"job_id": submit_season_publish(safe)}), 202


@api.route("/markers/sources/local", methods=["GET"])
@api_token_required
def marker_local_sources():
    """Whether the local detectors can run in this container: season audio needs an ffmpeg with chromaprint, credit text
    ONNX Runtime, OpenCV and the text detection model.

    Returns:
        200 with ``{"season_audio": {"available", "ffmpeg", "message"}, "credits_text": {"available", "message"}}``;
        ``message`` says why when a source isn't available. A check that raises answers ``"available": null`` (not
        known) for its own source only, so the other source's row still shows.
    """
    from ...markers.audio import fingerprint
    from ...markers.credits import textdet_helper

    def season_audio() -> dict:
        # No setting to pass: jobs use ffmpeg on PATH.
        found, reason = fingerprint.chromaprint_status(None)
        return {"available": found is not None, "ffmpeg": found, "message": reason}

    def credits_text() -> dict:
        available, reason = textdet_helper.text_detection_status()
        return {"available": available, "message": reason}

    return jsonify(
        {
            "season_audio": _local_source_status("season audio", season_audio, {"ffmpeg": None}),
            "credits_text": _local_source_status("credit text", credits_text, {}),
        }
    )


def _local_source_status(name: str, check: Callable[[], dict], unknown_extra: dict) -> dict:
    """One local detector's status; ``available: None`` with a fixed message when its check raises."""
    try:
        return check()
    except Exception as exc:
        logger.warning("Couldn't check whether {} can run here: {}", name, type(exc).__name__)
        return {"available": None, **unknown_extra, "message": f"Couldn't check whether {name} can run here"}
