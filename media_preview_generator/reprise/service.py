"""Library discovery, exact playback resolution, and queued Reprise analysis."""

from __future__ import annotations

import collections
import hashlib
import json
import os
import signal
import stat
import subprocess
import tempfile
import threading
import time
from dataclasses import asdict, replace
from datetime import UTC, datetime
from urllib.parse import urlsplit

from loguru import logger

from ..gpu.ffmpeg_capabilities import ffmpeg_binary
from ..servers.ownership import apply_path_mappings
from ..shutdown import is_shutting_down
from . import chapters, recovery, scan_cache
from .admission import MAX_REFERENCES, admit_references, pair_provenance
from .identity import currency, media_version
from .models import SUGGESTION_ORIGINS, AnalysisRefused, CandidateWindow, PlaybackIdentity, is_owner_span
from .policy import eligible_ranges
from .setup_guide import plex_database_findings, sending_refusal
from .verification_profile import POLICY_VERSION, PROFILE_FINGERPRINT, is_current_profile

_SERVICE = None

# How many episodes the Plex switch tries before it reports why none can be sent.
SWITCH_CHECK_LIMIT = 25

# The ``source`` of the jobs Reprise starts by itself: new and changed files, and the slow backfill of the library.
AUTOPILOT_JOB_SOURCE = "autopilot"
AUTOPILOT_BACKFILL_SOURCE = "autopilot_backfill"
AUTOPILOT_SOURCES = (AUTOPILOT_JOB_SOURCE, AUTOPILOT_BACKFILL_SOURCE)

NOT_IN_LIBRARY = "This episode is not in the library any more. Refresh the library, then try again."
CHANGED_ON_DISK = (
    "This file changed on disk after the library was last refreshed. Refresh the library, then analyze it again."
)

# Why the Library says a row is not current, in plain words. ``action`` is the button that helps: a refresh re-reads
# the files, an analysis redoes the comparison. Each cause gets its own wording: a file Sonarr upgraded is gone, not
# "changed", and an engine update changes no file at all.
STALE_REASONS = {
    "file_missing": (
        "File missing",
        "This file is no longer on disk. It was renamed, replaced or removed after Reprise last read it, "
        "for example by a Sonarr or Radarr upgrade.",
        "Refresh the library so Reprise reads the file that is there now.",
        "refresh",
    ),
    "file_changed": (
        "File changed",
        "This file has changed since Reprise last read it.",
        "Refresh the library so Reprise reads the new file, then analyze it again.",
        "refresh",
    ),
    "file_unavailable": (
        "File unavailable",
        "Reprise can't open this file right now.",
        "Check that your media storage is available, then refresh the library.",
        "refresh",
    ),
    "library_unavailable": (
        "Media folder unavailable",
        "Reprise can't open the media folder for this library right now.",
        "If your storage is still starting up, wait a moment and refresh the library.",
        "refresh",
    ),
    "library_changed": (
        "Library settings changed",
        "The Plex server, library or folder mapping Reprise saved for this file no longer matches.",
        "Refresh the library so the file is read with the current settings.",
        "refresh",
    ),
    "plex_changed": (
        "Plex changed",
        "Plex's details for this episode changed after it was analyzed.",
        "Analyze it again so the result matches what Plex has now.",
        "analyze",
    ),
    "needs_reanalysis": (
        "Analyze again",
        "Reprise's comparison method was updated after this episode was analyzed.",
        "Analyze it again to review it with the current method.",
        "analyze",
    ),
    "comparison_changed": (
        "Comparison changed",
        "An episode this one was compared with has changed or is gone.",
        "Analyze it again so it is compared with the episodes in the library now.",
        "analyze",
    ),
}


def _stale(code):
    label, message, next_step, action = STALE_REASONS[code]
    return {"code": code, "label": label, "message": message, "next": next_step, "action": action}


def _result_stale_reason(result):
    """Why a stored analysis no longer counts although its file is as it was read."""
    if result.policy_version == "binding-changed":
        return "plex_changed"
    if result.policy_version != POLICY_VERSION or not all(is_current_profile(e.metrics) for e in result.evidence):
        return "needs_reanalysis"
    return "comparison_changed"


def _suggesters(ranges):
    """Who suggested these ranges, in the order first met ("Plex", "IntroDB", ...); a Plex suggestion from before
    online suggestions existed names no one, and is Plex's."""
    return list(dict.fromkeys(name for r in ranges for name in (r.suggested_by or ("Plex",))))


def _range_counts(ranges):
    """``to_review``: skips Reprise confirmed and the owner has not decided; ``suggested``: Plex's or an online
    database's, likewise, and ``suggested_by`` who they are. A kind the owner set an exact skip for has nothing left
    to decide: their span replaces every other skip of it."""
    owned = {r.kind for r in ranges if is_owner_span(r) and r.review_status == "approved"}
    undecided = [r for r in ranges if r.action == "skip" and r.review_status == "proposed" and r.kind not in owned]
    suggested = [r for r in undecided if r.origin in SUGGESTION_ORIGINS]
    return {
        "skip": sum(1 for r in ranges if r.action == "skip"),
        "keep": sum(1 for r in ranges if r.action == "keep"),
        "proposed": sum(1 for r in ranges if r.review_status == "proposed"),
        "approved": sum(1 for r in ranges if r.review_status == "approved"),
        "to_review": sum(1 for r in undecided if r.origin not in SUGGESTION_ORIGINS),
        "suggested": len(suggested),
        "suggested_by": _suggesters(suggested),
    }


def _kinds_text(ranges):
    kinds = sorted({r.kind for r in ranges}, key=lambda kind: ({"intro": 0, "credits": 1}.get(kind, 2), kind))
    return " and ".join(kinds)


def _suggesters_text(ranges):
    """Who suggested these ranges, in plain words ("Plex", "Plex and IntroDB")."""
    listed = _suggesters(ranges)
    return listed[0] if len(listed) == 1 else f"{', '.join(listed[:-1])} and {listed[-1]}"


def _analysis_summary(ranges, reasons):
    """(state, sentence) for an analysis that finished: what it confirmed, what only Plex or an online database
    suggested, or the first reason it proposed nothing."""
    skips = [r for r in ranges if r.action == "skip"]
    suggested = [r for r in skips if r.origin in SUGGESTION_ORIGINS]
    confirmed = [r for r in skips if r.origin not in SUGGESTION_ORIGINS]
    if confirmed:
        noun = "range" if len(confirmed) == 1 else "ranges"
        message = f"Found {len(confirmed)} {noun} to review ({_kinds_text(confirmed)})."
        if suggested:
            message += (
                f" {_suggesters_text(suggested)} also suggested the {_kinds_text(suggested)}, "
                "which Reprise could not confirm."
            )
        return "found", message
    if suggested:
        it = "it" if len(suggested) == 1 else "them"
        return "suggested", (
            f"{_suggesters_text(suggested)} suggested the {_kinds_text(suggested)}, but Reprise could not confirm {it} "
            f"by comparing episodes. Review {it} before approving."
        )
    return "nothing_found", reasons[0] if reasons else "Nothing was found to skip."


def _unfinished_warning(counts):
    """The warning a finished analysis carries for the episodes it did not analyze (None when none were left out)."""
    parts = []
    files, refused, set_aside = counts[recovery.FILE_LEFT_OUT], counts[recovery.REFUSED], counts[recovery.SET_ASIDE]
    if files:
        one = files == 1
        parts.append(
            f"{files} {'episode was' if one else 'episodes were'} not analyzed because "
            f"{'its' if one else 'their'} file is missing, changed or unavailable. "
            f"Refresh the library, then analyze {'it' if one else 'them'} again."
        )
    if refused:
        parts.append(
            f"{refused} {'episode' if refused == 1 else 'episodes'} could not be analyzed (open each to see why)."
        )
    if set_aside:
        one = set_aside == 1
        parts.append(
            f"{set_aside} {'episode was' if one else 'episodes were'} set aside after {recovery.MAX_ATTEMPTS} "
            f"interrupted attempts. Analyze {'it' if one else 'them'} again to retry."
        )
    return " ".join(parts) or None


def _has_files(folder):
    """Whether a media folder is there with something in it: storage that is not mounted yet shows as missing or empty."""
    try:
        with os.scandir(folder) as entries:
            return next(entries, None) is not None
    except OSError:
        return False


def set_service(service):
    global _SERVICE
    _SERVICE = service


def get_service():
    if _SERVICE is None:
        raise RuntimeError("Reprise service is not initialized")
    return _SERVICE


def _inside(path, root):
    root = os.path.realpath(root)
    return root != os.path.sep and os.path.commonpath((path, root)) == root


def _mapping_fingerprint(config, library):
    payload = {"path_mappings": config.path_mappings, "library_roots": list(library.remote_paths)}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _probe(path, streams, *, cancel_check=None, pause_check=None, ffmpeg_path=None):
    from ..markers.freeze import Freeze
    from .media import Cancelled, paired_ffprobe

    cancel = cancel_check or (lambda: False)
    freeze = Freeze.of(pause_check)
    freeze.hold(cancel_check=cancel)
    if cancel():
        raise Cancelled("Library refresh cancelled")
    command = [
        paired_ffprobe(ffmpeg_path or ffmpeg_binary()),
        "-v",
        "error",
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        path,
    ]
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output, stderr=errors, start_new_session=True)
        deadline = freeze.clock() + 30
        try:
            while proc.poll() is None:
                freeze.hold(proc, cancel_check=cancel, name="library refresh")
                if cancel():
                    raise Cancelled("Library refresh cancelled")
                if freeze.clock() >= deadline:
                    raise subprocess.TimeoutExpired(command, 30)
                if output.tell() > 2 * 1024 * 1024:
                    raise ValueError("Media metadata is too large")
                time.sleep(0.05)
            if cancel():
                raise Cancelled("Library refresh cancelled")
            if proc.returncode:
                raise subprocess.CalledProcessError(proc.returncode, command)
            if output.tell() > 2 * 1024 * 1024:
                raise ValueError("Media metadata is too large")
            output.seek(0)
            info = json.load(output)
        finally:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()
    audio = {int(s["index"]) for s in info.get("streams", []) if s.get("codec_type") == "audio"}
    mapped = [{"plex_id": str(s["id"]), "index": int(s["index"])} for s in streams if str(s.get("streamType")) == "2"]
    if (
        not mapped
        or len({x["plex_id"] for x in mapped}) != len(mapped)
        or len({x["index"] for x in mapped}) != len(mapped)
        or {x["index"] for x in mapped} != audio
    ):
        raise ValueError("Plex audio tracks do not match the local file")
    start = float(info.get("format", {}).get("start_time", 0) or 0)
    return json.dumps(
        {"audio_streams": mapped, "start_time_ms": round(start * 1000), "streams": info.get("streams", [])},
        sort_keys=True,
        separators=(",", ":"),
    )


# How many unreadable episodes a refresh names in full; the count above them is always complete.
_SKIPPED_ITEMS_KEPT = 50

