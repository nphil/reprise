"""Intro & Credits jobs end to end on the real JobManager, JobGate and dispatcher (fake servers and publishers)."""

import os
import signal
import sqlite3
import sys
import threading
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator import shutdown
from media_preview_generator.job_kinds import JOB_KIND_INTRO_CREDITS, ItemOutcome, KindHandlers
from media_preview_generator.jobs.dispatcher import reset_dispatcher
from media_preview_generator.markers import job_runner, pipeline, triggers
from media_preview_generator.markers.pipeline import PipelineContext
from media_preview_generator.markers.probe import Chapter, MediaProbe
from media_preview_generator.markers.settings import load_global, validate_global
from media_preview_generator.markers.source_counts import DecidedByTally
from media_preview_generator.markers.store import MarkerStore
from media_preview_generator.processing.types import ProcessableItem
from media_preview_generator.servers.base import ServerType
from media_preview_generator.web.job_gate import JobGate
from media_preview_generator.web.jobs import JobManager, JobStatus, is_user_visible_job
from media_preview_generator.web.settings_manager import reset_settings_manager
from tests.markers.fakes import FakeRegistry, ready_publisher, server_config, wait_for
from tests.markers.test_external_ids import EXTRA_SUFFIXES, EXTRAS_FOLDERS

DURATION = 1_321_472
CHAPTERS = (
    Chapter(0, 126_771, "Chapter 1"),
    Chapter(126_771, 157_068, "Intro"),
    Chapter(157_068, 1_295_324, "Chapter 2"),
    Chapter(1_295_324, None, "Credits"),
)


@pytest.fixture
def engine(tmp_path, monkeypatch):
    """Real JobManager + JobGate + shared dispatcher (1 CPU worker); settings, config and GPUs stubbed."""
    # The pool reads its worker groups from the live settings singleton first: one an earlier test left behind
    # would rebuild this test's workers from that test's groups.
    reset_settings_manager()
    reset_dispatcher()
    jm = JobManager(config_dir=str(tmp_path / "config"))
    gate = JobGate()
    settings = {"log_level": "INFO", "webhook_retry_count": 3, "webhook_retry_delay": 30}
    sm = MagicMock(processing_paused=False, gpu_config=[])
    sm.get.side_effect = lambda key, default=None: settings.get(key, default)
    config = SimpleNamespace(cpu_threads=1, gpu_threads=0, scan_workers=4, ffmpeg_path="ffmpeg")
    monkeypatch.setattr(job_runner, "get_job_manager", lambda: jm)
    monkeypatch.setattr(triggers, "get_job_manager", lambda: jm)
    monkeypatch.setattr("media_preview_generator.web.jobs.get_job_manager", lambda *a, **k: jm)
    monkeypatch.setattr(job_runner, "get_settings_manager", lambda: sm)
    monkeypatch.setattr(job_runner, "get_job_gate", lambda: gate)
    monkeypatch.setattr(job_runner, "load_config", lambda: config)
    monkeypatch.setattr(job_runner, "_build_selected_gpus", lambda s, **kw: [])
    yield SimpleNamespace(jm=jm, gate=gate, settings=settings)
    reset_dispatcher()
    reset_settings_manager()


def _outcome(jm, job_id):
    return {k: v for k, v in (jm.get_job(job_id).progress.outcome or {}).items() if v}


