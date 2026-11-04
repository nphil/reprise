"""Online intro and credits times for the episodes Reprise compares.

IntroDB, SkipDB and (with a key) TheIntroDB say where an episode's intro and credits are. Reprise never publishes
that on its own: the times only say where to look in the other episodes (hints) and, when comparing cannot confirm
anything, are offered as suggestions the owner must approve.

The clients, pacing, daily budgets, ids, series pauses and speed rules are the Intro & Credits ones, used as they are.
Each episode is asked once: an answer is remembered (with how it was parsed), a "no entry" is asked again after
14-28 days, and a refusal (budget used up, network error, blocked slot) is not remembered, so the next analysis asks
again. Nothing here may fail an analysis: whatever goes wrong, the episode simply has no online times.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from loguru import logger

from ..markers.decide import DecisionContext, read_on_file_clock, resolve_end_ms
from ..markers.external_ids import ids_from_path, ids_from_server_dict, merge_ids
from ..markers.models import Candidate, MarkerType, MediaIds, Source
from ..markers.pipeline import (
    NO_DATA_RETRY,
    NO_DATA_RETRY_SPREAD_DAYS,
    PARSER_VERSIONS,
    SERIES_NO_ENTRY_MISSES,
    SERIES_NO_ENTRY_PAUSE,
    build_clients,
)
from ..markers.probe import _video_frame_rate
from ..markers.settings import get_global_settings
from ..markers.sources.online import LookupResult, is_budget_exhausted
from ..markers.sources.ratelimit import capped_waits
from ..markers.sources.theintrodb import series_key
from .models import CandidateWindow, OnlineWindow
from .verification_profile import PROFILE_SPEC

SETTING = "reprise_online_lookups"  # the owner's switch; on unless explicitly turned off
# The longest a lookup waits for a request slot: an analysis holds a worker, and an answer is optional.
LOOKUP_WAIT_S = 30.0
AGREE_MS = PROFILE_SPEC["online"]["agree_ms"]
# Best first: SkipDB's answer is matched to this file's duration, the others' carry no duration.
SOURCE_ORDER = (Source.SKIPDB, Source.INTRODB, Source.THEINTRODB)
LABELS = {Source.SKIPDB: "SkipDB", Source.INTRODB: "IntroDB", Source.THEINTRODB: "TheIntroDB"}
KINDS = (MarkerType.INTRO, MarkerType.CREDITS)
_SERIES_PAUSED = frozenset({Source.THEINTRODB})


def switch_on() -> bool:
    """The owner's "look up intro times online" switch; on unless explicitly turned off."""
    from ..web.settings_manager import peek_settings_manager

    manager = peek_settings_manager()
    return manager is None or manager.get(SETTING, True) is not False


def set_switch(value: bool) -> None:
    from ..web.settings_manager import get_settings_manager

    get_settings_manager().set(SETTING, bool(value))


def frame_rate_of(stream_signature: str) -> float | None:
    """The file's video frame rate from the streams the library registered it with (None when unknown)."""
    try:
        return _video_frame_rate(json.loads(stream_signature).get("streams"))
    except (ValueError, AttributeError, TypeError):
        return None


def show_ids(path: str, fetch: Callable[[], Mapping[str, Any] | None]) -> MediaIds:
    """The show's ids: from the path's ``{imdb-tt…}`` folder tags when they give the imdb id, else completed from the
    server's answer for the episode (``fetch``: ``MediaServer.get_external_ids``). Season and episode are the
    caller's, per episode."""
    from_path = ids_from_path(path)
    if from_path.is_episode and from_path.imdb:
        return from_path
    try:
        raw = fetch()
    except Exception as exc:
        logger.debug("External ids for {} failed: {}", path, type(exc).__name__)
        raw = None
    answer = ids_from_server_dict(dict(raw)) if raw else MediaIds()
    if not answer.is_episode:
        return from_path
    return merge_ids(from_path, answer) if from_path.is_episode else answer


def _encode(candidates: Sequence[Candidate]) -> str:
    return json.dumps([[c.type.value, c.start_ms, c.end_ms, c.confidence] for c in candidates], separators=(",", ":"))