_SKIP_REASONS = {
    "File path is missing, ambiguous, or outside the enabled library": "Reprise can't find its file where Plex says it is",
    "Plex part size differs from local file": (
        "The file on disk isn't the size Plex has recorded for it (it was probably replaced, so Plex needs to scan it "
        "again)"
    ),
    "Plex audio tracks do not match the local file": "Plex's list of its audio tracks doesn't match the file",
    "Media changed during discovery": "The file changed while Reprise was reading it",
    "Plex library roots differ from configured library": "Its file is outside the folders set up for this library",
    "Media metadata is too large": "The file's stream list is too large to read",
}


def _episode_label(info):
    """An episode as the owner knows it (``Show S01E02 Title``) from a Plex metadata row, or its id when Plex gave none."""
    show = str(info.get("grandparentTitle") or "").strip()
    title = str(info.get("title") or "").strip()
    season, episode = info.get("parentIndex"), info.get("index")
    numbers = f"S{season:02d}E{episode:02d}" if isinstance(season, int) and isinstance(episode, int) else ""
    label = " ".join(part for part in (show, numbers, title) if part)
    return label or f"Episode {info.get('ratingKey', '?')}"


def _skip_reason(exc, part):
    """Why one episode could not be read, in a plain sentence with nothing in it a path or a stack trace would add."""
    if isinstance(exc, subprocess.CalledProcessError):
        streams = part.get("Stream", []) if isinstance(part, dict) else []
        if not any(str(stream.get("streamType")) in ("1", "2") for stream in streams):
            return "Plex and FFprobe can't read its audio/video streams (the file may be damaged or incomplete)"
        return "FFprobe can't read the file (it may be damaged or incomplete)"
    if isinstance(exc, subprocess.TimeoutExpired):
        return "Reading the file took too long"
    if isinstance(exc, ValueError) and str(exc) in _SKIP_REASONS:
        return _SKIP_REASONS[str(exc)]
    if isinstance(exc, OSError):
        return "Reprise couldn't open its file"
    return "Plex's details for it were incomplete or unexpected"


class SettingsRegistry:
    """Refresh the registry when an owner saves server settings."""

    def __init__(self, settings):
        self.settings = settings
        self._snapshot = None
        self._registry = None
        self._lock = threading.Lock()

    def _current(self):
        from ..servers.registry import ServerRegistry

        configured = self.settings.get("media_servers") or []
        snapshot = json.dumps(configured, sort_keys=True)
        with self._lock:
            if snapshot != self._snapshot:
                self._registry = ServerRegistry.from_settings(configured)
                self._snapshot = snapshot
            return self._registry

    def get(self, server_id):
        return self._current().get(server_id)

    def get_config(self, server_id):
        return self._current().get_config(server_id)

    def configs(self):
        return self._current().configs()

    def servers(self):
        return self._current().servers()


def _film_title(item):
    """A film's name as the library shows it: its title and, when Plex knows it, its year."""
    title = str(item.get("title", ""))
    year = item.get("year")
    return f"{title} ({year})" if title and isinstance(year, int) else title


