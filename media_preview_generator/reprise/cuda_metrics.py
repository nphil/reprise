"""GPU verdict for one plane pair of ``recurrence.video_matches``, through the NVIDIA driver API.

Comparing two 1080p frames takes a second of CPU in numpy and is most of a confirmation (a pair is compared every
frame). The same limits are checked here on the GPU: ``cuda_kernels/verdict.cu`` sums the exact integer differences per
tile or window, and a plane is accepted or rejected only when the CPU's float32 / float64 arithmetic is certain to give
the same verdict; inside the arithmetic's error band the answer is None and the caller decides on the CPU. So the
verdict never differs from ``recurrence._plane_matches``'s, only the time it takes does.

Nothing from NVIDIA ships with this: the kernels are PTX built from our own source (``tools/build_reprise_ptx.py``)
and loaded with ``libcuda``, which the driver (or the NVIDIA container runtime) provides. Without it, with an
unsupported GPU, with the GPU switched off, or after any failure, ``plane_verdict`` answers None and the CPU decides
silently. The first use runs a self-test of the kernels against the CPU on synthetic planes (boundary cases at both bit
depths); a disagreement turns the GPU off for the process.

The device is chosen by ``media.native_frames`` (``use``): the first NVIDIA GPU the owner allows for decoding.
``REPRISE_CUDA_VERDICT=0`` turns this off.
"""

from __future__ import annotations

import ctypes
import os
import threading
from pathlib import Path

import numpy as np
from loguru import logger

from .verification_profile import PROFILE_SPEC

VIDEO = PROFILE_SPEC["video"]
KERNELS = Path(__file__).with_name("cuda_kernels")
BLOCK = 128
MIN_SIDE = 32  # smaller planes take the CPU's whole-plane mean for the signed metrics
MAX_FAILURES = 3

_lock = threading.Lock()
_device: int | None = None  # what ``use`` selected
_driver = None  # the loaded _Driver for ``_driver_device``
_driver_device: int | None = None
_off = False  # this device failed its self-test or too many launches: the CPU decides
_failures = 0


class _CudaError(RuntimeError):
    pass


def use(device: int | None) -> None:
    """Select the CUDA device index the verdicts may use (None: CPU only). Cheap; the GPU is touched on first use."""
    global _device, _off, _failures
    if device != _device:
        with _lock:
            _device, _off, _failures = device, False, 0


def reset() -> None:
    """Forget everything (tests)."""
    global _device, _driver, _driver_device, _off, _failures
    with _lock:
        _device, _driver, _driver_device, _off, _failures = None, None, None, False, 0


def _first_raw_over(limit, bit_depth):
    """The smallest |L - R| whose CPU value float32(|raw|) * float32(255 / (2**depth - 1)) exceeds ``limit``, computed
    with the CPU's own arithmetic (the scaling is monotone, so every larger difference exceeds it too)."""
    raw = np.arange(1 << bit_depth, dtype=np.float32)
    over = np.flatnonzero(raw * (255 / ((1 << bit_depth) - 1)) > np.float32(limit))
    return int(over[0]) if len(over) else 1 << bit_depth


