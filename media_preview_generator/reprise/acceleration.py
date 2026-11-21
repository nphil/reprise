"""Optional GPU decoding for native analysis frames; the CPU stays the fallback and the reference.

Only the video decoder inside the native frame reader's own FFmpeg command moves to the GPU (NVDEC through
``-hwaccel cuda``, VA-API through ``-hwaccel vaapi``). FFmpeg downloads every decoded surface to system memory and the
reader converts it, by plane copy, to the stream's native pixel format *before* ``showinfo`` takes its plane checksums,
so each GPU frame passes the same timestamp, format and checksum protocol as a CPU frame. Audio, the ffprobe audit and
every comparison remain on the CPU.

This does not change a verdict because the output is the same samples. H.264 and HEVC decoding is normative, and that
is checked rather than assumed: before a device is used for a codec family it decodes the packaged reference clips, and
every frame's timestamp, format, colour declarations and plane checksums must equal the CPU's. A device that differs
is not used for that family. A decode that fails at any point is repeated on the CPU (``media.native_frames``); a file
the CPU cannot decode fails exactly as it did before.

With a CUDA GPU the frame can also be reduced there (``reduction``): ``scale_cuda`` takes it to the canonical grid in
16-bit arithmetic that loses nothing, so FFmpeg downloads the grid's cells, an eighth of the bytes, and the CPU never
sums a native sample. That is held to the same standard: a device's reduced decode of the reference clips must equal
the CPU's reduction of the whole frames, cell for cell, before it is used, and every reduced frame is checked against
FFmpeg's own checksum of what it wrote.
"""

from __future__ import annotations

import contextlib
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from loguru import logger

from ..gpu.ffmpeg_capabilities import ffmpeg_binary
from ..processing.hwaccel import hwaccel_decode_args
from . import reduction

SETTING = "reprise_gpu_decode"
CLIPS_DIR = Path(__file__).resolve().parents[1] / "markers" / "credits" / "reference_clips"
# Codec family -> (packaged clip, its pixel format). The clips are 1280x720: 8-bit H.264 High and 10-bit HEVC Main 10,
# with B-frames, so a family is verified on the decoder paths real episodes use.
REFERENCE = {"h264": ("h264-8bit.mkv", "yuv420p"), "hevc": ("hevc-10bit.mkv", "yuv420p10le")}
# What the hardware decoders take and the packaged clips represent: 4:2:0 only. Anything else, and every other codec
# (MPEG-2, MPEG-4, VC-1 and VP9 are not verified), decodes on the CPU.
_FAMILIES = {
    "h264": ({"Constrained Baseline", "Main", "High", "Constrained High"}, {"yuv420p"}),
    "hevc": ({"Main", "Main 10"}, {"yuv420p", "yuv420p10le"}),
}
MIN_SIDE = 144
MAX_SIDE = 4096
CHECK_TIMEOUT_S = 30.0
# A device whose check found nothing usable is checked again after this long (a busy or just-started GPU); a device
# that passed is trusted for the life of the process.
RETRY_AFTER_S = 600.0
# This many consecutive GPU decodes that only the next device or the CPU could finish suspend the device for a while,
# so a GPU without memory for a decoder stops costing a failed start per window.
FAILURES_BEFORE_SUSPEND = 5
SUSPEND_S = 600.0
# The suffix of a family's withdrawn entry that stops only the reduction, not the decode.
REDUCE = "+reduce"
# Reductions of one family on one device that only a plain decode could finish, in a row, before the reduction is
# withdrawn until the app restarts (a transient failure is forgotten by the next reduction that succeeds).
REDUCTION_FAILURES_BEFORE_WITHDRAW = 3
# Video memory the GPU comparison may hold at once, in MiB (the owner's setting), and how much of the card's free
# memory it always leaves to the other programs sharing the card.
VRAM_SETTING = "reprise_gpu_vram_cap_mb"
DEFAULT_VRAM_CAP_MB = 2048
VRAM_CAP_RANGE_MB = (256, 16384)
VRAM_MARGIN_BYTES = 1024**3