class RepriseService:
    def __init__(self, store, registry, job_manager):
        self.store, self.registry, self.job_manager = store, registry, job_manager
        self.publisher = None
        self.autopilot = None  # Reprise's own automatic work (``autopilot.Autopilot``), set by the app at start
        self._enqueue_lock = threading.Lock()
        self._refresh_locks = {}
        self._show_ids = {}
        self._resume_stop = threading.Event()

    def set_writes_enabled(self, enabled):
        if type(enabled) is not bool or self.publisher is None:
            raise ValueError("Publication is unavailable")
        if enabled:
            plex = [
                cfg for cfg in self.registry.configs() if cfg.enabled and getattr(cfg.type, "value", cfg.type) == "plex"
            ]
            # Sending writes markers into Plex's own database file, so Reprise's container must see it. The setup
            # guide reads the same findings, so the switch and the guide name the same missing folder.
            findings = [plex_database_findings(cfg) for cfg in plex]
            if not any(finding["database_found"] for finding in findings):
                raise ValueError(sending_refusal(findings))
            cursor, reasons, tried = None, collections.Counter(), 0
            valid = False
            # One current episode that passes every check is enough; a handful that fail tell why.
            while not valid and tried < SWITCH_CHECK_LIMIT:
                page = self.store.list_media(cursor=cursor, limit=100)
                for row in page["items"]:
                    identity = PlaybackIdentity(**row["identity"])
                    tried += 1
                    try:
                        self.publisher.validate_target(row["media_version"]["id"], identity)
                        valid = True
                        break
                    except Exception as exc:
                        reasons[str(exc)] += 1
                    if tried >= SWITCH_CHECK_LIMIT:
                        break
                if not page.get("next_cursor"):
                    break
                cursor = page["next_cursor"]
            if not valid:
                why = reasons.most_common(1)[0][0] if reasons else "no episodes are in the library yet"
                raise ValueError(f"Sending to Plex can't be turned on: {why}")
        settings = self.registry.settings
        settings.set("reprise_plex_writes_enabled", enabled)
        self.publisher.writes_enabled = enabled

    def _server(self, server_id, library_id):
        cfg = self.registry.get_config(server_id)
        server = self.registry.get(server_id)
        if not cfg or not cfg.enabled or getattr(cfg.type, "value", cfg.type) != "plex" or not server:
            raise ValueError("Select an enabled Plex server")
        library = next((x for x in server.list_libraries() if str(x.id) == str(library_id) and x.enabled), None)
        if not library:
            raise ValueError("Select an enabled Plex library")
        return cfg, server, library

    def _library_roots(self, cfg, library):
        return [
            os.path.realpath(p)
            for root in library.remote_paths
            for p in apply_path_mappings(root, cfg.path_mappings)
            if os.path.isabs(p)
        ]

    def _local_path(self, cfg, library, remote):
        roots = self._library_roots(cfg, library)
        paths = {
            os.path.realpath(p)
            for p in apply_path_mappings(remote, cfg.path_mappings)
            if os.path.isabs(p) and os.path.isfile(p)
        }
        paths = {p for p in paths if any(_inside(p, root) for root in roots)}
        if len(paths) != 1:
            raise ValueError("File path is missing, ambiguous, or outside the enabled library")
        return paths.pop()

    def _library_rows(self, cfg, server, library, checkpoint=lambda: None):
        """Everything Plex lists for one library, page by page and checked against the size Plex reports (films and
        episodes each by their own type): ``(machine id, rows)``."""
        machine = str(server._plex_http("get", "/").json()["MediaContainer"]["machineIdentifier"])
        if cfg.server_identity and cfg.server_identity != machine:
            raise ValueError("Plex server identity changed")
        if not str(library.id).isdecimal():
            raise ValueError("Invalid Plex library id")
        kind = "1" if getattr(library, "kind", None) == "movie" else "4"
        rows, offset, expected_total = [], 0, None
        while True:
            checkpoint()
            container = server._plex_http(
                "get",
                f"/library/sections/{library.id}/all",
                params={"type": kind, "X-Plex-Container-Start": str(offset), "X-Plex-Container-Size": "200"},
            ).json()["MediaContainer"]
            batch = container.get("Metadata", [])
            if not isinstance(batch, list):
                raise ValueError("Incomplete Plex library response")
            rows.extend(batch)
            offset += len(batch)
            total = int(container["totalSize"]) if "totalSize" in container else None
            if total is None:
                if len(batch) >= 200:
                    raise ValueError("Plex omitted the complete library size")
                break
            if expected_total is not None and total != expected_total:
                raise ValueError("Plex library changed during enumeration")
            expected_total = total
            if offset > total:
                raise ValueError("Invalid Plex library page")
            if offset == total:
                break
            if not batch:
                raise ValueError("Incomplete Plex library response")
        keys = [str(item.get("ratingKey", "")) for item in rows]
        if len(keys) != len(set(keys)):
            raise ValueError("Duplicate Plex episodes in library response")
        return machine, rows

    def list_library(self, server_id, library_id):
        """What Plex lists for one library right now, registering nothing: ``(config, library, machine id, rows)``."""
        cfg, server, library = self._server(server_id, library_id)
        machine, rows = self._library_rows(cfg, server, library)
        return cfg, library, machine, rows

    def local_file(self, cfg, library, remote):
        """Where a file Plex lists is on this machine, or None when it can't be read here (missing, outside the library
        or ambiguous)."""
        try:
            return self._local_path(cfg, library, remote)
        except (ValueError, OSError):
            return None

    def _may_hold(self, cfg, library, summary, requested):
        """Whether a library listing row may hold one of the ``requested`` files. It is false only when the row lists
        its files and none is one of them: a read of a few named files then skips that row's detail request."""
        media_rows = summary.get("Media")
        if not isinstance(media_rows, list) or not media_rows:
            return True
        for media in media_rows:
            parts = media.get("Part") if isinstance(media, dict) else None
            if not isinstance(parts, list):
                return True
            for part in parts:
                remote = part.get("file") if isinstance(part, dict) else None
                if not remote:
                    return True
                if self.local_file(cfg, library, remote) in requested:
                    return True
        return False

    def _scan_library(
        self,
        server_id,
        library_id,
        *,
        cancel_check=None,
        pause_check=None,
        progress_callback=None,
        register=None,
        candidate_paths=None,
        ffmpeg_path=None,
    ):
        """Read one library; reconciliation is reserved for a successful queue commit."""
        from ..markers.freeze import Freeze
        from .media import Cancelled

        cancel = cancel_check or (lambda: False)
        freeze = Freeze.of(pause_check)
        register = register or self.store.register_media
        requested = None if candidate_paths is None else set(candidate_paths)
        matched = set()

        def checkpoint():
            freeze.hold(cancel_check=cancel)
            if cancel():
                raise Cancelled("Library refresh cancelled")

        checkpoint()
        cfg, server, library = self._server(server_id, library_id)
        configured_library = next(
            (item for item in cfg.libraries if item.enabled and str(item.id) == str(library.id)), None
        )
        if not configured_library:
            raise ValueError("Plex library is not enabled in the saved configuration")
        mapping_fingerprint = _mapping_fingerprint(cfg, configured_library)
        config_signature = json.dumps([cfg.url, cfg.server_identity, cfg.auth, mapping_fingerprint], sort_keys=True)

        def config_current():
            current = self.registry.get_config(server_id)
            if not current or not current.enabled or getattr(current.type, "value", current.type) != "plex":
                return False
            selected = next(
                (item for item in current.libraries if item.enabled and str(item.id) == str(library_id)), None
            )
            return bool(
                selected
                and json.dumps(
                    [current.url, current.server_identity, current.auth, _mapping_fingerprint(current, selected)],
                    sort_keys=True,
                )
                == config_signature
            )

        checkpoint()
        machine, rows = self._library_rows(cfg, server, library, checkpoint)
        films = getattr(library, "kind", None) == "movie"
        registered = skipped = 0
        seen = []
        unreadable, touched, listed = set(), set(), {}
        skipped_items: list[dict[str, str]] = []

        def skip(info, reason):
            """One episode this scan could not read: counted, and named with the plain reason (the first few kept)."""
            nonlocal skipped
            skipped += 1
            if len(skipped_items) < _SKIPPED_ITEMS_KEPT:
                skipped_items.append({"episode": _episode_label(info), "reason": reason})

        total = len(rows)
        if progress_callback:
            progress_callback(0, total)
        for index, summary in enumerate(rows):
            checkpoint()
            if not config_current():
                raise ValueError("Library configuration changed during refresh")
            rating_key = str(summary.get("ratingKey", ""))
            if not rating_key.isdecimal():
                skip(summary, "Plex gave it an id Reprise can't use")
                if progress_callback:
                    progress_callback(index + 1, total)
                continue
            if requested is not None and not self._may_hold(cfg, library, summary, requested):
                if progress_callback:
                    progress_callback(index + 1, total)
                continue
            detail = (
                server._plex_http("get", f"/library/metadata/{rating_key}", params={"includeMarkers": "1"})
                .json()["MediaContainer"]
                .get("Metadata", [])
            )
            if len(detail) != 1 or str(detail[0].get("ratingKey", "")) != rating_key:
                raise ValueError("Incomplete Plex episode metadata")
            item = detail[0]
            media_rows = item.get("Media", [])
            if requested is not None:
                listed[rating_key] = {
                    (str(media.get("id")), str(part.get("id")))
                    for media in media_rows
                    for part in media.get("Part", [])
                }
            if not media_rows and requested is None:
                skip(item, "Plex lists no file for it")
                unreadable.add(rating_key)
            for media in media_rows:
                checkpoint()
                parts = media.get("Part", [])
                if requested is not None:
                    try:
                        paths = {self._local_path(cfg, library, part["file"]) for part in parts}
                    except (KeyError, TypeError, ValueError, OSError):
                        continue
                    if not paths.intersection(requested):
                        continue
                if len(parts) != 1:
                    skip(item, "Plex lists more than one file for it" if parts else "Plex lists no file for it")
                    unreadable.add(rating_key)
                    continue
                part = parts[0]
                try:
                    duration = int(media.get("duration") or item["duration"])
                    path = self._local_path(cfg, library, part["file"])
                    if requested is not None and path not in requested:
                        continue
                    if self._local_path(cfg, configured_library, part["file"]) != path:
                        raise ValueError("Plex library roots differ from configured library")
                    size = int(part["size"])
                    before = os.stat(path)
                    if size != before.st_size:
                        raise ValueError("Plex part size differs from local file")
                    signature = _probe(
                        path, part.get("Stream", []), cancel_check=cancel, pause_check=freeze, ffmpeg_path=ffmpeg_path
                    )
                    version = media_version(path, duration, signature)
                    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                        version.device,
                        version.inode,
                        version.size_bytes,
                        version.mtime_ns,
                    ):
                        raise ValueError("Media changed during discovery")
                    part_key = urlsplit(str(part["key"])).path
                    if not part_key.startswith("/library/parts/"):
                        raise ValueError("Invalid Plex part key")
                    wanted = ("credits",) if films else ("intro", "credits")
                    candidates = [
                        {
                            "kind": marker["type"],
                            "start_ms": int(marker["startTimeOffset"]),
                            "end_ms": int(marker["endTimeOffset"]),
                            "source": "plex",
                        }
                        for marker in item.get("Marker", [])
                        if marker.get("type") in wanted
                    ]
                    metadata = {
                        "library_id": str(library.id),
                        "server_url": cfg.url,
                        "title": item.get("title", ""),
                        "remote_part_path": part["file"],
                        "mapping_fingerprint": mapping_fingerprint,
                        "show_title": item.get("grandparentTitle", ""),
                        "show_key": str(item.get("grandparentRatingKey", "")),
                        "season": item.get("parentIndex"),
                        "episode": item.get("index"),
                        "candidates": candidates,
                    }
                    if films:
                        # A film belongs to no show and has no season: its credits are all that is ever asked about.
                        metadata.update(
                            kind="movie",
                            title=_film_title(item),
                            show_title="",
                            show_key=None,
                            season=None,
                            episode=None,
                        )
                    for audio in json.loads(signature)["audio_streams"]:
                        checkpoint()
                        if not config_current():
                            raise ValueError("Library configuration changed during refresh")
                        identity = PlaybackIdentity(
                            machine,
                            str(item["ratingKey"]),
                            str(media["id"]),
                            str(part["id"]),
                            part_key,
                            size,
                            duration,
                            audio["plex_id"],
                        )
                        register(version, identity, server_id=server_id, metadata=metadata)
                        seen.append(identity)
                        if requested is not None:
                            matched.add(path)
                            touched.add(rating_key)
                    registered += 1
                except (
                    KeyError,
                    TypeError,
                    ValueError,
                    OSError,
                    subprocess.SubprocessError,
                    json.JSONDecodeError,
                ) as exc:
                    skip(item, _skip_reason(exc, part))
                    unreadable.add(rating_key)
            if progress_callback:
                progress_callback(index + 1, total)
        checkpoint()
        if not config_current():
            raise ValueError("Library configuration changed during refresh")
        if requested is not None:
            skipped = max(skipped, len(requested - matched))
        return {
            "registered": registered,
            "skipped": skipped,
            "skipped_items": skipped_items,
            "seen": seen,
            # What a refresh may drop afterwards: an episode whose media this scan could not read keeps its entries.
            "unreadable": frozenset(unreadable),
            # Scoped scans only: the media Plex lists now for each episode a requested file was registered for.
            "listed": {key: media for key, media in listed.items() if key in touched and key not in unreadable},
            "config_signature": config_signature,
        }

    def _bindings(self, version_id):
        return self.store.get_bindings(version_id)

    def _binding_problem(self, version, identity, server_id, metadata):
        """Why this binding no longer describes a readable file (a ``STALE_REASONS`` code), or None while it does."""
        # Resolution is deliberately read-only and never contacts Plex or decodes media.
        try:
            cfg = self.registry.get_config(server_id)
            if not cfg or not cfg.enabled or getattr(cfg.type, "value", cfg.type) != "plex":
                return "library_changed"
            if cfg.server_identity and cfg.server_identity != identity.plex_machine_identifier:
                return "library_changed"
            if not cfg.server_identity and metadata.get("server_url") != cfg.url:
                return "library_changed"
            library = next(
                (lib for lib in cfg.libraries if lib.enabled and str(lib.id) == str(metadata["library_id"])), None
            )
            if not library:
                return "library_changed"
            gone = currency(version)
            if gone == "missing":
                roots = self._library_roots(cfg, library)
                # Storage that is not mounted (an array still starting) loses every file at once.
                return "library_unavailable" if roots and not any(os.path.isdir(r) for r in roots) else "file_missing"
            if gone == "unreachable":
                return "file_unavailable"
            if gone:
                return "file_changed"
            if metadata.get("mapping_fingerprint") != _mapping_fingerprint(cfg, library):
                return "library_changed"
            remote_path = metadata.get("remote_part_path")
            if not isinstance(remote_path, str) or not remote_path:
                return "library_changed"
            if identity.duration_ms != version.duration_ms or identity.part_size_bytes != version.size_bytes:
                return "library_changed"
            if self._local_path(cfg, library, remote_path) != version.canonical_path:
                return "library_changed"
            return None
        except (KeyError, TypeError, ValueError, OSError):
            return "library_changed"

    def _valid_binding(self, version, identity, server_id, metadata):
        return self._binding_problem(version, identity, server_id, metadata) is None

    def _unavailable_code(self, version, bindings):
        """Why none of these bindings describes a readable file (a ``STALE_REASONS`` code), or None while one does."""
        if not version or not bindings:
            return "file_missing"
        problems = [self._binding_problem(version, *b) for b in bindings]
        return None if None in problems else problems[0]

    def _references_current(self, result):
        for version_id in {result.media_version_id} | {
            e.reference_version_id for e in result.evidence if e.reference_version_id
        }:
            version = self.store.get_media(version_id)
            bindings = self._bindings(version_id)
            if not version or not bindings or not all(self._valid_binding(version, *b) for b in bindings):
                return False
        target = self.store.get_media(result.media_version_id)
        target_bindings = self._bindings(result.media_version_id)
        for evidence in result.evidence:
            if evidence.verdict.lower() != "match":
                continue
            reference = self.store.get_media(evidence.reference_version_id)
            snapshot = (
                pair_provenance(target, target_bindings, reference, self._bindings(reference.id)) if reference else None
            )
            if not snapshot or evidence.metrics.get("admission_provenance") != snapshot:
                return False
        return True

    def lookup(self, identity):
        return self._lookup(identity)[0]

    def _lookup(self, identity):
        """``(answer, why)``: the answer clients get, and, when its freshness is "stale", the ``STALE_REASONS`` code."""
        answer = {
            "schema_version": 1,
            "resolution": "unresolved",
            "analysis_status": "missing",
            "freshness": "unavailable",
            "media_version_id": None,
            "analysis_id": None,
            "result_revision": None,
            "timebase": "file_presentation_ms",
            "duration_ms": identity.duration_ms,
            "eligible_skip_ranges": [],
            "protected_keep_ranges": [],
            "proposed_ranges": [],
            "reasons": [],
        }
        version = self.store.resolve_binding(identity)
        if not version:
            answer["reasons"].append("No exact registered Plex playback identity")
            return answer, None
        bindings = [b for b in self._bindings(version.id) if b[0] == identity]
        if len(bindings) != 1:
            answer.update(resolution="ambiguous", reasons=["Playback identity is ambiguous"])
            return answer, None
        answer.update(resolution="resolved", media_version_id=version.id)
        problem = self._binding_problem(version, *bindings[0])
        if problem:
            answer.update(freshness="stale", reasons=["Plex mapping or local media has changed or is unavailable"])
            return answer, problem
        result = self.store.get_result(version.id)
        if result is not None and result.analysis_status == "running":
            # "Running" is true only of an analysis a thread here is carrying out. One a stop or restart cut off is
            # ended now, not left to read "Analyzing" for good.
            self.store.fail_stale_analyses(version.id)
            result = self.store.get_result(version.id)
        if result is None:
            answer["freshness"] = "current"
            return answer, None
        why = _result_stale_reason(result) if result.freshness == "stale" else None
        if not self._references_current(result):
            result = replace(
                result, freshness="stale", reasons=(*result.reasons, "Reference media is stale or unavailable")
            )
            why = why or "comparison_changed"
        answer.update(
            analysis_status=result.analysis_status,
            freshness=result.freshness,
            analysis_id=result.analysis_id,
            result_revision=result.result_revision,
            reasons=list(result.reasons),
            eligible_skip_ranges=[asdict(r) for r in self.eligible_ranges(result, identity)],
            protected_keep_ranges=[asdict(r) for r in result.ranges if r.action == "keep"],
            proposed_ranges=[asdict(r) for r in result.ranges if r.review_status == "proposed"],
        )
        if result.analysis_status == "complete" and not answer["eligible_skip_ranges"]:
            answer["reasons"].append("unapproved_or_unverified")
        return answer, why

    def eligible_ranges(self, result, identity):
        version = self.store.resolve_binding(identity)
        if not version or version.id != result.media_version_id:
            return ()
        if self.store.get_result(version.id) != result:
            return ()
        bindings = [b for b in self._bindings(version.id) if b[0] == identity]
        if len(bindings) != 1 or not self._valid_binding(version, *bindings[0]) or not self._references_current(result):
            return ()
        return eligible_ranges(result, identity.audio_stream_id)

    def list_media(self, server_id=None, cursor=None, limit=50, show=None, season=None, search=None):
        rows = self.store.catalogue(server_id)
        needle = str(search).lower() if search else ""
        show_needle = str(show).lower() if show else ""
        matched = []
        seen = set()
        shows = sorted({str(row["metadata"].get("show_title")) for row in rows if row["metadata"].get("show_title")})
        seasons = sorted(
            {
                row["metadata"].get("season")
                for row in rows
                if row["metadata"].get("season") is not None
                and (not show_needle or show_needle in str(row["metadata"].get("show_title", "")).lower())
            }
        )
        for row in rows:
            version_id, meta = row["version_id"], row["metadata"]
            if version_id in seen:
                continue
            seen.add(version_id)
            if show_needle and show_needle not in str(meta.get("show_title", "")).lower():
                continue
            if season is not None and str(meta.get("season")) != str(season):
                continue
            if needle and needle not in (str(meta.get("title", "")) + " " + str(meta.get("show_title", ""))).lower():
                continue
            matched.append(row)
        start = int(cursor or 0)
        limit = min(max(int(limit), 1), 100)
        items = []
        for row in matched[start : start + limit]:
            version_id, meta = row["version_id"], row["metadata"]
            version = self.store.get_media(version_id)
            if version is None:
                continue
            bindings = self._bindings(version_id)
            current, why = self._lookup(bindings[0][0]) if bindings else (None, None)
            stored = self.store.get_result(version_id) if current and current["analysis_status"] != "missing" else None
            items.append(
                {
                    "id": version_id,
                    "server_id": row["server_id"],
                    "title": meta.get("title", ""),
                    "show_title": meta.get("show_title", ""),
                    "season": meta.get("season"),
                    "episode": meta.get("episode"),
                    "duration_ms": version.duration_ms,
                    "analysis_status": current["analysis_status"] if current else "missing",
                    "freshness": current["freshness"] if current else "unavailable",
                    "stale": _stale(why) if why else None,
                    "ranges": _range_counts(stored.ranges if stored else ()),
                    "reasons": list(stored.reasons) if stored else [],
                    "identities": [asdict(b[0]) for b in bindings],
                }
            )
        return {
            "items": items,
            "next_cursor": str(start + limit) if start + limit < len(matched) else None,
            "shows": shows,
            "seasons": seasons,
        }

    def report(self, show, season=None):
        """The Plex-versus-Reprise report for one show or season (see ``reprise.report``); read-only."""
        from .report import build_report

        return build_report(self, show, season)

    def get_result(self, media_version_id):
        bindings = self._bindings(media_version_id)
        if not bindings:
            raise KeyError(media_version_id)
        result, why = self._lookup(bindings[0][0])
        result["identities"] = [asdict(b[0]) for b in bindings]
        result["stale"] = _stale(why) if why else None
        stored = self.store.get_result(media_version_id)
        if stored:
            result["ranges"] = [asdict(r) for r in stored.ranges]
            result["evidence"] = [asdict(e) for e in stored.evidence]
        return result

    def open_stream(self, media_version_id):
        version = self.store.get_media(media_version_id)
        bindings = self._bindings(media_version_id)
        if not version or not bindings or not any(self._valid_binding(version, *b) for b in bindings):
            raise FileNotFoundError("Media is unavailable or no longer current")
        fd = os.open(version.canonical_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            info = os.fstat(fd)
            current = os.stat(version.canonical_path)
            if (
                not stat.S_ISREG(info.st_mode)
                or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino)
                or (info.st_size, info.st_mtime_ns) != (version.size_bytes, version.mtime_ns)
                or not any(self._valid_binding(version, *b) for b in bindings)
            ):
                raise FileNotFoundError("Media changed while opening")
            return os.fdopen(fd, "rb"), version
        except BaseException:
            os.close(fd)
            raise

    def review(self, media_version_id, expected_revision, edits, actor):
        version = self.store.get_media(media_version_id)
        if not version or not any(self._valid_binding(version, *b) for b in self._bindings(media_version_id)):
            raise ValueError("Media is unavailable or stale")
        result = self.store.get_result(media_version_id)
        if result is None or result.freshness != "current" or not self._references_current(result):
            raise ValueError("Analysis is unavailable or stale")
        return self.store.save_review(media_version_id, expected_revision, edits, actor)

    def _refresh_signature(self, server_id, library_id):
        cfg = self.registry.get_config(server_id)
        if not cfg or not cfg.enabled or getattr(cfg.type, "value", cfg.type) != "plex":
            raise ValueError("Select an enabled Plex server")
        library = next((item for item in cfg.libraries if item.enabled and str(item.id) == str(library_id)), None)
        if not library or not str(library_id).isdecimal():
            raise ValueError("Select an enabled Plex library")
        return json.dumps([cfg.url, cfg.server_identity, cfg.auth, _mapping_fingerprint(cfg, library)], sort_keys=True)

    def enqueue_refresh(self, server_id, library_id, *, principal="owner"):
        self._refresh_signature(server_id, library_id)
        owner = getattr(principal, "id", None) or str(principal or "owner")
        request = {"server_id": server_id, "library_id": str(library_id)}
        with self._enqueue_lock:
            for job in self.job_manager.get_all_jobs():
                if (
                    job.kind == "reprise"
                    and (job.config or {}).get("refresh_request") == request
                    and job.status.value in ("pending", "running")
                ):
                    return self._job_json(job)
            job = self.job_manager.create_job(
                library_name="Reprise library refresh",
                kind="reprise",
                config={"refresh_request": request, "reprise_owner": owner},
            )
            from ..web.routes.job_runner import _start_job_async

            _start_job_async(job.id, dict(job.config))
            return self._job_json(job)

    def enqueue_library_analysis(
        self,
        server_id,
        library_id,
        *,
        candidate_paths=None,
        trigger="scheduled",
        parent_schedule_id=None,
        priority=None,
    ):
        """Queue one durable discovery-and-analysis job for one exact Plex library."""
        from ..web.jobs import PRIORITY_LOW, parse_priority
        from ..web.routes.job_runner import RECENTLY_ADDED_JOB_SOURCE, _start_job_async

        self._refresh_signature(server_id, library_id)
        if candidate_paths is not None:
            if not candidate_paths or any(
                not isinstance(path, str) or not os.path.isabs(path) for path in candidate_paths
            ):
                raise ValueError("Select at least one absolute candidate path")
            candidate_paths = sorted({os.path.realpath(path) for path in candidate_paths})
        request = {"server_id": server_id, "library_id": str(library_id), "candidate_paths": candidate_paths}
        source = {"recently_added": RECENTLY_ADDED_JOB_SOURCE, "autopilot": AUTOPILOT_JOB_SOURCE}.get(
            trigger, "schedule"
        )
        with self._enqueue_lock:
            for job in self.job_manager.get_all_jobs():
                config = job.config or {}
                if (
                    job.kind == "reprise"
                    and job.status.value in ("pending", "running")
                    and job.parent_schedule_id == (parent_schedule_id or "")
                    and config.get("source") == source
                    and config.get("library_analysis_request") == request
                ):
                    return self._job_json(job)
            job = self.job_manager.create_job(
                library_name="Reprise: new files" if trigger == "autopilot" else "Reprise analysis",
                kind="reprise",
                server_id=server_id,
                library_id=str(library_id),
                parent_schedule_id=parent_schedule_id or "",
                priority=parse_priority(priority) if priority is not None else PRIORITY_LOW,
                config={"library_analysis_request": request, "reprise_owner": "owner", "source": source},
            )
            _start_job_async(job.id, dict(job.config))
            return self._job_json(job)

    def enqueue_analysis(self, media_version_ids, *, principal, idempotency_key=None):
        ids = sorted(set(media_version_ids))
        if not ids:
            raise ValueError("Select at least one media version")
        for mid in ids:
            version = self.store.get_media(mid)
            bindings = self._bindings(mid) if version else []
            if not bindings:
                raise AnalysisRefused(NOT_IN_LIBRARY)
            if not any(self._valid_binding(version, *b) for b in bindings):
                raise AnalysisRefused(CHANGED_ON_DISK)
        owner = getattr(principal, "id", None) or str(principal or "owner")
        with self._enqueue_lock:
            for job in self.job_manager.get_all_jobs():
                c = job.config or {}
                if job.kind != "reprise" or c.get("reprise_owner") != owner:
                    continue
                if idempotency_key and c.get("idempotency_key") == idempotency_key:
                    if c.get("media_version_ids") != ids:
                        raise ValueError("Idempotency key was already used for different media")
                    return self._job_json(job)
                if c.get("media_version_ids") == ids and job.status.value in ("pending", "running"):
                    return self._job_json(job)
            job = self.job_manager.create_job(
                library_name="Reprise analysis",
                kind="reprise",
                config={"media_version_ids": ids, "reprise_owner": owner, "idempotency_key": idempotency_key},
            )
            from ..web.routes.job_runner import _start_job_async

            _start_job_async(job.id, dict(job.config))
            return self._job_json(job)

    def enqueue_backfill(self, media_version_ids, label):
        """Queue the slow, background analysis of some files for Reprise's own automatic work, in the order given. It
        runs below everything the owner or a new import asks for. Files that can't be read any more are left out;
        returns the job, or None when none was left."""
        from ..web.jobs import PRIORITY_LOW
        from ..web.routes.job_runner import _start_job_async

        ids = []
        for mid in dict.fromkeys(media_version_ids):
            version = self.store.get_media(mid)
            bindings = self._bindings(mid) if version else []
            if bindings and any(self._valid_binding(version, *b) for b in bindings):
                ids.append(mid)
        if not ids:
            return None
        with self._enqueue_lock:
            for job in self.job_manager.get_all_jobs():
                c = job.config or {}
                if (
                    job.kind == "reprise"
                    and c.get("source") == AUTOPILOT_BACKFILL_SOURCE
                    and c.get("media_version_ids") == ids
                    and job.status.value in ("pending", "running")
                ):
                    return self._job_json(job)
            job = self.job_manager.create_job(
                library_name=f"Reprise backfill: {label}"[:80],
                kind="reprise",
                priority=PRIORITY_LOW,
                config={"media_version_ids": ids, "reprise_owner": "owner", "source": AUTOPILOT_BACKFILL_SOURCE},
            )
            _start_job_async(job.id, dict(job.config))
            return self._job_json(job)

    def queued_candidate_paths(self):
        """The files already waiting in, or being read by, a library analysis that names its files: a new look at the
        library does not queue them a second time."""
        paths = set()
        for job in self.job_manager.get_all_jobs():
            request = (job.config or {}).get("library_analysis_request") or {}
            if job.kind == "reprise" and job.status.value in ("pending", "running") and request.get("candidate_paths"):
                paths.update(request["candidate_paths"])
        return paths

    def _is_autopilot_job(self, job_id):
        """Whether Reprise's own automatic work queued this job (new files, or the slow backfill)."""
        job = self.job_manager.get_job(job_id)
        return bool(job and (job.config or {}).get("source") in AUTOPILOT_SOURCES)

    def _is_current(self, media_id, bindings):
        """Whether this file already has a finished analysis that still counts: what Reprise's own looks leave alone."""
        answer = self._lookup(bindings[0][0])[0]
        return (
            answer.get("media_version_id") == media_id
            and answer.get("analysis_status") == "complete"
            and answer.get("freshness") == "current"
        )

    def _notify_autopilot(self, method, *args):
        """Tell Reprise's automatic work about something it raises an alert for. It never affects the job that said it."""
        autopilot = self.autopilot
        if autopilot is None:
            return
        try:
            getattr(autopilot, method)(*args)
        except Exception:
            logger.opt(exception=True).warning("Reprise's automatic work could not note {}", method)

    def enqueue_preview(self, media_version_id, start_ms, end_ms, audio_stream_id, *, principal="owner"):
        if type(start_ms) is not int or type(end_ms) is not int or not isinstance(audio_stream_id, str):
            raise ValueError("Invalid preview window")
        version = self.store.get_media(media_version_id)
        bindings = [b for b in self._bindings(media_version_id) if b[0].audio_stream_id == audio_stream_id]
        if (
            not version
            or not 0 <= start_ms < end_ms <= version.duration_ms
            or end_ms - start_ms > 90000
            or not any(self._valid_binding(version, *b) for b in bindings)
        ):
            raise ValueError("Preview is unavailable")
        owner = getattr(principal, "id", None) or str(principal or "owner")
        spec = {
            "media_version_id": media_version_id,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "audio_stream_id": audio_stream_id,
        }
        with self._enqueue_lock:
            for job in self.job_manager.get_all_jobs():
                if (
                    job.kind == "reprise"
                    and job.config.get("reprise_owner") == owner
                    and job.config.get("preview_request") == spec
                ):
                    if job.status.value in ("pending", "running"):
                        return self._job_json(job)
                    if job.status.value == "completed":
                        try:
                            stream, _ = self.open_preview(job.id)
                            stream.close()
                            return self._job_json(job)
                        except (OSError, ValueError):
                            pass
            job = self.job_manager.create_job(
                library_name="Reprise preview", kind="reprise", config={"preview_request": spec, "reprise_owner": owner}
            )
            from ..web.routes.job_runner import _start_job_async

            _start_job_async(job.id, dict(job.config))
            return self._job_json(job)

    def open_preview(self, job_id):
        from types import SimpleNamespace

        job = self.job_manager.get_job(job_id)
        if not job or job.kind != "reprise" or job.status.value != "completed":
            raise FileNotFoundError("Preview unavailable")
        spec = job.config.get("preview_request") or {}
        artifact = job.config.get("preview_artifact") or {}
        version = self.store.get_media(spec.get("media_version_id"))
        bindings = self._bindings(spec.get("media_version_id"))
        if not version or not any(
            b[0].audio_stream_id == spec.get("audio_stream_id") and self._valid_binding(version, *b) for b in bindings
        ):
            raise FileNotFoundError("Preview source unavailable")
        root = os.path.realpath(os.path.join(self.job_manager.config_dir, "reprise-previews"))
        path = os.path.realpath(artifact.get("path", ""))
        if not _inside(path, root):
            raise FileNotFoundError("Preview unavailable")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            st = os.fstat(fd)
            current = os.stat(path)
            if not stat.S_ISREG(st.st_mode) or (st.st_dev, st.st_ino) != (current.st_dev, current.st_ino):
                raise FileNotFoundError("Preview changed")
            return os.fdopen(fd, "rb"), SimpleNamespace(canonical_path=path, size_bytes=st.st_size)
        except BaseException:
            os.close(fd)
            raise

    def _job_json(self, job):
        data = {
            "id": job.id,
            "status": job.status.value,
            "progress": job.progress.to_dict(),
            "created_at": job.created_at,
            "error": "Job failed" if job.error and job.status.value != "completed" else None,
            "message": None,
            "pause_requested": self.job_manager.is_pause_requested(job.id),
        }
        config = job.config or {}
        data["operation"] = (
            "refresh" if config.get("refresh_request") else "preview" if config.get("preview_request") else "analysis"
        )
        if job.status.value == "completed" and job.error:
            data["warning"] = job.error
        if config.get("analysis_result"):
            data["analysis"] = config["analysis_result"]
        if config.get("refresh_result"):
            data["refresh"] = config["refresh_result"]
        if config.get("media_version_ids"):
            data["media_version_ids"] = list(config["media_version_ids"])
        note = recovery.describe(config.get(recovery.QUEUE_KEY) or {})
        if note:
            data["recovery"] = note
        outcome = self._analysis_outcome(job)
        if outcome:
            data["outcome"] = outcome
            if outcome["state"] in ("failed", "cancelled"):
                data["message"] = outcome["message"]
        artifact = (job.config or {}).get("preview_artifact")
        if job.status.value == "completed" and artifact:
            data["preview"] = {
                "url": "/api/reprise/previews/" + job.id + "/stream",
                "origin_ms": artifact["origin_ms"],
                "duration_ms": artifact["duration_ms"],
                "mime_type": artifact["mime_type"],
            }
        return data

    def _record_outcome(self, job_id, media_id, state, message):
        """Remember how one requested episode ended, so the queue can say so without reading every result."""
        job = self.job_manager.get_job(job_id)
        config = (job.config if job else None) or {}
        if config.get("media_version_ids"):
            outcomes = {**(config.get("analysis_outcomes") or {}), media_id: {"state": state, "message": message}}
            self.job_manager.merge_job_config(job_id, {"analysis_outcomes": outcomes})

    @staticmethod
    def _analysis_outcome(job):
        """What an owner-requested analysis ended with, in sentences, from what its run recorded per episode."""
        config, status = job.config or {}, job.status.value
        ids, recorded = config.get("media_version_ids"), config.get("analysis_outcomes") or {}
        if not ids or status not in ("completed", "failed", "cancelled") or (status == "completed" and not recorded):
            return None
        stopped = {"state": "cancelled", "message": "Not analyzed because the job stopped first."}
        per_media = [{"id": media_id, **recorded.get(media_id, stopped)} for media_id in ids]
        if status == "cancelled":
            return {"state": "cancelled", "message": "Analysis was cancelled.", "per_media": per_media}
        if status == "failed":
            failed = next((entry for entry in per_media if entry["state"] == "failed"), None)
            message = failed["message"] if failed else "Analysis could not be completed."
            return {"state": "failed", "message": message, "per_media": per_media}
        found = [entry for entry in per_media if entry["state"] == "found"]
        suggested = [entry for entry in per_media if entry["state"] == "suggested"]
        failed = [entry for entry in per_media if entry["state"] == "failed"]
        set_aside = [entry for entry in per_media if entry["state"] == "set_aside"]
        many = len(per_media) > 1
        if set_aside and not many:
            return {"state": "failed", "message": set_aside[0]["message"], "per_media": per_media}
        # A finished job only has failed entries for episodes it could not analyze (the file went missing or changed, or
        # Reprise refuses the media) and set-aside ones for episodes a stop cut off too often: the rest ran.
        left_out = ""
        if many and failed:
            left_out += (
                f" {len(failed)} {'episode' if len(failed) == 1 else 'episodes'} could not be analyzed "
                "(open each to see why)."
            )
        if many and set_aside:
            one = len(set_aside) == 1
            left_out += (
                f" {len(set_aside)} {'episode was' if one else 'episodes were'} set aside after "
                f"{recovery.MAX_ATTEMPTS} interrupted attempts; analyze {'it' if one else 'them'} again to retry."
            )
        if many and not set_aside and len(failed) == len(per_media):
            return {
                "state": "failed",
                "message": "No episode could be analyzed because its file is missing, changed or unavailable.",
                "per_media": per_media,
            }
        if many and len(failed) + len(set_aside) == len(per_media):
            return {
                "state": "failed",
                "message": "No episode could be analyzed. Open each one to see why.",
                "per_media": per_media,
            }
        if found:
            message = (
                f"Found ranges to review in {len(found)} of {len(per_media)} episodes." if many else found[0]["message"]
            )
            if many and suggested:
                message += f" Suggestions were made for {len(suggested)} more."
            return {"state": "found", "message": message + left_out, "per_media": per_media}
        if suggested:
            message = (
                f"Suggestions were made for {len(suggested)} of {len(per_media)} episodes; Reprise could not confirm them."
                if many
                else suggested[0]["message"]
            )
            return {"state": "suggested", "message": message + left_out, "per_media": per_media}
        analyzed = len(per_media) - len(failed) - len(set_aside)
        message = f"Nothing found in {analyzed} episodes." if many else per_media[0]["message"]
        return {"state": "nothing_found", "message": message + left_out, "per_media": per_media}

    def get_job(self, job_id, principal=None):
        job = self.job_manager.get_job(job_id)
        if not job or job.kind != "reprise":
            raise KeyError(job_id)
        if principal is not None and (job.config or {}).get("reprise_owner") != (
            getattr(principal, "id", None) or str(principal)
        ):
            raise KeyError(job_id)
        return self._job_json(job)

    def list_jobs(self):
        return {"items": [self._job_json(j) for j in self.job_manager.get_all_jobs() if j.kind == "reprise"]}

    def control_job(self, job_id, action):
        self.get_job(job_id)
        fn = {
            "pause": self.job_manager.request_pause,
            "resume": self.job_manager.request_resume,
            "cancel": self.job_manager.cancel_job_if_active,
        }.get(action)
        if not fn:
            raise ValueError("Unknown job action")
        changed = fn(job_id)
        if action == "resume" and changed:
            from ..web.routes.job_runner import _start_job_async

            # The owner takes over a job that waited or paused after a restart: the loop guard starts afresh.
            self._queue_set(job_id, recovery.owner_resumed)
            _start_job_async(job_id)
        return self.get_job(job_id)

    # ── restart recovery: a job keeps its place (see ``recovery``), so a restart is not a failure ──────────────────

    def _queue(self, job_id):
        """The job's saved place: what is done, what was cut off, what is left (``recovery``)."""
        job = self.job_manager.get_job(job_id)
        return recovery.queue_of(job.config if job else None)

    def _queue_set(self, job_id, update):
        """Change a job's saved queue with ``update(queue) -> queue`` and save it, under the one lock every writer of a
        queue takes. Nothing is written when ``update`` changed nothing."""
        with recovery.LOCK:
            before = self._queue(job_id)
            after = update(before)
            if after != before:
                self.job_manager.merge_job_config(job_id, {recovery.QUEUE_KEY: after})
            return after

    def _setting(self, key, default):
        settings = getattr(self.registry, "settings", None)
        return default if settings is None else settings.get(key, default)

    def _auto_resume(self):
        """Whether a restart carries unfinished analyses on by itself (the setting ``reprise_auto_resume``)."""
        raw = self._setting("reprise_auto_resume", True)
        return raw if isinstance(raw, bool) else str(raw).strip().lower() in ("true", "1", "yes")

    def _resume_delay(self):
        """Seconds the first resume waits after a start (``reprise_resume_delay_seconds``), so storage and Plex settle."""
        try:
            seconds = int(self._setting("reprise_resume_delay_seconds", recovery.DEFAULT_DELAY_SECONDS))
        except (TypeError, ValueError):
            return recovery.DEFAULT_DELAY_SECONDS
        return max(0, min(recovery.MAX_DELAY_SECONDS, seconds))

    def _processing_paused(self):
        return bool(getattr(getattr(self.registry, "settings", None), "processing_paused", False))

    def recover_after_restart(self, now=None):
        """Settle the Reprise jobs the last stop left unfinished; returns the ids of those that will resume by themselves.

        A restart no longer fails a running job (``JobManager.consume_interrupted_reprise_jobs``): it waits, with its
        queue, for ``resume_after_restart``. An episode still marked running was cut off without warning and keeps the
        attempt that was counted when it began. A preview is no queue: it is given up, to be created again.
        """
        now = now or datetime.now(UTC)
        automatic = self._auto_resume()
        waiting = []
        for job in self.job_manager.consume_interrupted_reprise_jobs():
            try:
                if (job.config or {}).get("preview_request"):
                    self.job_manager.complete_job(job.id, error=recovery.PREVIEW_INTERRUPTED)
                    continue
                held = self.job_manager.is_pause_requested(job.id)
                self._queue_set(job.id, lambda queue, held=held: recovery.note_restart(queue, now, wait=not held))
                if held:
                    continue  # the owner (or a schedule) holds it: whoever resumes it, it carries on from its queue
                if automatic:
                    waiting.append(job.id)
                else:
                    self._pause_for_owner(job.id, "switched_off")
            except Exception:
                logger.exception("Reprise job {} could not be prepared to resume after the restart", job.id)
                self._pause_for_owner(job.id, "resume_failed")
        if waiting:
            logger.info(
                "{} Reprise job(s) stopped by the restart will resume in about {} seconds",
                len(waiting),
                self._resume_delay(),
            )
        return waiting

    def _pause_for_owner(self, job_id, reason):
        """Stop resuming a job by itself and give it to its owner: held like any paused job, with a plain notice."""
        message = recovery.PAUSED_MESSAGES[reason]
        try:
            with recovery.LOCK:
                self.job_manager.request_pause(job_id)
                self._queue_set(job_id, lambda queue: recovery.pause(queue, reason))
            self.job_manager.update_progress(job_id, current_item="")
            self.job_manager.add_log(job_id, f"WARNING - {message}")
        except Exception:
            logger.exception("Reprise job {} could not be paused for its owner", job_id)
        logger.warning("Reprise job {} is waiting for its owner: {}", job_id[:8], message)
        if reason == "kept_stopping":
            self._notify_autopilot("note_paused", job_id)

    def resume_after_restart(self, now=None):
        """One pass over the jobs waiting to carry on after a restart: start those that may run, hand to their owner
        those that must not run by themselves, leave the rest waiting. Returns ``(started, still_waiting)`` job ids.

        Nothing starts while the media folders or Plex are unreachable (that wait is not an attempt and not a resume),
        and a job that keeps stopping without finishing an episode is paused rather than started once more.
        """
        from ..web.routes.job_runner import _start_job_async

        now = now or datetime.now(UTC)
        started, waiting = [], []
        for job in self.job_manager.get_all_jobs():
            queue = recovery.queue_of(job.config)
            if job.kind != "reprise" or job.status.value != "pending" or queue.get("recovery") != "waiting":
                continue
            try:
                outcome = self._resume_one(job, queue, now, _start_job_async)
            except Exception:
                logger.exception("Reprise job {} could not be resumed", job.id)
                self._pause_for_owner(job.id, "resume_failed")
                continue
            if outcome == "started":
                started.append(job.id)
            elif outcome == "waiting":
                waiting.append(job.id)
        return started, waiting

    def _resume_one(self, job, queue, now, start):
        """``"started"``, ``"waiting"`` (look again later) or None (no longer waiting for the resume pass)."""
        if self.job_manager.is_pause_requested(job.id):
            self._queue_set(job.id, recovery.release)  # taken by its owner: they resume it when they choose
            return None
        if self._processing_paused():
            return "waiting"
        blocked = self._resume_blocker(job)
        if blocked:
            if recovery.waited_too_long(queue, now):
                self._pause_for_owner(job.id, "media_unavailable")
                return None
            if queue.get("blocked") != blocked:
                self._queue_set(job.id, lambda current: recovery.block(current, blocked))
            return "waiting"
        reason = recovery.verdict(queue, job.config, now)
        if reason:
            self._pause_for_owner(job.id, reason)
            return None
        self._queue_set(job.id, lambda current: recovery.record_resume(current, now))
        start(job.id)
        return "started"

    def _resume_libraries(self, job):
        """The ``(server, library)`` pairs the job reads next: its library, or the libraries of its next episodes."""
        config = job.config or {}
        request = config.get("library_analysis_request") or config.get("refresh_request")
        if request:
            return {(request["server_id"], str(request["library_id"]))}
        return {
            (server_id, str(meta.get("library_id")))
            for media_id in recovery.todo(config, recovery.CHECK_EPISODES)
            for _, server_id, meta in self._bindings(media_id)
        }

    def _resume_blocker(self, job):
        """``"plex"`` or ``"folders"`` while the media this job reads cannot be reached yet, else None."""
        for server_id, library_id in sorted(self._resume_libraries(job)):
            cfg, server = self.registry.get_config(server_id), self.registry.get(server_id)
            if not cfg or not server:
                continue  # the server is gone or off: the run says so, episode by episode
            try:
                reachable = server.test_connection().ok
            except Exception:
                reachable = False
            if not reachable:
                return "plex"
            library = next((lib for lib in cfg.libraries if str(lib.id) == library_id), None)
            roots = self._library_roots(cfg, library) if library else []
            if roots and not any(_has_files(root) for root in roots):
                return "folders"
        return None

    def start_resume_timer(self, delay_seconds=None):
        """Resume the waiting jobs after the start-up delay, then look at them every few seconds until none waits."""
        delay = self._resume_delay() if delay_seconds is None else delay_seconds
        thread = threading.Thread(target=self._resume_when_ready, args=(delay,), name="reprise-resume", daemon=True)
        thread.start()
        return thread

    def _resume_when_ready(self, delay):
        stop = self._resume_stop
        if stop.wait(delay):
            return
        try:
            while True:
                _, waiting = self.resume_after_restart()
                if not waiting or stop.wait(recovery.POLL_SECONDS):
                    return
        except Exception:
            logger.exception("Reprise could not resume the jobs the restart stopped")

    def checkpoint_for_shutdown(self):
        """The app was told to stop: leave every running analysis ready to be resumed, in the few seconds a stop allows.

        The episode being analyzed is cut off by the stop, not by anything wrong with it: its attempt is given back, so a
        deploy never counts toward setting an episode aside. One quick write per running job; the job itself stays
        running, and the next start resumes it (a job is only failed or completed by what happened to its episodes).
        """
        self._resume_stop.set()
        for job in self.job_manager.get_all_jobs():
            running = recovery.queue_of(job.config).get("running")
            if job.kind != "reprise" or job.status.value != "running" or not running:
                continue
            try:
                self._queue_set(job.id, lambda queue, running=running: recovery.give_back(queue, running))
            except Exception:
                logger.opt(exception=True).warning("Could not checkpoint Reprise job {} for the stop", job.id[:8])

    def _mark_set_aside(self, job_id, media_id):
        """Say, on the episode itself, why it was not analyzed (the job's own notes carry the reason too)."""
        self._notify_autopilot("note_set_aside", job_id, media_id)
        try:
            run = self.store.begin_analysis(media_id, job_id, "set-aside")
        except Exception:
            return
        try:
            self.store.fail_analysis(run, "cancelled", recovery.SET_ASIDE_REASON)
        except Exception:
            logger.opt(exception=True).debug("Could not record why episode {} was set aside", media_id)
        finally:
            self.store.release_analysis(run)

    def _run_refresh(self, job_id, request, pause_check, *, for_analysis=False, ffmpeg_path=None):
        from ..markers.freeze import Freeze
        from .media import Cancelled

        server_id, library_id = request["server_id"], request["library_id"]

        def cancel():
            return self.job_manager.is_cancellation_requested(job_id)

        freeze = Freeze.of(pause_check)
        with self._enqueue_lock:
            lock = self._refresh_locks.setdefault((server_id, library_id), threading.Lock())
        acquired = False
        registered_ids = set()
        try:
            # Refresh probes files on this host and must not consume a GPU-only
            # or remote-only worker policy. The shared gate still limits jobs.
            from ..jobs.group_runtime import current_groups
            from ..worker_groups import group_is_available, member_policies, supports_job

            while True:
                freeze.hold(cancel_check=cancel)
                if cancel():
                    raise Cancelled("Library refresh cancelled")
                groups = current_groups()
                if groups is None or any(
                    member["enabled"]
                    and member["resource"] == "cpu"
                    and not member.get("node")
                    and supports_job(member, "previews")
                    and group_is_available(member)
                    for member in member_policies(groups)
                ):
                    break
                self.job_manager.update_progress(job_id, current_item="Waiting for a local CPU preview worker")
                time.sleep(0.2)
            while not acquired:
                freeze.hold(cancel_check=cancel)
                if cancel():
                    raise Cancelled("Library refresh cancelled")
                acquired = lock.acquire(timeout=0.1)

            def register(version, identity, *, server_id, metadata):
                if not self.job_manager.commit_if_active(
                    job_id, lambda: self.store.register_media(version, identity, server_id=server_id, metadata=metadata)
                ):
                    raise Cancelled("Library refresh cancelled")
                registered_ids.add(version.id)

            def progress(processed, total):
                if cancel():
                    raise Cancelled("Library refresh cancelled")
                self.job_manager.update_progress(
                    job_id,
                    processed_items=processed,
                    total_items=total,
                    percent=(100 * processed / total if total else 0),
                    current_item=f"Indexing episodes: {processed} of {total}",
                )

            scanned = self._scan_library(
                server_id,
                library_id,
                cancel_check=cancel,
                pause_check=freeze,
                progress_callback=progress,
                register=register,
                ffmpeg_path=ffmpeg_path,
                candidate_paths=request.get("candidate_paths"),
            )
            summary = {
                "server_id": server_id,
                "library_id": library_id,
                "registered": scanned["registered"],
                "skipped": scanned["skipped"],
                "removed": 0,
            }
            if scanned["skipped_items"]:
                summary["skipped_items"] = scanned["skipped_items"]

            def reconcile():
                if self._refresh_signature(server_id, library_id) != scanned["config_signature"]:
                    raise ValueError("Library configuration changed during refresh")
                # A scan that could not read some episodes still shows which other entries Plex dropped (a file a
                # Sonarr upgrade replaced, say): those go, and the unreadable episodes keep theirs.
                if request.get("candidate_paths") is None:
                    summary["removed"] = self.store.reconcile_library(
                        server_id, library_id, scanned["seen"], keep_rating_keys=scanned["unreadable"]
                    )
                else:
                    summary["removed"] = self.store.drop_unlisted_media(server_id, library_id, scanned["listed"])
                self.job_manager.merge_job_config(job_id, {"refresh_result": summary})

            # New files found by Reprise itself go on with the ones that were read: a file that can't be read yet must
            # not hold back the others, and a later look at Plex tries it again.
            carry_on = for_analysis and bool(registered_ids) and self._is_autopilot_job(job_id)
            if carry_on and scanned["skipped"]:
                self.job_manager.add_log(
                    job_id,
                    f"WARNING - {scanned['skipped']} file(s) could not be read yet; they will be looked at again",
                )
            if scanned["skipped"] and not carry_on:
                # A refresh of the whole library whose only trouble is episodes it could not read did its job: it
                # completes with a warning naming them. A scan of specific files (a webhook, an analysis) has no use for
                # a partial answer, so it still fails.
                whole_library = not for_analysis and request.get("candidate_paths") is None

                def finish_partial():
                    reconcile()
                    if whole_library:
                        count = scanned["skipped"]
                        one = count == 1
                        self.job_manager.complete_job(
                            job_id,
                            warning=(
                                f"{count} {'episode' if one else 'episodes'} could not be read and kept "
                                f"{'its' if one else 'their'} earlier {'entry' if one else 'entries'}"
                            ),
                        )
                    else:
                        self.job_manager.complete_job(
                            job_id,
                            error="Library refresh incomplete; episodes that could not be read kept their entries",
                        )

                if not self.job_manager.commit_if_active(job_id, finish_partial):
                    raise Cancelled("Library refresh cancelled")
                return

            if not self.job_manager.commit_if_active(job_id, reconcile, complete=not for_analysis):
                raise Cancelled("Library refresh cancelled")
            return sorted(registered_ids)
        except Cancelled:
            self.job_manager.cancel_job(job_id)
        finally:
            if acquired:
                lock.release()

    def online_lookups_enabled(self):
        from . import online

        return online.switch_on()

    def set_online_lookups_enabled(self, enabled):
        from . import online

        if type(enabled) is not bool:
            raise ValueError("Invalid setting")
        online.set_switch(enabled)

    def _online_lookups(self):
        """This job's online lookups (None when the switch is off or settings cannot be read): nothing here may fail an
        analysis, so a problem only means no online times."""
        from . import online

        try:
            lookups = online.OnlineLookups(self.store)
        except Exception as exc:
            logger.warning("Online lookups are unavailable: {}", type(exc).__name__)
            return None
        return lookups if lookups.sources else None

    def _server_external_ids(self, server_id, rating_key):
        return self.registry.get(server_id).get_external_ids(rating_key)

    def _film_windows(self, lookups, version, identity, server_id, meta, job):
        """Where the online databases put a film's credits, on this file's own clock: ``{media version id: (OnlineWindow,)}``.
        Only the credits are asked for: a film's opening is not an intro that repeats."""
        from . import online

        ids = online.film_ids(version.canonical_path, lambda: self._server_external_ids(server_id, identity.rating_key))
        if ids.kind != "movie":
            return {}
        windows = lookups.windows(
            ids,
            duration_ms=version.duration_ms,
            frame_rate=online.frame_rate_of(version.stream_signature),
            plex=[CandidateWindow(**c) for c in meta.get("candidates", [])],
            priority=job.priority,
            cancel_check=lambda: self.job_manager.is_cancellation_requested(job.id),
        )
        credits = tuple(window for window in windows if window.kind == "credits")
        return {version.id: credits} if credits else {}

    def _online_windows(self, lookups, version, bindings, references, job):
        """Where the online databases and the files' own chapter marks put the intro and credits of this episode and
        of each reference, each read on its own file's clock: ``{media version id: (OnlineWindow, ...)}``. Every
        episode is asked at most once; an episode with no answer is simply missing. The chapter marks need no online
        lookup, so they are read whether or not it is on."""
        from .media import paired_ffprobe

        found = self._database_windows(lookups, version, bindings, references, job)
        try:
            files = [version, *references]
            ffprobe = paired_ffprobe(ffmpeg_binary())
            marks = {f.id: chapters.read(ffprobe, f.canonical_path) for f in files}
            kinds = ("credits",) if bindings[0][2].get("kind") == "movie" else ("intro", "credits")
            found = chapters.merge(
                found, chapters.windows(marks, {f.id: f.duration_ms for f in files}, version.id, kinds=kinds)
            )
        except Exception as exc:
            logger.warning("Chapter marks could not be read: {}", type(exc).__name__)
        return found

    def _database_windows(self, lookups, version, bindings, references, job):
        from . import online

        if lookups is None:
            return {}
        identity, server_id, meta = bindings[0]
        try:
            if meta.get("kind") == "movie":
                return self._film_windows(lookups, version, identity, server_id, meta, job)
            key = (server_id, meta.get("show_key"))
            ids = self._show_ids.get(key) if key[1] else None
            if ids is None:
                ids = online.show_ids(
                    version.canonical_path, lambda: self._server_external_ids(server_id, identity.rating_key)
                )
                if key[1] and ids.is_episode:
                    self._show_ids[key] = ids
            if not ids.is_episode:
                return {}
            found = {}
            files = [(version, meta)] + [
                (ref, (self._bindings(ref.id) or [(None, None, {})])[0][2]) for ref in references
            ]
            for file, file_meta in files:
                season, episode = file_meta.get("season"), file_meta.get("episode")
                if not isinstance(season, int) or not isinstance(episode, int):
                    continue
                windows = lookups.windows(
                    replace(ids, season=season, episode=episode),
                    duration_ms=file.duration_ms,
                    frame_rate=online.frame_rate_of(file.stream_signature),
                    plex=[CandidateWindow(**c) for c in file_meta.get("candidates", [])],
                    priority=job.priority,
                    cancel_check=lambda: self.job_manager.is_cancellation_requested(job.id),
                )
                if windows:
                    found[file.id] = windows
            return found
        except Exception as exc:
            logger.warning("Online lookups failed: {}", type(exc).__name__)
            return {}

    def run_job(self, job_id):
        from .analysis import (
            ENDING_WINDOW,
            FALLBACK_WINDOW_MS,
            MOVIE_NOTE,
            NO_OTHER_EPISODES,
            NO_REFERENCES,
            OPENING_WINDOW,
            OTHER_EPISODES_NOT_READY,
            analyze,
        )

        job = self.job_manager.get_job(job_id)
        from ..web.settings_manager import get_settings_manager

        ffmpeg_path = ffmpeg_binary()
        # Frames scanned for one episode serve the episodes that compare with it (kept with the other state, bounded).
        config_dir = getattr(self.job_manager, "config_dir", None)
        if config_dir:
            scan_cache.configure(os.path.join(config_dir, "reprise-scan-cache.db"))

        def paused():
            return self.job_manager.is_pause_requested(job_id) or get_settings_manager().processing_paused

        if job.config.get("refresh_request"):
            self._run_refresh(job_id, job.config["refresh_request"], paused, ffmpeg_path=ffmpeg_path)
            return
        library_request = job.config.get("library_analysis_request")
        if library_request:
            # A library read before a restart is not read again: its episodes are in the job's saved queue.
            ids = recovery.queue_of(job.config).get("ids")
            if ids is None:
                ids = self._run_refresh(job_id, library_request, paused, for_analysis=True, ffmpeg_path=ffmpeg_path)
                if ids is None:
                    return
                if not ids:
                    if not self.job_manager.commit_if_active(job_id, lambda: None, complete=True):
                        self.job_manager.cancel_job(job_id)
                    return
                self._queue_set(job_id, lambda current: {**current, "ids": list(ids)})
        if job.config.get("preview_request"):
            from .preview import build_preview

            spec = job.config["preview_request"]
            version = self.store.get_media(spec["media_version_id"])
            bindings = self._bindings(spec["media_version_id"])
            if not version or not any(
                b[0].audio_stream_id == spec["audio_stream_id"] and self._valid_binding(version, *b) for b in bindings
            ):
                raise ValueError("Preview source unavailable")
            from .media import Cancelled

            try:
                artifact = build_preview(
                    version,
                    start_ms=spec["start_ms"],
                    end_ms=spec["end_ms"],
                    audio_stream_id=spec["audio_stream_id"],
                    cache_dir=os.path.join(self.job_manager.config_dir, "reprise-previews"),
                    cancel_check=lambda: self.job_manager.is_cancellation_requested(job_id),
                    pause_check=paused,
                    ffmpeg_threads=1,
                    ffmpeg_path=ffmpeg_path,
                )
            except Cancelled:
                self.job_manager.cancel_job(job_id)
                return
            if self.job_manager.is_cancellation_requested(job_id):
                self.job_manager.cancel_job(job_id)
                return
            if not any(self._valid_binding(version, *b) for b in bindings):
                raise ValueError("Preview source changed")
            artifact_data = asdict(artifact)
            artifact_data["path"] = str(artifact_data["path"])
            if not self.job_manager.commit_if_active(
                job_id,
                lambda: self.job_manager.merge_job_config(job_id, {"preview_artifact": artifact_data}),
                complete=True,
            ):
                self.job_manager.cancel_job(job_id)
            return
        if not library_request:
            ids = job.config["media_version_ids"]
        lookups = self._online_lookups()
        queue = recovery.queue_of(job.config)
        saved = job.config.get("analysis_result") or {}
        # What the job left out before a restart stays in its notes, so they carry on rather than start again.
        left_out = {"skipped": int(saved.get("skipped") or 0), "skipped_items": list(saved.get("skipped_items") or [])}
        finished = False

        def settle(index, media_id, outcome):
            """Episode ``index`` is dealt with, however it ended: the job's saved place moves past it."""
            self._queue_set(job_id, lambda current: recovery.settle(current, index, media_id, outcome))

        def note_skipped(media_id, bindings, reason):
            """Name an episode this job did not analyze, and why, in the job's notes."""
            meta = bindings[0][2] if bindings else {}
            info = {
                "grandparentTitle": meta.get("show_title"),
                "title": meta.get("title"),
                "parentIndex": meta.get("season"),
                "index": meta.get("episode"),
                "ratingKey": media_id,
            }
            left_out["skipped"] += 1
            if len(left_out["skipped_items"]) < _SKIPPED_ITEMS_KEPT:
                left_out["skipped_items"].append({"episode": _episode_label(info), "reason": reason.rstrip(".")})
            self.job_manager.merge_job_config(
                job_id, {"analysis_result": {**left_out, "skipped_items": list(left_out["skipped_items"])}}
            )

        def leave_out(index, media_id, code, bindings):
            """One episode whose file is gone, changed or unreachable: say so on it and on the job, and go on."""
            stale = _stale(code)
            self._record_outcome(job_id, media_id, "failed", f"{stale['message']} {stale['next']}")
            note_skipped(media_id, bindings, stale["message"])
            settle(index, media_id, recovery.FILE_LEFT_OUT)
            logger.warning("Analysis left out episode {}: {}", media_id, code)

        def refuse(index, media_id, bindings, reason):
            """An episode Reprise cannot analyze (media it will not compare, a format it cannot read): the reason is
            recorded once, on the episode and on the job. It is not tried again by itself, and the others go on."""
            self._record_outcome(job_id, media_id, "failed", reason)
            note_skipped(media_id, bindings, reason)
            settle(index, media_id, recovery.REFUSED)
            logger.warning("Analysis refused episode {}: {}", media_id, reason)

        def set_aside(index, media_id, bindings):
            """An episode whose analysis was cut off as often as a job allows: it is left alone, with the reason said, so
            one episode that keeps stopping the app never holds up the rest."""
            self._record_outcome(job_id, media_id, "set_aside", recovery.SET_ASIDE_REASON)
            note_skipped(media_id, bindings, recovery.SET_ASIDE_REASON)
            self._mark_set_aside(job_id, media_id)
            settle(index, media_id, recovery.SET_ASIDE)
            logger.warning(
                "Analysis set aside episode {} after {} interrupted attempts", media_id, recovery.MAX_ATTEMPTS
            )

        def stop_here(analysis_id, media_id):
            """The app is stopping and cut this episode off. That is not a failure: its run ends as interrupted (the way
            the startup cleanup ends one), the attempt is given back, and the job stays running for the next start."""
            try:
                self.store.fail_analysis(analysis_id, "cancelled", recovery.INTERRUPTED_REASON)
            except Exception:
                logger.opt(exception=True).debug("The stopped run of episode {} had already ended", media_id)
            self._queue_set(job_id, lambda current: recovery.give_back(current, media_id))

        def finish(commit, last):
            """Commit one result atomically with the cancellation check; after the last episode the job completes: with a
            warning when episodes were left out, and as failed when every episode that ran was refused."""
            nonlocal finished
            # The queue's lock is taken before the job manager's, which ``commit_if_active`` holds while it commits.
            with recovery.LOCK:
                if not last:
                    return self.job_manager.commit_if_active(job_id, commit)

                def done():
                    commit()
                    counts = recovery.counts_of(self._queue(job_id))
                    if counts[recovery.REFUSED] and not counts[recovery.RESULT]:
                        self.job_manager.complete_job(job_id, error="Analysis did not complete")
                    else:
                        self.job_manager.complete_job(job_id, warning=_unfinished_warning(counts))

                finished = self.job_manager.commit_if_active(job_id, done)
                return finished

        # Episodes finished before a restart are not done again: the job's queue says where it stood, and the store knows
        # the one whose result was saved just before the stop.
        resumed = any(queue.get(key) for key in ("next", "attempts", "running", "recovery"))
        saved_before = self.store.finished_in_job(job_id) if resumed else frozenset()
        autopilot_job = (job.config or {}).get("source") in AUTOPILOT_SOURCES
        for index in range(min(recovery.position(queue), len(ids)), len(ids)):
            mid = ids[index]
            from ..markers.freeze import Freeze

            Freeze.of(paused).hold(cancel_check=lambda: self.job_manager.is_cancellation_requested(job_id))
            if self.job_manager.is_cancellation_requested(job_id):
                self.job_manager.cancel_job(job_id)
                return
            if is_shutting_down():
                return  # no new episode starts while the app stops; the next start carries on from here
            if mid in saved_before:
                settle(index, mid, recovery.RESULT)
                continue
            version = self.store.get_media(mid)
            bindings = all_bindings = self._bindings(mid)
            if library_request:
                bindings = [
                    b
                    for b in bindings
                    if version
                    and b[1] == library_request["server_id"]
                    and b[2].get("library_id") == library_request["library_id"]
                    and self._valid_binding(version, *b)
                ]
            if not version or not bindings or not any(self._valid_binding(version, *b) for b in bindings):
                code = (
                    self._unavailable_code(version, bindings)
                    if bindings
                    else (self._unavailable_code(version, all_bindings) or "library_changed")
                )
                leave_out(index, mid, code, all_bindings)
                continue
            if autopilot_job and self._is_current(mid, bindings):
                # Reprise's own looks at the library never redo what is already done and still counts.
                settle(index, mid, recovery.RESULT)
                continue
            if recovery.exhausted(self._queue(job_id), mid):
                set_aside(index, mid, all_bindings)
                continue
            meta = bindings[0][2]
            movie = meta.get("kind") == "movie"
            candidates = [CandidateWindow(**c) for c in meta.get("candidates", [])]
            present = {candidate.kind for candidate in candidates}
            midpoint = version.duration_ms // 2
            if not movie and "intro" not in present and midpoint > 0:
                candidates.append(CandidateWindow("intro", 0, min(FALLBACK_WINDOW_MS, midpoint), OPENING_WINDOW))
            if not movie and "credits" not in present and version.duration_ms > midpoint:
                candidates.append(
                    CandidateWindow(
                        "credits",
                        max(midpoint, version.duration_ms - FALLBACK_WINDOW_MS),
                        version.duration_ms,
                        ENDING_WINDOW,
                    )
                )
            references = []
            cursor = None
            seen_refs = set()
            other_episodes = set()
            while not movie:
                page = self.store.list_media(server_id=bindings[0][1], cursor=cursor, limit=100)
                for row in page["items"]:
                    rid = row["media_version"]["id"]
                    if rid == mid or rid in seen_refs:
                        continue
                    seen_refs.add(rid)
                    other = row["metadata"]
                    if meta.get("show_key") is not None:
                        # Only other episodes of this very season can ever be admitted: leave the rest unread.
                        if (other.get("show_key"), other.get("season")) != (meta["show_key"], meta.get("season")):
                            continue
                        if other.get("episode") != meta.get("episode"):
                            other_episodes.add(other.get("episode"))
                    ref = self.store.get_media(rid)
                    ref_bindings = self._bindings(rid)
                    if ref and ref_bindings and all(self._valid_binding(ref, *b) for b in ref_bindings):
                        references.append(ref)
                cursor = page.get("next_cursor")
                if not cursor:
                    break
            # Every other episode of the season that may serve, nearest first: the analysis uses the first few it can
            # compare exactly (an episode in another picture format is passed over for the next nearest). Only those
            # nearest are asked about at the online databases, and they alone stand for the inputs in the signature.
            admitted = admit_references(version, references, self.store)
            nearest = admitted[:MAX_REFERENCES]
            references = [ref for ref, _ in admitted]
            if movie:
                no_comparison_note = MOVIE_NOTE
            else:
                no_comparison_note = OTHER_EPISODES_NOT_READY if other_episodes else NO_OTHER_EPISODES
            owner_keeps = self.store.owner_keep_ranges(mid)
            online_windows = self._online_windows(lookups, version, bindings, [ref for ref, _ in nearest], job)
            signature = hashlib.sha256(
                (
                    version.stream_signature
                    + json.dumps([asdict(c) for c in candidates], sort_keys=True)
                    + PROFILE_FINGERPRINT
                    + json.dumps([(ref.id, snapshot) for ref, snapshot in nearest])
                    + json.dumps(owner_keeps)
                    + json.dumps({k: [asdict(w) for w in v] for k, v in sorted(online_windows.items())})
                ).encode()
            ).hexdigest()
            with recovery.LOCK:
                if is_shutting_down():
                    return  # the stop came while this episode was prepared: nothing was started, nothing to undo
                self._queue_set(job_id, lambda current, mid=mid: recovery.begin(current, mid))
                aid = self.store.begin_analysis(mid, job_id, signature)
            recorded_failure = False
            try:
                draft = analyze(
                    version,
                    candidates,
                    references,
                    store=self.store,
                    cancel_check=lambda: self.job_manager.is_cancellation_requested(job_id),
                    pause_check=paused,
                    progress_callback=lambda percent, message, index=index: self.job_manager.update_progress(
                        job_id, percent=(index * 100 + percent) / len(ids), current_item=message
                    ),
                    ffmpeg_threads=1,
                    ffmpeg_path=ffmpeg_path,
                    owner_keeps=owner_keeps,
                    online=online_windows,
                    movie=movie,
                )
                if is_shutting_down():
                    # The stop killed ffmpeg along with the app, so this draft may be cut short, whatever its status: it
                    # is not saved, and the episode is done again at the next start.
                    stop_here(aid, mid)
                    return
                if draft.status != "complete":
                    why = draft.reasons or (
                        {
                            "failed": "Media analysis failed",
                            "unsupported": "Media is unsupported",
                            "cancelled": "Analysis cancelled",
                        }.get(draft.status, "Analysis did not complete"),
                    )
                    self.store.fail_analysis(aid, draft.status, why)
                    recorded_failure = True
                    if draft.status == "cancelled":
                        self.job_manager.cancel_job(job_id)
                        return
                    # A file that went missing or changed while it was read is this episode's problem, not the job's;
                    # so is media Reprise will not compare. Either is recorded once, and the others go on.
                    code = self._unavailable_code(version, bindings)
                    if code is None:
                        refuse(index, mid, bindings, why[0])
                    else:
                        leave_out(index, mid, code, bindings)
                    continue
                elif not any(self._valid_binding(version, *b) for b in bindings):
                    raise ValueError("Media changed during analysis")
                else:
                    reasons = [no_comparison_note if reason == NO_REFERENCES else reason for reason in draft.reasons]

                    def commit(aid=aid, draft=draft, version=version, reasons=reasons, index=index):
                        self.store.finish_analysis(aid, draft.evidence, draft.ranges, version.id, reasons)
                        self._record_outcome(job_id, version.id, *_analysis_summary(draft.ranges, reasons))
                        settle(index, version.id, recovery.RESULT)

                    committed = finish(commit, index == len(ids) - 1)
                    if not committed:
                        self.store.fail_analysis(aid, "cancelled", "Analysis cancelled")
                        recorded_failure = True
                        self.job_manager.cancel_job(job_id)
                        return
            except Exception:
                if is_shutting_down():
                    stop_here(aid, mid)
                    return
                if not recorded_failure:
                    self.store.fail_analysis(aid, "failed", "Analysis failed or media changed")
                    self._record_outcome(job_id, mid, "failed", "Analysis failed or media changed")
                # A file that went missing or changed while it was read is this episode's problem, not the job's.
                code = self._unavailable_code(version, bindings)
                if code is None:
                    raise
                leave_out(index, mid, code, bindings)
                continue
            finally:
                self.store.release_analysis(aid)
        if not finished and not finish(lambda: None, True):
            self.job_manager.cancel_job(job_id)
