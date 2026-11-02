"""Scheduled Reprise work uses real discovery, queueing, and persisted results."""

import json
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest
import requests

from media_preview_generator.markers.store import MarkerStore
from media_preview_generator.processing.types import ProcessableItem
from media_preview_generator.reprise.service import RepriseService, set_service
from media_preview_generator.reprise.store import RepriseStore
from media_preview_generator.servers.base import Library, ServerConfig, ServerType
from media_preview_generator.servers.registry import ServerRegistry
from media_preview_generator.web import jobs, scheduler
from media_preview_generator.web.job_gate import reset_job_gate
from media_preview_generator.web.settings_manager import get_settings_manager, reset_settings_manager
from tests.reprise.test_service_bindings import wait_for_queue

pytestmark = pytest.mark.real_job_async


@pytest.fixture
def scheduled_library(tmp_path, monkeypatch):
    paths = [tmp_path / "first.mp4", tmp_path / "second.mp4"]
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=size=32x32:rate=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440",
            "-t",
            "2",
            "-threads",
            "1",
            "-c:v",
            "mpeg4",
            "-c:a",
            "aac",
            str(paths[0]),
        ],
        check=True,
        timeout=20,
    )
    shutil.copyfile(paths[0], paths[1])
    other_dir = tmp_path / "other-library"
    other_dir.mkdir()
    other_path = other_dir / "other.mp4"
    shutil.copyfile(paths[0], other_path)
    files = {"101": paths[0], "102": paths[1], "103": other_path}

    class PlexFixture(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/":
                result = {"machineIdentifier": "machine"}
            elif path == "/library/sections/1/all":
                result = {"totalSize": 2, "Metadata": [{"ratingKey": k} for k in ("101", "102")]}
            elif path == "/library/sections/2/all":
                result = {"totalSize": 1, "Metadata": [{"ratingKey": "103"}]}
            else:
                key = path.rsplit("/", 1)[-1]
                file = files[key]
                result = {
                    "Metadata": [
                        {
                            "ratingKey": key,
                            "title": "Episode " + key,
                            "duration": 2000,
                            "Media": [
                                {
                                    "id": int(key) + 1000,
                                    "duration": 2000,
                                    "Part": [
                                        {
                                            "id": int(key) + 2000,
                                            "file": str(file),
                                            "size": file.stat().st_size,
                                            "key": "/library/parts/" + key + "/file",
                                            "Stream": [{"streamType": 2, "id": int(key) + 3000, "index": 1}],
                                        }
                                    ],
                                }
                            ],
                        }
                    ]
                }
            body = json.dumps({"MediaContainer": result}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    plex = ThreadingHTTPServer(("127.0.0.1", 0), PlexFixture)
    thread = threading.Thread(target=plex.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{plex.server_port}"
    library = Library("1", "TV", (str(tmp_path),), kind="show")
    other_library = Library("2", "Other TV", (str(other_dir),), kind="show")
    config = ServerConfig(
        id="server",
        type=ServerType.PLEX,
        name="Plex",
        enabled=True,
        url=url,
        auth={},
        libraries=[library, other_library],
        server_identity="machine",
    )

    class PlexAdapter:
        def list_libraries(self):
            return [library, other_library]

        def _plex_http(self, method, path, **kwargs):
            response = requests.request(method, url + path, timeout=20, **kwargs)
            response.raise_for_status()
            return response

    registry = ServerRegistry()
    registry._configs["server"] = config
    registry._servers["server"] = PlexAdapter()
    markers = MarkerStore(str(tmp_path / "markers.db"))
    store = RepriseStore(markers)
    reset_settings_manager()
    settings = get_settings_manager(str(tmp_path))
    plex_config = tmp_path / "plex_config"
    (plex_config / "Media" / "localhost").mkdir(parents=True)
    settings.set(
        "media_servers",
        [
            {
                "id": "server",
                "type": "plex",
                "name": "Plex",
                "enabled": True,
                "url": url,
                "auth": {"token": "test-token"},
                "libraries": [
                    {"id": "1", "name": "TV", "remote_paths": [str(tmp_path)], "enabled": True},
                    {"id": "2", "name": "Other TV", "remote_paths": [str(other_dir)], "enabled": True},
                ],
                "output": {"adapter": "plex_bundle", "plex_config_folder": str(plex_config)},
            }
        ],
    )
    settings.set(
        "worker_groups",
        [
            {
                "id": "local",
                "name": "Local",
                "enabled": True,
                "availability": {"mode": "always", "windows": []},
                "members": [
                    {"id": "cpu", "resource": "cpu", "device": None, "count": 1, "job_types": ["reprise", "previews"]}
                ],
            }
        ],
    )
    manager = jobs.JobManager(config_dir=str(tmp_path))
    monkeypatch.setattr(jobs, "_job_manager", manager)
    reset_job_gate()
    service = RepriseService(store, registry, manager)
    set_service(service)
    try:
        yield SimpleNamespace(paths=paths, config=config, store=store, manager=manager, service=service)
    finally:
        wait_for_queue()
        plex.shutdown()
        thread.join()
        plex.server_close()
        manager.close()
        markers.close()
        reset_job_gate()
        reset_settings_manager()
        set_service(None)


def _results(fixture):
    rows = fixture.store.list_media(server_id="server")["items"]
    return {row["metadata"]["remote_part_path"]: fixture.store.get_result(row["media_version"]["id"]) for row in rows}


def test_whole_library_schedule_discovers_and_analyzes(scheduled_library, monkeypatch):
    fixture = scheduled_library
    last_runs = []
    monkeypatch.setattr(scheduler, "get_schedule_manager", lambda: SimpleNamespace(_update_last_run=last_runs.append))
    scheduler.execute_scheduled_job("schedule-1", ["1"], "TV", {"job_kinds": ["reprise"]}, 2, "server")
    wait_for_queue()
    matching = [job for job in fixture.manager.get_all_jobs() if job.kind == "reprise"]
    assert len(matching) == 1
    assert matching[0].parent_schedule_id == "schedule-1"
    assert matching[0].status.value == "completed"
    assert last_runs == ["schedule-1"]
    results = _results(fixture)
    assert set(results) == {str(path) for path in fixture.paths}
    assert all(result is not None and result.analysis_status == "complete" for result in results.values())


def test_recent_followup_only_analyzes_selected_paths_without_pruning(scheduled_library, monkeypatch):
    from media_preview_generator.jobs import orchestrator

    fixture = scheduled_library
    initial = fixture.service.enqueue_refresh("server", "1")
    wait_for_queue()
    assert fixture.service.get_job(initial["id"])["status"] == "completed"
    assert all(result is None for result in _results(fixture).values())
    item = ProcessableItem(str(fixture.paths[0]), "server", library_id="1")
    monkeypatch.setattr(
        orchestrator, "_build_multi_server_registry", lambda: SimpleNamespace(configs=lambda: [fixture.config])
    )
    monkeypatch.setattr(orchestrator, "_remember_item_origins", lambda *args: None)

    def enumerate_recent(candidates, *, raw_items, **kwargs):
        raw_items.append((fixture.config, item))
        return [(fixture.config, item)], []

    monkeypatch.setattr(orchestrator, "_enumerate_items_for_servers", enumerate_recent)
    monkeypatch.setattr(
        scheduler,
        "get_schedule_manager",
        lambda: SimpleNamespace(
            _update_last_run=lambda _: None,
            get_schedule=lambda _: {
                "enabled": True,
                "server_id": "server",
                "library_ids": ["1"],
                "config": {"job_type": "recently_added", "job_kinds": ["reprise"]},
            },
        ),
    )
    scheduler.execute_scheduled_job(
        "schedule-recent",
        ["1"],
        "TV",
        {"job_type": "recently_added", "job_kinds": ["reprise"], "lookback_hours": 1},
        2,
        "server",
    )
    wait_for_queue()
    matching = [job for job in fixture.manager.get_all_jobs() if job.kind == "reprise" and job.id != initial["id"]]
    assert len(matching) == 1
    job = matching[0]
    assert job.parent_schedule_id == "schedule-recent"
    assert job.status.value == "completed"
    results = _results(fixture)
    assert set(results) == {str(path) for path in fixture.paths}
    assert results[str(fixture.paths[0])].analysis_status == "complete"
    assert results[str(fixture.paths[1])] is None


def test_pending_scheduled_analysis_deduplicates_redacts_and_can_be_cancelled(scheduled_library, monkeypatch):
    fixture = scheduled_library
    monkeypatch.setattr("media_preview_generator.web.routes.job_runner._start_job_async", lambda *args: None)
    options = {
        "candidate_paths": [str(fixture.paths[0])],
        "trigger": "recently_added",
        "parent_schedule_id": "schedule-recent",
    }
    first = fixture.service.enqueue_library_analysis("server", "1", **options)
    second = fixture.service.enqueue_library_analysis("server", "1", **options)
    assert first["id"] == second["id"]
    assert first["operation"] == "analysis"
    assert str(fixture.paths[0]) not in json.dumps(fixture.service.get_job(first["id"]))
    assert str(fixture.paths[0]) not in json.dumps(fixture.manager.get_job(first["id"]).to_dict())
    assert fixture.service.control_job(first["id"], "cancel")["status"] == "cancelled"


def test_scheduled_work_persists_private_paths_and_fails_closed_after_started_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs.JobManager, "_start_activity_timer", lambda self: None)
    manager = jobs.JobManager(str(tmp_path))
    request = {"server_id": "server", "library_id": "1", "candidate_paths": ["/private/episode.mkv"]}
    config = {"library_analysis_request": request, "source": "scheduled_recently_added"}
    started = manager.create_job(kind="reprise", config=config, parent_schedule_id="schedule-1")
    waiting = manager.create_job(kind="reprise", config=config, parent_schedule_id="schedule-2")
    manager.start_job(started.id)
    manager.close()

    reopened = jobs.JobManager(str(tmp_path))
    try:
        loaded = reopened.get_job(started.id)
        assert loaded.config["library_analysis_request"]["candidate_paths"] == ["/private/episode.mkv"]
        assert "/private/episode.mkv" not in json.dumps(loaded.to_dict())
        revived = reopened.requeue_interrupted_jobs(max_age_minutes=60)
        assert [job.id for job in revived] == [waiting.id]
        reopened.fail_unrevived_interrupted_jobs("reprise")
        assert reopened.get_job(started.id).status is jobs.JobStatus.FAILED
        assert reopened.get_job(waiting.id).status is jobs.JobStatus.PENDING
    finally:
        reopened.close()


def test_cancel_between_discovery_and_analysis_never_creates_results(scheduled_library, monkeypatch):
    fixture = scheduled_library
    scanned = threading.Event()
    release = threading.Event()
    original_scan = fixture.service._scan_library

    def stop_after_discovery(*args, **kwargs):
        result = original_scan(*args, **kwargs)
        scanned.set()
        assert release.wait(20), "test did not release discovery"
        return result

    monkeypatch.setattr(fixture.service, "_scan_library", stop_after_discovery)
    job = fixture.service.enqueue_library_analysis("server", "1", parent_schedule_id="schedule-cancel")
    try:
        assert scanned.wait(20), "discovery did not finish"
        fixture.service.control_job(job["id"], "cancel")
    finally:
        release.set()
    wait_for_queue()
    assert fixture.service.get_job(job["id"])["status"] == "cancelled"
    assert all(result is None for result in _results(fixture).values())
