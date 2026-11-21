"""Batched comparison of canonical pictures on the GPU: many frame pairs per kernel launch, nothing decided here.

A ``Session`` takes frames the GPU already reduced to their grid (``canonical.Cells``) and returns, for each pair of
frames, the seven numbers of ``recurrence.picture_metrics`` (worst value over the three planes). It applies no limit and
makes no verdict: the caller compares the numbers with ``recurrence.VIDEO`` exactly as it does for the CPU's.

Two kernels do the work (``cuda_kernels/compare.cu``, shipped as PTX): ``cells_to_picture`` turns the cells of every
distinct frame into its float32 canonical picture, byte for byte what ``canonical.picture`` gives, and
``pair_metrics`` computes the metrics of every pair of those pictures, one thread block per pair and plane. Sums are
float64 in an order fixed by the picture's shape, so the numbers equal the CPU's exactly for limited-range sources
(every sum is exact there) and agree to about 1e-9 relative for the rest.

The runtime is the CUDA driver API through ``ctypes`` (``libcuda.so.1``, which the NVIDIA container runtime provides, as
for FFmpeg's decoder): no CUDA toolkit, no GPU Python package. The GPU is shared, so a session is a polite guest: it
holds one context, accounts every byte it allocates against a budget taken from the memory that is free when it opens,
works through the pairs in chunks that fit, and gives everything back when it closes.

Anything the GPU cannot answer raises ``GpuUnavailable`` (and never leaves memory or a context behind): the caller then
computes on the CPU. ``REPRISE_CUDA_COMPARE=0`` switches the GPU off.
"""

from __future__ import annotations

import contextlib
import ctypes
import functools
import os
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace, TracebackType
from typing import Any, NamedTuple, Protocol

import numpy as np
from loguru import logger

from . import canonical, recurrence

METRICS = ("peak", "local4", "local8", "concentration8", "signed8", "signed16", "signed32")

KILL_SWITCH = "REPRISE_CUDA_COMPARE"
PTX = Path(__file__).with_name("cuda_kernels") / "compare.ptx"
MIN_SIDE = 32  # every plane needs a 32 x 32 window (signed32); smaller pictures are the CPU's
MIN_BUDGET_BYTES = 64 * 1024**2  # a session with less than this to hold does not open
MAX_CHUNK_PAIRS = 1024  # more pairs than this per launch buys nothing and only lengthens one kernel
_BLOCK = 256  # threads per block of pair_metrics
_SCHED_BLOCKING_SYNC = 0x4  # CU_CTX_SCHED_BLOCKING_SYNC: a waiting thread sleeps instead of spinning
_OUT_OF_MEMORY = 2  # CUDA_ERROR_OUT_OF_MEMORY
_PAIR_BYTES = 2 * 4 + 3 * len(METRICS) * 8  # a pair's row of the pair table and its three rows of results
# Kernel signatures: p device pointer, q 64-bit unsigned, i 32-bit integer, f float, d double (as in compare.cu).
_CELLS_TO_PICTURE = ("cells_to_picture", "ppqiiidd")
_PAIR_METRICS = ("pair_metrics", "pppiiiiifp")


class GpuUnavailable(Exception):
    """The GPU cannot serve this request (no driver or device, too little free memory, a picture it cannot take, a CUDA
    error). The message is plain English; the caller computes on the CPU. Never raised with memory or a context left
    behind."""


class GpuMismatch(GpuUnavailable):
    """``Session.selftest`` found a kernel disagreeing with the CPU reference: the GPU must not be trusted for the
    rest of the process (every other failure is a plain ``GpuUnavailable``)."""


class GpuOutOfMemory(Exception):
    """A device allocation failed: the session's own budget or the driver's CUDA_ERROR_OUT_OF_MEMORY. The session
    answers by halving its chunk; it only reaches a caller as ``GpuUnavailable``."""


