"""Binding safety and real queue/SQLite checks using generated audiovisual media."""

import os
import shutil
import threading
import time
from dataclasses import replace

import pytest

from media_preview_generator.markers.store import MarkerStore
from media_preview_generator.reprise.admission import admit_references
from media_preview_generator.reprise import service as service_module
from media_preview_generator.reprise.identity import media_version
from media_preview_generator.reprise.models import DecisionRange, Evidence, PlaybackIdentity
from media_preview_generator.reprise.service import RepriseService, _mapping_fingerprint
from media_preview_generator.reprise.store import RepriseStore
from media_preview_generator.reprise.verification_profile import PROFILE_FINGERPRINT, PROFILE_ID
from media_preview_generator.servers.registry import ServerRegistry
from media_preview_generator.web import jobs, settings_manager
from media_preview_generator.web.routes import job_runner
from tests.reprise.test_analysis_recurrence import make_video

pytestmark = pytest.mark.real_job_async


def test_lookup_is_exact_and_stream_rejects_replaced_file(tmp_path):
    library = tmp_path / "library"
    library.mkdir()
    path = library / "episode.mp4"
    path.write_bytes(b"original")
    version = media_version(str(path), 1000, "{}")
    identity = PlaybackIdentity("machine", "1", "2", "3", "/library/parts/3/file", 8, 1000, "4")
    marker_store = MarkerStore(str(tmp_path / "markers.db"))
    store = RepriseStore(marker_store)
    metadata = {"library_id": "library", "remote_part_path": str(path)}
    registry = ServerRegistry.from_settings(
        [
            {
                "id": "server",
                "name": "Test",
                "enabled": True,
                "type": "plex",
                "url": "http://127.0.0.1:9",
                "auth": {},
                "server_identity": "machine",
                "libraries": [{"id": "library", "name": "Library", "enabled": True, "remote_paths": [str(library)]}],
            }
        ]
    )
    metadata["mapping_fingerprint"] = _mapping_fingerprint(
        registry.get_config("server"), registry.get_config("server").libraries[0]
    )
    store.register_media(version, identity, server_id="server", metadata=metadata)
    service = RepriseService(store, registry, None)
    assert service.lookup(identity)["resolution"] == "resolved"
    assert service.lookup(replace(identity, audio_stream_id="other"))["resolution"] == "unresolved"
    file, _ = service.open_stream(version.id)
    with file:
        assert file.read() == b"original"
    path.write_bytes(b"replaced")
    assert service.lookup(identity)["freshness"] == "stale"
    with pytest.raises(FileNotFoundError):
        service.open_stream(version.id)
    marker_store.close()


