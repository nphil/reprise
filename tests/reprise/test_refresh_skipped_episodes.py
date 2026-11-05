"""A library refresh that could not read some episodes: warning for the owner, not a failure."""

import subprocess
import threading
from unittest.mock import MagicMock

import pytest

from media_preview_generator.reprise.service import RepriseService, _episode_label, _skip_reason


def _service(skipped_items, *, skipped=None):
    """A service whose scan reports ``skipped_items`` and whose job manager commits and records the final state."""
    svc = RepriseService.__new__(RepriseService)
    svc._enqueue_lock = threading.Lock()
    svc._refresh_locks = {}
    svc.store = MagicMock()
    svc.store.reconcile_library.return_value = 3
    svc.store.drop_unlisted_media.return_value = 0
    svc.job_manager = MagicMock()
    svc.job_manager.is_cancellation_requested.return_value = False
    svc.job_manager.commit_if_active.side_effect = lambda job_id, commit, complete=False: commit() or True
    svc._refresh_signature = lambda server_id, library_id: "sig"
    svc._scan_library = lambda *a, **k: {
        "registered": 10,
        "skipped": len(skipped_items) if skipped is None else skipped,
        "skipped_items": skipped_items,
        "seen": [],
        "unreadable": frozenset({"9"}),
        "listed": {},
        "config_signature": "sig",
    }
    return svc


@pytest.fixture(autouse=True)
def _no_worker_groups(monkeypatch):
    monkeypatch.setattr("media_preview_generator.jobs.group_runtime.current_groups", lambda: None)


BROKEN = [{"episode": "Atlanta S03E08 New Jazz", "reason": "Plex and FFprobe can't read its audio/video streams"}]


def _run(svc, request, **kwargs):
    return svc._run_refresh("job", request, lambda: False, **kwargs)


class TestWholeLibraryRefresh:
    def test_unreadable_episodes_complete_the_job_with_a_warning_naming_them(self):
        svc = _service(BROKEN)

        _run(svc, {"server_id": "s", "library_id": "1"})

        (call,) = svc.job_manager.complete_job.call_args_list
        assert call.kwargs.get("error") is None
        assert call.kwargs["warning"] == "1 episode could not be read and kept its earlier entry"
        (merge,) = svc.job_manager.merge_job_config.call_args_list
        result = merge.args[1]["refresh_result"]
        assert result["skipped"] == 1 and result["skipped_items"] == BROKEN and result["removed"] == 3

    def test_the_warning_counts_every_skipped_episode(self):
        svc = _service(BROKEN * 3)

        _run(svc, {"server_id": "s", "library_id": "1"})

        assert svc.job_manager.complete_job.call_args.kwargs["warning"] == (
            "3 episodes could not be read and kept their earlier entries"
        )

    def test_a_clean_refresh_has_no_warning_and_no_skipped_list(self):
        svc = _service([], skipped=0)

        _run(svc, {"server_id": "s", "library_id": "1"})

        svc.job_manager.complete_job.assert_not_called()
        (merge,) = svc.job_manager.merge_job_config.call_args_list
        assert "skipped_items" not in merge.args[1]["refresh_result"]

    def test_a_cancelled_refresh_is_still_cancelled(self):
        svc = _service(BROKEN)
        svc.job_manager.commit_if_active.side_effect = lambda *a, **k: False

        _run(svc, {"server_id": "s", "library_id": "1"})

        svc.job_manager.cancel_job.assert_called_once_with("job")
        svc.job_manager.complete_job.assert_not_called()


class TestStillFailing:
    """A scan of specific files, or one an analysis waits on, has no use for a partial answer."""

    def test_a_scan_of_specific_files_still_fails(self):
        svc = _service(BROKEN)

        _run(svc, {"server_id": "s", "library_id": "1", "candidate_paths": ["/m/a.mkv"]})

        (call,) = svc.job_manager.complete_job.call_args_list
        assert call.kwargs["error"].startswith("Library refresh incomplete")
        assert "warning" not in call.kwargs

    def test_the_refresh_before_an_analysis_still_fails(self):
        svc = _service(BROKEN)

        assert _run(svc, {"server_id": "s", "library_id": "1"}, for_analysis=True) is None

        assert svc.job_manager.complete_job.call_args.kwargs["error"].startswith("Library refresh incomplete")

    def test_plex_being_unreachable_still_raises(self):
        svc = _service(BROKEN)

        def unreachable(*args, **kwargs):
            raise ConnectionError("Plex is down")

        svc._scan_library = unreachable

        with pytest.raises(ConnectionError):
            _run(svc, {"server_id": "s", "library_id": "1"})
        svc.job_manager.complete_job.assert_not_called()


class TestJobJson:
    def _job(self, status, error):
        job = MagicMock()
        job.id = "job"
        job.status.value = status
        job.error = error
        job.progress.to_dict.return_value = {}
        job.created_at = "2026-11-04T19:38:05"
        job.config = {"refresh_request": {}, "refresh_result": {"registered": 1, "skipped": 1, "removed": 0}}
        return job

    def test_a_completed_job_with_a_warning_says_warning_not_failed(self):
        svc = _service(BROKEN)
        svc.job_manager.is_pause_requested.return_value = False

        data = svc._job_json(self._job("completed", "1 episode could not be read"))

        assert data["status"] == "completed" and data["error"] is None
        assert data["warning"] == "1 episode could not be read"

    def test_a_failed_job_still_says_failed(self):
        svc = _service(BROKEN)
        svc.job_manager.is_pause_requested.return_value = False

        data = svc._job_json(self._job("failed", "boom"))

        assert data["error"] == "Job failed" and "warning" not in data


class TestPlainReasons:
    def test_label_reads_like_the_episode_list(self):
        info = {"grandparentTitle": "Atlanta", "parentIndex": 3, "index": 8, "title": "New Jazz", "ratingKey": "9486"}
        assert _episode_label(info) == "Atlanta S03E08 New Jazz"

    def test_label_falls_back_to_the_title_then_the_id(self):
        assert _episode_label({"title": "Pilot", "ratingKey": "1"}) == "Pilot"
        assert _episode_label({"ratingKey": "77"}) == "Episode 77"

    def test_unreadable_streams_when_plex_lists_no_audio_or_video(self):
        exc = subprocess.CalledProcessError(1, ["ffprobe"])
        part = {"Stream": [{"streamType": 3, "codec": "srt"}]}
        assert _skip_reason(exc, part).startswith("Plex and FFprobe can't read its audio/video streams")

    def test_ffprobe_alone_failing_when_plex_does_list_streams(self):
        exc = subprocess.CalledProcessError(1, ["ffprobe"])
        part = {"Stream": [{"streamType": 1}, {"streamType": 2}]}
        assert _skip_reason(exc, part).startswith("FFprobe can't read the file")

    def test_a_size_mismatch_says_plex_needs_to_scan_again(self):
        reason = _skip_reason(ValueError("Plex part size differs from local file"), {})
        assert "Plex needs to scan it again" in reason

    @pytest.mark.parametrize(
        "exc",
        [
            KeyError("file"),
            ValueError("anything else /media/tv/secret.mkv"),
            OSError("/media/tv/secret.mkv: I/O error"),
        ],
    )
    def test_reasons_never_carry_paths_or_raw_exception_text(self, exc):
        assert "/media" not in _skip_reason(exc, {})
