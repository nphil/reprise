"""GPU decode selection and CPU fallback for the native frame reader (no GPU needed; the command shape is real)."""

import json
import shutil
import subprocess
import time
from fractions import Fraction
from types import SimpleNamespace

import pytest

from media_preview_generator.reprise import acceleration, media, preview
from media_preview_generator.reprise.acceleration import Accelerator, Verdict
from media_preview_generator.reprise.identity import media_version

NVIDIA = Accelerator("cuda", "cuda:0", "NVIDIA", "Tesla P40", ("-hwaccel", "cuda", "-hwaccel_device", "0"))
INTEL = Accelerator(
    "vaapi",
    "/dev/dri/renderD129",
    "INTEL",
    "Intel UHD Graphics 630",
    ("-hwaccel", "vaapi", "-hwaccel_device", "/dev/dri/renderD129"),
)
ALL = frozenset({"h264", "hevc"})
REAL_ENABLED = acceleration.enabled


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    acceleration.reset()
    monkeypatch.setattr(acceleration, "enabled", lambda: True)
    yield
    acceleration.reset()


def verified(accel, ffmpeg, families=ALL):
    acceleration._verdicts[(ffmpeg, accel.device)] = Verdict(frozenset(families), "", time.monotonic())


@pytest.mark.parametrize(
    "codec,profile,pix,size,family",
    [
        ("h264", "High", "yuv420p", (1920, 1080), "h264"),
        ("h264", "Main", "yuv420p", (1916, 1076), "h264"),
        ("h264", "Constrained Baseline", "yuv420p", (640, 480), "h264"),
        ("h264", "High 10", "yuv420p10le", (1920, 1080), None),
        ("h264", "High", "yuv422p", (1920, 1080), None),
        ("h264", "High 4:4:4 Predictive", "yuv444p", (1920, 1080), None),
        ("hevc", "Main", "yuv420p", (1920, 1080), "hevc"),
        ("hevc", "Main 10", "yuv420p10le", (3840, 2160), "hevc"),
        ("hevc", "Main 10", "yuv420p10le", (8192, 4320), None),
        ("hevc", "Main 10", "yuv420p10le", (64, 48), None),
        ("hevc", "Rext", "yuv420p10le", (1920, 1080), None),
        ("mpeg2video", "Main", "yuv420p", (720, 480), None),
        ("vp9", "Profile 0", "yuv420p", (1920, 1080), None),
        ("av1", "Main", "yuv420p", (1920, 1080), None),
        (None, None, "yuv420p", (1920, 1080), None),
    ],
)
def test_only_verified_hardware_decodable_streams_have_a_family(codec, profile, pix, size, family):
    assert acceleration.family_of(codec, profile, pix, *size) == family


def test_eligibility_needs_a_passed_check_for_that_family(monkeypatch):
    assert not acceleration.eligible(NVIDIA, "ffmpeg", "h264", "High", "yuv420p", 1920, 1080)
    verified(NVIDIA, "ffmpeg", {"h264"})
    assert acceleration.eligible(NVIDIA, "ffmpeg", "h264", "High", "yuv420p", 1920, 1080)
    assert not acceleration.eligible(NVIDIA, "ffmpeg", "hevc", "Main 10", "yuv420p10le", 1920, 1080)
    assert not acceleration.eligible(NVIDIA, "other-ffmpeg", "h264", "High", "yuv420p", 1920, 1080)
    acceleration.withdraw(NVIDIA, "ffmpeg", "h264", "frames differed")
    assert not acceleration.eligible(NVIDIA, "ffmpeg", "h264", "High", "yuv420p", 1920, 1080)


@pytest.mark.parametrize(
    "vendor,name,title",
    [
        ("INTEL", "Intel Corporation CoffeeLake-S GT2 [UHD Graphics 630] (rev 02)", "Intel UHD Graphics 630"),
        ("NVIDIA", "Tesla P40", "NVIDIA Tesla P40"),
        ("NVIDIA", "NVIDIA GeForce RTX 3080", "NVIDIA GeForce RTX 3080"),
        ("NVIDIA", "NVIDIA Corporation GP102GL [Tesla P40] (rev a1)", "NVIDIA Tesla P40"),
        ("AMD", "Advanced Micro Devices, Inc. [AMD/ATI] Navi 23 [Radeon RX 6600] (rev c1)", "AMD Radeon RX 6600"),
        ("INTEL", "Intel Corporation Raptor Lake-S GT1 (rev 04)", "Intel Raptor Lake-S GT1"),
        ("INTEL", "", "Intel"),
    ],
)
def test_gpu_names_are_shortened_for_display(vendor, name, title):
    accel = Accelerator("vaapi", "/dev/dri/renderD129", vendor, name, ())
    assert accel.title == title and accel.label == f"{title} (VAAPI decode)"


