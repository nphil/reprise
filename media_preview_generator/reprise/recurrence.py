"""Bounded alignment hints and exhaustive native-code-signal verification.

Descriptors suggest offsets only. Match evidence requires every native plane and
channel, witnessed temporal support, and the compatible declaration profile.
Tolerances describe decoded code values, NOT rendered/perceptual equivalence.
"""

from collections import Counter, defaultdict
from fractions import Fraction

import numpy as np

PROFILE = "native-code-signal-v1"
# Development calibration constants. Freeze these with the profile before holdout.
VIDEO_PEAK = 48.0
VIDEO_LOCAL4 = 12.0
VIDEO_LOCAL8 = 8.0
VIDEO_CONCENTRATION_LEVEL = 12.0
VIDEO_CONCENTRATION8 = 0.20
AUDIO_WINDOW_MS = 20
AUDIO_HOP_MS = 10
AUDIO_NRMSE = 0.05
AUDIO_CORRELATION = 0.998
AUDIO_ABSOLUTE_RMSE = 0.003
AUDIO_ENERGY_DELTA = 0.002
AUDIO_BAND_ABSOLUTE = 0.0002
AUDIO_BAND_RELATIVE = 0.25
AUDIO_SILENCE = 1e-7
# Conservative content/word preservation, NOT inferred timestamp uncertainty.
# This margin is independent of the maximum accepted native video frame gap.
CHANGE_GUARD_MS = 250
MIN_RUN_MS = 500
MAX_FRAME_GAP_MS = 250
COLOR_FIELDS = ("color_range", "color_space", "color_primaries", "color_transfer")
UNKNOWN_DECLARATIONS = {None, "unknown", "unspecified", "reserved", ""}


def shifts(target, reference, maximum=16):
    """Exact-frame offset hints; repetitive/black frames cannot establish anchors."""
    positions = defaultdict(list)
    for pts, digest in reference:
        if len(positions[digest]) < 33:
            positions[digest].append(pts)
    votes = Counter()
    for pts, digest in target:
        matches = positions.get(digest, ())
        if len(matches) <= 32:
            for other in matches:
                votes[round(other - pts)] += 1
    return [offset for offset, count in votes.most_common(maximum) if count >= 3]


def matching_second(target, reference, start_ms, offset_ms):
    """Exact fast path: require complete native-frame continuity including edges."""

    def section(frames, start, end):
        inside = [(p, h) for p, h in frames if start <= p < end]
        before = next(((p, h) for p, h in reversed(frames) if p < start), None)
        after = next(((p, h) for p, h in frames if p >= end), None)
        return [before, *inside, after] if inside and before and after else None

    a = section(target, Fraction(start_ms), Fraction(start_ms + 1000))
    b = section(reference, Fraction(start_ms + offset_ms), Fraction(start_ms + offset_ms + 1000))
    return bool(
        a
        and b
        and len(a) == len(b)
        and all(h == k and abs(q - p - offset_ms) <= Fraction(1, 2) for (p, h), (q, k) in zip(a, b, strict=True))
        and all(q[0] - p[0] <= MAX_FRAME_GAP_MS for p, q in zip(a, a[1:], strict=False))
    )


def plane_array(plane):
    dtype = np.dtype("u1" if plane.sample_bytes == 1 else "<u2")
    return np.ndarray(
        (plane.height, plane.width), dtype=dtype, buffer=plane.data, strides=(plane.stride_bytes, plane.sample_bytes)
    )


def declaration(value):
    return "unspecified" if value in UNKNOWN_DECLARATIONS else str(value)


def frame_declarations(frame):
    return (
        frame.width,
        frame.height,
        frame.pixel_format,
        frame.bit_depth,
        frame.sample_aspect_ratio,
        *(declaration(getattr(frame, key)) for key in COLOR_FIELDS),
        frame.chroma_location,
        frame.interpretation_flags,
    )


def descriptor(frame):
    """Small full-color spatial hint, never evidence. Native pixels still verified."""
    values = []
    for plane in frame.planes:
        data = plane_array(plane)
        # Spatially located tiles, unlike a palette histogram. Sampling is allowed
        # ONLY here: every sample is revisited by the native verifier.
        for rows in np.array_split(data, 4, axis=0):
            for tile in np.array_split(rows, 4, axis=1):
                values.append(float(tile[::4, ::4].mean()) * 255 / ((1 << frame.bit_depth) - 1))
    return np.asarray(values, dtype=np.float32)


