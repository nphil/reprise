"""Reprise's autopilot finds new files, plans the backlog, watches for trouble and tells the owner, all by itself.

Everything runs over a real store and job manager with an in-memory Plex, an injected clock and no network: nothing
sleeps, no job ever starts, and every file lives under ``tmp_path``.
"""

import itertools
import json
import os
import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest
from loguru import logger

from media_preview_generator.markers.store import MarkerStore
from media_preview_generator.reprise import recovery
from media_preview_generator.reprise.alerts import TOKEN_FILE, Alerts
from media_preview_generator.reprise.autopilot import (
    ASIDE_QUIET_S,
    BACKFILL_BACKOFF_S,
    BACKFILL_JOB_SIZE,
    BACKFILL_TRIES,
    BACKLOG_FILES_PER_JOB,
    BACKLOG_JOBS_ACTIVE,
    BACKLOG_JOBS_PER_PASS,
    DISK_OK_BYTES,
    FILES_PER_JOB,
    HEALTH_EVERY_S,
    KEEP_TRIES_S,
    LARGE_BATCH,
    LOW_DISK_BYTES,
    NUDGE_DELAY_CAP_S,
    NUDGE_LIMIT,
    NUDGE_RETRY_S,
    NUDGE_TRIES,
    NUDGE_WAIT_S,
    PART_BACKOFF_S,
    PART_TRIES,
    PLEX_DOWN_ALERT_S,
    RECONCILE_EVERY_S,
    SETTING_ALERTS,
    SETTING_AUTO_SEND,
    SETTING_AUTO_SEND_ENDINGS,
    SETTING_AUTO_SEND_INTROS,
    SETTING_BACKFILL,
    Autopilot,
    backfill_order,
    build,
)
from media_preview_generator.reprise.identity import media_version
from media_preview_generator.reprise.models import PlaybackIdentity
from media_preview_generator.reprise.service import (
    AUTOPILOT_BACKFILL_SOURCE,
    AUTOPILOT_JOB_SOURCE,
    RepriseService,
    _mapping_fingerprint,
)
from media_preview_generator.reprise.store import RepriseStore
from media_preview_generator.servers.base import Library, ServerConfig, ServerType
from media_preview_generator.servers.registry import ServerRegistry
from media_preview_generator.web.jobs import PRIORITY_HIGH, PRIORITY_LOW, PRIORITY_NORMAL, JobManager
from media_preview_generator.web.settings_manager import SettingsManager, get_settings_manager, reset_settings_manager

START = 1_700_000_000
START_ISO = "2023-11-14T22:13:20Z"  # START as the status line shows it
TOKEN = "tk_s3cret-TOKEN-value"  # a fake phone-alert token
PLEX_SECRET = "plextoken-0123456789"  # a fake Plex token, which a connection error can carry in its text
MACHINE = "machine"
SERVER = "server"
DURATION_MS = 1_320_000
GIB = 1024**3
SIGNATURE = json.dumps({"audio_streams": [{"plex_id": "1", "index": 1}], "start_time_ms": 0, "streams": []})
NOT_SENDING = "Sending to Plex is switched off"
TABLES = (
    "reprise_alerts",
    "reprise_alert_log",
    "reprise_autopilot_state",
    "reprise_autopilot_tries",
    "reprise_autopilot_plays",
    "reprise_autopilot_attempts",
)


class Clock:
    def __init__(self, now=START):
        self.now = now

    def __call__(self):
        return self.now


class Response:
    def __init__(self, status_code):
        self.status_code = status_code


class FakePost:
    """Records every alert sent; ``outcome`` is a status code or an exception to raise."""

    def __init__(self):
        self.calls = []
        self.outcome = 200

    def __call__(self, url, *, data, headers, timeout):
        self.calls.append({"url": url, "data": data, "headers": dict(headers), "timeout": timeout})
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return Response(self.outcome)

    @property
    def titles(self):
        return [call["headers"]["Title"] for call in self.calls]


class Disk:
    """Stands in for ``shutil`` inside the autopilot: how much space is free is whatever the test says."""

    def __init__(self):
        self.free = 100 * GIB
        self.checks = 0

    def disk_usage(self, path):
        self.checks += 1
        return SimpleNamespace(free=self.free)


class Reply:
    """What Plex answers: a JSON body with a ``MediaContainer``."""

    def __init__(self, container):
        self._container = container

    def json(self):
        return {"MediaContainer": self._container}


class FakePlex:
    """An in-memory Plex: it lists libraries and their episodes page by page, and can be made to fail."""

    def __init__(self, libraries):
        self.libraries = libraries
        self.rows = {}  # library id -> the rows its listing returns
        self.looks = 0  # how many library listings were started
        self.listed = []  # the library ids whose listing was started
        self._broken = None

    def fail(self, prefix="/", error=None):
        """Every request whose path starts with ``prefix`` fails (``"/"`` is all of them)."""
        self._broken = (prefix, error or ConnectionError(f"could not connect (X-Plex-Token={PLEX_SECRET})"))

    def heal(self):
        self._broken = None

    def list_libraries(self):
        return list(self.libraries)

    def _plex_http(self, method, path, **kwargs):
        if self._broken and path.startswith(self._broken[0]):
            raise self._broken[1]
        if path == "/":
            return Reply({"machineIdentifier": MACHINE})
        library_id = path.split("/")[3]  # /library/sections/<id>/all
        rows = self.rows.get(library_id, [])
        start = int(kwargs["params"]["X-Plex-Container-Start"])
        size = int(kwargs["params"]["X-Plex-Container-Size"])
        if start == 0:
            self.looks += 1
            self.listed.append(library_id)
        return Reply({"totalSize": len(rows), "Metadata": rows[start : start + size]})


@pytest.fixture
def env(tmp_path, monkeypatch):
    """The autopilot over a real store and job manager and an in-memory Plex. No job ever starts, and time and free
    disk space are whatever the test says."""
    monkeypatch.setattr("media_preview_generator.web.routes.job_runner._start_job_async", lambda *args, **kwargs: None)
    monkeypatch.setattr(JobManager, "_start_activity_timer", lambda self: None)
    disk = Disk()
    monkeypatch.setattr("media_preview_generator.reprise.autopilot.shutil", disk)
    root = tmp_path / "tv"
    root.mkdir()
    library = Library("1", "TV", (str(root),), kind="show")
    config = ServerConfig(
        id=SERVER,
        type=ServerType.PLEX,
        name="Plex",
        enabled=True,
        url="http://plex.invalid",
        auth={},
        libraries=[library],
        server_identity=MACHINE,
    )
    plex = FakePlex([library])
    registry = ServerRegistry()
    registry._configs[SERVER] = config
    registry._servers[SERVER] = plex
    reset_settings_manager()
    get_settings_manager(str(tmp_path))
    manager = JobManager(config_dir=str(tmp_path))
    monkeypatch.setattr("media_preview_generator.web.jobs._job_manager", manager)
    markers = MarkerStore(str(tmp_path / "markers.db"))
    store = RepriseStore(markers)
    (tmp_path / TOKEN_FILE).write_text(f"  {TOKEN}\n")
    service = RepriseService(store, registry, manager)
    clock, post = Clock(), FakePost()
    alerts = Alerts(store, config_dir=str(tmp_path), post=post, clock=clock)
    autopilot = Autopilot(service, alerts, config_dir=str(tmp_path), clock=clock)
    try:
        yield SimpleNamespace(
            path=tmp_path,
            dir=str(tmp_path),
            database=tmp_path / "markers.db",
            root=root,
            config=config,
            plex=plex,
            registry=registry,
            store=store,
            manager=manager,
            service=service,
            post=post,
            clock=clock,
            disk=disk,
            autopilot=autopilot,
            ids=itertools.count(1),
        )
    finally:
        manager.close()
        markers.close()
        reset_settings_manager()


