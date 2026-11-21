"""A restart or a deploy never fails a Reprise analysis: it carries on where it stopped, and never in a useless loop."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from media_preview_generator import shutdown
from media_preview_generator.markers.store import MarkerStore
from media_preview_generator.reprise import analysis, recovery
from media_preview_generator.reprise import service as service_module
from media_preview_generator.reprise.identity import media_version
from media_preview_generator.reprise.models import AnalysisDraft, PlaybackIdentity
from media_preview_generator.reprise.service import RepriseService, _mapping_fingerprint
from media_preview_generator.reprise.store import RepriseStore
from media_preview_generator.servers.registry import ServerRegistry
from media_preview_generator.web import jobs, settings_manager
from media_preview_generator.web.routes import job_runner

pytestmark = pytest.mark.real_job_async

REAL_START = job_runner._start_job_async  # the real runner entry, before the tests replace it with a recorder
NOW = datetime(2026, 11, 21, 9, 0, tzinfo=UTC)
SETTLED = NOW + timedelta(minutes=2)  # the first resume: after the start-up delay
REFUSAL = "Reprise cannot compare this media"


class Plant:
    """The app as it stands between restarts: one config folder and one results store, and a new job manager and
    service for each start (``restart`` makes the next). Analyses are fakes that do what ``behave`` says, so a test
    chooses exactly when the process is "killed" and nothing depends on timing."""

    def __init__(self, tmp_path, monkeypatch):
        self.monkeypatch = monkeypatch
        self.library = tmp_path / "library"
        self.library.mkdir()
        (tmp_path / "config").mkdir()
        self.config_dir = str(tmp_path / "config")
        self.registry = ServerRegistry.from_settings(
            [
                {
                    "id": "server",
                    "name": "Test",
                    "enabled": True,
                    "type": "plex",
                    "url": "http://127.0.0.1:9",
                    "auth": {},
                    "server_identity": "machine",
                    "libraries": [
                        {"id": "library", "name": "TV", "enabled": True, "remote_paths": [str(self.library)]}
                    ],
                }
            ]
        )
        self.settings = {}
        self.app_settings = SimpleNamespace(get=lambda key, default=None: self.settings.get(key, default))
        self.app_settings.processing_paused = False
        self.registry.settings = self.app_settings
        self.markers = MarkerStore(str(tmp_path / "markers.db"))
        self.store = RepriseStore(self.markers)
        self.versions = {}
        self.analyzed = []  # the episodes an analysis ran for, in order
        self.started = []  # the jobs the resume pass (or an owner's Resume) started
        self.blocked = None  # what the media check reports: None, "folders" or "plex"
        self.behaviour = lambda version: "ok"
        monkeypatch.setattr(jobs.JobManager, "_start_activity_timer", lambda manager: None)
        monkeypatch.setattr(settings_manager, "_settings_manager", settings_manager.SettingsManager(self.config_dir))
        monkeypatch.setattr(service_module, "ffmpeg_binary", lambda: "ffmpeg")
        monkeypatch.setattr(service_module, "admit_references", lambda version, refs, store: [])
        monkeypatch.setattr(RepriseService, "_online_lookups", lambda service: None)
        monkeypatch.setattr(analysis, "analyze", self._analyze)
        monkeypatch.setattr(job_runner, "_start_job_async", self.started.append)
        shutdown._shutting_down = False
        self.manager = self.service = None
        self.boot()

    def boot(self):
        self.manager = jobs.JobManager(self.config_dir)
        self.monkeypatch.setattr(jobs, "_job_manager", self.manager)
        self.service = RepriseService(self.store, self.registry, self.manager)
        self.service._resume_blocker = lambda job: self.blocked
        self.monkeypatch.setattr(service_module, "_SERVICE", self.service)

    def restart(self):
        """The app stops, however hard, and starts again; returns the jobs waiting to resume (as start-up does)."""
        self.manager.close()
        shutdown._shutting_down = False
        self.boot()
        self.store.fail_stale_analyses()
        return self.service.recover_after_restart(NOW)

    def close(self):
        shutdown._shutting_down = False
        self.manager.close()
        self.markers.close()

    def add_episodes(self, count):
        cfg = self.registry.get_config("server")
        for number in range(1, count + 1):
            name = f"ep{number}"
            path = self.library / f"{name}.mkv"
            path.write_bytes(name.encode() * 50)
            version = media_version(str(path), 7000, "sig")
            identity = PlaybackIdentity(
                "machine", f"rk-{number}", name, name, f"/part/{name}", path.stat().st_size, 7000, "123"
            )
            metadata = {
                "library_id": "library",
                "remote_part_path": str(path),
                "show_key": "show",
                "show_title": "Community",
                "title": name,
                "season": 1,
                "episode": number,
                "candidates": [{"kind": "intro", "start_ms": 1000, "end_ms": 6000, "source": "fixture"}],
                "mapping_fingerprint": _mapping_fingerprint(cfg, cfg.libraries[0]),
            }
            self.store.register_media(version, identity, server_id="server", metadata=metadata)
            self.versions[name] = version
        return [version.id for version in self.versions.values()]

    def behave(self, default="ok", **by_name):
        """What each episode's analysis does: ``ok``, ``refuse``, ``crash`` (the process is killed), or a stop that
        arrives while it is read (``stop-failed``: ffmpeg died with the app; ``stop-complete``: it just finished)."""
        by_id = {self.versions[name].id: what for name, what in by_name.items()}
        self.behaviour = lambda version: by_id.get(version.id, default)

    def _analyze(self, version, *args, **kwargs):
        self.analyzed.append(version.id)
        what = self.behaviour(version)
        if what == "crash":
            raise SystemExit("the process was killed")  # no Exception: nothing in the run can catch or record it
        if what == "refuse":
            return AnalysisDraft((), (), "failed", (REFUSAL,))
        if what in ("stop-failed", "stop-complete"):
            shutdown.request_shutdown()
            return (
                AnalysisDraft((), (), "complete", ())
                if what == "stop-complete"
                else AnalysisDraft((), (), "failed", ("Media analysis failed",))
            )
        return AnalysisDraft((), (), "complete", ())

    def names(self):
        by_id = {version.id: name for name, version in self.versions.items()}
        return [by_id[media_id] for media_id in self.analyzed]

    def job(self, ids, **config):
        return self.manager.create_job(
            library_name="Reprise analysis",
            kind="reprise",
            config={"media_version_ids": list(ids), "reprise_owner": "owner", **config},
        )

    def run(self, job_id):
        """What the job runner does: the job starts running, then the service works through it."""
        self.manager.start_job(job_id)
        self.service.run_job(job_id)

    def crash(self, job_id):
        with pytest.raises(SystemExit):
            self.run(job_id)

    def queue(self, job_id):
        return recovery.queue_of(self.manager.get_job(job_id).config)

    def info(self, job_id):
        return self.service.get_job(job_id)


@pytest.fixture
def plant(tmp_path, monkeypatch):
    plant = Plant(tmp_path, monkeypatch)
    yield plant
    plant.close()


def resume_in_a_row(plant, job_id, crashes):
    """A job that is resumed ``crashes`` times, each run ending in a hard kill (the first run is the job's own)."""
    plant.crash(job_id)
    for n in range(crashes):
        assert plant.restart() == [job_id]
        assert plant.service.resume_after_restart(SETTLED + timedelta(minutes=3 * n)) == ([job_id], [])
        plant.crash(job_id)


# ── a restart resumes the job ───────────────────────────────────────────────────────────────────────────────────


def test_a_restart_mid_job_resumes_only_what_is_left_and_never_reads_as_failed(plant):
    ids = plant.add_episodes(5)
    job = plant.job(ids)
    plant.behave(ep3="crash")
    plant.crash(job.id)
    assert plant.names() == ["ep1", "ep2", "ep3"]

    assert plant.restart() == [job.id]
    waiting = plant.info(job.id)
    assert waiting["status"] == "pending" and waiting["error"] is None
    assert waiting["recovery"] == {"state": "waiting", "message": recovery.WAITING_MESSAGES[None]}
    assert plant.started == []  # nothing starts before the start-up delay has passed

    assert plant.service.resume_after_restart(SETTLED) == ([job.id], [])
    assert plant.started == [job.id]
    plant.analyzed.clear()
    plant.behave()
    plant.run(job.id)

    assert plant.names() == ["ep3", "ep4", "ep5"]  # the in-flight episode again; the finished two not at all
    done = plant.info(job.id)
    assert done["status"] == "completed" and done["error"] is None and "warning" not in done
    assert done["recovery"] == {"state": "resumed", "message": "Resumed after a restart"}
    assert recovery.counts_of(plant.queue(job.id))[recovery.RESULT] == 5
    assert all(plant.store.get_result(version.id).analysis_status == "complete" for version in plant.versions.values())


def test_an_episode_saved_just_before_the_stop_is_not_analyzed_again(plant):
    ids = plant.add_episodes(3)
    job = plant.job(ids)
    plant.behave(ep3="crash")
    plant.crash(job.id)
    # The stop came after ep2's result was saved but before the queue noted it.
    lagging = {**plant.queue(job.id), "next": 1, "counts": {recovery.RESULT: 1}}
    plant.manager.merge_job_config(job.id, {recovery.QUEUE_KEY: lagging})

    plant.restart()
    plant.service.resume_after_restart(SETTLED)
    plant.analyzed.clear()
    plant.behave()
    plant.run(job.id)

    assert plant.names() == ["ep3"]
    assert recovery.counts_of(plant.queue(job.id))[recovery.RESULT] == 3


def test_a_library_job_resumes_from_its_saved_episode_list(plant):
    ids = plant.add_episodes(3)
    reads = []

    def read_library(service, job_id, request, pause_check, **kwargs):
        reads.append(job_id)
        return list(ids)

    plant.monkeypatch.setattr(RepriseService, "_run_refresh", read_library)
    request = {"server_id": "server", "library_id": "library"}
    job = plant.manager.create_job(
        library_name="Reprise analysis",
        kind="reprise",
        config={"library_analysis_request": request, "reprise_owner": "owner"},
    )
    plant.behave(ep2="crash")
    plant.crash(job.id)
    assert plant.queue(job.id)["ids"] == ids and reads == [job.id]

    plant.restart()
    plant.service.resume_after_restart(SETTLED)
    plant.analyzed.clear()
    plant.behave()
    plant.run(job.id)

    assert reads == [job.id]  # the library was not read again
    assert plant.names() == ["ep2", "ep3"]
    assert plant.info(job.id)["status"] == "completed"


def test_a_job_the_owner_cancelled_is_never_resumed(plant):
    ids = plant.add_episodes(3)
    job = plant.job(ids)
    plant.behave(ep2="crash")
    plant.crash(job.id)
    plant.manager.cancel_job(job.id)

    assert plant.restart() == []
    assert plant.service.resume_after_restart(SETTLED) == ([], [])
    assert plant.started == []
    cancelled = plant.info(job.id)
    assert cancelled["status"] == "cancelled" and "recovery" not in cancelled


def test_a_job_the_owner_paused_stays_paused_across_a_restart(plant):
    ids = plant.add_episodes(3)
    job = plant.job(ids)
    plant.behave(ep2="crash")
    plant.crash(job.id)
    plant.manager.request_pause(job.id)

    assert plant.restart() == []
    assert plant.service.resume_after_restart(SETTLED) == ([], [])
    held = plant.info(job.id)
    assert held["status"] == "pending" and held["pause_requested"] and "recovery" not in held

    plant.behave()
    plant.service.control_job(job.id, "resume")  # the owner's own Resume carries on from the saved queue
    assert plant.started == [job.id]
    plant.analyzed.clear()
    plant.run(job.id)
    assert plant.names() == ["ep2", "ep3"] and plant.info(job.id)["status"] == "completed"


def test_a_preview_cut_off_by_a_restart_is_given_up_with_a_plain_reason(plant):
    spec = {"media_version_id": "x", "audio_stream_id": "1", "start_ms": 0, "end_ms": 10}
    job = plant.manager.create_job(
        library_name="Reprise preview", kind="reprise", config={"preview_request": spec, "reprise_owner": "owner"}
    )
    plant.manager.start_job(job.id)

    assert plant.restart() == []
    given_up = plant.manager.get_job(job.id)
    assert given_up.status is jobs.JobStatus.FAILED and given_up.error == recovery.PREVIEW_INTERRUPTED


def test_a_job_listing_does_not_carry_a_whole_librarys_episode_list(plant):
    request = {"server_id": "server", "library_id": "library"}
    job = plant.manager.create_job(
        library_name="Reprise analysis",
        kind="reprise",
        config={"library_analysis_request": request, "reprise_queue": {"ids": ["a", "b"], "next": 1}},
    )
    assert job.to_dict()["config"]["reprise_queue"] == {"next": 1}
    assert plant.manager.get_job(job.id).config["reprise_queue"]["ids"] == ["a", "b"]  # kept for the resume


def test_a_waiting_job_cannot_be_started_by_anything_but_the_resume_pass(plant):
    ids = plant.add_episodes(2)
    job = plant.job(ids)
    plant.behave(ep1="crash")
    plant.crash(job.id)
    assert plant.restart() == [job.id]

    REAL_START(job.id)

    assert plant.manager.get_job(job.id).status is jobs.JobStatus.PENDING
    with job_runner._inflight_lock:
        assert job.id not in job_runner._inflight_jobs


# ── no useless loops ────────────────────────────────────────────────────────────────────────────────────────────


def test_an_episode_interrupted_twice_is_set_aside_and_the_job_moves_on(plant):
    ids = plant.add_episodes(3)
    job = plant.job(ids)
    plant.behave(ep2="crash")
    resume_in_a_row(plant, job.id, crashes=1)  # ep2 was cut off twice: once by each run
    assert plant.names().count("ep2") == 2

    assert plant.restart() == [job.id]
    assert plant.service.resume_after_restart(SETTLED + timedelta(minutes=9)) == ([job.id], [])
    plant.analyzed.clear()
    plant.run(job.id)

    assert plant.names() == ["ep3"]  # ep2 is not tried a third time (it would crash again); the rest goes on
    done = plant.info(job.id)
    assert done["status"] == "completed" and done["error"] is None
    assert done["warning"] == "1 episode was set aside after 2 interrupted attempts. Analyze it again to retry."
    assert done["analysis"]["skipped_items"] == [
        {"episode": "Community S01E02 ep2", "reason": recovery.SET_ASIDE_REASON}
    ]
    assert recovery.SET_ASIDE_REASON == "Skipped after 2 interrupted attempts — analyze it again to retry"
    per_media = {entry["id"]: entry for entry in done["outcome"]["per_media"]}
    assert per_media[ids[1]]["state"] == "set_aside" and per_media[ids[1]]["message"] == recovery.SET_ASIDE_REASON
    assert "set aside" in done["outcome"]["message"]
    counts = recovery.counts_of(plant.queue(job.id))
    assert counts == {recovery.RESULT: 2, recovery.FILE_LEFT_OUT: 0, recovery.REFUSED: 0, recovery.SET_ASIDE: 1}
    assert plant.store.get_result(ids[1]).analysis_status == "cancelled"
    assert recovery.SET_ASIDE_REASON in str(plant.store.get_result(ids[1]))


def test_a_restart_loop_pauses_for_the_owner_instead_of_running_forever(plant):
    ids = plant.add_episodes(3)
    job = plant.job(ids)
    plant.behave(default="crash")  # every run dies before it finishes an episode
    resume_in_a_row(plant, job.id, crashes=recovery.LOOP_RESUMES)

    assert plant.restart() == [job.id]
    assert plant.service.resume_after_restart(SETTLED + timedelta(minutes=10)) == ([], [])
    assert plant.started == [job.id] * recovery.LOOP_RESUMES
    paused = plant.info(job.id)
    assert paused["recovery"] == {
        "state": "paused",
        "reason": "kept_stopping",
        "message": "Analysis paused: it kept stopping. Resume when ready.",
    }
    assert paused["status"] == "pending" and paused["pause_requested"] and paused["error"] is None
    ran = len(plant.analyzed)

    # Nothing runs by itself afterwards, however often the pass looks again.
    assert plant.service.resume_after_restart(SETTLED + timedelta(hours=3)) == ([], [])
    assert len(plant.analyzed) == ran and len(plant.started) == recovery.LOOP_RESUMES

    plant.behave()
    plant.service.control_job(job.id, "resume")  # only the owner restarts it, and the guard starts afresh
    assert len(plant.started) == recovery.LOOP_RESUMES + 1
    assert plant.info(job.id)["recovery"]["state"] == "resumed"
    assert plant.queue(job.id)["resumes"] == []
    plant.analyzed.clear()
    plant.run(job.id)
    assert plant.info(job.id)["status"] == "completed"
    assert plant.names() == ["ep3"]  # ep1 and ep2 had used up their attempts


def test_resumes_that_finish_episodes_or_are_long_past_do_not_count_as_a_loop():
    config = {"media_version_ids": ["a", "b", "c"]}
    at = NOW
    stuck = {"resumes": [{"at": (at + timedelta(minutes=n)).isoformat(), "completed": 0} for n in range(3)]}
    assert recovery.verdict(stuck, config, at + timedelta(minutes=5)) == "kept_stopping"
    assert recovery.verdict(stuck, config, at + timedelta(minutes=30)) is None  # all older than the window
    progressed = {**stuck, "counts": {recovery.RESULT: 1}}
    assert recovery.verdict(progressed, config, at + timedelta(minutes=5)) is None  # an episode finished since
    assert recovery.verdict({"resumes": stuck["resumes"][:2]}, config, at + timedelta(minutes=5)) is None


def test_a_stop_given_back_leaves_the_attempt_count_where_it_was():
    started = recovery.begin(recovery.begin({}, "a"), "b")
    assert recovery.attempts_of(started, "a") == 1 and started["running"] == "b"
    given = recovery.give_back(started, "b")
    assert recovery.attempts_of(given, "b") == 0 and given["running"] is None
    assert recovery.give_back(given, "b") == given  # asking twice changes nothing
    assert not recovery.exhausted(recovery.begin(given, "b"), "b")
    assert recovery.exhausted(recovery.begin(recovery.begin(given, "b"), "b"), "b")


# ── unavailable media is not an attempt ────────────────────────────────────────────────────────────────────────


def test_media_that_is_not_there_yet_is_waited_for_and_never_counted(plant):
    ids = plant.add_episodes(3)
    job = plant.job(ids)
    plant.behave(ep2="crash")
    plant.crash(job.id)
    before = plant.queue(job.id)["attempts"]
    plant.restart()

    plant.blocked = "folders"
    assert plant.service.resume_after_restart(SETTLED) == ([], [job.id])
    waiting = plant.info(job.id)
    assert waiting["recovery"] == {"state": "waiting", "message": recovery.WAITING_MESSAGES["folders"]}
    plant.blocked = "plex"
    assert plant.service.resume_after_restart(SETTLED + timedelta(minutes=10)) == ([], [job.id])
    assert plant.info(job.id)["recovery"]["message"] == recovery.WAITING_MESSAGES["plex"]
    assert plant.started == []
    assert not plant.queue(job.id).get("resumes") and plant.queue(job.id)["attempts"] == before

    plant.blocked = None
    assert plant.service.resume_after_restart(SETTLED + timedelta(minutes=15)) == ([job.id], [])
    assert len(plant.queue(job.id)["resumes"]) == 1 and plant.queue(job.id)["attempts"] == before


def test_media_that_never_comes_back_pauses_for_the_owner_without_counting(plant):
    ids = plant.add_episodes(2)
    job = plant.job(ids)
    plant.behave(ep1="crash")
    plant.crash(job.id)
    plant.restart()
    plant.blocked = "folders"

    late = NOW + recovery.WAIT_LIMIT + timedelta(minutes=1)
    assert plant.service.resume_after_restart(late) == ([], [])
    paused = plant.info(job.id)
    assert paused["recovery"]["state"] == "paused" and paused["recovery"]["reason"] == "media_unavailable"
    assert paused["pause_requested"] and plant.started == []
    assert not plant.queue(job.id).get("resumes")


def test_the_media_check_notices_plex_and_empty_folders(plant):
    ids = plant.add_episodes(1)
    job = plant.job(ids)
    service = RepriseService(plant.store, plant.registry, plant.manager)  # the real check, not the test's stand-in
    assert service._resume_blocker(plant.manager.get_job(job.id)) == "plex"  # nothing listens where Plex should be

    server = SimpleNamespace(test_connection=lambda: SimpleNamespace(ok=True))
    plant.monkeypatch.setitem(plant.registry._servers, "server", server)
    assert service._resume_blocker(plant.manager.get_job(job.id)) is None
    for path in plant.library.iterdir():
        path.unlink()
    assert service._resume_blocker(plant.manager.get_job(job.id)) == "folders"  # not mounted yet, or empty


def test_a_global_pause_keeps_a_resumed_job_waiting(plant):
    ids = plant.add_episodes(2)
    job = plant.job(ids)
    plant.behave(ep1="crash")
    plant.crash(job.id)
    plant.restart()
    plant.app_settings.processing_paused = True
    assert plant.service.resume_after_restart(SETTLED) == ([], [job.id])
    plant.app_settings.processing_paused = False
    assert plant.service.resume_after_restart(SETTLED) == ([job.id], [])


def test_with_automatic_resume_switched_off_the_job_waits_for_its_owner(plant):
    ids = plant.add_episodes(2)
    job = plant.job(ids)
    plant.behave(ep1="crash")
    plant.crash(job.id)
    plant.settings["reprise_auto_resume"] = False

    assert plant.restart() == []
    assert plant.service.resume_after_restart(SETTLED) == ([], [])
    held = plant.info(job.id)
    assert held["recovery"]["state"] == "paused" and held["recovery"]["reason"] == "switched_off"
    assert held["recovery"]["message"] == recovery.PAUSED_MESSAGES["switched_off"] and plant.started == []
    plant.behave()
    plant.service.control_job(job.id, "resume")
    assert plant.started == [job.id]


# ── deploys: a stop is a checkpoint, not a failure ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("behaviour", ["stop-failed", "stop-complete"])
def test_a_stop_marks_the_episode_interrupted_not_failed_and_the_next_start_resumes_it(plant, behaviour):
    ids = plant.add_episodes(3)
    job = plant.job(ids)
    plant.behave(ep2=behaviour)
    plant.run(job.id)  # returns quietly: a stop is not an error

    assert plant.names() == ["ep1", "ep2"]  # ep3 never started
    live = plant.manager.get_job(job.id)
    assert live.status is jobs.JobStatus.RUNNING and live.error is None
    interrupted = plant.store.get_result(ids[1])
    assert interrupted.analysis_status == "cancelled"  # not "failed", and nothing partial was saved
    queue = plant.queue(job.id)
    assert queue["running"] is None and recovery.attempts_of(queue, ids[1]) == 0
    plant.service.checkpoint_for_shutdown()  # what the stop's hook does: nothing is left to give back
    assert plant.queue(job.id) == queue

    assert plant.restart() == [job.id]
    plant.service.resume_after_restart(SETTLED)
    plant.analyzed.clear()
    plant.behave()
    plant.run(job.id)
    assert plant.names() == ["ep2", "ep3"]
    assert plant.info(job.id)["status"] == "completed"


def test_stops_never_count_toward_setting_an_episode_aside(plant):
    ids = plant.add_episodes(2)
    job = plant.job(ids)
    plant.behave(ep2="stop-failed")
    plant.run(job.id)
    for n in range(3):  # three deploys in a row, each during ep2
        assert plant.restart() == [job.id]
        assert plant.service.resume_after_restart(SETTLED + timedelta(minutes=20 * n)) == ([job.id], [])
        plant.run(job.id)
    assert plant.names().count("ep2") == 4

    assert plant.restart() == [job.id]
    plant.service.resume_after_restart(SETTLED + timedelta(minutes=90))
    plant.behave()
    plant.run(job.id)
    done = plant.info(job.id)
    assert done["status"] == "completed" and "warning" not in done  # ep2 was analyzed, not set aside


def test_the_stop_hook_gives_back_an_attempt_the_run_could_not_give_back_itself(plant):
    ids = plant.add_episodes(2)
    job = plant.job(ids)
    plant.behave(ep1="crash")
    plant.crash(job.id)  # the run died without noticing; the queue still says ep1 is being read
    assert plant.queue(job.id)["running"] == ids[0]

    plant.service.checkpoint_for_shutdown()
    queue = plant.queue(job.id)
    assert queue["running"] is None and recovery.attempts_of(queue, ids[0]) == 0
    assert plant.manager.get_job(job.id).status is jobs.JobStatus.RUNNING


# ── scheduled and refresh jobs share the treatment ─────────────────────────────────────────────────────────────


def refresh_job(plant, behaviour):
    def refresh(service, job_id, request, pause_check, **kwargs):
        if behaviour["now"] == "crash":
            raise SystemExit("the process was killed")
        service.job_manager.complete_job(job_id)

    plant.monkeypatch.setattr(RepriseService, "_run_refresh", refresh)
    request = {"server_id": "server", "library_id": "library"}
    return plant.manager.create_job(
        library_name="Reprise refresh", kind="reprise", config={"refresh_request": request, "reprise_owner": "owner"}
    )


def test_an_interrupted_refresh_runs_once_more_and_then_finishes(plant):
    behaviour = {"now": "crash"}
    job = refresh_job(plant, behaviour)
    plant.crash(job.id)

    assert plant.restart() == [job.id]
    assert plant.service.resume_after_restart(SETTLED) == ([job.id], [])
    behaviour["now"] = "ok"
    plant.run(job.id)
    done = plant.info(job.id)
    assert done["status"] == "completed" and done["recovery"]["state"] == "resumed"


def test_a_refresh_that_keeps_stopping_is_not_run_a_third_time_by_itself(plant):
    job = refresh_job(plant, {"now": "crash"})
    plant.crash(job.id)
    assert plant.restart() == [job.id]
    assert plant.service.resume_after_restart(SETTLED) == ([job.id], [])
    plant.crash(job.id)

    assert plant.restart() == [job.id]
    assert plant.service.resume_after_restart(SETTLED + timedelta(minutes=5)) == ([], [])
    assert plant.info(job.id)["recovery"]["reason"] == "kept_stopping" and plant.started == [job.id]


# ── deterministic errors are recorded once ─────────────────────────────────────────────────────────────────────


def test_a_refused_episode_is_recorded_once_and_a_resume_never_retries_it(plant):
    ids = plant.add_episodes(4)
    job = plant.job(ids)
    plant.behave(ep2="refuse", ep3="crash")
    plant.crash(job.id)
    plant.restart()
    plant.service.resume_after_restart(SETTLED)
    plant.analyzed.clear()
    plant.behave()
    plant.run(job.id)

    assert plant.names() == ["ep3", "ep4"]  # ep2 was refused the first time and is left as it was
    done = plant.info(job.id)
    assert done["status"] == "completed"
    assert done["warning"] == "1 episode could not be analyzed (open each to see why)."
    assert done["analysis"]["skipped_items"] == [{"episode": "Community S01E02 ep2", "reason": REFUSAL}]


def test_a_job_whose_every_episode_was_refused_fails_with_the_reason(plant):
    ids = plant.add_episodes(2)
    job = plant.job(ids)
    plant.behave(default="refuse")
    plant.run(job.id)

    assert plant.names() == ["ep1", "ep2"]  # the first refusal did not end the job
    failed = plant.info(job.id)
    assert failed["status"] == "failed" and failed["message"] == REFUSAL