def _decode(source: Source, text: str) -> list[Candidate]:
    return [
        Candidate(MarkerType(kind), start, end, source, confidence) for kind, start, end, confidence in json.loads(text)
    ]


def _lookup_key(ids: MediaIds, duration_ms: int | None) -> str:
    """What one answer is for: the show, the episode and, for the sources that match it, the file's length."""
    return json.dumps([ids.imdb, ids.tmdb, ids.tvdb, ids.season, ids.episode, duration_ms or 0], separators=(",", ":"))


def _retry_after(key: str) -> timedelta:
    """How old a stored "no entry" gets before its source is asked again: 14 to 28 days, fixed per answer."""
    spread = int(hashlib.sha256(key.encode()).hexdigest(), 16) % (NO_DATA_RETRY_SPREAD_DAYS + 1)
    return NO_DATA_RETRY + timedelta(days=spread)


def _sources_ready() -> dict[Source, Any]:
    """The clients of the databases the owner has turned on (Settings → Intro & Credits), none when the switch is off.
    TheIntroDB is asked only with a key."""
    if not switch_on():
        return {}
    settings = get_global_settings()
    clients = build_clients(settings)
    keyed = settings.source("theintrodb")
    if keyed is None or not keyed.api_key:
        clients.pop("theintrodb", None)
    return {source: clients[source.value] for source in SOURCE_ORDER if source.value in clients}


@dataclass
class Asked:
    """What one analysis asked: for the owner's job log and for tests."""

    network: int = 0  # requests sent
    reused: int = 0  # answers taken from what is remembered
    unavailable: int = 0  # asks that got no answer (refused, blocked, network error)


