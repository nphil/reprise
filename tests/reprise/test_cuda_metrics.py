"""The GPU frame-pair verdict (reprise/cuda_metrics.py, cuda_kernels/verdict.cu) and the exact CPU speed-ups beside it.

Three layers: the kernel's arithmetic modelled in numpy (always runs: proves the error bands and the integer
thresholds on boundary cases at both bit depths), the real kernels (only where a CUDA device exists), and the host
logic around them (device choice, fallback, switching off). Every layer is held to the same rule: a verdict the GPU
path gives outright is the verdict the original CPU code gives.
"""

import hashlib
import os
from dataclasses import replace
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest

from media_preview_generator.reprise import cuda_metrics, media, recurrence
from media_preview_generator.reprise.verification_profile import PROFILE_SPEC

VIDEO = PROFILE_SPEC["video"]
KERNELS = Path(recurrence.__file__).with_name("cuda_kernels")
BIT_DEPTHS = [8, 10]


@pytest.fixture(autouse=True)
def no_leftover_device():
    cuda_metrics.reset()
    yield
    cuda_metrics.reset()


def frame(left_planes, bit_depth):
    """A native frame of bare planes (the comparison only looks at planes, depth and declarations)."""
    sample = 1 if bit_depth == 8 else 2
    planes = tuple(
        media.NativePlane(
            memoryview(np.ascontiguousarray(p).tobytes()), p.shape[1], p.shape[0], p.shape[1] * sample, sample
        )
        for p in left_planes
    )
    return media.NativeFrame(
        Fraction(0),
        planes,
        planes[0].width,
        planes[0].height,
        "yuv420p" if bit_depth == 8 else "yuv420p10le",
        bit_depth,
        "unknown",
        "unknown",
        "unknown",
        "unknown",
        "left",
        sample_aspect_ratio=Fraction(1),
        interpretation_flags=(),
    )


