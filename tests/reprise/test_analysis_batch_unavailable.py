"""An analysis batch whose episode went missing or changed on disk carries on with the others."""

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from media_preview_generator.reprise import service as service_module
from media_preview_generator.reprise.service import RepriseService

IDS = ["e17", "e18", "e19"]


class FakeJobs:
    """Just enough job manager: a config dict, the commit/complete/cancel calls, and a cancellation flag."""

    config_dir = None

    def __init__(self, ids):
        self.config = {"media_version_ids": list(ids)}
        self.cancel_requested = False
        self.completed = []
        self.cancelled = False
        self.job = SimpleNamespace(config=self.config, priority=0)

    def get_job(self, job_id):
        return self.job

    def merge_job_config(self, job_id, updates):
        self.config.update(updates)

    def is_cancellation_requested(self, job_id):
        return self.cancel_requested

    def is_pause_requested(self, job_id):
        return False

    def update_progress(self, *args, **kwargs):
        pass

    def commit_if_active(self, job_id, commit, *, complete=False):
        if self.cancel_requested:
            return False
        commit()
        return True

    def complete_job(self, job_id, error=None, warning=None):
        self.completed.append({"error": error, "warning": warning})

    def cancel_job(self, job_id):
        self.cancelled = True


@pytest.fixture
def batch(monkeypatch):
    """A service with three episodes (Community S03E17 to E19); ``gone`` holds the ones whose file is missing."""
    monkeypatch.setattr(service_module, "admit_references", lambda version, refs, store: [])
    monkeypatch.setattr(service_module, "ffmpeg_binary", lambda: "ffmpeg")
    monkeypatch.setattr(
        "media_preview_generator.web.settings_manager.get_settings_manager",
        lambda: SimpleNamespace(processing_paused=False),
    )
    state = SimpleNamespace(gone={}, analyzed=[], on_analyze=lambda version: None)

    def analyze(version, *args, **kwargs):
        state.on_analyze(version)
        state.analyzed.append(version.id)
        return SimpleNamespace(status="complete", reasons=[], evidence=[], ranges=[])

    monkeypatch.setattr("media_preview_generator.reprise.analysis.analyze", analyze)

    svc = RepriseService.__new__(RepriseService)
    svc._enqueue_lock = threading.Lock()
    svc.job_manager = FakeJobs(IDS)
    svc.store = MagicMock()
    svc.store.get_media.side_effect = lambda mid: SimpleNamespace(id=mid, duration_ms=2_700_000, stream_signature="sig")
    svc.store.get_bindings.side_effect = lambda mid: [
        (
            object(),
            "srv",
            {
                "show_title": "Community",
                "title": "Basic Lupine Urology" if mid == "e18" else "Episode",
                "season": 3,
                "episode": int(mid[1:]),
                "candidates": [],
            },
        )
    ]
    svc.store.list_media.return_value = {"items": [], "next_cursor": None}
    svc.store.owner_keep_ranges.return_value = []
    svc.store.begin_analysis.side_effect = lambda mid, job_id, signature: "a-" + mid
    svc._online_lookups = lambda: None
    svc._online_windows = lambda *a, **k: {}
    svc._binding_problem = lambda version, identity, server_id, meta: state.gone.get(version.id)
    svc.state = state
    return svc


def run(svc):
    return svc.run_job("job")


def test_a_missing_middle_episode_is_noted_and_the_others_are_analyzed(batch):
    batch.state.gone["e18"] = "file_missing"

    run(batch)

    jobs = batch.job_manager
    assert batch.state.analyzed == ["e17", "e19"]
    assert not jobs.cancelled
    (done,) = jobs.completed
    assert done["error"] is None
    assert done["warning"].startswith("1 episode was not analyzed because its file is missing, changed or unavailable")
    result = jobs.config["analysis_result"]
    assert result["skipped"] == 1
    (item,) = result["skipped_items"]
    assert item["episode"] == "Community S03E18 Basic Lupine Urology"
    assert item["reason"].startswith("This file is no longer on disk")
    outcomes = jobs.config["analysis_outcomes"]
    assert outcomes["e18"]["state"] == "failed" and "Refresh the library" in outcomes["e18"]["message"]
    assert outcomes["e17"]["state"] != "failed" and outcomes["e19"]["state"] != "failed"