@pytest.fixture
def settings(env):
    """Saved settings, attached to the registry the way the app does it. Without this fixture there are none."""
    manager = SettingsManager(env.dir)
    env.registry.settings = manager
    return manager


@pytest.fixture
def queued(env, monkeypatch):
    """Every call the autopilot makes to queue new files (nothing is really queued)."""
    calls = []

    def record(server_id, library_id, **kwargs):
        calls.append({"server": server_id, "library": library_id, **kwargs})
        return {}

    monkeypatch.setattr(env.service, "enqueue_library_analysis", record)
    return calls


@pytest.fixture
def backfill(env, monkeypatch):
    """What is unanalysed (``rows``) and every backfill job the autopilot asks for (``jobs``: ``(ids, label)``)."""
    plan = SimpleNamespace(rows=[], jobs=[])
    monkeypatch.setattr(env.service.store, "unanalyzed", lambda: list(plan.rows))
    monkeypatch.setattr(env.service, "enqueue_backfill", lambda ids, label: plan.jobs.append((list(ids), label)))
    return plan


@pytest.fixture
def logs():
    lines = []
    sink = logger.add(lines.append, level="DEBUG")
    yield lines
    logger.remove(sink)


def tick_at(env, when):
    """One autopilot pass at ``when``, with the clock the alerts use showing the same time."""
    env.clock.now = when
    env.autopilot.tick(when)


def add_library(env, library_id, *, kind="show", enabled=True):
    """Another library of the server, with a folder of its own."""
    folder = env.path / f"library-{library_id}"
    folder.mkdir()
    library = Library(library_id, f"Library {library_id}", (str(folder),), enabled=enabled, kind=kind)
    env.plex.libraries.append(library)
    env.config.libraries.append(library)
    return folder


def add_episode(
    env, name, *, folder=None, library="1", show="Show", show_key="900", season=1, episode=1, plays=0, viewed=0
):
    """A new file on disk and the row Plex lists for it (shaped like ``/library/sections/<id>/all``)."""
    number = next(env.ids)
    path = (folder or env.root) / name
    path.write_bytes(f"{name}:{number}".encode() * 4)
    part = {
        "id": 7000 + number,
        "file": str(path),
        "size": path.stat().st_size,
        "key": f"/library/parts/{7000 + number}/file",
    }
    media = {"id": 5000 + number, "duration": DURATION_MS, "Part": [part]}
    row = {
        "ratingKey": str(1000 + number),
        "duration": DURATION_MS,
        "grandparentRatingKey": show_key,
        "grandparentTitle": show,
        "parentIndex": season,
        "index": episode,
        "viewCount": plays,
        "lastViewedAt": viewed,
        "Media": [media],
    }
    env.plex.rows.setdefault(library, []).append(row)
    return SimpleNamespace(
        path=os.path.realpath(path),
        row=row,
        media=media,
        part=part,
        size=part["size"],
        show=show,
        show_key=show_key,
        season=season,
        episode=episode,
        library=library,
    )


def bind(env, episode):
    """Register the episode as a finished read of Plex would have stored it: Reprise then knows its exact file."""
    version = media_version(episode.path, DURATION_MS, SIGNATURE)
    identity = PlaybackIdentity(
        MACHINE,
        episode.row["ratingKey"],
        str(episode.media["id"]),
        str(episode.part["id"]),
        episode.part["key"],
        episode.size,
        DURATION_MS,
        "1",
    )
    metadata = {
        "library_id": episode.library,
        "show_key": episode.show_key,
        "show_title": episode.show,
        "season": episode.season,
        "episode": episode.episode,
        "title": f"Episode {episode.episode}",
        "remote_part_path": episode.path,
        "mapping_fingerprint": _mapping_fingerprint(env.config, env.config.libraries[0]),
        "candidates": [],
    }
    env.store.register_media(version, identity, server_id=SERVER, metadata=metadata)
    return version


def analyse(env, version):
    """Give the registered file a finished analysis."""
    run = env.store.begin_analysis(version.id, "analysis-job", "signature")
    env.store.finish_analysis(run, [], [], version.id)
    env.store.release_analysis(run)


def make_job(env, config=None, *, running=False):
    """A Reprise job with the given settings, waiting (or running)."""
    job = env.manager.create_job(library_name="Reprise: new files", kind="reprise", config=dict(config or {}))
    if running:
        env.manager.start_job(job.id)
    return job


def own_job(env, *, running=False):
    """A job of the automatic kind (one the autopilot itself queued)."""
    return make_job(env, {"source": AUTOPILOT_JOB_SOURCE}, running=running)


def expected(*episodes, priority=PRIORITY_HIGH):
    """What the autopilot asks the service for when it queues these files."""
    return {
        "server": SERVER,
        "library": "1",
        "candidate_paths": [episode.path for episode in episodes],
        "trigger": "autopilot",
        "priority": priority,
    }


def set_aside(env, count=2):
    """A running job that set ``count`` bound episodes aside, at ``START``: ``(job, versions)``."""
    job = own_job(env, running=True)
    versions = [bind(env, add_episode(env, f"aside{n}.mkv", episode=n)) for n in range(1, count + 1)]
    env.clock.now = START
    for version in versions:
        env.autopilot.note_set_aside(job.id, version.id)
    return job, versions


def database_text(env):
    """Everything the autopilot and the alerts keep in the database, as text."""
    with closing(sqlite3.connect(str(env.database))) as connection:
        return "\n".join(str(row) for table in TABLES for row in connection.execute(f"SELECT * FROM {table}"))


# ── new files ─────────────────────────────────────────────────────────────────────────────────────────────────────


def test_a_new_file_is_queued_once_and_not_again_straight_away(env, queued):
    episode = add_episode(env, "new.mkv")
    assert env.autopilot.reconcile(START) == 1
    assert queued == [expected(episode)]
    assert env.autopilot.reconcile(START + 300) == 0
    assert len(queued) == 1