SHOWINFO = (
    "[Parsed_showinfo_1 @ 0x1] [info] n:   {n} pts:{pts:7d} pts_time:0       duration:   1000 duration_time:1       "
    "fmt:yuv420p cl:{cl} sar:1/1 s:1280x720 i:P iskey:1 type:I checksum:3FC2E582 plane_checksum:[{a} 4DD149CE 1514D031] "
    "mean:[1 2 3] stdev:[0.1 0.2 0.3]\n"
    "[Parsed_showinfo_1 @ 0x1] [info] color_range:tv color_space:bt709 color_primaries:bt709 color_trc:bt709\n"
)


def fake_run(stderr, returncode=0):
    return lambda *a, **k: SimpleNamespace(stderr=stderr.encode(), returncode=returncode)


def test_check_reads_timestamp_format_checksums_and_colour_but_not_chroma_location(monkeypatch):
    left = SHOWINFO.format(n=0, pts=0, cl="left", a="F901CB74") + SHOWINFO.format(
        n=1, pts=1000, cl="left", a="5719DC1C"
    )
    unspecified = left.replace("cl:left", "cl:unspecified")  # hardware frames do not carry the decoder's chroma tag
    monkeypatch.setattr(acceleration.subprocess, "run", fake_run(left))
    cpu = acceleration._showinfo_frames("ffmpeg", acceleration.CLIPS_DIR / "h264-8bit.mkv", "yuv420p", ())
    monkeypatch.setattr(acceleration.subprocess, "run", fake_run(unspecified))
    gpu = acceleration._showinfo_frames("ffmpeg", acceleration.CLIPS_DIR / "h264-8bit.mkv", "yuv420p", NVIDIA.args)
    assert len(cpu) == 2 and cpu == gpu
    assert cpu[0][0] == "0" and cpu[1][0] == "1000" and cpu[0][-1].startswith("color_range:tv")
    changed = left.replace("F901CB74", "F901CB75")
    monkeypatch.setattr(acceleration.subprocess, "run", fake_run(changed))
    assert acceleration._showinfo_frames("ffmpeg", acceleration.CLIPS_DIR / "h264-8bit.mkv", "yuv420p", ()) != cpu


@pytest.mark.parametrize(
    "stderr,returncode",
    [
        ("[AVHWDeviceContext @ 0x1] [error] Cannot load libcuda.so.1\n", 0),
        ("[VAAPI @ 0x1] [error] libva: iHD_drv_video.so has no function __vaDriverInit_1_0\n", 0),
        ("[h264 @ 0x1] [warning] something odd\n", 0),
        ("", 0),
        (SHOWINFO.format(n=0, pts=0, cl="left", a="AA"), 1),
    ],
)
def test_check_fails_on_diagnostics_no_frames_or_exit_status(monkeypatch, stderr, returncode):
    monkeypatch.setattr(acceleration.subprocess, "run", fake_run(stderr, returncode))
    with pytest.raises(acceleration._CheckFailed):
        acceleration._showinfo_frames("ffmpeg", acceleration.CLIPS_DIR / "h264-8bit.mkv", "yuv420p", NVIDIA.args)


def test_check_timeout_and_missing_binary_are_check_failures(monkeypatch):
    def timeout(*a, **k):
        raise subprocess.TimeoutExpired("ffmpeg", 30)

    monkeypatch.setattr(acceleration.subprocess, "run", timeout)
    with pytest.raises(acceleration._CheckFailed, match="timed out"):
        acceleration._showinfo_frames("ffmpeg", acceleration.CLIPS_DIR / "h264-8bit.mkv", "yuv420p", ())

    def missing(*a, **k):
        raise FileNotFoundError("ffmpeg")

    monkeypatch.setattr(acceleration.subprocess, "run", missing)
    with pytest.raises(acceleration._CheckFailed, match="cannot run"):
        acceleration._showinfo_frames("ffmpeg", acceleration.CLIPS_DIR / "h264-8bit.mkv", "yuv420p", ())


def test_packaged_reference_clips_exist():
    for clip, _pix in acceleration.REFERENCE.values():
        assert (acceleration.CLIPS_DIR / clip).is_file()


def fake_decode(monkeypatch, gpu_frames):
    """CPU frames are fixed; the GPU's are whatever the test says, per clip."""

    def frames(ffmpeg, clip, pix, args):
        if not args:
            return [(clip.name, "cpu")]
        result = gpu_frames(clip.name)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(acceleration, "_showinfo_frames", frames)


