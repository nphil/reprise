"""The canonical picture and sound: one representation for every file format, stated exactly.

Everything here is synthetic and deterministic. The crop offsets are the ones measured on real encodes of one season
(two columns trimmed on both sides of one episode, on the right only of another).
"""

import math
from fractions import Fraction

import numpy as np
import pytest

from media_preview_generator.reprise import canonical, media, recurrence


def native(luma, blue=None, red=None, *, depth=8, color_range="tv", aspect=1, pts=0):
    """A decoded 4:2:0 frame from arrays of code values (the colour planes default to no colour)."""
    height, width = luma.shape
    middle = 1 << (depth - 1)
    half = ((height + 1) // 2, (width + 1) // 2)
    arrays = [luma, *(np.full(half, middle) if plane is None else plane for plane in (blue, red))]
    kind, size = ("u1", 1) if depth == 8 else ("<u2", 2)
    planes = tuple(
        media.NativePlane(memoryview(a.astype(kind).tobytes()), a.shape[1], a.shape[0], a.shape[1] * size, size)
        for a in arrays
    )
    return media.NativeFrame(
        Fraction(pts),
        planes,
        width,
        height,
        "yuv420p" if depth == 8 else "yuv420p10le",
        depth,
        color_range,
        "unknown",
        "unknown",
        "unknown",
        "left",
        sample_aspect_ratio=Fraction(aspect),
        interpretation_flags=(),
    )


def detail(height, width, seed=7, top=236):
    """A picture with structure at every scale: smooth shapes plus fine grain, in limited-range code values."""
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[0:height, 0:width]
    smooth = 110 + 60 * np.sin(x / 9.0) * np.cos(y / 7.0) + 20 * np.sin((x + 2 * y) / 23.0)
    return np.clip(smooth + rng.integers(-12, 13, size=(height, width)), 16, top - 1).astype(np.int64)


# ---- the picture ------------------------------------------------------------------------------------------------------


def test_a_cell_is_the_exact_mean_of_the_pixels_it_covers():
    luma = detail(540, 960)
    blue, red = detail(270, 480, 1), detail(270, 480, 2)
    picture = canonical.picture(native(luma, blue, red))
    # 960x540 on the 480x270 grid: cells of 2x2 luma pixels, colour cells of 2x2 colour samples; the outer two luma
    # cells (one colour cell) are the margin that is left out.
    cells = luma.reshape(270, 2, 480, 2).mean(axis=(1, 3))
    assert picture.luma.shape == (266, 476) and picture.luma.dtype == np.float32
    np.testing.assert_array_equal(picture.luma, cells[2:-2, 2:-2].astype(np.float32))
    for plane, source in ((picture.blue, blue), (picture.red, red)):
        np.testing.assert_array_equal(
            plane, source.reshape(135, 2, 240, 2).mean(axis=(1, 3))[1:-1, 1:-1].astype(np.float32)
        )


def test_bit_depth_and_range_reach_one_scale():
    luma, blue = detail(64, 96), detail(32, 48, 3)
    eight = canonical.picture(native(luma, blue, blue))
    ten = canonical.picture(native(luma * 4, blue * 4, blue * 4, depth=10))
    for a, b in zip(eight, ten, strict=True):
        np.testing.assert_array_equal(a, b)  # 10-bit 940 is 8-bit 235: a power of two, so exactly
    flat = np.full((64, 96), 255)
    full = canonical.picture(native(flat, np.full((32, 48), 128), np.full((32, 48), 255), color_range="pc"))
    assert float(full.luma[0, 0]) == pytest.approx(235) and float(full.blue[0, 0]) == pytest.approx(128)
    assert float(full.red[0, 0]) == pytest.approx(128 + 127 * 224 / 255)
    black = canonical.picture(native(np.zeros((64, 96), dtype=np.int64), color_range="pc"))
    assert float(black.luma[0, 0]) == pytest.approx(16)


def test_two_resolutions_of_one_picture_are_one_canonical_picture():
    small = detail(180, 320)
    blue = detail(90, 160, 4)
    large, large_blue = (np.repeat(np.repeat(a, 2, axis=0), 2, axis=1) for a in (small, blue))
    framing = canonical.regions((320, 180, 1), (640, 360, 1))
    assert framing is not None and not framing.measured
    assert (framing.target.rows, framing.target.columns) == (framing.reference.rows, framing.reference.columns)
    a = canonical.picture(native(small, blue, blue), framing.target)
    b = canonical.picture(native(large, large_blue, large_blue), framing.reference)
    assert recurrence.picture_excess(a, b) == pytest.approx(0, abs=1e-4)
    # an anamorphic frame of the same displayed shape is the same framing too; another shape is not
    assert canonical.regions((1440, 1080, Fraction(4, 3)), (1920, 1080, 1)) is not None
    assert canonical.regions((1920, 1080, 1), (1920, 800, 1)) is None
    assert canonical.regions((1920, 1080, 1), (1440, 1080, 1)) is None


def test_a_grid_is_never_finer_than_the_pixels_and_keeps_the_displayed_shape():
    assert canonical.grid(1920, 1080) == (270, 480)
    assert canonical.grid(3840, 2160) == (270, 480)
    assert canonical.grid(1280, 720) == (270, 480)
    assert canonical.grid(720, 576, Fraction(16, 11)) == (264, 480)  # anamorphic: its displayed shape, not its pixels
    assert canonical.grid(96, 64) == (64, 96)
    # 1918x1078 (two pixels of black trimmed) uses whole 4-pixel cells of its centre
    assert canonical.region(1918, 1078) == canonical.Region(0, 0, 1916, 1076, 269, 479)
    with pytest.raises(ValueError):
        canonical.grid(0, 1080)


@pytest.mark.parametrize(
    ("columns", "expected"),
    [
        (slice(2, 198), (2, 0)),  # trimmed on both sides: where the centred guess already is
        (slice(0, 196), (0, 0)),  # trimmed on the right only (as measured on The Office S01E04 against E01)
        (slice(4, 200), (4, 0)),  # ... or on the left only
    ],
)
def test_a_crop_is_found_from_the_two_frames_themselves(columns, expected):
    """Real encodes of one season trim two columns on both sides of one episode and on one side of the next, and no
    header says which. A wrong guess of one pixel already costs most of the picture limits (measured: a median of
    0.64 of them instead of 0.12), two pixels fail them."""
    full, colour = detail(120, 200), detail(60, 100, 5)
    whole = native(full, colour, colour)
    half = slice(columns.start // 2, columns.stop // 2)
    cropped = native(full[:, columns], colour[:, half], colour[:, half])
    framing = canonical.regions((200, 120, 1), (196, 120, 1))
    assert framing.measured and (framing.target.left, framing.reference.left) == (2, 0)  # centred until measured
    region, other = canonical.registered(framing, whole, cropped)
    assert (region.left, other.left) == expected and (region.top, other.top) == (0, 0)
    assert recurrence.picture_excess(canonical.picture(whole, region), canonical.picture(cropped, other)) == 0
    # and the other way round: the target is the cropped one
    back = canonical.regions((196, 120, 1), (200, 120, 1))
    other, region = canonical.registered(back, cropped, whole)
    assert (region.left, other.left) == expected
    if expected != (2, 0):
        guessed = recurrence.picture_excess(
            canonical.picture(whole, framing.target), canonical.picture(cropped, framing.reference)
        )
        assert guessed > 1  # the centred guess would have refused the same picture


def test_frames_without_structure_keep_the_centred_guess_and_different_footage_still_fails():
    framing = canonical.regions((200, 120, 1), (196, 120, 1))
    black, other_black = native(np.full((120, 200), 16)), native(np.full((120, 196), 16))
    assert canonical.registered(framing, black, other_black) == (framing.target, framing.reference)
    whole = native(detail(120, 200, 1))
    another = native(np.flipud(detail(120, 196, 2)))
    region, other = canonical.registered(framing, whole, another)
    assert recurrence.picture_excess(canonical.picture(whole, region), canonical.picture(another, other)) > 1
    # frames of one format are never moved
    same = canonical.regions((200, 120, 1), (200, 120, 1))
    assert not same.measured and canonical.registered(same, whole, whole) == (same.target, same.reference)


def test_pictures_compare_only_within_one_colour_class():
    assert canonical.colour_class("bt709", "bt709", 1920, 1080) == ("sdr", "bt709")
    assert canonical.colour_class(None, None, 1920, 1080) == ("sdr", "bt709")  # undeclared: what players assume
    assert canonical.colour_class("unknown", "unknown", 720, 480) == ("sdr", "bt601")
    assert canonical.colour_class("smpte170m", "smpte170m", 720, 480) == ("sdr", "bt601")
    assert canonical.colour_class("smpte2084", "bt2020nc", 3840, 2160) == ("pq", "bt2020")
    assert canonical.colour_class("arib-std-b67", "bt2020nc", 3840, 2160) == ("hlg", "bt2020")
    with pytest.raises(ValueError, match="declaration"):
        canonical.colour_class("reserved", "bt709", 1920, 1080)


# ---- the sound --------------------------------------------------------------------------------------------------------


def test_the_mono_mix_is_the_fold_down_of_the_layout_and_leaves_the_lfe_out():
    rng = np.random.default_rng(3)
    surround = rng.standard_normal((4800, 6)) * 0.1
    names = ("FL", "FR", "FC", "LFE", "SL", "SR")
    fl, fr, fc, _, sl, sr = surround.T
    stereo = np.column_stack((fl + math.sqrt(0.5) * (fc + sl), fr + math.sqrt(0.5) * (fc + sr)))  # ITU-R BS.775
    folded = canonical.mono(stereo, ("FL", "FR"))
    direct = canonical.mono(surround, names)
    gain = float(folded @ direct / (direct @ direct))
    np.testing.assert_allclose(direct * gain, folded, atol=1e-12)  # the same sound, at another level
    louder = surround.copy()
    louder[:, 3] += 0.5
    np.testing.assert_array_equal(canonical.mono(louder, names), direct)
    assert canonical.mixed(names) == ["FL", "FR", "FC", "SL", "SR"]
    with pytest.raises(ValueError, match="channel"):
        canonical.mono(surround, ("FL", "FR", "FC", "LFE", "TFL", "TFR"))
    with pytest.raises(ValueError, match="layout"):
        canonical.mono(surround[:, :2], names)


def tones(times):
    """A band-limited sound (well under the canonical Nyquist rate) as a function of time in seconds."""
    return (
        0.3 * np.sin(2 * np.pi * 440 * times)
        + 0.2 * np.sin(2 * np.pi * 1337 * times + 0.5)
        + 0.1 * np.sin(2 * np.pi * 5200 * times + 1.1)
    )


@pytest.mark.parametrize("rate", [48000, 44100, 32000, 96000])
def test_every_sample_rate_reaches_the_same_canonical_sound(rate):
    data = tones(np.arange(rate) / rate)[:, None]
    first, samples = canonical.sound(data, ("FC",), rate, Fraction(0))
    wanted = tones((first + np.arange(len(samples))) / canonical.SOUND_RATE)
    assert len(samples) > 0.95 * canonical.SOUND_RATE
    np.testing.assert_allclose(samples, wanted, atol=2e-3)


def test_a_sped_up_copy_and_a_fractional_offset_are_read_back_onto_the_targets_clock():
    """A 25 fps copy of film-rate footage plays everything 25/24 faster: reference time is 24/25 of target time. Its
    sound, read at that speed and at the sound's offset, is the target's sound sample for sample."""
    rate, speed, offset = 48000, Fraction(24, 25), Fraction(100375, 1000)  # ms, not a whole number of samples
    target = tones(np.arange(2 * rate) / rate)[:, None]
    # the reference holds the target's moment t at speed * t + offset
    start = Fraction(80)  # the reference excerpt starts 80 ms into its file
    moments = (float(start - offset) / 1000 + np.arange(2 * rate) / rate) / float(speed)
    reference = tones(moments)[:, None]
    ours_at, ours = canonical.sound(target, ("FC",), rate, Fraction(0))
    theirs_at, theirs = canonical.sound(reference, ("FC",), rate, start, speed, offset)
    first, last = max(ours_at, theirs_at), min(ours_at + len(ours), theirs_at + len(theirs))
    assert last - first > canonical.SOUND_RATE
    np.testing.assert_allclose(
        ours[first - ours_at : last - ours_at], theirs[first - theirs_at : last - theirs_at], atol=3e-3
    )


@pytest.mark.parametrize(("rate", "speed"), [(48000, 1), (44100, 1), (48000, Fraction(24, 25))])
def test_a_stream_fed_in_pieces_is_the_stream_fed_whole(rate, speed):
    data = np.random.default_rng(11).standard_normal(rate)
    whole = canonical.resampler(rate, Fraction(1, 3), speed, Fraction(5, 7))
    expected = whole.feed(data)
    pieces = canonical.resampler(rate, Fraction(1, 3), speed, Fraction(5, 7))
    got = np.concatenate([pieces.feed(data[start : start + 997]) for start in range(0, len(data), 997)])
    assert pieces.start == whole.start and pieces.next == whole.next
    np.testing.assert_allclose(got, expected, rtol=0, atol=1e-12)


def test_a_resampler_refuses_a_step_it_cannot_take():
    with pytest.raises(ValueError):
        canonical.Resampler(0)
    with pytest.raises(ValueError):
        canonical.Resampler(float("nan"))