def test_a_file_already_waiting_in_a_job_is_not_queued_again(env):
    episode = add_episode(env, "waiting.mkv")
    env.service.enqueue_library_analysis(SERVER, "1", candidate_paths=[episode.path], trigger="autopilot")
    assert env.autopilot.reconcile(START) == 0
    assert len(env.manager.get_all_jobs()) == 1


def test_new_files_go_into_small_jobs(env, queued):
    episodes = [add_episode(env, f"new{n}.mkv", episode=n) for n in range(FILES_PER_JOB + 1)]
    assert env.autopilot.reconcile(START) == FILES_PER_JOB + 1
    assert [len(call["candidate_paths"]) for call in queued] == [FILES_PER_JOB, 1]
    assert {call["priority"] for call in queued} == {PRIORITY_HIGH}
    assert [path for call in queued for path in call["candidate_paths"]] == [episode.path for episode in episodes]


def test_new_files_use_the_priority_the_owner_chose_for_incoming_jobs(env, settings, queued):
    settings.set("incoming_job_priority", "normal")
    add_episode(env, "new.mkv")
    env.autopilot.reconcile(START)
    assert [call["priority"] for call in queued] == [PRIORITY_NORMAL]


def test_an_analysed_episode_whose_file_has_not_changed_is_left_alone(env, queued):
    analyse(env, bind(env, add_episode(env, "known.mkv")))
    assert env.autopilot.reconcile(START) == 0
    assert queued == []


@pytest.mark.parametrize("change", ["size", "duration"])
def test_a_known_episode_whose_file_was_replaced_is_queued_again(env, queued, change):
    episode = add_episode(env, "upgrade.mkv")
    analyse(env, bind(env, episode))
    if change == "size":
        (env.root / "upgrade.mkv").write_bytes(b"x" * (episode.size + 10))
        episode.part["size"] = episode.size + 10
    else:
        episode.media["duration"] = DURATION_MS + 1000
    assert env.autopilot.reconcile(START) == 1
    assert queued == [expected(episode)]


def test_the_half_hourly_look_finds_a_file_no_webhook_announced(env, queued):
    tick_at(env, START)  # the first pass looks once
    looks = env.plex.looks
    episode = add_episode(env, "missed.mkv")
    tick_at(env, START + RECONCILE_EVERY_S - 1)
    assert env.plex.looks == looks and queued == []
    tick_at(env, START + RECONCILE_EVERY_S)
    assert env.plex.looks == looks + 1
    assert queued == [expected(episode)]


# ── webhooks ──────────────────────────────────────────────────────────────────────────────────────────────────────


def test_a_webhook_makes_the_next_pass_look_at_plex_soon(env, queued):
    tick_at(env, START)  # the last look was just now
    looks = env.plex.looks
    episode = add_episode(env, "arrived.mkv")
    env.clock.now = START + 10
    env.autopilot.nudge(episode.path)
    tick_at(env, START + 10 + NUDGE_WAIT_S - 1)
    assert env.plex.looks == looks
    tick_at(env, START + 10 + NUDGE_WAIT_S)
    assert env.plex.looks == looks + 1
    assert queued == [expected(episode)]


def test_a_file_plex_never_lists_is_looked_for_a_few_times_and_then_dropped(env):
    tick_at(env, START)
    looks = env.plex.looks
    env.clock.now = START
    env.autopilot.nudge("/media/tv/never.mkv")
    turns = [START + NUDGE_WAIT_S + n * NUDGE_RETRY_S for n in range(NUDGE_TRIES)]
    for when in turns:
        tick_at(env, when - 1)  # not yet its turn
        assert env.plex.looks == looks
        tick_at(env, when)
        looks += 1
        assert env.plex.looks == looks
    tick_at(env, turns[-1] + NUDGE_RETRY_S)  # it was given up: no look of its own
    assert env.plex.looks == looks


def test_a_nudge_ends_once_plex_lists_the_file_even_if_there_is_nothing_to_queue(env, queued):
    episode = add_episode(env, "listed.mkv")
    bind(env, episode)  # Reprise already knows this file
    tick_at(env, START)
    looks = env.plex.looks
    env.clock.now = START
    env.autopilot.nudge(episode.path)
    tick_at(env, START + NUDGE_WAIT_S)
    assert env.plex.looks == looks + 1 and queued == []
    tick_at(env, START + NUDGE_WAIT_S + NUDGE_RETRY_S)  # the retry it no longer needs
    assert env.plex.looks == looks + 1


def test_only_so_many_webhooks_are_remembered_at_once(env):
    tick_at(env, START)
    looks = env.plex.looks
    env.clock.now = START
    for number in range(NUDGE_LIMIT):
        env.autopilot.nudge(f"/media/tv/{number}.mkv")
    env.autopilot.nudge("/media/tv/one-too-many.mkv", delay=NUDGE_DELAY_CAP_S)  # dropped: the list is full
    first = START + NUDGE_WAIT_S
    second = first + NUDGE_RETRY_S
    third = second + NUDGE_RETRY_S
    dropped_turn = START + NUDGE_DELAY_CAP_S + NUDGE_WAIT_S
    assert third < dropped_turn < third + NUDGE_RETRY_S  # the dropped one would fall between two retries
    for when in (first, second, third):
        tick_at(env, when)
    assert env.plex.looks == looks + 3
    tick_at(env, dropped_turn)
    assert env.plex.looks == looks + 3


@pytest.mark.parametrize("path", [None, "", b"/media/tv/x.mkv", 123], ids=["none", "empty", "bytes", "number"])
def test_a_webhook_without_a_usable_path_is_ignored(env, path):
    tick_at(env, START)
    looks = env.plex.looks
    env.clock.now = START
    env.autopilot.nudge(path)
    tick_at(env, START + NUDGE_WAIT_S)
    assert env.plex.looks == looks


@pytest.mark.parametrize(
    "delay, wait",
    [
        (0, NUDGE_WAIT_S),
        (30, 30 + NUDGE_WAIT_S),
        (-5, NUDGE_WAIT_S),
        ("soon", NUDGE_WAIT_S),
        (10 * NUDGE_DELAY_CAP_S, NUDGE_DELAY_CAP_S + NUDGE_WAIT_S),
    ],
    ids=["no delay", "a short delay", "a negative delay", "a delay that is no number", "a long delay is capped"],
)
def test_a_webhook_delay_is_added_to_the_wait_up_to_a_cap(env, delay, wait):
    tick_at(env, START)
    looks = env.plex.looks
    env.clock.now = START
    env.autopilot.nudge("/media/tv/slow.mkv", delay=delay)
    tick_at(env, START + wait - 1)
    assert env.plex.looks == looks
    tick_at(env, START + wait)
    assert env.plex.looks == looks + 1