_FRAME = re.compile(
    r"^\[Parsed_showinfo_\d+\s+@[^]]+\]\s+\[info\]\s+n:\s*\d+\s+pts:\s*(-?\d+)\b.*?\bfmt:(\S+)"
    r".*?\bs:(\d+x\d+)\s+i:(\S+).*?plane_checksum:\[([^]]+)\]"
)
_COLOR = re.compile(
    r"^\[Parsed_showinfo_\d+\s+@[^]]+\]\s+\[info\]\s+(color_range:\S+ color_space:\S+ color_primaries:\S+ color_trc:\S+)"
)
_DIAGNOSTIC = re.compile(r"^(?:\[[^\]\r\n]*\]\s*)?\[(?:warning|error|fatal|panic)\]")


@dataclass(frozen=True)
class Accelerator:
    """One usable GPU decode device, as GPU detection found and tested it."""

    kind: str  # "cuda" (NVDEC) or "vaapi"
    device: str  # "cuda:0", "/dev/dri/renderD129"
    vendor: str
    name: str
    args: tuple[str, ...]  # input options that put the decoder on this device

    @property
    def method(self) -> str:
        return "NVDEC" if self.kind == "cuda" else "VAAPI"

    @property
    def title(self) -> str:
        """A short display name: lspci's "Intel Corporation CoffeeLake-S GT2 [UHD Graphics 630] (rev 02)" reads as
        "Intel UHD Graphics 630"; nvidia-smi's "Tesla P40" gains its vendor."""
        vendor = {"NVIDIA": "NVIDIA", "INTEL": "Intel", "AMD": "AMD"}.get(self.vendor, self.vendor.title())
        name = re.sub(r"\s*\(rev [0-9a-f]+\)\s*$", "", self.name.strip())
        brackets = re.findall(r"\[([^\]]+)\]", name)
        name = brackets[-1] if brackets else re.sub(r"\b(?:Corporation|Corp\.?|Inc\.?)\s*", "", name).strip()
        return name if name.lower().startswith(vendor.lower()) else f"{vendor} {name}".strip()

    @property
    def label(self) -> str:
        return f"{self.title} ({self.method} decode)"


@dataclass(frozen=True)
class Verdict:
    """What one device's check found: the codec families it decodes exactly as the CPU does, and those of them whose
    reduced decode also equals the CPU's reduction."""

    families: frozenset[str]
    detail: str
    at: float
    reduced: frozenset[str] = frozenset()


class _CheckFailed(Exception):
    pass


def _now() -> float:
    return time.monotonic()


_lock = threading.RLock()
_gates: dict[tuple[str, str], threading.Lock] = {}
_verdicts: dict[tuple[str, str], Verdict] = {}
_baselines: dict[tuple[str, str], list[tuple[Any, ...]]] = {}
_withdrawn: set[tuple[str, str, str]] = set()  # (ffmpeg, device, family) a decode disagreed on: not used again
_failures: dict[str, int] = {}
_suspended_until: dict[str, float] = {}
_counts = {"gpu": 0, "cpu": 0, "fallbacks": 0, "reduced": 0}
_reduction_failures: dict[tuple[str, str], int] = {}
_last_problem: str | None = None
_logged: set[tuple[str, str]] = set()
_warm_thread: threading.Thread | None = None


def enabled() -> bool:
    """The owner's "use the GPU when available" switch; on unless explicitly turned off."""
    from ..web.settings_manager import peek_settings_manager

    manager = peek_settings_manager()
    return manager is None or manager.get(SETTING, True) is not False


def set_enabled(value: bool) -> None:
    from ..web.settings_manager import get_settings_manager

    get_settings_manager().set(SETTING, bool(value))


def vram_cap_mb() -> int:
    """The most video memory, in MiB, the GPU comparison may hold at once (the owner's setting)."""
    from ..web.settings_manager import peek_settings_manager

    manager = peek_settings_manager()
    value = manager.get(VRAM_SETTING, DEFAULT_VRAM_CAP_MB) if manager is not None else DEFAULT_VRAM_CAP_MB
    if isinstance(value, bool) or not isinstance(value, int):
        value = DEFAULT_VRAM_CAP_MB
    return max(VRAM_CAP_RANGE_MB[0], min(VRAM_CAP_RANGE_MB[1], value))


def set_vram_cap_mb(value: int) -> None:
    from ..web.settings_manager import get_settings_manager

    get_settings_manager().set(VRAM_SETTING, max(VRAM_CAP_RANGE_MB[0], min(VRAM_CAP_RANGE_MB[1], int(value))))


