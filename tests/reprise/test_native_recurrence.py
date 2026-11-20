"""Verifier unit contracts on the canonical picture and sound; end-to-end fitness is evaluated on real files.

Numbers quoted as measured come from canonical pictures and sounds of real library files (see the calibration notes
in ``verification_profile``); everything computed here is synthetic and deterministic.
"""

from contextlib import contextmanager
from dataclasses import replace
from fractions import Fraction

import numpy as np
import pytest

from media_preview_generator.reprise import analysis, canonical, media, recurrence
from media_preview_generator.reprise.identity import media_version

RATE = canonical.SOUND_RATE
SAME_SPEED = recurrence.Clock(Fraction(1), Fraction(0))


def frame(pts=0, *, flags=(), luma=100):
    arrays = [
        np.full((32, 32), luma, dtype=np.uint8),
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


FRAMING = canonical.regions((32, 32, 1), (32, 32, 1))


# ---- the picture limits -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("plane_index", [1, 2])
def test_chroma_only_scene_change_is_rejected(plane_index):
    original = frame()
    source = original.planes[plane_index]
    data = np.full((source.height, source.width), 250, dtype=np.uint8)
    planes = list(original.planes)
    planes[plane_index] = media.NativePlane(memoryview(data.tobytes()), source.width, source.height, source.width, 1)
    changed = replace(original, planes=tuple(planes))
    assert recurrence.picture_excess(canonical.picture(original), canonical.picture(original)) == 0
    assert recurrence.picture_excess(canonical.picture(original), canonical.picture(changed)) > recurrence.NEAR_RATIO


def test_frames_of_another_colour_class_or_framing_are_not_compared():
    declared = recurrence.frame_declarations(frame())
    assert analysis._framing(declared, declared) == FRAMING
    with pytest.raises(analysis._PairUnsupported):  # undeclared at this size is read as bt601, never guessed equal
        analysis._framing(declared, recurrence.frame_declarations(replace(frame(), color_space="bt709")))
    with pytest.raises(analysis._PairUnsupported):
        analysis._framing(declared, recurrence.frame_declarations(replace(frame(), sample_aspect_ratio=Fraction(2))))
    with pytest.raises(analysis._PairUnsupported):
        analysis._framing(declared, recurrence.frame_declarations(replace(frame(), color_transfer="smpte2084")))


def test_the_limits_sit_between_measured_same_footage_and_measured_different_footage():
    """Largest clean values over about 10,300 frame pairs of the same footage in two files (same encode, 8-bit
    against 10-bit, Blu-ray against web, 1080p against 720p, 24 against 25 fps, cropped against uncropped), and the
    smallest excess measured between different footage."""
    same = {"peak": 68.25, "local4": 15.71, "local8": 10.23, "concentration8": 0.44}
    same |= {"signed8": 6.78, "signed16": 4.49, "signed32": 3.09}  # the binding ones: their 99th percentile
    assert set(same) == set(recurrence.PICTURE_METRICS)
    for name, value in same.items():
        assert value < recurrence.VIDEO[name] <= 1.6 * value, name
    for different in (22, 6.45, 2.6):  # another scene, another title card, one episode's end logos against another's
        assert different > recurrence.NEAR_RATIO


FILM = Fraction(1001, 24)  # ms per frame at 24000/1001 fps


def times(count):
    return [n * FILM for n in range(count)]


def test_an_isolated_near_miss_does_not_break_a_run_and_a_frame_that_differs_does():
    """Measured between a Blu-ray and a web copy of one title sequence: 0.90 at most, but for two single frames at
    1.06 and 1.53; between an 8-bit and a 10-bit copy single fade frames reach 2.14 to 2.84."""
    ratios = [0.5] * 96
    ratios[20], ratios[60] = 1.06, 1.53
    assert all(recurrence.agreeing(times(96), ratios))
    ratios[40] = 2.14  # past near_ratio: a frame that differs, however isolated
    verdicts = recurrence.agreeing(times(96), ratios)
    assert verdicts.count(False) == 1 and not verdicts[40]
    ratios[41] = None  # a frame with no partner never agrees either
    assert recurrence.agreeing(times(96), ratios).count(False) == 2


def test_near_misses_count_only_while_they_are_few_of_the_frames_around_them():
    """Footage that really differs misses in every frame (measured: 2.6 to 3.0 throughout for the nearest case,
    never a mix of passes and near misses); a share above a quarter of the frames within half a second is refused."""
    assert recurrence.NEAR_SHARE == Fraction(1, 4) and recurrence.NEAR_REACH_MS == 500
    steady = [1.2] * 96
    assert not any(recurrence.agreeing(times(96), steady))
    alternating = [1.2 if n % 2 else 0.5 for n in range(96)]
    verdicts = recurrence.agreeing(times(96), alternating)
    assert all(verdicts[n] for n in range(0, 96, 2)) and not any(verdicts[n] for n in range(1, 96, 2))
    # 23 frames lie within half a second of a frame here: 5 misses are within a quarter of them, 6 are not
    five = [1.2 if 40 <= n < 45 else 0.5 for n in range(96)]
    assert all(recurrence.agreeing(times(96), five))
    six = [1.2 if 40 <= n < 46 else 0.5 for n in range(96)]
    verdicts = recurrence.agreeing(times(96), six)
    assert not any(verdicts[40:46]) and all(verdicts[:40]) and all(verdicts[46:])


# ---- the picture over time --------------------------------------------------------------------------------------------


class Frames:
    """What analysis sees of a native frame reader: an iterator that knows its stream's time base."""

    def __init__(self, frames, time_base=Fraction(1, 1000)):
        self.frames, self.time_base = iter(frames), time_base

    def __iter__(self):
        return self

    def __next__(self):
        return next(self.frames)


def pair(tmp_path, duration=3000):
    paths = [tmp_path / name for name in ("target", "reference")]
    for path in paths:
        path.write_bytes(path.name.encode())
    return [media_version(str(path), duration, "{}") for path in paths]


def support(target, reference, start, end, clock=SAME_SPEED, hold=Fraction(0)):
    return analysis._video_support(
        target, reference, 0, 0, Fraction(start), Fraction(end), clock, hold, FRAMING, lambda: False, lambda: False, 1
    )


def readers(monkeypatch, target, target_frames, reference_frames, tick=Fraction(1, 1000)):
    @contextmanager
    def native(item, *args, **kwargs):
        yield Frames(iter(target_frames if item.id == target.id else reference_frames), tick)

    monkeypatch.setattr(media, "native_frames", native)


@pytest.mark.parametrize("failing_side", ["target", "reference"])
def test_failure_after_last_native_yield_discards_all_staged_video(tmp_path, monkeypatch, failing_side):
    items = dict(zip(("target", "reference"), pair(tmp_path, 1000), strict=True))
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
    # a reference's failure is the reference's: the analysis leaves it out instead of failing the target
    expected = analysis._ReferenceFailed if failing_side == "reference" else media.DecodeError
    with pytest.raises(expected, match="after final") as raised:
        support(items["target"], items["reference"], 0, 1000)
    assert (type(raised.value) is analysis._ReferenceFailed) == (failing_side == "reference")
    assert "target" in exhausted and "reference" in exhausted


@pytest.mark.parametrize("tick,confirmed", [(Fraction(1, 1000), True), (Fraction(1, 24000), False)])
def test_frames_pair_within_one_container_tick(tmp_path, monkeypatch, tick, confirmed):
    """Two 24000/1001 fps Matroska files of the same material whose frame grids sit half a millisecond apart keep
    whole-millisecond timestamps that land 0 or 1 ms off each other, alternating (real Blu-ray rips do). Pairing
    within one tick of the stream confirms them; with a finer time base the same 1 ms is a real disagreement."""
    target, reference = pair(tmp_path)
    target_times = [round(n * FILM) for n in range(70)]
    reference_times = [round(n * FILM + Fraction(41, 2)) for n in range(70)]
    assert {b - a for a, b in zip(target_times, reference_times, strict=True)} == {20, 21}
    readers(monkeypatch, target, [frame(t) for t in target_times], [frame(t) for t in reference_times], tick)
    found = support(target, reference, 0, 2800, recurrence.Clock(Fraction(1), Fraction(20)))
    assert bool(found) is confirmed, found
    if confirmed:
        # one unbroken run: a rounding flip between consecutive pairs must not leave a millisecond hole
        assert len(found) == 1 and found[0][0] <= 50 and found[0][1] >= 2700, found


def test_variable_pts_and_inserted_reference_record_veto_native_support(tmp_path, monkeypatch):
    target, reference = pair(tmp_path)
    moments = [0]
    for i in range(29):
        moments.append(moments[-1] + (80 if i % 2 else 120))
    readers(monkeypatch, target, [frame(t) for t in moments], [frame(t) for t in sorted([*moments, 1450])])
    found = support(target, reference, 0, 2900)
    assert found
    assert not any(lo < 1500 and hi > 1400 for lo, hi in found)


def film(n):
    """Film frame n as a flat picture no neighbour agrees with (16 code values apart: 4.6 times the tightest limit)."""
    return 40 + 16 * (n % 10)


def test_a_copy_with_repeated_frames_pairs_each_frame_with_the_ones_shown_meanwhile(tmp_path, monkeypatch):
    """The same footage at 24 and at 30 frames a second (every fourth film frame shown twice) is not frame for frame:
    a frame's partner is any frame the other file shows within one hold of it."""
    target, reference = pair(tmp_path)
    hold = recurrence.hold(1, 24, 30)
    assert hold == Fraction(1000, 24) and recurrence.hold(1, 24, 24) == 0
    assert recurrence.hold(Fraction(24, 25), 24, 25) == 0  # a 25 fps speed-up runs frame for frame at its speed
    target_frames = [frame(Fraction(1000 * n, 24), luma=film(n)) for n in range(60)]
    reference_frames = [frame(Fraction(1000 * k, 30), luma=film(4 * k // 5)) for k in range(75)]
    readers(monkeypatch, target, target_frames, reference_frames)
    found = support(target, reference, 0, 2400, hold=hold)
    assert len(found) == 1 and found[0][0] <= 50 and found[0][1] >= 2300, found
    # footage only the reference has (five of its frames) is footage the target lacks: it vetoes like a failed frame
    for k in range(35, 40):
        reference_frames[k] = frame(Fraction(1000 * k, 30), luma=250)
    readers(monkeypatch, target, target_frames, reference_frames)
    found = support(target, reference, 0, 2400, hold=hold)
    assert found and not any(lo < 1300 and hi > 1167 for lo, hi in found)
    # and without the hold the two cadences do not pair at all
    readers(monkeypatch, target, target_frames, reference_frames)
    assert support(target, reference, 0, 2400) == []


@pytest.mark.parametrize(("brighter", "unbroken"), [(5, True), (8, False)])
def test_one_near_miss_frame_keeps_the_run_and_one_differing_frame_breaks_it(tmp_path, monkeypatch, brighter, unbroken):
    """A frame 5 code values brighter all over is 1.43 times the tightest limit (3.5): a near miss, forgiven alone.
    At 8 it is 2.29 times: a frame that differs, which breaks the run and vetoes a quarter second each side."""
    target, reference = pair(tmp_path)
    moments = [round(n * FILM) for n in range(70)]
    reference_frames = [frame(t, luma=100 + (brighter if n == 30 else 0)) for n, t in enumerate(moments)]
    readers(monkeypatch, target, [frame(t) for t in moments], reference_frames)
    found = support(target, reference, 0, 2800)
    if unbroken:
        assert len(found) == 1 and found[0][0] <= 50 and found[0][1] >= 2700, found
    else:
        assert len(found) == 2 and found[0][1] <= moments[30] - 250 and found[1][0] >= moments[30] + 250, found


# ---- hints ------------------------------------------------------------------------------------------------------------


def hints(count, fps=24):
    return [(Fraction(i * 1000, fps), np.arange(48, dtype=np.float32) + i * 20, 20) for i in range(count)]


def test_fractional_offsets_remain_exact_proposal_hints():
    features = hints(20)
    offset = Fraction(1000, 24)
    shifted = [(pts + offset, feature, std) for pts, feature, std in features]
    assert (recurrence.Clock(Fraction(1), offset), Fraction(0)) in recurrence.perceptual_shifts(features, shifted)


def test_a_copy_at_another_speed_is_proposed_at_that_speed_only():
    """Measured: a 24.000 fps episode against a 25 fps one of the same series shares its title sequence at 24/25 of
    the target's time plus 102,800 ms, both files' timestamps rounded to whole milliseconds."""
    features = [(Fraction(round(pts)), feature, std) for pts, feature, std in hints(40)]
    speed = Fraction(24, 25)
    copy = [(Fraction(round(speed * pts + 102800)), feature, std) for pts, feature, std in features]
    assert recurrence.speeds(24, 25) == [1, speed] and recurrence.speeds(25, 24) == [1, Fraction(25, 24)]
    assert recurrence.speeds(Fraction(24000, 1001), Fraction(30000, 1001)) == [1]  # repeated frames, not a speed
    assert recurrence.speeds(Fraction(24000, 1001), 24) == [1, Fraction(1000, 1001)]
    found = recurrence.perceptual_shifts(features, copy, [(rate, Fraction(0)) for rate in recurrence.speeds(24, 25)])
    assert [clock.rate for clock, _ in found] == [speed]
    assert abs(found[0][0].offset - 102800) <= 1
    assert found[0][0].at(Fraction(1000)) == speed * 1000 + found[0][0].offset
    assert found[0][0].back(found[0][0].at(Fraction(1234))) == 1234


def test_hints_of_a_copy_with_repeated_frames_agree_within_one_hold():
    hold = recurrence.hold(1, Fraction(24000, 1001), Fraction(30000, 1001))
    features = [(n * FILM, np.arange(48, dtype=np.float32) + n * 20, 20) for n in range(40)]
    copy = [(k * Fraction(1001, 30) + 5000, features[4 * k // 5][1], 20) for k in range(50)]
    found = recurrence.perceptual_shifts(features, copy, [(Fraction(1), hold)])
    assert found and found[0][1] == hold and abs(found[0][0].offset - 5000) <= hold
    # frame for frame only every fifth reference frame lands on a film frame: proposed, but never verified so
    assert all(held == 0 for _, held in recurrence.perceptual_shifts(features, copy))


def test_anchors_require_disjoint_native_groups_and_real_span():
    rows = [(Fraction(i * 1000, 24), np.full(48, i, dtype=np.float32), 20) for i in range(30)]
    anchored = recurrence.anchor(rows, rows, 0, 1250, SAME_SPEED, Fraction(1))
    assert anchored is not None
    start, end, proof = anchored
    assert start == 0 and end == 1208
    assert set(proof["start_pts_ms"]).isdisjoint(proof["end_pts_ms"])
    assert max(map(Fraction, proof["start_pts_ms"])) < min(map(Fraction, proof["end_pts_ms"]))
    assert recurrence.anchor(rows, rows, 0, 500, SAME_SPEED, Fraction(1)) is None
    flat = [(t, feature, 0) for t, feature, _ in rows]
    assert recurrence.anchor(flat, flat, 0, 1250, SAME_SPEED, Fraction(1)) is None
    # on another clock the same frames are found where that clock puts them
    moved = [(Fraction(24, 25) * t + 700, feature, std) for t, feature, std in rows]
    sped = recurrence.Clock(Fraction(24, 25), Fraction(700))
    assert recurrence.anchor(rows, moved, 0, 1250, sped, Fraction(1))[:2] == (0, 1208)
    assert recurrence.anchor(rows, moved, 0, 1250, SAME_SPEED, Fraction(1)) is None


def test_reserved_color_declarations_are_not_unspecified():
    with pytest.raises(ValueError, match="declaration"):
        recurrence.frame_declarations(replace(frame(), color_space="reserved"))


def test_entire_failed_overlap_is_removed_without_gap_filling():
    good = [(Fraction(0), Fraction(20)), (Fraction(10), Fraction(30)), (Fraction(30), Fraction(50))]
    assert recurrence.subtract(good, [(Fraction(15), Fraction(35))]) == [(0, 15), (35, 50)]
    assert recurrence.intersect([(0, 100)], [(10, 20), (30, 40)]) == [(10, 20), (30, 40)]


# ---- the sound --------------------------------------------------------------------------------------------------------


def tone(frequency, amplitude, seconds=1.0):
    return amplitude * np.sin(2 * np.pi * frequency * np.arange(int(RATE * seconds)) / RATE)


def verified(ours, theirs, origin=Fraction(0)):
    verifier = recurrence.AudioVerifier(RATE, origin)
    verifier.feed(ours[:, None], theirs[:, None])
    return verifier.finish()


def test_a_sound_only_one_file_has_is_refused():
    shared = tone(220, 0.1)
    assert verified(shared, shared) == [(40, 960)]
    assert verified(shared, shared + tone(1600, 0.03)) == []
    assert verified(shared + tone(1600, 0.03), shared) == []


def test_a_difference_is_judged_against_the_sound_both_files_share():
    """Two encodes or mixes of one recording differ in proportion to the sound itself (measured between a Blu-ray
    and a web copy: at most 0.063, 0.027 and 0.087 of the level in the three spectral tests, where the limits are
    0.08, 0.03 and 0.1), so the same faint tone is a codec's difference beside a loud sound and a sound of its own
    beside a quiet one."""
    faint = tone(1600, 0.008)
    loud, quiet = tone(220, 0.2), tone(220, 0.02)
    assert verified(loud, loud + faint) == [(40, 960)]  # 4 % of the shared sound
    assert verified(quiet, quiet + faint) == []  # 40 % of it
    # in silence the floors alone decide, as they always did
    silence = np.zeros(RATE)
    assert verified(silence, silence) == [(40, 960)]
    assert verified(silence, faint) == []


def test_the_same_sound_at_opposite_phase_is_refused_once_it_is_not_faint():
    carrier = tone(220, 0.1)
    assert verified(carrier + tone(1600, 0.03), carrier - tone(1600, 0.03)) == []
    assert verified(carrier + tone(1600, 0.004), carrier - tone(1600, 0.004)) == [(40, 960)]


@pytest.mark.parametrize("chunk_size", [137, 160, 1600, 8000])
def test_audio_chunk_boundaries_preserve_global_window_grid(chunk_size):
    baseline = tone(220, 0.1, 2) + tone(330, 0.05, 2) + tone(440, 0.05, 2)
    changed = baseline.copy()
    changed[RATE : RATE + 528] += tone(1600, 0.05, 2)[:528]
    whole = recurrence.AudioVerifier(RATE, Fraction(1, 3))
    whole.feed(baseline[:, None], changed[:, None])
    chunks = recurrence.AudioVerifier(RATE, Fraction(1, 3))
    for start in range(0, len(baseline), chunk_size):
        chunks.feed(baseline[start : start + chunk_size, None], changed[start : start + chunk_size, None])
    assert chunks.finish() == whole.finish()
    assert chunks.bad == whole.bad
    assert chunks.windows == whole.windows
    assert whole.finish() and not any(lo < Fraction(1033) and hi > 1000 for lo, hi in chunks.finish())


def test_the_verifier_refuses_what_is_not_a_pair_of_sounds_on_one_grid():
    with pytest.raises(ValueError, match="sample grid"):
        recurrence.AudioVerifier(11025, Fraction(0))
    verifier = recurrence.AudioVerifier(RATE, Fraction(0))
    with pytest.raises(ValueError, match="Malformed"):
        verifier.feed(np.zeros((100, 1)), np.zeros((99, 1)))
    with pytest.raises(ValueError, match="Malformed"):
        verifier.feed(np.full((100, 1), np.nan), np.zeros((100, 1)))


def _noise(samples, seed):
    return np.random.default_rng(seed).standard_normal(samples) * 0.1


def test_sound_lag_is_the_lag_and_the_level_despite_gain_and_codec_noise():
    search = 250 * RATE // 1000
    source = _noise(20 * RATE, 3)
    true = search + 2585  # 161.5625 ms later than the picture predicts (measured: Community S03E02 against E03)
    probe = source[true : true + 12 * RATE] * 1.108 + _noise(12 * RATE, 4) * 0.2
    lag, score, level = recurrence.sound_lag(probe, source[: 12 * RATE + 2 * search], search, RATE)
    assert lag == pytest.approx(true, abs=0.05) and score > 0.95
    assert level == pytest.approx(1.108, rel=0.02)  # what brings the reference to the target's level


def test_sound_lag_places_the_peak_between_samples():
    smooth = np.convolve(_noise(6 * RATE, 9), np.hanning(33) / 16, mode="same")  # band-limited, as real sound is
    moved = canonical.Resampler(1.0, 1000.4)
    probe = moved.feed(smooth)[: 2 * RATE]  # sample i is the source 1000.4 + moved.start + i samples in
    lag, score, _ = recurrence.sound_lag(probe, smooth, 1000, RATE)
    assert lag == pytest.approx(1000.4 + moved.start, abs=0.1) and score > 0.99


def test_sound_lag_finds_a_shared_stretch_inside_a_probe_that_is_mostly_different():
    search = 250 * RATE // 1000
    source = _noise(16 * RATE, 5)
    true = search + 1600  # 100 ms later than the picture predicts
    probe = _noise(12 * RATE, 6)  # 12 s of different sound ...
    probe[5 * RATE : 7 * RATE] = source[true + 5 * RATE : true + 7 * RATE]  # ... except two seconds in the middle
    lag, score, _ = recurrence.sound_lag(probe, source[: 12 * RATE + 2 * search], search, RATE)
    assert lag == pytest.approx(true, abs=0.05) and score > 0.99


def test_sound_lag_prefers_the_lag_nearest_the_picture_among_exact_ties():
    period = np.sin(2 * np.pi * np.arange(60000) / 100.0)  # a whole number of samples per period: exact ties
    lag, score, level = recurrence.sound_lag(period[:40000], period, 12300, 40000)
    assert (lag, score, level) == (pytest.approx(12300), pytest.approx(1.0), pytest.approx(1.0))
    assert recurrence.sound_lag(period[:40000], period, 12349, 40000)[0] == pytest.approx(12300)
    assert recurrence.sound_lag(period[:40000], period, 12351, 40000)[0] == pytest.approx(12400)


def test_sound_lag_reports_no_lag_for_silence_and_rejects_malformed_probes():
    assert recurrence.sound_lag(np.zeros(1000), np.zeros(2000), 0, 500) is None
    assert recurrence.sound_lag(np.zeros(1000), _noise(2000, 1), 0, 500) is None
    with pytest.raises(ValueError):
        recurrence.sound_lag(_noise(2000, 1), _noise(1000, 2), 0, 500)
    with pytest.raises(ValueError):
        recurrence.sound_lag(_noise(1000, 1)[:, None], _noise(2000, 2)[:, None], 0, 500)  # one signal per track
    with pytest.raises(ValueError):
        recurrence.sound_lag(_noise(1000, 1), _noise(2000, 2), 0, 0)


# ---- arithmetic -------------------------------------------------------------------------------------------------------


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


def test_every_metric_and_the_early_exit_agree_on_the_verdict():
    rng = np.random.default_rng(5)
    base = rng.integers(40, 200, size=(64, 96)).astype(np.float32)
    quiet = canonical.Picture(base, base[::2, ::2], base[::2, ::2])
    for bump, verdict in ((0.0, "same"), (2.0, "same"), (5.0, "near"), (40.0, "differs")):
        other = canonical.Picture(base + bump, base[::2, ::2], base[::2, ::2])
        metrics = recurrence.picture_metrics(quiet, other)
        worst = max(metrics[name] / recurrence.VIDEO[name] for name in recurrence.PICTURE_METRICS)
        excess = recurrence.picture_excess(quiet, other)
        if verdict == "differs":
            assert excess > recurrence.NEAR_RATIO and worst > recurrence.NEAR_RATIO  # stopped at the first to say so
        else:
            assert excess == pytest.approx(worst) and (excess <= 1) == (verdict == "same")
    with pytest.raises(ValueError, match="one grid"):
        recurrence.picture_excess(quiet, canonical.Picture(base[:-1], base[::2, ::2], base[::2, ::2]))