# ── files that are not queued ─────────────────────────────────────────────────────────────────────────────────────


def test_a_file_that_is_not_on_this_machine_yet_is_not_queued(env, queued):
    episode = add_episode(env, "later.mkv")
    os.remove(episode.path)
    assert env.autopilot.reconcile(START) == 0
    assert queued == []


def test_a_file_outside_the_library_folders_is_not_queued(env, queued):
    elsewhere = env.path / "elsewhere"
    elsewhere.mkdir()
    add_episode(env, "stray.mkv", folder=elsewhere)
    assert env.autopilot.reconcile(START) == 0
    assert queued == []


def test_a_file_still_being_copied_waits_until_it_is_complete(env, queued):
    episode = add_episode(env, "copying.mkv")
    episode.part["size"] = episode.size + 1024  # Plex already lists the full size
    assert env.autopilot.reconcile(START) == 0
    assert queued == []
    (env.root / "copying.mkv").write_bytes(b"x" * (episode.size + 1024))  # the copy finishes
    assert env.autopilot.reconcile(START + 60) == 1
    assert queued == [expected(episode)]


def test_a_file_in_several_parts_is_never_queued(env, queued):
    episode = add_episode(env, "split.mkv")
    second = env.root / "split-part2.mkv"
    second.write_bytes(b"the second part")
    episode.media["Part"].append(
        {"id": 9999, "file": str(second), "size": second.stat().st_size, "key": "/library/parts/9999/file"}
    )
    assert env.autopilot.reconcile(START) == 0
    assert queued == []


def test_an_episode_without_media_is_not_queued(env, queued):
    del add_episode(env, "bare.mkv").row["Media"]
    add_episode(env, "empty.mkv", episode=2).row["Media"] = []
    assert env.autopilot.reconcile(START) == 0
    assert queued == []


def test_a_library_that_is_switched_off_is_not_read(env, queued):
    folder = add_library(env, "2", enabled=False)
    add_episode(env, "old.mkv", folder=folder, library="2")
    assert env.autopilot.reconcile(START) == 0
    assert "2" not in env.plex.listed and queued == []


@pytest.mark.parametrize("kind", ["artist", "photo"])
def test_music_and_photo_libraries_are_never_read(env, queued, kind):
    folder = add_library(env, "2", kind=kind)
    add_episode(env, "song.mkv", folder=folder, library="2")
    assert env.autopilot.reconcile(START) == 0
    assert "2" not in env.plex.listed and queued == []


@pytest.mark.parametrize("kind", ["movie", None])
def test_film_libraries_and_libraries_of_no_kind_are_read(env, queued, kind):
    folder = add_library(env, "2", kind=kind)
    add_episode(env, "film.mkv", folder=folder, library="2")
    assert env.autopilot.reconcile(START) == 1
    assert "2" in env.plex.listed
    assert [call["library"] for call in queued] == ["2"]


# ── files that stay unread ────────────────────────────────────────────────────────────────────────────────────────


def test_a_file_that_stays_unread_is_retried_alone_with_growing_gaps_and_then_left_alone(env, queued):
    episode = add_episode(env, "stuck.mkv")
    at = START
    assert env.autopilot.reconcile(at) == 1
    assert queued[-1] == expected(episode)  # the first time it is like any new file
    for gap in PART_BACKOFF_S:
        assert env.autopilot.reconcile(at + gap - 1) == 0  # too soon
        at += gap
        assert env.autopilot.reconcile(at) == 1
        assert queued[-1] == expected(episode, priority=PRIORITY_LOW)  # a repeat goes alone and below the rest
    assert len(queued) == PART_TRIES
    assert env.autopilot.reconcile(at + 10 * PART_BACKOFF_S[-1]) == 0
    assert len(queued) == PART_TRIES


def test_a_file_that_was_given_up_on_is_tried_again_after_a_month(env, queued):
    episode = add_episode(env, "stuck.mkv")
    at = START
    env.autopilot.reconcile(at)
    for gap in PART_BACKOFF_S:
        at += gap
        env.autopilot.reconcile(at)
    assert len(queued) == PART_TRIES
    later = at + KEEP_TRIES_S + 1
    tick_at(env, later)  # this pass still sees the old tries; its health step then clears those older than a month
    assert len(queued) == PART_TRIES
    tick_at(env, later + RECONCILE_EVERY_S)
    assert queued[PART_TRIES:] == [expected(episode)]  # a fresh start: a new file again, not a repeat


# ── backlog ───────────────────────────────────────────────────────────────────────────────────────────────────────


def test_a_big_batch_is_queued_as_a_slow_backlog_a_few_jobs_at_a_time(env, queued):
    total = BACKLOG_JOBS_PER_PASS * BACKLOG_FILES_PER_JOB + LARGE_BATCH + 1
    for number in range(total):
        add_episode(env, f"e{number:03}.mkv", episode=number)  # more than one page of Plex's listing
    first_pass = BACKLOG_JOBS_PER_PASS * BACKLOG_FILES_PER_JOB
    assert env.autopilot.reconcile(START) == first_pass
    assert [len(call["candidate_paths"]) for call in queued] == [BACKLOG_FILES_PER_JOB] * BACKLOG_JOBS_PER_PASS
    assert {call["priority"] for call in queued} == {PRIORITY_LOW}
    left = total - first_pass  # still a backlog on the next pass, so still slow and in big jobs
    assert env.autopilot.reconcile(START + 60) == left
    assert [len(call["candidate_paths"]) for call in queued[BACKLOG_JOBS_PER_PASS:]] == [
        BACKLOG_FILES_PER_JOB,
        left - BACKLOG_FILES_PER_JOB,
    ]
    assert {call["priority"] for call in queued} == {PRIORITY_LOW}


@pytest.mark.parametrize(
    "waiting, jobs", [(BACKLOG_JOBS_ACTIVE, 0), (BACKLOG_JOBS_ACTIVE - 1, 1)], ids=["room for none", "room for one"]
)
def test_no_more_backlog_jobs_are_queued_while_many_of_its_own_are_waiting(env, queued, waiting, jobs):
    for _ in range(waiting):
        own_job(env)
    for number in range(LARGE_BATCH + 1):
        add_episode(env, f"e{number:03}.mkv", episode=number)
    env.autopilot.reconcile(START)
    assert len(queued) == jobs


# ── trouble with Plex ─────────────────────────────────────────────────────────────────────────────────────────────


def test_a_plex_that_lists_no_libraries_counts_as_a_failed_look(env, queued):
    env.plex.libraries = []
    assert env.autopilot.reconcile(START) == 0
    assert env.autopilot.state.get("last_reconcile") is None
    assert env.autopilot.status()["last_reconcile"] is None
    assert env.autopilot.state.get(f"reconcile_failed:{SERVER}") == START
    assert queued == []