class TestRetryThroughThePipeline:
    """Real jobs persist proposals and retry files that are not yet on disk."""

    @pytest.fixture
    def setup(self, engine, tmp_path, monkeypatch):
        folder = tmp_path / "tv" / "Rick and Morty (2013) {tvdb-275274}" / "Season 01"
        folder.mkdir(parents=True)
        media = folder / "Rick and Morty (2013) - S01E01 - Pilot.mkv"
        media.write_bytes(b"x" * 100)
        root = str(tmp_path / "tv")
        store = MarkerStore(str(tmp_path / "markers.db"))
        settings = load_global(
            validate_global(
                {
                    "sources": [
                        {"id": "chapters", "enabled": True},
                        {"id": "theintrodb", "enabled": False},
                        {"id": "introdb", "enabled": False},
                        {"id": "skipdb", "enabled": False},
                    ]
                },
                None,
            )[0]
        )

        def make(servers):
            registry = FakeRegistry({sid: server_config(sid, stype, root=root) for sid, stype in servers})
            publishers = {
                sid: ready_publisher("plex_db" if stype is ServerType.PLEX else "jellyfin_bridge")
                for sid, stype in servers
            }

            def build_context(
                *,
                registry,
                config,
                priority,
                force=False,
                recheck_empty_server_markers=False,
            ):
                return PipelineContext(
                    registry=registry,
                    config=config,
                    settings=settings,
                    store=store,
                    priority=priority,
                    ffprobe="ffprobe",
                    force=force,
                    clients={},
                    live_config=registry.get_config,  # the fake registry stands in for the saved servers
                    recheck_empty_server_markers=recheck_empty_server_markers,
                )

            monkeypatch.setattr(job_runner, "_build_multi_server_registry", lambda: registry)
            monkeypatch.setattr(job_runner, "build_context", build_context)
            monkeypatch.setattr(triggers, "start_intro_credits_job_async", lambda job_id: None)
            return registry, publishers

        yield SimpleNamespace(path=str(media), make=make, store=store)
        store.close()

    def _run_pipeline(self, publishers):
        return (
            patch.object(pipeline, "probe_media", return_value=MediaProbe(DURATION, CHAPTERS)),
            patch.object(pipeline, "publisher_for", side_effect=lambda srv, cfg, **kw: publishers[cfg.id]),
        )

    def _retries(self, jm):
        return [j for j in jm.get_all_jobs() if j.config.get("parent_job_id")]

    def _run_retry(self, monkeypatch, retry):
        due = datetime.fromisoformat(retry.config["retry_not_before"])
        monkeypatch.setattr(job_runner, "_utcnow", lambda: due + timedelta(seconds=1))
        job_runner.run_intro_credits_job(retry.id)

    @pytest.mark.parametrize("source", ["sonarr", "jellyfin", "manual", "inspector"])
    def test_unindexed_file_produces_a_proposal_without_retry_or_write(self, engine, setup, source):
        from media_preview_generator.markers.models import Source

        registry, publishers = setup.make([("jf-1", ServerType.JELLYFIN)])
        registry.get("jf-1").resolve_remote_path_to_item_id.return_value = None
        first_patch, second_patch = self._run_pipeline(publishers)
        with first_patch, second_patch:
            job = triggers.create_intro_credits_job(
                library_name="Intro & Credits · Pilot", priority=2, source=source, file_paths=[setup.path]
            ).id
            job_runner.run_intro_credits_job(job)

        assert engine.jm.get_job(job).status is JobStatus.COMPLETED
        assert _outcome(engine.jm, job) == {"markers_skipped": 1}
        assert self._retries(engine.jm) == []
        publishers["jf-1"].write.assert_not_called()
        rec = setup.store.get_file(setup.path)
        assert {c.source for c in setup.store.get_evidence(rec.id)} >= {Source.CHAPTERS}
        assert setup.store.get_publish_state(rec.id, "jf-1") is None

    def test_the_job_reads_its_files_seasons_ahead_and_stops_when_it_ends(self, engine, setup, monkeypatch):
        registry, publishers = setup.make([("plex-1", ServerType.PLEX)])
        made = []

        class Recorded(job_runner.SeasonReadAhead):
            def __init__(self, ctx, paths, **kwargs):
                super().__init__(ctx, paths, **kwargs)
                self.paths, self.closed, self.ctx = list(paths), False, ctx
                made.append(self)

            def close(self):
                self.closed = True
                super().close()

        monkeypatch.setattr(job_runner, "SeasonReadAhead", Recorded)
        first_patch, second_patch = self._run_pipeline(publishers)
        with first_patch, second_patch:
            job = triggers.create_intro_credits_job(
                library_name="Intro & Credits · Pilot", priority=2, source="manual", file_paths=[setup.path]
            ).id
            job_runner.run_intro_credits_job(job)
        assert engine.jm.get_job(job).status is JobStatus.COMPLETED
        assert [(a.paths, a.closed, a.ctx.season_read_ahead is a) for a in made] == [([setup.path], True, True)]
        assert made[0]._threads == []  # one season: nothing after it to list

    def test_a_file_the_first_run_couldnt_find_counts_in_decided_by_once_the_retry_decides_it(
        self, engine, setup, monkeypatch
    ):
        _, publishers = setup.make([("jf-1", ServerType.JELLYFIN)])
        copying = setup.path + ".partial"
        os.rename(setup.path, copying)  # Sonarr's import is still copying the file
        first_patch, second_patch = self._run_pipeline(publishers)
        with first_patch, second_patch:
            first = triggers.create_intro_credits_job(
                library_name="x", priority=2, source="sonarr", file_paths=[setup.path]
            ).id
            job_runner.run_intro_credits_job(first)
            (retry,) = self._retries(engine.jm)
            head = engine.jm.get_job(first)
            assert head.status is JobStatus.PENDING and head.completed_at is None
            assert (retry.config["file_paths"], retry.config["retry_attempt"], retry.config["retry_delay"]) == (
                [setup.path],
                1,
                60,
            )
            assert retry.config["parent_job_id"] == first and not is_user_visible_job(retry)
            assert head.progress.retry_eta == retry.config["retry_not_before"]
            assert _outcome(engine.jm, first) == {"skipped_file_not_found": 1}
            assert not engine.jm.get_job(first).progress.marker_sources
            os.rename(copying, setup.path)
            self._run_retry(monkeypatch, retry)

        head = engine.jm.get_job(first)
        assert head.status is JobStatus.COMPLETED
        assert _outcome(engine.jm, first) == {"markers_skipped": 1}
        publishers["jf-1"].write.assert_not_called()
        assert head.progress.marker_sources == {"intro": {"chapters": 1}, "credits": {"chapters": 1}}
        assert engine.jm.get_job(retry.id).status is JobStatus.COMPLETED
        assert head.config["last_outcome"] == "completed" and head.progress.retry_eta is None
        assert [(r["file"], r["outcome"]) for r in engine.jm.get_file_results(first)] == [
            (setup.path, "markers_skipped")
        ]
        assert engine.jm.get_file_results(retry.id) == []
        stored = JobManager(config_dir=engine.jm.config_dir).get_job(first)
        assert stored.progress.marker_sources == head.progress.marker_sources

    def test_an_old_top_level_retry_job_still_runs(self, engine, setup, monkeypatch):
        # A "Retry: …" job from before retries ran in their job's row (retry_attempt, no chain) runs as it did.
        registry, publishers = setup.make([("jf-1", ServerType.JELLYFIN)])
        first_patch, second_patch = self._run_pipeline(publishers)
        not_before = "2026-09-22T10:00:00+00:00"
        old = engine.jm.create_job(
            library_name="Retry: Intro & Credits · Pilot",
            kind=JOB_KIND_INTRO_CREDITS,
            priority=2,
            config={
                "kind": JOB_KIND_INTRO_CREDITS,
                "source": "sonarr",
                "libraries": [],
                "file_paths": [setup.path],
                "follows_job_id": None,
                "force": False,
                "webhook_item_id_hints": {},
                "retry_attempt": 1,
                "retry_delay": 60,
                "retry_not_before": not_before,
            },
        )
        assert is_user_visible_job(old)
        with first_patch, second_patch:
            job_runner.run_intro_credits_job(old.id)
        job = engine.jm.get_job(old.id)
        assert (job.status, job.error) == (JobStatus.COMPLETED, None)
        assert _outcome(engine.jm, old.id) == {"markers_skipped": 1}
        publishers["jf-1"].write.assert_not_called()
        assert engine.jm.get_all_jobs() == [job]

    def test_the_finished_job_carries_which_sources_decided_its_markers(self, engine, setup, tmp_path):
        _, publishers = setup.make([("plex-1", ServerType.PLEX)])
        first_patch, second_patch = self._run_pipeline(publishers)
        with first_patch, second_patch:
            job_id = triggers.create_intro_credits_job(
                library_name="x", priority=2, source="manual", file_paths=[setup.path]
            ).id
            job_runner.run_intro_credits_job(job_id)

        expected = {"intro": {"chapters": 1}, "credits": {"chapters": 1}}
        assert engine.jm.get_job(job_id).to_dict()["progress"]["marker_sources"] == expected
        # Stored with the job, so the finished job's summary still shows them after a restart.
        reloaded = JobManager(config_dir=str(tmp_path / "config"))
        assert reloaded.get_job(job_id).progress.marker_sources == expected

    def _with_extras(self, episode):
        """Every extra shape next to the episode; the server lists only the episode as an item, like a real one."""
        season = os.path.dirname(episode)
        extras = [os.path.join(season, f"Rick and Morty (2013) - S01E01 - Pilot-{s}.mkv") for s in EXTRA_SUFFIXES]
        extras += [os.path.join(season, folder, "Making Of.mkv") for folder in EXTRAS_FOLDERS]
        for path in extras:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(b"x" * 100)
        return extras

    def _assert_only_the_episode_was_checked(self, engine, job_id, episode, extras, publisher, server):
        assert engine.jm.get_job(job_id).status is JobStatus.COMPLETED
        assert _outcome(engine.jm, job_id) == {"markers_skipped": len(extras) + 1}
        rows = {r["file"]: r for r in engine.jm.get_file_results(job_id)}
        assert set(rows) == {episode, *extras}
        for path in extras:
            assert (rows[path]["outcome"], rows[path]["reason"]) == (
                "markers_skipped",
                "Extras aren't checked for markers",
            )
            assert not rows[path].get("servers")
        assert rows[episode]["outcome"] == "markers_skipped"
        publisher.write.assert_not_called()
        assert [j.id for j in engine.jm.get_all_jobs()] == [job_id]  # no retry, no verify

    @pytest.mark.parametrize("source", ["manual", "radarr"])
    def test_a_folder_job_skips_its_extras_and_queues_no_retry_for_them(self, engine, setup, source):
        registry, publishers = setup.make([("jf-1", ServerType.JELLYFIN)])
        server = registry.get("jf-1")
        server.resolve_remote_path_to_item_id.side_effect = lambda path, **kw: (
            "item-jf-1" if path == setup.path else None
        )
        extras = self._with_extras(setup.path)
        first_patch, second_patch = self._run_pipeline(publishers)
        with first_patch, second_patch:
            job = triggers.create_intro_credits_job(
                library_name="Season 01", priority=2, source=source, file_paths=[os.path.dirname(setup.path)]
            ).id
            job_runner.run_intro_credits_job(job)
        self._assert_only_the_episode_was_checked(engine, job, setup.path, extras, publishers["jf-1"], server)

    def test_a_library_job_skips_the_extras_the_listing_returns(self, engine, setup, monkeypatch):
        from media_preview_generator.jobs import orchestrator

        registry, publishers = setup.make([("jf-1", ServerType.JELLYFIN)])
        server = registry.get("jf-1")
        server.resolve_remote_path_to_item_id.side_effect = lambda path, **kw: (
            "item-jf-1" if path == setup.path else None
        )
        extras = self._with_extras(setup.path)
        listed = [setup.path, *extras]
        enumerate_items = MagicMock(
            side_effect=lambda candidates, **kw: ([(candidates[0], ProcessableItem(p, "jf-1")) for p in listed], [])
        )
        monkeypatch.setattr(orchestrator, "_enumerate_items_for_servers", enumerate_items)
        first_patch, second_patch = self._run_pipeline(publishers)
        with first_patch, second_patch:
            job = triggers.create_intro_credits_job(
                library_name="TV Shows",
                priority=3,
                source="schedule",
                libraries=[{"server_id": "jf-1", "library_id": "1"}],
            ).id
            job_runner.run_intro_credits_job(job)
        assert [cfg.id for cfg in enumerate_items.call_args.args[0]] == ["jf-1"]
        self._assert_only_the_episode_was_checked(engine, job, setup.path, extras, publishers["jf-1"], server)


