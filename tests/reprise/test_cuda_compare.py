"""The batched GPU comparison of canonical pictures (cuda_compare).

Most tests run everywhere, against a fake CUDA driver. It keeps "device memory" as numpy bytes with a capacity, binds
one context per thread, remembers everything left live, and emulates both kernels with the CPU reference, so these
tests hold the session itself to account: its memory budget, chunking, shrinking when the device is full, cleanup,
threads, and the arguments it hands the kernels. The tests marked ``gpu`` (``pytest -m gpu``) run the real kernels on a
real GPU and skip where there is none.
"""

import ctypes
import hashlib
import threading
import tomllib
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from loguru import logger

from media_preview_generator.reprise import canonical, cuda_compare, recurrence
from media_preview_generator.reprise.cuda_compare import GpuMismatch, GpuOutOfMemory, GpuUnavailable, Session

LEVEL = recurrence.VIDEO["concentration_level"]
FLAGS = [(False, False), (True, False), (False, True), (True, True)]
GIB = 1024**3
KERNELS_DIR = Path(cuda_compare.__file__).parent / "cuda_kernels"
real_gpu = pytest.mark.gpu


# ---- frames and the CPU reference ------------------------------------------------------------------------------------


def make_cells(rng, rows, columns, step):
    """One frame's cells on a rows x columns grid: a smooth picture and noise, every sample a whole multiple of ``step``
    256ths (16 is what an 8-bit source gives, 4 what a 10-bit one does)."""
    region = canonical.Region(0, 0, 4 * columns, 4 * rows, rows, columns)
    half = ((rows + 1) // 2, (columns + 1) // 2)
    planes = []
    for (height, width), base in (((rows, columns), 140), (half, 128), (half, 110)):
        y, x = np.mgrid[0:height, 0:width]
        values = base + 60 * np.sin(x / 9.0) * np.cos(y / 7.0) + rng.normal(0, 6, (height, width))
        planes.append((np.clip(np.rint(values * 256 / step), 0, 65535 // step) * step).astype(np.uint16))
    luma, blue, red = planes
    return canonical.Cells(luma, blue, red, region)


def bulk(count, *, seed=7, rows=75, columns=133):
    """``count`` frames (8-bit-like and 10-bit-like alternately) and the same frames with a block of cells changed."""
    rng = np.random.default_rng(seed)
    left = [make_cells(rng, rows, columns, 4 if k % 2 else 16) for k in range(count)]
    right = []
    for k, cells in enumerate(left):
        size = 1 + k % 9
        luma, blue = cells.luma.astype(np.int64), cells.blue.astype(np.int64)
        y, x = (int(v) for v in rng.integers(0, [rows - size, columns - size]))
        luma[y : y + size, x : x + size] += 256 * int(rng.integers(1, 30))
        blue[2:6, 3:7] += 16 * int(rng.integers(0, 4))
        right.append(
            canonical.Cells(
                np.clip(luma, 0, 65535).astype(np.uint16),
                np.clip(blue, 0, 65535).astype(np.uint16),
                cells.red,
                cells.region,
            )
        )
    return left, right


def cpu_picture(cells, full):
    """What the CPU's ``canonical.picture`` makes of the cells."""
    frame = SimpleNamespace(
        interpretation_flags=(),
        planes=(),
        cells=cells,
        color_range="pc" if full else "tv",
        width=cells.region.width,
        height=cells.region.height,
        sample_aspect_ratio=Fraction(1),
    )
    return canonical.picture(frame, cells.region)


def cpu_metrics(left, right, pairs, left_full, right_full):
    """``recurrence.picture_metrics`` of every pair, as the rows ``Session.metrics`` returns."""
    cache = {}

    def picture(side, number, cells, full):
        if (side, number) not in cache:
            cache[side, number] = cpu_picture(cells, full)
        return cache[side, number]

    rows = []
    for i, j in np.asarray(pairs).tolist():
        values = recurrence.picture_metrics(
            picture("left", i, left[i], left_full), picture("right", j, right[j], right_full)
        )
        rows.append([values[name] for name in cuda_compare.METRICS])
    return np.array(rows, np.float64).reshape(len(rows), len(cuda_compare.METRICS))


def reference_plane_metrics(left, right, level):
    """``recurrence._plane_metrics`` with the concentration level a parameter, as the kernel takes it."""
    residual = left - right
    absolute = np.abs(residual)
    values = [float(absolute.max())]
    integral = recurrence._integral(absolute)
    values += [recurrence._local_max(integral, absolute.shape, size) for size in (4, 8)]
    values.append(recurrence._local_max(recurrence._integral(absolute > np.float32(level)), absolute.shape, 8))
    integral = recurrence._integral(residual)
    values += [recurrence._signed_local(residual, size, integral) for size in (8, 16, 32)]
    return values


# ---- the fake driver --------------------------------------------------------------------------------------------------

SIGNATURES = {("cells_to_picture", "ppqiiidd"), ("pair_metrics", "pppiiiiifp")}


class FakeDriver:
    """A CUDA driver made of numpy: byte-addressed memory with a capacity, one current context per thread, the two
    kernels emulated with the CPU reference. Misuse (a call with no context bound, freeing what is not allocated,
    reading past an allocation, arguments of the wrong kind) raises ``AssertionError``. ``wrong`` makes a kernel
    deliberately wrong ("pictures" or "metrics"); ``reported_free`` is what it says is free whatever the truth."""

    def __init__(
        self,
        *,
        capacity=8 * GIB,
        reported_free=None,
        wrong=None,
        fail_launch=None,
        free_failures=0,
        module_full=False,
    ):
        self.capacity = capacity
        self.reported_free = reported_free
        self.wrong = wrong
        self.fail_launch = fail_launch  # the 1-based number of the launch that fails
        self.free_failures = free_failures  # how many of the first frees fail
        self.module_full = module_full  # loading the module finds the device full
        self.lock = threading.RLock()
        self.memory = {}  # base pointer -> bytes: the live allocations
        self.next_pointer = 1 << 32
        self.contexts = set()
        self.modules = set()
        self.current = {}  # thread -> its current context
        self.next_handle = 0
        self.created = 0  # contexts ever created
        self.peak = 0  # most bytes allocated at once
        self.launches = []  # kernel names in launch order
        self.leaked = 0  # bytes (and modules) still held when their context was destroyed
        self.leaked_modules = 0

    @property
    def live_bytes(self):
        return sum(buffer.nbytes for buffer in self.memory.values())

    @property
    def live_contexts(self):
        return len(self.contexts)

    @property
    def live_modules(self):
        return len(self.modules)

    def _bound(self):
        assert self.current.get(threading.get_ident()) in self.contexts, "no live context is current on this thread"

    def create_context(self, device):
        if device != 0:
            raise GpuUnavailable(f"There is no NVIDIA GPU number {device}")
        with self.lock:
            self.next_handle += 1
            self.created += 1
            self.contexts.add(self.next_handle)
            return self.next_handle

    def destroy_context(self, context):
        with self.lock:
            assert context in self.contexts
            self.leaked += self.live_bytes
            self.leaked_modules += len(self.modules)
            self.memory.clear()
            self.modules.clear()
            self.contexts.discard(context)

    def current_context(self):
        return self.current.get(threading.get_ident())

    def set_current(self, context):
        assert context is None or context in self.contexts
        if context is None:
            self.current.pop(threading.get_ident(), None)
        else:
            self.current[threading.get_ident()] = context

    def memory_info(self):
        self._bound()
        return (self.capacity - self.live_bytes if self.reported_free is None else self.reported_free), self.capacity

    def allocate(self, nbytes):
        self._bound()
        assert nbytes > 0
        with self.lock:
            if self.live_bytes + nbytes > self.capacity:
                raise GpuOutOfMemory("The fake device is full")
            pointer = self.next_pointer
            self.next_pointer += (nbytes + 4095) // 4096 * 4096 + 4096
            self.memory[pointer] = np.zeros(nbytes, np.uint8)
            self.peak = max(self.peak, self.live_bytes)
            return pointer

    def free(self, pointer):
        self._bound()
        with self.lock:
            if self.free_failures:
                self.free_failures -= 1
                raise GpuUnavailable("The GPU reported CUDA_ERROR_UNKNOWN while freeing GPU memory")
            assert pointer in self.memory, "freeing something that is not an allocation"
            del self.memory[pointer]

    def _locate(self, pointer, nbytes):
        for base, buffer in self.memory.items():
            if base <= pointer < base + buffer.nbytes:
                assert pointer - base + nbytes <= buffer.nbytes, "access past the end of an allocation"
                return buffer, pointer - base
        raise AssertionError("access to memory that is not allocated")

    def _view(self, pointer, dtype, count):
        buffer, offset = self._locate(pointer, count * np.dtype(dtype).itemsize)
        return buffer[offset : offset + count * np.dtype(dtype).itemsize].view(dtype)

    def upload(self, pointer, data):
        self._bound()
        assert data.flags.c_contiguous
        buffer, offset = self._locate(pointer, data.nbytes)
        buffer[offset : offset + data.nbytes] = data.reshape(-1).view(np.uint8)

    def download(self, pointer, out):
        self._bound()
        assert out.flags.c_contiguous and out.flags.writeable
        buffer, offset = self._locate(pointer, out.nbytes)
        out.reshape(-1).view(np.uint8)[:] = buffer[offset : offset + out.nbytes]

    def load_module(self, image):
        self._bound()
        assert isinstance(image, bytes) and b"cells_to_picture" in image and b"pair_metrics" in image
        if self.module_full:
            raise GpuOutOfMemory("The fake device is full")
        with self.lock:
            self.next_handle += 1
            self.modules.add(self.next_handle)
            return self.next_handle

    def unload_module(self, module):
        self._bound()
        assert module in self.modules
        self.modules.discard(module)

    def function(self, module, name, signature):
        self._bound()
        assert module in self.modules and (name, signature) in SIGNATURES
        return name, signature

    def launch(self, function, grid, block, args):
        self._bound()
        name, signature = function
        assert len(args) == len(signature)
        for kind, value in zip(signature, args, strict=True):
            assert type(value) is (float if kind in "fd" else int), f"{value!r} is not an argument of kind {kind}"
        assert grid >= 1 and block == 256
        self.launches.append(name)
        if self.fail_launch == len(self.launches):
            raise GpuUnavailable("The GPU reported CUDA_ERROR_LAUNCH_FAILED (unspecified launch failure) while ...")
        getattr(self, "_kernel_" + name)(grid, *args)

    def _kernel_cells_to_picture(
        self, grid, cells, picture, count, frame_size, luma_size, full, luma_scale, chroma_scale
    ):
        values = self._view(cells, np.uint16, count).astype(np.float64) / 256.0
        if full:
            luma = np.arange(count) % frame_size < luma_size
            values = np.where(luma, 16 + values * luma_scale, 128 + (values - 128) * chroma_scale)
        out = self._view(picture, np.float32, count)
        out[:] = values.astype(np.float32)
        if self.wrong == "pictures":
            out[0] += 1

    def _kernel_pair_metrics(
        self, grid, left, right, pairs, count, luma_height, luma_width, colour_height, colour_width, level, out
    ):
        assert grid == 3 * count
        luma_size, colour_size = luma_height * luma_width, colour_height * colour_width
        frame = luma_size + 2 * colour_size
        a = self.memory[left].view(np.float32).reshape(-1, frame)
        b = self.memory[right].view(np.float32).reshape(-1, frame)
        table = self._view(pairs, np.int32, 2 * count).reshape(count, 2)
        result = self._view(out, np.float64, count * 3 * len(cuda_compare.METRICS)).reshape(count, 3, -1)
        planes = (
            (0, luma_size, (luma_height, luma_width)),
            (luma_size, luma_size + colour_size, (colour_height, colour_width)),
            (luma_size + colour_size, frame, (colour_height, colour_width)),
        )
        for k, (i, j) in enumerate(table.tolist()):
            assert 0 <= i < len(a) and 0 <= j < len(b)
            for number, (start, stop, shape) in enumerate(planes):
                result[k, number] = reference_plane_metrics(
                    a[i, start:stop].reshape(shape), b[j, start:stop].reshape(shape), level
                )
        if self.wrong == "metrics":
            result[:, :, 0] += 1.0

    def clean(self):
        """Nothing live and nothing that had to be reclaimed by destroying a context."""
        return not (self.live_bytes or self.live_contexts or self.live_modules or self.leaked or self.leaked_modules)


def run(session, left, right, pairs, left_full=False, right_full=False):
    return session.metrics(
        left, right, np.asarray(pairs), left_full=left_full, right_full=right_full, concentration_level=LEVEL
    )


# ---- the module's constants and shipped kernels ---------------------------------------------------------------------


def test_the_metric_names_and_order_are_the_cpus():
    assert cuda_compare.METRICS == recurrence.PICTURE_METRICS


def test_the_test_replica_of_the_cpu_metrics_is_the_cpus():
    left, right, _pairs = cuda_compare._synthetic_frames()
    a, b = cpu_picture(left[3], False), cpu_picture(right[3], False)
    for x, y in zip(a, b, strict=True):
        assert reference_plane_metrics(x, y, LEVEL) == [value for _name, value in recurrence._plane_metrics(x, y)]


def test_the_ptx_was_built_from_the_current_source():
    source = (KERNELS_DIR / "compare.cu").read_text()
    ptx = (KERNELS_DIR / "compare.ptx").read_text()
    digest = hashlib.sha256(source.encode()).hexdigest()
    assert ptx.splitlines()[0] == f"// compare.cu sha256 {digest}", "compare.cu changed: run tools/build_reprise_ptx.py"
    assert ".visible .entry cells_to_picture(" in ptx and ".visible .entry pair_metrics(" in ptx
    assert "fma." not in ptx  # sums must not be fused: that would change them from the CPU's
    assert "#include" not in source  # the kernels need no NVIDIA header


def test_the_ptx_ships_in_the_wheel():
    """The Docker builder has no .git, so only ``package-data`` in pyproject.toml puts the PTX in the image: without it
    ``available()`` is quietly False there and every comparison runs on the CPU."""
    root = Path(cuda_compare.__file__).resolve().parents[2]
    package_data = tomllib.loads((root / "pyproject.toml").read_text())["tool"]["setuptools"]["package-data"]
    package = root / "media_preview_generator" / "reprise"
    patterns = package_data.get("media_preview_generator.reprise", [])
    shipped = {path.resolve() for pattern in patterns for path in package.glob(pattern)}
    assert cuda_compare.PTX.resolve() in shipped, (
        'add "media_preview_generator.reprise" = ["cuda_kernels/*.cu", "cuda_kernels/*.ptx"] to package-data'
    )


def test_the_selftest_frames_make_every_metric_non_zero():
    left, right, pairs = cuda_compare._synthetic_frames()
    worst = cpu_metrics(left, right, pairs, False, False).max(axis=0)
    assert (worst > 0).all(), dict(zip(cuda_compare.METRICS, worst, strict=True))
    assert {cells.luma.dtype for cells in left + right} == {np.dtype(np.uint16)}


# ---- opening: budget, kill switch, failures --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "free, cap, margin, expected",
    [
        (8 * GIB, 2 * GIB, GIB, 2 * GIB),  # the cap
        (3 * GIB // 2, 2 * GIB, GIB, GIB // 2),  # what is free beyond the margin
        (3 * GIB, GIB, 0, GIB),
    ],
)
def test_the_budget_is_the_smaller_of_the_cap_and_the_free_memory_beyond_the_margin(free, cap, margin, expected):
    fake = FakeDriver(reported_free=free)
    with Session(driver=fake, cap_bytes=cap, margin_bytes=margin) as session:
        assert session.budget == expected and type(session.budget) is int
    assert fake.clean()


def test_too_little_free_memory_means_no_session():
    fake = FakeDriver(reported_free=GIB + 10 * 1024**2)  # 10 MiB beyond the margin: less than a session needs
    with pytest.raises(GpuUnavailable, match="Not enough free GPU memory"):
        Session(driver=fake).__enter__()
    assert fake.created == 1 and fake.clean()


def test_a_full_device_cannot_load_the_kernels():
    fake = FakeDriver(module_full=True)
    with pytest.raises(GpuUnavailable, match="to load the comparison kernels"):
        Session(driver=fake).__enter__()
    assert fake.clean()


def test_the_kill_switch_stops_a_session_and_available(monkeypatch):
    monkeypatch.setattr(cuda_compare, "_libcuda", lambda: object())
    assert cuda_compare.available()
    monkeypatch.setenv("REPRISE_CUDA_COMPARE", "0")
    assert not cuda_compare.available()
    fake = FakeDriver()
    with pytest.raises(GpuUnavailable, match="switched off"):
        Session(driver=fake).__enter__()
    assert fake.created == 0


def test_available_needs_the_driver_library(monkeypatch):
    monkeypatch.setattr(cuda_compare, "_libcuda", lambda: None)
    assert not cuda_compare.available()


def test_a_session_that_is_not_open_refuses_work():
    left, right = bulk(2)
    fake = FakeDriver()
    session = Session(driver=fake)
    with pytest.raises(GpuUnavailable, match="not open"):
        run(session, left, right, [[0, 1]])
    with session:
        pass
    with pytest.raises(GpuUnavailable, match="not open"):
        run(session, left, right, [[0, 1]])
    assert fake.clean()


# ---- the answers ---------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("left_full, right_full", FLAGS)
@pytest.mark.parametrize("cap, chunked", [(2 * GIB, False), (1_000_000, True)])
def test_metrics_equal_the_cpus_however_the_pairs_are_chunked(monkeypatch, left_full, right_full, cap, chunked):
    monkeypatch.setattr(cuda_compare, "MIN_BUDGET_BYTES", 1)
    left, right, pairs = cuda_compare._synthetic_frames()
    pairs = pairs[np.random.default_rng(5).permutation(len(pairs))]  # in no order, frames used again in later chunks
    pairs = np.concatenate([pairs, pairs[:3]])  # and some pairs twice
    fake = FakeDriver()
    with Session(driver=fake, cap_bytes=cap, margin_bytes=0) as session:
        assert session.budget == cap
        got = run(session, left, right, pairs, left_full, right_full)
    assert got.dtype == np.float64 and got.shape == (len(pairs), 7)
    assert np.array_equal(got, cpu_metrics(left, right, pairs, left_full, right_full))
    assert (fake.launches.count("pair_metrics") > 2) if chunked else (fake.launches.count("pair_metrics") == 1)
    assert fake.peak == session.peak_held <= session.budget
    assert fake.clean()


def test_no_pairs_is_no_work():
    left, right = bulk(2)
    fake = FakeDriver()
    with Session(driver=fake) as session:
        for pairs in ([], np.zeros((0, 2), np.int64)):
            got = run(session, left, right, pairs)
            assert got.shape == (0, 7) and got.dtype == np.float64
    assert fake.launches == [] and fake.clean()


@pytest.mark.parametrize("pairs", [[[0, 5]], [[-1, 0]], [[0, 1, 2]], [0, 1], [[0.0, 1.0]]])
def test_malformed_pairs_are_a_value_error(pairs):
    left, right = bulk(2)
    with Session(driver=FakeDriver()) as session, pytest.raises(ValueError):
        run(session, left, right, pairs)


def test_frames_on_different_grids_are_a_value_error():
    left, _ = bulk(2)
    _, right = bulk(2, rows=77)
    with Session(driver=FakeDriver()) as session, pytest.raises(ValueError, match="not on one grid"):
        run(session, left, right, [[0, 0]])


def test_pairs_on_different_grids_are_the_cpus():
    rng = np.random.default_rng(9)
    left = [make_cells(rng, 75, 133, 16), make_cells(rng, 85, 133, 16)]
    right = [make_cells(rng, 75, 133, 16), make_cells(rng, 85, 133, 16)]
    fake = FakeDriver()
    with Session(driver=fake) as session, pytest.raises(GpuUnavailable, match="not all on one picture grid"):
        run(session, left, right, [[0, 0], [1, 1]])
    assert fake.clean()


def test_planes_below_32_samples_are_the_cpus():
    rng = np.random.default_rng(3)
    small = [make_cells(rng, 40, 40, 16)]  # luma 36 x 36, but colour 18 x 18 once the margins are left out
    fake = FakeDriver()
    with Session(driver=fake) as session:
        with pytest.raises(GpuUnavailable, match="smaller than 32"):
            run(session, small, small, [[0, 0]])
        with pytest.raises(GpuUnavailable, match="smaller than 32"):
            session.pictures(small, full=False)
        assert fake.live_bytes == 0 and fake.launches == []


def test_cells_that_are_not_16_bit_are_the_cpus():
    rng = np.random.default_rng(3)
    cells = make_cells(rng, 75, 133, 16)
    narrow = canonical.Cells(cells.luma.astype(np.uint8), cells.blue, cells.red, cells.region)
    with Session(driver=FakeDriver()) as session, pytest.raises(GpuUnavailable, match="16-bit"):
        run(session, [narrow], [cells], [[0, 0]])


def test_a_grid_of_exactly_32_colour_samples_is_taken():
    left, right = bulk(3, rows=68, columns=68)  # luma 64 x 64, colour 32 x 32: the smallest the kernels take
    pairs = [[0, 0], [1, 1], [2, 2], [0, 2]]
    with Session(driver=FakeDriver()) as session:
        got = run(session, left, right, pairs)
    assert np.array_equal(got, cpu_metrics(left, right, pairs, False, False))


@pytest.mark.parametrize("full", [False, True])
def test_pictures_equal_the_cpus_byte_for_byte(monkeypatch, full):
    monkeypatch.setattr(cuda_compare, "MIN_BUDGET_BYTES", 1)
    left, right, _pairs = cuda_compare._synthetic_frames()
    frames = left + right
    fake = FakeDriver()
    with Session(driver=fake, cap_bytes=600_000, margin_bytes=0) as session:  # a few frames at a time
        got = session.pictures(frames, full=full)
        assert session.pictures([], full=full) == []
    assert len(got) == len(frames) and fake.launches.count("cells_to_picture") > 2
    for picture, cells in zip(got, frames, strict=True):
        for a, b in zip(picture, cpu_picture(cells, full), strict=True):
            assert a.dtype == np.float32 and a.shape == b.shape and a.tobytes() == b.tobytes()
    assert fake.clean()


# ---- memory discipline ---------------------------------------------------------------------------------------------------


def test_a_full_device_halves_the_chunk_and_warns_once():
    left, right = bulk(16)
    pairs = [[k, k] for k in range(16)]
    fake = FakeDriver(capacity=400_000, reported_free=8 * GIB)  # the device is fuller than it said
    messages = []
    sink = logger.add(messages.append, level="WARNING", format="{message}")
    try:
        with Session(driver=fake) as session:
            got = run(session, left, right, pairs)
            again = run(session, left, right, pairs[:5])
    finally:
        logger.remove(sink)
    assert np.array_equal(got, cpu_metrics(left, right, pairs, False, False))
    assert np.array_equal(again, got[:5])
    assert len(messages) == 1 and "memory is tight" in messages[0]
    assert fake.peak <= 400_000 and fake.clean()


def test_a_device_too_full_for_one_pair_is_the_cpus():
    left, right = bulk(2)
    fake = FakeDriver(capacity=50_000, reported_free=8 * GIB)
    with Session(driver=fake) as session:
        with pytest.raises(GpuUnavailable, match="too little free memory"):
            run(session, left, right, [[0, 0], [1, 1]])
        assert fake.live_bytes == 0  # nothing held between calls
    assert fake.clean()


def test_the_session_never_holds_more_than_its_budget(monkeypatch):
    monkeypatch.setattr(cuda_compare, "MIN_BUDGET_BYTES", 1)
    left, right = bulk(4)
    fake = FakeDriver()
    with Session(driver=fake, cap_bytes=100_000, margin_bytes=0) as session:  # less than one pair needs
        with pytest.raises(GpuUnavailable, match="too little free memory"):
            run(session, left, right, [[0, 0]])
        assert session.peak_held <= 100_000
    assert fake.peak <= 100_000 and fake.clean()


@pytest.mark.parametrize("fail_launch", [1, 2, 3])  # a call launches kernel 1 for each side, then kernel 2
def test_everything_is_freed_when_a_launch_fails(fail_launch):
    left, right = bulk(3)
    fake = FakeDriver(fail_launch=fail_launch)
    with Session(driver=fake) as session:
        with pytest.raises(GpuUnavailable, match="LAUNCH_FAILED"):
            run(session, left, right, [[0, 0], [1, 2]])
        assert fake.live_bytes == 0 and session.peak_held > 0
        fake.fail_launch = None
        assert run(session, left, right, [[1, 1]]).shape == (1, 7)  # the session goes on working
    assert fake.clean()


def test_a_free_that_fails_does_not_leave_the_rest_behind():
    left, right = bulk(2)
    fake = FakeDriver(free_failures=2)
    with Session(driver=fake) as session:
        with pytest.raises(GpuUnavailable, match="freeing GPU memory"):
            run(session, left, right, [[0, 1]])
        assert fake.live_bytes > 0  # the allocation that would not go is still held, and known to the session
    assert fake.clean()


def test_the_session_closes_cleanly_after_an_exception_and_twice():
    left, right = bulk(2)
    fake = FakeDriver()
    with pytest.raises(RuntimeError, match="boom"), Session(driver=fake) as session:
        run(session, left, right, [[0, 1]])
        raise RuntimeError("boom")
    assert fake.clean()
    session.close()
    session.close()
    assert fake.clean()


def test_close_works_from_another_thread():
    left, right = bulk(2)
    fake = FakeDriver()
    session = Session(driver=fake).__enter__()
    run(session, left, right, [[0, 1]])
    errors = []

    def close():
        try:
            session.close()
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=close)
    thread.start()
    thread.join(10)
    assert not thread.is_alive() and not errors
    assert fake.clean()
    assert fake.current_context() is None


def test_threads_share_a_session_and_keep_their_own_context():
    left, right = bulk(8)
    expected = {
        name: pairs for name, pairs in (("a", [[0, 1], [2, 3]]), ("b", [[4, 5], [6, 7]]), ("c", [[1, 0], [3, 2]]))
    }
    fake = FakeDriver()
    results, errors = {}, []

    def work(name):
        try:
            for _ in range(3):
                results[name] = run(session, left, right, expected[name])
            assert fake.current_context() is None  # the session put this thread's own context (none) back
        except BaseException as exc:
            errors.append(exc)

    with Session(driver=fake) as session:
        threads = [threading.Thread(target=work, args=(name,)) for name in expected]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
    assert not errors, errors
    for name, pairs in expected.items():
        assert np.array_equal(results[name], cpu_metrics(left, right, pairs, False, False))
    assert fake.clean()


# ---- the self-test ---------------------------------------------------------------------------------------------------


def test_the_selftest_passes_on_a_correct_driver():
    fake = FakeDriver()
    with Session(driver=fake) as session:
        session.selftest()
    assert fake.clean()


@pytest.mark.parametrize("wrong", ["pictures", "metrics"])
def test_a_kernel_that_disagrees_with_the_cpu_is_a_mismatch(wrong):
    assert issubclass(GpuMismatch, GpuUnavailable)
    fake = FakeDriver(wrong=wrong)
    with Session(driver=fake) as session, pytest.raises(GpuMismatch, match="GPU self-test"):
        session.selftest()
    assert fake.clean()


def test_a_selftest_the_gpu_cannot_run_is_not_a_mismatch():
    fake = FakeDriver(fail_launch=3)
    with Session(driver=fake) as session, pytest.raises(GpuUnavailable) as caught:
        session.selftest()
    assert type(caught.value) is GpuUnavailable
    assert fake.clean()


# ---- the real driver, without a GPU ----------------------------------------------------------------------------------


class RecordingLibrary:
    """Stands in for libcuda.so.1 and records the kernel launch it is handed."""

    def __init__(self):
        self.seen = {}

    def cuLaunchKernel(self, handle, gx, gy, gz, bx, by, bz, shared, stream, params, extra):
        self.seen["geometry"] = (gx, gy, gz, bx, by, bz, shared, stream, extra)
        kinds = (ctypes.c_uint64, ctypes.c_uint64, ctypes.c_int32, ctypes.c_float, ctypes.c_double)
        self.seen["values"] = [ctypes.cast(params[k], ctypes.POINTER(kind))[0] for k, kind in enumerate(kinds)]
        return 0

    def cuGetErrorName(self, code, name):
        return 1  # unknown error code


def real_driver_with(library):
    driver = cuda_compare.CudaDriver.__new__(cuda_compare.CudaDriver)
    driver._lib = library
    return driver


def test_launch_hands_each_argument_to_the_driver_as_its_kind():
    library = RecordingLibrary()
    driver = real_driver_with(library)
    kernel = cuda_compare._Kernel(ctypes.c_void_p(1), "pqifd")
    driver.launch(kernel, 7, 256, (2**40 + 5, 2**33, -3, 12.0, 219 / 255))
    assert library.seen["geometry"] == (7, 1, 1, 256, 1, 1, 0, None, None)
    assert library.seen["values"] == [2**40 + 5, 2**33, -3, 12.0, 219 / 255]


def test_a_cuda_error_is_a_plain_message_and_out_of_memory_is_its_own_kind():
    driver = real_driver_with(RecordingLibrary())
    driver._check(0, "doing nothing")
    with pytest.raises(GpuOutOfMemory):
        driver._check(2, "allocating GPU memory")
    with pytest.raises(GpuUnavailable, match="error 700 while starting a GPU kernel"):
        driver._check(700, "starting a GPU kernel")


# ---- the real kernels (pytest -m gpu) ----------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def gpu():
    if not cuda_compare.available():
        pytest.skip("no NVIDIA driver library, or the kernels are switched off")
    session = Session(cap_bytes=512 * 1024**2)
    try:
        session.__enter__()
    except GpuUnavailable as exc:
        pytest.skip(f"no usable GPU: {exc}")
    yield session
    session.close()


def assert_agree(got, want, *, exact):
    """Exactly equal for limited-range sources (every sum is exact), within 1e-9 relative otherwise."""
    if exact:
        assert np.array_equal(got, want), np.abs(got - want).max(axis=0)
    else:
        np.testing.assert_allclose(got, want, rtol=1e-9, atol=1e-12)


@real_gpu
def test_the_gpu_selftest_passes(gpu):
    gpu.selftest()
    assert gpu.peak_held <= gpu.budget


@real_gpu
@pytest.mark.parametrize("full", [False, True])
def test_gpu_pictures_are_the_cpus_byte_for_byte(gpu, full):
    left, right, _pairs = cuda_compare._synthetic_frames()
    rng = np.random.default_rng(1)
    luma, blue, red = (rng.integers(0, 65536, shape).astype(np.uint16) for shape in ((75, 133), (38, 67), (38, 67)))
    wild = canonical.Cells(luma, blue, red, left[0].region)  # every 16-bit value, the extremes too
    frames = left + right + [wild]
    for picture, cells in zip(gpu.pictures(frames, full=full), frames, strict=True):
        for a, b in zip(picture, cpu_picture(cells, full), strict=True):
            assert a.dtype == np.float32 and a.shape == b.shape and a.tobytes() == b.tobytes()


@real_gpu
@pytest.mark.parametrize("left_full, right_full", FLAGS)
def test_gpu_metrics_equal_the_cpus(gpu, left_full, right_full):
    left, right, pairs = cuda_compare._synthetic_frames()
    got = run(gpu, left, right, pairs, left_full, right_full)
    assert_agree(got, cpu_metrics(left, right, pairs, left_full, right_full), exact=not (left_full or right_full))


@real_gpu
@pytest.mark.parametrize("left_full, right_full", FLAGS)
def test_gpu_metrics_of_many_pairs_equal_the_cpus(gpu, left_full, right_full):
    left, right = bulk(24, seed=31)
    pairs = np.random.default_rng(2).integers(0, 24, (60, 2))
    got = run(gpu, left, right, pairs, left_full, right_full)
    assert_agree(got, cpu_metrics(left, right, pairs, left_full, right_full), exact=not (left_full or right_full))


@real_gpu
def test_gpu_structured_cases(gpu):
    rng = np.random.default_rng(11)
    base = make_cells(rng, 75, 133, 16)
    copy = canonical.Cells(base.luma.copy(), base.blue.copy(), base.red.copy(), base.region)
    limit = int(recurrence.VIDEO["peak"])
    low, high = copy.luma.copy(), base.luma.copy()
    low[30, 40], high[30, 40] = 100 * 256, (100 + limit) * 256  # one cell a whole peak limit apart
    cases = [
        (base, copy),  # the same picture
        (
            canonical.Cells(low, base.blue, base.red, base.region),
            canonical.Cells(high, base.blue, base.red, base.region),
        ),
    ]
    got = run(gpu, [a for a, _b in cases], [b for _a, b in cases], [[0, 0], [1, 1]])
    assert (got[0] == 0).all()  # identical pictures
    assert got[1, 0] == limit  # the peak lands exactly on its limit, and is not rounded away from it
    assert_agree(
        got, cpu_metrics([a for a, _b in cases], [b for _a, b in cases], [[0, 0], [1, 1]], False, False), exact=True
    )


@real_gpu
@pytest.mark.parametrize("rows, columns", [(270, 480), (101, 207), (68, 68)])
def test_gpu_metrics_at_real_odd_and_smallest_sizes(gpu, rows, columns):
    """270 x 480 is the real grid (luma 266 x 476, colour 133 x 238); 101 x 207 has odd planes; 68 x 68 leaves colour
    planes of exactly the 32 x 32 the kernels need."""
    left, right = bulk(4, seed=rows, rows=rows, columns=columns)
    pairs = [[0, 0], [1, 1], [2, 2], [3, 3], [0, 3], [2, 1]]
    assert_agree(run(gpu, left, right, pairs), cpu_metrics(left, right, pairs, False, False), exact=True)
    full = run(gpu, left, right, pairs, True, True)
    assert_agree(full, cpu_metrics(left, right, pairs, True, True), exact=False)


@real_gpu
def test_gpu_chunks_that_do_not_fit_one_launch(gpu, monkeypatch):
    """A budget of a few MiB holds one pair of 270 x 480 pictures (about 2 MB) at a time: the numbers are the same."""
    monkeypatch.setattr(cuda_compare, "MIN_BUDGET_BYTES", 1)
    left, right = bulk(4, seed=5, rows=270, columns=480)
    pairs = [[0, 0], [1, 1], [2, 2], [3, 3], [0, 3], [3, 0]]
    with Session(cap_bytes=6 * 1024**2, margin_bytes=0) as session:
        got = run(session, left, right, pairs)
        assert session.peak_held <= session.budget
    assert_agree(got, cpu_metrics(left, right, pairs, False, False), exact=True)


@real_gpu
def test_gpu_a_real_out_of_memory_is_a_gpu_out_of_memory(gpu):
    """The driver's own out-of-memory error is what makes a session halve its chunk (nothing is taken here: no GPU has
    64 TiB)."""
    driver = cuda_compare.CudaDriver()
    context = driver.create_context(0)
    try:
        driver.set_current(context)
        with pytest.raises(GpuOutOfMemory):
            driver.allocate(1 << 46)
    finally:
        driver.set_current(None)
        driver.destroy_context(context)


@real_gpu
def test_gpu_threads_share_a_session(gpu):
    """Threads share one session, and each leaves its own CUDA context (none) as it found it."""
    left, right = bulk(8, seed=17, rows=270, columns=480)
    pairs = [[k, (k + 3) % 8] for k in range(8)]
    want = cpu_metrics(left, right, pairs, False, False)
    driver = cuda_compare.CudaDriver()
    results, errors = [], []

    def work():
        try:
            for _ in range(3):
                results.append(run(session, left, right, pairs))
                assert driver.current_context() is None
        except BaseException as exc:
            errors.append(exc)

    with Session(cap_bytes=256 * 1024**2, driver=driver) as session:
        threads = [threading.Thread(target=work) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(120)
    assert not errors, errors
    assert len(results) == 12
    for got in results:
        assert_agree(got, want, exact=True)
