"""Frame pairs measured in batches on the GPU, with every verdict the CPU's.

``recurrence.picture_excess`` says how far two canonical pictures are from agreeing. For frames the GPU already
reduced to their canonical cells (``canonical.Cells``) the CUDA comparison (``cuda_compare``) takes the same
measurements of many pairs at once and only a few numbers per pair come back.

What it returns is the CPU's measurement summed in float64 in a fixed order, so the two agree to the last bit on
the common data (8-bit limited range: every canonical value is a multiple of 1/16) and to about a part in 10^8 on
the rest (a full-range file's mapped values). ``recurrence.agreeing`` only asks whether a ratio is at most 1 and
whether it is at most ``near_ratio``, so a pair whose ratio is within ``RECHECK_BAND`` of either is measured again on
the CPU and the CPU's number is used: every verdict is the one the CPU gives. Whatever the GPU cannot do (no session,
a frame that was not reduced, an error of any kind) the CPU does, for the pair, the batch or the rest of the window,
and an analysis never fails because of it.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from loguru import logger

from . import acceleration, canonical
from . import recurrence as verify

# Pairs queued before the GPU is asked: a few tens of megabytes of cells, one launch's worth of work.
BATCH_PAIRS = 128
# How close (relative) a ratio may come to a threshold of ``recurrence.agreeing`` before the CPU measures the pair.
# The GPU's and the CPU's float64 sums differ by at most about 10^-8 relative; this is a thousand times that.
RECHECK_BAND = 1e-5

_LIMITS = np.array([verify.VIDEO[name] for name in verify.PICTURE_METRICS], dtype=np.float64)
_THRESHOLDS = (1.0, float(verify.NEAR_RATIO))


def cpu_excess(frame, other, framing) -> float:
    """The CPU's measurement of a pair of reduced or whole frames: ``recurrence.picture_excess`` of their pictures."""
    return verify.picture_excess(canonical.picture(frame, framing.target), canonical.picture(other, framing.reference))


def near_a_threshold(ratio: float) -> bool:
    return any(abs(ratio - limit) <= RECHECK_BAND * limit for limit in _THRESHOLDS)


def _position(seen: dict[int, int], items: list, frame) -> int:
    place = seen.get(id(frame))
    if place is None:
        place = seen[id(frame)] = len(items)
        items.append(frame.cells)
    return place


class GpuPairs:
    """The pairs of one window that wait for the GPU.

    ``offer`` takes a pair (``index``: which target frame it is of; ``entry``: the reference frame's slot) and
    ``flush`` measures all that are queued, handing each ``settle(index, entry, excess)``. ``framing`` must not be
    a measured one: its two regions are the frames' own canonical regions.
    """

    def __init__(self, framing, settle):
        self.framing = framing
        self.settle = settle
        self.queue: list[tuple] = []
        self.session: Any = None
        self.asked = False  # the session was asked for (once per window)
        self.full: Any = None  # (left, right): whether each side's frames are full range, from the first pair offered

    def offer(self, index, entry, frame, other) -> bool:
        """Queue the pair for the GPU and return True, or False when the CPU has to measure it now: a frame that was
        not reduced, cells that are not the framing's regions, or no GPU session to take it."""
        if (
            frame.cells is None
            or other.cells is None
            or frame.cells.region != self.framing.target
            or other.cells.region != self.framing.reference
        ):
            return False
        full = (canonical.full_range(frame.color_range), canonical.full_range(other.color_range))
        if self.full is None:
            self.full = full
        if full != self.full:
            return False
        if not self.asked:
            self.asked = True
            self.session = acceleration.compare_session()
        if self.session is None:
            return False
        self.queue.append((index, entry, frame, other))
        if len(self.queue) >= BATCH_PAIRS:
            self.flush()
        return True

    def flush(self) -> None:
        """Measure every queued pair, on the GPU or, when it cannot, on the CPU, and settle each."""
        queue, self.queue = self.queue, []
        if not queue:
            return
        ratios = self._gpu_ratios(queue)
        again = 0
        for place, (index, entry, frame, other) in enumerate(queue):
            ratio = None if ratios is None else float(ratios[place])
            if ratio is None or near_a_threshold(ratio):
                again += ratio is not None
                ratio = cpu_excess(frame, other, self.framing)
            self.settle(index, entry, ratio)
        if ratios is None:
            acceleration.record_compare(fallbacks=1)
        else:
            acceleration.record_compare(pairs=len(queue), rechecked=again)

    def _gpu_ratios(self, queue):
        session, full = self.session, self.full
        try:
            lefts: list = []
            rights: list = []
            left_at: dict[int, int] = {}
            right_at: dict[int, int] = {}
            pairs = np.empty((len(queue), 2), dtype=np.int64)
            for place, (_, _, frame, other) in enumerate(queue):
                pairs[place] = _position(left_at, lefts, frame), _position(right_at, rights, other)
            values = np.asarray(
                session.metrics(
                    lefts,
                    rights,
                    pairs,
                    left_full=full[0],
                    right_full=full[1],
                    concentration_level=verify.VIDEO["concentration_level"],
                ),
                dtype=np.float64,
            )
            if values.shape != (len(queue), len(verify.PICTURE_METRICS)) or not np.isfinite(values).all():
                raise ValueError("the GPU returned unusable numbers")
            return (values / _LIMITS).max(axis=1)
        except Exception as exc:  # whatever stops the GPU, the CPU measures these pairs and the window's later ones
            logger.warning("Reprise measured {} frame pairs on the CPU instead of the GPU: {}", len(queue), exc)
            self.session = None
            return None