class TestGoneFromDiskThroughThePipeline:
    """A file missing from disk, on the real runner and pipeline, as previews treat it (``source_replaced_reason``).

    Sonarr replaced a file with a new release. The preview job counted the
    old path "Gone from disk"; the Intro & Credits follow-up ran three retries over 16 minutes and ended in two red jobs
    and an ERROR. A file a newer one replaced in its folder now ends "Gone from disk" with no retry and the job green;
    one with no replacement keeps today's retry (a webhook's file may still be copying) or today's "not found" (a scan).
    """

    setup = TestRetryThroughThePipeline.setup
    _run_pipeline = TestRetryThroughThePipeline._run_pipeline
    _retries = TestRetryThroughThePipeline._retries
    _run_retry = TestRetryThroughThePipeline._run_retry

    STALE_NAME = "Rick and Morty (2013) - S01E01 - Pilot-CAKES.mkv"

    def _job(self, engine, monkeypatch, path, source):
        """Queue and run a webhook job naming ``path``, or a library scan whose listing returns it."""
        if source == "sonarr":
            job_id = triggers.create_intro_credits_job(
                library_name="x", priority=2, source="sonarr", file_paths=[path]
            ).id
        else:
            from media_preview_generator.jobs import orchestrator

            listed = MagicMock(
                side_effect=lambda candidates, **kw: ([(candidates[0], ProcessableItem(path, "plex-1"))], [])
            )
            monkeypatch.setattr(orchestrator, "_enumerate_items_for_servers", listed)
            job_id = triggers.create_intro_credits_job(
                library_name="TV Shows",
                priority=3,
                source="schedule",
                libraries=[{"server_id": "plex-1", "library_id": "1"}],
            ).id
        job_runner.run_intro_credits_job(job_id)
        return job_id

    @pytest.mark.parametrize("source", ["sonarr", "schedule"], ids=["webhook", "scan"])
    def test_a_file_a_newer_file_replaced_ends_gone_from_disk_with_no_retry_and_the_job_green(
        self, engine, setup, monkeypatch, source
    ):
        _, publishers = setup.make([("plex-1", ServerType.PLEX)])
        # The newer release (setup.path) sits in the folder; the path the job names is the old one Sonarr deleted.
        stale = os.path.join(os.path.dirname(setup.path), self.STALE_NAME)
        first_patch, second_patch = self._run_pipeline(publishers)
        with first_patch, second_patch:
            job_id = self._job(engine, monkeypatch, stale, source)

        assert _outcome(engine.jm, job_id) == {"skipped_source_gone": 1}
        [row] = engine.jm.get_file_results(job_id)
        assert (row["file"], row["outcome"], row["reason"]) == (
            stale,
            "skipped_source_gone",
            "Skipped: replaced by a newer file (Rick and Morty (2013) - S01E01 - Pilot.mkv)",
        )
        assert self._retries(engine.jm) == []
        job = engine.jm.get_job(job_id)
        assert (job.status, job.error) == (JobStatus.COMPLETED, None)
        assert job.config.get("last_outcome") is None  # no retry chain was ever started
        publishers["plex-1"].write.assert_not_called()
        logs = engine.jm.get_logs(job_id)
        assert any("INFO - Done: 1 file" in line and "1 gone from disk" in line for line in logs), logs
        assert not any("ERROR" in line or "not on disk" in line for line in logs), logs
        # One compact line for the file; the app log's longer account stays out of the job's log.
        messages = [line.split("] ", 1)[1] for line in logs]
        assert (
            "INFO - Rick and Morty (2013) S01E01: Skipped: replaced by a newer file (Rick and Morty (2013) - S01E01 - "
            "Pilot.mkv)"
        ) in messages, logs
        assert not any("Source file" in line for line in logs), logs

    def test_a_webhook_file_missing_that_appears_later_is_processed_by_the_retry(self, engine, setup, monkeypatch):
        _, publishers = setup.make([("plex-1", ServerType.PLEX)])
        copying = setup.path + ".partial"  # not a video: no replacement, so the file may still be copying in
        os.rename(setup.path, copying)
        first_patch, second_patch = self._run_pipeline(publishers)
        with first_patch, second_patch:
            first = self._job(engine, monkeypatch, setup.path, "sonarr")
            assert _outcome(engine.jm, first) == {"skipped_file_not_found": 1}
            (retry,) = self._retries(engine.jm)
            assert (retry.config["file_paths"], retry.config["retry_attempt"]) == ([setup.path], 1)
            os.rename(copying, setup.path)
            self._run_retry(monkeypatch, retry)

        head = engine.jm.get_job(first)
        assert (head.status, head.error, head.config["last_outcome"]) == (JobStatus.COMPLETED, None, "completed")
        assert _outcome(engine.jm, first) == {"markers_skipped": 1}
        publishers["plex-1"].write.assert_not_called()
        assert setup.store.get_evidence(setup.store.get_file(setup.path).id)

    def test_a_webhook_file_a_newer_file_replaces_while_it_waits_ends_the_chain_green(self, engine, setup, monkeypatch):
        _, publishers = setup.make([("plex-1", ServerType.PLEX)])
        newer = setup.path + ".partial"  # the newer release is still copying in under a temporary name
        os.rename(setup.path, newer)
        stale = os.path.join(os.path.dirname(setup.path), self.STALE_NAME)
        first_patch, second_patch = self._run_pipeline(publishers)
        with first_patch, second_patch:
            first = self._job(engine, monkeypatch, stale, "sonarr")
            (retry,) = self._retries(engine.jm)
            os.rename(newer, setup.path)  # the import finished: the old file's place is taken
            self._run_retry(monkeypatch, retry)

        assert [r.id for r in self._retries(engine.jm)] == [retry.id]  # no second retry
        head = engine.jm.get_job(first)
        assert (head.status, head.error, head.config["last_outcome"]) == (JobStatus.COMPLETED, None, "completed")
        assert _outcome(engine.jm, first) == {"skipped_source_gone": 1}
        publishers["plex-1"].write.assert_not_called()

    def test_a_webhook_file_missing_for_good_still_retries_until_the_retries_run_out(self, engine, setup, monkeypatch):
        engine.settings["webhook_retry_count"] = 1
        _, publishers = setup.make([("plex-1", ServerType.PLEX)])
        os.remove(setup.path)  # nothing took its place: it may still be copying, so it is waited for
        first_patch, second_patch = self._run_pipeline(publishers)
        with first_patch, second_patch:
            first = self._job(engine, monkeypatch, setup.path, "sonarr")
            (retry,) = self._retries(engine.jm)
            self._run_retry(monkeypatch, retry)

        head = engine.jm.get_job(first)
        assert (head.status, head.config["last_outcome"]) == (JobStatus.FAILED, "exhausted")
        assert head.error == "1 file(s) still not on disk after 1 retry. Check the Files panel for the affected paths."
        assert _outcome(engine.jm, first) == {"skipped_file_not_found": 1}
        publishers["plex-1"].write.assert_not_called()

    def test_a_scan_file_missing_gets_no_retry_and_the_next_scan_processes_it_once_it_is_back(
        self, engine, setup, monkeypatch
    ):
        _, publishers = setup.make([("plex-1", ServerType.PLEX)])
        away = setup.path + ".away"
        os.rename(setup.path, away)
        first_patch, second_patch = self._run_pipeline(publishers)
        with first_patch, second_patch:
            first = self._job(engine, monkeypatch, setup.path, "schedule")
            # A listing only names files the server already has: waiting won't bring one back, the next scan does.
            assert self._retries(engine.jm) == []
            job = engine.jm.get_job(first)
            assert _outcome(engine.jm, first) == {"skipped_file_not_found": 1}
            assert (job.status, job.error) == (
                JobStatus.FAILED,
                "All 1 file(s) weren't found on disk — check the path mappings",
            )
            os.rename(away, setup.path)
            second = self._job(engine, monkeypatch, setup.path, "schedule")

        assert _outcome(engine.jm, second) == {"markers_skipped": 1}
        assert engine.jm.get_job(second).status is JobStatus.COMPLETED
        assert self._retries(engine.jm) == []
        publishers["plex-1"].write.assert_not_called()
        assert setup.store.get_evidence(setup.store.get_file(setup.path).id)


