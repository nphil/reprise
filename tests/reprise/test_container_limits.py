"""A container's process limit counts every thread, so searches that run together are sized to what it has left.

Found on the NAS: the Reprise container allows 128 processes and threads, one FFmpeg decode held 37 of them (55 on the
GPU: the encoder behind the decoder started one thread per CPU of the host) and three references searched at once went
past the limit. FFmpeg could not start a thread, logged an error, and the analysis of Community S1E03 ended with
"Native frame metadata failed".
"""

import pytest

from media_preview_generator.reprise import analysis, container_limits


@pytest.fixture
def cgroup(tmp_path, monkeypatch):
    monkeypatch.setattr(container_limits, "_CGROUP", str(tmp_path))
    return tmp_path


def limit(directory, maximum, current):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "pids.max").write_text(f"{maximum}\n")
    (directory / "pids.current").write_text(f"{current}\n")


def test_headroom_is_the_limit_less_what_is_in_use(cgroup):
    limit(cgroup, 128, 37)  # the Reprise container on the NAS, idle
    assert container_limits.process_headroom() == 91
    limit(cgroup, 128, 140)
    assert container_limits.process_headroom() == 0


def test_cgroup_v1_keeps_the_same_files_one_folder_down(cgroup):
    limit(cgroup / "pids", 128, 37)
    assert container_limits.process_headroom() == 91


@pytest.mark.parametrize("maximum", ["max", "unlimited", ""])
def test_a_container_without_a_limit_has_no_figure(cgroup, maximum):
    limit(cgroup, maximum, 37)
    assert container_limits.process_headroom() is None


def test_a_limit_without_a_count_has_no_figure(cgroup):
    (cgroup / "pids.max").write_text("128\n")
    assert container_limits.process_headroom() is None


def test_a_machine_without_the_pids_controller_has_no_figure(cgroup):
    assert container_limits.process_headroom() is None


@pytest.mark.parametrize(
    ("room", "workers"),
    [
        (None, 3),  # no limit: as many as are configured
        (400, 3),
        (91, 2),  # the NAS: 128 allowed, 37 in use
        (60, 1),
        (0, 1),  # never fewer than one: the search must still run
    ],
)
def test_searches_run_together_only_as_far_as_the_limit_has_room(monkeypatch, room, workers):
    monkeypatch.setattr(analysis, "SEARCH_WORKERS", 3)
    monkeypatch.setattr(container_limits, "process_headroom", lambda: room)
    assert analysis._search_workers() == workers