def perceptual_shifts(target, reference, maximum=12):
    """Vote exact native-PTS offsets from coherent spatial hints, bounded memory."""
    if not target or not reference:
        return []
    right = np.stack([item[1] for item in reference])
    votes = defaultdict(list)
    # Limit proposal work; final verification has no temporal subsampling.
    step = max(1, len(target) // 96)
    for pts, feature in target[::step]:
        if float(feature.std()) < 2:
            continue
        distance = np.mean((right - feature) ** 2, axis=1)
        indices = np.argpartition(distance, min(2, len(distance) - 1))[:3]
        for idx in indices:
            if distance[idx] <= 25:
                offset = reference[int(idx)][0] - pts
                votes[round(offset * 2)].append((pts, offset))
    ordered = sorted(votes.values(), key=lambda points: (-len(points), abs(points[0][1])))
    return [
        points[len(points) // 2][1]
        for points in ordered
        if len(points) >= 3 and max(p[0] for p in points) - min(p[0] for p in points) >= 500
    ][:maximum]


def _local_max(data, size):
    """Four overlapping tile phases; every edge pixel participates, without resize."""
    worst = 0.0
    h, w = data.shape
    for oy in (0, size // 2):
        for ox in (0, size // 2):
            ys = np.unique(np.r_[0, np.arange(oy, h, size), h]).astype(int)
            xs = np.unique(np.r_[0, np.arange(ox, w, size), w]).astype(int)
            sums = np.add.reduceat(np.add.reduceat(data, ys[:-1], axis=0), xs[:-1], axis=1)
            means = sums / (np.diff(ys)[:, None] * np.diff(xs)[None, :])
            worst = max(worst, float(means.max()))
    return worst


def video_metrics(left, right):
    """Worst native-plane code residuals; no time or channel averaging."""
    if (
        left.interpretation_flags
        or right.interpretation_flags
        or frame_declarations(left) != frame_declarations(right)
        or len(left.planes) != len(right.planes)
    ):
        return None
    maxima = {"peak": 0.0, "local4": 0.0, "local8": 0.0, "concentration8": 0.0}
    scale = 255 / ((1 << left.bit_depth) - 1)
    for a, b in zip(left.planes, right.planes, strict=True):
        if (a.width, a.height, a.sample_bytes) != (b.width, b.height, b.sample_bytes):
            return None
        residual = np.abs(plane_array(a).astype(np.float32) - plane_array(b)) * scale
        maxima["peak"] = max(maxima["peak"], float(residual.max()))
        maxima["local4"] = max(maxima["local4"], _local_max(residual, 4))
        maxima["local8"] = max(maxima["local8"], _local_max(residual, 8))
        maxima["concentration8"] = max(
            maxima["concentration8"], _local_max((residual > VIDEO_CONCENTRATION_LEVEL).astype(np.float32), 8)
        )
    return maxima


def video_matches(left, right):
    metrics = video_metrics(left, right)
    return bool(
        metrics is not None
        and metrics["peak"] <= VIDEO_PEAK
        and metrics["local4"] <= VIDEO_LOCAL4
        and metrics["local8"] <= VIDEO_LOCAL8
        and metrics["concentration8"] <= VIDEO_CONCENTRATION8
    )


def audio_metrics(left, right, rate):
    """20ms per-channel native waveform AND spectrum; no gain/lag/downmix fitting."""
    if left.shape != right.shape or not len(left) or not np.isfinite(left).all() or not np.isfinite(right).all():
        return None
    x = np.sqrt(np.mean(left * left, axis=0))
    y = np.sqrt(np.mean(right * right, axis=0))
    error = np.sqrt(np.mean((left - right) ** 2, axis=0))
    scale = np.maximum(x, y)
    correlation = np.sum(left * right, axis=0) / np.maximum(
        np.sqrt(np.sum(left * left, axis=0) * np.sum(right * right, axis=0)), 1e-30
    )
    silent = scale <= AUDIO_SILENCE
    correlation[silent] = np.where(error[silent] <= AUDIO_SILENCE, 1.0, -1.0)
    window = np.hanning(len(left))[:, None]
    a = np.fft.rfft(left * window, axis=0)
    b = np.fft.rfft(right * window, axis=0)
    frequency = np.fft.rfftfreq(len(left), 1 / rate)
    bands = (0, 200, 500, 1000, 2000, 4000, 8000, 12000, rate / 2 + 1)
    spectral = []
    for lo, hi in zip(bands, bands[1:], strict=False):
        selected = (frequency >= lo) & (frequency < hi)
        if not selected.any():
            continue
        band_a = np.sqrt(2 * np.sum(np.abs(a[selected]) ** 2, axis=0)) / len(left)
        band_b = np.sqrt(2 * np.sum(np.abs(b[selected]) ** 2, axis=0)) / len(left)
        residual = np.sqrt(2 * np.sum((np.abs(a[selected]) - np.abs(b[selected])) ** 2, axis=0)) / len(left)
        spectral.append((residual, residual / np.maximum(np.maximum(band_a, band_b), AUDIO_SILENCE)))
    return {
        "rmse": error,
        "nrmse": error / np.maximum(scale, AUDIO_SILENCE),
        "correlation": correlation,
        "energy_delta": np.abs(x - y),
        "spectral": spectral,
    }


def audio_matches(left, right, rate):
    m = audio_metrics(left, right, rate)
    return bool(
        m is not None
        and np.all(m["rmse"] <= AUDIO_ABSOLUTE_RMSE)
        and np.all(m["nrmse"] <= AUDIO_NRMSE)
        and np.all(m["correlation"] >= AUDIO_CORRELATION)
        and np.all(m["energy_delta"] <= AUDIO_ENERGY_DELTA)
        and all(
            not np.any((absolute > AUDIO_BAND_ABSOLUTE) & (relative > AUDIO_BAND_RELATIVE))
            for absolute, relative in m["spectral"]
        )
    )


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
