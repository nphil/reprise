"""Shared setup of the Reprise tests."""

import pytest

from media_preview_generator.reprise import container_limits


@pytest.fixture(autouse=True)
def the_machine_has_no_process_limit(monkeypatch):
    """Analyses search as many references at once as the test says, whatever process limit the machine running the
    tests has: the real limit is read from /sys/fs/cgroup, and a test of it points that somewhere else itself."""
    monkeypatch.setattr(container_limits, "_CGROUP", "/nonexistent")