@pytest.mark.real_job_async
class TestRealJobThread:
    def test_job_holds_no_slot_once_submitted_so_a_high_job_starts_up_then_it_finishes(self, engine, monkeypatch):
        release_item = threading.Event()
        checked = []

        def check_fn(item, *, cancel_check=None):
            checked.append(item.canonical_path)
            if item.canonical_path.endswith("slow.mkv"):
                release_item.wait(10)
            return ItemOutcome("markers_published", "ok", [])

        handlers = KindHandlers(
            check_fn=check_fn,
            process_fn=lambda item, **kw: ItemOutcome("failed", "unexpected worker stage"),
            outcome_keys=("markers_published", "failed"),
            check_share=0.25,
        )
        monkeypatch.setattr(job_runner, "_build_multi_server_registry", lambda: MagicMock())
        # The real job manager stores the context's counts on the job, so those have to be real.
        monkeypatch.setattr(
            job_runner,
            "build_context",
            lambda **kw: MagicMock(decided_by=DecidedByTally(), **{"take_missing.return_value": 0}),
        )
        monkeypatch.setattr(job_runner, "kind_handlers", lambda ctx: handlers)
        items = [ProcessableItem(f"/m/{name}.mkv", "") for name in ("a", "slow", "c")]
        monkeypatch.setattr(job_runner, "build_items", lambda cfg, **kw: (items, [], {}))

        job = triggers.create_intro_credits_job(library_name="real thread", priority=3, source="manual")
        jm, gate = engine.jm, engine.gate
        try:
            assert wait_for(lambda: "/m/slow.mkv" in checked), "the job thread never dispatched"
            assert jm.get_job(job.id).status is JobStatus.RUNNING
            assert wait_for(lambda: gate.snapshot()[0] == 0), "a submitted job kept its start-up slot"
            assert jm.request_pause(job.id)
            assert gate.acquire(1, cancel_check=lambda: False) is True  # a HIGH preview job starts up at once
            gate.release()
            release_item.set()
            assert jm.request_resume(job.id)
            assert wait_for(lambda: jm.get_job(job.id).status is JobStatus.COMPLETED), jm.get_job(job.id).status
        finally:
            release_item.set()
        assert _outcome(jm, job.id) == {"markers_published": 3}
        # The job is marked completed before its thread's teardown gives the slot back.
        assert wait_for(lambda: gate.snapshot()[0] == 0), "the finished job kept its slot"
        assert sorted(r["file"] for r in jm.get_file_results(job.id)) == ["/m/a.mkv", "/m/c.mkv", "/m/slow.mkv"]
        assert wait_for(lambda: job.id not in job_runner._inflight_jobs)