def test_device_is_verified_per_family_by_matching_the_cpu(monkeypatch):
    fake_decode(
        monkeypatch,
        lambda name: [(name, "cpu")] if name.startswith("h264") else [(name, "different checksums")],
    )
    verdict = acceleration.verify(NVIDIA, "ffmpeg")
    assert verdict.families == {"h264"}
    assert "hevc: decoded frames differ from the CPU's" in verdict.detail
    assert acceleration.eligible(NVIDIA, "ffmpeg", "h264", "High", "yuv420p", 1920, 1080)
    assert not acceleration.eligible(NVIDIA, "ffmpeg", "hevc", "Main 10", "yuv420p10le", 1920, 1080)


def test_checked_once_and_failed_checks_are_repeated_later(monkeypatch):
    calls = []
    now = [100.0]
    monkeypatch.setattr(acceleration, "_now", lambda: now[0])

    def frames(ffmpeg, clip, pix, args):
        calls.append(bool(args))
        if args:
            raise acceleration._CheckFailed("CUDA out of memory")
        return [(clip.name, "cpu")]

    monkeypatch.setattr(acceleration, "_showinfo_frames", frames)
    first = acceleration.verify(NVIDIA, "ffmpeg")
    assert first.families == frozenset() and "CUDA out of memory" in first.detail
    count = len(calls)
    assert acceleration.verify(NVIDIA, "ffmpeg") is first and len(calls) == count
    now[0] += acceleration.RETRY_AFTER_S + 1
    monkeypatch.setattr(acceleration, "_showinfo_frames", lambda ffmpeg, clip, pix, args: [(clip.name, "cpu")])
    assert acceleration.verify(NVIDIA, "ffmpeg").families == ALL