def test_mapping_changes_immediately_invalidate_stream_and_publication_eligibility(tmp_path):
    from media_preview_generator.reprise.review import plex_projection

    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "episode.mp4").write_bytes(b"original")
    (b / "episode.mp4").write_bytes(b"original")
    (a / "reference.mp4").write_bytes(b"reference")
    remote = "/plex/tv/episode.mp4"
    version = media_version(str(a / "episode.mp4"), 10000, "{}")
    identity = PlaybackIdentity("machine", "1", "2", "3", "/library/parts/3/file", 8, 10000, "4")
    markers = MarkerStore(str(tmp_path / "markers.db"))
    store = RepriseStore(markers)
    registry = ServerRegistry.from_settings(
        [
            {
                "id": "server",
                "name": "Test",
                "enabled": True,
                "type": "plex",
                "url": "http://127.0.0.1:9",
                "auth": {},
                "server_identity": "machine",
                "libraries": [{"id": "library", "name": "TV", "enabled": True, "remote_paths": ["/plex/tv"]}],
                "path_mappings": [{"remote_prefix": "/plex/tv", "local_prefix": str(a)}],
            }
        ]
    )
    cfg = registry.get_config("server")
    metadata = {
        "library_id": "library",
        "remote_part_path": remote,
        "mapping_fingerprint": _mapping_fingerprint(cfg, cfg.libraries[0]),
        "show_key": "show", "season": 1, "episode": 1,
    }
    store.register_media(version, identity, server_id="server", metadata=metadata)
    reference = media_version(str(a / "reference.mp4"), 10000, "{}")
    ref_identity = replace(identity, rating_key="2", media_id="20", part_id="30",
                           part_key="/library/parts/30/file", part_size_bytes=9)
    ref_metadata = {**metadata, "episode": 2, "remote_part_path": "/plex/tv/reference.mp4"}
    store.register_media(reference, ref_identity, server_id="server", metadata=ref_metadata)
    snapshot = admit_references(version, [reference], store)[0][1]
    service = RepriseService(store, registry, None)
    evidence = Evidence(
        "e", 0, 1000, "audiovisual", "match", "match", "", reference.id, 0, 1000,
        {"checked_audio_stream_ids": ["4"], "admission_provenance": snapshot,
         "verification_profile": PROFILE_ID, "profile_fingerprint": PROFILE_FINGERPRINT}
    )
    proposal = DecisionRange("r", "intro", "skip", 0, 1000, "proposed", False, "detector", "match", "", ("e",), ("4",))
    run = store.begin_analysis(version.id, "job", "signature")
    result = store.finish_analysis(run, (evidence,), (proposal,), version.id)
    result = store.save_review(version.id, result.result_revision, [{"range_id": "r", "review_status": "approved"}], "owner")
    assert service.eligible_ranges(result, identity)
    with service.open_stream(version.id)[0] as source:
        assert source.read() == b"original"
    cfg.path_mappings.append({"remote_prefix": "/plex/tv", "local_prefix": str(b)})
    assert service.lookup(identity)["freshness"] == "stale"
    assert service.list_media()["items"][0]["freshness"] == "stale"
    assert not service.eligible_ranges(result, identity)
    with pytest.raises((ValueError, PermissionError)):
        plex_projection(result, identity, service.eligible_ranges(result, identity))
    with pytest.raises(FileNotFoundError):
        service.open_stream(version.id)
    cfg.path_mappings[:] = [{"remote_prefix": "/plex/tv", "local_prefix": str(b)}]
    assert service.lookup(identity)["freshness"] == "stale"
    assert not service.eligible_ranges(result, identity)
    with pytest.raises(FileNotFoundError):
        service.open_stream(version.id)
    cfg.path_mappings[:] = [
        {"remote_prefix": "/plex/tv", "local_prefix": str(a)},
        {"remote_prefix": "/plex/tv", "local_prefix": str(b)},
    ]
    (b / "episode.mp4").unlink()
    metadata["mapping_fingerprint"] = _mapping_fingerprint(cfg, cfg.libraries[0])
    store.register_media(version, identity, server_id="server", metadata=metadata)
    ref_metadata["mapping_fingerprint"] = metadata["mapping_fingerprint"]
    store.register_media(reference, ref_identity, server_id="server", metadata=ref_metadata)
    assert not service.eligible_ranges(result, identity)
    with service.open_stream(version.id)[0] as source:
        assert source.read() == b"original"
    (b / "episode.mp4").write_bytes(b"original")
    assert service.lookup(identity)["freshness"] == "stale"
    assert not service.eligible_ranges(result, identity)
    with pytest.raises(FileNotFoundError):
        service.open_stream(version.id)
    markers.close()


@pytest.fixture(scope="module")
def synthetic_episode(tmp_path_factory):
    return make_video(tmp_path_factory.mktemp("lifecycle-media") / "source.mkv")


@pytest.fixture
def lifecycle(tmp_path, monkeypatch, synthetic_episode):
    library = tmp_path / "library"
    library.mkdir()
    registry = ServerRegistry.from_settings(
        [
            {
                "id": "server",
                "name": "Test",
                "enabled": True,
                "type": "plex",
                "url": "http://127.0.0.1:9",
                "auth": {},
                "server_identity": "machine",
                "libraries": [{"id": "library", "name": "TV", "enabled": True, "remote_paths": [str(library)]}],
            }
        ]
    )
    markers = MarkerStore(str(tmp_path / "markers.db"))
    store = RepriseStore(markers)
    manager = jobs.JobManager(str(tmp_path / "config"))
    settings = settings_manager.SettingsManager(str(tmp_path / "config"))
    monkeypatch.setattr(jobs, "_job_manager", manager)
    monkeypatch.setattr(settings_manager, "_settings_manager", settings)
    service = RepriseService(store, registry, manager)
    monkeypatch.setattr(service_module, "_SERVICE", service)

    def register(name, rating_key):
        path = library / (name + ".mkv")
        shutil.copyfile(synthetic_episode.canonical_path, path)
        version = media_version(str(path), 7000, synthetic_episode.stream_signature)
        identity = PlaybackIdentity(
            "machine", rating_key, name, name, "/part/" + name, path.stat().st_size, 7000, "123"
        )
        cfg = registry.get_config("server")
        store.register_media(
            version,
            identity,
            server_id="server",
            metadata={
                "library_id": "library",
                "remote_part_path": str(path),
                "show_key": "show",
                "season": 1,
                "episode": int(rating_key.rsplit("-", 1)[-1]),
                "candidates": [
                    {"kind": kind, "start_ms": 1000, "end_ms": 6000, "source": "fixture"}
                    for kind in ("intro", "credits")
                ],
                "mapping_fingerprint": _mapping_fingerprint(cfg, cfg.libraries[0]),
            },
        )
        return version, identity

    yield service, register
    wait_for_queue()
    manager.close()
    markers.close()


