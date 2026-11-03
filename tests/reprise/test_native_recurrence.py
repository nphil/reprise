"""Verifier unit contracts; end-to-end codec fitness is evaluated separately."""

from contextlib import contextmanager
from dataclasses import replace
from fractions import Fraction

import numpy as np
import pytest

from media_preview_generator.reprise import analysis, media, recurrence
from media_preview_generator.reprise.identity import media_version


def frame(pts=0, *, flags=()):
    arrays = [
        np.full((32, 32), 100, dtype=np.uint8),
        np.full((16, 16), 128, dtype=np.uint8),
        np.full((16, 16), 128, dtype=np.uint8),
    ]
    planes = tuple(media.NativePlane(memoryview(a.tobytes()), a.shape[1], a.shape[0], a.shape[1], 1) for a in arrays)
    return media.NativeFrame(
        Fraction(pts),
        planes,
        32,
        32,
        "yuv420p",
        8,
        "unknown",
        "unknown",
        "unknown",
        "unknown",
        "left",
        sample_aspect_ratio=Fraction(1),
        interpretation_flags=flags,
    )


@pytest.mark.parametrize("plane_index", [1, 2])
def test_chroma_only_scene_change_is_rejected(plane_index):
    original = frame()
    source = original.planes[plane_index]
    data = np.full((source.height, source.width), 250, dtype=np.uint8)
    planes = list(original.planes)
    planes[plane_index] = media.NativePlane(memoryview(data.tobytes()), source.width, source.height, source.width, 1)
    assert not recurrence.video_matches(original, replace(original, planes=tuple(planes)))


def test_matching_unspecified_is_not_guessed_rendering_and_asymmetry_fails():
    assert recurrence.video_matches(frame(), frame())
    assert not recurrence.video_matches(frame(), replace(frame(), color_space="bt709"))
    assert not recurrence.video_matches(frame(), replace(frame(), sample_aspect_ratio=Fraction(2)))


def test_opposite_sign_stereo_change_cannot_cancel_in_verifier():
    t = np.arange(48000) / 48000
    baseline = np.column_stack((0.1 * np.sin(2 * np.pi * 220 * t), 0.1 * np.sin(2 * np.pi * 220 * t)))
    voice = 0.03 * np.sin(2 * np.pi * 1600 * t)
    changed = baseline + np.column_stack((voice, -voice))
    np.testing.assert_allclose(changed.sum(axis=1), baseline.sum(axis=1), atol=1e-16)
    equal = recurrence.AudioVerifier(48000, Fraction(0))
    equal.feed(baseline, baseline)
    assert equal.finish() == [(40, 960)]
    different = recurrence.AudioVerifier(48000, Fraction(0))
    different.feed(baseline, changed)
    assert different.finish() == []


def test_entire_failed_overlap_is_removed_without_gap_filling():
    good = [(Fraction(0), Fraction(20)), (Fraction(10), Fraction(30)), (Fraction(30), Fraction(50))]
    assert recurrence.subtract(good, [(Fraction(15), Fraction(35))]) == [(0, 15), (35, 50)]
    assert recurrence.intersect([(0, 100)], [(10, 20), (30, 40)]) == [(10, 20), (30, 40)]


class Frames:
    """What analysis sees of a native frame reader: an iterator that knows its stream's time base."""

    def __init__(self, frames, time_base=Fraction(1, 1000)):
        self.frames, self.time_base = iter(frames), time_base

    def __iter__(self):
        return self

    def __next__(self):
        return next(self.frames)


@pytest.mark.parametrize("failing_side", ["target", "reference"])
def test_failure_after_last_native_yield_discards_all_staged_video(tmp_path, monkeypatch, failing_side):
    items = {}
    for side in ("target", "reference"):
        path = tmp_path / side
        path.write_bytes(side.encode())
        items[side] = media_version(str(path), 1000, "{}")
    exhausted = []

    @contextmanager
    def native(item, *args, **kwargs):
        side = next(k for k, v in items.items() if v.id == item.id)

        def records():
            for pts in range(0, 1200, 40):
                yield frame(pts)
            exhausted.append(side)

        yield Frames(records())
        if side == failing_side:
            raise media.DecodeError("failure after final yielded frame")

    monkeypatch.setattr(media, "native_frames", native)
    with pytest.raises(media.DecodeError, match="after final"):
        analysis._video_support(
            items["target"],
            items["reference"],
            0,
            0,
            Fraction(0),
            Fraction(1000),
            Fraction(0),
            lambda: False,
            lambda: False,
            1,
        )
    assert "target" in exhausted and "reference" in exhausted


