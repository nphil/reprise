"""Library discovery, exact playback resolution, and queued Reprise analysis."""

from __future__ import annotations

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
from urllib.parse import urlsplit

from ..gpu.ffmpeg_capabilities import ffmpeg_binary
from ..servers.ownership import apply_path_mappings
from .admission import admit_references, pair_provenance
from .identity import is_current, media_version
from .models import CandidateWindow, PlaybackIdentity
from .policy import eligible_ranges
from .verification_profile import PROFILE_FINGERPRINT

_SERVICE = None


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
    command = [paired_ffprobe(ffmpeg_path or ffmpeg_binary()), "-v", "error", "-show_streams",
               "-show_format", "-of", "json", path]
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


class RepriseService:
    def __init__(self, store, registry, job_manager):
        self.store, self.registry, self.job_manager = store, registry, job_manager
        self.publisher = None
        self._enqueue_lock = threading.Lock()
        self._refresh_locks = {}

    def set_writes_enabled(self, enabled):
        if type(enabled) is not bool or self.publisher is None:
            raise ValueError("Publication is unavailable")
        if enabled:
            cursor = None
            valid = False
            while True:
                page = self.store.list_media(cursor=cursor, limit=100)
                for row in page["items"]:
                    identity = PlaybackIdentity(**row["identity"])
                    try:
                        self.publisher.validate_target(row["media_version"]["id"], identity)
                        valid = True
                        break
                    except Exception:
                        continue
                if valid or not page.get("next_cursor"):
                    break
                cursor = page["next_cursor"]
            if not valid:
                raise ValueError("No configured Plex database passed publication safety checks")
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

    def _local_path(self, cfg, library, remote):
        roots = [
            os.path.realpath(p)
            for root in library.remote_paths
            for p in apply_path_mappings(root, cfg.path_mappings)
            if os.path.isabs(p)
        ]
        paths = {
            os.path.realpath(p)
            for p in apply_path_mappings(remote, cfg.path_mappings)
            if os.path.isabs(p) and os.path.isfile(p)
        }
        paths = {p for p in paths if any(_inside(p, root) for root in roots)}
        if len(paths) != 1:
            raise ValueError("File path is missing, ambiguous, or outside the enabled library")
        return paths.pop()

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
        machine = str(server._plex_http("get", "/").json()["MediaContainer"]["machineIdentifier"])
        if cfg.server_identity and cfg.server_identity != machine:
            raise ValueError("Plex server identity changed")
        if not str(library.id).isdecimal():
            raise ValueError("Invalid Plex library id")
        rows, offset, expected_total = [], 0, None
        while True:
            checkpoint()
            container = server._plex_http(
                "get",
                f"/library/sections/{library.id}/all",
                params={"type": "4", "X-Plex-Container-Start": str(offset), "X-Plex-Container-Size": "200"},
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
        registered = skipped = 0
        seen = []
        total = len(rows)
        if progress_callback:
            progress_callback(0, total)
        for index, summary in enumerate(rows):
            checkpoint()
            if not config_current():
                raise ValueError("Library configuration changed during refresh")
            rating_key = str(summary.get("ratingKey", ""))
            if not rating_key.isdecimal():
                skipped += 1
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
            if not media_rows and requested is None:
                skipped += 1
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
                    skipped += 1
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
                    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                        version.device,
                        version.inode,
                        version.size_bytes,
                        version.mtime_ns,
                        version.ctime_ns,
                    ):
                        raise ValueError("Media changed during discovery")
                    part_key = urlsplit(str(part["key"])).path
                    if not part_key.startswith("/library/parts/"):
                        raise ValueError("Invalid Plex part key")
                    candidates = [
                        {
                            "kind": marker["type"],
                            "start_ms": int(marker["startTimeOffset"]),
                            "end_ms": int(marker["endTimeOffset"]),
                            "source": "plex",
                        }
                        for marker in item.get("Marker", [])
                        if marker.get("type") in ("intro", "credits")
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
                    registered += 1
                except (KeyError, TypeError, ValueError, OSError, subprocess.SubprocessError, json.JSONDecodeError):
                    skipped += 1
            if progress_callback:
                progress_callback(index + 1, total)
        checkpoint()
        if not config_current():
            raise ValueError("Library configuration changed during refresh")
        if requested is not None:
            skipped = max(skipped, len(requested - matched))
        return {"registered": registered, "skipped": skipped, "seen": seen, "config_signature": config_signature}

    def _bindings(self, version_id):
        return self.store.get_bindings(version_id)

    def _valid_binding(self, version, identity, server_id, metadata):
        # Resolution is deliberately read-only and never contacts Plex or decodes media.
        try:
            cfg = self.registry.get_config(server_id)
            if not cfg or not cfg.enabled or getattr(cfg.type, "value", cfg.type) != "plex":
                return False
            if cfg.server_identity and cfg.server_identity != identity.plex_machine_identifier:
                return False
            if not cfg.server_identity and metadata.get("server_url") != cfg.url:
                return False
            library = next(
                (lib for lib in cfg.libraries if lib.enabled and str(lib.id) == str(metadata["library_id"])), None
            )
            if not library or not is_current(version):
                return False
            if metadata.get("mapping_fingerprint") != _mapping_fingerprint(cfg, library):
                return False
            remote_path = metadata.get("remote_part_path")
            if not isinstance(remote_path, str) or not remote_path:
                return False
            return (
                identity.duration_ms == version.duration_ms
                and identity.part_size_bytes == version.size_bytes
                and self._local_path(cfg, library, remote_path) == version.canonical_path
            )
        except (KeyError, TypeError, ValueError, OSError):
            return False

    def _references_current(self, result):
        for version_id in {result.media_version_id} | {e.reference_version_id for e in result.evidence if e.reference_version_id}:
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
            snapshot = (pair_provenance(target, target_bindings, reference, self._bindings(reference.id))
                        if reference else None)
            if not snapshot or evidence.metrics.get("admission_provenance") != snapshot:
                return False
        return True

    def lookup(self, identity):
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
            return answer
        bindings = [b for b in self._bindings(version.id) if b[0] == identity]
        if len(bindings) != 1:
            answer.update(resolution="ambiguous", reasons=["Playback identity is ambiguous"])
            return answer
        answer.update(resolution="resolved", media_version_id=version.id)
        if not self._valid_binding(version, *bindings[0]):
            answer.update(freshness="stale", reasons=["Plex mapping or local media has changed or is unavailable"])
            return answer
        result = self.store.get_result(version.id)
        if result is None:
            answer["freshness"] = "current"
            return answer
        if not self._references_current(result):
            result = replace(
                result, freshness="stale", reasons=(*result.reasons, "Reference media is stale or unavailable")
            )
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
        return answer

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
        rows, pos = [], None
        while True:
            page = self.store.list_media(server_id=server_id, cursor=pos, limit=100)
            rows.extend(page["items"])
            pos = page.get("next_cursor")
            if not pos:
                break
        items = []
        seen = set()
        shows = sorted({str(row["metadata"].get("show_title")) for row in rows if row["metadata"].get("show_title")})
        seasons = sorted(
            {
                row["metadata"].get("season")
                for row in rows
                if row["metadata"].get("season") is not None
                and (not show or str(show).lower() in str(row["metadata"].get("show_title", "")).lower())
            }
        )
        for row in rows:
            version, meta = row["media_version"], row["metadata"]
            if version["id"] in seen:
                continue
            seen.add(version["id"])
            if show and str(show).lower() not in str(meta.get("show_title", "")).lower():
                continue
            if season is not None and str(meta.get("season")) != str(season):
                continue
            if (
                search
                and str(search).lower()
                not in (str(meta.get("title", "")) + " " + str(meta.get("show_title", ""))).lower()
            ):
                continue
            bindings = self._bindings(version["id"])
            current = self.lookup(bindings[0][0]) if bindings else None
            items.append(
                {
                    "id": version["id"],
                    "server_id": row["server_id"],
                    "title": meta.get("title", ""),
                    "show_title": meta.get("show_title", ""),
                    "season": meta.get("season"),
                    "episode": meta.get("episode"),
                    "duration_ms": version["duration_ms"],
                    "analysis_status": current["analysis_status"] if current else "missing",
                    "freshness": current["freshness"] if current else "unavailable",
                    "identities": [asdict(b[0]) for b in bindings],
                }
            )
        start = int(cursor or 0)
        limit = min(max(int(limit), 1), 100)
        return {
            "items": items[start : start + limit],
            "next_cursor": str(start + limit) if start + limit < len(items) else None,
            "shows": shows,
            "seasons": seasons,
        }

    def get_result(self, media_version_id):
        bindings = self._bindings(media_version_id)
        if not bindings:
            raise KeyError(media_version_id)
        result = self.lookup(bindings[0][0])
        result["identities"] = [asdict(b[0]) for b in bindings]
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
                or (info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                != (version.size_bytes, version.mtime_ns, version.ctime_ns)
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
        source = RECENTLY_ADDED_JOB_SOURCE if trigger == "recently_added" else "schedule"
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
                library_name="Reprise analysis",
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
            if not version or not any(self._valid_binding(version, *b) for b in self._bindings(mid)):
                raise ValueError("Media is unavailable or stale")
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
            "error": "Job failed" if job.error else None,
            "pause_requested": self.job_manager.is_pause_requested(job.id),
        }
        config = job.config or {}
        data["operation"] = (
            "refresh" if config.get("refresh_request") else "preview" if config.get("preview_request") else "analysis"
        )
        if config.get("refresh_result"):
            data["refresh"] = config["refresh_result"]
        artifact = (job.config or {}).get("preview_artifact")
        if job.status.value == "completed" and artifact:
            data["preview"] = {
                "url": "/api/reprise/previews/" + job.id + "/stream",
                "origin_ms": artifact["origin_ms"],
                "duration_ms": artifact["duration_ms"],
                "mime_type": artifact["mime_type"],
            }
        return data

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

            _start_job_async(job_id)
        return self.get_job(job_id)

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
            if scanned["skipped"]:

                def finish_partial():
                    self.job_manager.merge_job_config(job_id, {"refresh_result": summary})
                    self.job_manager.complete_job(
                        job_id, error="Library refresh incomplete; existing entries were retained"
                    )

                if not self.job_manager.commit_if_active(job_id, finish_partial):
                    raise Cancelled("Library refresh cancelled")
                return

            def finish():
                if self._refresh_signature(server_id, library_id) != scanned["config_signature"]:
                    raise ValueError("Library configuration changed during refresh")
                if request.get("candidate_paths") is None:
                    summary["removed"] = self.store.reconcile_library(server_id, library_id, scanned["seen"])
                self.job_manager.merge_job_config(job_id, {"refresh_result": summary})

            if not self.job_manager.commit_if_active(job_id, finish, complete=not for_analysis):
                raise Cancelled("Library refresh cancelled")
            return sorted(registered_ids)
        except Cancelled:
            self.job_manager.cancel_job(job_id)
        finally:
            if acquired:
                lock.release()

    def run_job(self, job_id):
        from .analysis import analyze

        job = self.job_manager.get_job(job_id)
        from ..web.settings_manager import get_settings_manager

        ffmpeg_path = ffmpeg_binary()

        def paused():
            return self.job_manager.is_pause_requested(job_id) or get_settings_manager().processing_paused

        if job.config.get("refresh_request"):
            self._run_refresh(job_id, job.config["refresh_request"], paused, ffmpeg_path=ffmpeg_path)
            return
        library_request = job.config.get("library_analysis_request")
        if library_request:
            ids = self._run_refresh(job_id, library_request, paused, for_analysis=True, ffmpeg_path=ffmpeg_path)
            if ids is None:
                return
            if not ids:
                if not self.job_manager.commit_if_active(job_id, lambda: None, complete=True):
                    self.job_manager.cancel_job(job_id)
                return
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
        for index, mid in enumerate(ids):
            from ..markers.freeze import Freeze

            Freeze.of(paused).hold(cancel_check=lambda: self.job_manager.is_cancellation_requested(job_id))
            if self.job_manager.is_cancellation_requested(job_id):
                self.job_manager.cancel_job(job_id)
                return
            version = self.store.get_media(mid)
            bindings = self._bindings(mid)
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
                raise ValueError("Media became unavailable or stale")
            meta = bindings[0][2]
            candidates = [CandidateWindow(**c) for c in meta.get("candidates", [])]
            present = {candidate.kind for candidate in candidates}
            midpoint = version.duration_ms // 2
            if "intro" not in present and midpoint > 0:
                candidates.append(CandidateWindow("intro", 0, min(90000, midpoint), "opening-window"))
            if "credits" not in present and version.duration_ms > midpoint:
                candidates.append(
                    CandidateWindow(
                        "credits", max(midpoint, version.duration_ms - 90000), version.duration_ms, "ending-window"
                    )
                )
            references = []
            cursor = None
            seen_refs = set()
            while True:
                page = self.store.list_media(server_id=bindings[0][1], cursor=cursor, limit=100)
                for row in page["items"]:
                    rid = row["media_version"]["id"]
                    if rid == mid or rid in seen_refs:
                        continue
                    seen_refs.add(rid)
                    ref = self.store.get_media(rid)
                    ref_bindings = self._bindings(rid)
                    if ref and ref_bindings and all(self._valid_binding(ref, *b) for b in ref_bindings):
                        references.append(ref)
                cursor = page.get("next_cursor")
                if not cursor:
                    break
            admitted = admit_references(version, references, self.store)
            references = [ref for ref, _ in admitted]
            signature = hashlib.sha256(
                (
                    version.stream_signature
                    + json.dumps([asdict(c) for c in candidates], sort_keys=True)
                    + PROFILE_FINGERPRINT
                    + json.dumps([(ref.id, snapshot) for ref, snapshot in admitted])
                ).encode()
            ).hexdigest()
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
                )
                if draft.status != "complete":
                    self.store.fail_analysis(
                        aid,
                        draft.status,
                        {
                            "failed": "Media analysis failed",
                            "unsupported": "Media is unsupported",
                            "cancelled": "Analysis cancelled",
                        }.get(draft.status, "Analysis did not complete"),
                    )
                    recorded_failure = True
                    if draft.status == "cancelled":
                        self.job_manager.cancel_job(job_id)
                        return
                    raise RuntimeError("Analysis did not complete")
                elif not any(self._valid_binding(version, *b) for b in bindings):
                    raise ValueError("Media changed during analysis")
                else:
                    committed = self.job_manager.commit_if_active(
                        job_id,
                        lambda aid=aid, draft=draft, version=version: self.store.finish_analysis(
                            aid, draft.evidence, draft.ranges, version.id
                        ),
                        complete=index == len(ids) - 1,
                    )
                    if not committed:
                        self.store.fail_analysis(aid, "cancelled", "Analysis cancelled")
                        recorded_failure = True
                        self.job_manager.cancel_job(job_id)
                        return
            except Exception:
                if not recorded_failure:
                    self.store.fail_analysis(aid, "failed", "Analysis failed or media changed")
                raise