class Driver(Protocol):
    """The CUDA driver operations a ``Session`` needs (``CudaDriver`` is the real one; tests pass a fake). Contexts and
    modules are opaque integer handles, device memory is addressed by integer pointers, a function by anything."""

    def create_context(self, device: int) -> int:
        """A new context on ``device`` with blocking-sync scheduling, current on no thread."""

    def destroy_context(self, context: int) -> None: ...

    def current_context(self) -> int | None:
        """The calling thread's current context."""

    def set_current(self, context: int | None) -> None: ...

    def memory_info(self) -> tuple[int, int]:
        """(free, total) bytes of the current context's device."""

    def allocate(self, nbytes: int) -> int:
        """A device pointer to ``nbytes`` bytes; ``GpuOutOfMemory`` when there is no room."""

    def free(self, pointer: int) -> None: ...

    def upload(self, pointer: int, data: np.ndarray) -> None:
        """Copy a C-contiguous array to the device at ``pointer`` (which may lie inside an allocation)."""

    def download(self, pointer: int, out: np.ndarray) -> None:
        """Fill a C-contiguous array from the device at ``pointer``; waits for the work queued before it."""

    def load_module(self, image: bytes) -> int: ...

    def unload_module(self, module: int) -> None: ...

    def function(self, module: int, name: str, signature: str) -> object:
        """The kernel ``name`` of ``module``, taking arguments of the kinds ``signature`` lists."""

    def launch(self, function: object, grid: int, block: int, args: Sequence[int | float]) -> None:
        """Queue ``function`` on ``grid`` blocks of ``block`` threads; errors may surface at the next ``download``."""


# ---- the real driver ------------------------------------------------------------------------------------------------


@functools.cache
def _libcuda() -> ctypes.CDLL | None:
    try:
        return ctypes.CDLL("libcuda.so.1")
    except OSError:
        return None


def available() -> bool:
    """Whether a session can be tried: the driver library loads, the kernels are shipped, and the kill switch is off.
    Cheap (no context, no allocation); a session can still find no usable device or too little free memory."""
    return os.environ.get(KILL_SWITCH) != "0" and PTX.is_file() and _libcuda() is not None


_ARGUMENT: dict[str, Callable[[Any], Any]] = {
    "p": ctypes.c_uint64,
    "q": ctypes.c_uint64,
    "i": ctypes.c_int32,
    "f": ctypes.c_float,
    "d": ctypes.c_double,
}


class _Kernel:
    __slots__ = ("handle", "signature")

    def __init__(self, handle: ctypes.c_void_p, signature: str) -> None:
        self.handle = handle
        self.signature = signature