def wait_for_queue():
    deadline = time.monotonic() + 30
    while True:
        with job_runner._inflight_lock:
            if not job_runner._inflight_jobs:
                return
        assert time.monotonic() < deadline, "Reprise runner did not stop"
        time.sleep(0.01)


def queued_analysis(service, version):
    job = service.enqueue_analysis([version.id], principal="owner")
    wait_for_queue()
    assert service.get_job(job["id"])["status"] == "completed"
    return job["id"]


def approve_skips(service, version):
    result = service.store.get_result(version.id)
    skips = [r for r in result.ranges if r.action == "skip"]
    assert skips, "real detector did not find matching synthetic content"
    return service.store.save_review(
        version.id, result.result_revision, [{"range_id": r.id, "review_status": "approved"} for r in skips], "owner"
    )


def test_new_reference_replaces_untouched_keeps_and_unchanged_rescan_retains_approval(lifecycle):
    service, register = lifecycle
    target, identity = register("target", "episode-1")
    queued_analysis(service, target)
    assert all(r.action == "keep" for r in service.store.get_result(target.id).ranges)
    register("reference", "episode-2")
    queued_analysis(service, target)
    reviewed = approve_skips(service, target)
    eligible = service.lookup(identity)["eligible_skip_ranges"]
    assert eligible, "obsolete detector keeps still veto freshly verified skips"
    queued_analysis(service, target)
    assert service.store.get_result(target.id).result_revision == reviewed.result_revision
    assert service.lookup(identity)["eligible_skip_ranges"] == eligible


def test_alternate_episode_version_cannot_supply_recurrence(lifecycle):
    service, register = lifecycle
    target, identity = register("target", "episode-1")
    register("alternate", "episode-1")
    queued_analysis(service, target)
    result = service.store.get_result(target.id)
    service.store.save_review(
        target.id,
        result.result_revision,
        [{"range_id": r.id, "review_status": "approved"} for r in result.ranges],
        "owner",
    )
    assert not service.lookup(identity)["eligible_skip_ranges"]
    assert not any(e.reference_version_id for e in result.evidence)

def test_hardlinked_episode_alias_cannot_supply_recurrence(lifecycle):
    service, register = lifecycle
    original, identity = register("target", "episode-1")
    path = original.canonical_path
    alias_path = path + ".hardlink"
    os.link(path, alias_path)
    target = media_version(path, original.duration_ms, original.stream_signature)
    alias = media_version(alias_path, original.duration_ms, original.stream_signature)
    metadata = service.store.get_bindings(original.id)[0][2]
    service.store.register_media(target, identity, server_id="server", metadata=metadata)
    alias_identity = replace(identity, rating_key="episode-2", media_id="alias", part_id="alias", part_key="/part/alias")
    service.store.register_media(alias, alias_identity, server_id="server",
                                 metadata={**metadata, "episode": 2, "remote_part_path": alias_path})
    assert target.id != alias.id and (target.device, target.inode) == (alias.device, alias.inode)
    queued_analysis(service, target)
    result = service.store.get_result(target.id)
    assert not any(e.reference_version_id for e in result.evidence)
    assert not any(r.action == "skip" for r in result.ranges)


@pytest.mark.parametrize("cancel_at", ["analyzer_return", "binding_check"])
def test_late_cancel_never_restores_approved_results(lifecycle, monkeypatch, cancel_at):
    from media_preview_generator.reprise import analysis

    service, register = lifecycle
    target, identity = register("target", "episode-1")
    register("reference", "episode-2")
    queued_analysis(service, target)
    approve_skips(service, target)
    assert service.lookup(identity)["eligible_skip_ranges"]
    returned = threading.Event()
    release = threading.Event()
    real_analyze = analysis.analyze
    real_binding = service._valid_binding
    analyzed = False

    def boundary():
        returned.set()
        assert release.wait(20), "test did not release analysis boundary"

    def analyze(*args, **kwargs):
        nonlocal analyzed
        draft = real_analyze(*args, **kwargs)
        assert draft.status == "complete"
        analyzed = True
        if cancel_at == "analyzer_return":
            boundary()
        return draft

    def binding(*args):
        valid = real_binding(*args)
        if analyzed and cancel_at == "binding_check":
            boundary()
        return valid

    monkeypatch.setattr(analysis, "analyze", analyze)
    monkeypatch.setattr(service, "_valid_binding", binding)
    job = service.enqueue_analysis([target.id], principal="owner")
    try:
        assert returned.wait(20)
        assert service.control_job(job["id"], "cancel")["status"] == "cancelled"
    finally:
        release.set()
    wait_for_queue()
    result = service.store.get_result(target.id)
    assert service.get_job(job["id"])["status"] == "cancelled"
    assert result.analysis_status == "cancelled"
    assert not service.lookup(identity)["eligible_skip_ranges"]
    assert not any(r.action == "skip" and r.review_status == "approved" for r in result.ranges)