class TestCreditTextOnTheWorkers:
    """Worker → pipeline → credit text detector on the real runner, gate, dispatcher and worker (the text is read on the worker's own GPU, a GPU decode failure reruns the file on the CPU in the same worker,
    and a cancel or a text detection failure gives every worker and job slot back. Only the decode (with its start-time
    probe) and the helper pool are faked (the frame and helper boundaries)."""

    @pytest.fixture
    def setup(self, engine, tmp_path, monkeypatch):
        import numpy as np

        from media_preview_generator.markers.credits import detector, frames
        from media_preview_generator.markers.credits.textdet_helper import TextDetState
        from media_preview_generator.markers.pipeline import default_local_detectors

        folder = tmp_path / "tv" / "Rick and Morty (2013) {tvdb-275274}" / "Season 01"
        folder.mkdir(parents=True)
        media = folder / "Rick and Morty (2013) - S01E01 - Pilot.mkv"
        media.write_bytes(b"x" * 100)
        store = MarkerStore(str(tmp_path / "markers.db"))
        raw = {
            "detect": {"intro": False, "credits": True},
            "sources": [{"id": source, "enabled": source == "credits_text"} for source in (
                "chapters", "theintrodb", "introdb", "skipdb", "season_audio", "credits_text", "server_markers")],
        }  # fmt: skip
        settings = load_global(validate_global(raw, None)[0])
        registry = FakeRegistry({"plex-1": server_config("plex-1", ServerType.PLEX, root=str(tmp_path / "tv"))})
        publisher = ready_publisher()
        # One GPU worker and no CPU worker: the CPU rerun can only be that same worker's.
        config = SimpleNamespace(cpu_threads=0, gpu_threads=1, scan_workers=4, ffmpeg_path="ffmpeg")
        monkeypatch.setattr(job_runner, "load_config", lambda: config)
        monkeypatch.setattr(
            job_runner, "_build_selected_gpus", lambda s, **kw: [("NVIDIA", "cuda:0", {"name": "Test GPU"})]
        )
        monkeypatch.setattr(job_runner, "_build_multi_server_registry", lambda: registry)
        monkeypatch.setattr(triggers, "start_intro_credits_job_async", lambda job_id: None)

        def build_context(
            *,
            registry,
            config,
            priority,
            force=False,
            recheck_empty_server_markers=False,
        ):
            return PipelineContext(
                registry=registry,
                config=config,
                settings=settings,
                store=store,
                priority=priority,
                ffprobe="ffprobe",
                force=force,
                clients={},
                local_detectors=default_local_detectors(settings, config, credits_text=TextDetState.AVAILABLE),
                credits_text=TextDetState.AVAILABLE,
                live_config=registry.get_config,
            )

        monkeypatch.setattr(job_runner, "build_context", build_context)
        decodes: list[tuple] = []
        effects: dict = {}

        def decode_rows(path, *, gpu, gpu_device_path, detect_boxes, cancel_check, **kwargs):
            decodes.append((gpu, gpu_device_path))
            effect = effects.get("gpu" if gpu else "cpu")
            if callable(effect):
                effect()
            elif isinstance(effect, BaseException):
                raise effect
            detect_boxes(np.zeros((1, frames.FRAME_H, frames.FRAME_W), np.uint8))
            # No credit roll in the tail ("nothing found"), unless a test hands this decoder frames of its own.
            return effects.get("gpu rows" if gpu else "cpu rows", [])

        pool = MagicMock()
        pool.detect_boxes.return_value = [()]
        monkeypatch.setattr(frames, "decode_rows", decode_rows)
        # The detector reads the container's start time and which packets its keyframe pass drops once per file before
        # its decodes: part of the faked decode.
        monkeypatch.setattr(frames, "container_start_s", lambda *args, **kwargs: 0.0)
        monkeypatch.setattr(frames, "keyframe_thinning", lambda *args, **kwargs: frames.KeyframeThinning())
        monkeypatch.setattr(detector, "get_textdet_pool", lambda: pool)
        yield SimpleNamespace(path=str(media), store=store, publisher=publisher, decodes=decodes, effects=effects,
                              pool=pool)  # fmt: skip
        store.close()

    def _run(self, engine, setup):
        with (
            patch.object(pipeline, "probe_media", return_value=MediaProbe(DURATION, ())),
            patch.object(pipeline, "publisher_for", return_value=setup.publisher),
        ):
            job = triggers.create_intro_credits_job(library_name="TV", priority=2, source="manual",
                                                    file_paths=[setup.path])  # fmt: skip
            job_runner.run_intro_credits_job(job.id)
        setup.publisher.write.assert_not_called()
        return engine.jm.get_job(job.id)

    @staticmethod
    def _worker():
        from media_preview_generator.jobs.dispatcher import get_dispatcher

        (worker,) = get_dispatcher().worker_pool._snapshot_workers()
        return worker

    def _released(self, engine):
        worker = self._worker()
        return engine.gate.snapshot()[0] == 0 and wait_for(lambda: not worker.is_busy)

    def test_the_worker_reads_the_text_on_its_gpu(self, engine, setup):
        job = self._run(engine, setup)
        assert setup.decodes == [("NVIDIA", "cuda:0")]
        kwargs = setup.pool.detect_boxes.call_args.kwargs
        assert (kwargs["gpu"], kwargs["gpu_device_path"], kwargs["gpu_worker"]) == ("NVIDIA", "cuda:0", True)
        assert callable(kwargs["on_cpu"]) and callable(kwargs["cancel_check"])
        assert job.status is JobStatus.COMPLETED and _outcome(engine.jm, job.id) == {"markers_skipped": 1}
        assert self._worker().fallback_active is False
        assert self._released(engine)

    def test_a_gpu_decode_failure_reruns_the_file_on_the_cpu_in_the_same_worker(self, engine, setup):
        from media_preview_generator.markers.credits import detector, frames
        from media_preview_generator.markers.models import Source

        setup.effects["gpu"] = frames.GpuDecodeError("ffmpeg exited 1 decoding S01E01.mkv on the GPU")
        job = self._run(engine, setup)
        assert setup.decodes == [("NVIDIA", "cuda:0"), (None, None)]
        # Still a GPU worker's request with no GPU: its CPU text detection gets a helper of its own, not a CPU worker's.
        kwargs = setup.pool.detect_boxes.call_args.kwargs
        assert (kwargs["gpu"], kwargs["gpu_device_path"], kwargs["gpu_worker"]) == (None, None, True)
        worker = self._worker()
        assert worker.fallback_active is True and "exited 1" in worker.fallback_reason
        rec = setup.store.get_file(setup.path)
        assert setup.store.evidence_version(rec.id, Source.CREDITS_TEXT) == detector.CREDITS_TEXT_VERSION
        # Counted once, as the CPU rerun ended.
        assert job.status is JobStatus.COMPLETED and _outcome(engine.jm, job.id) == {"markers_skipped": 1}
        [row] = engine.jm.get_file_results(job.id)
        assert row["worker"] == "GPU Worker 1 (Test GPU)"
        assert self._released(engine)

    def test_a_file_cut_short_is_no_gpu_fallback_and_is_not_read_again(self, engine, setup, monkeypatch):
        # Most GPU->CPU fallbacks seen in production were files cut short, the GPU blamed for each.
        from media_preview_generator.markers.credits import detector, frames
        from media_preview_generator.markers.models import Source

        setup.effects["gpu"] = frames.GpuReadNothingError()
        measured = []
        monkeypatch.setattr(frames, "readable_video_s", lambda *a, **kw: measured.append(kw["from_s"]) or 600.0)
        job = self._run(engine, setup)
        # The GPU's reading, then the CPU's check of the same file in the same worker: no rerun of the whole file.
        assert setup.decodes == [("NVIDIA", "cuda:0"), (None, None)]
        assert measured == [DURATION / 1000 - frames.EPISODE_TAIL_S]
        worker = self._worker()
        assert (worker.fallback_active, worker.fallback_reason) == (False, None)
        rec = setup.store.get_file(setup.path)
        cut_short = "the file ends before its stated length (10:00 of 22:01 readable)"
        assert setup.store.get_detector_failure(rec.id, Source.CREDITS_TEXT) == cut_short
        # The file's own read failed and nothing was written: the file failed and says why, with no retry job.
        assert (job.status, job.error) == (JobStatus.FAILED, "All 1 file(s) failed — see the Files panel")
        assert _outcome(engine.jm, job.id) == {"failed": 1}
        [row] = engine.jm.get_file_results(job.id)
        assert row["reason"].startswith(f"Couldn't read the credits: {cut_short}; ")
        assert [(r["id"], r["status"]) for r in row["servers"]] == [("plex-1", "failed")]
        assert self._released(engine)
        episode = "Rick and Morty (2013) S01E01"
        first = [line.split("] ", 1)[1] for line in engine.jm.get_logs(job.id)]
        assert f"INFO - {episode} · Credit text: {cut_short}" in first

        # The next scan of the same file: no worker, no decode; the recorded reason fails the file again.
        again = self._run(engine, setup)
        assert setup.decodes == [("NVIDIA", "cuda:0"), (None, None)]
        assert measured == [DURATION / 1000 - frames.EPISODE_TAIL_S]
        assert again.status is JobStatus.FAILED and _outcome(engine.jm, again.id) == {"failed": 1}
        assert any(cut_short in line for line in engine.jm.get_logs(again.id))
        # Nothing changed since, so the file still gets its full lines, not a decode.
        messages = [line.split("] ", 1)[1] for line in engine.jm.get_logs(again.id)]
        assert f"INFO - {episode}: checking credits" in messages
        assert f"INFO - {episode} · Credit text: {cut_short}" in messages
        assert f"INFO - {episode} · Decided: credits nothing found" in messages
        assert detector.credits_text_failed_here(rec, SimpleNamespace(store=setup.store)) is True

    def test_a_detector_stopped_by_a_siblings_folder_leaves_the_job_green(self, engine, setup):
        from media_preview_generator.markers.pipeline import DetectorUnavailableError

        # Season audio's listing error on other episodes' folders, as the detector raises it: not this file's read.
        setup.effects["gpu"] = DetectorUnavailableError("Season 02 can't be read")
        job = self._run(engine, setup)
        assert setup.decodes == [("NVIDIA", "cuda:0")]  # no CPU rerun: the GPU didn't fail
        assert job.status is JobStatus.COMPLETED and job.error is None
        assert _outcome(engine.jm, job.id) == {"markers_skipped": 1}
        [row] = engine.jm.get_file_results(job.id)
        assert "Couldn't read" not in (row["reason"] or "")
        assert [(r["id"], r["status"]) for r in row["servers"]] == [("plex-1", "markers_skipped")]
        messages = [line.split("] ", 1)[1] for line in engine.jm.get_logs(job.id)]
        assert (
            "INFO - Rick and Morty (2013) S01E01 · Credit text: no answer this time (Season 02 can't be read)"
            in messages
        )
        assert self._released(engine)

    def test_a_gpu_that_misses_frames_the_cpu_reads_is_a_gpu_fallback(self, engine, setup, monkeypatch):
        from media_preview_generator.markers.credits import frames

        setup.effects["gpu"] = frames.GpuReadNothingError()
        setup.effects["cpu rows"] = [(1000.0 + 2 * i, 0, 120.0, ()) for i in range(10)]  # frames, no credit roll
        measured = []
        monkeypatch.setattr(frames, "readable_video_s", lambda *a, **kw: measured.append(kw) or 600.0)
        job = self._run(engine, setup)
        assert setup.decodes[:2] == [("NVIDIA", "cuda:0"), (None, None)]
        assert all(device == (None, None) for device in setup.decodes[1:])  # the rest of it read on the CPU too
        assert measured == []
        worker = self._worker()
        assert worker.fallback_active is True
        assert worker.fallback_reason == (
            "The GPU read no frames in the end of Rick and Morty (2013) - S01E01 - Pilot.mkv, but the CPU did; its "
            "credits were read on the CPU"
        )
        assert job.status is JobStatus.COMPLETED and _outcome(engine.jm, job.id) == {"markers_skipped": 1}
        assert self._released(engine)
        import re

        credit_text = [line for line in engine.jm.get_logs(job.id) if "· Credit text: " in line]
        assert len(credit_text) == 1, credit_text
        assert re.search(
            r"· Credit text: none found \(read on the CPU after the GPU read nothing \([\d.]+ s in all\)\)$",
            credit_text[0],
        ), credit_text

    def test_text_detection_read_on_the_cpu_on_a_gpu_worker_shows_on_the_worker_row(self, engine, setup):
        # The GPU helper failed this request: the pool reads it on the CPU and says so through the worker's callback.
        def on_the_cpu(planes, **kwargs):
            kwargs["on_cpu"](
                "Credits read on the CPU: the GPU crashed (killed by SIGKILL) on this file. It's tried again shortly."
            )
            return [()] * len(planes)

        setup.pool.detect_boxes.side_effect = on_the_cpu
        job = self._run(engine, setup)
        assert setup.decodes == [("NVIDIA", "cuda:0")]  # the decode itself stayed on the GPU
        worker = self._worker()
        assert worker.fallback_active is True
        assert (
            worker.fallback_reason
            == "Credits read on the CPU: the GPU crashed (killed by SIGKILL) on this file. It's tried again shortly."
        )
        assert job.status is JobStatus.COMPLETED and _outcome(engine.jm, job.id) == {"markers_skipped": 1}
        assert self._released(engine)

    def test_a_cancel_during_the_decode_frees_the_worker_and_the_slot_without_a_cpu_rerun(self, engine, setup):
        from media_preview_generator.markers.credits import frames

        def cancel_then_stop():
            (running,) = engine.jm.get_running_jobs()
            engine.jm.request_cancellation(running.id)
            raise frames.DecodeCancelledError("cancelled while decoding S01E01.mkv")  # what run_decode then raises

        setup.effects["gpu"] = cancel_then_stop
        job = self._run(engine, setup)
        assert setup.decodes == [("NVIDIA", "cuda:0")]
        assert job.status is JobStatus.CANCELLED
        assert setup.publisher.write.call_count == 0
        assert self._released(engine)

    @pytest.mark.parametrize("where", ["gpu-helper", "cpu-helper-after-gpu-decode-failure"])
    def test_a_text_detection_failure_fails_the_file_unanswered_and_frees_every_slot(self, engine, setup, where):
        from media_preview_generator.markers.credits import frames
        from media_preview_generator.markers.credits.textdet_helper import TextDetUnavailableError
        from media_preview_generator.markers.models import Source

        # The pool itself moves a crashed GPU helper's device to the CPU; what reaches the detector is the CPU helper
        # failing too (``TextDetectorPool.detect_boxes``).
        setup.pool.detect_boxes.side_effect = TextDetUnavailableError("Text detection failed: the helper exited (-9)")
        if where != "gpu-helper":
            setup.effects["gpu"] = frames.GpuDecodeError("ffmpeg exited 1 decoding S01E01.mkv on the GPU")
        job = self._run(engine, setup)
        rec = setup.store.get_file(setup.path)
        assert setup.store.evidence_version(rec.id, Source.CREDITS_TEXT) is None  # asked again next run
        # Nothing written and the file's own read failed: the file failed and says why (no retry job: the next run
        # reads it again anyway).
        assert job.status is JobStatus.FAILED and _outcome(engine.jm, job.id) == {"failed": 1}
        [row] = engine.jm.get_file_results(job.id)
        failure = "Couldn't read the credits: Text detection failed: the helper exited (-9)"
        assert row["reason"].startswith(f"{failure}; ")
        assert [(r["id"], r["status"]) for r in row["servers"]] == [("plex-1", "failed")]
        assert self._released(engine)


