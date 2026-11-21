"""The GPU's share of a comparison, held to the CPU's: reduced frames, batched pair metrics, the session's lifecycle.

Nothing here needs a GPU. The reduction is emulated in numpy (the real chain is checked on the device itself, by the
startup check in ``acceleration``) and the GPU session is a stand-in that measures with the CPU's own functions.
"""

import zlib
from contextlib import contextmanager
from dataclasses import replace
from fractions import Fraction
from types import SimpleNamespace

import numpy as np
import pytest

from media_preview_generator import reprise
from media_preview_generator.reprise import acceleration, analysis, canonical, gpu_pairs, media, recurrence, reduction
from media_preview_generator.reprise.acceleration import Accelerator
from media_preview_generator.reprise.identity import media_version

NVIDIA = Accelerator("cuda", "cuda:0", "NVIDIA", "Tesla P40", ("-hwaccel", "cuda", "-hwaccel_device", "0"))


@pytest.fixture(autouse=True)
def fresh_state():
    acceleration.reset()
    yield
    acceleration.reset()


# ---- the reduction: plans, the bytes a GPU leaves, the exact cells they give back --------------------------------------


@pytest.mark.parametrize(
    ("pix", "size", "cell"),
    [
        ("yuv420p", (1920, 1080), 4),
        ("yuv420p10le", (1920, 1080), 4),
        ("yuv420p", (960, 540), 2),
        ("yuv420p", (3840, 2160), 8),
        ("yuvj420p", (1920, 1080), 4),
    ],
)
def test_plans_reduce_square_pixel_420_frames_to_their_canonical_grid(pix, size, cell):
    plan = reduction.plan_for(pix, *size, Fraction(1))
    assert plan is not None and plan.cell == cell and plan.region == canonical.region(*size)
    assert sum(f.startswith("scale_cuda=w=iw/2") for f in plan.filters) == cell.bit_length() - 1
    assert plan.filters[-2:] == ("hwdownload", "format=" + plan.pix)
    assert plan.filters[0].endswith("format=p016le") == (plan.depth == 8)
    assert plan.size == 3 * plan.region.rows * plan.region.columns


@pytest.mark.parametrize(
    ("pix", "size", "sar"),
    [
        ("yuv420p", (1280, 720), Fraction(1)),  # cells of 5 samples
        ("yuv420p", (1918, 1078), Fraction(1)),  # the canonical picture is a centred part of the frame
        ("yuv420p", (480, 270), Fraction(1)),  # cells of one sample
        ("yuv420p", (1920, 1080), Fraction(4, 3)),
        ("yuv422p", (1920, 1080), Fraction(1)),
        ("yuv444p", (1920, 1080), Fraction(1)),
        ("yuv420p12le", (1920, 1080), Fraction(1)),
    ],
)
def test_everything_else_stays_whole_on_the_cpu(pix, size, sar):
    assert reduction.plan_for(pix, *size, sar) is None


def test_only_the_frames_own_region_is_reduced():
    whole = canonical.region(1920, 1080)
    assert reduction.plan_for("yuv420p", 1920, 1080, Fraction(1), whole) is not None
    assert reduction.plan_for("yuv420p", 1920, 1080, Fraction(1), canonical.Region(2, 2, 1916, 1076, 269, 479)) is None


