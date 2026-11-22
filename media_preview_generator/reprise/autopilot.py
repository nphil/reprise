"""Reprise's own automatic work: it notices new and changed files, works through the rest of the library in the
background, sends sure matches to Plex and raises phone alerts.

One scheduled job inside the app calls :meth:`Autopilot.tick` every minute (``web/scheduler.py``). There is no extra
thread, cron entry or outside script, and nothing here needs an AI session. Each pass does, in this order:

* **Look for new files** (every 30 minutes, or soon after a Sonarr/Radarr webhook): Plex's own listing is compared with
  what Reprise has read, and only files that are new or changed are queued, at the priority of any new import. A file
  already queued, or already analysed and unchanged, is left alone. Plex is only read.
* **Backfill** (low priority, when nothing of its own is running): the rest of the library is analysed a few episodes
  at a time, most-watched shows first, specials and films last.
* **Send sure matches to Plex** (``autosend``): see that module for the exact rules.
* **Watch for problems**: Plex unreachable, a full disk, episodes set aside, a job paused, a failed Plex backup. Each
  is one alert, never one per file, with a plain "fixed" follow-up.
"""

from __future__ import annotations

import os
import shutil
import threading
import time

from loguru import logger

from ..shutdown import is_shutting_down
from . import recovery
from .alerts import Alerts
from .autosend import AutoSender, label_of
from .autostate import AutopilotState, Concerns
from .service import AUTOPILOT_SOURCES

SETTING_AUTO_SEND = "reprise_auto_send"
SETTING_BACKFILL = "reprise_backfill"
SETTING_ALERTS = "reprise_alerts_enabled"
SETTINGS = {"auto_send": SETTING_AUTO_SEND, "backfill": SETTING_BACKFILL, "alerts": SETTING_ALERTS}

RECONCILE_EVERY_S = 30 * 60  # the safety net that finds what a webhook missed
HEALTH_EVERY_S = 5 * 60
NUDGE_WAIT_S = 60  # a webhook is followed by a look at Plex after this long (Plex needs a moment to scan the file)
NUDGE_RETRY_S = 120
NUDGE_TRIES = 6
NUDGE_LIMIT = 500
NUDGE_DELAY_CAP_S = 300
FILES_PER_JOB = 10  # new files are queued in small jobs so one bad file costs little
LARGE_BATCH = 50  # more new files than this at once is a backlog, not an import
BACKLOG_FILES_PER_JOB = 50
BACKLOG_JOBS_PER_PASS = 4
BACKLOG_JOBS_ACTIVE = 8  # no more backlog jobs while this many of Reprise's own are waiting
SUSPECT_JOBS_PER_PASS = 5
PART_BACKOFF_S = (30 * 60, 2 * 3600, 8 * 3600)  # the wait after a file was queued once, twice, three times
PART_TRIES = len(PART_BACKOFF_S) + 1
BACKFILL_JOB_SIZE = 5
BACKFILL_BACKOFF_S = (3600, 6 * 3600)
BACKFILL_TRIES = len(BACKFILL_BACKOFF_S) + 1
PLEX_DOWN_ALERT_S = 3600
LOW_DISK_BYTES = 2 * 1024**3
DISK_OK_BYTES = 3 * 1024**3
ASIDE_QUIET_S = 120  # a job's set-aside episodes are reported once nothing more was set aside for this long
KEEP_TRIES_S = 30 * 86400
FINAL_STATES = ("completed", "failed", "cancelled")
LIBRARY_KINDS = ("movie", "episode", "show", None)  # music and photo libraries are never read


def _flag(raw):
    return raw if isinstance(raw, bool) else str(raw).strip().lower() in ("true", "1", "yes")


def _iso(seconds):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))


def _count(value):
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _why(exc):
    """What went wrong, for the log: an error's words only when Reprise wrote them (a network error can carry an
    address), otherwise just its kind."""
    return str(exc) if isinstance(exc, ValueError) else type(exc).__name__


def _same_size(path, size):
    try:
        return os.stat(path).st_size == size
    except OSError:
        return False


