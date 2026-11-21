"""The canonical grid, summed by the GPU before a frame is downloaded.

Decoding on the GPU left most of a frame's CPU cost where it was: FFmpeg downloaded 3 MB of samples (6 MB at 10 bits),
converted them to the stream's pixel format, passed them through ``showinfo`` twice and over a pipe, and the canonical
picture then added up every 4 x 4 luma samples of them. The GPU can do the adding. ``scale_cuda`` halves a frame in
whole 2 x 2 blocks with the integer mean ``(a + b + c + d + 2) >> 2``, which rounds only when the sum is not a multiple
of 4. Run on 16-bit samples that hold a sample in their high bits, with the low bits zero, every halving up to the
cell's size is exact, so what comes back is the exact mean of every cell (``canonical.Cells``), an eighth of the
bytes, and ``canonical.picture`` gives the same picture byte for byte as it gives from the native samples.

- 8-bit sources are first widened to ``p016le`` (``v << 8``, a plain copy for limited range), which leaves 8 spare bits:
  four halvings (16 x 16 cells) would be exact. A full-range source is widened as ``v * 257``, which is not a shift: it
  rounds by under half a unit per halving, so ``frame_cells`` recovers the exact sum from it and refuses a frame that
  is not within that bound.
- 10-bit sources already are ``p010le`` (``v << 6``): six spare bits, three halvings (8 x 8 cells), whatever the range.

Cells are 2, 4 or 8 native samples wide (480 x 270 from 960, 1920 and 3840 pixels wide). Frames of another shape, and
any region that is not the whole frame, are decoded to the CPU as before. ``acceleration`` proves every device's
reduction against the CPU's on the packaged clips before it is used, and ``media`` checks every frame's bytes against
what FFmpeg's own checksum says it produced.
"""

from __future__ import annotations

import zlib
from typing import NamedTuple

import numpy as np

from . import canonical

# The bit depth of each pixel format reduced: the 4:2:0 formats a hardware decoder produces.
DEPTHS = {"yuv420p": 8, "yuvj420p": 8, "yuv420p10le": 10}
# What FFmpeg downloads and writes, by the source's bit depth.
PIXEL_FORMAT = {8: "p016le", 10: "p010le"}
# What the mean of a cell is worth in a reduced frame's 16-bit samples: 256 times its value on the 8-bit scale.
SAMPLE_UNIT = {8: 256, 10: 64}
# The cell sides (native luma samples) that reduce exactly in both depths: three halvings.
CELL_SIDES = (2, 4, 8)


class Plan(NamedTuple):
    """How one stream's frames are reduced: its source's bit depth, the cell's side in native samples, the region
    (always the whole frame) and the filters that take the decoder's CUDA surface to the frame that is downloaded."""

    depth: int
    cell: int
    region: canonical.Region
    filters: tuple[str, ...]

    @property
    def pix(self) -> str:
        return PIXEL_FORMAT[self.depth]

    @property
    def size(self) -> int:
        """The bytes of one reduced frame: its luma plane and its interleaved colour plane, 16 bits a sample."""
        return 3 * self.region.rows * self.region.columns


