"""A stop-time cannot miss a Reprise follow-up created concurrently."""

import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from media_preview_generator.jobs.orchestrator import _queue_reprise_follow_ups
from media_preview_generator.processing.types import ProcessableItem
from media_preview_generator.reprise import service as service_module
from media_preview_generator.web import jobs, scheduler, settings_manager
from media_preview_generator.web.routes.job_runner import RECENTLY_ADDED_JOB_SOURCE


def test_stop_during_followup_creation_holds_both_jobs(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs.JobManager, "_start_activity_timer", lambda self: None)
    manager = jobs.JobManager(str(tmp_path))
    monkeypatch.setattr(jobs, "_job_manager", manager)
    monkeypatch.setattr(settings_manager, "get_settings_manager", lambda: SimpleNamespace(processing_paused=False))
    creating = threading.Event()
    release = threading.Event()
    stop_entered = threading.Event()

    class ObservedLock:
        def __init__(self):
            self.lock = threading.Lock()

        def __enter__(self):
            if self.lock.locked():
                stop_entered.set()
            self.lock.acquire()
            return self

        def __exit__(self, *_):
            self.lock.release()

    monkeypatch.setattr(scheduler, "_SCHEDULE_STOP_LOCK", ObservedLock())
    parent = manager.create_job(
        kind="previews", config={"source": RECENTLY_ADDED_JOB_SOURCE}, parent_schedule_id="schedule-1"
    )
    manager.start_job(parent.id)

    class Service:
        def enqueue_library_analysis(self, server_id, library_id, **kwargs):
            creating.set()
            assert release.wait(20), "creation was not released"
            child = manager.create_job(kind="reprise", parent_schedule_id=kwargs["parent_schedule_id"])
            return {"id": child.id}

    def get_schedule_manager():
        return SimpleNamespace(
            get_schedule=lambda _: {
                "name": "TV",
                "stop_time": "06:00",
                "enabled": True,
                "config": {"job_type": "recently_added", "job_kinds": ["reprise"]},
            }
        )

    monkeypatch.setattr(service_module, "get_service", lambda: Service())
    monkeypatch.setattr(scheduler, "get_schedule_manager", get_schedule_manager)
    cfg = SimpleNamespace(id="server", type="plex")
    item = ProcessableItem("/media/episode.mkv", "server", library_id="1")
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            create = pool.submit(_queue_reprise_follow_ups, parent.id, [(cfg, item)])
            try:
                assert creating.wait(20), "follow-up creation did not start"
                stop = pool.submit(scheduler.execute_schedule_stop, "schedule-1")
                assert stop_entered.wait(20), "stop did not start"
            finally:
                release.set()
            (child_id,) = create.result(timeout=20)
            stop.result(timeout=20)
        assert manager.get_job(parent.id).paused
        assert manager.get_job(child_id).paused
        assert manager.get_job(child_id).parent_schedule_id == "schedule-1"
    finally:
        release.set()
        manager.close()