def test_a_passed_check_is_kept_and_a_crashing_check_is_a_failed_check(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(acceleration, "_now", lambda: now[0])
    fake_decode(monkeypatch, lambda name: [(name, "cpu")])
    passed = acceleration.verify(NVIDIA, "ffmpeg")
    assert passed.families == ALL
    now[0] += 10 * acceleration.RETRY_AFTER_S
    assert acceleration.verify(NVIDIA, "ffmpeg") is passed

    def broken(ffmpeg, clip, pix, args):
        raise RuntimeError("boom")

    monkeypatch.setattr(acceleration, "_showinfo_frames", broken)
    failed = acceleration.verify(INTEL, "ffmpeg")
    assert failed.families == frozenset() and "RuntimeError: boom" in failed.detail


def gpu(kind, device, name, vendor, **extra):
    return {"type": vendor, "device": device, "name": name, "acceleration": kind, "status": "ok", **extra}


class Manager:
    def __init__(self, gpu_config=(), **values):
        self.gpu_config = list(gpu_config)
        self.values = values

    def get(self, key, default=None):
        return self.values.get(key, default)


@pytest.fixture
def detection(monkeypatch):
    def install(gpus, manager=None):
        from media_preview_generator.web import settings_manager
        from media_preview_generator.web.routes import _helpers

        monkeypatch.setattr(_helpers, "_ensure_gpu_cache", lambda: gpus)
        monkeypatch.setattr(settings_manager, "peek_settings_manager", lambda: manager)

    return install


def test_detected_devices_nvidia_first_and_only_those_that_passed_detection(detection):
    detection(
        [
            gpu("VAAPI", "/dev/dri/renderD129", "Intel UHD Graphics 630", "INTEL"),
            gpu("CUDA", "cuda:0", "Tesla P40", "NVIDIA"),
            {**gpu("CUDA", "cuda:1", "Broken", "NVIDIA"), "status": "failed"},
            gpu("VAAPI", "", "No device", "AMD"),
            gpu("D3D11VA", "d3d11", "Windows", "WINDOWS_GPU"),
        ]
    )
    found = acceleration._detected()
    assert [(a.kind, a.device, allowed) for a, allowed in found] == [
        ("cuda", "cuda:0", True),
        ("vaapi", "/dev/dri/renderD129", True),
    ]
    assert found[0][0].args == ("-hwaccel", "cuda", "-hwaccel_device", "0")
    assert found[1][0].args == ("-hwaccel", "vaapi", "-hwaccel_device", "/dev/dri/renderD129")
    assert found[0][0].label == "NVIDIA Tesla P40 (NVDEC decode)"
    assert found[1][0].label == "Intel UHD Graphics 630 (VAAPI decode)"


def test_gpu_settings_that_disable_a_device_apply_here_too(detection):
    detection(
        [gpu("CUDA", "cuda:0", "Tesla P40", "NVIDIA"), gpu("VAAPI", "/dev/dri/renderD129", "Intel UHD", "INTEL")],
        Manager(
            [{"device": "cuda:0", "enabled": False, "workers": 1}, {"device": "/dev/dri/renderD129", "workers": 0}]
        ),
    )
    assert [allowed for _, allowed in acceleration._detected()] == [False, False]
    detection(
        [gpu("CUDA", "cuda:0", "Tesla P40", "NVIDIA")], Manager([{"device": "cuda:0", "enabled": True, "workers": 2}])
    )
    assert [allowed for _, allowed in acceleration._detected()] == [True]


def test_plan_lists_verified_devices_best_first_and_nothing_when_off_or_unverified(detection, monkeypatch):
    detection([gpu("VAAPI", "/dev/dri/renderD129", "Intel UHD", "INTEL"), gpu("CUDA", "cuda:0", "Tesla P40", "NVIDIA")])
    fake_decode(monkeypatch, lambda name: [(name, "cpu")])
    assert [a.kind for a in acceleration.plan("ffmpeg")] == ["cuda", "vaapi"]
    fake_decode(monkeypatch, lambda name: [(name, "wrong")])
    acceleration.reset()
    assert acceleration.plan("ffmpeg") == []
    fake_decode(monkeypatch, lambda name: [(name, "cpu")])
    acceleration.reset()
    monkeypatch.setattr(acceleration, "enabled", lambda: False)
    assert acceleration.plan("ffmpeg") == []


def test_repeated_fallbacks_pause_a_device_then_it_returns(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(acceleration, "_now", lambda: now[0])
    for _ in range(acceleration.FAILURES_BEFORE_SUSPEND - 1):
        acceleration.record_fallback(NVIDIA, "out of memory")
    assert not acceleration._suspended(NVIDIA)
    acceleration.record_gpu(NVIDIA, "h264")  # a success resets the run of failures
    for _ in range(acceleration.FAILURES_BEFORE_SUSPEND):
        acceleration.record_fallback(NVIDIA, "out of memory")
    assert acceleration._suspended(NVIDIA) and not acceleration._suspended(INTEL)
    now[0] += acceleration.SUSPEND_S + 1
    assert not acceleration._suspended(NVIDIA)


def test_status_summary_for_every_situation(detection, monkeypatch):
    monkeypatch.setattr(acceleration, "ffmpeg_binary", lambda: "ffmpeg")
    monkeypatch.setattr(acceleration, "warm", lambda ffmpeg=None: None)
    detection([])
    assert acceleration.status()["label"] == "CPU (no GPU found)"
    detection([gpu("CUDA", "cuda:0", "Tesla P40", "NVIDIA"), gpu("VAAPI", "/dev/dri/renderD129", "Intel UHD", "INTEL")])
    checking = acceleration.status()
    assert checking["label"] == "CPU (checking GPU decode)" and not checking["active"]
    assert {d["state"] for d in checking["devices"]} == {"checking"}
    verified(NVIDIA, "ffmpeg")
    verified(INTEL, "ffmpeg", {"h264"})
    ready = acceleration.status()
    assert ready["active"] and ready["enabled"]
    assert ready["label"] == "GPU: NVIDIA Tesla P40 (NVDEC decode), then Intel UHD (VAAPI decode)"
    assert [(d["method"], d["state"], d["decodes"]) for d in ready["devices"]] == [
        ("NVDEC", "ready", ["h264", "hevc"]),
        ("VAAPI", "ready", ["h264"]),
    ]
    acceleration._verdicts[("ffmpeg", NVIDIA.device)] = Verdict(frozenset(), "cuda: out of memory", time.monotonic())
    verified(INTEL, "ffmpeg", {"h264"})
    one = acceleration.status()
    assert one["label"] == "GPU: Intel UHD (VAAPI decode)"
    assert one["devices"][0]["state"] == "unavailable" and "out of memory" in one["devices"][0]["detail"]
    acceleration._verdicts.clear()
    acceleration._verdicts[("ffmpeg", NVIDIA.device)] = Verdict(frozenset(), "no", time.monotonic())
    acceleration._verdicts[("ffmpeg", INTEL.device)] = Verdict(frozenset(), "no", time.monotonic())
    assert acceleration.status()["label"] == "CPU (GPU decode unavailable)"
    monkeypatch.setattr(acceleration, "enabled", lambda: False)
    off = acceleration.status()
    assert off["label"] == "CPU (GPU decode is turned off)" and not off["active"] and not off["enabled"]
    assert {d["state"] for d in off["devices"]} == {"off"}


# --- the reader's GPU command shape, decoded in software (an accelerator with no hwaccel options) ------------------


def x264_clip(tmp_path, name="clip.mkv"):
    path = tmp_path / name
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-v", "error", "-threads", "1", "-f", "lavfi", "-i", "testsrc2=size=192x144:rate=24:duration=3",
            "-c:v", "libx264", "-profile:v", "high", "-pix_fmt", "yuv420p", "-bf", "2", "-g", "12", "-chroma_sample_location", "left",
            "-threads", "1", "-y", str(path),
        ],
        check=True,
    )  # fmt: skip
    return media_version(str(path), 3000, "{}")


