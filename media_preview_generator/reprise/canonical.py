"""The canonical picture and the canonical sound: one representation for every file format.

Two files of the same footage differ in codec, bit depth, chroma layout, resolution, colour range and a few pixels of
frame size, and their sound in codec, channel layout and sample rate. The comparison never looks at those native
signals directly: every decoded frame becomes a small picture of exact area means on a fixed grid in one code-value
scale, and every sound becomes one mono signal at one sample rate on the target's clock. Both are pure functions of
the decoded native samples (numpy only: no FFmpeg scaling, colour conversion or resampling), so they are the same on
every machine and FFmpeg build, and a test can state them exactly.

Nothing here decides anything: limits and run rules live in ``recurrence``, decode lifecycle in ``analysis``.
"""

import functools
import math
from fractions import Fraction
from typing import NamedTuple

import numpy as np

from .verification_profile import PROFILE_SPEC

PICTURE = PROFILE_SPEC["canonical"]["picture"]
SOUND = PROFILE_SPEC["canonical"]["sound"]
METADATA = PROFILE_SPEC["metadata"]
SOUND_RATE = SOUND["rate"]
_MIX = {name: math.sqrt(Fraction(power)) for name, power in SOUND["mix_power"].items()}
# How finely a position between two input samples is resolved by the resampling kernels (1/64 of a 48 kHz sample is
# a third of a microsecond).
_PHASES = 64


class Picture(NamedTuple):
    """One frame on the canonical grid: float32 code values on the 8-bit limited-range scale (16 is black, 235 white,
    128 no colour), the luma plane and the two colour planes at half its size. The frame's outer margin is left out."""

    luma: np.ndarray
    blue: np.ndarray
    red: np.ndarray


class Region(NamedTuple):
    """The rectangle of a native frame (whole luma pixels) that becomes a canonical picture, and that picture's grid
    of ``rows`` x ``columns`` luma cells before the margin is left out. Two files of one comparison use regions that
    cover the same part of the footage and the same grid."""

    left: int
    top: int
    width: int
    height: int
    rows: int
    columns: int


def declaration(value, field):
    """A colour declaration as the profile names it: ``unspecified`` for an absent one, the token for a known one, and
    ValueError for a reserved or malformed one (never guessed to be unspecified)."""
    if value in METADATA["unspecified_tokens"]:
        return "unspecified"
    if value not in METADATA[field.removeprefix("color_")]:
        raise ValueError("Unsupported native " + field + " declaration")
    return value


def colour_class(transfer, matrix, width, height):
    """What kind of code values a picture carries, as ``(transfer, matrix)``, from its declared transfer and matrix
    and its size: pictures compare only within one class.

    Standard dynamic range in every flavour is one transfer class (its code values are the picture itself), PQ and
    HLG are their own. An undeclared matrix is the one players assume for the picture's size."""
    transfer = METADATA["transfer_classes"].get(declaration(transfer, "color_transfer"), "sdr")
    matrix = declaration(matrix, "color_space")
    if matrix == "unspecified":
        matrix = "bt709" if width >= PICTURE["hd_width"] or height >= PICTURE["hd_height"] else "bt601"
    return transfer, METADATA["matrix_classes"].get(matrix, matrix)


def grid(width, height, sample_aspect_ratio=1):
    """The canonical ``(rows, columns)`` of a picture ``width`` x ``height`` native pixels: its displayed shape fitted
    inside the canonical box, never finer than the native pixels."""
    shown_width, shown_height = Fraction(width) * Fraction(sample_aspect_ratio), Fraction(height)
    if width <= 0 or height <= 0 or shown_width <= 0:
        raise ValueError("Invalid picture size")
    scale = min(Fraction(PICTURE["columns"]) / shown_width, Fraction(PICTURE["rows"]) / shown_height, Fraction(1))
    rows = min(height, max(1, math.floor(shown_height * scale + Fraction(1, 2))))
    columns = min(width, max(1, math.floor(shown_width * scale + Fraction(1, 2))))
    return rows, columns