def test_a_plex_that_errors_out_does_not_crash_the_look(env, queued):
    add_episode(env, "new.mkv")
    env.plex.fail()
    assert env.autopilot.reconcile(START) == 0
    assert env.autopilot.state.get("last_reconcile") is None
    assert queued == []


def test_the_first_failed_look_is_the_one_remembered_until_a_look_works(env):
    env.plex.fail("/library")
    env.autopilot.reconcile(START)
    env.autopilot.reconcile(START + 600)
    assert env.autopilot.state.get(f"reconcile_failed:{SERVER}") == START
    env.plex.heal()
    env.autopilot.reconcile(START + 1200)
    assert env.autopilot.state.get(f"reconcile_failed:{SERVER}") is None
    assert env.autopilot.state.get("last_reconcile") == START + 1200


def test_a_library_that_cannot_be_read_for_an_hour_raises_one_alert_and_clears_when_it_can(env):
    title = "Reprise can't read Plex's library"
    env.plex.fail("/library")  # Plex answers, but its libraries can't be read
    for when in range(START, START + PLEX_DOWN_ALERT_S, HEALTH_EVERY_S):
        tick_at(env, when)
    assert env.post.calls == []
    tick_at(env, START + PLEX_DOWN_ALERT_S)
    assert env.post.titles == [title]
    healed = START + 3 * RECONCILE_EVERY_S  # a pass that also looks at the library
    for when in range(START + PLEX_DOWN_ALERT_S + HEALTH_EVERY_S, healed, HEALTH_EVERY_S):
        tick_at(env, when)
    assert env.post.titles == [title]  # asked again every pass, told once
    env.plex.heal()
    tick_at(env, healed)
    assert env.post.titles == [title, f"Resolved: {title}"]
    tick_at(env, healed + HEALTH_EVERY_S)
    assert len(env.post.calls) == 2


def test_the_plays_of_each_show_are_remembered_from_the_listing(env, queued):
    add_episode(env, "a1.mkv", show_key="900", episode=1, plays=2, viewed=100)
    add_episode(env, "a2.mkv", show_key="900", episode=2, plays=1, viewed=300)
    add_episode(env, "b1.mkv", show="Unwatched", show_key="901", plays=0)
    add_episode(env, "c1.mkv", show="Third", show_key="902", plays=4, viewed=50)
    env.autopilot.reconcile(START)
    assert env.autopilot.state.plays() == {(SERVER, "1", "900"): (3, 300), (SERVER, "1", "902"): (4, 50)}


# ── backfill: the order ───────────────────────────────────────────────────────────────────────────────────────────


def unread(version_id, *, show="Show", season=1, episode=1, show_key="900", **meta):
    """One entry of ``store.unanalyzed()``: a registered file that has no analysis."""
    metadata = {
        "show_title": show,
        "title": f"{show} {season}x{episode}",
        "show_key": show_key,
        "library_id": "1",
        "season": season,
        "episode": episode,
        **meta,
    }
    return {"version_id": version_id, "server_id": SERVER, "metadata": metadata}


def film(version_id, title):
    return unread(version_id, show="", title=title, kind="movie", season=None, episode=None, show_key=None)


def ids_of(rows):
    return [row["version_id"] for row in rows]


def test_the_backfill_takes_the_most_watched_show_first_then_the_latest_watched_then_a_to_z():
    rows = [
        unread("quiet-zulu", show="Zulu", show_key="z"),
        unread("quiet-alpha", show="Alpha", show_key="a"),
        unread("seen-once", show="Once", show_key="o"),
        unread("seen-often-long-ago", show="Often", show_key="p"),
        unread("seen-often-lately", show="Often too", show_key="q"),
    ]
    plays = {(SERVER, "1", "o"): (1, 900), (SERVER, "1", "p"): (5, 100), (SERVER, "1", "q"): (5, 200)}
    assert ids_of(backfill_order(rows, plays)) == [
        "seen-often-lately",
        "seen-often-long-ago",
        "seen-once",
        "quiet-alpha",
        "quiet-zulu",
    ]


def test_the_backfill_takes_a_shows_episodes_by_season_then_episode():
    rows = [
        unread("s2e1", season=2, episode=1),
        unread("s1e2", season=1, episode=2),
        unread("s1e1", season=1, episode=1),
        unread("s1-no-number", season=1, episode=None),
        unread("no-season", season=None, episode=None),
    ]
    assert ids_of(backfill_order(rows, {})) == ["s1e1", "s1e2", "s1-no-number", "s2e1", "no-season"]


def test_the_backfill_takes_films_after_the_episodes_and_specials_last():
    rows = [
        unread("special", season=0, episode=1),
        film("film-b", "B Film"),
        film("film-a", "A Film"),
        unread("unwatched-episode", show="Zulu", show_key="z"),
    ]
    assert ids_of(backfill_order(rows, {})) == ["unwatched-episode", "film-a", "film-b", "special"]


def test_the_backfill_order_copes_with_season_and_episode_that_are_not_numbers():
    rows = [
        unread("text-numbers", season="1", episode="2"),
        unread("real", season=1, episode=1),
        unread("missing", season=None, episode=None),
        unread("same-again", season=None, episode=None),
    ]
    assert ids_of(backfill_order(rows, {})) == ["real", "text-numbers", "missing", "same-again"]


# ── backfill: the plan ────────────────────────────────────────────────────────────────────────────────────────────


def test_the_backfill_queues_one_small_job_of_the_next_files_in_order(env, backfill):
    backfill.rows = [unread(f"v{n}", episode=n) for n in reversed(range(1, 2 * BACKFILL_JOB_SIZE + 1))]
    wanted = [f"v{n}" for n in range(1, BACKFILL_JOB_SIZE + 1)]
    assert env.autopilot.plan_backfill(START) == wanted
    assert backfill.jobs == [(wanted, "Show")]


@pytest.mark.parametrize(
    "rows, label",
    [
        ([unread("a", show="Alpha"), unread("b", show="Alpha", episode=2)], "Alpha"),
        ([unread("a", show="Alpha"), unread("b", show="Beta", show_key="b")], "Alpha and more"),
        ([film("film", "The Film")], "The Film"),
        ([unread("blank", show="", title="")], "library"),
    ],
    ids=["one show", "several shows", "a film", "no name at all"],
)
def test_the_backfill_job_is_named_after_its_first_show(env, backfill, rows, label):
    backfill.rows = rows
    env.autopilot.plan_backfill(START)
    assert [name for _, name in backfill.jobs] == [label]


@pytest.mark.parametrize("running", [False, True], ids=["waiting", "running"])
def test_the_backfill_waits_while_one_of_its_own_jobs_is_waiting_or_running(env, backfill, running):
    backfill.rows = [unread("a")]
    own_job(env, running=running)
    assert env.autopilot.plan_backfill(START) == []
    assert backfill.jobs == []