def planes_of(depth, width, height, seed):
    rng = np.random.default_rng(seed)
    dtype = np.uint8 if depth == 8 else np.uint16
    shapes = ((height, width), (height // 2, width // 2), (height // 2, width // 2))
    return [rng.integers(0, 256 if depth == 8 else 1024, shape).astype(dtype) for shape in shapes]


def emulate_gpu(arrays, plan, full):
    """The bytes the scale_cuda chain leaves of a frame: its samples widened to 16 bits (the 10-bit ones shifted up, the
    8-bit ones times 256, or times 257 for a full range file), every halving averaging 2 x 2 samples and rounding, and
    the colour planes interleaved after the luma plane."""
    done = []
    for plane in arrays:
        values = plane.astype(np.float64) * ((257 if full else 256) if plan.depth == 8 else 64)
        for _ in range(plan.cell.bit_length() - 1):
            rows, columns = values.shape
            values = np.floor(values.reshape(rows // 2, 2, columns // 2, 2).mean(axis=(1, 3)) + 0.5)
        done.append(values.astype("<u2"))
    rows, columns = done[0].shape
    colour = np.empty((rows // 2, columns), dtype="<u2")
    colour[:, 0::2], colour[:, 1::2] = done[1], done[2]
    return done[0].tobytes() + colour.tobytes()


FORMATS = [(8, False), (8, True), (10, False), (10, True)]
PIX = {8: "yuv420p", 10: "yuv420p10le"}


@pytest.mark.parametrize(("depth", "full"), FORMATS)
@pytest.mark.parametrize("cell", reduction.CELL_SIDES)
def test_the_reduced_bytes_give_back_the_exact_cells_of_the_native_planes(depth, full, cell):
    width, height = 32 * cell, 16 * cell
    plan = reduction.plan_with_cell(PIX[depth], width, height, cell)
    arrays = planes_of(depth, width, height, seed=cell + depth)
    cells = reduction.frame_cells(emulate_gpu(arrays, plan, full), plan, full)
    expected = reduction.reduce_planes(arrays, plan)
    assert cells.region == plan.region == expected.region
    assert all(np.array_equal(a, b) and a.dtype == np.uint16 for a, b in zip(cells[:3], expected[:3], strict=True))
    unit = reduction.SAMPLE_UNIT[depth] // (cell * cell)  # a cell's sum, as a count of what the bytes are worth
    assert cells.luma[1, 2] == int(arrays[0][cell : 2 * cell, 2 * cell : 3 * cell].astype(np.uint32).sum()) * unit
    assert cells.luma.shape == (plan.region.rows, plan.region.columns) and cells.blue.shape == cells.red.shape


@pytest.mark.parametrize(("depth", "full", "cell"), [(8, False, 4), (10, False, 2), (8, True, 4), (10, True, 4)])
def test_a_reduction_that_rounded_is_refused(depth, full, cell):
    plan = reduction.plan_with_cell(PIX[depth], 32 * cell, 16 * cell, cell)
    payload = np.frombuffer(emulate_gpu(planes_of(depth, 32 * cell, 16 * cell, 3), plan, full), dtype="<u2").copy()
    payload[5] += 37  # no sum of whole samples comes to this
    with pytest.raises(ValueError, match="not exact"):
        reduction.frame_cells(payload.tobytes(), plan, full)
    with pytest.raises(ValueError, match="wrong size"):
        reduction.frame_cells(payload.tobytes()[:-2], plan, full)


def test_the_cells_checksums_are_the_ones_showinfo_reports_for_the_wire_planes():
    plan = reduction.plan_with_cell("yuv420p", 256, 128, 4)
    payload = emulate_gpu(planes_of(8, 256, 128, 5), plan, False)
    cells = reduction.frame_cells(payload, plan, False)
    luma_bytes = plan.region.rows * plan.region.columns * 2
    # FFmpeg seeds its plane_checksum (an Adler-32) with 0, zlib with 1; the reader's own pipe check uses 0 too.
    assert reduction.payload_checksums(cells) == (
        zlib.adler32(payload[:luma_bytes], 0),
        zlib.adler32(payload[luma_bytes:], 0),
    )
    assert zlib.adler32(payload[:luma_bytes]) != reduction.payload_checksums(cells)[0]
    assert reduction.fingerprint(cells) == reduction.fingerprint(
        reduction.reduce_planes(planes_of(8, 256, 128, 5), plan)
    )


def native_frame(arrays, depth, full, size):
    width = 1 if depth == 8 else 2
    dtype = np.uint8 if depth == 8 else "<u2"
    planes = tuple(
        media.NativePlane(memoryview(a.astype(dtype).tobytes()), a.shape[1], a.shape[0], a.shape[1] * width, width)
        for a in arrays
    )
    return media.NativeFrame(
        Fraction(0), planes, *size, PIX[depth], depth, "pc" if full else "tv", "bt709", "bt709", "bt709", "left",
        sample_aspect_ratio=Fraction(1), interpretation_flags=(),
    )  # fmt: skip


@pytest.mark.parametrize(("depth", "full"), FORMATS)
@pytest.mark.parametrize("size", [(960, 540), (1920, 1080)])
def test_a_reduced_frames_picture_is_the_native_frames_picture_to_the_bit(depth, full, size):
    plan = reduction.plan_for(PIX[depth], *size, Fraction(1))
    arrays = planes_of(depth, *size, seed=size[0] + depth)
    native = native_frame(arrays, depth, full, size)
    reduced = replace(native, planes=(), cells=reduction.frame_cells(emulate_gpu(arrays, plan, full), plan, full))
    for ours, theirs in zip(canonical.picture(reduced), canonical.picture(native), strict=True):
        assert ours.dtype == theirs.dtype == np.float32 and np.array_equal(ours, theirs)
    with pytest.raises(ValueError, match="region"):
        canonical.picture(reduced, canonical.Region(2, 2, size[0] - 4, size[1] - 4, 269, 479))


# ---- pairs measured in batches ------------------------------------------------------------------------------------------

GRID = canonical.Region(0, 0, 192, 128, 32, 48)
FRAMING = canonical.Framing(GRID, GRID, False)
TIGHTEST = 3.5  # code values: a frame this much brighter all over is exactly at the tightest limit


def cells_frame(luma, pts=0, full=False, region=GRID):
    """A frame as the GPU hands it over: 32 x 48 luma cells in 1/256ths and its colour cells, no planes."""
    chroma = np.full((16, 24), 128 * 256, dtype=np.uint16)
    cells = canonical.Cells(np.asarray(luma, dtype=np.uint16), chroma, chroma.copy(), region)
    return media.NativeFrame(
        Fraction(pts), (), 192, 128, "yuv420p", 8, "pc" if full else "tv", "bt709", "bt709", "bt709", "left",
        sample_aspect_ratio=Fraction(1), interpretation_flags=(), cells=cells,
    )  # fmt: skip


def scene(seed):
    return np.random.default_rng(seed).integers(40, 200, (32, 48)) * 256


class CpuSession:
    """The GPU session's stand-in: the CPU's own metrics (times ``scale``), or an error to raise."""

    def __init__(self, scale=1.0, error=None):
        self.scale, self.error, self.batches = scale, error, []

    def metrics(self, left, right, pairs, *, left_full, right_full, concentration_level):
        self.batches.append((len(left), len(right), len(pairs)))
        if self.error is not None:
            raise self.error
        rows = []
        for i, j in pairs:
            a = canonical._reduced_picture(left[i], left_full, left[i].region)
            b = canonical._reduced_picture(right[j], right_full, right[j].region)
            metrics = recurrence.picture_metrics(a, b)
            rows.append([metrics[name] for name in recurrence.PICTURE_METRICS])
        return np.array(rows) * self.scale


def settled_by(monkeypatch, session):
    monkeypatch.setattr(acceleration, "compare_session", lambda: session)
    settled = {}
    return settled, gpu_pairs.GpuPairs(FRAMING, lambda index, entry, excess: settled.update({(index, entry): excess}))


def test_a_batch_settles_every_pair_with_the_cpus_verdict_and_the_cpus_ratio_wherever_the_cpu_stops(monkeypatch):
    session = CpuSession()
    settled, pairs = settled_by(monkeypatch, session)
    base = scene(1)
    brighter = [0, 1, 2, 3.5, 3.6, 5, 6.9, 7.0, 7.2, 9, 30]
    frames = [(cells_frame(base), cells_frame(base + round(d * 256))) for d in brighter]
    for index, (frame, other) in enumerate(frames):
        assert pairs.offer(index, index, frame, other)
    pairs.flush()
    assert session.batches == [(len(frames), len(frames), len(frames))]
    for index, (frame, other) in enumerate(frames):
        cpu, gpu = gpu_pairs.cpu_excess(frame, other, FRAMING), settled[(index, index)]
        assert (gpu <= 1, gpu <= recurrence.NEAR_RATIO) == (cpu <= 1, cpu <= recurrence.NEAR_RATIO)
        if cpu <= recurrence.NEAR_RATIO:
            assert gpu == cpu  # the CPU stops early only above the near-miss limit, and the verdict is already given


def test_a_pair_within_the_band_of_a_limit_is_measured_again_and_takes_the_cpus_ratio(monkeypatch):
    base = scene(2)
    at_limit = cells_frame(base), cells_frame(base + round(TIGHTEST * 256))
    cpu = gpu_pairs.cpu_excess(*at_limit, FRAMING)
    assert cpu == pytest.approx(1.0, abs=1e-12)
    settled, pairs = settled_by(
        monkeypatch, CpuSession(scale=1 + 1e-7)
    )  # sums that differ from the CPU's in the 8th digit
    assert pairs.offer(0, 0, *at_limit)
    pairs.flush()
    assert settled[(0, 0)] == cpu and acceleration.compare_status()["cpu_rechecks"] == 1
    far = cells_frame(base), cells_frame(base + 5 * 256)
    assert pairs.offer(1, 1, *far)
    pairs.flush()
    assert settled[(1, 1)] == pytest.approx(gpu_pairs.cpu_excess(*far, FRAMING) * (1 + 1e-7), rel=1e-12)
    assert acceleration.compare_status()["pairs"] == 2 and acceleration.compare_status()["cpu_rechecks"] == 1


def test_a_gpu_error_hands_the_batch_and_the_rest_of_the_window_to_the_cpu(monkeypatch):
    session = CpuSession(error=RuntimeError("CUDA_ERROR_OUT_OF_MEMORY"))
    settled, pairs = settled_by(monkeypatch, session)
    base = scene(3)
    frame, other = cells_frame(base), cells_frame(base + 5 * 256)
    assert pairs.offer(0, 0, frame, other)
    pairs.flush()
    assert settled[(0, 0)] == gpu_pairs.cpu_excess(frame, other, FRAMING)
    assert not pairs.offer(1, 1, frame, other)  # the window's later pairs go straight to the CPU
    assert acceleration.compare_status()["cpu_fallbacks"] == 1 and len(session.batches) == 1


@pytest.mark.parametrize("bad", [np.full((1, 7), np.nan), np.ones((2, 7)), np.ones((1, 6))])
def test_numbers_that_cannot_be_right_are_not_believed(monkeypatch, bad):
    session = CpuSession()
    session.metrics = lambda *args, **kwargs: bad
    settled, pairs = settled_by(monkeypatch, session)
    base = scene(4)
    frame, other = cells_frame(base), cells_frame(base)
    assert pairs.offer(0, 0, frame, other)
    pairs.flush()
    assert settled[(0, 0)] == 0.0 and acceleration.compare_status()["cpu_fallbacks"] == 1


def test_frames_the_gpu_did_not_reduce_or_cannot_pair_are_left_to_the_cpu(monkeypatch):
    asked = []
    session = CpuSession()
    monkeypatch.setattr(acceleration, "compare_session", lambda: asked.append(1) or session)
    pairs = gpu_pairs.GpuPairs(FRAMING, lambda *args: None)
    base = scene(5)
    whole = replace(cells_frame(base), cells=None)
    assert not pairs.offer(0, 0, whole, cells_frame(base)) and not pairs.offer(0, 0, cells_frame(base), whole)
    elsewhere = cells_frame(base, region=canonical.Region(0, 0, 192, 128, 16, 24))
    assert (
        not pairs.offer(0, 0, elsewhere, cells_frame(base)) and not asked
    )  # the session is asked for once a pair is fit
    assert pairs.offer(0, 0, cells_frame(base), cells_frame(base))
    assert not pairs.offer(1, 1, cells_frame(base), cells_frame(base, full=True))  # one window, one colour range
    assert not pairs.offer(2, 2, cells_frame(base, full=True), cells_frame(base)) and asked == [1]
    no_gpu = gpu_pairs.GpuPairs(FRAMING, lambda *args: None)
    monkeypatch.setattr(acceleration, "compare_session", lambda: None)
    assert not no_gpu.offer(0, 0, cells_frame(base), cells_frame(base))


def test_pairs_are_sent_in_batches_and_each_frame_only_once(monkeypatch):
    monkeypatch.setattr(gpu_pairs, "BATCH_PAIRS", 3)
    session = CpuSession()
    settled, pairs = settled_by(monkeypatch, session)
    base = scene(6)
    target = cells_frame(base)
    references = [cells_frame(base, pts=n) for n in range(7)]
    for n, reference in enumerate(references):
        assert pairs.offer(0, n, target, reference)
    pairs.flush()
    pairs.flush()  # nothing queued: nothing sent
    assert session.batches == [(1, 3, 3), (1, 3, 3), (1, 1, 1)] and len(settled) == 7


# ---- the whole window: GPU pairs and CPU pairs decide the same --------------------------------------------------------

FILM = Fraction(1001, 24)


class Reader:
    """What analysis sees of a native frame reader: an iterator that knows its stream's time base."""

    def __init__(self, frames):
        self.frames, self.time_base = iter(frames), Fraction(1, 1000)

    def __iter__(self):
        return self

    def __next__(self):
        return next(self.frames)


def window(monkeypatch, tmp_path, session, brighter, framing=FRAMING):
    target, reference = (tmp_path / name for name in ("target", "reference"))
    items = []
    for path in (target, reference):
        path.write_bytes(path.name.encode())
        items.append(media_version(str(path), 3000, "{}"))
    times = [round(n * FILM) for n in range(70)]
    base = scene(7)
    frames = {
        items[0].id: [cells_frame(base, pts=t) for t in times],
        items[1].id: [
            cells_frame(base + (round(brighter * 256) if n == 30 else 0), pts=t) for n, t in enumerate(times)
        ],
    }
    asked = []

    @contextmanager
    def native(item, *args, **kwargs):
        asked.append(kwargs.get("reduce"))
        yield Reader(frames[item.id])

    monkeypatch.setattr(media, "native_frames", native)
    monkeypatch.setattr(acceleration, "compare_session", lambda: session)
    clock = recurrence.Clock(Fraction(1), Fraction(0))
    args = (
        items[0],
        items[1],
        0,
        0,
        Fraction(0),
        Fraction(2800),
        clock,
        Fraction(0),
        framing,
        lambda: False,
        lambda: False,
        1,
    )
    return analysis._video_support(*args), asked, times


@pytest.mark.parametrize(("brighter", "pieces"), [(0, 1), (5, 1), (8, 2)])
def test_a_window_compared_on_the_gpu_finds_what_the_cpu_finds(monkeypatch, tmp_path, brighter, pieces):
    cpu, asked, times = window(monkeypatch, tmp_path, None, brighter)
    session = CpuSession()
    gpu, _, _ = window(monkeypatch, tmp_path, session, brighter)
    assert gpu == cpu and len(cpu) == pieces
    assert asked == [GRID, GRID]  # the frames come reduced
    assert sum(batch[2] for batch in session.batches) >= len(times)  # and their pairs are measured in batches


def test_a_measured_framing_is_never_reduced_and_never_batched(monkeypatch, tmp_path):
    """Measuring where two frames line up needs their planes: the decoder is asked for whole frames and the CPU pairs."""
    monkeypatch.setattr(canonical, "registered", lambda framing, frame, other: (framing.target, framing.reference))
    session = CpuSession()
    found, asked, _ = window(monkeypatch, tmp_path, session, 0, framing=canonical.Framing(GRID, GRID, True))
    assert asked == [None, None] and session.batches == [] and len(found) == 1


# ---- the session's lifecycle: opened late, shared, closed with the last analysis --------------------------------------


def install_compare(monkeypatch, open_error=None, selftest_error=None):
    class GpuUnavailable(Exception):
        pass

    class GpuMismatch(GpuUnavailable):
        pass

    owner = SimpleNamespace(sessions=[], selftests=0, attempts=0, errors=(open_error, selftest_error))

    class Session:
        def __init__(self, **options):
            self.options, self.closed, self.budget = options, False, 512 << 20

        def __enter__(self):
            owner.attempts += 1
            if owner.errors[0]:
                raise owner.errors[0]
            owner.sessions.append(self)
            return self

        def selftest(self):
            owner.selftests += 1
            if owner.errors[1]:
                raise owner.errors[1]

        def close(self):
            self.closed = True

    owner.module = SimpleNamespace(
        available=lambda: True, GpuUnavailable=GpuUnavailable, GpuMismatch=GpuMismatch, Session=Session
    )
    monkeypatch.setattr(reprise, "cuda_compare", owner.module, raising=False)
    monkeypatch.setattr(acceleration, "enabled", lambda: True)
    monkeypatch.setattr(acceleration, "plan", lambda ffmpeg: [NVIDIA])
    monkeypatch.setattr(acceleration, "ffmpeg_binary", lambda: "ffmpeg")
    monkeypatch.setattr(acceleration, "vram_cap_mb", lambda: 1536)
    return owner


def test_the_session_opens_on_first_use_is_shared_and_closes_with_the_last_lease(monkeypatch):
    owner = install_compare(monkeypatch)
    assert acceleration.compare_session() is None  # no analysis, no lease, no session
    with acceleration.compare_lease():
        assert owner.attempts == 0 and not acceleration.compare_status()["running"]
        first = acceleration.compare_session()
        with acceleration.compare_lease():
            assert acceleration.compare_session() is first
        assert not first.closed and acceleration.compare_status()["budget_mb"] == 512
    assert first.closed and not acceleration.compare_status()["running"]
    assert first.options == {"device": 0, "cap_bytes": 1536 << 20, "margin_bytes": 1 << 30}
    with acceleration.compare_lease():
        second = acceleration.compare_session()
    assert second is not first and second.closed and owner.selftests == 1  # the self-test runs once per process


def test_a_lease_that_ends_by_an_error_still_closes_the_session(monkeypatch):
    install_compare(monkeypatch)
    with pytest.raises(RuntimeError, match="analysis failed"):
        with acceleration.compare_lease():
            session = acceleration.compare_session()
            raise RuntimeError("analysis failed")
    assert session.closed


def test_a_card_that_cannot_take_the_work_leaves_it_to_the_cpu_without_failing(monkeypatch):
    owner = install_compare(monkeypatch)
    owner.errors = (owner.module.GpuUnavailable("only 100 MiB of video memory is free"), None)
    with acceleration.compare_lease():
        assert acceleration.compare_session() is None and acceleration.compare_session() is None
    assert owner.attempts == 1  # asked once per lease, not once per pair
    assert "100 MiB" in acceleration.status()["last_problem"]
    owner.errors = (None, None)
    with acceleration.compare_lease():
        assert acceleration.compare_session() is not None  # the next analysis tries again


def test_kernels_that_disagree_with_the_cpu_are_switched_off_until_the_app_restarts(monkeypatch):
    owner = install_compare(monkeypatch)
    owner.errors = (None, owner.module.GpuMismatch("signed32 differs on the test frames"))
    with acceleration.compare_lease():
        assert acceleration.compare_session() is None
    assert owner.sessions[0].closed
    status = acceleration.compare_status()
    assert status["enabled"] is False and "signed32" in status["problem"]
    owner.errors = (None, None)
    with acceleration.compare_lease():
        assert acceleration.compare_session() is None and owner.attempts == 1


def test_without_the_gpu_switch_or_the_kernels_the_comparison_is_the_cpus(monkeypatch):
    install_compare(monkeypatch)
    assert acceleration.compare_enabled()
    monkeypatch.setattr(acceleration, "enabled", lambda: False)
    assert not acceleration.compare_enabled()
    with acceleration.compare_lease():
        assert acceleration.compare_session() is None
    monkeypatch.setattr(acceleration, "enabled", lambda: True)
    monkeypatch.setattr(reprise.cuda_compare, "available", lambda: False)
    assert not acceleration.compare_enabled()
    monkeypatch.setattr(reprise.cuda_compare, "available", lambda: 1 / 0)  # a driver library that will not load
    assert not acceleration.compare_enabled()
