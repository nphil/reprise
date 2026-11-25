"""Pausing Reprise's work while someone watches Plex, and carrying on afterwards.

Once a minute (the autopilot's pass) Plex's own list of what is being played (``/status/sessions``) is read. While
anyone is watching, Reprise adds one more owner to the app's normal global pause: the same hold that quiet hours and the
owner's "Pause all" use, with its own name (``plex_playing``). That reuses everything that already keeps a pause safe:
no new episode is started, work in progress waits where it is and carries on from the same place, a change to Plex that
is being written finishes first, and nothing is lost. Playback only ever adds or removes its own hold, so a pause the
owner made by hand is never undone by it. The hold is lifted ``resume delay`` after Plex goes quiet.

If Plex can't be answered for a few minutes it is treated as not playing (and the status says so), so a Plex that is
down never keeps Reprise paused.
"""

from __future__ import annotations

import threading
import time

from loguru import logger

SETTING_ENABLED = "reprise_pause_while_watching"
SETTING_MODE = "reprise_pause_mode"
SETTING_RESUME_MINUTES = "reprise_pause_resume_minutes"
HOLD = "plex_playing"  # this feature's name in the app's list of who holds processing
POLL_MODES = ("any", "transcodes")
DEFAULT_MODE = "any"
DEFAULT_RESUME_MINUTES = 2
MAX_RESUME_MINUTES = 120
UNREACHABLE_S = 180  # how long Plex may stay unanswered before it counts as "not playing"
PAUSED_IGNORED_S = 15 * 60  # a stream someone left paused for this long no longer counts
NOT_VIDEO = ("track", "photo")  # music and pictures never hold Reprise back


def _minutes(count):
    return "less than a minute" if count < 1 else "about a minute" if count < 2 else f"about {count} minutes"


def streams(container, mode, since, now):
    """How many streams in Plex's ``/status/sessions`` answer count, given the owner's ``mode``.

    ``any`` counts every video being played or buffered, local or remote; ``transcodes`` only those Plex is
    converting (a stream sent as it is costs Plex almost nothing). A stream paused for longer than
    ``PAUSED_IGNORED_S`` stops counting. ``since`` is ``{stream key: when it was first seen paused}`` and is updated
    here."""
    items = (container or {}).get("MediaContainer", container or {}).get("Metadata") or []
    live, count = set(), 0
    for item in items:
        if not isinstance(item, dict) or item.get("type") in NOT_VIDEO:
            continue
        key = str((item.get("Session") or {}).get("id") or item.get("sessionKey") or id(item))
        live.add(key)
        if mode == "transcodes" and not _transcoding(item.get("TranscodeSession")):
            continue
        if str((item.get("Player") or {}).get("state", "")).lower() == "paused":
            if now - since.setdefault(key, now) >= PAUSED_IGNORED_S:
                continue
        else:
            since.pop(key, None)
        count += 1
    for key in set(since) - live:
        del since[key]
    return count


def _transcoding(session):
    """Whether Plex is converting this stream's picture or sound (a stream it only repackages is not)."""
    if not isinstance(session, dict):
        return False
    decisions = (session.get("videoDecision"), session.get("audioDecision"))
    return "transcode" in decisions or not any(decisions)