def test_a_file_changed_on_disk_is_noted_with_its_own_reason(batch):
    batch.state.gone["e18"] = "file_changed"

    run(batch)

    (item,) = batch.job_manager.config["analysis_result"]["skipped_items"]
    assert item["reason"] == "This file has changed since Reprise last read it"
    assert batch.state.analyzed == ["e17", "e19"]


def test_a_missing_last_episode_still_completes_the_job_with_the_warning(batch):
    batch.state.gone["e19"] = "file_missing"

    run(batch)

    assert batch.state.analyzed == ["e17", "e18"]
    (done,) = batch.job_manager.completed
    assert done["error"] is None and done["warning"]


def test_a_file_that_vanishes_while_it_is_analyzed_is_left_out_too(batch):
    def vanish(version):
        if version.id == "e18":
            batch.state.gone["e18"] = "file_missing"
            raise OSError("gone")

    batch.state.on_analyze = vanish

    run(batch)

    assert batch.state.analyzed == ["e17", "e19"]
    (done,) = batch.job_manager.completed
    assert done["error"] is None and done["warning"]
    assert [i["episode"] for i in batch.job_manager.config["analysis_result"]["skipped_items"]] == [
        "Community S03E18 Basic Lupine Urology"
    ]
    batch.store.fail_analysis.assert_called_once()


def test_every_episode_missing_completes_with_a_warning_not_a_failure(batch):
    batch.state.gone.update(dict.fromkeys(IDS, "file_missing"))

    run(batch)

    assert batch.state.analyzed == []
    (done,) = batch.job_manager.completed
    assert done["error"] is None and done["warning"].startswith("3 episodes were not analyzed")


def test_a_clean_batch_completes_without_a_warning(batch):
    run(batch)

    assert batch.state.analyzed == IDS
    assert batch.job_manager.completed == [{"error": None, "warning": None}]
    assert "analysis_result" not in batch.job_manager.config


def test_a_job_wide_failure_still_fails_the_job(batch):
    batch.state.on_analyze = lambda version: (_ for _ in ()).throw(ConnectionError("Plex is down"))

    with pytest.raises(ConnectionError):
        run(batch)

    assert batch.job_manager.completed == [] and not batch.job_manager.cancelled


def test_cancelling_still_cancels_even_after_an_episode_was_left_out(batch):
    batch.state.gone["e17"] = "file_missing"
    batch.state.on_analyze = lambda version: setattr(batch.job_manager, "cancel_requested", True)

    run(batch)

    assert batch.job_manager.cancelled
    assert batch.job_manager.completed == []


def test_cancelling_during_the_last_episode_cancels(batch):
    batch.state.on_analyze = lambda version: (
        setattr(batch.job_manager, "cancel_requested", True) if version.id == "e19" else None
    )

    run(batch)

    assert batch.job_manager.cancelled and batch.job_manager.completed == []


def test_the_finished_job_says_which_episode_was_left_out(batch):
    batch.state.gone["e18"] = "file_missing"
    run(batch)
    jobs = batch.job_manager
    job = SimpleNamespace(
        id="job",
        status=SimpleNamespace(value="completed"),
        error=jobs.completed[0]["warning"],
        config=jobs.config,
        progress=SimpleNamespace(to_dict=lambda: {}),
        created_at="2026-11-20T00:00:00",
    )

    data = batch._job_json(job)

    assert data["status"] == "completed" and data["error"] is None
    assert data["warning"] == jobs.completed[0]["warning"]
    assert data["analysis"]["skipped_items"][0]["episode"] == "Community S03E18 Basic Lupine Urology"
    assert "1 episode could not be analyzed" in data["outcome"]["message"]