@pytest.mark.parametrize("tick,confirmed", [(Fraction(1, 1000), True), (Fraction(1, 24000), False)])
def test_frames_pair_within_one_container_tick(tmp_path, monkeypatch, tick, confirmed):
    """Two 24000/1001 fps Matroska files of the same material whose frame grids sit half a millisecond apart keep
    whole-millisecond timestamps that land 0 or 1 ms off each other, alternating (real Blu-ray rips do). Pairing
    within one tick of the stream confirms them; with a finer time base the same 1 ms is a real disagreement."""
    paths = [tmp_path / name for name in ("target", "reference")]
    for path in paths:
        path.write_bytes(path.name.encode())
    target, reference = [media_version(str(path), 3000, "{}") for path in paths]
    cadence = Fraction(1001, 24)
    target_times = [round(n * cadence) for n in range(70)]
    reference_times = [round(n * cadence + Fraction(41, 2)) for n in range(70)]
    assert {b - a for a, b in zip(target_times, reference_times, strict=True)} == {20, 21}

    @contextmanager
    def native(item, *args, **kwargs):
        yield Frames((frame(t) for t in (target_times if item.id == target.id else reference_times)), tick)

    monkeypatch.setattr(media, "native_frames", native)
    support = analysis._video_support(
        target, reference, 0, 0, Fraction(0), Fraction(2800), Fraction(20), lambda: False, lambda: False, 1
    )
    assert bool(support) is confirmed, support
    if confirmed:
        # one unbroken run: a rounding flip between consecutive pairs must not leave a millisecond hole
        assert len(support) == 1 and support[0][0] <= 50 and support[0][1] >= 2700, support


def test_fractional_offsets_remain_exact_proposal_hints():
    features = [(Fraction(i * 1000, 24), np.arange(48, dtype=np.float32) + i * 20, 20) for i in range(20)]
    offset = Fraction(1000, 24)
    shifted = [(pts + offset, feature, std) for pts, feature, std in features]
    assert offset in recurrence.perceptual_shifts(features, shifted)


@pytest.mark.parametrize("chunk_size", [137, 441, 4410, 22050])
def test_audio_chunk_boundaries_preserve_global_window_grid(chunk_size):
    rate = 44100
    t = np.arange(rate * 2) / rate
    baseline = np.column_stack([0.1 * np.sin(2 * np.pi * f * t) for f in (220, 330, 440)])
    changed = baseline.copy()
    changed[rate : rate + 1455, 2] += 0.015 * np.sin(2 * np.pi * 1600 * t[:1455])
    whole = recurrence.AudioVerifier(rate, Fraction(1, 3))
    whole.feed(baseline, changed)
    chunks = recurrence.AudioVerifier(rate, Fraction(1, 3))
    for start in range(0, len(t), chunk_size):
        chunks.feed(baseline[start : start + chunk_size], changed[start : start + chunk_size])
    assert chunks.finish() == whole.finish()
    assert chunks.bad == whole.bad
    assert chunks.windows == whole.windows
    assert not any(lo < Fraction(1033) and hi > 1000 for lo, hi in chunks.finish())


def test_phase_branch_preserves_quiet_opposite_phase_channel():
    rate = 48000
    t = np.arange(rate) / rate
    carrier = 0.1 * np.sin(2 * np.pi * 220 * t)
    quiet = 0.01 * np.sin(2 * np.pi * 1600 * t)
    left = np.column_stack((carrier, carrier + quiet))
    right = np.column_stack((carrier, carrier - quiet))
    verifier = recurrence.AudioVerifier(rate, Fraction(0))
    verifier.feed(left, right)
    assert verifier.finish() == []


def test_anchors_require_disjoint_native_groups_and_real_span():
    rows = [(Fraction(i * 1000, 24), np.full(48, i, dtype=np.float32), 20) for i in range(30)]
    anchored = recurrence.anchor(rows, rows, 0, 1250, 0)
    assert anchored is not None
    start, end, proof = anchored
    assert start == 0 and end == 1208
    assert set(proof["start_pts_ms"]).isdisjoint(proof["end_pts_ms"])
    assert max(map(Fraction, proof["start_pts_ms"])) < min(map(Fraction, proof["end_pts_ms"]))
    assert recurrence.anchor(rows, rows, 0, 500, 0) is None
    flat = [(t, feature, 0) for t, feature, _ in rows]
    assert recurrence.anchor(flat, flat, 0, 1250, 0) is None


def test_reserved_color_declarations_are_not_unspecified():
    with pytest.raises(ValueError, match="declaration"):
        recurrence.frame_declarations(replace(frame(), color_space="reserved"))