class OnlineLookups:
    """One analysis job's online lookups: the sources it may use, and what it has asked so far."""

    def __init__(
        self, store, clients: Mapping[Source, Any] | None = None, *, clock: Callable[[], datetime] | None = None
    ):
        self._store = store
        self._clients = dict(_sources_ready() if clients is None else clients)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._exhausted: set[Source] = set()
        self._memo: dict[tuple[Source, str], list[Candidate] | None] = {}
        self.asked = Asked()

    @property
    def sources(self) -> tuple[Source, ...]:
        return tuple(self._clients)

    def _ask(
        self, source: Source, ids: MediaIds, duration_ms: int, priority: int, cancel_check: Callable[[], bool] | None
    ) -> list[Candidate] | None:
        """One source's remembered or fresh answer for the episode: its candidates ([] for "no entry"), or None when
        it gave none."""
        # Only SkipDB and TheIntroDB are matched to the file's length; IntroDB's answer is the episode's.
        key = _lookup_key(ids, None if source is Source.INTRODB else duration_ms)
        if (source, key) in self._memo:
            return self._memo[source, key]
        answer = self._memo[source, key] = self._answer(source, key, ids, duration_ms, priority, cancel_check)
        return answer

    def _answer(
        self,
        source: Source,
        key: str,
        ids: MediaIds,
        duration_ms: int,
        priority: int,
        cancel_check: Callable[[], bool] | None,
    ) -> list[Candidate] | None:
        stored = self._store.online_answer(source.value, key)
        if stored is not None and stored["parser_version"] == PARSER_VERSIONS[source]:
            fresh = stored["status"] == "ok" or self._clock() - stored["fetched_at"] <= _retry_after(key)
            if fresh:
                self.asked.reused += 1
                return _decode(source, stored["candidates"]) if stored["status"] == "ok" else []
        if source in self._exhausted:
            return None
        marker_store = self._store.marker_store
        series = series_key(ids) if source in _SERIES_PAUSED else None
        if series is not None and marker_store.series_lookups_paused_until(source, series, pause=SERIES_NO_ENTRY_PAUSE):
            return None
        try:
            with capped_waits(LOOKUP_WAIT_S):
                result = self._clients[source].lookup(
                    ids, duration_ms=duration_ms, priority=priority, cancel_check=cancel_check
                )
        except Exception as exc:
            logger.warning("{} lookup failed: {}", LABELS[source], type(exc).__name__)
            self.asked.unavailable += 1
            return None
        if not isinstance(result, LookupResult):
            self.asked.unavailable += 1
            return None
        self.asked.network += 1
        if result.status not in ("ok", "no_data"):
            self.asked.unavailable += 1
            if is_budget_exhausted(result.detail):
                self._exhausted.add(source)
            logger.debug("{} lookup: {}", LABELS[source], result)
            return None
        candidates = list(result.candidates)
        self._store.save_online_answer(
            source.value, key, result.status, _encode(candidates), PARSER_VERSIONS[source], self._clock()
        )
        if series is not None and ids.season is not None and ids.episode is not None:
            marker_store.record_series_lookup(
                source,
                series,
                f"S{ids.season:02d}E{ids.episode:02d}",
                found=result.status == "ok",
                misses=SERIES_NO_ENTRY_MISSES,
                pause=SERIES_NO_ENTRY_PAUSE,
            )
        return candidates

    def windows(
        self,
        ids: MediaIds,
        *,
        duration_ms: int,
        frame_rate: float | None,
        plex: Sequence[CandidateWindow] = (),
        priority: int,
        cancel_check: Callable[[], bool] | None = None,
    ) -> tuple[OnlineWindow, ...]:
        """Where the databases put this episode's intro and credits, on this file's own clock; one window per kind.

        Args:
            ids: The show's ids with this episode's season and episode.
            duration_ms: This file's duration (SkipDB matches it; every answer is checked against it).
            frame_rate: Its video frame rate: IntroDB and TheIntroDB carry no duration, so their times are read on this
                file's clock the way the Intro & Credits rules read them (a 25 fps file and a film-rate one differ by
                4.3 %). Plex's marker for the file, when it has one, is the agreeing source that decides the reading.
            plex: Plex's own markers for this file.
            priority: The job's priority, for the limiters.
            cancel_check: Polled while waiting for a request slot.

        Returns:
            At most one window per kind; empty when no source gave usable times.
        """
        if not self._clients or not ids.is_episode or not duration_ms:
            return ()
        found: list[Candidate] = []
        for source in self._clients:
            answer = self._ask(source, ids, duration_ms, priority, cancel_check)
            if answer:
                found.extend(answer)
        if not found:
            return ()
        context = DecisionContext(duration_ms, False, "", frozenset(), (), frame_rate=frame_rate)
        out = []
        for kind in KINDS:
            of_type = [c for c in found if c.type is kind]
            server = [
                Candidate(kind, w.start_ms, w.end_ms, Source.SERVER_MARKERS)
                for w in plex
                if w.kind == kind.value and w.source == "plex"
            ]
            readable = [c for c in read_on_file_clock(of_type + server, context) if c.source in LABELS]
            if window := _merge(kind, readable, duration_ms):
                out.append(window)
        return tuple(out)


def _merge(kind: MarkerType, readable: Sequence[Candidate], duration_ms: int) -> OnlineWindow | None:
    """One window from the sources' readings: the best source's times, naming every source that agrees with them
    within ``AGREE_MS`` at both ends and every one that gave other times."""
    ranked = sorted(readable, key=lambda c: SOURCE_ORDER.index(c.source))
    if not ranked:
        return None
    first = ranked[0]
    start, end = first.start_ms, resolve_end_ms(first, duration_ms)
    if not 0 <= start < end <= duration_ms:
        return None
    agree: list[Source] = [first.source]
    dissent: list[Source] = []
    for other in ranked[1:]:
        same = abs(other.start_ms - start) <= AGREE_MS and abs(resolve_end_ms(other, duration_ms) - end) <= AGREE_MS
        (agree if same else dissent).append(other.source)
    return OnlineWindow(
        kind.value,
        start,
        end,
        tuple(dict.fromkeys(LABELS[s] for s in agree)),
        tuple(dict.fromkeys(LABELS[s] for s in dissent if LABELS[s] not in {LABELS[a] for a in agree})),
    )