def region(width, height, sample_aspect_ratio=1):
    """The region that is a ``width`` x ``height`` frame's own canonical picture.

    It is the whole frame on its grid, except that a square-pixel frame whose cells would be within the profile's
    tolerance of a whole number of pixels gets cells of exactly that many: the region is then the centred part of
    the frame those cells fill (1918x1078 uses 1916x1076 in 4-pixel cells), on even offsets so colour samples stay
    whole too. Whole cells are exact integer sums; the pixels given up lie inside the margin that is left out."""
    rows, columns = grid(width, height, sample_aspect_ratio)
    if Fraction(sample_aspect_ratio) == 1:
        cell = max(Fraction(width, columns), Fraction(height, rows))
        pixels = round(cell)
        if abs(cell - pixels) <= Fraction(PICTURE["whole_cell_tolerance"]):
            rows, columns = height // pixels, width // pixels
            used_width, used_height = columns * pixels, rows * pixels
            left, top = (width - used_width) // 4 * 2, (height - used_height) // 4 * 2
            return Region(left, top, used_width, used_height, rows, columns)
    return Region(0, 0, width, height, rows, columns)


def whole(frame):
    """The region that is the frame's own canonical picture (see ``region``)."""
    return region(frame.width, frame.height, frame.sample_aspect_ratio)


class Framing(NamedTuple):
    """How two files' frames show the same part of the footage: the region of each that becomes its canonical
    picture, on one grid. ``measured`` says the regions' positions are only a first guess, because one file's frame
    is a crop of the other's and nothing declares where: ``registered`` then finds them for each pair of frames."""

    target: Region
    reference: Region
    measured: bool


def regions(target, reference):
    """The framing of two frame formats, each ``(width, height, sample aspect ratio)`` (see ``Framing``); None when
    the formats do not frame the picture the same way.

    - The same format: each frame's own region.
    - Sizes within ``crop_pixels`` of each other each way (an encode that trimmed a few pixels of black from the
      edges): a rectangle of the size both have, on its own region's grid, centred until it is measured.
    - The same displayed shape (within ``shape_tolerance``) at another resolution (720p, 1080p, 2160p): both whole
      frames on the grid of the one with fewer pixels.
    Anything else (black bars burned into one of them, another aspect ratio) is not the same framing."""
    (width, height, aspect), (other_width, other_height, other_aspect) = target, reference
    aspect, other_aspect = Fraction(aspect), Fraction(other_aspect)
    if (width, height, aspect) == (other_width, other_height, other_aspect):
        own = region(width, height, aspect)
        return Framing(own, own, False)
    crop = PICTURE["crop_pixels"]
    if aspect == other_aspect and abs(width - other_width) <= crop and abs(height - other_height) <= crop:
        shared = region(min(width, other_width), min(height, other_height), aspect)
        return Framing(
            *(
                shared._replace(
                    left=(w - min(width, other_width)) // 2 + shared.left,
                    top=(h - min(height, other_height)) // 2 + shared.top,
                )
                for w, h in ((width, height), (other_width, other_height))
            ),
            True,
        )
    shape, other_shape = Fraction(width) * aspect / height, Fraction(other_width) * other_aspect / other_height
    if abs(shape / other_shape - 1) <= Fraction(PICTURE["shape_tolerance"]):
        rows, columns = (
            grid(width, height, aspect)
            if width * height <= other_width * other_height
            else grid(other_width, other_height, other_aspect)
        )
        return Framing(
            Region(0, 0, width, height, rows, columns), Region(0, 0, other_width, other_height, rows, columns), False
        )
    return None


def _edges(frame, axis):
    """How a frame's luma changes along one axis (1: across its columns, 0: down its rows): the differences of its
    mean over the other axis, on the 8-bit scale. A crop moves this profile by whole pixels and changes nothing else."""
    luma = plane_array(frame.planes[0])
    totals = luma.sum(axis=1 - axis, dtype=np.uint64).astype(np.float64)
    return np.diff(totals) * (2.0 ** (8 - frame.bit_depth) / luma.shape[1 - axis])


def _shift(profile, other, low, high, prefer):
    """The whole offset e in low..high at which ``other[x - e]`` is ``profile[x]``: the smallest mean absolute
    difference over the samples both have, and the offset nearest ``prefer`` when several are as small."""
    best = None
    for offset in sorted(range(low, high + 1), key=lambda e: (abs(e - prefer), e)):
        start, stop = max(0, offset), min(len(profile), len(other) + offset)
        distance = float(np.abs(profile[start:stop] - other[start - offset : stop - offset]).mean())
        if best is None or distance < best[0]:
            best = (distance, offset)
    return best[1]