def read_all(item, ffmpeg, **kw):
    with media.NativeFrameReader(item, 0, 500, 2500, lambda: False, lambda: False, 1, ffmpeg, **kw) as reader:
        frames = list(reader)
        return reader, frames, reader.termination


SOFTWARE_GPU = Accelerator("cuda", "cuda:0", "NVIDIA", "Test", ())


def test_gpu_command_shape_produces_the_cpu_readers_frames_exactly(tmp_path):
    ffmpeg = shutil.which("ffmpeg")
    item = x264_clip(tmp_path)
    verified(SOFTWARE_GPU, ffmpeg)
    cpu_reader, cpu_frames, cpu_end = read_all(item, ffmpeg)
    gpu_reader, gpu_frames, gpu_end = read_all(item, ffmpeg, accel=SOFTWARE_GPU)
    assert not cpu_reader.accelerated and gpu_reader.accelerated
    cpu_args, gpu_args = cpu_reader.proc.args, gpu_reader.proc.args
    assert cpu_args[cpu_args.index("-vf") + 1].startswith("showinfo,trim=end=")
    assert gpu_args[gpu_args.index("-vf") + 1].startswith("format=yuv420p,showinfo,trim=end=")
    assert gpu_reader.showinfo == {"1": "0", "3": "2"} and cpu_reader.showinfo == {"0": "0", "2": "2"}
    assert len(cpu_frames) > 20 and len(gpu_frames) == len(cpu_frames) and gpu_end == cpu_end
    for a, b in zip(cpu_frames, gpu_frames, strict=True):
        assert a.pts_ms == b.pts_ms
        assert [bytes(p.data) for p in a.planes] == [bytes(p.data) for p in b.planes]
        assert (a.pixel_format, a.color_range, a.color_space, a.chroma_location) == (
            b.pixel_format, b.color_range, b.color_space, b.chroma_location,
        )  # fmt: skip


def test_unverified_or_unsupported_streams_decode_on_the_cpu_with_the_old_command(tmp_path):
    ffmpeg = shutil.which("ffmpeg")
    item = x264_clip(tmp_path)
    reader, frames, _ = read_all(item, ffmpeg, accel=SOFTWARE_GPU)  # no check has passed for this device
    assert not reader.accelerated and reader.showinfo == {"0": "0", "2": "2"} and frames
    assert reader.proc.args[reader.proc.args.index("-vf") + 1].startswith("showinfo,trim=end=")
    verified(SOFTWARE_GPU, ffmpeg, {"hevc"})  # verified for another codec only
    reader, _, _ = read_all(item, ffmpeg, accel=SOFTWARE_GPU)
    assert not reader.accelerated


def test_native_frames_without_a_gpu_is_the_plain_reader(tmp_path, detection):
    detection([])
    ffmpeg = shutil.which("ffmpeg")
    item = x264_clip(tmp_path)
    with media.native_frames(item, 0, 500, 2500, lambda: False, lambda: False, 1, ffmpeg_path=ffmpeg) as reader:
        assert type(reader) is media.NativeFrameReader and not reader.accelerated
        assert list(reader)
    assert acceleration.status()["windows"]["cpu"] == 1


def test_native_frames_with_a_verified_device_decodes_through_the_gpu_command(tmp_path, detection, monkeypatch):
    ffmpeg = shutil.which("ffmpeg")
    item = x264_clip(tmp_path)
    monkeypatch.setattr(acceleration, "_detected", lambda: [(SOFTWARE_GPU, True)])
    verified(SOFTWARE_GPU, ffmpeg)
    with media.native_frames(item, 0, 500, 2500, lambda: False, lambda: False, 1, ffmpeg_path=ffmpeg) as reader:
        frames = list(reader)
        assert reader.accelerated and reader.termination == "boundary" and reader.proc.args
    expected = read_all(item, ffmpeg)[1]
    assert [f.pts_ms for f in frames] == [f.pts_ms for f in expected]
    assert acceleration.status()["windows"] == {"gpu": 1, "cpu": 0, "gpu_fallbacks": 0}


# --- fallback, with scripted readers -------------------------------------------------------------------------------


def frame(i, tag=b"x"):
    return SimpleNamespace(pts_ms=Fraction(40 * i), planes=(SimpleNamespace(data=memoryview(tag + bytes([i]))),))


