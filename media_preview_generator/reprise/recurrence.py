"""Bounded gates on the canonical picture and sound, and disjoint alignment anchors.

Hints never constitute support. Decode lifecycle and generation checks belong to
analysis; this module retains only descriptors, interval vetoes and rolling spectra.
"""

import bisect
import functools
import math
from collections import defaultdict, deque
from fractions import Fraction
from typing import NamedTuple

import numpy as np

from . import canonical
from .verification_profile import PROFILE_SPEC

CANDIDATE = PROFILE_SPEC["candidate"]
VIDEO = PROFILE_SPEC["video"]
AUDIO = PROFILE_SPEC["audio"]
ANCHORS = PROFILE_SPEC["anchors"]
SOUND = PROFILE_SPEC["sound_offset"]
TIMING = PROFILE_SPEC["timing"]
COLOR_FIELDS = tuple(PROFILE_SPEC["metadata"]["color_fields"])
CHANGE_GUARD_MS = PROFILE_SPEC["support"]["guard_ms"]
MIN_RUN_MS = PROFILE_SPEC["support"]["minimum_run_ms"]
MAX_FRAME_GAP_MS = VIDEO["max_frame_gap_ms"]
PICTURE_METRICS = ("peak", "local4", "local8", "concentration8", "signed8", "signed16", "signed32")
NEAR_RATIO = VIDEO["near_ratio"]
NEAR_SHARE = Fraction(VIDEO["near_share"])
NEAR_REACH_MS = Fraction(VIDEO["near_window_ms"], 2)


class Clock(NamedTuple):
    """Where a reference shows what the target shows: reference time = ``rate`` x target time + ``offset`` (ms).
    The rate is 1 unless the two files play the footage at different speeds (a 25 fps copy of film-rate footage)."""

    rate: Fraction
    offset: Fraction

    def at(self, time):
        """The reference time of a target time."""
        return self.rate * time + self.offset

    def back(self, time):
        """The target time of a reference time."""
        return (time - self.offset) / self.rate


def speeds(target_fps, reference_fps):
    """The speeds a reference may play the target's footage at, as reference time per target time: the same speed,
    and the ratio of the two frame rates when that is a speed change a transfer makes (within the profile's
    tolerance of 1: film rate against 25 fps, 23.976 against 24, 29.97 against 30). A larger ratio is the same speed
    with repeated frames (23.976 against 29.97), never another speed."""
    ratio = Fraction(target_fps) / Fraction(reference_fps)
    if ratio != 1 and abs(ratio - 1) <= Fraction(TIMING["speed_tolerance"]):
        return [Fraction(1), ratio]
    return [Fraction(1)]


def hold(rate, target_fps, reference_fps):
    """How long a frame of the slower stream stays up (ms) when the two files do not run frame for frame at ``rate``:
    a frame's partner is then any frame the other file shows meanwhile. Zero when they do run frame for frame (one
    reference frame per target frame), where a partner sits within a container tick of where the clock puts it."""
    mapped, own = Fraction(rate) * 1000 / Fraction(target_fps), Fraction(1000) / Fraction(reference_fps)
    return Fraction(0) if mapped == own else max(mapped, own)


def frame_declarations(frame):
    return (
        frame.width,
        frame.height,
        frame.pixel_format,
        frame.bit_depth,
        frame.sample_aspect_ratio,
        *(canonical.declaration(getattr(frame, key), key) for key in COLOR_FIELDS),
        frame.chroma_location,
        frame.interpretation_flags,
    )


def descriptor(picture):
    """4x4 tile means of each canonical plane: where a frame may recur, never whether it does."""
    values = []
    for plane in picture:
        for rows in np.array_split(plane, 4, axis=0):
            for tile in np.array_split(rows, 4, axis=1):
                values.append(tile.mean(dtype=np.float64))
    return np.asarray(values, dtype=np.float32)


def spatial_information(picture):
    """The largest within-plane population standard deviation of a canonical picture: a flat frame (black, a fade)
    recurs everywhere and is never a hint."""
    return max(float(plane.std(dtype=np.float64)) for plane in picture)


