"""Bounded native-signal gates and disjoint alignment anchors.

Hints never constitute support. Decode lifecycle and generation checks belong to
analysis; this module retains only descriptors, interval vetoes and rolling spectra.
"""

import math
from collections import defaultdict, deque
from fractions import Fraction

import numpy as np

from . import cuda_metrics
from .verification_profile import PROFILE_SPEC

CANDIDATE = PROFILE_SPEC["candidate"]
VIDEO = PROFILE_SPEC["video"]
AUDIO = PROFILE_SPEC["audio"]
ANCHORS = PROFILE_SPEC["anchors"]
SOUND = PROFILE_SPEC["sound_offset"]
COLOR_FIELDS = tuple(PROFILE_SPEC["metadata"]["color_fields"])
CHANGE_GUARD_MS = PROFILE_SPEC["support"]["guard_ms"]
MIN_RUN_MS = PROFILE_SPEC["support"]["minimum_run_ms"]
MAX_FRAME_GAP_MS = VIDEO["max_frame_gap_ms"]


def plane_array(plane):
    return np.ndarray(
        (plane.height, plane.width),
        dtype=np.dtype("u1" if plane.sample_bytes == 1 else "<u2"),
        buffer=plane.data,
        strides=(plane.stride_bytes, plane.sample_bytes),
    )


def declaration(value, field):
    metadata = PROFILE_SPEC["metadata"]
    if value in metadata["unspecified_tokens"]:
        return "unspecified"
    if value not in metadata[field.removeprefix("color_")]:
        raise ValueError("Unsupported native " + field + " declaration")
    return value


def frame_declarations(frame):
    return (
        frame.width,
        frame.height,
        frame.pixel_format,
        frame.bit_depth,
        frame.sample_aspect_ratio,
        *(declaration(getattr(frame, key), key) for key in COLOR_FIELDS),
        frame.chroma_location,
        frame.interpretation_flags,
    )


def descriptor(frame):
    values = []
    for plane in frame.planes:
        for rows in np.array_split(plane_array(plane), 4, axis=0):
            for tile in np.array_split(rows, 4, axis=1):
                values.append(float(tile[::4, ::4].mean()) * 255 / ((1 << frame.bit_depth) - 1))
    return np.asarray(values, dtype=np.float32)


def _plane_std(plane, bit_depth):
    return float(plane_array(plane).std(dtype=np.float64)) * 255 / ((1 << bit_depth) - 1)


def spatial_information(frame):
    """The largest within-plane population standard deviation, normalised to 8 bits.

    Only ever compared with ``plane_std``, so it comes from the planes' exact integer moments (sum and sum of squares:
    no float64 copy of the plane, 3x faster) instead of numpy's float64 two-pass. The two agree to 1e-14; within 1e-6 of
    the limit the numpy value decides, so the comparison is the one numpy's value would give.
    """
    limit = CANDIDATE["plane_std"]
    best = 0.0
    for plane in frame.planes:
        flat = plane_array(plane).reshape(-1)
        n = flat.size
        total = int(flat.sum(dtype=np.uint64))
        squares = int(np.einsum("i,i->", flat, flat, dtype=np.uint64))
        value = math.sqrt((n * squares - total * total) / (n * n)) * 255 / ((1 << frame.bit_depth) - 1)
        best = max(best, _plane_std(plane, frame.bit_depth) if abs(value - limit) < 1e-6 else value)
    return best