class CudaDriver:
    """The CUDA driver API (``libcuda.so.1``) through ctypes. Work goes to the default stream and copies wait for it, so
    nothing here needs an explicit synchronisation."""

    def __init__(self) -> None:
        lib = _libcuda()
        if lib is None:
            raise GpuUnavailable("The NVIDIA driver library (libcuda.so.1) is not available")
        self._lib = lib
        self._check(lib.cuInit(0), "starting the CUDA driver")

    def _describe(self, code: int) -> str:
        name, text = ctypes.c_char_p(), ctypes.c_char_p()
        if self._lib.cuGetErrorName(code, ctypes.byref(name)) or not name.value:
            return f"error {code}"
        if self._lib.cuGetErrorString(code, ctypes.byref(text)) or not text.value:
            return name.value.decode()
        return f"{name.value.decode()} ({text.value.decode()})"

    def _check(self, code: int, doing: str) -> None:
        if code == 0:
            return
        if code == _OUT_OF_MEMORY:
            raise GpuOutOfMemory(f"The GPU ran out of memory while {doing}")
        raise GpuUnavailable(f"The GPU reported {self._describe(code)} while {doing}")

    def create_context(self, device: int) -> int:
        lib = self._lib
        count = ctypes.c_int()
        self._check(lib.cuDeviceGetCount(ctypes.byref(count)), "counting the GPUs")
        if not 0 <= device < count.value:
            raise GpuUnavailable(f"There is no NVIDIA GPU number {device} (the driver sees {count.value})")
        handle = ctypes.c_int()
        self._check(lib.cuDeviceGet(ctypes.byref(handle), device), "selecting the GPU")
        context = ctypes.c_void_p()
        self._check(lib.cuCtxCreate_v2(ctypes.byref(context), _SCHED_BLOCKING_SYNC, handle), "creating a GPU context")
        try:  # leave the creating thread as it was: every public call of a session binds the context itself
            self._check(lib.cuCtxPopCurrent_v2(ctypes.byref(ctypes.c_void_p())), "releasing the new GPU context")
        except BaseException:
            lib.cuCtxDestroy_v2(context)
            raise
        return int(context.value or 0)

    def destroy_context(self, context: int) -> None:
        self._check(self._lib.cuCtxDestroy_v2(ctypes.c_void_p(context)), "destroying the GPU context")

    def current_context(self) -> int | None:
        context = ctypes.c_void_p()
        self._check(self._lib.cuCtxGetCurrent(ctypes.byref(context)), "reading the current GPU context")
        return context.value

    def set_current(self, context: int | None) -> None:
        self._check(self._lib.cuCtxSetCurrent(ctypes.c_void_p(context)), "binding the GPU context")

    def memory_info(self) -> tuple[int, int]:
        free, total = ctypes.c_size_t(), ctypes.c_size_t()
        self._check(self._lib.cuMemGetInfo_v2(ctypes.byref(free), ctypes.byref(total)), "reading free GPU memory")
        return free.value, total.value

    def allocate(self, nbytes: int) -> int:
        pointer = ctypes.c_uint64()
        self._check(self._lib.cuMemAlloc_v2(ctypes.byref(pointer), ctypes.c_size_t(nbytes)), "allocating GPU memory")
        return pointer.value

    def free(self, pointer: int) -> None:
        self._check(self._lib.cuMemFree_v2(ctypes.c_uint64(pointer)), "freeing GPU memory")

    def upload(self, pointer: int, data: np.ndarray) -> None:
        code = self._lib.cuMemcpyHtoD_v2(
            ctypes.c_uint64(pointer), ctypes.c_void_p(data.ctypes.data), ctypes.c_size_t(data.nbytes)
        )
        self._check(code, "copying pictures to the GPU")

    def download(self, pointer: int, out: np.ndarray) -> None:
        code = self._lib.cuMemcpyDtoH_v2(
            ctypes.c_void_p(out.ctypes.data), ctypes.c_uint64(pointer), ctypes.c_size_t(out.nbytes)
        )
        self._check(code, "copying results from the GPU")

    def load_module(self, image: bytes) -> int:
        module = ctypes.c_void_p()
        text = ctypes.create_string_buffer(image)  # the driver wants PTX text NUL-terminated
        self._check(self._lib.cuModuleLoadData(ctypes.byref(module), text), "loading the comparison kernels")
        return int(module.value or 0)

    def unload_module(self, module: int) -> None:
        self._check(self._lib.cuModuleUnload(ctypes.c_void_p(module)), "unloading the comparison kernels")

    def function(self, module: int, name: str, signature: str) -> object:
        handle = ctypes.c_void_p()
        code = self._lib.cuModuleGetFunction(ctypes.byref(handle), ctypes.c_void_p(module), name.encode())
        self._check(code, f"finding the kernel {name}")
        return _Kernel(handle, signature)

    def launch(self, function: object, grid: int, block: int, args: Sequence[int | float]) -> None:
        assert isinstance(function, _Kernel)
        values = [_ARGUMENT[kind](value) for kind, value in zip(function.signature, args, strict=True)]
        params = (ctypes.c_void_p * len(values))(*(ctypes.addressof(value) for value in values))
        code = self._lib.cuLaunchKernel(function.handle, grid, 1, 1, block, 1, 1, 0, None, params, None)
        self._check(code, "starting a GPU kernel")


# ---- frames and chunks ----------------------------------------------------------------------------------------------


class _Layout(NamedTuple):
    """How one frame's cells crop to its picture: the plane sizes without the margin, and the margins left out."""

    luma: tuple[int, int]
    colour: tuple[int, int]
    margin: int
    colour_margin: int

    @property
    def shape(self) -> tuple[int, int, int, int]:
        return (*self.luma, *self.colour)


def _layout(cells: canonical.Cells) -> _Layout:
    """The picture layout of ``cells``, as ``canonical.picture`` crops it (``GpuUnavailable`` for a layout the kernels
    do not take: not 16-bit, or the two colour planes of different sizes)."""
    region = cells.region
    margin = canonical.PICTURE["margin"] if min(region.rows, region.columns) >= canonical.PICTURE["margin_from"] else 0
    colour_margin = (margin + 1) // 2
    sizes = []
    for number, plane in enumerate((cells.luma, cells.blue, cells.red)):
        if plane.dtype != np.uint16 or plane.ndim != 2:
            raise GpuUnavailable("A frame's cells are not 16-bit grids, which is all the GPU takes")
        leave = margin if number == 0 else colour_margin
        sizes.append((plane.shape[0] - 2 * leave, plane.shape[1] - 2 * leave))
    if sizes[1] != sizes[2]:
        raise GpuUnavailable("A frame's two colour planes differ in size, which the GPU does not take")
    return _Layout(sizes[0], sizes[1], margin, colour_margin)


def _check_size(layout: _Layout) -> None:
    if min(*layout.luma, *layout.colour) < MIN_SIDE:
        raise GpuUnavailable(f"A picture plane is smaller than {MIN_SIDE} samples a side, which the GPU does not take")


def _frame_size(shape: tuple[int, int, int, int]) -> int:
    """Samples in one frame of a layout: the luma plane and the two colour planes, back to back."""
    return shape[0] * shape[1] + 2 * shape[2] * shape[3]