def test_the_backfill_is_not_held_back_by_other_jobs_or_finished_ones(env, backfill):
    backfill.rows = [unread("a")]
    make_job(env, {"reprise_owner": "owner", "media_version_ids": ["elsewhere"]})  # the owner's own analysis
    done = own_job(env, running=True)
    env.manager.complete_job(done.id)
    assert env.autopilot.plan_backfill(START) == ["a"]


def test_the_backfill_skips_files_that_already_wait_in_any_job(env, backfill):
    backfill.rows = [unread("a"), unread("b", episode=2), unread("c", episode=3)]
    make_job(env, {"reprise_owner": "owner", "media_version_ids": ["a", "c"]})
    assert env.autopilot.plan_backfill(START) == ["b"]


def test_the_backfill_gives_a_file_a_few_spaced_out_turns_and_then_stops(env, backfill):
    backfill.rows = [unread("stuck")]  # the service never manages to queue it, but each turn still counts
    at = START
    assert env.autopilot.plan_backfill(at) == ["stuck"]
    for gap in BACKFILL_BACKOFF_S:
        assert env.autopilot.plan_backfill(at + gap - 1) == []
        at += gap
        assert env.autopilot.plan_backfill(at) == ["stuck"]
    assert len(backfill.jobs) == BACKFILL_TRIES
    assert env.autopilot.plan_backfill(at + 100 * BACKFILL_BACKOFF_S[-1]) == []


def test_the_backfill_queues_a_real_low_priority_job_for_registered_episodes_in_order(env):
    episodes = [add_episode(env, f"ep{n}.mkv", episode=n) for n in (2, 3, 1)]
    versions = {episode.episode: bind(env, episode) for episode in episodes}
    ids = env.autopilot.plan_backfill(START)
    assert ids == [versions[number].id for number in (1, 2, 3)]
    (job,) = env.manager.get_all_jobs()
    assert (job.kind, job.status.value, job.priority) == ("reprise", "pending", PRIORITY_LOW)
    assert job.config["source"] == AUTOPILOT_BACKFILL_SOURCE
    assert job.config["media_version_ids"] == ids
    assert job.library_name == "Reprise backfill: Show"
    assert env.autopilot.plan_backfill(START + 1) == []  # it never queues ahead of itself
    assert len(env.manager.get_all_jobs()) == 1


def test_turning_the_backfill_off_stops_the_planning(env, settings):
    bind(env, add_episode(env, "unread.mkv"))
    env.autopilot.update_settings({"backfill": False})
    tick_at(env, START)
    assert env.manager.get_all_jobs() == []
    env.autopilot.update_settings({"backfill": True})
    tick_at(env, START + HEALTH_EVERY_S)
    assert [job.config["source"] for job in env.manager.get_all_jobs()] == [AUTOPILOT_BACKFILL_SOURCE]


def test_a_paused_app_plans_no_backfill(env, settings):
    bind(env, add_episode(env, "unread.mkv"))
    settings.processing_paused = True
    tick_at(env, START)
    assert env.manager.get_all_jobs() == []
    settings.processing_paused = False
    tick_at(env, START + HEALTH_EVERY_S)
    assert len(env.manager.get_all_jobs()) == 1


def test_new_imports_go_ahead_of_the_backfill(env):
    bind(env, add_episode(env, "old.mkv"))
    tick_at(env, START)  # plans the backfill
    add_episode(env, "new.mkv", episode=2)
    env.autopilot.reconcile(START + RECONCILE_EVERY_S)
    priorities = {job.config["source"]: job.priority for job in env.manager.get_all_jobs()}
    assert priorities == {AUTOPILOT_BACKFILL_SOURCE: PRIORITY_LOW, AUTOPILOT_JOB_SOURCE: PRIORITY_HIGH}


# ── the switches ──────────────────────────────────────────────────────────────────────────────────────────────────


THREE_ON = {"auto_send": True, "backfill": True, "alerts": True}


def test_three_switches_are_on_and_the_endings_switch_off_until_the_owner_changes_them(env):
    assert env.autopilot.settings() == {**THREE_ON, "auto_send_endings": False}


def test_changed_switches_are_saved_and_survive_a_restart(env, settings):
    assert env.autopilot.update_settings({"auto_send": False, "alerts": False, "auto_send_endings": True}) == {
        "auto_send": False,
        "backfill": True,
        "alerts": False,
        "auto_send_endings": True,
    }
    saved = SettingsManager(env.dir)  # a fresh read of the saved file, as after a restart
    assert saved.get(SETTING_AUTO_SEND) is False and saved.get(SETTING_ALERTS) is False
    assert saved.get(SETTING_AUTO_SEND_ENDINGS) is True
    assert saved.get(SETTING_BACKFILL, True) is True


def test_the_intro_switch_is_not_offered_while_intros_are_not_safe(env, settings):
    assert "auto_send_intros" not in env.autopilot.settings()
    with pytest.raises(ValueError, match="Choose on or off"):
        env.autopilot.update_settings({"auto_send_intros": True})
    assert settings.get(SETTING_AUTO_SEND_INTROS) is None
    settings.set(SETTING_AUTO_SEND_INTROS, True)  # even a stored "on" is inert: no intro is ever a sender's kind
    assert env.autopilot._local_kinds() == ()


def test_the_intro_switch_is_offered_off_and_live_once_intros_are_safe(env, settings, monkeypatch):
    monkeypatch.setattr("media_preview_generator.reprise.autosend.INTRO_AUTOSEND_SAFE", True)
    assert env.autopilot.settings() == {**THREE_ON, "auto_send_endings": False, "auto_send_intros": False}
    assert env.autopilot._local_kinds() == ()
    assert env.autopilot.update_settings({"auto_send_intros": True})["auto_send_intros"] is True
    assert env.autopilot._local_kinds() == ("intro",)
    env.autopilot.update_settings({"auto_send_endings": True})
    assert env.autopilot._local_kinds() == ("credits", "intro")


@pytest.mark.parametrize(
    "values",
    [
        {},
        {"sound": True},
        {"backfill": False, "sound": True},
        {"backfill": 1},
        {"backfill": "yes"},
        {"backfill": None},
        [],
        None,
        "backfill",
    ],
    ids=[
        "nothing",
        "unknown switch",
        "one good and one unknown",
        "a number",
        "a word",
        "none",
        "a list",
        "no dict",
        "text",
    ],
)
def test_a_change_that_is_not_plain_on_or_off_is_refused_and_saves_nothing(env, settings, values):
    with pytest.raises(ValueError, match="Choose on or off"):
        env.autopilot.update_settings(values)
    assert env.autopilot.settings() == {**THREE_ON, "auto_send_endings": False}