class Script:
    """Fake reader: what each device (None = CPU) yields, an Exception raising where it sits in the list."""

    def __init__(self, monkeypatch, scripts, codec="h264", profile="High", pix="yuv420p", size=(1920, 1080)):
        self.scripts, self.opened, self.exits, self.audits = scripts, [], [], 0
        outer = self

        class Reader:
            def __init__(self, item, stream, start, end, cancel, pause, threads, ffmpeg_path, accel=None, audit=None):
                self.accel, self.audit = accel, audit
                self.codec, self.profile, self.pix = codec, profile, pix
                self.width, self.height = size
                self.accelerated = accel is not None
                self.termination = None

            def __enter__(self):
                outer.opened.append(self.accel)
                self.events = iter(outer.scripts[self.accel])
                return self

            def __next__(self):
                try:
                    event = next(self.events)
                except StopIteration:
                    self.termination = "eof"
                    raise
                if isinstance(event, Exception):
                    raise event
                return event

            def __exit__(self, exc_type, exc, tb):
                outer.exits.append(exc_type)
                if exc_type is None and self.termination is None:
                    raise media.DecodeError("not exhausted")
                return False

        def audit(*args):
            outer.audits += 1
            return (pix, size[0], size[1], 1, 1, 1, 1, "left", 1, (), set(), 1, codec, profile)

        monkeypatch.setattr(media, "NativeFrameReader", Reader)
        monkeypatch.setattr(media, "_native_audit", audit)

    def frames(self, attempts, ffmpeg="ffmpeg"):
        reader = media._FallbackFrames("item", 0, 0, 1000, lambda: False, lambda: False, 1, ffmpeg, attempts)
        with reader as frames:
            return reader, list(frames)


def test_a_gpu_decode_that_fails_at_the_start_is_repeated_on_the_cpu(monkeypatch):
    verified(NVIDIA, "ffmpeg")
    script = Script(
        monkeypatch, {NVIDIA: [media.DecodeError("CUDA_ERROR_OUT_OF_MEMORY")], None: [frame(i) for i in range(4)]}
    )
    _, delivered = script.frames([NVIDIA])
    assert [f.pts_ms for f in delivered] == [0, 40, 80, 120]
    assert script.opened == [NVIDIA, None] and script.audits == 1
    status = acceleration.status()
    assert status["windows"] == {"gpu": 0, "cpu": 1, "gpu_fallbacks": 1}
    assert "CUDA_ERROR_OUT_OF_MEMORY" in status["last_problem"]


def test_a_gpu_decode_that_fails_half_way_is_repeated_without_repeating_frames(monkeypatch):
    verified(NVIDIA, "ffmpeg")
    full = [frame(i) for i in range(6)]
    script = Script(monkeypatch, {NVIDIA: [*full[:2], media.DecodeError("Native frame payload stalled")], None: full})
    reader, delivered = script.frames([NVIDIA])
    assert [f.pts_ms for f in delivered] == [0, 40, 80, 120, 160, 200]
    assert script.opened == [NVIDIA, None] and script.audits == 1
    assert acceleration.status()["windows"] == {"gpu": 0, "cpu": 1, "gpu_fallbacks": 1}
    assert reader.termination == "eof"  # the reader's own attributes pass through


def test_a_failed_decode_goes_to_the_cpu_not_to_a_second_gpu(monkeypatch):
    # The CPU decides whose fault a failure was; a file it cannot decode is not spent again on every device first.
    verified(NVIDIA, "ffmpeg")
    verified(INTEL, "ffmpeg")
    full = [frame(i) for i in range(3)]
    script = Script(monkeypatch, {NVIDIA: [media.DecodeError("busy")], INTEL: full, None: full})
    _, delivered = script.frames([NVIDIA, INTEL])
    assert len(delivered) == 3 and script.opened == [NVIDIA, None]
    assert acceleration.status()["windows"] == {"gpu": 0, "cpu": 1, "gpu_fallbacks": 1}


def test_a_repeat_that_disagrees_fails_and_withdraws_the_device_for_the_codec(monkeypatch):
    verified(NVIDIA, "ffmpeg")
    Script(
        monkeypatch,
        {NVIDIA: [frame(0), frame(1), media.DecodeError("decode error")], None: [frame(0), frame(1, b"y"), frame(2)]},
    )
    reader = media._FallbackFrames("item", 0, 0, 1000, lambda: False, lambda: False, 1, "ffmpeg", [NVIDIA])
    with pytest.raises(media.DecodeError, match="disagree"):
        with reader as frames:
            list(frames)
    assert not acceleration.eligible(NVIDIA, "ffmpeg", "h264", "High", "yuv420p", 1920, 1080)
    assert "differs" in acceleration.status()["last_problem"]