# Stands in for ffmpeg: notes its argv, then decodes until it is killed (luma frames for a rawvideo decode, nothing for
# a fingerprint). With the stop file there it fails at once, so a test's teardown can end whatever still runs.
_FAKE_FFMPEG = """#!{python}
import os, sys, time
here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, "started.log"), "a") as log:
    log.write(str(os.getpid()) + " " + " ".join(sys.argv[1:]) + "\\n")
if os.path.exists(os.path.join(here, "stop")):
    sys.exit(1)
if "rawvideo" in sys.argv:
    # Black NV12 frames: luma 0, chroma 128 (all-zero chroma is a GPU decode that lost the colours).
    frame = bytes({frame_bytes}) + bytes([128]) * ({frame_bytes} // 2)
    while True:
        sys.stdout.buffer.write(frame)
        sys.stdout.buffer.flush()
time.sleep(60)  # the test kills the process; this only bounds a leak
"""


def _running(pid: int) -> bool:
    """Whether a process is alive (a zombie isn't: it has stopped decoding)."""
    try:
        with open(f"/proc/{pid}/stat") as stat:
            return stat.read().rsplit(")", 1)[1].split()[0] != "Z"
    except (FileNotFoundError, ProcessLookupError):
        return False


class _Hold:
    """Holds the first thread that reaches it, once armed, until the test releases it: a step that doesn't look at the
    job's cancel meanwhile (an ffprobe, the worker picking the file up, a text detection helper starting)."""

    def __init__(self) -> None:
        self.armed = threading.Event()
        self.reached = threading.Event()
        self.released = threading.Event()
        self._lock = threading.Lock()

    def __call__(self) -> None:
        if not self.armed.is_set():
            return
        with self._lock:
            if self.reached.is_set():
                return
            self.reached.set()
        self.released.wait(30)