def _detected() -> list[tuple[Accelerator, bool]]:
    """Every GPU that passed detection's own decode test, with whether the GPU settings still allow it."""
    from ..web.routes._helpers import _ensure_gpu_cache
    from ..web.settings_manager import peek_settings_manager

    manager = peek_settings_manager()
    configured = {e["device"]: e for e in (manager.gpu_config if manager else []) if e.get("device")}
    found = []
    for gpu in _ensure_gpu_cache():
        device = gpu.get("device") or gpu.get("device_path") or ""
        if gpu.get("status") == "failed" or not device:
            continue
        decode = hwaccel_decode_args(gpu.get("type"), device, keep_on_gpu=False)
        kind = (
            {"cuda": "cuda", "vaapi": "vaapi"}.get(decode.args[1])
            if decode.active and decode.args[:1] == ("-hwaccel",)
            else None
        )
        if kind is None:
            continue
        entry = configured.get(device)
        allowed = entry is None or (entry.get("enabled", True) is not False and entry.get("workers", 1) > 0)
        found.append(
            (Accelerator(kind, device, str(gpu.get("type")), str(gpu.get("name") or ""), decode.args), allowed)
        )
    # NVDEC first: it needs no CPU for the decode itself and decodes faster than an iGPU.
    return sorted(found, key=lambda pair: (pair[0].kind != "cuda", pair[0].device))


def _run(command: list[str]) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            command, stdin=subprocess.DEVNULL, capture_output=True, timeout=CHECK_TIMEOUT_S, start_new_session=True
        )
    except subprocess.TimeoutExpired as exc:
        raise _CheckFailed(f"timed out after {CHECK_TIMEOUT_S:.0f} s") from exc
    except OSError as exc:
        raise _CheckFailed(f"cannot run FFmpeg: {exc}") from exc


def _parse_showinfo(done: subprocess.CompletedProcess[bytes]) -> list[tuple[Any, ...]]:
    frames: list[tuple[Any, ...]] = []
    for line in done.stderr.decode("utf-8", "replace").splitlines():
        if _DIAGNOSTIC.match(line):
            raise _CheckFailed(line.strip()[:200])
        frame = _FRAME.match(line)
        if frame:
            frames.append(frame.groups())
            continue
        color = _COLOR.match(line)
        if color and frames:
            frames[-1] = (*frames[-1], color.group(1))
    if done.returncode or not frames:
        raise _CheckFailed(f"FFmpeg exited {done.returncode} after {len(frames)} frames")
    return frames


def _showinfo_frames(ffmpeg: str, clip: Path, pix: str, args: tuple[str, ...]) -> list[tuple[Any, ...]]:
    """Every frame's timestamp, format, size, interlacing, colour declarations and plane checksums."""
    # The reader's own decode options; ``format`` first on a hardware decode, as the reader builds it.
    graph = ("format=" + pix + "," if args else "") + "showinfo"
    command = [
        ffmpeg, "-nostdin", "-nostats", "-hide_banner", "-loglevel", "level+info", "-xerror", "-err_detect", "explode",
        "-threads", "1", *args, "-i", str(clip), "-map", "0:v:0", "-an", "-sn", "-dn", "-vf", graph,
        "-fps_mode", "passthrough", "-f", "null", "-",
    ]  # fmt: skip
    return _parse_showinfo(_run(command))


def _baseline(ffmpeg: str, family: str) -> list[tuple[Any, ...]]:
    key = (ffmpeg, family)
    with _lock:
        cached = _baselines.get(key)
    if cached is None:
        clip, pix = REFERENCE[family]
        path = CLIPS_DIR / clip
        if not path.is_file():
            raise _CheckFailed(f"reference clip {clip} is missing")
        cached = _showinfo_frames(ffmpeg, path, pix, ())
        with _lock:
            _baselines[key] = cached
    return cached


