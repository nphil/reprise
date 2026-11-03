"""Binding safety and real queue/SQLite checks using generated audiovisual media."""

import os
import shutil
import threading
import time
from dataclasses import replace

import pytest

from media_preview_generator.markers.store import MarkerStore
from media_preview_generator.reprise import service as service_module
from media_preview_generator.reprise.admission import admit_references
from media_preview_generator.reprise.analysis import NO_OTHER_EPISODES, OTHER_EPISODES_NOT_READY, UNCOMPARABLE_MEDIA
from media_preview_generator.reprise.identity import is_current, media_version
from media_preview_generator.reprise.models import (
    AnalysisDraft,
    AnalysisRefused,
    DecisionRange,
    Evidence,
    PlaybackIdentity,
    ReviewRefused,
)
from media_preview_generator.reprise.service import CHANGED_ON_DISK, RepriseService, _mapping_fingerprint
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
        "show_key": "show",
        "season": 1,
        "episode": 1,
    }
    store.register_media(version, identity, server_id="server", metadata=metadata)
    reference = media_version(str(a / "reference.mp4"), 10000, "{}")
    ref_identity = replace(
        identity, rating_key="2", media_id="20", part_id="30", part_key="/library/parts/30/file", part_size_bytes=9
    )
    ref_metadata = {**metadata, "episode": 2, "remote_part_path": "/plex/tv/reference.mp4"}
    store.register_media(reference, ref_identity, server_id="server", metadata=ref_metadata)
    snapshot = admit_references(version, [reference], store)[0][1]
    service = RepriseService(store, registry, None)
    evidence = Evidence(
        "e",
        0,
        1000,
        "audiovisual",
        "match",
        "match",
        "",
        reference.id,
        0,
        1000,
        {
            "checked_audio_stream_ids": ["4"],
            "admission_provenance": snapshot,
            "verification_profile": PROFILE_ID,
            "profile_fingerprint": PROFILE_FINGERPRINT,
        },
    )
    proposal = DecisionRange("r", "intro", "skip", 0, 1000, "proposed", False, "detector", "match", "", ("e",), ("4",))
    run = store.begin_analysis(version.id, "job", "signature")
    result = store.finish_analysis(run, (evidence,), (proposal,), version.id)
    result = store.save_review(
        version.id, result.result_revision, [{"range_id": "r", "review_status": "approved"}], "owner"
    )
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

    def register(name, rating_key, candidates=None):
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
                "candidates": candidates
                if candidates is not None
                else [
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
    alias_identity = replace(
        identity, rating_key="episode-2", media_id="alias", part_id="alias", part_key="/part/alias"
    )
    service.store.register_media(
        alias, alias_identity, server_id="server", metadata={**metadata, "episode": 2, "remote_part_path": alias_path}
    )
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


def test_single_episode_analysis_tells_the_owner_why_nothing_was_proposed(lifecycle):
    service, register = lifecycle
    target, _ = register("target", "episode-1")
    job_id = queued_analysis(service, target)
    answer = service.get_result(target.id)
    assert answer["analysis_status"] == "complete" and NO_OTHER_EPISODES in answer["reasons"]
    job = service.get_job(job_id)
    assert job["media_version_ids"] == [target.id] and job["message"] is None
    assert job["outcome"] == {
        "state": "nothing_found",
        "message": NO_OTHER_EPISODES,
        "per_media": [{"id": target.id, "state": "nothing_found", "message": NO_OTHER_EPISODES}],
    }
    (item,) = service.list_media()["items"]
    assert (item["ranges"]["skip"], item["ranges"]["to_review"]) == (0, 0) and item["reasons"] == [NO_OTHER_EPISODES]


def test_single_episode_analysis_uses_season_siblings_and_reports_ranges_to_review(lifecycle):
    service, register = lifecycle
    target, _ = register("target", "episode-1")
    register("sibling", "episode-2")
    job = service.get_job(queued_analysis(service, target))
    assert job["outcome"]["state"] == "found" and job["message"] is None
    item = next(i for i in service.list_media()["items"] if i["id"] == target.id)
    assert item["ranges"]["skip"] > 0 and item["ranges"]["to_review"] == item["ranges"]["skip"]
    assert NO_OTHER_EPISODES not in item["reasons"] and OTHER_EPISODES_NOT_READY not in item["reasons"]


def test_siblings_that_changed_on_disk_are_reported_as_not_ready(lifecycle):
    service, register = lifecycle
    target, _ = register("target", "episode-1")
    sibling, _ = register("sibling", "episode-2")
    with open(sibling.canonical_path, "ab") as file:
        file.write(b"changed")
    queued_analysis(service, target)
    assert OTHER_EPISODES_NOT_READY in service.get_result(target.id)["reasons"]


def test_permission_change_does_not_make_an_episode_stale(lifecycle):
    service, register = lifecycle
    target, identity = register("target", "episode-1")
    time.sleep(0.02)
    os.chmod(target.canonical_path, 0o600)  # moves only the change time, as a permissions or hard-link pass does
    assert os.stat(target.canonical_path).st_ctime_ns != target.ctime_ns
    assert service.lookup(identity)["freshness"] == "current"
    queued_analysis(service, target)
    service.open_stream(target.id)[0].close()


def test_analysis_of_a_replaced_file_is_refused_in_plain_words(lifecycle):
    service, register = lifecycle
    target, _ = register("target", "episode-1")
    with open(target.canonical_path, "ab") as file:
        file.write(b"changed")
    with pytest.raises(AnalysisRefused) as refused:
        service.enqueue_analysis([target.id], principal="owner")
    assert str(refused.value) == CHANGED_ON_DISK


def test_failed_analysis_keeps_its_reason_instead_of_a_generic_failure(lifecycle, monkeypatch):
    from media_preview_generator.reprise import analysis

    service, register = lifecycle
    target, _ = register("target", "episode-1")
    detail = "Native audio timestamps are discontinuous"
    monkeypatch.setattr(
        analysis, "analyze", lambda *a, **k: AnalysisDraft((), (), "failed", (UNCOMPARABLE_MEDIA, detail))
    )
    job_id = service.enqueue_analysis([target.id], principal="owner")["id"]
    wait_for_queue()
    stored = service.store.get_result(target.id)
    assert (stored.analysis_status, stored.reasons) == ("failed", (UNCOMPARABLE_MEDIA, detail))
    job = service.get_job(job_id)
    assert (job["status"], job["message"], job["outcome"]["state"]) == ("failed", UNCOMPARABLE_MEDIA, "failed")


def test_refreshing_after_a_permission_change_keeps_the_same_version_and_its_results(lifecycle):
    service, register = lifecycle
    target, identity = register("target", "episode-1")
    queued_analysis(service, target)
    before = service.store.get_result(target.id)
    time.sleep(0.02)
    os.chmod(target.canonical_path, 0o600)
    again = media_version(target.canonical_path, target.duration_ms, target.stream_signature)
    assert again.id == target.id and again.ctime_ns != target.ctime_ns
    metadata = service.store.get_bindings(target.id)[0][2]
    service.store.register_media(again, identity, server_id="server", metadata=metadata)  # what a refresh does
    assert service.store.get_result(target.id) == before
    assert service.lookup(identity)["freshness"] == "current"


def test_a_new_device_number_or_inode_does_not_make_an_episode_stale_but_a_new_size_or_time_does(lifecycle):
    """What a reboot of the user-share mount (device) or a mover pass (inode) leaves behind in the stored identity."""
    service, register = lifecycle
    target, identity = register("target", "episode-1")
    queued_analysis(service, target)
    before = service.store.get_result(target.id)
    drifted = replace(target, device=target.device + 1, inode=target.inode + 1)
    metadata = service.store.get_bindings(target.id)[0][2]
    service.store.register_media(drifted, identity, server_id="server", metadata=metadata)  # what a refresh does
    assert media_version(target.canonical_path, target.duration_ms, target.stream_signature).id == target.id
    assert service.store.get_result(target.id) == before
    assert service.lookup(identity)["freshness"] == "current"
    service.open_stream(target.id)[0].close()
    assert is_current(replace(target, device=None, inode=None))
    assert not is_current(replace(target, size_bytes=target.size_bytes + 1))
    assert not is_current(replace(target, mtime_ns=target.mtime_ns + 1))


PLEX_MARKERS = [
    {"kind": "intro", "start_ms": 1000, "end_ms": 3000, "source": "plex"},
    {"kind": "credits", "start_ms": 4000, "end_ms": 7000, "source": "plex"},
]


def test_plex_markers_nothing_confirmed_are_suggestions_the_owner_approves_range_by_range(lifecycle):
    service, register = lifecycle
    target, identity = register("target", "episode-1", PLEX_MARKERS)
    job = service.get_job(queued_analysis(service, target))
    assert job["outcome"]["state"] == "suggested"
    assert job["outcome"]["message"].startswith("Plex suggested the intro and credits, but Reprise could not confirm")
    answer = service.get_result(target.id)
    assert NO_OTHER_EPISODES in answer["reasons"]
    assert any(reason.startswith("Plex suggested the intro (from 0:01 to 0:03)") for reason in answer["reasons"])
    (item,) = service.list_media()["items"]
    assert (item["ranges"]["suggested"], item["ranges"]["to_review"]) == (2, 0)
    # Plex's word is not enough to skip: nothing is eligible, the suggestions are only proposals
    assert not answer["eligible_skip_ranges"]
    assert {r["origin"] for r in answer["proposed_ranges"]} == {"plex"}

    result = service.store.get_result(target.id)
    intro = next(r for r in result.ranges if r.kind == "intro")
    with pytest.raises(ReviewRefused):
        service.review(
            target.id, result.result_revision, [{"range_id": intro.id, "review_status": "approved"}], "owner"
        )
    assert not service.lookup(identity)["eligible_skip_ranges"]
    approval = {"range_id": intro.id, "review_status": "approved", "acknowledge_plex_suggestion": True}
    service.review(target.id, result.result_revision, [approval], "owner")
    eligible = service.lookup(identity)["eligible_skip_ranges"]
    assert [(r["kind"], r["start_ms"], r["end_ms"]) for r in eligible] == [("intro", 1000, 3000)]  # not the credits
    (item,) = service.list_media()["items"]
    assert (item["ranges"]["suggested"], item["ranges"]["approved"]) == (1, 1)


def test_a_keep_still_vetoes_an_approved_plex_suggestion(lifecycle):
    service, register = lifecycle
    target, identity = register("target", "episode-1", PLEX_MARKERS)
    queued_analysis(service, target)
    result = service.store.get_result(target.id)
    intro = next(r for r in result.ranges if r.kind == "intro")
    edits = [
        {"range_id": intro.id, "review_status": "approved", "acknowledge_plex_suggestion": True},
        {"range_id": None, "action": "keep", "kind": "intro", "start_ms": 1500, "end_ms": 2000},
    ]
    service.review(target.id, result.result_revision, edits, "owner")
    eligible = service.lookup(identity)["eligible_skip_ranges"]
    assert [(r["start_ms"], r["end_ms"]) for r in eligible] == [(1000, 1500), (2000, 3000)]


def test_analysis_is_given_only_the_three_nearest_episodes_in_a_stable_order(lifecycle, monkeypatch):
    from media_preview_generator.reprise import analysis

    service, register = lifecycle
    target, _ = register("target", "episode-3")
    siblings = {n: register(f"sibling-{n}", f"episode-{n}")[0] for n in (1, 2, 4, 5, 6)}
    seen = []

    def capture(media, candidates, references, **kwargs):
        seen.append([reference.id for reference in references])
        return AnalysisDraft((), (), "complete", ())

    monkeypatch.setattr(analysis, "analyze", capture)
    queued_analysis(service, target)
    assert seen == [[siblings[n].id for n in (2, 4, 1)]]