class TestACancelStopsTheFileWheneverItLands:
    """A cancel stops the file's work whenever it lands. The job's own thread ends as soon
    as the dispatcher lets go of a cancelled job, and its teardown clears the job's cancel flag while the file may still
    be running: a step that didn't look at the cancel in those milliseconds (a text detection helper starting on a fresh
    app, an ffprobe, the worker picking the file up) must still see it afterwards.

    Every cell holds the file at one step, lands the cancel as ``POST /api/jobs/<id>/cancel`` does, waits for the job's
    thread to end (the flag is gone from then on), and lets the file go on. Real runner, gate, dispatcher, worker,
    pipeline and detectors; only ffprobe, the server, text detection and the ffmpeg binary (a script that decodes until
    it is killed) are faked."""

    SOURCES = ("chapters", "theintrodb", "introdb", "skipdb", "season_audio", "credits_text", "server_markers")

    @pytest.fixture
    def lab(self, engine, tmp_path, monkeypatch):
        from media_preview_generator.markers.audio import end_picture, season
        from media_preview_generator.markers.credits import detector, frames
        from media_preview_generator.markers.credits.textdet_helper import TextDetState
        from media_preview_generator.markers.pipeline import default_local_detectors
        from media_preview_generator.markers.probe import StreamStarts

        folder = tmp_path / "tv" / "Rick and Morty (2013) {tvdb-275274}" / "Season 01"
        folder.mkdir(parents=True)
        episodes = []
        for number in (1, 2, 3):
            media = folder / f"Rick and Morty (2013) - S01E{number:02d}.mkv"
            media.write_bytes(b"x" * (100 + number))
            episodes.append(str(media))
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        ffmpeg = bin_dir / "ffmpeg"
        ffmpeg.write_text(_FAKE_FFMPEG.format(python=sys.executable, frame_bytes=frames.FRAME_W * frames.FRAME_H))
        ffmpeg.chmod(0o755)
        db_path = str(tmp_path / "markers.db")
        store = MarkerStore(db_path)
        registry = FakeRegistry({"plex-1": server_config("plex-1", ServerType.PLEX, root=str(tmp_path / "tv"))})
        # One GPU worker and no CPU worker, as the lab's app.
        config = SimpleNamespace(cpu_threads=0, gpu_threads=1, scan_workers=4, ffmpeg_path=str(ffmpeg))
        lab = SimpleNamespace(
            episodes=episodes, store=store, publisher=ready_publisher(), source="credits_text", hold=_Hold(),
            hold_at="", picked_up=[],
        )  # fmt: skip

        def build_context(*, registry, config, priority, force=False, **_kwargs):
            raw = {
                "detect": {"intro": lab.source == "season_audio", "credits": lab.source == "credits_text"},
                "sources": [{"id": source, "enabled": source == lab.source} for source in self.SOURCES],
            }
            settings = load_global(validate_global(raw, None)[0])
            return PipelineContext(
                registry=registry, config=config, settings=settings, store=store, priority=priority, ffprobe="ffprobe",
                force=force, clients={}, credits_text=TextDetState.AVAILABLE, live_config=registry.get_config,
                local_detectors=default_local_detectors(settings, config, credits_text=TextDetState.AVAILABLE),
            )  # fmt: skip

        def held_at(step):
            if lab.hold_at == step:
                lab.hold()

        def probe(path, **_kwargs):
            held_at("ffprobe")
            return MediaProbe(DURATION, ())

        real_log_pickup = pipeline.log_pickup

        def log_pickup(item, worker, *, ctx):
            lab.picked_up.append(item.canonical_path)
            held_at("pickup")
            real_log_pickup(item, worker, ctx=ctx)

        def detect_boxes(planes, **_kwargs):
            held_at("text detection")
            return [()] * len(planes)

        def read_text(planes, **_kwargs):
            held_at("card read")
            return [["The investigation is now closed."]] * len(planes)

        def frozen():
            held_at("decode poll")
            return False

        pool = MagicMock()
        pool.detect_boxes.side_effect = detect_boxes
        pool.read_text.side_effect = read_text
        monkeypatch.setattr(job_runner, "load_config", lambda: config)
        monkeypatch.setattr(
            job_runner, "_build_selected_gpus", lambda s, **kw: [("NVIDIA", "cuda:0", {"name": "Test GPU"})]
        )
        monkeypatch.setattr(job_runner, "_build_multi_server_registry", lambda: registry)
        monkeypatch.setattr(job_runner, "build_context", build_context)
        monkeypatch.setattr(job_runner, "run_detector_checks", lambda *args, **kwargs: None)
        # Every decode loop asks the job's freeze check once per poll: a poll held there hasn't looked at the cancel.
        monkeypatch.setattr(job_runner, "job_freeze_check", lambda jm, job_id: frozen)
        monkeypatch.setattr(triggers, "start_intro_credits_job_async", lambda job_id: None)
        monkeypatch.setattr(pipeline, "log_pickup", log_pickup)
        monkeypatch.setattr(pipeline, "probe_media", probe)
        monkeypatch.setattr(pipeline, "publisher_for", lambda *args, **kwargs: lab.publisher)
        monkeypatch.setattr(season, "probe_media", lambda path, **kwargs: MediaProbe(DURATION, ()))
        monkeypatch.setattr(season, "chromaprint_ffmpeg", lambda configured: configured)
        monkeypatch.setattr(end_picture, "stream_starts", lambda path, **kwargs: StreamStarts(0.0, 0.0))
        monkeypatch.setattr(frames, "container_start_s", lambda *args, **kwargs: 0.0)
        monkeypatch.setattr(frames, "keyframe_thinning", lambda *args, **kwargs: frames.KeyframeThinning())
        monkeypatch.setattr(detector, "get_textdet_pool", lambda: pool)

        def started() -> list[tuple[int, str]]:
            try:
                lines = (bin_dir / "started.log").read_text().splitlines()
            except FileNotFoundError:
                return []
            return [(int(pid), argv) for pid, _, argv in (line.partition(" ") for line in lines)]

        def snapshot() -> list[str]:
            db = sqlite3.connect(db_path)
            try:
                return list(db.iterdump())
            finally:
                db.close()

        lab.started, lab.snapshot = started, snapshot
        yield lab
        # Whatever a failing cell left running: nothing new decodes, what runs is killed, and the worker is let go.
        lab.hold.released.set()
        (bin_dir / "stop").touch()
        for pid, _argv in started():
            if _running(pid):
                os.kill(pid, signal.SIGKILL)
        wait_for(lambda: not TestCreditTextOnTheWorkers._worker().is_busy, timeout=15)
        store.close()

    @staticmethod
    def _cancel_while_held(engine, lab, paths, *, hold_at, arm_on=None, before_cancel=None, before_release=None):
        """Run a job on its own thread, hold the file at ``hold_at``, land the cancel, and let the file go on once the
        job's thread has ended (``before_release`` runs just before, with the file still held). Returns the job and the
        markers database as it was when the cancel landed."""
        lab.hold_at = hold_at
        if arm_on is None:
            lab.hold.armed.set()
        job = triggers.create_intro_credits_job(library_name="TV", priority=2, source="manual", file_paths=paths)
        runner = threading.Thread(target=job_runner.run_intro_credits_job, args=(job.id,), daemon=True)
        runner.start()
        if arm_on is not None:
            assert wait_for(lambda: any(arm_on in argv for _pid, argv in lab.started())), lab.started()
            lab.hold.armed.set()
        assert lab.hold.reached.wait(10), f"the file never reached the {hold_at}"
        if before_cancel is not None:
            before_cancel(job)
        stored = lab.snapshot()
        # POST /api/jobs/<id>/cancel
        engine.jm.request_cancellation(job.id)
        engine.jm.add_log(job.id, "WARNING - Cancellation requested by user")
        engine.jm.cancel_job(job.id)
        runner.join(10)
        assert not runner.is_alive(), "the job's thread didn't end after the cancel"
        # The teardown cleared the job's cancel flag: from here on the file sees only what the dispatcher kept.
        assert engine.jm.is_cancellation_requested(job.id) is False
        if before_release is not None:
            before_release()
        lab.hold.released.set()
        return engine.jm.get_job(job.id), stored

    @staticmethod
    def _assert_stopped_with_nothing_kept(engine, lab, job, stored, *, decodes):
        worker = TestCreditTextOnTheWorkers._worker()
        assert wait_for(lambda: not worker.is_busy, timeout=5), f"the file ran on after the cancel: {lab.started()}"
        assert len(lab.started()) == decodes, lab.started()
        assert not [pid for pid, _argv in lab.started() if _running(pid)], "a decode outlived the cancel"
        assert lab.snapshot() == stored  # no answer, fingerprint, end picture or decision stored after the cancel
        assert lab.publisher.write.call_count == 0
        assert job.status is JobStatus.CANCELLED
        assert engine.gate.snapshot()[0] == 0

    def test_a_file_waiting_for_a_worker_when_the_cancel_lands_is_never_picked_up(self, engine, lab):
        # The only worker is held picking up the first episode while the second waits in the queue for it.
        from media_preview_generator.jobs.dispatcher import get_dispatcher

        # The two files are checked side by side, so either can reach the worker first.
        first, second, _third = lab.episodes
        queued: list[str] = []

        def other_queued(job):
            tracker = get_dispatcher()._trackers[job.id]
            assert wait_for(lambda: len(tracker.item_queue) == 1)
            queued.extend(item.canonical_path for item in tracker.item_queue)

        job, stored = self._cancel_while_held(engine, lab, [first, second], hold_at="pickup",
                                              before_cancel=other_queued)  # fmt: skip
        self._assert_stopped_with_nothing_kept(engine, lab, job, stored, decodes=0)
        assert len(queued) == 1 and lab.picked_up == [({first, second} - set(queued)).pop()]

    def test_a_cancel_as_the_worker_picks_the_file_up_starts_no_decode(self, engine, lab):
        job, stored = self._cancel_while_held(engine, lab, lab.episodes[:1], hold_at="pickup")
        self._assert_stopped_with_nothing_kept(engine, lab, job, stored, decodes=0)

    def test_a_cancel_during_the_chapters_step_stores_nothing_and_hands_no_worker_the_file(self, engine, lab):
        # The checking stage's ffprobe of the file: it runs before any worker has it.
        job, stored = self._cancel_while_held(engine, lab, lab.episodes[:1], hold_at="ffprobe")
        self._assert_stopped_with_nothing_kept(engine, lab, job, stored, decodes=0)
        assert lab.picked_up == []
        assert lab.store.get_file(lab.episodes[0]) is None

    @staticmethod
    def _decode_ends_while_held(lab):
        # The request is still busy (a helper starting takes 10-14 s): the decode must already be gone, not wait for it.
        def check():
            assert wait_for(lambda: not [pid for pid, _argv in lab.started() if _running(pid)], timeout=3), (
                "the decode ran on while text detection was busy"
            )

        return check

    def test_a_cancel_while_a_fresh_apps_text_detection_helper_starts_stops_the_credit_text_decode(self, engine, lab):
        # The first text detection request of a fresh app waits for its helper to start and self-test (10-14 s): the
        # the cancel lands there, just after the decode appears, and ffmpeg stops then, not after the request.
        job, stored = self._cancel_while_held(engine, lab, lab.episodes[:1], hold_at="text detection",
                                              before_release=self._decode_ends_while_held(lab))  # fmt: skip
        self._assert_stopped_with_nothing_kept(engine, lab, job, stored, decodes=1)
        assert "rawvideo" in lab.started()[0][1]

    def test_a_cancel_during_the_card_read_stops_its_decode_and_stores_nothing(self, engine, lab, monkeypatch):
        # Credit text v8 reads the card the credits start lands on: a full-size one-second decode whose frame goes to
        # the helper's reader. The roll's own readings are stood in for (a start on a dark card, and the card's 1 fps
        # rows), so the file's first decode is the card read, held in the reader when the cancel lands.
        from media_preview_generator.markers.credits import detector, frames

        start_s = DURATION / 1000 - 100
        card = ((30, 70, 290, 80),)
        found = detector.CreditsTextResult(start_s, None, ((start_s, 1, 12.0, card),), (), (), ())
        monkeypatch.setattr(detector, "_the_roll", lambda path, read: found)
        monkeypatch.setattr(frames, "decode_rows", lambda path, **kwargs: [
            (start_s + t, 1, 12.0, card) for t in range(5)
        ])  # fmt: skip
        job, stored = self._cancel_while_held(engine, lab, lab.episodes[:1], hold_at="card read",
                                              before_release=self._decode_ends_while_held(lab))  # fmt: skip
        self._assert_stopped_with_nothing_kept(engine, lab, job, stored, decodes=1)
        (read,) = [argv for _pid, argv in lab.started()]
        assert f"-ss {start_s + 2:.3f} -t 1.000" in read and "scale=1280:720" in read  # the card's read, full size

    def test_a_cancel_during_the_season_audio_fingerprint_stops_it(self, engine, lab):
        lab.source = "season_audio"
        job, stored = self._cancel_while_held(engine, lab, lab.episodes[:1], hold_at="decode poll",
                                              arm_on="chromaprint")  # fmt: skip
        self._assert_stopped_with_nothing_kept(engine, lab, job, stored, decodes=1)

    def test_a_cancel_during_the_end_picture_decode_stops_it(self, engine, lab, monkeypatch):
        # The season's fingerprints match on an intro in the first 30 s, so its end picture is decoded.
        from media_preview_generator.markers.audio import fingerprint
        from tests.markers.audio.test_season import early_points

        monkeypatch.setattr(fingerprint, "compute_fingerprint", lambda path, duration_ms, **kwargs: early_points(path))
        lab.source = "season_audio"
        job, stored = self._cancel_while_held(engine, lab, lab.episodes[:1], hold_at="decode poll", arm_on="rawvideo")
        self._assert_stopped_with_nothing_kept(engine, lab, job, stored, decodes=1)


