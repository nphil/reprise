"""What the container the engine runs in allows, so parallel work never asks for more than it has.

A container started with a process limit (Docker's ``--pids-limit``) counts every thread of every process against it,
FFmpeg's and Python's alike. Past the limit a thread cannot start, FFmpeg logs an error ("Resource temporarily
unavailable", "ff_frame_thread_encoder_init failed"), and a decode has to treat that as fatal like any other log error:
the analysis then ends with "Native frame metadata failed".
"""

from __future__ import annotations

_CGROUP = "/sys/fs/cgroup"


def _read(path):
    try:
        with open(path, encoding="ascii") as handle:
            return handle.read().strip()
    except OSError:
        return None


def process_headroom():
    """How many more processes and threads this container may start, or ``None`` when it has no limit (or the limit
    cannot be read, which is taken as none)."""
    for root in (_CGROUP, f"{_CGROUP}/pids"):  # cgroup v2 as a container sees it, then cgroup v1
        limit = _read(f"{root}/pids.max")
        if limit is None:
            continue
        used = _read(f"{root}/pids.current")
        if not limit.isdigit() or used is None or not used.isdigit():
            return None
        return max(0, int(limit) - int(used))
    return None
