"""Verifier unit contracts; end-to-end codec fitness is evaluated separately."""

from contextlib import contextmanager
from dataclasses import replace
from fractions import Fraction

import numpy as np
import pytest

from media_preview_generator.reprise import analysis, media, recurrence
from media_preview_generator.reprise.identity import media_version


def frame(pts=0, *, chroma_change=False, flags=()):
    arrays = [
        np.full((32, 32), 100, dtype=np.uint8),
        np.full((16, 16), 128, dtype=np.uint8),
        np.full((16, 16), 128, dtype=np.uint8),
    ]
    if chroma_change:
        arrays[1][7:9, 7:9] = 250
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


def test_native_chroma_and_small_concentrated_change_are_not_averaged_away():
    assert recurrence.video_matches(frame(), frame())
    assert not recurrence.video_matches(frame(), frame(chroma_change=True))


def test_matching_unspecified_is_not_guessed_rendering_and_asymmetry_fails():
    assert recurrence.video_matches(frame(), frame())
    assert not recurrence.video_matches(frame(), replace(frame(), color_space="bt709"))
    assert not recurrence.video_matches(frame(), replace(frame(), sample_aspect_ratio=Fraction(2)))


def test_opposite_sign_stereo_change_cannot_cancel_in_verifier():
    t = np.arange(960) / 48000
    baseline = np.column_stack((0.1 * np.sin(2 * np.pi * 220 * t), 0.1 * np.sin(2 * np.pi * 220 * t)))
    voice = 0.03 * np.sin(2 * np.pi * 1600 * t)
    changed = baseline + np.column_stack((voice, -voice))
    np.testing.assert_allclose(changed.sum(axis=1), baseline.sum(axis=1), atol=1e-16)
    assert recurrence.audio_matches(baseline, baseline, 48000)
    assert not recurrence.audio_matches(baseline, changed, 48000)


def test_entire_failed_overlap_is_removed_without_gap_filling():
    good = [(Fraction(0), Fraction(20)), (Fraction(10), Fraction(30)), (Fraction(30), Fraction(50))]
    assert recurrence.subtract(good, [(Fraction(15), Fraction(35))]) == [(0, 15), (35, 50)]
    assert recurrence.intersect([(0, 100)], [(10, 20), (30, 40)]) == [(10, 20), (30, 40)]


@pytest.mark.parametrize("failing_side", ["target", "reference"])
def test_failure_after_last_native_yield_discards_all_staged_video(tmp_path, monkeypatch, failing_side):
    items = {}
    for side in ("target", "reference"):
        path = tmp_path / side
        path.write_bytes(side.encode())
        items[side] = media_version(str(path), 1000, "{}")
    exhausted = []

    @contextmanager
    def native(item, *args):
        side = next(k for k, v in items.items() if v.id == item.id)

        def records():
            for pts in range(0, 1200, 40):
                yield frame(pts)
            exhausted.append(side)

        yield records()
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


def test_fractional_offsets_remain_exact_proposal_hints():
    features = [(Fraction(i * 1000, 24), np.arange(48, dtype=np.float32) + i * 20) for i in range(20)]
    offset = Fraction(1000, 24)
    shifted = [(pts + offset, feature) for pts, feature in features]
    assert offset in recurrence.perceptual_shifts(features, shifted)