def perceptual_shifts(target, reference):
    if not target or not reference:
        return []
    right = np.stack([r[1] for r in reference])
    votes = defaultdict(list)
    for pts, feature, std in target[:: max(1, len(target) // CANDIDATE["sample_count"])]:
        if std < CANDIDATE["plane_std"]:
            continue
        distance = np.mean((right - feature) ** 2, axis=1)
        count = min(CANDIDATE["neighbors"], len(reference))
        for idx in np.argpartition(distance, count - 1)[:count]:
            if reference[int(idx)][2] < CANDIDATE["plane_std"] or distance[idx] > CANDIDATE["descriptor_mse"]:
                continue
            offset = reference[int(idx)][0] - pts
            votes[round(offset * CANDIDATE["bucket_per_ms"])].append((pts, offset))
    valid = [
        p
        for p in votes.values()
        if len(p) >= CANDIDATE["minimum_votes"]
        and max(q[0] for q in p) - min(q[0] for q in p) >= CANDIDATE["minimum_vote_span_ms"]
    ]
    valid.sort(key=lambda p: (-len(p), abs(p[0][1])))
    return [p[len(p) // 2][1] for p in valid[: CANDIDATE["maximum_offsets"]]]


def sound_lag(target, reference, nearest, block):
    """The whole-sample lag at which the longer ``reference`` PCM best contains ``target`` (both samples x channels),
    as ``(lag, score)``. The target is cut into blocks of ``block`` samples (a shorter last block is dropped unless
    it is the only one); each block's normalised cross-correlation (every channel as one signal) is taken over every
    lag the reference allows, and the block with the highest peak decides: a probe that straddles shared and unshared
    sound still has blocks wholly inside the shared part. Among that block's scores within the profile's tie of its
    peak, the lag nearest ``nearest`` (the lag the picture predicts) wins. ``None`` when nothing audible can be
    correlated. A lag is a proposal, never support."""
    x, y = np.asarray(target, dtype=np.float64), np.asarray(reference, dtype=np.float64)
    if x.ndim != 2 or y.shape[1:] != x.shape[1:] or len(x) == 0 or len(y) < len(x) or block <= 0:
        raise ValueError("Malformed sound probe")
    lags = len(y) - len(x) + 1
    floor = AUDIO["denominator_floor"]
    best = None
    for start in range(0, len(x), block):
        xs = x[start : start + block]
        if len(xs) < block and start:
            break
        ys = y[start : start + len(xs) + lags - 1]
        energy = float(np.sum(xs * xs))
        squares = np.concatenate(([0.0], np.cumsum(np.sum(ys * ys, axis=1))))
        windows = squares[len(xs) : len(xs) + lags] - squares[:lags]
        audible = windows > floor
        if energy <= floor or not audible.any():
            continue
        size = 1 << (len(ys) - 1).bit_length()
        spectrum = np.zeros(size // 2 + 1, dtype=np.complex128)
        for channel in range(x.shape[1]):
            spectrum += np.fft.rfft(ys[:, channel], size) * np.conj(np.fft.rfft(xs[:, channel], size))
        correlation = np.fft.irfft(spectrum, size)[:lags]
        scores = np.where(audible, correlation / np.sqrt(np.maximum(windows, floor) * energy), -1.0)
        peak = float(scores.max())
        if best is None or peak > best[0]:
            candidates = np.flatnonzero(scores >= peak - SOUND["tie"])
            lag = int(candidates[np.argmin(np.abs(candidates - nearest))])
            best = (peak, lag)
    return None if best is None else (best[1], best[0])


def agreeing_frames(target, reference, lo, hi, offset):
    """The target hints in [lo, hi) whose descriptor agrees with the reference's at exactly ``offset``, as (pts,
    descriptor). Agreement places anchors and the sound probe; it is never support."""
    mapped = {t: (feature, std) for t, feature, std in reference}
    points = []
    for t, feature, std in target:
        if not lo <= t < hi or std < CANDIDATE["plane_std"]:
            continue
        ref = mapped.get(t + offset)
        if (
            ref is not None
            and ref[1] >= CANDIDATE["plane_std"]
            and np.mean((feature - ref[0]) ** 2) <= CANDIDATE["descriptor_mse"]
        ):
            points.append((t, feature))
    return points


def anchor(target, reference, lo, hi, offset):
    points = agreeing_frames(target, reference, lo, hi, offset)
    if len(points) < ANCHORS["minimum_total_frames"]:
        return None

    def valid(group):
        return (
            len(group) >= ANCHORS["minimum_group_frames"]
            and group[-1][0] - group[0][0] >= ANCHORS["span_ms"]
            and float(np.sqrt(np.mean((group[-1][1] - group[0][1]) ** 2))) >= ANCHORS["temporal_rms"]
        )

    left = next(
        (
            edge
            for i, p in enumerate(points)
            if valid(
                edge := [
                    q
                    for q in points[i:]
                    if q[0] <= p[0] + min(Fraction(ANCHORS["edge_ms"]), (points[-1][0] - p[0]) / 2)
                ]
            )
        ),
        None,
    )
    if left is None:
        return None
    right = next(
        (
            edge
            for i in range(len(points) - 1, -1, -1)
            if valid(
                edge := [q for q in points[: i + 1] if q[0] >= points[i][0] - ANCHORS["edge_ms"] and q[0] > left[-1][0]]
            )
        ),
        None,
    )
    if right is None or right[-1][0] - left[0][0] < MIN_RUN_MS:
        return None
    return (
        math.ceil(left[0][0]),
        math.floor(right[-1][0]),
        {
            "start_pts_ms": [str(q[0]) for q in left],
            "end_pts_ms": [str(q[0]) for q in right],
        },
    )


def _local_max(data, size):
    worst = 0.0
    h, w = data.shape
    for oy in (0, size // 2):
        for ox in (0, size // 2):
            ys = np.unique(np.r_[0, np.arange(oy, h, size), h]).astype(int)
            xs = np.unique(np.r_[0, np.arange(ox, w, size), w]).astype(int)
            sums = np.add.reduceat(np.add.reduceat(data, ys[:-1], axis=0), xs[:-1], axis=1)
            worst = max(worst, float((sums / (np.diff(ys)[:, None] * np.diff(xs)[None, :])).max()))
    return worst


def _integral(data):
    """Inclusive float64 prefix sums with a zero row and column, shared by every signed tile size."""
    h, w = data.shape
    integral = np.zeros((h + 1, w + 1), dtype=np.float64)
    np.cumsum(data, axis=0, dtype=np.float64, out=integral[1:, 1:])
    np.cumsum(integral[1:, 1:], axis=1, out=integral[1:, 1:])
    return integral


def _signed_local(data, size, integral):
    h, w = data.shape
    if h < size or w < size:
        return float(abs(data.mean()))
    ys = np.unique(np.r_[np.arange(0, h - size + 1, size // 2), h - size])
    xs = np.unique(np.r_[np.arange(0, w - size + 1, size // 2), w - size])
    sums = (
        integral[(ys + size)[:, None], xs + size]
        - integral[ys[:, None], xs + size]
        - integral[(ys + size)[:, None], xs]
        + integral[ys[:, None], xs]
    )
    return float(np.max(np.abs(sums)) / (size * size))


def video_metrics(left, right):
    """Every metric's worst value over the planes (None when the frames are not comparable): the reference the
    verdict functions are tested against, and the numbers behind a diagnosis."""
    if (
        left.interpretation_flags
        or right.interpretation_flags
        or frame_declarations(left) != frame_declarations(right)
        or len(left.planes) != len(right.planes)
    ):
        return None
    maxima = dict.fromkeys(("peak", "local4", "local8", "concentration8", "signed8", "signed16", "signed32"), 0.0)
    for a, b in zip(left.planes, right.planes, strict=True):
        if (a.width, a.height, a.sample_bytes) != (b.width, b.height, b.sample_bytes):
            return None
        left_data, right_data = plane_array(a).astype(np.float32), plane_array(b).astype(np.float32)
        raw = left_data - right_data
        absolute = np.abs(raw) * (255 / ((1 << left.bit_depth) - 1))
        residual = raw * 255 / ((1 << left.bit_depth) - 1)
        maxima["peak"] = max(maxima["peak"], float(absolute.max()))
        for size in (4, 8):
            key = "local" + str(size)
            maxima[key] = max(maxima[key], _local_max(absolute, size))
        maxima["concentration8"] = max(
            maxima["concentration8"], _local_max((absolute > VIDEO["concentration_level"]).astype(np.float32), 8)
        )
        integral = _integral(residual)
        for size in (8, 16, 32):
            key = "signed" + str(size)
            maxima[key] = max(maxima[key], _signed_local(residual, size, integral))
    return maxima


def _plane_matches(left_data, right_data, bit_depth):
    """Whether every VIDEO limit holds on one plane pair, stopping at the first limit exceeded.

    The limits are one conjunction, so the stop gives the verdict ``video_metrics`` gives; it only skips metrics that can
    no longer change it. The arithmetic of every metric is ``video_metrics``'s own, cheapest metrics first: a pair that
    differs (a neighbouring frame, another scene) fails on the peak alone, which costs 1/15th of the full set.
    """
    scale = 255 / ((1 << bit_depth) - 1)
    raw = left_data.astype(np.float32) - right_data.astype(np.float32)
    absolute = np.abs(raw) * scale
    if float(absolute.max()) > VIDEO["peak"]:
        return False
    for size in (4, 8):
        if _local_max(absolute, size) > VIDEO["local" + str(size)]:
            return False
    if _local_max((absolute > VIDEO["concentration_level"]).astype(np.float32), 8) > VIDEO["concentration8"]:
        return False
    residual = raw * 255 / ((1 << bit_depth) - 1)
    integral = _integral(residual)
    return all(_signed_local(residual, size, integral) <= VIDEO["signed" + str(size)] for size in (8, 16, 32))


def video_matches(left, right):
    """Whether two decoded frames agree within every VIDEO limit (``video_metrics``'s verdict, computed lazily).

    Each plane pair goes to the GPU first when one is usable (``cuda_metrics``): it answers pass or fail only when the
    exact result is outside the arithmetic's error band, and None otherwise, which is decided here on the CPU.
    """
    if (
        left.interpretation_flags
        or right.interpretation_flags
        or frame_declarations(left) != frame_declarations(right)
        or len(left.planes) != len(right.planes)
    ):
        return False
    pairs = list(zip(left.planes, right.planes, strict=True))
    if any((a.width, a.height, a.sample_bytes) != (b.width, b.height, b.sample_bytes) for a, b in pairs):
        return False
    for a, b in pairs:
        left_data, right_data = plane_array(a), plane_array(b)
        verdict = cuda_metrics.plane_verdict(left_data, right_data, left.bit_depth)
        if verdict is None:
            verdict = _plane_matches(left_data, right_data, left.bit_depth)
        if not verdict:
            return False
    return True


class AudioVerifier:
    """One exact sample grid, rolling spectra, and a permanent union of vetoes."""

    def __init__(self, rate, origin):
        if rate <= 0 or rate % 100:
            raise ValueError("Native audio window does not fit sample grid")
        self.rate, self.origin = rate, origin
        self.width, self.hop = rate * AUDIO["window_ms"] // 1000, rate * AUDIO["hop_ms"] // 1000
        self.window = np.hanning(self.width)[:, None]
        self.tail = None
        self.samples = 0
        self.windows = 0
        self.magnitude = deque(maxlen=AUDIO["novelty_windows"])
        self.phase = deque(maxlen=AUDIO["phase_windows"])
        self.levels = deque(maxlen=AUDIO["moving_windows"])
        self.bad = []
        self.last = None

    def _veto(self, start, duration, guard):
        lo, hi = math.floor(start) - guard, math.ceil(start + duration) + guard
        # Branch witnesses arrive in different temporal order; retain a union,
        # never allow a later pass to erase an earlier witnessed veto.
        self.bad = union([*self.bad, (lo, hi)])

    def feed(self, left, right):
        if left.shape != right.shape or left.ndim != 2 or not np.isfinite(left).all() or not np.isfinite(right).all():
            raise ValueError("Malformed or nonfinite native audio")
        if self.tail is not None:
            left = np.concatenate((self.tail[0], left))
            right = np.concatenate((self.tail[1], right))
        consumed = 0
        for index in range(0, len(left) - self.width + 1, self.hop):
            x, y = left[index : index + self.width], right[index : index + self.width]
            start = self.origin + Fraction((self.samples + index) * 1000, self.rate)
            residual = x - y
            rms = np.maximum(np.sqrt(np.mean(x * x, axis=0)), np.sqrt(np.mean(y * y, axis=0)))
            error = np.sqrt(np.mean(residual * residual, axis=0))
            correlation = np.sum(x * y, axis=0) / np.maximum(
                np.sqrt(np.sum(x * x, axis=0) * np.sum(y * y, axis=0)), AUDIO["denominator_floor"]
            )
            identity = np.all(
                (error <= AUDIO["rmse"])
                & (error / np.maximum(rms, AUDIO["silence"]) <= AUDIO["nrmse"])
                & (np.max(np.abs(residual), axis=0) <= AUDIO["peak"])
                & ((rms <= AUDIO["silence"]) | (correlation >= AUDIO["correlation"]))
            )
            if not identity:
                self._veto(start, AUDIO["window_ms"], CHANGE_GUARD_MS)
            X = np.fft.rfft(x * self.window, axis=0) * math.sqrt(2) / self.width
            Y = np.fft.rfft(y * self.window, axis=0) * math.sqrt(2) / self.width
            A, B = np.abs(X), np.abs(Y)
            M = np.maximum(np.maximum(A - AUDIO["novelty_ratio"] * B, B - AUDIO["novelty_ratio"] * A), 0)
            self.magnitude.append(M)
            self.phase.append(np.maximum(np.abs(X - Y) - AUDIO["phase_shared_ratio"] * np.minimum(A, B), 0))
            self.levels.append(np.sqrt(np.sum(M * M, axis=0)))
            for history, floor, guard in (
                (self.magnitude, AUDIO["novelty_floor"], CHANGE_GUARD_MS),
                (self.phase, AUDIO["phase_floor"], AUDIO["short_phase_guard_ms"]),
            ):
                if len(history) == history.maxlen:
                    held = np.minimum.reduce(history)
                    if float(np.max(np.sqrt(np.sum(held * held, axis=0)))) > floor:
                        span = (history.maxlen - 1) * AUDIO["hop_ms"]
                        self._veto(start - span, span + AUDIO["window_ms"], guard)
            if (
                len(self.levels) == self.levels.maxlen
                and float(np.max(np.minimum.reduce(self.levels))) > AUDIO["moving_floor"]
            ):
                span = (self.levels.maxlen - 1) * AUDIO["hop_ms"]
                self._veto(start - span, span + AUDIO["window_ms"], AUDIO["short_phase_guard_ms"])
            self.last = start
            self.windows += 1
            consumed = index + self.hop
        self.samples += consumed
        self.tail = (left[consumed:].copy(), right[consumed:].copy())

    def finish(self):
        if self.last is None:
            return []
        extent = [
            (
                math.ceil(self.origin) + AUDIO["context_trim_ms"],
                math.floor(self.last + AUDIO["window_ms"]) - AUDIO["context_trim_ms"],
            )
        ]
        return [(a, b) for a, b in subtract(extent, self.bad) if b - a >= MIN_RUN_MS]


def union(intervals):
    result = []
    for lo, hi in sorted(intervals):
        if lo >= hi:
            continue
        if result and lo <= result[-1][1]:
            result[-1] = (result[-1][0], max(hi, result[-1][1]))
        else:
            result.append((lo, hi))
    return result


def intersect(left, right):
    a, b = union(left), union(right)
    i = j = 0
    result = []
    while i < len(a) and j < len(b):
        lo, hi = max(a[i][0], b[j][0]), min(a[i][1], b[j][1])
        if lo < hi:
            result.append((lo, hi))
        if a[i][1] <= b[j][1]:
            i += 1
        else:
            j += 1
    return result


def subtract(intervals, barriers):
    pieces = union(intervals)
    for start, end in union(barriers):
        next_pieces = []
        for lo, hi in pieces:
            if end <= lo or start >= hi:
                next_pieces.append((lo, hi))
            else:
                if lo < start:
                    next_pieces.append((lo, start))
                if end < hi:
                    next_pieces.append((end, hi))
        pieces = next_pieces
    return pieces