def test_switches_cannot_be_saved_when_the_app_has_no_settings(env):
    with pytest.raises(ValueError, match="can't save settings right now"):
        env.autopilot.update_settings({"backfill": False})


def test_the_alerts_switch_is_read_at_every_send(env, settings, monkeypatch):
    post = FakePost()
    monkeypatch.setattr("media_preview_generator.reprise.alerts.requests", SimpleNamespace(post=post))
    pilot = build(env.service, env.dir)
    assert isinstance(pilot, Autopilot)
    settings.set(SETTING_ALERTS, False)
    assert pilot.alerts.problem("disk", "Disk is full", "No space.") is False
    assert post.calls == [] and pilot.alerts.is_open("disk")
    settings.set(SETTING_ALERTS, True)
    assert pilot.alerts.problem("disk", "Disk is full", "No space.") is True
    assert post.titles == ["Disk is full"]
    settings.set(SETTING_ALERTS, False)
    assert pilot.send_test_alert()["sent"] is True  # a test alert goes out even while alerts are off
    assert post.titles == ["Disk is full", "Test alert from Reprise"]


# ── the status line ───────────────────────────────────────────────────────────────────────────────────────────────


def test_status_counts_the_files_waiting_in_its_own_jobs(env):
    assert env.autopilot.status()["queue_length"] == 0
    make_job(env, {"source": AUTOPILOT_JOB_SOURCE, "library_analysis_request": {"candidate_paths": ["/a", "/b", "/c"]}})
    assert env.autopilot.status()["queue_length"] == 3  # not read yet: the files it was told about
    make_job(
        env,
        {
            "source": AUTOPILOT_BACKFILL_SOURCE,
            "media_version_ids": ["a", "b", "c", "d"],
            recovery.QUEUE_KEY: {"next": 1},
        },
    )
    assert env.autopilot.status()["queue_length"] == 3 + 3  # one is done: three remain
    make_job(
        env,
        {
            "source": AUTOPILOT_JOB_SOURCE,
            "library_analysis_request": {"candidate_paths": ["/1", "/2", "/3", "/4", "/5"]},
            recovery.QUEUE_KEY: {"ids": ["x", "y"], "next": 0},
        },
    )
    assert env.autopilot.status()["queue_length"] == 3 + 3 + 2  # read already: the episodes it found
    make_job(env, {"source": "schedule", "media_version_ids": list("1234567")})  # not the autopilot's
    finished = own_job(env, running=True)
    env.manager.complete_job(finished.id)  # finished
    assert env.autopilot.status()["queue_length"] == 8


def test_status_shows_when_plex_was_last_looked_at(env):
    assert env.autopilot.status()["last_reconcile"] is None
    env.autopilot.reconcile(START)
    assert env.autopilot.status()["last_reconcile"] == START_ISO


def test_status_names_the_last_alert_that_was_sent(env):
    assert env.autopilot.status()["last_alert"] is None
    env.autopilot.alerts.problem("disk", "Disk is full", "No space.")
    assert env.autopilot.status()["last_alert"] == {"kind": "problem", "title": "Disk is full", "at": START_ISO}


def test_status_says_why_automatic_sending_is_blocked(env, settings):
    assert env.autopilot.status()["auto_send_blocked"].startswith(NOT_SENDING)  # no publisher yet
    env.service.publisher = SimpleNamespace(writes_enabled=True, unfinished_writes=lambda: [])
    assert env.autopilot.status()["auto_send_blocked"] is None
    env.service.publisher = None
    env.autopilot.update_settings({"auto_send": False})  # switched off by the owner: nothing to explain
    assert env.autopilot.status()["auto_send_blocked"] is None


# ── set-aside episodes and paused jobs ────────────────────────────────────────────────────────────────────────────


def test_set_aside_episodes_are_reported_once_nothing_more_is_set_aside_for_a_couple_of_minutes(env):
    set_aside(env, 2)
    tick_at(env, START + ASIDE_QUIET_S - 1)
    assert env.post.calls == []
    tick_at(env, START + ASIDE_QUIET_S)
    assert env.post.titles == ["Episodes set aside"]
    tick_at(env, START + ASIDE_QUIET_S + 1000)  # asked again, told once
    assert len(env.post.calls) == 1


@pytest.mark.parametrize(
    "count, said",
    [
        (1, "1 episode was set aside after repeated failures: Show S01E01."),
        (3, "3 episodes were set aside after repeated failures: Show S01E01, Show S01E02, Show S01E03."),
        (5, "5 episodes were set aside after repeated failures: Show S01E01, Show S01E02, Show S01E03 and 2 more."),
    ],
)
def test_the_set_aside_alert_names_up_to_three_episodes(env, count, said):
    set_aside(env, count)
    tick_at(env, START + ASIDE_QUIET_S)
    assert said in env.post.calls[0]["data"].decode()


def test_set_aside_episodes_of_a_finished_job_are_reported_at_once(env):
    job, _ = set_aside(env, 2)
    env.manager.complete_job(job.id)
    tick_at(env, START + 1)
    assert env.post.titles == ["Episodes set aside"]


def test_the_set_aside_report_is_closed_once_every_episode_is_analysed(env):
    job, versions = set_aside(env, 2)
    tick_at(env, START + ASIDE_QUIET_S)
    analyse(env, versions[0])
    tick_at(env, START + ASIDE_QUIET_S + 60)
    assert len(env.post.calls) == 1  # one is still waiting
    analyse(env, versions[1])
    tick_at(env, START + ASIDE_QUIET_S + 120)
    assert env.post.titles == ["Episodes set aside", "Resolved: Episodes set aside"]
    tick_at(env, START + ASIDE_QUIET_S + 180)
    assert len(env.post.calls) == 2
    assert env.autopilot.state.get(f"aside:{job.id}") is None


def test_the_set_aside_report_is_closed_when_the_episodes_left_the_library(env):
    set_aside(env, 1)
    tick_at(env, START + ASIDE_QUIET_S)
    env.store.reconcile_library(SERVER, "1", [])
    tick_at(env, START + ASIDE_QUIET_S + 60)
    assert env.post.titles == ["Episodes set aside", "Resolved: Episodes set aside"]


def test_a_paused_analysis_is_reported_at_once_and_closed_when_it_runs_again(env):
    job = own_job(env, running=True)
    env.manager.merge_job_config(job.id, {recovery.QUEUE_KEY: recovery.pause({}, "kept_stopping")})
    env.clock.now = START
    env.autopilot.note_paused(job.id)
    assert env.post.titles == ["Reprise paused an analysis"]
    assert 'Reprise paused "Reprise: new files"' in env.post.calls[0]["data"].decode()
    tick_at(env, START + 60)
    assert len(env.post.calls) == 1  # still paused: nothing new to say
    env.manager.merge_job_config(job.id, {recovery.QUEUE_KEY: {}})  # the owner pressed Resume
    tick_at(env, START + 120)
    assert env.post.titles == ["Reprise paused an analysis", "Resolved: Reprise paused an analysis"]