def _pack(cells: canonical.Cells, layout: _Layout, into: np.ndarray) -> None:
    """``cells`` cropped to the picture's planes, luma then blue then red, as one frame's samples in ``into``."""
    (luma_rows, luma_columns), (colour_rows, colour_columns) = layout.luma, layout.colour
    margin, colour_margin = layout.margin, layout.colour_margin
    rows, columns = cells.luma.shape
    luma_size = luma_rows * luma_columns
    into[:luma_size].reshape(luma_rows, luma_columns)[...] = cells.luma[
        margin : rows - margin, margin : columns - margin
    ]
    colour_size = colour_rows * colour_columns
    for start, plane in ((luma_size, cells.blue), (luma_size + colour_size, cells.red)):
        rows, columns = plane.shape
        into[start : start + colour_size].reshape(colour_rows, colour_columns)[...] = plane[
            colour_margin : rows - colour_margin, colour_margin : columns - colour_margin
        ]


def _chunk_bytes(frame_size: int, left_frames: int, right_frames: int, pairs: int) -> int:
    """The most device memory a chunk holds at once: both sides' pictures (4 bytes a sample) and, while the second is
    made, the cells it comes from (2 bytes a sample; the first side's are already gone), then the pair table and results."""
    return frame_size * (4 * (left_frames + right_frames) + 2 * max(left_frames, right_frames)) + pairs * _PAIR_BYTES


# ---- the session ----------------------------------------------------------------------------------------------------