def plan_with_cell(pix, width, height, cell):
    """The plan for a ``pix`` frame of ``width`` x ``height`` whose cells are ``cell`` x ``cell`` samples, or None when
    that cannot be reduced exactly (a format other than 4:2:0 8-bit or 10-bit, another cell size, or sides that do not
    hold whole colour cells). ``plan_for`` is it for a real stream; the device check runs it on the clips' 1280 x 720
    frames, which are no stream's grid."""
    depth = DEPTHS.get(pix)
    if depth is None or cell not in CELL_SIDES or width % (2 * cell) or height % (2 * cell):
        return None
    steps = cell.bit_length() - 1
    filters = ["scale_cuda=w=iw:h=ih:interp_algo=nearest:format=p016le"] if depth == 8 else []
    filters += ["scale_cuda=w=iw/2:h=ih/2:interp_algo=bilinear"] * steps
    filters += ["hwdownload", "format=" + PIXEL_FORMAT[depth]]
    region = canonical.Region(0, 0, width, height, height // cell, width // cell)
    return Plan(depth, cell, region, tuple(filters))


def plan_for(pix, width, height, sample_aspect_ratio, region=None):
    """How to reduce the frames of a ``pix`` stream of ``width`` x ``height`` on the GPU, or None when the CPU has to
    be given them whole: a format other than 4:2:0 8-bit or 10-bit, a picture that is not the whole frame in square
    cells of 2, 4 or 8 samples, or a ``region`` other than the frame's own."""
    whole = canonical.region(width, height, sample_aspect_ratio)
    if region is not None and region != whole:
        return None
    if (whole.left, whole.top, whole.width, whole.height) != (0, 0, width, height):
        return None
    plan = plan_with_cell(pix, width, height, width // whole.columns)
    return plan if plan is not None and plan.region == whole else None


def _snap(values, plan):
    """The exact cell means of a full-range 8-bit plane from what ``v * 257`` widening and the halvings left of them.

    Cell (sum of ``c * c`` samples) ``s`` arrives as ``257 * s / c^2`` within ``steps / 2``: each of the ``steps``
    halvings rounds by at most half a unit and an earlier error only shrinks in the next mean."""
    area = plan.cell * plan.cell
    exact = values.astype(np.float64) * (area / 257.0)
    sums = np.rint(exact)
    steps = plan.cell.bit_length() - 1
    if np.abs(exact - sums).max() > steps / 2 * area / 257.0 + 1e-6:
        raise ValueError("The GPU's reduction of this full-range frame is not exact")
    return (sums * (SAMPLE_UNIT[8] // area)).astype(np.uint16)


def frame_cells(payload, plan, full_range):
    """The cells of one reduced frame as it came off the pipe: ``plan.size`` bytes of ``plan.pix``, the luma plane and
    then the colour plane with its blue and red samples alternating. ``ValueError`` when the bytes are not what the
    reduction can have produced (a sum that is not a whole count of 256ths), so a GPU that rounds is never believed."""
    if len(payload) != plan.size:
        raise ValueError("A reduced frame has the wrong size")
    rows, columns = plan.region.rows, plan.region.columns
    samples = np.frombuffer(payload, dtype="<u2")
    colour = samples[rows * columns :].reshape(rows // 2, columns)
    planes = [samples[: rows * columns].reshape(rows, columns), colour[:, 0::2], colour[:, 1::2]]
    if plan.depth == 8 and full_range:
        planes = [_snap(values, plan) for values in planes]
    else:
        # A sum of c * c samples is a multiple of this many of the frame's units: anything else was rounded.
        unit = SAMPLE_UNIT[plan.depth] // (plan.cell * plan.cell)
        if unit > 1 and any((values & (unit - 1)).any() for values in planes):
            raise ValueError("The GPU's reduction is not exact")
    return canonical.Cells(*planes, plan.region)


def reduce_planes(planes, plan):
    """The exact cells of a native frame, by whole-number sums in numpy: ``planes`` are its three planes as 2-D arrays
    (luma, then the colour planes at half size). The reference the GPU is held to, and what a frame the CPU decoded
    is compared with when it takes over from a GPU decode."""
    unit = SAMPLE_UNIT[plan.depth] // (plan.cell * plan.cell)
    cell = plan.cell
    cells = []
    for plane in planes:
        rows, columns = plane.shape
        blocks = np.asarray(plane).reshape(rows // cell, cell, columns // cell, cell)
        cells.append((blocks.sum(axis=(1, 3), dtype=np.uint32) * unit).astype(np.uint16))
    return canonical.Cells(*cells, plan.region)


def fingerprint(cells):
    """A frame's cells as three checksums: what two decodes of one frame must agree on."""
    return tuple(zlib.adler32(np.ascontiguousarray(plane)) for plane in (cells.luma, cells.blue, cells.red))


def payload_checksums(cells):
    """The two plane checksums ``showinfo`` reports for the frame these cells came as: its luma plane and its
    interleaved colour plane (FFmpeg's Adler-32 starts from 0, as the reader's own checks of the pipe do)."""
    rows, columns = cells.luma.shape
    colour = np.empty((rows // 2, columns), dtype="<u2")
    colour[:, 0::2], colour[:, 1::2] = cells.blue, cells.red
    return zlib.adler32(np.ascontiguousarray(cells.luma), 0), zlib.adler32(colour, 0)