@pytest.mark.parametrize("state", ["pending", "completed", "failed", "cancelled"])
def test_unsuccessful_resume_does_not_run_analysis(lifecycle, state):
    service, register = lifecycle
    target, identity = register("target", "episode-1")
    manager = service.job_manager
    job = manager.create_job(
        library_name="Reprise analysis",
        kind="reprise",
        config={"media_version_ids": [target.id], "reprise_owner": "owner"},
    )
    if state == "completed":
        manager.complete_job(job.id)
    elif state == "failed":
        manager.complete_job(job.id, error="Stopped")
    elif state == "cancelled":
        manager.cancel_job(job.id)
        manager.clear_cancellation_flag(job.id)
    before = service.get_job(job.id)
    service.control_job(job.id, "resume")
    wait_for_queue()
    assert service.get_job(job.id) == before
    assert service.store.get_result(target.id) is None
    assert not service.lookup(identity)["eligible_skip_ranges"]


def test_successful_resume_runs_pending_analysis(lifecycle):
    service, register = lifecycle
    target, _ = register("target", "episode-1")
    manager = service.job_manager
    job = manager.create_job(
        library_name="Reprise analysis",
        kind="reprise",
        config={"media_version_ids": [target.id], "reprise_owner": "owner"},
    )
    assert manager.request_pause(job.id)
    service.control_job(job.id, "resume")
    wait_for_queue()
    assert service.get_job(job.id)["status"] == "completed"
    assert service.store.get_result(target.id).analysis_status == "complete"


def test_final_commit_wins_atomically_against_cancel(lifecycle, monkeypatch):
    service, register = lifecycle
    target, identity = register("target", "episode-1")
    register("reference", "episode-2")
    queued_analysis(service, target)
    reviewed = approve_skips(service, target)
    before = service.lookup(identity)["eligible_skip_ranges"]
    assert before
    committing = threading.Event()
    release = threading.Event()
    cancelling = threading.Event()
    real_finish = service.store.finish_analysis
    responses = []
    errors = []

    def finish(*args):
        committing.set()
        assert release.wait(20)
        return real_finish(*args)

    def cancel():
        try:
            # Prove contention before invoking the consumer control path. Its
            # initial status response also reads pause state under this lock;
            # signalling inside cancel_job_if_active would be too late.
            acquired = service.job_manager._lock.acquire(blocking=False)
            if acquired:
                service.job_manager._lock.release()
            assert not acquired, "result commit must hold the cancellation lock"
            cancelling.set()
            responses.append(service.control_job(job["id"], "cancel"))
        except BaseException as exc:
            errors.append(exc)
        finally:
            cancelling.set()

    monkeypatch.setattr(service.store, "finish_analysis", finish)
    job = service.enqueue_analysis([target.id], principal="owner")
    thread = None
    try:
        assert committing.wait(20)
        thread = threading.Thread(target=cancel)
        thread.start()
        assert cancelling.wait(20)
    finally:
        release.set()
        if thread is not None:
            thread.join(20)
        wait_for_queue()
    assert thread is not None and not thread.is_alive()
    assert not errors, errors
    assert responses[0]["status"] == "completed"
    assert service.get_job(job["id"])["status"] == "completed"
    assert service.store.get_result(target.id).result_revision == reviewed.result_revision
    assert service.lookup(identity)["eligible_skip_ranges"] == before


def test_real_preview_queue_completes_and_opens_artifact(lifecycle):
    service, register = lifecycle
    target, _ = register("target", "episode-1")
    job = service.enqueue_preview(target.id, 1000, 2000, "123")
    wait_for_queue()
    assert service.get_job(job["id"])["status"] == "completed"
    with service.open_preview(job["id"])[0] as stream:
        assert b"ftyp" in stream.read(32)