def test_variable_pts_and_inserted_reference_record_veto_native_support(tmp_path, monkeypatch):
    paths = [tmp_path / name for name in ("target", "reference")]
    for path in paths:
        path.write_bytes(path.name.encode())
    target, reference = [media_version(str(path), 3000, "{}") for path in paths]
    times = [0]
    for i in range(29):
        times.append(times[-1] + (80 if i % 2 else 120))
    ref_times = sorted([*times, 1450])

    @contextmanager
    def native(item, *args, **kwargs):
        yield Frames(frame(t) for t in (times if item.id == target.id else ref_times))

    monkeypatch.setattr(media, "native_frames", native)
    support = analysis._video_support(
        target, reference, 0, 0, Fraction(0), Fraction(2900), Fraction(0), lambda: False, lambda: False, 1
    )
    assert support
    assert not any(lo < 1500 and hi > 1400 for lo, hi in support)


def _noise(samples, channels, seed):
    return np.random.default_rng(seed).standard_normal((samples, channels)) * 0.1


def test_sound_lag_is_the_exact_sample_lag_despite_gain_and_codec_noise():
    rate, search = 48000, 250 * 48
    source = _noise(20 * rate, 6, 3)
    source[:, 3] *= 0.01  # a near-silent LFE must not steer the search
    true = search + 7755  # 161.5625 ms later than the picture predicts
    probe = source[true : true + 12 * rate] * 1.108 + _noise(12 * rate, 6, 4) * 0.2
    lag, score = recurrence.sound_lag(probe, source[: 12 * rate + 2 * search], search, rate)
    assert lag == true and score > 0.95


def test_sound_lag_finds_a_shared_stretch_inside_a_probe_that_is_mostly_different():
    rate, search = 48000, 250 * 48
    source = _noise(16 * rate, 2, 5)
    true = search + 4800  # 100 ms later than the picture predicts
    probe = _noise(12 * rate, 2, 6)  # 12 s of different sound ...
    probe[5 * rate : 7 * rate] = source[true + 5 * rate : true + 7 * rate]  # ... except two seconds in the middle
    lag, score = recurrence.sound_lag(probe, source[: 12 * rate + 2 * search], search, rate)
    assert lag == true and score > 0.99


def test_sound_lag_prefers_the_lag_nearest_the_picture_among_exact_ties():
    period = np.sin(2 * np.pi * np.arange(60000) / 100.0)[:, None]  # a whole number of samples per period: exact ties
    assert recurrence.sound_lag(period[:40000], period, 12300, 40000) == (12300, pytest.approx(1.0))
    assert recurrence.sound_lag(period[:40000], period, 12349, 40000)[0] == 12300
    assert recurrence.sound_lag(period[:40000], period, 12351, 40000)[0] == 12400


def test_sound_lag_reports_no_lag_for_silence_and_rejects_malformed_probes():
    assert recurrence.sound_lag(np.zeros((1000, 2)), np.zeros((2000, 2)), 0, 500) is None
    assert recurrence.sound_lag(np.zeros((1000, 2)), _noise(2000, 2, 1), 0, 500) is None
    with pytest.raises(ValueError):
        recurrence.sound_lag(_noise(2000, 2, 1), _noise(1000, 2, 2), 0, 500)
    with pytest.raises(ValueError):
        recurrence.sound_lag(_noise(1000, 2, 1), _noise(2000, 3, 2), 0, 500)
    with pytest.raises(ValueError):
        recurrence.sound_lag(_noise(1000, 2, 1), _noise(2000, 2, 2), 0, 0)


def _signed_local_with_its_own_integral(data, size):
    """The pre-shared-integral formula, kept as the oracle: the shared integral must be bit-identical to it."""
    h, w = data.shape
    if h < size or w < size:
        return float(abs(data.mean()))
    ys = np.unique(np.r_[np.arange(0, h - size + 1, size // 2), h - size])
    xs = np.unique(np.r_[np.arange(0, w - size + 1, size // 2), w - size])
    integral = np.pad(data.cumsum(0, dtype=np.float64).cumsum(1), ((1, 0), (1, 0)))
    sums = (
        integral[(ys + size)[:, None], xs + size]
        - integral[ys[:, None], xs + size]
        - integral[(ys + size)[:, None], xs]
        + integral[ys[:, None], xs]
    )
    return float(np.max(np.abs(sums)) / (size * size))


@pytest.mark.parametrize("depth,shape", [(8, (33, 47)), (10, (7, 90)), (10, (135, 240)), (8, (5, 5))])
def test_shared_integral_signed_tiles_are_bit_identical(depth, shape):
    rng = np.random.default_rng(depth * 1000 + shape[0])
    left = rng.integers(0, 1 << depth, size=shape).astype(np.float32)
    right = np.clip(left + rng.integers(-3, 4, size=shape), 0, (1 << depth) - 1).astype(np.float32)
    residual = (left - right) * 255 / ((1 << depth) - 1)
    integral = recurrence._integral(residual)
    for size in (8, 16, 32):
        assert recurrence._signed_local(residual, size, integral) == _signed_local_with_its_own_integral(residual, size)