def _placed(low, high, wanted):
    """The position in low..high nearest ``wanted``, an even one when there is one (colour samples then stay whole)."""
    choices = [p for p in range(low, high + 1) if p % 2 == 0] or list(range(low, high + 1))
    return min(choices, key=lambda p: (abs(p - wanted), p))


def registered(framing, frame, other):
    """The regions of a target frame and a reference frame that show the same part of the picture, as
    ``(target region, reference region)``: the framing's own regions, moved to where these two frames line up when
    that is to be measured (see ``Framing``).

    One frame is a crop of the other by a few whole pixels at each edge, in a split no header declares (real
    encodes of one season trim two columns on the left of one episode and on both sides of the next). The offset is
    read from the two frames themselves, per axis: the shift at which the luma's edge profiles agree best. A pair
    of frames without structure along an axis gives no reading and keeps the centred guess, where it does not matter;
    a pair that shows different footage gets some offset and fails the comparison at any."""
    if not framing.measured:
        return framing.target, framing.reference
    places = []
    for axis, size, start in ((1, "width", "left"), (0, "height", "top")):
        span = getattr(framing.target, size)
        room, other_room = getattr(frame, size) - span, getattr(other, size) - span
        guess, other_guess = getattr(framing.target, start), getattr(framing.reference, start)
        offset = _shift(_edges(frame, axis), _edges(other, axis), -other_room, room, guess - other_guess)
        place = _placed(max(0, offset), min(room, other_room + offset), guess)
        places.append((place, place - offset))
    (left, other_left), (top, other_top) = places
    return framing.target._replace(left=left, top=top), framing.reference._replace(left=other_left, top=other_top)


def plane_array(plane):
    return np.ndarray(
        (plane.height, plane.width),
        dtype=np.dtype("u1" if plane.sample_bytes == 1 else "<u2"),
        buffer=plane.data,
        strides=(plane.stride_bytes, plane.sample_bytes),
    )


def _sums(data, edges, axis):
    """Exact sums of ``data`` along ``axis`` over the cells between consecutive fractional sample positions
    ``edges`` (non-decreasing, inside the plane): whole samples by reduction between the positions' sample indices,
    the cut sample at each end of a cell by the fraction of it the cell covers."""
    size = data.shape[axis]
    index = np.minimum(np.floor(edges).astype(np.intp), size)
    steps = np.diff(index)
    if not (edges - index).any() and steps[0] > 0 and (steps == steps[0]).all():
        # Cells of whole samples, all the same size (1080p and 2160p on the canonical grid): add the cell's samples
        # as strided slices, which is several times faster than a reduction over a reshaped block.
        step = int(steps[0])
        block = data[index[0] : index[-1]] if axis == 0 else data[:, index[0] : index[-1]]
        slices = [block[k::step] if axis == 0 else block[:, k::step] for k in range(step)]
        total = slices[0].astype(np.float64 if data.dtype.kind == "f" else np.uint32)
        for part in slices[1:]:
            total += part
        return total
    bounds = np.unique(np.concatenate(([0], index, [size])))
    parts = np.add.reduceat(data, bounds[:-1], axis=axis, dtype=np.float64)
    shape = [1, 1]
    shape[axis] = -1
    before = np.concatenate((np.zeros_like(np.take(parts, [0], axis=axis)), np.cumsum(parts, axis=axis)), axis=axis)
    upto = np.take(before, np.searchsorted(bounds, index), axis=axis)  # the sum of the samples before each position
    inside = index < size
    cut = np.take(data, np.where(inside, index, 0), axis=axis) * np.where(inside, edges - index, 0.0).reshape(shape)
    return np.diff(upto + cut, axis=axis)


def _area_means(data, rows, columns):
    """The mean of ``data`` over every cell of the grid whose row and column boundaries are the fractional sample
    positions ``rows`` and ``columns``."""
    top, bottom = int(np.floor(rows[0])), min(data.shape[0], int(np.ceil(rows[-1])))
    sums = _sums(_sums(data[top:bottom], columns, 1), rows - top, 0)
    return sums / (np.diff(rows)[:, None] * np.diff(columns)[None, :])