def perceptual_shifts(target, reference, speeds=((Fraction(1), Fraction(0)),)):
    """Up to maximum_offsets clocks at which sampled target hints recur in the reference, most votes first, each with
    the hold of its speed: ``speeds`` are the ``(rate, hold)`` to try (see ``speeds`` and ``hold``).

    A vote is a sampled target frame and one of its nearest reference descriptors; votes agree when their offsets at
    a rate fall in one bucket. Frame for frame at the same speed an offset repeats exactly, so the bucket is the
    profile's fraction of a millisecond; at another speed both timestamps' rounding moves it (``speed_vote_spread_ms``);
    with different cadences the same frame is up for a hold, and the bucket is that wide."""
    if not target or not reference:
        return []
    right = np.stack([r[1] for r in reference])
    pairs = []
    for pts, feature, std in target[:: max(1, len(target) // CANDIDATE["sample_count"])]:
        if std < CANDIDATE["plane_std"]:
            continue
        distance = np.mean((right - feature) ** 2, axis=1)
        count = min(CANDIDATE["neighbors"], len(reference))
        for idx in np.argpartition(distance, count - 1)[:count]:
            if reference[int(idx)][2] < CANDIDATE["plane_std"] or distance[idx] > CANDIDATE["descriptor_mse"]:
                continue
            pairs.append((pts, reference[int(idx)][0]))
    found = []
    for rate, held in speeds:
        per_ms = (
            1 / Fraction(held)
            if held
            else Fraction(CANDIDATE["bucket_per_ms"])
            if rate == 1
            else 1 / Fraction(TIMING["speed_vote_spread_ms"])
        )
        votes = defaultdict(list)
        for pts, reference_pts in pairs:
            offset = reference_pts - rate * pts
            votes[round(offset * per_ms)].append((pts, offset))
        for group in votes.values():
            if (
                len(group) >= CANDIDATE["minimum_votes"]
                and max(q[0] for q in group) - min(q[0] for q in group) >= CANDIDATE["minimum_vote_span_ms"]
            ):
                found.append((group, rate, held))
    found.sort(key=lambda entry: (-len(entry[0]), abs(entry[0][0][1])))
    return [
        (Clock(rate, sorted(q[1] for q in group)[len(group) // 2]), held)
        for group, rate, held in found[: CANDIDATE["maximum_offsets"]]
    ]


def sound_lag(target, reference, nearest, block):
    """Where the longer ``reference`` sound best contains ``target`` (both canonical mono), as ``(lag, score, gain)``:
    the lag in samples, to a fraction of one, the correlation there, and the factor that brings the reference to the
    target's level. The target is cut into blocks of ``block`` samples (a shorter last block is dropped unless it is
    the only one); each block's normalised cross-correlation is taken over every whole-sample lag the reference
    allows, and the block with the highest peak decides: a probe that straddles shared and unshared sound still has
    blocks wholly inside the shared part. Among that block's scores within the profile's tie of its peak, the lag
    nearest ``nearest`` (the lag the picture predicts) wins, and a parabola through it and its two neighbours places
    the peak between samples. ``None`` when nothing audible can be correlated. A lag is a proposal, never support."""
    x, y = np.asarray(target, dtype=np.float64), np.asarray(reference, dtype=np.float64)
    if x.ndim != 1 or y.ndim != 1 or len(x) == 0 or len(y) < len(x) or block <= 0:
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
        squares = np.concatenate(([0.0], np.cumsum(ys * ys)))
        windows = squares[len(xs) : len(xs) + lags] - squares[:lags]
        audible = windows > floor
        if energy <= floor or not audible.any():
            continue
        size = 1 << (len(ys) - 1).bit_length()
        correlation = np.fft.irfft(np.fft.rfft(ys, size) * np.conj(np.fft.rfft(xs, size)), size)[:lags]
        scores = np.where(audible, correlation / np.sqrt(np.maximum(windows, floor) * energy), -1.0)
        peak = float(scores.max())
        if best is None or peak > best[1]:
            candidates = np.flatnonzero(scores >= peak - SOUND["tie"])
            lag = int(candidates[np.argmin(np.abs(candidates - nearest))])
            fraction = 0.0
            if 0 < lag < lags - 1 and audible[lag - 1] and audible[lag + 1]:
                before, here, after = (float(scores[lag + step]) for step in (-1, 0, 1))
                bend = before - 2 * here + after
                if bend < 0:
                    fraction = max(-0.5, min(0.5, 0.5 * (before - after) / bend))
            best = (lag + fraction, peak, float(correlation[lag] / windows[lag]))
    return best


def agreeing_frames(target, reference, lo, hi, clock, slack):
    """The target hints in [lo, hi) whose descriptor agrees with a reference hint within ``slack`` ms of where
    ``clock`` puts it, as (pts, descriptor). Agreement places anchors and the sound probe; it is never support."""
    times = [t for t, _, _ in reference]
    points = []
    for t, feature, std in target:
        if not lo <= t < hi or std < CANDIDATE["plane_std"]:
            continue
        wanted = clock.at(t)
        for index in range(bisect.bisect_left(times, wanted - slack), len(times)):
            if times[index] > wanted + slack:
                break
            _, other, other_std = reference[index]
            if other_std >= CANDIDATE["plane_std"] and np.mean((feature - other) ** 2) <= CANDIDATE["descriptor_mse"]:
                points.append((t, feature))
                break
    return points


def anchor(target, reference, lo, hi, clock, slack):
    points = agreeing_frames(target, reference, lo, hi, clock, slack)
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


def _integral(data):
    """Inclusive float64 prefix sums with a zero row and column: every tile sum is four lookups."""
    h, w = data.shape
    integral = np.zeros((h + 1, w + 1), dtype=np.float64)
    integral[1:, 1:] = data
    np.cumsum(integral, axis=1, out=integral)
    np.cumsum(integral, axis=0, out=integral)
    return integral


def _tile_sums(integral, ys, xs, height, width):
    """The sums over the tiles whose top-left corners are ``ys`` x ``xs`` and whose sizes are ``height`` x ``width``
    (scalars or one per corner)."""
    bottom, right = (ys + height)[:, None], (xs + width)[None, :]
    return (
        integral[bottom, right]
        - integral[ys[:, None], right]
        - integral[bottom, xs[None, :]]
        + integral[ys[:, None], xs]
    )


@functools.lru_cache(maxsize=256)
def _grid(length, size, offset):
    """Where the tiles of one phase start along an axis and how long each is: whole tiles from ``offset``, the cells
    before it and after the last whole tile as partial tiles of their actual length."""
    edges = np.unique(np.r_[0, np.arange(offset, length, size), length])
    return edges[:-1], np.diff(edges)


def _local_max(integral, shape, size):
    """The largest tile mean over four grids of ``size`` tiles (offsets 0 and half a tile each way)."""
    worst = 0.0
    for oy in (0, size // 2):
        ys, heights = _grid(shape[0], size, oy)
        for ox in (0, size // 2):
            xs, widths = _grid(shape[1], size, ox)
            sums = _tile_sums(integral, ys, xs, heights, widths)
            worst = max(worst, float((sums / (heights[:, None] * widths[None, :])).max()))
    return worst


@functools.lru_cache(maxsize=256)
def _strides(length, size):
    """Where whole ``size`` windows start along an axis: every half window, and the one that ends at the far edge."""
    return np.unique(np.r_[np.arange(0, length - size + 1, size // 2), length - size])


def _signed_local(data, size, integral):
    """The largest absolute signed mean over whole ``size`` windows (the plane's own mean when it is smaller)."""
    h, w = data.shape
    if h < size or w < size:
        return float(abs(data.mean()))
    sums = _tile_sums(integral, _strides(h, size), _strides(w, size), size, size)
    return float(np.max(np.abs(sums)) / (size * size))


def _plane_metrics(left, right):
    """One plane pair's metrics, cheapest first, each computed only when asked for (a generator)."""
    residual = left - right
    absolute = np.abs(residual)
    yield "peak", float(absolute.max())
    integral = _integral(absolute)
    for size in (4, 8):
        yield "local" + str(size), _local_max(integral, absolute.shape, size)
    yield "concentration8", _local_max(_integral(absolute > VIDEO["concentration_level"]), absolute.shape, 8)
    integral = _integral(residual)
    for size in (8, 16, 32):
        yield "signed" + str(size), _signed_local(residual, size, integral)


def picture_metrics(left, right):
    """Every metric's worst value over the planes of two canonical pictures of one grid: the numbers behind a
    verdict and the reference ``picture_excess`` is tested against."""
    maxima = dict.fromkeys(PICTURE_METRICS, 0.0)
    for a, b in zip(left, right, strict=True):
        if a.shape != b.shape:
            raise ValueError("Canonical pictures are not on one grid")
        for name, value in _plane_metrics(a, b):
            maxima[name] = max(maxima[name], value)
    return maxima


def picture_excess(left, right):
    """How far two canonical pictures are from agreeing: the largest ratio of a metric to its limit over the planes.
    At most 1 is agreement within every limit; above ``near_ratio`` the pictures differ, and the answer is then only
    known to be above it (the remaining metrics cannot change the verdict, so they are not computed: a neighbouring
    frame or another scene is decided by the first metric)."""
    worst = 0.0
    for a, b in zip(left, right, strict=True):
        if a.shape != b.shape:
            raise ValueError("Canonical pictures are not on one grid")
        for name, value in _plane_metrics(a, b):
            worst = max(worst, value / VIDEO[name])
            if worst > NEAR_RATIO:
                return worst
    return worst


def agreeing(times, ratios):
    """Per frame, whether its picture counts as the reference's: ``ratios`` are the frames' ``picture_excess`` (None
    for a frame with no partner) at the increasing ``times`` (ms).

    A frame within every limit agrees. A near miss (above a limit by at most ``near_ratio``) agrees only as an
    isolated one: when at most ``near_share`` of the frames within half ``near_window_ms`` of it, itself included,
    are outside the limits. Two encodes of one picture miss a limit in single frames (a fade, a flash); footage that
    really differs misses in every frame, and a frame that differs outright (another scene) never agrees."""
    outside = [ratio is None or ratio > 1 for ratio in ratios]
    result = []
    first = last = 0  # the frames within reach of frame i are first..last-1
    misses = 0
    for i, ratio in enumerate(ratios):
        while last < len(times) and times[last] <= times[i] + NEAR_REACH_MS:
            misses += outside[last]
            last += 1
        while times[first] < times[i] - NEAR_REACH_MS:
            misses -= outside[first]
            first += 1
        result.append(
            ratio is not None and (ratio <= 1 or (ratio <= NEAR_RATIO and misses <= NEAR_SHARE * (last - first)))
        )
    return result


class AudioVerifier:
    """One exact sample grid, rolling spectra, and a permanent union of vetoes.

    A spectral difference is judged against how loud the shared sound is: two encodes or mixes of one recording
    differ in proportion to the sound itself, a sound only one of them has does not shrink with it. Each limit is
    therefore its floor or its share of the quietest window the difference is held across, whichever is larger.
    """

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
        self.loud = deque(maxlen=max(AUDIO["novelty_windows"], AUDIO["phase_windows"], AUDIO["moving_windows"]))
        self.bad = []
        self.last = None

    def _veto(self, start, duration, guard):
        lo, hi = math.floor(start) - guard, math.ceil(start + duration) + guard
        # Branch witnesses arrive in different temporal order; retain a union,
        # never allow a later pass to erase an earlier witnessed veto.
        self.bad = union([*self.bad, (lo, hi)])

    def _limit(self, floor, share, windows):
        """Per channel, what a difference held across the last ``windows`` windows may not exceed."""
        return np.maximum(floor, share * np.minimum.reduce(list(self.loud)[-windows:]))

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
            self.loud.append(rms)
            for history, floor, share, guard in (
                (self.magnitude, AUDIO["novelty_floor"], AUDIO["novelty_level"], CHANGE_GUARD_MS),
                (self.phase, AUDIO["phase_floor"], AUDIO["phase_level"], AUDIO["short_phase_guard_ms"]),
            ):
                if len(history) == history.maxlen:
                    held = np.minimum.reduce(history)
                    if np.any(np.sqrt(np.sum(held * held, axis=0)) > self._limit(floor, share, history.maxlen)):
                        span = (history.maxlen - 1) * AUDIO["hop_ms"]
                        self._veto(start - span, span + AUDIO["window_ms"], guard)
            if len(self.levels) == self.levels.maxlen and np.any(
                np.minimum.reduce(self.levels)
                > self._limit(AUDIO["moving_floor"], AUDIO["moving_level"], self.levels.maxlen)
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
