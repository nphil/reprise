"""An analysis cut off by a stop or a restart is ended, not left reading "Analyzing" for good.

Community S1E04 read "Analyzing" in the Library with no job behind it: the container was stopped in the middle of its
analysis, so nothing ever finished the run, and nothing noticed that it had not finished.
"""

import pytest

from media_preview_generator.markers.store import MarkerStore
from media_preview_generator.reprise.identity import media_version
from media_preview_generator.reprise.models import PlaybackIdentity
from media_preview_generator.reprise.service import RepriseService, _mapping_fingerprint
from media_preview_generator.reprise.store import RepriseStore, RevisionConflict
from media_preview_generator.servers.registry import ServerRegistry

INTERRUPTED = "Analysis was interrupted before it finished"


@pytest.fixture
def library(tmp_path):
    """Two registered episodes; the store of this process (it carries out analyses) and the store of the process
    before the restart (which cannot carry out any more: a new store has no run of its own yet)."""
    folder = tmp_path / "library"
    folder.mkdir()
    markers = MarkerStore(str(tmp_path / "markers.db"))
    before_restart, store = RepriseStore(markers), RepriseStore(markers)
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
                "libraries": [{"id": "library", "name": "Library", "enabled": True, "remote_paths": [str(folder)]}],
            }
        ]
    )
    fingerprint = _mapping_fingerprint(registry.get_config("server"), registry.get_config("server").libraries[0])

    def add(number):
        path = folder / f"{number}.mp4"
        path.write_bytes(b"episode %d" % number)
        version = media_version(str(path), 1000, "{}")
        identity = PlaybackIdentity(
            "machine", str(number), "2", "3", f"/library/parts/{number}", path.stat().st_size, 1000, "4"
        )
        metadata = {
            "library_id": "library",
            "remote_part_path": str(path),
            "mapping_fingerprint": fingerprint,
            "show_title": "Community",
            "title": f"Episode {number}",
            "season": 1,
            "episode": number,
        }
        store.register_media(version, identity, server_id="server", metadata=metadata)
        return version, identity

    yield store, before_restart, RepriseService(store, registry, None), add(3), add(4)
    markers.close()


def test_an_analysis_cut_off_by_a_restart_is_ended_and_one_still_running_is_left_alone(library):
    store, before_restart, _, (cut_off, _), (running, _) = library
    cut_off_run = before_restart.begin_analysis(cut_off.id, "job-1", "signature")  # the container is stopped here
    live_run = store.begin_analysis(running.id, "job-2", "signature")  # an analysis this process is carrying out
    assert store.fail_stale_analyses() == 1
    ended = store.get_result(cut_off.id)
    assert (ended.analysis_status, ended.reasons) == ("cancelled", (INTERRUPTED,))
    assert store.get_result(running.id).analysis_status == "running"
    assert store.fail_stale_analyses() == 0
    # What held the ended run cannot finish it afterwards, and the episode can be analyzed again.
    with pytest.raises(RevisionConflict):
        store.finish_analysis(cut_off_run, (), (), cut_off.id)
    again = store.begin_analysis(cut_off.id, "job-3", "signature")
    assert store.finish_analysis(again, (), (), cut_off.id).analysis_status == "complete"
    # A run whose thread ended without finishing it (an unexpected error) is ended by the next look too.
    store.release_analysis(live_run)
    assert store.fail_stale_analyses() == 1
    assert store.get_result(running.id).analysis_status == "cancelled"


def test_a_failed_start_of_a_run_leaves_nothing_carried_out(library):
    store, _, _, _, _ = library
    with pytest.raises(KeyError):
        store.begin_analysis("no-such-version", "job", "signature")  # no binding: nothing was recorded either
    assert store._live_runs == set()


def test_the_library_and_the_result_never_say_analyzing_for_a_run_nothing_carries_out(library):
    store, before_restart, service, (cut_off, cut_off_identity), (running, _) = library
    before_restart.begin_analysis(cut_off.id, "job-1", "signature")
    live_run = store.begin_analysis(running.id, "job-2", "signature")

    def statuses():
        return {item["id"]: item["analysis_status"] for item in service.list_media()["items"]}

    assert statuses() == {cut_off.id: "cancelled", running.id: "running"}
    result = service.get_result(cut_off.id)
    assert (result["analysis_status"], result["reasons"]) == ("cancelled", [INTERRUPTED])
    assert service.lookup(cut_off_identity)["analysis_status"] == "cancelled"
    assert service.get_result(running.id)["analysis_status"] == "running"
    store.release_analysis(live_run)  # the job ended without recording how
    assert statuses() == {cut_off.id: "cancelled", running.id: "cancelled"}


def test_starting_the_app_ends_the_analyses_a_restart_cut_off(tmp_path, mock_auth_config):
    from media_preview_generator.markers.store import get_marker_store, reset_marker_store
    from media_preview_generator.web.app import create_app

    reset_marker_store()
    try:
        before_restart = RepriseStore(get_marker_store(str(tmp_path)))
        path = tmp_path / "episode.mp4"
        path.write_bytes(b"episode")
        version = media_version(str(path), 1000, "{}")
        identity = PlaybackIdentity("machine", "4", "2", "3", "/library/parts/4", path.stat().st_size, 1000, "4")
        before_restart.register_media(version, identity, server_id="server")
        before_restart.begin_analysis(version.id, "job", "signature")  # the container is stopped here
        app = create_app(config_dir=str(tmp_path))
        result = app.extensions["reprise"].store.get_result(version.id)
        assert (result.analysis_status, result.reasons) == ("cancelled", (INTERRUPTED,))
    finally:
        reset_marker_store()


def test_a_failed_sweep_never_stops_the_app_starting():
    from media_preview_generator.web.app import _end_interrupted_reprise_analyses

    class Broken:
        def fail_stale_analyses(self):
            raise RuntimeError("database is locked")

    _end_interrupted_reprise_analyses(Broken())