class TestStopDuringAFile:
    def test_a_file_killed_by_the_stop_is_not_failed_and_its_job_is_revived(self, engine, monkeypatch):
        def check_fn(item, *, cancel_check=None):
            shutdown.request_shutdown()  # the stop signal reaches the app; the file's FFmpeg exits 255 with it
            return ItemOutcome("failed", "ffmpeg exited 255 decoding a.mkv: Exiting normally, received signal 15", [])

        handlers = KindHandlers(
            check_fn=check_fn,
            process_fn=lambda item, **kw: ItemOutcome("failed", "unexpected worker stage"),
            outcome_keys=("markers_published", "failed"),
        )
        monkeypatch.setattr(job_runner, "_build_multi_server_registry", lambda: MagicMock())
        monkeypatch.setattr(
            job_runner,
            "build_context",
            lambda **kw: MagicMock(decided_by=DecidedByTally(), **{"take_missing.return_value": 0}),
        )
        monkeypatch.setattr(job_runner, "kind_handlers", lambda ctx: handlers)
        monkeypatch.setattr(job_runner, "build_items", lambda cfg, **kw: ([ProcessableItem("/m/a.mkv", "")], [], {}))
        monkeypatch.setattr(triggers, "start_intro_credits_job_async", lambda job_id: None)
        job = triggers.create_intro_credits_job(library_name="stopped", priority=3, source="manual")

        job_runner.run_intro_credits_job(job.id)

        stored = engine.jm.get_job(job.id)
        assert engine.jm.get_file_results(job.id) == []
        assert (stored.status, stored.error, stored.completed_at) == (JobStatus.RUNNING, None, None)
        engine.jm.close()
        shutdown._shutting_down = False
        restored = JobManager(config_dir=engine.jm.config_dir)
        try:
            assert [revived.id for revived in restored.requeue_interrupted_jobs()] == [job.id]
        finally:
            restored.close()