class Session:
    """One CUDA context, its memory budget and the loaded kernels. Use as a context manager: everything is released on
    exit (also while an exception propagates, and from any thread). Threads may share one session; its calls are
    serialised. ``budget`` is the bytes it may hold at once (set when it opens)."""

    budget: int

    def __init__(
        self,
        *,
        device: int = 0,
        cap_bytes: int = 2 * 1024**3,
        margin_bytes: int = 1024**3,
        driver: Driver | None = None,
    ) -> None:
        self._device = device
        self._cap_bytes = cap_bytes
        self._margin_bytes = margin_bytes
        self._driver = driver
        self._lock = threading.RLock()
        self._context: int | None = None
        self._module: int | None = None
        self._functions: dict[str, object] = {}
        self._live: dict[int, int] = {}  # device pointer -> bytes, every allocation that has not been freed
        self._held = 0
        self.budget = 0
        self.peak_held = 0  # the most bytes held at once so far
        self._chunk_limit = {"pairs": MAX_CHUNK_PAIRS, "frames": MAX_CHUNK_PAIRS}  # lowered by an out-of-memory
        self._shrink_logged = False

    def __enter__(self) -> Session:
        with self._lock:
            if self._context is not None:
                raise RuntimeError("The GPU session is already open")
            if os.environ.get(KILL_SWITCH) == "0":
                raise GpuUnavailable(f"GPU picture comparison is switched off ({KILL_SWITCH}=0)")
            try:
                image = PTX.read_bytes()
            except OSError as exc:
                raise GpuUnavailable("The GPU comparison kernels (compare.ptx) are missing") from exc
            driver = self._driver if self._driver is not None else CudaDriver()
            context = driver.create_context(self._device)
            module = None
            try:
                with _bound(driver, context):
                    free, _total = driver.memory_info()
                    budget = min(self._cap_bytes, free - self._margin_bytes)
                    if budget < MIN_BUDGET_BYTES:
                        raise GpuUnavailable(
                            f"Not enough free GPU memory: {free // 2**20} MiB free, and the comparison needs "
                            f"{MIN_BUDGET_BYTES // 2**20} MiB beyond the {self._margin_bytes // 2**20} MiB it leaves to others"
                        )
                    try:
                        module = driver.load_module(image)
                        functions = {
                            name: driver.function(module, name, signature)
                            for name, signature in (_CELLS_TO_PICTURE, _PAIR_METRICS)
                        }
                    except GpuOutOfMemory as exc:
                        raise GpuUnavailable("Not enough free GPU memory to load the comparison kernels") from exc
            except BaseException:
                _discard(driver, context, module)
                raise
            self._driver, self._context, self._module, self._functions, self.budget = (
                driver,
                context,
                module,
                functions,
                budget,
            )
            logger.debug("GPU session open: {} MiB budget of {} MiB free", budget // 2**20, free // 2**20)
            return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Free every allocation, unload the kernels and destroy the context. Safe to repeat, from any thread, and
        while an exception propagates: a failure to free one thing does not stop the rest."""
        with self._lock:
            driver, context, module = self._driver, self._context, self._module
            if driver is None or context is None:
                return
            self._context = self._module = None
            self._functions = {}
            live, self._live, self._held = self._live, {}, 0
            try:
                with _bound(driver, context):
                    for pointer in live:
                        with contextlib.suppress(Exception):
                            driver.free(pointer)
                    if module is not None:
                        with contextlib.suppress(Exception):
                            driver.unload_module(module)
            except Exception as exc:  # the context could not even be bound: destroying it frees what it holds
                logger.debug("GPU session close: {}", exc)
            _destroy(driver, context)

    @contextlib.contextmanager
    def _use(self) -> Iterator[Driver]:
        """The lock held and this session's context current on the calling thread; the thread's own is put back."""
        with self._lock:
            driver, context = self._driver, self._context
            if driver is None or context is None:
                raise GpuUnavailable("The GPU session is not open")
            with _bound(driver, context):
                yield driver

    # -- memory --

    def _allocate(self, driver: Driver, nbytes: int) -> int:
        """The one place device memory is taken: refuses (as an out-of-memory) what would pass the budget."""
        if self._held + nbytes > self.budget:
            raise GpuOutOfMemory(f"The session's {self.budget // 2**20} MiB GPU memory budget is full")
        pointer = driver.allocate(nbytes)
        self._live[pointer] = nbytes
        self._held += nbytes
        self.peak_held = max(self.peak_held, self._held)
        return pointer

    def _release(self, driver: Driver, pointer: int) -> None:
        driver.free(pointer)
        self._held -= self._live.pop(pointer)

    @contextlib.contextmanager
    def _scope(self, driver: Driver) -> Iterator[_Scope]:
        """Allocations made through the yielded scope are freed when the block ends, whatever happens in it."""
        scope = _Scope(self, driver)
        try:
            yield scope
        finally:
            scope.close()

    # -- chunking --

    def _run_chunks(
        self, kind: str, count: int, end_of: Callable[[int, int], int], run: Callable[[int, int], None]
    ) -> None:
        """Work through ``count`` items in chunks: ``end_of(start, limit)`` is where the chunk from ``start`` ends (at most
        ``limit`` items, as many as fit the budget), ``run(start, end)`` does it. On an out-of-memory the chunk is halved
        and retried, down to one item, which is ``GpuUnavailable`` when it still does not fit."""
        start = 0
        while start < count:
            end = end_of(start, self._chunk_limit[kind])
            try:
                run(start, end)
            except GpuOutOfMemory as exc:
                if end - start == 1:
                    raise GpuUnavailable("The GPU has too little free memory to compare even a single pair") from exc
                self._shrink(kind, end - start)
                continue
            start = end

    def _shrink(self, kind: str, size: int) -> None:
        """A chunk of ``size`` items did not fit: from now on this session takes half as many at a time."""
        if not self._shrink_logged:
            self._shrink_logged = True
            logger.warning("GPU memory is tight: comparing {} {} at a time instead of {}", size // 2, kind, size)
        self._chunk_limit[kind] = size // 2

    # -- public work --

    def metrics(
        self,
        left: Sequence[canonical.Cells],
        right: Sequence[canonical.Cells],
        pairs: np.ndarray,
        *,
        left_full: bool,
        right_full: bool,
        concentration_level: float,
    ) -> np.ndarray:
        """For each pair (row ``k`` of ``pairs`` = index into ``left``, index into ``right``) the worst value over the
        three planes of the canonical pictures of the two frames, of each metric in ``METRICS`` order: a float64 array
        (P, 7) holding exactly the numbers ``recurrence.picture_metrics`` gives. ``left_full`` / ``right_full`` say the
        side's frames are full range. ``ValueError`` when two frames of a pair are not on one grid, ``GpuUnavailable``
        for anything the GPU does not take (see its message): then the CPU computes the pair."""
        pairs = np.asarray(pairs) if len(pairs) else np.zeros((0, 2), np.int64)
        if pairs.ndim != 2 or pairs.shape[1] != 2 or not np.issubdtype(pairs.dtype, np.integer):
            raise ValueError("pairs must be an integer array of shape (P, 2)")
        if len(pairs) and (pairs.min() < 0 or pairs[:, 0].max() >= len(left) or pairs[:, 1].max() >= len(right)):
            raise ValueError("A pair names a frame that does not exist")
        result = np.zeros((len(pairs), len(METRICS)), np.float64)
        with self._use() as driver:
            if not len(pairs):
                return result
            shape, layouts_left, layouts_right = self._pairs_layout(left, right, pairs)
            frame_size = _frame_size(shape)
            rows = pairs.tolist()

            def end_of(start: int, limit: int) -> int:
                seen_left: set[int] = set()
                seen_right: set[int] = set()
                end = start
                while end < len(rows) and end - start < limit:
                    i, j = rows[end]
                    left_frames = len(seen_left) + (i not in seen_left)
                    right_frames = len(seen_right) + (j not in seen_right)
                    need = _chunk_bytes(frame_size, left_frames, right_frames, end - start + 1)
                    if end > start and need > self.budget // 2:
                        break
                    seen_left.add(i)
                    seen_right.add(j)
                    end += 1
                return end

            def run(start: int, end: int) -> None:
                chunk = pairs[start:end]
                frames_left, inverse_left = np.unique(chunk[:, 0], return_inverse=True)
                frames_right, inverse_right = np.unique(chunk[:, 1], return_inverse=True)
                local = np.stack([inverse_left, inverse_right], axis=1).astype(np.int32)
                began = time.perf_counter()
                result[start:end] = self._compare(
                    driver,
                    shape,
                    [(left[i], layouts_left[i]) for i in frames_left.tolist()],
                    [(right[j], layouts_right[j]) for j in frames_right.tolist()],
                    local,
                    left_full,
                    right_full,
                    float(concentration_level),
                )
                logger.debug(
                    "GPU compared {} pairs of {} + {} frames in {:.1f} ms (peak {} MiB held)",
                    end - start,
                    len(frames_left),
                    len(frames_right),
                    (time.perf_counter() - began) * 1000,
                    self.peak_held // 2**20,
                )

            self._run_chunks("pairs", len(rows), end_of, run)
        return result

    def _pairs_layout(
        self, left: Sequence[canonical.Cells], right: Sequence[canonical.Cells], pairs: np.ndarray
    ) -> tuple[tuple[int, int, int, int], dict[int, _Layout], dict[int, _Layout]]:
        """The one grid of all the pairs and the layout of every frame they use (``ValueError`` when the two frames of a
        pair are not on one grid, ``GpuUnavailable`` when the pairs are not all on one grid or a plane is too small)."""
        layouts_left = {i: _layout(left[i]) for i in np.unique(pairs[:, 0]).tolist()}
        layouts_right = {j: _layout(right[j]) for j in np.unique(pairs[:, 1]).tolist()}
        shape = None
        for i, j in pairs.tolist():
            if layouts_left[i].shape != layouts_right[j].shape:
                raise ValueError("Canonical pictures are not on one grid")
            if shape is None:
                shape = layouts_left[i].shape
            elif layouts_left[i].shape != shape:
                raise GpuUnavailable("The pairs are not all on one picture grid, which the GPU does not take")
        assert shape is not None
        _check_size(layouts_left[int(pairs[0, 0])])
        return shape, layouts_left, layouts_right

    def _compare(
        self,
        driver: Driver,
        shape: tuple[int, int, int, int],
        left: list[tuple[canonical.Cells, _Layout]],
        right: list[tuple[canonical.Cells, _Layout]],
        pairs: np.ndarray,
        left_full: bool,
        right_full: bool,
        level: float,
    ) -> np.ndarray:
        """One chunk: both sides' pictures on the device, every pair's metrics, then the worst over the planes."""
        with self._scope(driver) as scope:
            left_pictures = self._pictures(driver, scope, shape, left, left_full)
            right_pictures = self._pictures(driver, scope, shape, right, right_full)
            table = scope.allocate(pairs.nbytes)
            driver.upload(table, pairs)
            planes = scope.allocate(len(pairs) * 3 * len(METRICS) * 8)
            driver.launch(
                self._functions[_PAIR_METRICS[0]],
                3 * len(pairs),
                _BLOCK,
                (left_pictures, right_pictures, table, len(pairs), *shape, level, planes),
            )
            values = np.empty((len(pairs), 3, len(METRICS)), np.float64)
            driver.download(planes, values)
        return values.max(axis=1)

    def _pictures(
        self,
        driver: Driver,
        scope: _Scope,
        shape: tuple[int, int, int, int],
        frames: list[tuple[canonical.Cells, _Layout]],
        full: bool,
    ) -> int:
        """The float32 pictures of ``frames`` on the device (a pointer to frame 0), made from their cells by kernel 1."""
        frame_size = _frame_size(shape)
        cells = scope.allocate(len(frames) * frame_size * 2)
        staging = np.empty(frame_size, np.uint16)
        for number, (frame, layout) in enumerate(frames):
            _pack(frame, layout, staging)
            driver.upload(cells + number * frame_size * 2, staging)
        pictures = scope.allocate(len(frames) * frame_size * 4)
        count = len(frames) * frame_size
        driver.launch(
            self._functions[_CELLS_TO_PICTURE[0]],
            min(-(-count // _BLOCK), 65535),
            _BLOCK,
            (cells, pictures, count, frame_size, shape[0] * shape[1], int(full), 219 / 255, 224 / 255),
        )
        scope.release(cells)
        return pictures

    def pictures(self, cells: Sequence[canonical.Cells], *, full: bool) -> list[canonical.Picture]:
        """The canonical pictures of ``cells`` as kernel 1 makes them, downloaded (a test and diagnostic: ``metrics`` keeps
        the pictures on the device)."""
        frames = list(cells)
        pictures: list[canonical.Picture] = []
        with self._use() as driver:
            if not frames:
                return pictures
            layouts = [_layout(frame) for frame in frames]
            shape = layouts[0].shape
            if any(layout.shape != shape for layout in layouts):
                raise GpuUnavailable("The frames are not all on one picture grid, which the GPU does not take")
            _check_size(layouts[0])
            frame_size = _frame_size(shape)

            def end_of(start: int, limit: int) -> int:
                fit = max(1, (self.budget // 2) // (frame_size * 6))  # 4 bytes of picture and 2 of cells a sample
                return min(len(frames), start + min(limit, fit))

            def run(start: int, end: int) -> None:
                with self._scope(driver) as scope:
                    device = self._pictures(
                        driver, scope, shape, list(zip(frames[start:end], layouts[start:end], strict=True)), full
                    )
                    values = np.empty((end - start, frame_size), np.float32)
                    driver.download(device, values)
                luma_size, colour_size = shape[0] * shape[1], shape[2] * shape[3]
                for row in values:
                    pictures.append(
                        canonical.Picture(
                            row[:luma_size].reshape(shape[0], shape[1]),
                            row[luma_size : luma_size + colour_size].reshape(shape[2], shape[3]),
                            row[luma_size + colour_size :].reshape(shape[2], shape[3]),
                        )
                    )

            self._run_chunks("frames", len(frames), end_of, run)
        return pictures

    def selftest(self) -> None:
        """Both kernels against the CPU reference on a small deterministic set of frames (8-bit-like and 10-bit-like
        cells, every metric made non-zero, both range flags on each side): ``GpuMismatch`` naming the first disagreement,
        a plain ``GpuUnavailable`` when the GPU could not run it. Pictures must be byte-equal; metrics exact for limited
        range and within 1e-9 relative otherwise."""
        left, right, pairs = _synthetic_frames()
        level = recurrence.VIDEO["concentration_level"]
        for full in (False, True):
            for side, frames in (("left", left), ("right", right)):
                for number, (got, want) in enumerate(
                    zip(self.pictures(frames, full=full), (_cpu_picture(cells, full) for cells in frames), strict=True)
                ):
                    for plane, (a, b) in zip(("luma", "blue", "red"), zip(got, want, strict=True), strict=True):
                        if a.shape != b.shape or a.tobytes() != b.tobytes():
                            raise GpuMismatch(
                                f"GPU self-test: the {plane} picture of {side} frame {number} "
                                f"({'full' if full else 'limited'} range) differs from the CPU's"
                            )
        for left_full, right_full in ((False, False), (True, False), (False, True), (True, True)):
            pictures_left = [_cpu_picture(cells, left_full) for cells in left]
            pictures_right = [_cpu_picture(cells, right_full) for cells in right]
            expected = np.array(
                [
                    [recurrence.picture_metrics(pictures_left[i], pictures_right[j])[name] for name in METRICS]
                    for i, j in pairs.tolist()
                ]
            )
            actual = self.metrics(
                left, right, pairs, left_full=left_full, right_full=right_full, concentration_level=level
            )
            exact = not (left_full or right_full)
            close = np.asarray(
                actual == expected if exact else np.abs(actual - expected) <= 1e-9 * np.maximum(1.0, np.abs(expected))
            )
            if not close.all():
                k, m = (int(v) for v in np.argwhere(~close)[0])
                raise GpuMismatch(
                    f"GPU self-test: {METRICS[m]} of pair {k} ({'full' if left_full else 'limited'}/"
                    f"{'full' if right_full else 'limited'} range) is {actual[k, m]!r} on the GPU and "
                    f"{expected[k, m]!r} on the CPU"
                )


class _Scope:
    """The device allocations of one block of work, freed together when it ends."""

    def __init__(self, session: Session, driver: Driver) -> None:
        self._session = session
        self._driver = driver
        self._pointers: list[int] = []

    def allocate(self, nbytes: int) -> int:
        pointer = self._session._allocate(self._driver, nbytes)
        self._pointers.append(pointer)
        return pointer

    def release(self, pointer: int) -> None:
        self._session._release(self._driver, pointer)
        self._pointers.remove(pointer)

    def close(self) -> None:
        """Free what is left, newest first; a failure to free one is logged and leaves it to ``Session.close``."""
        while self._pointers:
            pointer = self._pointers[-1]
            try:
                self._session._release(self._driver, pointer)
            except Exception as exc:
                logger.debug("GPU free failed: {}", exc)
            self._pointers.pop()


@contextlib.contextmanager
def _bound(driver: Driver, context: int) -> Iterator[None]:
    """``context`` current on the calling thread, the thread's own current context put back afterwards."""
    previous = driver.current_context()
    driver.set_current(context)
    try:
        yield
    finally:
        driver.set_current(None if previous == context else previous)


def _destroy(driver: Driver, context: int) -> None:
    """Destroy a context that is not bound, whatever state it is in; the calling thread's own context is kept."""
    previous: int | None = None
    with contextlib.suppress(Exception):
        previous = driver.current_context()
    try:
        driver.destroy_context(context)
    except Exception as exc:
        logger.debug("GPU context destroy failed: {}", exc)
    finally:
        with contextlib.suppress(Exception):
            driver.set_current(None if previous == context else previous)


def _discard(driver: Driver, context: int, module: int | None) -> None:
    """Undo a half-opened session: the module (if loaded) and the context."""
    if module is not None:
        with contextlib.suppress(Exception), _bound(driver, context):
            driver.unload_module(module)
    _destroy(driver, context)


# ---- the self-test's frames and CPU reference -----------------------------------------------------------------------


def _cpu_picture(cells: canonical.Cells, full: bool) -> canonical.Picture:
    """The CPU's canonical picture of ``cells`` (the reference the kernels are held to)."""
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


def _synthetic_frames() -> tuple[list[canonical.Cells], list[canonical.Cells], np.ndarray]:
    """Six frames of cells and six versions of them that differ in a way each of the metrics sees (a hot cell, a block,
    a wide offset, a block where most cells differ, a little noise, none), with pairs that mix them. Odd sizes, so the
    last tile and the far-edge window of every plane are partial / unaligned; sample values are 8-bit-like (multiples of
    16) in the first three frames and 10-bit-like (multiples of 4) in the rest."""
    rng = np.random.default_rng(20260721)
    rows, columns = 75, 133
    region = canonical.Region(0, 0, 4 * columns, 4 * rows, rows, columns)
    shapes = ((rows, columns), ((rows + 1) // 2, (columns + 1) // 2), ((rows + 1) // 2, (columns + 1) // 2))

    def frame(step: int) -> list[np.ndarray]:
        planes = []
        for (height, width), base in zip(shapes, (140, 128, 110), strict=True):
            y, x = np.mgrid[0:height, 0:width]
            values = base + 60 * np.sin(x / 9.0) * np.cos(y / 7.0) + rng.normal(0, 6, (height, width))
            planes.append((np.clip(np.rint(values * 256 / step), 0, 65535 // step) * step).astype(np.int32))
        return planes

    left = [frame(16) for _ in range(3)] + [frame(4) for _ in range(3)]
    right = [[plane.copy() for plane in planes] for planes in left]
    right[1][0] += 16 * rng.integers(-2, 3, shapes[0])
    right[2][0][30, 40] += 40 * 256
    right[3][0][20:28, 50:58] += 20 * 256
    right[3][1][10:14, 12:16] += 12 * 256
    right[4][0] += 4 * 256
    right[4][2] += 2 * 256
    right[5][0][8:16, 8:16] += np.where(rng.random((8, 8)) < 0.9, 14 * 256, 0)

    def cells(planes: list[np.ndarray]) -> canonical.Cells:
        luma, blue, red = (np.clip(plane, 0, 65535).astype(np.uint16) for plane in planes)
        return canonical.Cells(luma, blue, red, region)

    pairs = np.array([[0, 0], [1, 1], [2, 2], [3, 3], [4, 4], [5, 5], [0, 3], [3, 0], [2, 5], [4, 1], [1, 0]])
    return [cells(planes) for planes in left], [cells(planes) for planes in right], pairs