def backfill_order(rows, plays):
    """The unanalysed files in the order the backfill should take them.

    Most-watched shows first (then the most recently watched, then A to Z), a show's episodes in season and episode
    order; then films; then specials (season 0). ``rows`` are ``store.unanalyzed()`` entries and ``plays`` is
    ``{(server id, library id, show key): (plays, last viewed)}``."""

    def rank(row):
        meta = row["metadata"]
        title = str(meta.get("show_title") or meta.get("title") or "").lower()
        if meta.get("kind") == "movie":
            return (1, 0, 0, title, "", False, 0, False, 0)
        season, episode = meta.get("season"), meta.get("episode")
        season, episode = (season if isinstance(season, int) else None), (episode if isinstance(episode, int) else None)
        show = str(meta.get("show_key"))
        played, viewed = plays.get((row["server_id"], str(meta.get("library_id")), show), (0, 0))
        return (
            2 if season == 0 else 0,
            -played,
            -viewed,
            title,
            show,
            season is None,
            season or 0,
            episode is None,
            episode or 0,
        )

    return sorted(rows, key=rank)


class Autopilot:
    """The automatic work of one Reprise service. Built once by the app (``build``) and ticked by the scheduler."""

    def __init__(self, service, alerts, *, config_dir, clock=time.time, backup=None):
        self.service, self.alerts, self.config_dir = service, alerts, config_dir
        self._clock = clock
        self.state = AutopilotState(service.store)
        self.concerns = Concerns(alerts, self.state)
        self.sender = AutoSender(
            service,
            self.concerns,
            self.state,
            config_dir=config_dir,
            enabled=lambda: self._on(SETTING_AUTO_SEND),
            clock=clock,
            backup=backup,
        )
        self._nudges = {}
        self._nudge_lock = threading.Lock()
        self._note_lock = threading.Lock()
        self._tick_lock = threading.Lock()  # one pass at a time
        self._first_pass = True

    # ── what the owner sees and changes ───────────────────────────────────────────────────────────────────────────

    def _on(self, setting):
        return _flag(self.service._setting(setting, True))

    def settings(self):
        """The three switches (all on until the owner turns one off): ``{"auto_send", "backfill", "alerts"}``."""
        return {name: self._on(setting) for name, setting in SETTINGS.items()}

    def update_settings(self, values):
        """Change some of the switches; ``values`` maps a switch name to True or False. Anything else is refused."""
        if (
            not isinstance(values, dict)
            or not values
            or set(values) - set(SETTINGS)
            or any(type(value) is not bool for value in values.values())
        ):
            raise ValueError("Choose on or off for each of: automatic sending, backfill, alerts.")
        store = getattr(self.service.registry, "settings", None)
        if store is None:
            raise ValueError("Reprise can't save settings right now.")
        for name, value in values.items():
            store.set(SETTINGS[name], value)
        return self.settings()

    def status(self):
        """The small status line of the Settings page: files waiting, the last look at Plex, the last alert."""
        last = self.state.get("last_reconcile")
        alert = self.alerts.last()
        blocked = self.sender.blocked(self._clock()) if self._on(SETTING_AUTO_SEND) else None
        return {
            "queue_length": self._queue_length(),
            "last_reconcile": _iso(last) if last else None,
            "last_alert": {key: alert[key] for key in ("kind", "title", "at")} if alert else None,
            "auto_send_blocked": blocked,
        }

    def send_test_alert(self):
        """Send one test alert to the phone (also when alerts are switched off) and say whether it arrived."""
        return self.alerts.test()

    def _active_jobs(self, *, own_only=True):
        """The reprise jobs that are waiting or running (only Reprise's own automatic ones by default)."""
        found = []
        for job in self.service.job_manager.get_all_jobs():
            if job.kind != "reprise" or job.status.value not in ("pending", "running"):
                continue
            if not own_only or (job.config or {}).get("source") in AUTOPILOT_SOURCES:
                found.append(job)
        return found

    def _queue_length(self):
        """How many files wait in Reprise's own automatic jobs, counting those a job has not even listed yet."""
        total = 0
        for job in self._active_jobs():
            config = job.config or {}
            if recovery.analyzing(config):
                total += len(recovery.todo(config))
            else:
                total += len((config.get("library_analysis_request") or {}).get("candidate_paths") or [])
        return total

    # ── the pass ──────────────────────────────────────────────────────────────────────────────────────────────────

    def tick(self, now=None):
        """One pass, called by the scheduler every minute. A step that fails is logged and does not stop the others."""
        if is_shutting_down() or not self._tick_lock.acquire(blocking=False):
            return
        try:
            now = self._clock() if now is None else now
            self._step("looking for new files", self._reconcile_if_due, now)
            self._step("planning the backfill", self._backfill_if_due, now)
            self._step("sending sure matches to Plex", self.sender.sweep, now)
            self._step("checking for problems", self._watch, now)
        finally:
            self._tick_lock.release()

    @staticmethod
    def _step(what, action, now):
        try:
            action(now)
        except Exception:
            logger.exception("Reprise's automatic work failed while {}. It will try again on the next pass.", what)

    # ── new files ─────────────────────────────────────────────────────────────────────────────────────────────────

    def nudge(self, path, delay=0):
        """A webhook says ``path`` just arrived. Plex is looked at soon, and again every couple of minutes until it
        lists the file; the 30-minute look at everything covers what this misses."""
        if not isinstance(path, str) or not path:
            return
        try:
            wait = min(max(0.0, float(delay or 0)), NUDGE_DELAY_CAP_S)
        except (TypeError, ValueError):
            wait = 0.0
        with self._nudge_lock:
            if path in self._nudges or len(self._nudges) < NUDGE_LIMIT:
                self._nudges[path] = {"due": self._clock() + wait + NUDGE_WAIT_S, "tries": 0}

    def _nudge_due(self, now):
        with self._nudge_lock:
            return any(nudge["due"] <= now for nudge in self._nudges.values())

    def _settle_nudges(self, names, now):
        """After a look at Plex: a nudge whose file Plex lists is done; the others try again a little later."""
        with self._nudge_lock:
            for path, nudge in list(self._nudges.items()):
                if nudge["due"] > now:
                    continue
                if os.path.basename(path) in names or nudge["tries"] + 1 >= NUDGE_TRIES:
                    del self._nudges[path]
                else:
                    nudge["tries"] += 1
                    nudge["due"] = now + NUDGE_RETRY_S

    def _reconcile_if_due(self, now):
        started = self.state.get("reconcile_started", 0)
        if self._first_pass or now - started >= RECONCILE_EVERY_S or self._nudge_due(now):
            self._first_pass = False
            self.reconcile(now)

    def _plex_servers(self):
        found = []
        for cfg in self.service.registry.configs():
            if cfg.enabled and getattr(cfg.type, "value", cfg.type) == "plex":
                server = self.service.registry.get(cfg.id)
                if server is not None:
                    found.append((cfg, server))
        return found

    def reconcile(self, now=None):
        """Compare Plex's listing of every enabled library with what Reprise has read and queue what is new or
        changed. Returns how many files were queued. Plex is only read."""
        now = self._clock() if now is None else now
        self.state.put("reconcile_started", now)
        names, queued, looked = set(), 0, 0
        tries = self.state.all_tries("part")
        for cfg, server in self._plex_servers():
            try:
                queued += self._reconcile_server(cfg, server, now, names, tries)
                looked += 1
                self._source_ok(cfg)
            except Exception as exc:
                self._source_failed(cfg, exc, now)
        if looked:
            self.state.put("last_reconcile", now)
        self._settle_nudges(names, now)
        if queued:
            logger.info("Reprise found {} new or changed file(s) in Plex and queued them", queued)
        return queued

    def _reconcile_server(self, cfg, server, now, names, tries):
        libraries = [lib for lib in server.list_libraries() if lib.enabled and lib.kind in LIBRARY_KINDS]
        if not libraries and any(lib.enabled for lib in cfg.libraries):
            # Plex answers a failed request with an empty list: that is no news about the library.
            raise ValueError("Plex did not list its libraries")
        return sum(self._reconcile_library(cfg, library, now, names, tries) for library in libraries)

    def _reconcile_library(self, cfg, library, now, names, tries):
        service = self.service
        _, _, _, rows = service.list_library(cfg.id, library.id)
        self._remember_plays(cfg.id, library, rows)
        known = service.store.bound_parts(cfg.id, str(library.id))
        waiting = service.queued_candidate_paths()
        wanted = []
        for row in rows:
            rating_key = str(row.get("ratingKey", ""))
            for media in row.get("Media") or []:
                parts = media.get("Part") or []
                names.update(os.path.basename(str(part.get("file") or "")) for part in parts)
                if len(parts) != 1:
                    continue  # a file in several parts is never read
                part = parts[0]
                remote = part.get("file")
                size, duration = _count(part.get("size")), _count(media.get("duration") or row.get("duration"))
                if known.get((rating_key, str(media.get("id")), str(part.get("id")))) == (size, duration):
                    continue
                path = service.local_file(cfg, library, remote) if isinstance(remote, str) and remote else None
                if path is None or path in waiting or not _same_size(path, size):
                    continue  # not here yet, queued already, or still being copied
                key = f"{cfg.id}:{rating_key}:{media.get('id')}:{part.get('id')}:{size}"
                count, at = tries.get(key, (0, 0))
                if count >= PART_TRIES or (count and now < at + PART_BACKOFF_S[min(count, len(PART_BACKOFF_S)) - 1]):
                    continue
                wanted.append((path, key, count > 0))
        return self._queue_files(cfg, library, wanted, now)

    def _remember_plays(self, server_id, library, rows):
        """Keep how much each show was watched (Plex's own counts), which orders the backfill."""
        shows = {}
        for row in rows:
            key = str(row.get("grandparentRatingKey") or "")
            if not key:
                continue
            title, plays, viewed = shows.get(key, (str(row.get("grandparentTitle") or ""), 0, 0))
            shows[key] = (title, plays + _count(row.get("viewCount")), max(viewed, _count(row.get("lastViewedAt"))))
        if shows:
            self.state.set_plays(server_id, str(library.id), shows)

    def _queue_files(self, cfg, library, wanted, now):
        """Queue the new or changed files ``(path, key, tried before)``: a few new ones per job, at the priority of any
        new import; a big backlog at low priority, a few jobs per pass; a file that failed before, alone and low."""
        if not wanted:
            return 0
        from ..web.jobs import PRIORITY_HIGH, PRIORITY_LOW, parse_priority

        incoming = parse_priority(self.service._setting("incoming_job_priority", PRIORITY_HIGH))
        fresh = [item for item in wanted if not item[2]]
        suspects = [item for item in wanted if item[2]]
        backlog = len(wanted) > LARGE_BATCH
        size = BACKLOG_FILES_PER_JOB if backlog else FILES_PER_JOB
        jobs = [
            (fresh[start : start + size], PRIORITY_LOW if backlog else incoming) for start in range(0, len(fresh), size)
        ]
        if backlog:
            room = max(0, BACKLOG_JOBS_ACTIVE - len(self._active_jobs()))
            jobs = jobs[: min(BACKLOG_JOBS_PER_PASS, room)]
        jobs += [([item], PRIORITY_LOW) for item in suspects[:SUSPECT_JOBS_PER_PASS]]
        queued = 0
        for items, priority in jobs:
            try:
                self.service.enqueue_library_analysis(
                    cfg.id,
                    library.id,
                    candidate_paths=[path for path, _, _ in items],
                    trigger="autopilot",
                    priority=priority,
                )
            except Exception as exc:
                logger.warning("Reprise could not queue {} new file(s) from Plex: {}", len(items), _why(exc))
                continue
            for _, key, _ in items:
                self.state.bump("part", key, now)
            queued += len(items)
        return queued

    def _source_ok(self, cfg):
        self.state.drop(f"reconcile_failed:{cfg.id}")
        self.concerns.resolve(
            f"reconcile_failing:{cfg.id}", "Reprise can read Plex's library again. Nothing more to do."
        )

    def _source_failed(self, cfg, exc, now):
        if self.state.get(f"reconcile_failed:{cfg.id}") is None:
            self.state.put(f"reconcile_failed:{cfg.id}", now)
        logger.warning("Reprise could not look at Plex ({}) for new files: {}", cfg.name, _why(exc))

    # ── the backfill ──────────────────────────────────────────────────────────────────────────────────────────────

    def _backfill_if_due(self, now):
        if self._on(SETTING_BACKFILL) and not self.service._processing_paused():
            self.plan_backfill(now)

    def _queued_versions(self):
        """The files that already wait in any reprise job, whoever queued it."""
        waiting = set()
        for job in self._active_jobs(own_only=False):
            waiting.update(recovery.todo(job.config))
        return waiting

    def plan_backfill(self, now=None):
        """Queue the next few unanalysed files, but only while none of Reprise's own jobs is waiting or running, so the
        backfill never queues ahead of itself. Returns the ids of the files queued."""
        now = self._clock() if now is None else now
        if self._active_jobs():
            return []
        tries = self.state.all_tries("version")
        waiting = self._queued_versions()
        ready = []
        for row in self.service.store.unanalyzed():
            count, at = tries.get(row["version_id"], (0, 0))
            if row["version_id"] in waiting or count >= BACKFILL_TRIES:
                continue
            if count and now < at + BACKFILL_BACKOFF_S[min(count, len(BACKFILL_BACKOFF_S)) - 1]:
                continue
            ready.append(row)
        if not ready:
            return []
        batch = backfill_order(ready, self.state.plays())[:BACKFILL_JOB_SIZE]
        ids = [row["version_id"] for row in batch]
        first = batch[0]["metadata"]
        shows = {row["metadata"].get("show_title") or row["metadata"].get("title") for row in batch}
        label = str(first.get("show_title") or first.get("title") or "library") + (
            " and more" if len(shows) > 1 else ""
        )
        self.service.enqueue_backfill(ids, label)
        for version_id in ids:
            self.state.bump("version", version_id, now)  # also those that could not be queued: not chosen again at once
        return ids

    # ── problems ──────────────────────────────────────────────────────────────────────────────────────────────────

    def note_set_aside(self, job_id, media_id):
        """A job set one episode aside after repeated failures. They are reported together, once the job is over or
        nothing more was set aside for a couple of minutes."""
        with self._note_lock:
            key = f"aside:{job_id}"
            entry = self.state.get(key) or {"ids": [], "raised": False}
            if media_id not in entry["ids"]:
                entry["ids"].append(media_id)
            entry["last"] = self._clock()
            self.state.put(key, entry)

    def note_paused(self, job_id):
        """Reprise paused a job because it kept stopping the app: the owner is told right away."""
        job = self.service.job_manager.get_job(job_id)
        name = str(getattr(job, "library_name", "") or "an analysis")
        self.concerns.raise_problem(
            f"paused:{job_id}",
            "Reprise paused an analysis",
            f'Reprise paused "{name}": it kept stopping before it could finish an episode. '
            "Open Reprise, look at the job, and press Resume when you are ready.",
        )

    def _name(self, media_id):
        bindings = self.service._bindings(media_id)
        return label_of(bindings[0][2]) if bindings else "an episode"

    def _analysed(self, media_id):
        """Whether an episode that was set aside no longer needs attention: analysed since, or gone from the library."""
        if not self.service._bindings(media_id):
            return True
        result = self.service.store.get_result(media_id)
        return bool(result and result.analysis_status == "complete")

    def _aside_text(self, ids):
        names = ", ".join(self._name(media_id) for media_id in ids[:3])
        more = f" and {len(ids) - 3} more" if len(ids) > 3 else ""
        count = "1 episode was" if len(ids) == 1 else f"{len(ids)} episodes were"
        return (
            f"{count} set aside after repeated failures: {names}{more}. "
            "Reprise will try again later. Open Reprise to see why."
        )

    def _check_jobs(self, now):
        manager = self.service.job_manager
        for key in self.concerns.open_keys("paused:"):
            job = manager.get_job(key.split(":", 1)[1])
            if (
                job is None
                or job.status.value in FINAL_STATES
                or recovery.queue_of(job.config).get("recovery") != "paused"
            ):
                self.concerns.resolve(key, "That analysis is running again or has finished. Nothing more to do.")
        for key, entry in self.state.items("aside:"):
            job_id = key.split(":", 1)[1]
            concern = f"set_aside:{job_id}"
            job = manager.get_job(job_id)
            over = job is None or job.status.value in FINAL_STATES
            if not entry.get("raised"):
                if over or now - entry["last"] >= ASIDE_QUIET_S:
                    self.concerns.raise_problem(concern, "Episodes set aside", self._aside_text(entry["ids"]))
                    self.state.put(key, {**entry, "raised": True})
            elif all(self._analysed(media_id) for media_id in entry["ids"]):
                self.concerns.resolve(concern, "The episodes that were set aside are analysed now. Nothing more to do.")
                self.state.drop(key)
            else:
                self.concerns.raise_problem(concern, "Episodes set aside", self._aside_text(entry["ids"]))

    def _check_plex(self, now):
        for cfg, server in self._plex_servers():
            down, key = f"plex_down:{cfg.id}", f"plex_unreachable:{cfg.id}"
            try:
                server._plex_http("get", "/")
            except Exception:
                since = self.state.get(down)
                if since is None:
                    self.state.put(down, now)
                    since = now
                if now - since >= PLEX_DOWN_ALERT_S:
                    self.concerns.raise_problem(
                        key,
                        "Plex can't be reached",
                        f"Reprise has not been able to reach Plex ({cfg.name}) for over an hour, so it can't look "
                        "for new files or send anything. It keeps trying.",
                    )
                continue
            if self.state.get(down) is not None:
                # It was down. A library look that failed meanwhile says nothing about the library list itself: the
                # next look decides.
                self.state.drop(down)
                self.state.drop(f"reconcile_failed:{cfg.id}")
            self.concerns.resolve(key, f"Plex ({cfg.name}) can be reached again. Nothing more to do.")
            failing = self.state.get(f"reconcile_failed:{cfg.id}")
            if failing is not None and now - failing >= PLEX_DOWN_ALERT_S:
                self.concerns.raise_problem(
                    f"reconcile_failing:{cfg.id}",
                    "Reprise can't read Plex's library",
                    f"Plex ({cfg.name}) answers, but Reprise has not been able to read its library list for over an "
                    "hour, so new files are not being found. Check the Plex settings in Reprise.",
                )

    def _check_disk(self, now):
        free = shutil.disk_usage(self.config_dir).free
        if free < LOW_DISK_BYTES:
            self.concerns.raise_problem(
                "low_disk",
                "Reprise is running low on disk space",
                f"Only {free / 1024**3:.1f} GB is free where Reprise keeps its data. "
                "Free some space soon, or Reprise will stop working.",
            )
        elif free >= DISK_OK_BYTES:
            self.concerns.resolve("low_disk", "There is enough free disk space again. Nothing more to do.")

    def _watch(self, now):
        self.concerns.refresh()
        self._step("checking jobs", self._check_jobs, now)
        if now - self.state.get("health_checked", 0) >= HEALTH_EVERY_S:
            self.state.put("health_checked", now)
            self._step("checking Plex", self._check_plex, now)
            self._step("checking the disk", self._check_disk, now)
            self.state.prune_tries(now - KEEP_TRIES_S)


def build(service, config_dir):
    """Reprise's automatic work for ``service``, with its phone alerts (the alerts switch is read at every send)."""
    alerts = Alerts(service.store, config_dir=config_dir, enabled=lambda: _flag(service._setting(SETTING_ALERTS, True)))
    return Autopilot(service, alerts, config_dir=config_dir)