@functools.lru_cache(maxsize=64)
def _cells(region, sizes):
    """For each plane of a frame (``sizes``: their width and height in samples), the row and column boundaries of
    the canonical cells kept from ``region``, in that plane's samples. A colour cell is two by two luma cells (the
    last one of an odd grid is what is left of the region); the outer margin is left out of every plane."""
    (width, height), *_ = sizes
    if (
        min(region.left, region.top) < 0
        or not 0 < region.rows <= region.height
        or not 0 < region.columns <= region.width
        or region.left + region.width > width
        or region.top + region.height > height
    ):
        raise ValueError("Invalid canonical picture region")
    margin = PICTURE["margin"] if min(region.rows, region.columns) >= PICTURE["margin_from"] else 0
    luma_rows = [region.top + Fraction(region.height * k, region.rows) for k in range(region.rows + 1)]
    luma_columns = [region.left + Fraction(region.width * k, region.columns) for k in range(region.columns + 1)]
    cells = []
    for number, (plane_width, plane_height) in enumerate(sizes):
        rows, columns, leave = luma_rows, luma_columns, margin
        if number:
            down = 1 if plane_height == height else 2
            across = 1 if plane_width == width else 2
            rows = [edge / down for edge in [*luma_rows[:-1:2], luma_rows[-1]]]
            columns = [edge / across for edge in [*luma_columns[:-1:2], luma_columns[-1]]]
            leave = (margin + 1) // 2
        cells.append(
            (
                np.minimum(np.array(rows[leave : len(rows) - leave], dtype=np.float64), plane_height),
                np.minimum(np.array(columns[leave : len(columns) - leave], dtype=np.float64), plane_width),
            )
        )
    return tuple(cells)


def picture(frame, region=None):
    """The canonical picture of one decoded frame (see ``Picture``), from ``region`` of it (default: all of it).

    Luma cell (r, c) is the exact mean of the native luma samples its rectangle covers; a colour cell covers two by
    two luma cells. Code values go to the 8-bit scale by a power of two (10-bit 940 is 235), and a full-range frame
    is mapped onto the limited scale. Chroma siting (half a native pixel) is ignored: it is an eighth of a cell.
    The outermost ``margin`` luma cells are left out: encodes differ in a pixel or two of black at the frame's edge."""
    if len(frame.planes) != 3 or frame.interpretation_flags:
        raise ValueError("Unsupported native picture layout")
    full = declaration(frame.color_range, "color_range") in METADATA["full_range_tokens"]
    scale = 2.0 ** (8 - frame.bit_depth)
    cells = _cells(region or whole(frame), tuple((plane.width, plane.height) for plane in frame.planes))
    planes = []
    for number, (plane, (rows, columns)) in enumerate(zip(frame.planes, cells, strict=True)):
        values = _area_means(plane_array(plane), rows, columns) * scale
        if full:
            values = 16 + values * (219 / 255) if number == 0 else 128 + (values - 128) * (224 / 255)
        planes.append(values.astype(np.float32))
    return Picture(*planes)


def mono(data, channel_ids):
    """One signal from a track's native channels (samples x channels): the ITU-R BS.775 stereo fold-down summed to
    mono, so a stereo track and the 5.1 track it was folded from give the same sound. The low-frequency effects
    channel is left out (it is optional in reproduction and two encodes of one mix disagree in it alone)."""
    try:
        weights = np.array([_MIX[name] for name in channel_ids], dtype=np.float64)
    except KeyError as exc:
        raise ValueError("Unsupported native audio channel") from exc
    total = float(weights.sum())
    if data.ndim != 2 or data.shape[1] != len(weights) or total <= 0:
        raise ValueError("Malformed native audio layout")
    return np.asarray(data, dtype=np.float64) @ (weights / total)