def test_a_paused_report_is_closed_when_the_job_is_over(env):
    job = own_job(env, running=True)
    env.manager.merge_job_config(job.id, {recovery.QUEUE_KEY: recovery.pause({}, "kept_stopping")})
    env.autopilot.note_paused(job.id)
    env.manager.complete_job(job.id)
    tick_at(env, START + 60)
    assert env.post.titles == ["Reprise paused an analysis", "Resolved: Reprise paused an analysis"]


# ── Plex and disk health ──────────────────────────────────────────────────────────────────────────────────────────


def test_a_plex_that_cannot_be_reached_for_an_hour_raises_one_alert_and_clears_when_it_answers(env):
    title = "Plex can't be reached"
    env.plex.fail()
    for when in range(START, START + PLEX_DOWN_ALERT_S, HEALTH_EVERY_S):
        tick_at(env, when)
    assert env.post.calls == []  # not an hour yet
    tick_at(env, START + PLEX_DOWN_ALERT_S)
    assert env.post.titles == [title]
    assert env.autopilot.alerts.open_keys() == [f"plex_unreachable:{SERVER}"]
    healed = START + 3 * RECONCILE_EVERY_S  # a pass that also looks at the library
    for when in range(START + PLEX_DOWN_ALERT_S + HEALTH_EVERY_S, healed, HEALTH_EVERY_S):
        tick_at(env, when)
    assert env.post.titles == [title]  # asked again every pass, told once
    env.plex.heal()
    tick_at(env, healed)
    assert env.post.titles == [title, f"Resolved: {title}"]
    tick_at(env, healed + HEALTH_EVERY_S)
    assert len(env.post.calls) == 2 and env.autopilot.alerts.open_keys() == []


def test_a_plex_that_comes_back_between_two_looks_does_not_raise_a_second_alert(env):
    title = "Plex can't be reached"
    env.plex.fail()
    for when in range(START, START + PLEX_DOWN_ALERT_S + 1, HEALTH_EVERY_S):
        tick_at(env, when)
    env.plex.heal()
    for when in range(START + PLEX_DOWN_ALERT_S + HEALTH_EVERY_S, START + 3 * RECONCILE_EVERY_S + 1, HEALTH_EVERY_S):
        tick_at(env, when)  # Plex is back at the first of these, which is not a half-hourly look
    assert env.post.titles == [title, f"Resolved: {title}"]


def test_low_disk_space_raises_one_alert_and_clears_only_when_there_is_clearly_enough(env):
    title = "Reprise is running low on disk space"
    env.disk.free = LOW_DISK_BYTES - 1
    tick_at(env, START)
    tick_at(env, START + HEALTH_EVERY_S)
    assert env.post.titles == [title]  # asked again, told once
    env.disk.free = LOW_DISK_BYTES + 1  # a little more is not enough to call it solved
    tick_at(env, START + 2 * HEALTH_EVERY_S)
    env.disk.free = DISK_OK_BYTES - 1
    tick_at(env, START + 3 * HEALTH_EVERY_S)
    assert len(env.post.calls) == 1 and env.autopilot.alerts.is_open("low_disk")
    env.disk.free = DISK_OK_BYTES
    tick_at(env, START + 4 * HEALTH_EVERY_S)
    assert env.post.titles == [title, f"Resolved: {title}"]
    assert not env.autopilot.alerts.is_open("low_disk")


# ── the pass ──────────────────────────────────────────────────────────────────────────────────────────────────────


def test_a_failing_step_is_logged_and_the_others_still_run(env, logs, monkeypatch):
    ran = []

    def explode(now):
        raise RuntimeError("the look for new files broke")

    monkeypatch.setattr(env.autopilot, "_reconcile_if_due", explode)
    monkeypatch.setattr(env.autopilot, "plan_backfill", lambda now: ran.append("backfill"))
    monkeypatch.setattr(env.autopilot.sender, "sweep", lambda now: ran.append("send"))
    monkeypatch.setattr(env.autopilot, "_check_plex", lambda now: ran.append("plex"))
    tick_at(env, START)
    assert ran == ["backfill", "send", "plex"]
    assert any("failed while looking for new files" in line for line in logs)


def test_a_pass_that_starts_while_another_is_running_does_nothing(env, monkeypatch):
    steps = []

    def reconcile_step(now):
        steps.append("reconcile")
        if steps.count("reconcile") == 1:
            env.autopilot.tick(now)  # a second pass begins while this one is still running

    monkeypatch.setattr(env.autopilot, "_reconcile_if_due", reconcile_step)
    monkeypatch.setattr(env.autopilot, "plan_backfill", lambda now: steps.append("backfill"))
    tick_at(env, START)
    assert steps == ["reconcile", "backfill"]


def test_nothing_runs_while_the_app_is_shutting_down(env, monkeypatch):
    stopping = {"now": True}
    monkeypatch.setattr("media_preview_generator.reprise.autopilot.is_shutting_down", lambda: stopping["now"])
    tick_at(env, START)
    assert env.plex.looks == 0 and env.disk.checks == 0
    assert env.autopilot.state.get("reconcile_started") is None
    stopping["now"] = False
    tick_at(env, START + 1)  # the first look was only put off
    assert env.plex.looks == 1 and env.disk.checks == 1


def test_the_health_checks_run_at_most_every_five_minutes(env):
    tick_at(env, START)
    assert env.disk.checks == 1
    tick_at(env, START + HEALTH_EVERY_S - 1)
    assert env.disk.checks == 1
    tick_at(env, START + HEALTH_EVERY_S)
    assert env.disk.checks == 2


# ── privacy ───────────────────────────────────────────────────────────────────────────────────────────────────────


def test_no_secret_reaches_the_logs_the_alerts_the_status_or_the_database(env, logs):
    env.plex.fail()  # the error Plex's side gives carries a Plex token in its text
    env.post.outcome = ConnectionError(f"could not reach the phone service with {TOKEN}")  # and so does this one
    for when in range(START, START + PLEX_DOWN_ALERT_S + 1, HEALTH_EVERY_S):
        tick_at(env, when)
    assert len(env.post.calls) == 1  # the alert was tried, and failed
    env.post.outcome = 200
    tick_at(env, START + PLEX_DOWN_ALERT_S + 4 * HEALTH_EVERY_S)  # the retry gets through
    assert env.post.titles == ["Plex can't be reached"] * 2
    for call in env.post.calls:
        assert TOKEN not in call["headers"]["Title"] + call["data"].decode()
        assert PLEX_SECRET not in call["headers"]["Title"] + call["data"].decode()
    shown = "\n".join(logs) + json.dumps(env.autopilot.status()) + database_text(env)
    assert TOKEN not in shown and PLEX_SECRET not in shown