def boundary_case(rng, bit_depth):
    """One plane pair whose difference sits at a limit of one metric: a spike at the peak, a block at a local mean, a
    tile at the concentration fraction, a window at a signed mean (edge windows and half-tile phases included)."""
    top = (1 << bit_depth) - 1
    height, width = int(rng.integers(33, 120)), int(rng.integers(33, 150))
    diff = np.zeros((height, width), dtype=np.int64)
    raw = lambda limit: limit * top / 255  # noqa: E731 - a limit in this bit depth's sample units
    kind = int(rng.integers(0, 6))
    sign = 1 if rng.random() < 0.5 else -1
    if kind == 0:
        diff[rng.integers(0, height), rng.integers(0, width)] = sign * max(
            int(round(raw(VIDEO["peak"]))) + int(rng.integers(-2, 3)), 0
        )
    elif kind in (1, 2):
        size, key = (4, "local4") if kind == 1 else (8, "local8")
        value = int(round(raw(VIDEO[key]))) + int(rng.integers(-2, 3))
        phase = size // 2 if rng.random() < 0.5 else 0
        y0 = int(rng.integers(0, max(1, (height - size) // size))) * size + phase
        x0 = int(rng.integers(0, max(1, (width - size) // size))) * size + phase
        diff[y0 : y0 + size, x0 : x0 + size] = sign * value
    elif kind == 3:
        level = cuda_metrics._first_raw_over(VIDEO["concentration_level"], bit_depth)
        count = round(VIDEO["concentration8"] * 64) + int(rng.integers(-1, 3))
        y0, x0 = 4 * int(rng.integers(0, (height - 8) // 4 + 1)), 4 * int(rng.integers(0, (width - 8) // 4 + 1))
        for cell in rng.permutation(64)[:count]:
            diff[y0 + cell // 8, x0 + cell % 8] = sign * (level + int(rng.integers(0, 2)) - (rng.random() < 0.02))
    elif kind == 4:
        size = int(rng.choice([8, 16, 32]))
        target = raw(VIDEO["signed" + str(size)]) + rng.integers(-1, 2) * 0.5
        y0 = int(rng.choice([0, height - size, rng.integers(0, height - size + 1)]))
        x0 = int(rng.choice([0, width - size, rng.integers(0, width - size + 1)]))
        low = int(np.floor(target))
        block = np.full((size, size), low, dtype=np.int64)
        block.flat[rng.permutation(size * size)[: int(round((target - low) * size * size))]] += 1
        diff[y0 : y0 + size, x0 : x0 + size] = sign * block
    else:
        diff = rng.integers(-3, 4, size=(height, width)) * (1 << (bit_depth - 8))
        for _ in range(int(rng.integers(0, 4))):
            diff[rng.integers(0, height), rng.integers(0, width)] = (
                int(rng.integers(-300, 300)) * (1 << (bit_depth - 8)) // 2
            )
    low, high = np.maximum(0, diff), np.minimum(top, top + diff)
    left = (low + (rng.random((height, width)) * (high - low + 1)).astype(np.int64)).clip(0, top)
    right = left - diff
    dtype = np.uint8 if bit_depth == 8 else np.uint16
    return left.astype(dtype), right.astype(dtype)


def reference_verdict(left, right, bit_depth):
    """The original definition: every metric's maximum against its limit (``video_metrics``)."""
    metrics = recurrence.video_metrics(frame([left], bit_depth), frame([right], bit_depth))
    return metrics is not None and all(value <= VIDEO[key] for key, value in metrics.items())


# ---- the CPU early-exit verdict ------------------------------------------------------------------------------------


@pytest.mark.parametrize("bit_depth", BIT_DEPTHS)
def test_early_exit_verdict_is_the_metrics_verdict_on_boundary_cases(bit_depth):
    rng = np.random.default_rng(100 + bit_depth)
    passed = 0
    for _ in range(400):
        left, right = boundary_case(rng, bit_depth)
        expected = reference_verdict(left, right, bit_depth)
        assert recurrence._plane_matches(left, right, bit_depth) == expected
        assert recurrence.video_matches(frame([left], bit_depth), frame([right], bit_depth)) == expected
        passed += expected
    assert 80 < passed < 380  # both outcomes are exercised


def test_video_matches_refuses_what_video_metrics_refuses():
    left = np.full((40, 40), 100, dtype=np.uint8)
    base = frame([left, left, left], 8)
    assert recurrence.video_matches(base, base)
    assert not recurrence.video_matches(base, replace(base, interpretation_flags=("display_matrix",)))
    assert not recurrence.video_matches(base, replace(base, color_space="bt709"))
    assert not recurrence.video_matches(base, frame([left, left], 8))
    other = frame([left, left, np.full((36, 40), 100, dtype=np.uint8)], 8)
    assert recurrence.video_metrics(base, other) is None and not recurrence.video_matches(base, other)


def test_a_neighbouring_frame_fails_on_the_peak_alone(monkeypatch):
    """Why the early exit is cheap for the wrong offsets: nothing after the peak is computed."""
    rng = np.random.default_rng(3)
    left = rng.integers(0, 255, size=(64, 96)).astype(np.uint8)
    right = 255 - left
    called = []
    monkeypatch.setattr(recurrence, "_local_max", lambda *a: called.append(a) or 0.0)
    assert not recurrence._plane_matches(left, right, 8)
    assert called == []


# ---- spatial information ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("bit_depth", BIT_DEPTHS)
def test_spatial_information_agrees_with_numpy_std_and_decides_the_limit_the_same_way(bit_depth):
    rng = np.random.default_rng(bit_depth)
    top = (1 << bit_depth) - 1
    dtype = np.uint8 if bit_depth == 8 else np.uint16
    limit = recurrence.CANDIDATE["plane_std"]
    unit = (1 << bit_depth) / 256
    planes = []
    for spread in (0, 0.5, 4, 7.9, 8.0, 8.1, 20, 60, 127):
        noise = rng.normal(0, spread * unit, size=(48, 64))
        planes.append(np.clip(np.round(top / 2 + noise), 0, top).astype(dtype))
    half = np.full((40, 50), top // 2, dtype=dtype)  # exactly two values, a population std of exactly (8 * unit)
    half[:, ::2] += int(8 * unit)
    planes.append(half)
    for plane in planes:
        f = frame([plane], bit_depth)
        reference = recurrence._plane_std(f.planes[0], bit_depth)
        value = recurrence.spatial_information(f)
        assert abs(value - reference) <= 1e-9 * max(1.0, reference)
        assert (value < limit) == (reference < limit)


# ---- the kernel, modelled -------------------------------------------------------------------------------------------


def tile_edges(n, offset, size):
    """The grid verdict.cu builds with tile_count / tile_edge."""
    count = -(-n // size) if offset == 0 else 1 + -(-(n - offset) // size)
    edges = []
    for k in range(count + 1):
        if offset == 0:
            edges.append(min(k * size, n))
        else:
            edges.append(0 if k == 0 else min(offset + (k - 1) * size, n))
    return edges


def window_origins(n, size):
    """The window origins verdict.cu enumerates: 0, stride, ... <= n - size, then the far-edge window."""
    stride = size // 2
    count = (n - size) // stride + 1
    origins = [k * stride for k in range(count)]
    if (n - size) % stride:
        origins.append(n - size)
    return origins


@pytest.mark.parametrize("n", [32, 33, 35, 36, 40, 63, 64, 65, 270, 540, 1079, 1080, 1081, 1920])
@pytest.mark.parametrize("size", [4, 8])
def test_the_kernels_tile_grid_is_numpys(n, size):
    for offset in (0, size // 2):
        built = [x for x in np.unique(np.r_[0, np.arange(offset, n, size), n]).tolist()]
        assert tile_edges(n, offset, size)[: len(built)] == built
        assert len(tile_edges(n, offset, size)) == len(built)


@pytest.mark.parametrize("n", [32, 33, 40, 47, 48, 63, 270, 540, 1079, 1080, 1081, 1920])
@pytest.mark.parametrize("size", [8, 16, 32])
def test_the_kernels_signed_windows_are_numpys(n, size):
    if n < size:
        return
    expected = np.unique(np.r_[np.arange(0, n - size + 1, size // 2), n - size]).tolist()
    assert window_origins(n, size) == expected


def kernel_model(left, right, bit_depth):
    """verdict.cu in numpy: exact integer sums, the same bands and thresholds. (certain failures, in-band metrics)."""
    scale = 255 / ((1 << bit_depth) - 1)
    diff = left.astype(np.int64) - right.astype(np.int64)
    magnitude = np.abs(diff)
    height, width = diff.shape
    fail = ambiguous = 0
    fail += int((magnitude >= cuda_metrics._first_raw_over(VIDEO["peak"], bit_depth)).sum())
    level = cuda_metrics._first_raw_over(VIDEO["concentration_level"], bit_depth)
    for size, key in ((4, "local4"), (8, "local8")):
        for oy in (0, size // 2):
            for ox in (0, size // 2):
                ys, xs = np.array(tile_edges(height, oy, size)), np.array(tile_edges(width, ox, size))
                area = np.diff(ys)[:, None] * np.diff(xs)[None, :]
                sums = np.add.reduceat(np.add.reduceat(magnitude, ys[:-1], axis=0), xs[:-1], axis=1)
                mean = sums * scale / area
                fail += int((mean > VIDEO[key] * (1 + 1e-5)).sum())
                ambiguous += int(((mean <= VIDEO[key] * (1 + 1e-5)) & (mean >= VIDEO[key] * (1 - 1e-5))).sum())
                if size == 8:
                    counts = np.add.reduceat(
                        np.add.reduceat((magnitude >= level).astype(np.int64), ys[:-1], axis=0), xs[:-1], axis=1
                    )
                    fail += int((counts / area > VIDEO["concentration8"]).sum())
    integral = np.zeros((height + 1, width + 1), dtype=np.int64)
    integral[1:, 1:] = diff.cumsum(axis=0).cumsum(axis=1)
    for size in (8, 16, 32):
        ys, xs = np.array(window_origins(height, size)), np.array(window_origins(width, size))
        sums = (
            integral[(ys + size)[:, None], xs + size]
            - integral[ys[:, None], xs + size]
            - integral[(ys + size)[:, None], xs]
            + integral[ys[:, None], xs]
        )
        value = np.abs(sums) * scale
        bound = VIDEO["signed" + str(size)] * size * size
        fail += int((value > bound + 0.05).sum())
        ambiguous += int(((value <= bound + 0.05) & (value >= bound - 0.05)).sum())
    return fail, ambiguous


def answer(fail, ambiguous):
    return False if fail else (None if ambiguous else True)


@pytest.mark.parametrize("bit_depth", BIT_DEPTHS)
def test_modelled_kernel_never_contradicts_the_cpu_and_the_band_is_narrow(bit_depth):
    rng = np.random.default_rng(7 + bit_depth)
    definite = undecided = 0
    for _ in range(1500):
        left, right = boundary_case(rng, bit_depth)
        expected = recurrence._plane_matches(left, right, bit_depth)
        given = answer(*kernel_model(left, right, bit_depth))
        assert given in (None, expected), (bit_depth, left.shape)
        undecided += given is None
        definite += given is not None
    assert definite > 6 * undecided  # the CPU redoes a small minority only


@pytest.mark.parametrize("bit_depth", BIT_DEPTHS)
def test_integer_thresholds_are_the_cpus_float32_comparisons(bit_depth):
    scale = 255 / ((1 << bit_depth) - 1)
    for limit in (VIDEO["peak"], VIDEO["concentration_level"]):
        first = cuda_metrics._first_raw_over(limit, bit_depth)
        raw = np.arange(1 << bit_depth, dtype=np.float32)
        cpu = np.abs(raw) * scale > limit  # the comparisons video_metrics makes on float32 arrays
        assert not cpu[:first].any() and cpu[first:].all()


# ---- the real kernels (a CUDA device only) ---------------------------------------------------------------------------


def cuda_present():
    cuda_metrics.reset()
    cuda_metrics.use(0)
    usable = cuda_metrics._usable() is not None
    cuda_metrics.reset()
    return usable


@pytest.mark.skipif(not cuda_present(), reason="needs an NVIDIA GPU and libcuda")
@pytest.mark.parametrize("bit_depth", BIT_DEPTHS)
def test_real_kernels_never_contradict_the_cpu_on_boundary_cases(bit_depth):
    cuda_metrics.use(0)
    rng = np.random.default_rng(31 + bit_depth)
    definite = 0
    for _ in range(1500):
        left, right = boundary_case(rng, bit_depth)
        expected = recurrence._plane_matches(left, right, bit_depth)
        given = cuda_metrics.plane_verdict(left, right, bit_depth)
        assert given in (None, expected), (bit_depth, left.shape)
        definite += given is not None
        # and the kernels' own counters are the model's
        driver = cuda_metrics._usable()
        assert answer(*driver.counts(left, right, bit_depth)) == answer(*kernel_model(left, right, bit_depth))
    assert definite > 1000


@pytest.mark.skipif(not cuda_present(), reason="needs an NVIDIA GPU and libcuda")
def test_real_kernels_on_full_hd_planes_in_threads():
    import concurrent.futures

    cuda_metrics.use(0)
    rng = np.random.default_rng(5)
    left = rng.integers(200, 700, size=(1080, 1920)).astype(np.uint16)
    cases = [(left, left), (left, np.clip(left.astype(np.int32) + 30, 0, 1023).astype(np.uint16)), (left, 1023 - left)]
    expected = [recurrence._plane_matches(a, b, 10) for a, b in cases]
    with concurrent.futures.ThreadPoolExecutor(3) as pool:
        got = list(pool.map(lambda c: cuda_metrics.plane_verdict(c[0], c[1], 10), cases * 4))
    assert all(g in (None, e) for g, e in zip(got, expected * 4, strict=True))
    assert got[0] is True and got[2] is False


# ---- the host logic ---------------------------------------------------------------------------------------------------


class FakeDriver:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = 0

    def counts(self, left, right, bit_depth):
        self.calls += 1
        result = self.results[min(self.calls - 1, len(self.results) - 1)]
        if isinstance(result, Exception):
            raise result
        return result


def install(monkeypatch, driver):
    monkeypatch.setattr(cuda_metrics, "_device", 0)
    monkeypatch.setattr(cuda_metrics, "_driver", driver)
    monkeypatch.setattr(cuda_metrics, "_driver_device", 0)


PLANE = np.full((64, 64), 100, dtype=np.uint8)


def test_no_device_selected_means_the_cpu_decides(monkeypatch):
    assert cuda_metrics.plane_verdict(PLANE, PLANE, 8) is None
    cuda_metrics.use(None)
    assert cuda_metrics.plane_verdict(PLANE, PLANE, 8) is None


def test_the_environment_switch_and_unsupported_planes_leave_it_to_the_cpu(monkeypatch):
    driver = FakeDriver((0, 0))
    install(monkeypatch, driver)
    assert cuda_metrics.plane_verdict(PLANE, PLANE, 8) is True
    assert cuda_metrics.plane_verdict(PLANE[:20], PLANE[:20], 8) is None  # under 32 rows: the CPU's whole-plane mean
    assert cuda_metrics.plane_verdict(PLANE, PLANE, 10) is None  # 8-bit samples in a 10-bit frame
    calls = driver.calls
    monkeypatch.setenv("REPRISE_CUDA_VERDICT", "0")
    assert cuda_metrics.plane_verdict(PLANE, PLANE, 8) is None and driver.calls == calls


@pytest.mark.parametrize(("counts", "expected"), [((0, 0), True), ((3, 0), False), ((3, 2), False), ((0, 2), None)])
def test_counters_become_pass_fail_or_cpu(monkeypatch, counts, expected):
    install(monkeypatch, FakeDriver(counts))
    assert cuda_metrics.plane_verdict(PLANE, PLANE, 8) is expected


def test_repeated_gpu_errors_turn_it_off_without_changing_any_verdict(monkeypatch):
    install(monkeypatch, FakeDriver(cuda_metrics._CudaError("launch failed")))
    for _ in range(cuda_metrics.MAX_FAILURES):
        assert cuda_metrics.plane_verdict(PLANE, PLANE, 8) is None
    assert cuda_metrics._off
    assert cuda_metrics._usable() is None
    assert recurrence.video_matches(frame([PLANE], 8), frame([PLANE], 8))


def test_video_matches_asks_the_gpu_per_plane_and_stops_at_the_first_failure(monkeypatch):
    asked = []
    answers = iter([True, False, True])
    monkeypatch.setattr(cuda_metrics, "plane_verdict", lambda a, b, depth: asked.append(a.shape) or next(answers))
    planes = [
        np.full((64, 64), 9, dtype=np.uint8),
        np.full((32, 32), 9, dtype=np.uint8),
        np.full((32, 32), 9, dtype=np.uint8),
    ]
    assert not recurrence.video_matches(frame(planes, 8), frame(planes, 8))
    assert asked == [(64, 64), (32, 32)]


def test_video_matches_decides_on_the_cpu_where_the_gpu_is_unsure(monkeypatch):
    monkeypatch.setattr(cuda_metrics, "plane_verdict", lambda a, b, depth: None)
    equal = np.full((64, 64), 9, dtype=np.uint8)
    far = np.full((64, 64), 200, dtype=np.uint8)
    assert recurrence.video_matches(frame([equal], 8), frame([equal], 8))
    assert not recurrence.video_matches(frame([equal], 8), frame([far], 8))


def test_use_selects_the_first_nvidia_device_native_frames_was_given():
    from media_preview_generator.reprise.acceleration import Accelerator

    nvidia = Accelerator("cuda", "cuda:2", "NVIDIA", "Tesla P40", ())
    intel = Accelerator("vaapi", "/dev/dri/renderD129", "INTEL", "UHD 630", ())
    assert nvidia.cuda_index == 2 and intel.cuda_index is None
    assert Accelerator("cuda", "cuda:", "NVIDIA", "x", ()).cuda_index is None


# ---- the shipped PTX ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["verdict_u8.ptx", "verdict_u16.ptx"])
def test_shipped_ptx_is_built_from_the_current_source(name):
    source = (KERNELS / "verdict.cu").read_text()
    header = (KERNELS / name).read_text().splitlines()[0]
    assert header == f"// verdict.cu sha256 {hashlib.sha256(source.encode()).hexdigest()}", (
        "verdict.cu changed: rebuild the PTX with tools/build_reprise_ptx.py (see cuda_kernels/README.md)"
    )
    assert ".target sm_61" in (KERNELS / name).read_text()


def test_the_kernel_source_includes_nothing_from_nvidia():
    source = (KERNELS / "verdict.cu").read_text()
    assert "#include" not in source and os.path.exists(KERNELS / "README.md")


def test_threads_that_arrive_together_load_the_driver_once(monkeypatch):
    import concurrent.futures
    import time

    built = []

    class Slow:
        def __init__(self, device):
            time.sleep(0.2)
            built.append(device)

    monkeypatch.setattr(cuda_metrics, "_Driver", Slow)
    monkeypatch.setattr(cuda_metrics, "_selftest", lambda driver: None)
    cuda_metrics.use(0)
    with concurrent.futures.ThreadPoolExecutor(6) as pool:
        drivers = list(pool.map(lambda _: cuda_metrics._usable(), range(6)))
    assert built == [0] and all(d is drivers[0] for d in drivers)


def test_a_failed_selftest_turns_the_gpu_off_and_the_cpu_decides(monkeypatch):
    class Broken:
        def __init__(self, device):
            pass

    def refuse(driver):
        raise cuda_metrics._CudaError("self-test: kernel said True, CPU said False")

    monkeypatch.setattr(cuda_metrics, "_Driver", Broken)
    monkeypatch.setattr(cuda_metrics, "_selftest", refuse)
    cuda_metrics.use(0)
    assert cuda_metrics.plane_verdict(PLANE, PLANE, 8) is None
    assert cuda_metrics._off and cuda_metrics._usable() is None


def test_a_missing_libcuda_is_silent(monkeypatch):
    def missing(name):
        raise OSError("libcuda.so.1: cannot open shared object file")

    monkeypatch.setattr(cuda_metrics.ctypes, "CDLL", missing)
    cuda_metrics.use(0)
    assert cuda_metrics.plane_verdict(PLANE, PLANE, 8) is None and cuda_metrics._off