def test_a_repeat_that_ends_early_also_disagrees(monkeypatch):
    verified(NVIDIA, "ffmpeg")
    Script(monkeypatch, {NVIDIA: [frame(0), frame(1), media.DecodeError("x")], None: [frame(0)]})
    reader = media._FallbackFrames("item", 0, 0, 1000, lambda: False, lambda: False, 1, "ffmpeg", [NVIDIA])
    with pytest.raises(media.DecodeError, match="disagree"):
        with reader as frames:
            list(frames)


def test_cancellation_is_never_retried_and_a_cpu_failure_keeps_its_own_error(monkeypatch):
    verified(NVIDIA, "ffmpeg")
    script = Script(monkeypatch, {NVIDIA: [frame(0), media.Cancelled("Analysis cancelled")], None: []})
    with pytest.raises(media.Cancelled):
        script.frames([NVIDIA])
    assert script.opened == [NVIDIA]
    script = Script(monkeypatch, {NVIDIA: [media.DecodeError("gpu")], None: [media.DecodeError("corrupt file")]})
    with pytest.raises(media.DecodeError, match="corrupt file"):
        script.frames([NVIDIA])
    assert script.opened == [NVIDIA, None]
    assert acceleration.status()["windows"]["gpu_fallbacks"] == 0  # the file's fault, not the device's


def test_only_devices_verified_for_the_stream_are_tried(monkeypatch):
    verified(NVIDIA, "ffmpeg", {"hevc"})
    verified(INTEL, "ffmpeg", {"h264"})
    script = Script(monkeypatch, {INTEL: [frame(0), frame(1)], None: []})
    _, delivered = script.frames([NVIDIA, INTEL])
    assert len(delivered) == 2 and script.opened == [INTEL]
    script = Script(monkeypatch, {None: [frame(0)]}, codec="h264", profile="High 10", pix="yuv420p10le")
    script.frames([NVIDIA, INTEL])
    assert script.opened == [None] and script.audits == 1


def test_gpu_success_is_counted_and_logged_once(monkeypatch):
    verified(NVIDIA, "ffmpeg")
    script = Script(monkeypatch, {NVIDIA: [frame(0), frame(1)]})
    script.frames([NVIDIA])
    script.frames([NVIDIA])
    assert acceleration.status()["windows"] == {"gpu": 2, "cpu": 0, "gpu_fallbacks": 0}


def test_abandoning_a_gpu_reader_is_not_a_success(monkeypatch):
    verified(NVIDIA, "ffmpeg")
    Script(monkeypatch, {NVIDIA: [frame(0), frame(1)]})
    reader = media._FallbackFrames("item", 0, 0, 1000, lambda: False, lambda: False, 1, "ffmpeg", [NVIDIA])
    with pytest.raises(media.DecodeError, match="not exhausted"):
        with reader as frames:
            next(frames)
    assert acceleration.status()["windows"]["gpu"] == 0


def preview_source(tmp_path):
    path = tmp_path / "source.mkv"
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=192x144:rate=10:duration=3",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=3", "-map", "0:v", "-map", "1:a",
            "-c:v", "libx264", "-profile:v", "high", "-pix_fmt", "yuv420p", "-threads", "1", "-c:a", "aac", "-y", str(path),
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip
    return media_version(str(path), 3000, json.dumps({"audio_streams": [{"plex_id": "first", "index": 1}]}))


def build_preview(source, tmp_path, ffmpeg):
    return preview.build_preview(
        source,
        start_ms=500,
        end_ms=2000,
        audio_stream_id="first",
        cache_dir=tmp_path / "cache",
        cancel_check=lambda: False,
        pause_check=lambda: False,
        ffmpeg_path=ffmpeg,
    )


def test_preview_decodes_on_a_verified_gpu_and_falls_back_to_the_cpu(tmp_path, monkeypatch):
    ffmpeg = shutil.which("ffmpeg")
    source = preview_source(tmp_path)
    monkeypatch.setattr(acceleration, "_detected", lambda: [(NVIDIA, True)])
    verified(NVIDIA, ffmpeg)
    original, commands = preview._encode, []

    def encode(command, path, cancel, pause):
        commands.append(command)
        if "-hwaccel" in command:
            raise media.DecodeError("CUDA_ERROR_NO_DEVICE")
        return original(command, path, cancel, pause)

    monkeypatch.setattr(preview, "_encode", encode)
    result = build_preview(source, tmp_path, ffmpeg)
    assert len(commands) == 2 and result.path.is_file() and result.path.stat().st_size > 0
    first, second = commands
    at = first.index("-hwaccel")
    assert first[at : at + 4] == list(NVIDIA.args) and at < first.index("-i") and "-hwaccel" not in second
    assert first[:at] + first[at + 4 :] == second  # the CPU repeat is the same command without the GPU
    assert acceleration.status()["windows"]["gpu_fallbacks"] == 1