class PlaybackPause:
    """Holds and releases the ``plex_playing`` pause. Built by the autopilot; ``check`` runs once a pass."""

    def __init__(self, service, *, clock=time.time, drain=None):
        self.service, self._clock = service, clock
        self._drain = drain
        self._lock = threading.Lock()
        self._paused_since = {}  # {stream key: first seen paused}
        self._quiet_since = None  # when Plex last answered "nobody is watching"
        self._failing = {}  # {server id: when it first stopped answering}
        self._state = {"playing": 0, "unreachable": False}

    # ── the owner's choices ───────────────────────────────────────────────────────────────────────────────────────

    def enabled(self):
        raw = self.service._setting(SETTING_ENABLED, True)
        return raw if isinstance(raw, bool) else str(raw).strip().lower() in ("true", "1", "yes")

    def mode(self):
        mode = str(self.service._setting(SETTING_MODE, DEFAULT_MODE))
        return mode if mode in POLL_MODES else DEFAULT_MODE

    def resume_minutes(self):
        try:
            minutes = int(self.service._setting(SETTING_RESUME_MINUTES, DEFAULT_RESUME_MINUTES))
        except (TypeError, ValueError):
            return DEFAULT_RESUME_MINUTES
        return max(0, min(MAX_RESUME_MINUTES, minutes))

    # ── the pause itself ──────────────────────────────────────────────────────────────────────────────────────────

    def _settings(self):
        store = getattr(self.service.registry, "settings", None)
        return store if hasattr(store, "set_processing_pause_reason") else None

    def holding(self):
        """Whether Reprise is held back by playback right now."""
        store = self._settings()
        return bool(store and HOLD in store.processing_pause_reasons)

    def send_hold(self):
        """The sentence for the automatic sender while it must wait, or None."""
        return "Waiting: Reprise is paused while Plex is playing." if self.holding() else None

    def _hold(self, on):
        """Add or remove only this feature's hold. Returns whether the app's effective pause changed."""
        store = self._settings()
        if store is None or not store.set_processing_pause_reason(HOLD, on):
            return False
        effective = store.processing_paused
        try:
            self.service.job_manager.emit_processing_paused_changed(effective)
        except Exception:
            logger.debug("Could not announce the playback pause change", exc_info=True)
        if effective:
            logger.info("Reprise paused: Plex is playing")
        elif not on:
            logger.info("Reprise resumed: Plex has been quiet long enough")
            self._resume_work()
        return True

    def _resume_work(self):
        if self._drain is not None:
            self._drain()
            return
        from ..web.routes.job_runner import resume_running_and_drain_pending

        resume_running_and_drain_pending()

    # ── looking at Plex ───────────────────────────────────────────────────────────────────────────────────────────

    def _watching(self, servers, now, holding):
        """``(streams counted, whether any Plex stayed unanswered past the wait)`` over every Plex server."""
        playing, unreachable, uncertain, mode = 0, False, False, self.mode()
        for cfg, server in servers:
            try:
                container = server._plex_http("get", "/status/sessions").json()
            except Exception as exc:
                since = self._failing.setdefault(cfg.id, now)
                if now - since >= UNREACHABLE_S:
                    unreachable = True
                else:
                    uncertain = True
                logger.debug("Reprise could not ask Plex ({}) who is watching: {}", cfg.name, type(exc).__name__)
                continue
            self._failing.pop(cfg.id, None)
            playing += streams(container, mode, self._paused_since, now)
        for gone in set(self._failing) - {cfg.id for cfg, _ in servers}:
            del self._failing[gone]
        # A Plex that did not answer yet does not release a hold, but it never starts one either.
        return playing + (1 if uncertain and holding and not playing else 0), unreachable

    def check(self, now, servers):
        """One look. Adds the hold when someone is watching, lifts it once Plex has been quiet for the resume delay."""
        with self._lock:
            self._check(now, servers)

    def _check(self, now, servers):
        if self._settings() is None:
            return  # nowhere to keep a pause, so there is nothing to hold
        holding = self.holding()
        if not self.enabled():
            self._quiet_since = None
            self._state = {"playing": 0, "unreachable": False}
            if holding:
                self._hold(False)
            return
        playing, unreachable = self._watching(servers, now, holding)
        self._state = {"playing": playing, "unreachable": unreachable}
        if playing:
            self._quiet_since = None
            if not holding:
                self._hold(True)
            return
        if not holding:
            self._quiet_since = None
            return
        if self._quiet_since is None:
            self._quiet_since = now
        if now - self._quiet_since >= self.resume_minutes() * 60:
            self._quiet_since = None
            self._hold(False)

    # ── what the owner sees ───────────────────────────────────────────────────────────────────────────────────────

    def status(self, now=None):
        """``{"enabled", "paused", "message", "plex_unreachable", "resume_at"}`` for the settings page and API."""
        now = self._clock() if now is None else now
        enabled, holding = self.enabled(), self.holding()
        unreachable = enabled and self._state["unreachable"]
        minutes = self.resume_minutes()
        resume_at, message = None, None
        if enabled and holding and self._quiet_since is None:
            message = "Paused while Plex is playing. Resumes " + (
                "as soon as it stops." if minutes == 0 else f"{_minutes(minutes)} after it stops."
            )
        elif enabled and holding:
            resume_at = self._quiet_since + minutes * 60
            left = _minutes(int(max(0, resume_at - now) // 60))
            message = (
                f"Reprise can't reach Plex to see whether anyone is watching, so it is treating Plex as not playing. "
                f"Resuming in {left}."
                if unreachable
                else f"Plex has stopped playing. Resuming in {left}."
            )
        elif unreachable:
            message = (
                "Reprise can't reach Plex to see whether anyone is watching, so it is treating Plex as not playing "
                "and carrying on."
            )
        return {
            "enabled": enabled,
            "paused": enabled and holding,
            "message": message,
            "plex_unreachable": unreachable,
            "resume_at": resume_at,
        }