class _Driver:
    """The loaded driver library, the primary context of one device, the kernels and a pool of device buffers."""

    def __init__(self, device):
        lib = ctypes.CDLL("libcuda.so.1")
        self.lib = lib
        self.device = device
        for name, argtypes in {
            "cuInit": [ctypes.c_uint],
            "cuDeviceGetCount": [ctypes.POINTER(ctypes.c_int)],
            "cuDeviceGet": [ctypes.POINTER(ctypes.c_int), ctypes.c_int],
            "cuDevicePrimaryCtxRetain": [ctypes.POINTER(ctypes.c_void_p), ctypes.c_int],
            "cuCtxSetCurrent": [ctypes.c_void_p],
            "cuModuleLoadData": [ctypes.POINTER(ctypes.c_void_p), ctypes.c_char_p],
            "cuModuleGetFunction": [ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.c_char_p],
            "cuMemAlloc_v2": [ctypes.POINTER(ctypes.c_uint64), ctypes.c_size_t],
            "cuMemFree_v2": [ctypes.c_uint64],
            "cuMemcpyHtoD_v2": [ctypes.c_uint64, ctypes.c_void_p, ctypes.c_size_t],
            "cuMemcpyDtoH_v2": [ctypes.c_void_p, ctypes.c_uint64, ctypes.c_size_t],
            "cuMemsetD32_v2": [ctypes.c_uint64, ctypes.c_uint, ctypes.c_size_t],
            "cuLaunchKernel": [
                ctypes.c_void_p,
                *([ctypes.c_uint] * 6),
                ctypes.c_uint,
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_void_p),
                ctypes.c_void_p,
            ],
        }.items():
            function = getattr(lib, name)
            function.argtypes = argtypes
            function.restype = ctypes.c_int
        self._call("cuInit", 0)
        count = ctypes.c_int()
        self._call("cuDeviceGetCount", ctypes.byref(count))
        if not 0 <= device < count.value:
            raise _CudaError(f"no CUDA device {device}")
        handle = ctypes.c_int()
        self._call("cuDeviceGet", ctypes.byref(handle), device)
        self.context = ctypes.c_void_p()
        self._call("cuDevicePrimaryCtxRetain", ctypes.byref(self.context), handle.value)
        self.bind()
        self.kernels = {}
        for sample_bytes, name in ((1, "verdict_u8.ptx"), (2, "verdict_u16.ptx")):
            module = ctypes.c_void_p()
            self._call("cuModuleLoadData", ctypes.byref(module), (KERNELS / name).read_bytes() + b"\0")
            functions = {}
            for kernel in ("peak", "tiles", "signed_windows"):
                function = ctypes.c_void_p()
                self._call("cuModuleGetFunction", ctypes.byref(function), module, kernel.encode())
                functions[kernel] = function
            self.kernels[sample_bytes] = functions
        self._pool: list[list] = []  # idle [left, right, result, capacity] buffer sets
        self._pool_lock = threading.Lock()

    def _call(self, name, *args):
        code = getattr(self.lib, name)(*args)
        if code:
            raise _CudaError(f"{name} failed with CUDA error {code}")

    def bind(self):
        """Make the device's context current on the calling thread (a context belongs to one thread at a time)."""
        self._call("cuCtxSetCurrent", self.context)

    def _buffers(self, size):
        with self._pool_lock:
            if self._pool:
                buffers = self._pool.pop()
            else:
                buffers = [ctypes.c_uint64(), ctypes.c_uint64(), ctypes.c_uint64(), 0]
                self._call("cuMemAlloc_v2", ctypes.byref(buffers[2]), 8)
        if buffers[3] < size:
            for slot in (0, 1):
                if buffers[3]:
                    self._call("cuMemFree_v2", buffers[slot])
                self._call("cuMemAlloc_v2", ctypes.byref(buffers[slot]), size)
            buffers[3] = size
        return buffers

    def counts(self, left, right, bit_depth):
        """(certain violations, metrics inside a band) over one plane pair."""
        height, width = left.shape
        size = left.nbytes
        self.bind()
        left, right = np.ascontiguousarray(left), np.ascontiguousarray(right)
        buffers = self._buffers(size)
        try:
            a, b, result = (buffers[0].value, buffers[1].value, buffers[2].value)
            self._call("cuMemcpyHtoD_v2", a, left.ctypes.data, size)
            self._call("cuMemcpyHtoD_v2", b, right.ctypes.data, size)
            self._call("cuMemsetD32_v2", result, 0, 2)
            scale = 255 / ((1 << bit_depth) - 1)
            functions = self.kernels[left.itemsize]
            p, i, d = ctypes.c_uint64, ctypes.c_int, ctypes.c_double
            self._launch(
                functions["peak"],
                512,
                256,
                (p(a), p(b), i(height * width), i(_first_raw_over(VIDEO["peak"], bit_depth)), p(result)),
            )
            level = _first_raw_over(VIDEO["concentration_level"], bit_depth)
            for tile, key in ((4, "local4"), (8, "local8")):
                for oy in (0, tile // 2):
                    for ox in (0, tile // 2):
                        rows = -(-height // tile) if not oy else 1 + -(-(height - oy) // tile)
                        columns = -(-width // tile) if not ox else 1 + -(-(width - ox) // tile)
                        self._launch(
                            functions["tiles"],
                            -(-rows * columns // BLOCK),
                            BLOCK,
                            (
                                p(a),
                                p(b),
                                i(height),
                                i(width),
                                i(tile),
                                i(oy),
                                i(ox),
                                d(scale),
                                d(VIDEO[key]),
                                i(level),
                                i(int(tile == 8)),
                                d(VIDEO["concentration8"]),
                                p(result),
                            ),
                        )
            for window in (8, 16, 32):
                stride = window // 2
                rows = (height - window) // stride + 1 + ((height - window) % stride != 0)
                columns = (width - window) // stride + 1 + ((width - window) % stride != 0)
                self._launch(
                    functions["signed_windows"],
                    -(-rows * columns // BLOCK),
                    BLOCK,
                    (p(a), p(b), i(height), i(width), i(window), d(scale), d(VIDEO["signed" + str(window)]), p(result)),
                )
            out = (ctypes.c_uint32 * 2)()
            self._call("cuMemcpyDtoH_v2", ctypes.byref(out), result, 8)  # waits for the launches above
        finally:
            with self._pool_lock:
                self._pool.append(buffers)
        return int(out[0]), int(out[1])

    def _launch(self, function, blocks, threads, arguments):
        pointers = (ctypes.c_void_p * len(arguments))(*(ctypes.addressof(x) for x in arguments))
        self._call("cuLaunchKernel", function, blocks, 1, 1, threads, 1, 1, 0, None, pointers, None)


def _selftest(driver):
    """The kernels against the CPU on synthetic planes at both bit depths: whatever the GPU answers outright must be the
    CPU's answer, and clear passes and clear failures must be answered outright."""
    from . import recurrence

    rng = np.random.default_rng(7)
    for bit_depth in (8, 10):
        top = (1 << bit_depth) - 1
        dtype = np.uint8 if bit_depth == 8 else np.uint16
        base = rng.integers(top // 4, top - top // 4, size=(72, 104))
        cases = []
        for kind in range(7):
            other = base.copy()
            if kind == 1:  # one sample far apart: fails the peak
                other[10, 10] += top // 2
            elif kind == 2:  # a block that fails the local mean limits
                other[8:16, 8:16] += top // 8
            elif kind == 3:  # a wide offset: fails the signed windows
                other += top // 20 + 1
            elif kind == 4:  # many samples over the concentration level in one tile
                other[8:16, 8:16] += np.where(rng.random((8, 8)) < 0.9, top // 12, 0)
            elif kind == 5:  # tiny noise: passes
                other += rng.integers(-2, 3, size=other.shape)
            cases.append((kind, base.astype(dtype), np.clip(other, 0, top).astype(dtype)))
        for kind, left, right in cases:
            truth = recurrence._plane_matches(left, right, bit_depth)
            fail, ambiguous = driver.counts(left, right, bit_depth)
            answer = False if fail else (None if ambiguous else True)
            if (answer is not None and answer != truth) or (answer is None and kind in (0, 1, 3, 5)):
                raise _CudaError(f"self-test: kernel said {answer}, CPU said {truth} (case {kind}, {bit_depth}-bit)")


def _usable():
    global _driver, _driver_device, _off
    device = _device
    if device is None or _off or os.environ.get("REPRISE_CUDA_VERDICT") == "0":
        return None
    driver = _driver
    if driver is not None and _driver_device == device:
        return driver
    with _lock:
        if _off or _device != device:
            return None
        if _driver is not None and _driver_device == device:  # another thread loaded it while this one waited
            return _driver
        try:
            driver = _Driver(device)
            _selftest(driver)
        except (OSError, _CudaError, AttributeError, ValueError) as exc:
            _off = True
            logger.info("Reprise compares frames on the CPU: the GPU verdict is not usable ({})", exc)
            return None
        _driver, _driver_device = driver, device
        logger.info("Reprise compares frame pairs on CUDA device {} (kernels self-tested against the CPU)", device)
        return driver


def plane_verdict(left, right, bit_depth):
    """True or False when the GPU is certain what ``recurrence._plane_matches`` would answer for this plane pair, None
    when the CPU has to decide (no usable GPU, a plane the kernels do not take, or a metric inside its error band)."""
    global _off, _failures
    driver = _usable()
    if driver is None or left.ndim != 2 or min(left.shape) < MIN_SIDE or left.dtype.itemsize != right.dtype.itemsize:
        return None
    if left.dtype.itemsize != (1 if bit_depth == 8 else 2):
        return None
    try:
        fail, ambiguous = driver.counts(left, right, bit_depth)
    except (OSError, _CudaError) as exc:
        with _lock:
            _failures += 1
            if _failures >= MAX_FAILURES and not _off:
                _off = True
                logger.warning("Reprise stopped using the GPU for frame comparison after repeated errors: {}", exc)
        return None
    _failures = 0
    if fail:
        return False
    return None if ambiguous else True