def test_preview_uses_no_gpu_when_none_is_verified_for_the_stream(tmp_path, monkeypatch):
    ffmpeg = shutil.which("ffmpeg")
    source = preview_source(tmp_path)
    monkeypatch.setattr(acceleration, "_detected", lambda: [(NVIDIA, True)])
    verified(NVIDIA, ffmpeg, {"hevc"})
    original, commands = preview._encode, []

    def encode(command, path, cancel, pause):
        commands.append(command)
        return original(command, path, cancel, pause)

    monkeypatch.setattr(preview, "_encode", encode)
    build_preview(source, tmp_path, ffmpeg)
    assert len(commands) == 1 and "-hwaccel" not in commands[0]


# --- the owner's switch and the status the UI shows ----------------------------------------------------------------


def test_owner_endpoint_reports_acceleration_and_toggles_it(tmp_path, monkeypatch):
    from flask import Flask

    from media_preview_generator.markers.store import MarkerStore
    from media_preview_generator.reprise.service import RepriseService
    from media_preview_generator.reprise.store import RepriseStore
    from media_preview_generator.servers.registry import ServerRegistry
    from media_preview_generator.web import settings_manager
    from media_preview_generator.web.routes import _helpers
    from media_preview_generator.web.routes.api_reprise import create_reprise_blueprint

    monkeypatch.setattr(acceleration, "enabled", REAL_ENABLED)  # the real owner switch, not the fixture's stand-in
    monkeypatch.setenv("WEB_AUTH_TOKEN", "owner-test-only")
    monkeypatch.setattr("media_preview_generator.web.auth.is_auth_external", lambda: True)
    monkeypatch.setattr(_helpers, "_ensure_gpu_cache", lambda: [])
    settings_manager.reset_settings_manager()
    manager = settings_manager.get_settings_manager(str(tmp_path))
    markers = MarkerStore(str(tmp_path / "markers.db"))
    try:
        app = Flask(__name__)
        app.secret_key = "throwaway"
        app.register_blueprint(create_reprise_blueprint(RepriseService(RepriseStore(markers), ServerRegistry(), None)))
        client = app.test_client()
        body = client.get("/api/reprise/settings").get_json()
        assert body["acceleration"]["label"] == "CPU (no GPU found)" and body["acceleration"]["enabled"] is True
        assert body["writes_enabled"] is False and "windows" in body["acceleration"]
        off = client.patch("/api/reprise/settings", json={"gpu_decode": False})
        assert off.status_code == 200 and off.get_json()["acceleration"]["enabled"] is False
        assert off.get_json()["acceleration"]["label"] == "CPU (GPU decode is turned off)"
        assert manager.get("reprise_gpu_decode") is False
        on = client.patch("/api/reprise/settings", json={"gpu_decode": True})
        assert on.get_json()["acceleration"]["enabled"] is True
        for bad in ({}, {"gpu_decode": "no"}, {"gpu_decode": 1}, {"other": True}, {"gpu_decode": True, "other": 1}):
            assert client.patch("/api/reprise/settings", json=bad).status_code == 400
    finally:
        markers.close()
        settings_manager.reset_settings_manager()


def test_reference_clip_frames_are_decodable_by_the_bundled_ffmpeg():
    """The packaged clips the checks decode, through the real FFmpeg on the CPU: the baseline a GPU is held to."""
    ffmpeg = shutil.which("ffmpeg")
    for family, (clip, pix) in acceleration.REFERENCE.items():
        frames = acceleration._showinfo_frames(ffmpeg, acceleration.CLIPS_DIR / clip, pix, ())
        assert len(frames) == 9, family
        assert {f[1] for f in frames} == {pix} and {f[2] for f in frames} == {"1280x720"}
        assert json.dumps(frames)  # plain data, comparable across processes


@pytest.mark.gpu
@pytest.mark.real_gpu_detection
def test_real_devices_match_the_cpu_frame_for_frame_on_the_reference_clips(monkeypatch):
    """Needs a GPU passed to the test environment and an FFmpeg with its decoder (the image's): `pytest -m gpu`."""
    monkeypatch.setattr(acceleration, "enabled", REAL_ENABLED)
    detected = acceleration._detected()
    if not detected:
        pytest.skip("no GPU decode device was detected")
    ffmpeg = media.ffmpeg_binary()
    for accel, _allowed in detected:
        verdict = acceleration.verify(accel, ffmpeg)
        assert verdict.families == ALL, f"{accel.label}: {verdict.detail}"