class Resampler:
    """A stream sampled at the positions ``first + n * step`` (in input samples), band-limited to the lower of the
    two Nyquist rates: ``out[n] = sum_k x[k] * h(first + n * step - k)`` with ``h`` a Kaiser-windowed sinc.

    One class does everything a sound needs to reach the canonical clock: the rate change (48000 or 44100 to the
    canonical rate), a different playback speed (a 25 fps copy of 24 fps footage) and an offset finer than a sample.
    Feeding the stream in pieces gives exactly the samples one piece would. Output ``n`` exists only when the input
    reaches ``reach`` samples to both sides of its position: ``start`` is the first that can, and outputs follow it
    without gaps."""

    def __init__(self, step, first=0.0):
        self.step, self.first = float(step), float(first)
        if not self.step > 0 or not math.isfinite(self.step) or not math.isfinite(self.first):
            raise ValueError("Invalid resampling step")
        widen = max(1.0, self.step)
        self.reach = math.ceil(SOUND["kernel_zeros"] * widen)
        offsets = np.arange(-self.reach, self.reach + 1)[None, :] - (np.arange(_PHASES + 1) / _PHASES)[:, None]
        window = np.i0(SOUND["kaiser_beta"] * np.sqrt(np.maximum(0.0, 1 - (offsets / (self.reach + 1)) ** 2)))
        bank = np.sinc(offsets / widen) * window
        self.bank = bank / bank.sum(axis=1, keepdims=True)  # every phase passes a constant unchanged
        self.buffer = np.zeros(0, dtype=np.float64)
        self.base = 0  # the input index of buffer[0]
        self.start = max(0, math.ceil((self.reach - self.first) / self.step))
        while self._position(self.start) < self.reach:
            self.start += 1
        self.next = self.start  # the next output to produce

    def _position(self, n):
        return self.first + n * self.step

    def feed(self, samples):
        """The outputs, in order from ``next``, that the input fed so far fully supports."""
        self.buffer = np.concatenate((self.buffer, np.asarray(samples, dtype=np.float64)))
        limit = self.base + len(self.buffer) - 1 - self.reach  # the last position whose kernel is inside the input
        last = math.floor((limit - self.first) / self.step)
        while self._position(last) > limit:
            last -= 1
        if last < self.next:
            return np.zeros(0, dtype=np.float64)
        positions = self.first + np.arange(self.next, last + 1, dtype=np.float64) * self.step
        whole_part = np.floor(positions).astype(np.int64)
        phase = np.rint((positions - whole_part) * _PHASES).astype(np.int64)
        begin = whole_part - self.reach - self.base
        if (phase == phase[0]).all():
            # One kernel for every output (a whole-number rate change): a plain filter, read at each position.
            out = np.convolve(self.buffer, self.bank[phase[0]][::-1], mode="valid")[begin]
        else:
            out = np.zeros(len(positions), dtype=np.float64)
            for tap in range(2 * self.reach + 1):
                out += self.buffer[begin + tap] * self.bank[phase, tap]
        self.next = last + 1
        drop = max(0, min(len(self.buffer), math.floor(self._position(self.next)) - self.reach - 1 - self.base))
        self.buffer = self.buffer[drop:]
        self.base += drop
        return out


def mixed(channel_ids):
    """The channels of a layout the canonical sound is mixed from: all but those ``mono`` leaves out."""
    return [name for name in channel_ids if _MIX[name] > 0]


def resampler(rate, start_ms, speed=1, offset_ms=0):
    """The ``Resampler`` that puts a native stream on a canonical clock: output n is the stream at
    ``speed * n / SOUND_RATE`` seconds plus ``offset_ms``. ``rate`` is the stream's sample rate and ``start_ms`` the
    time of the first sample it will be fed. Two streams resampled this way, the reference with the speed and offset
    of its sound's clock, hold the same moment of the footage at the same output index."""
    step = Fraction(rate) * Fraction(speed) / SOUND_RATE  # input samples per output sample
    first = (Fraction(offset_ms) - Fraction(start_ms)) * rate / 1000  # where output 0 falls, in input samples
    return Resampler(step, first)


def sound(data, channel_ids, rate, start_ms, speed=1, offset_ms=0):
    """The canonical sound of one whole native excerpt, as ``(index of its first sample, samples)``: see
    ``resampler`` for the clock and ``mono`` for the mix."""
    stream = resampler(rate, start_ms, speed, offset_ms)
    samples = stream.feed(mono(data, channel_ids))
    return stream.next - len(samples), samples