def _whole_frames(ffmpeg: str, clip: Path, pix: str, count: int, size: tuple[int, int]) -> list[tuple[Any, ...]]:
    """The clip decoded on the CPU, every frame as its three planes (2-D arrays)."""
    width, height = size
    command = [
        ffmpeg, "-nostdin", "-nostats", "-hide_banner", "-loglevel", "error", "-xerror", "-err_detect", "explode",
        "-threads", "1", "-i", str(clip), "-map", "0:v:0", "-an", "-sn", "-dn", "-fps_mode", "passthrough",
        "-pix_fmt", pix, "-f", "rawvideo", "-",
    ]  # fmt: skip
    done = _run(command)
    luma = width * height
    dtype = np.dtype("u1" if reduction.DEPTHS[pix] == 8 else "<u2")
    if done.returncode or len(done.stdout) != count * luma * 3 // 2 * dtype.itemsize:
        raise _CheckFailed("the CPU's decode of the reference clip has an unexpected size")
    samples = np.frombuffer(done.stdout, dtype=dtype).reshape(count, luma * 3 // 2)
    return [
        (
            frame[:luma].reshape(height, width),
            frame[luma : luma * 5 // 4].reshape(height // 2, width // 2),
            frame[luma * 5 // 4 :].reshape(height // 2, width // 2),
        )
        for frame in samples
    ]


def _checks(text: str) -> tuple[int, ...]:
    return tuple(int(word, 16) for word in text.split())


def _check_reduced(accel: Accelerator, ffmpeg: str, family: str) -> None:
    """The device's reduced decode of the family's clip must equal the CPU's reduction of its whole frames.

    The GPU runs the reader's own reduced graph on the clip's 1280x720 frames (a grid of 2, 4 and 8 samples to the
    cell in turn: every size a stream reduces at) and writes the reduced frames out; the CPU decodes the clip whole
    and reduces it in numpy. Timestamps, colour declarations, FFmpeg's plane checksums and every cell must match."""
    clip, pix = REFERENCE[family]
    baseline = _baseline(ffmpeg, family)
    width, height = (int(side) for side in baseline[0][2].split("x"))
    wholes = _whole_frames(ffmpeg, CLIPS_DIR / clip, pix, len(baseline), (width, height))
    for cell in reduction.CELL_SIDES:
        plan = reduction.plan_with_cell(pix, width, height, cell)
        if plan is None:
            raise _CheckFailed(f"the reference clip does not fit {cell} x {cell} sample cells")
        command = [
            ffmpeg, "-nostdin", "-nostats", "-hide_banner", "-loglevel", "level+info", "-xerror", "-err_detect",
            "explode", "-threads", "1", *accel.args, "-hwaccel_output_format", "cuda", "-i", str(CLIPS_DIR / clip),
            "-map", "0:v:0", "-an", "-sn", "-dn", "-vf", ",".join(plan.filters) + ",showinfo", "-fps_mode",
            "passthrough", "-pix_fmt", plan.pix, "-f", "rawvideo", "-",
        ]  # fmt: skip
        done = _run(command)
        shown = _parse_showinfo(done)
        if len(shown) != len(baseline) or len(done.stdout) != len(shown) * plan.size:
            raise _CheckFailed(f"{len(shown)} reduced frames of {len(baseline)} at {cell} x {cell} samples")
        size = f"{plan.region.columns}x{plan.region.rows}"
        for number, (frame, base) in enumerate(zip(shown, baseline, strict=True)):
            payload = done.stdout[number * plan.size : (number + 1) * plan.size]
            try:
                cells = reduction.frame_cells(payload, plan, False)
            except ValueError as exc:
                raise _CheckFailed(f"{cell} x {cell} samples: {exc}") from exc
            expected = reduction.reduce_planes(wholes[number], plan)
            if not all(np.array_equal(a, b) for a, b in zip(cells[:3], expected[:3], strict=True)):
                raise _CheckFailed(f"reduced frames at {cell} x {cell} samples differ from the CPU's")
            reported = (frame[0], frame[1], frame[2], frame[3], _checks(frame[4]), *frame[5:])
            wanted = (base[0], plan.pix, size, base[3], reduction.payload_checksums(cells), *base[5:])
            if reported != wanted:
                raise _CheckFailed(f"FFmpeg's record of reduced frame {number} at {cell} x {cell} samples differs")


def _check(accel: Accelerator, ffmpeg: str) -> Verdict:
    families: set[str] = set()
    reduced: set[str] = set()
    problems: list[str] = []
    for family, (clip, pix) in REFERENCE.items():
        try:
            expected = _baseline(ffmpeg, family)
            actual = _showinfo_frames(ffmpeg, CLIPS_DIR / clip, pix, accel.args)
        except _CheckFailed as exc:
            problems.append(f"{family}: {exc}")
            continue
        if actual != expected:
            problems.append(f"{family}: decoded frames differ from the CPU's")
            continue
        families.add(family)
        if accel.kind == "cuda" and reduction_enabled():
            try:
                _check_reduced(accel, ffmpeg, family)
            except _CheckFailed as exc:
                problems.append(f"{family} reduction: {exc}")
            else:
                reduced.add(family)
    return Verdict(frozenset(families), "; ".join(problems), _now(), frozenset(reduced))


def _announce(accel: Accelerator, verdict: Verdict) -> None:
    with _lock:
        if (accel.device, verdict.detail) in _logged:
            return
        _logged.add((accel.device, verdict.detail))
    if verdict.families:
        logger.info(
            "Reprise decodes on the GPU: {} matched the CPU frame for frame on the reference clips ({}). "
            "Streams of other codecs decode on the CPU.{}{}",
            accel.label,
            ", ".join(sorted(verdict.families)),
            f" It also reduces frames to the comparison grid on the GPU ({', '.join(sorted(verdict.reduced))})."
            if verdict.reduced
            else "",
            f" Not used for: {verdict.detail}." if verdict.detail else "",
        )
    else:
        logger.warning(
            "Reprise will not decode on the GPU {}: {}. Analysis continues on the CPU.", accel.label, verdict.detail
        )


def verify(accel: Accelerator, ffmpeg: str) -> Verdict:
    """The device's check, run once (a failed one again after RETRY_AFTER_S); concurrent callers wait for it."""
    key = (ffmpeg, accel.device)

    def current() -> Verdict | None:
        with _lock:
            verdict = _verdicts.get(key)
        fresh = verdict is not None and (verdict.families or _now() - verdict.at < RETRY_AFTER_S)
        return verdict if fresh else None

    verdict = current()
    if verdict is None:
        with _lock:
            gate = _gates.setdefault(key, threading.Lock())
        with gate:
            verdict = current()
            if verdict is None:
                try:
                    verdict = _check(accel, ffmpeg)
                except Exception as exc:  # a check that cannot run is a device that cannot be trusted yet
                    logger.debug("GPU decode check raised", exc_info=True)
                    detail = f"the check could not run ({type(exc).__name__}: {exc})"
                    verdict = Verdict(frozenset(), detail, _now())
                with _lock:
                    _verdicts[key] = verdict
                _announce(accel, verdict)
    return verdict


def _families(accel: Accelerator, ffmpeg: str, verdict: Verdict) -> frozenset[str]:
    with _lock:
        return frozenset(f for f in verdict.families if (ffmpeg, accel.device, f) not in _withdrawn)


def _suspended(accel: Accelerator) -> bool:
    with _lock:
        until = _suspended_until.get(accel.device)
        if until is not None and _now() >= until:
            del _suspended_until[accel.device]
            _failures[accel.device] = 0
            return False
        return until is not None


def plan(ffmpeg: str) -> list[Accelerator]:
    """The usable devices, best first; the reader takes the first one verified for the stream. Empty: the CPU alone.

    The first call for a device runs its check (a few seconds); later calls are lookups.
    """
    if not enabled():
        return []
    ready = []
    for accel, allowed in _detected():
        if allowed and not _suspended(accel) and _families(accel, ffmpeg, verify(accel, ffmpeg)):
            ready.append(accel)
    return ready


_slot = threading.local()


@contextlib.contextmanager
def pinned(slot: int):
    """Searches that run side by side each hold one slot; with REPRISE_SPREAD_DECODERS=1 ``order`` then starts each
    with a different device (a P40's decoder and an Intel iGPU's at once instead of everything on the P40's)."""
    previous = getattr(_slot, "value", None)
    _slot.value = slot
    try:
        yield
    finally:
        _slot.value = previous


def order(devices: list[Accelerator]) -> list[Accelerator]:
    """``plan``'s devices, rotated so this thread's slot picks the first one tried. Which device decodes never changes
    what is decoded (each is parity-checked per codec family, and a failed decode repeats on the CPU), only where."""
    slot = getattr(_slot, "value", None)
    if slot is None or len(devices) < 2 or os.environ.get("REPRISE_SPREAD_DECODERS") != "1":
        return devices
    start = slot % len(devices)
    return devices[start:] + devices[:start]


def warm(ffmpeg: str | None = None) -> None:
    """Run the devices' checks in the background so the first analysis does not wait for them."""
    global _warm_thread
    with _lock:
        if _warm_thread is not None and _warm_thread.is_alive():
            return

        def run() -> None:
            try:
                plan(ffmpeg or ffmpeg_binary())
            except Exception:
                logger.debug("GPU decode check did not complete", exc_info=True)

        _warm_thread = threading.Thread(target=run, name="reprise-gpu-check", daemon=True)
        _warm_thread.start()


def family_of(codec: str | None, profile: str | None, pix: str, width: int, height: int) -> str | None:
    """The verified codec family a stream belongs to, or None when only the CPU decodes it."""
    rule = _FAMILIES.get(codec or "")
    if rule is None or profile not in rule[0] or pix not in rule[1]:
        return None
    return codec if MIN_SIDE <= width <= MAX_SIDE and MIN_SIDE <= height <= MAX_SIDE else None


def eligible(
    accel: Accelerator, ffmpeg: str, codec: str | None, profile: str | None, pix: str, width: int, height: int
) -> bool:
    family = family_of(codec, profile, pix, width, height)
    if family is None:
        return False
    with _lock:
        verdict = _verdicts.get((ffmpeg, accel.device))
    return verdict is not None and family in _families(accel, ffmpeg, verdict)


def reduction_enabled() -> bool:
    """The kill switch of the GPU reduction (REPRISE_GPU_REDUCE=0); the GPU decode itself stays as it is."""
    return os.environ.get("REPRISE_GPU_REDUCE", "1") != "0"


def _reducing(accel: Accelerator, ffmpeg: str, family: str) -> bool:
    with _lock:
        return (ffmpeg, accel.device, family + REDUCE) not in _withdrawn


def reduction_plan(
    accel: Accelerator,
    ffmpeg: str,
    codec: str | None,
    profile: str | None,
    pix: str,
    width: int,
    height: int,
    sample_aspect_ratio: Any,
    region: Any = None,
) -> reduction.Plan | None:
    """How this stream's frames are reduced to the canonical grid on ``accel`` (``reduction.Plan``), or None: the
    stream must decode on the device, the device must have passed the reduction check for its codec family and not
    lost it since, and the frames must be whole cells of 2, 4 or 8 samples (``region`` the whole frame, or None)."""
    if accel.kind != "cuda" or not reduction_enabled():
        return None
    family = family_of(codec, profile, pix, width, height)
    if family is None:
        return None
    with _lock:
        verdict = _verdicts.get((ffmpeg, accel.device))
    if (
        verdict is None
        or family not in _families(accel, ffmpeg, verdict)
        or family not in verdict.reduced
        or not _reducing(accel, ffmpeg, family)
    ):
        return None
    return reduction.plan_for(pix, width, height, sample_aspect_ratio, region)


def withdraw(accel: Accelerator, ffmpeg: str, family: str | None, reason: str) -> None:
    """Stop using a device for a codec family after its frames differed from the CPU's."""
    if family is None:
        return
    with _lock:
        _withdrawn.add((ffmpeg, accel.device, family))
    logger.error(
        "Reprise stopped decoding {} on {}: {}. Streams like it now decode on the CPU until the app restarts.",
        family,
        accel.label,
        reason,
    )
    _problem(f"{accel.label}: {reason}")


def withdraw_reduced(accel: Accelerator, ffmpeg: str, family: str | None, reason: str) -> None:
    """Stop reducing a codec family's frames on a device (its decode is a separate matter, see ``withdraw``)."""
    if family is None:
        return
    with _lock:
        _withdrawn.add((ffmpeg, accel.device, family + REDUCE))
    logger.error(
        "Reprise stopped reducing {} frames on {}: {}. Streams like it now come to the CPU whole until the app "
        "restarts.",
        family,
        accel.label,
        reason,
    )
    _problem(f"{accel.label}: GPU reduction stopped: {reason}")


def _problem(text: str) -> None:
    global _last_problem
    with _lock:
        _last_problem = text[:300]


def record_cpu() -> None:
    with _lock:
        _counts["cpu"] += 1


def record_gpu(accel: Accelerator, family: str | None, reduced: bool = False) -> None:
    with _lock:
        _counts["gpu"] += 1
        _failures[accel.device] = 0
        if reduced:
            _counts["reduced"] += 1
            _reduction_failures.pop((accel.device, family or ""), None)
        first = (accel.device, f"decode:{family}") not in _logged
        _logged.add((accel.device, f"decode:{family}"))
    if first:
        logger.info("Reprise is decoding {} streams on the GPU: {}", family or "native", accel.label)


def record_fallback(accel: Accelerator, reason: str) -> None:
    """A decode on this device failed and the next device or the CPU finished it: the device's fault, not the file's."""
    with _lock:
        _counts["fallbacks"] += 1
        _failures[accel.device] = _failures.get(accel.device, 0) + 1
        suspend = _failures[accel.device] >= FAILURES_BEFORE_SUSPEND
        if suspend:
            _suspended_until[accel.device] = _now() + SUSPEND_S
    _problem(f"{accel.label}: {reason}")
    if suspend:
        logger.warning(
            "Reprise paused GPU decode on {} for {:.0f} minutes after {} failed decodes in a row; the CPU decodes "
            "meanwhile. Last error: {}",
            accel.label,
            SUSPEND_S / 60,
            FAILURES_BEFORE_SUSPEND,
            reason,
        )


def record_reduction_failure(accel: Accelerator, ffmpeg: str, family: str | None, reason: str) -> None:
    """A reduced decode on this device failed and a plain decode finished the window. The device's decode is not at
    fault, only its reduction, which is withdrawn after a few failures in a row."""
    with _lock:
        key = (accel.device, family or "")
        _reduction_failures[key] = _reduction_failures.get(key, 0) + 1
        give_up = _reduction_failures[key] >= REDUCTION_FAILURES_BEFORE_WITHDRAW
    _problem(f"{accel.label}: GPU reduction failed: {reason}")
    if give_up:
        reason = f"{REDUCTION_FAILURES_BEFORE_WITHDRAW} failed reductions in a row ({reason})"
        withdraw_reduced(accel, ffmpeg, family, reason)


# The GPU comparison of frame pairs (``cuda_compare``). One analysis holds a lease for its whole run, and its
# reference searches share the session the first of them opens; the last lease out closes it, so no video memory is
# held between analyses.
_compare_lock = threading.RLock()
_compare_holders = 0
_compare_session: Any = None
_compare_tried = False  # this lease already tried to open a session (and did not get one)
_compare_verified = False  # the kernels passed their self-test in this process
_compare_failed: str | None = None  # the kernels disagreed with the CPU: not used again until the app restarts
_compare_counts = {"pairs": 0, "rechecked": 0, "fallbacks": 0}


def compare_enabled() -> bool:
    """Whether frame pairs may be compared on the GPU at all: the owner's GPU switch, working CUDA kernels."""
    if not enabled() or _compare_failed is not None:
        return False
    try:
        from . import cuda_compare

        return bool(cuda_compare.available())
    except Exception:  # a driver library that will not load: the CPU compares, and the settings page still opens
        return False


@contextlib.contextmanager
def compare_lease():
    """One analysis' hold on the GPU comparison. Its session (a CUDA context and its buffers) opens on the first
    ``compare_session()`` of a lease and closes, every byte of video memory returned, when the last lease ends."""
    global _compare_holders, _compare_session, _compare_tried
    with _compare_lock:
        _compare_holders += 1
    try:
        yield
    finally:
        closing = None
        with _compare_lock:
            _compare_holders -= 1
            if _compare_holders == 0:
                closing, _compare_session, _compare_tried = _compare_session, None, False
        if closing is not None:
            try:
                closing.close()
            except Exception:
                logger.debug("Closing the GPU comparison session failed", exc_info=True)


def _open_compare() -> Any:
    global _compare_verified, _compare_failed
    if not compare_enabled():
        return None
    from . import cuda_compare

    accel = next((a for a in plan(ffmpeg_binary()) if a.kind == "cuda"), None)
    if accel is None:
        return None
    index = int(accel.device.rpartition(":")[2] or 0)
    session = cuda_compare.Session(
        device=index, cap_bytes=vram_cap_mb() * 1024 * 1024, margin_bytes=VRAM_MARGIN_BYTES
    ).__enter__()
    try:
        if not _compare_verified:
            session.selftest()
            _compare_verified = True
            logger.info(
                "Reprise compares frames on the GPU: {} agreed with the CPU on the self-test (at most {} MiB of video "
                "memory, {} MiB free for it now)",
                accel.label,
                vram_cap_mb(),
                session.budget // (1024 * 1024),
            )
    except cuda_compare.GpuMismatch as exc:
        _compare_failed = str(exc)
        session.close()
        logger.error("Reprise will not compare frames on the GPU {}: {}. The CPU compares.", accel.label, exc)
        return None
    except BaseException:
        session.close()
        raise
    return session


def compare_session() -> Any:
    """The current lease's open GPU comparison session, opened on first use, or None when the GPU cannot take the work
    now (no CUDA device, too little free video memory, a failed self-test): the CPU compares then."""
    global _compare_session, _compare_tried
    with _compare_lock:
        if _compare_session is not None or _compare_tried or not _compare_holders:
            return _compare_session
        _compare_tried = True
        try:
            _compare_session = _open_compare()
        except Exception as exc:  # a GPU that cannot start is not a reason to fail an analysis
            logger.info("Reprise compares frames on the CPU this time: {}", exc)
            _problem(f"GPU comparison unavailable: {exc}")
        return _compare_session


def record_compare(pairs: int = 0, rechecked: int = 0, fallbacks: int = 0) -> None:
    """Frame pairs the GPU compared, how many of them the CPU checked again (within the band of a limit), and how many
    chunks of pairs the GPU could not do and the CPU did."""
    with _lock:
        _compare_counts["pairs"] += pairs
        _compare_counts["rechecked"] += rechecked
        _compare_counts["fallbacks"] += fallbacks


def compare_status() -> dict[str, Any]:
    with _compare_lock:
        session = _compare_session
        failed = _compare_failed
    with _lock:
        counts = dict(_compare_counts)
    return {
        "enabled": compare_enabled(),
        "running": session is not None,
        "vram_cap_mb": vram_cap_mb(),
        "budget_mb": session.budget // (1024 * 1024) if session is not None else None,
        "pairs": counts["pairs"],
        "cpu_rechecks": counts["rechecked"],
        "cpu_fallbacks": counts["fallbacks"],
        "problem": failed,
    }


def status() -> dict[str, Any]:
    """The owner-facing summary: what decodes analysis frames right now and how the decodes have gone."""
    on = enabled()
    ffmpeg = ffmpeg_binary()
    devices = []
    ready: list[Accelerator] = []
    pending = False
    detected = _detected()
    for accel, allowed in detected:
        entry: dict[str, Any] = {"name": accel.title, "method": accel.method, "device": accel.device, "decodes": []}
        with _lock:
            verdict = _verdicts.get((ffmpeg, accel.device))
        if not on or not allowed:
            entry["state"] = "off"
        elif verdict is None:
            entry["state"] = "checking"
            pending = True
        else:
            families = _families(accel, ffmpeg, verdict)
            entry["decodes"] = sorted(families)
            entry["reduces"] = sorted(f for f in families if f in verdict.reduced and _reducing(accel, ffmpeg, f))
            entry["detail"] = verdict.detail
            if not families:
                entry["state"] = "unavailable"
            elif _suspended(accel):
                entry["state"] = "paused"
            else:
                entry["state"] = "ready"
                ready.append(accel)
        devices.append(entry)
    if pending:
        warm(ffmpeg)
    if not on:
        label = "CPU (GPU decode is turned off)"
    elif ready:
        label = "GPU: " + ", then ".join(a.label for a in ready)
    elif pending:
        label = "CPU (checking GPU decode)"
    elif detected:
        label = "CPU (GPU decode unavailable)"
    else:
        label = "CPU (no GPU found)"
    with _lock:
        counts = dict(_counts)
        problem = _last_problem
    return {
        "enabled": on,
        "active": bool(ready),
        "label": label,
        "devices": devices,
        "windows": {
            "gpu": counts["gpu"],
            "cpu": counts["cpu"],
            "gpu_fallbacks": counts["fallbacks"],
            "reduced": counts["reduced"],
        },
        "compare": compare_status(),
        "last_problem": problem,
    }


def reset() -> None:
    """Forget every check and counter (tests, and a GPU re-scan)."""
    global _last_problem, _compare_failed, _compare_verified
    with _lock:
        _gates.clear()
        _verdicts.clear()
        _baselines.clear()
        _withdrawn.clear()
        _failures.clear()
        _reduction_failures.clear()
        _compare_counts.update(pairs=0, rechecked=0, fallbacks=0)
        _suspended_until.clear()
        _logged.clear()
        _counts.update(gpu=0, cpu=0, fallbacks=0, reduced=0)
        _last_problem = None
        _compare_failed = None
        _compare_verified = False
