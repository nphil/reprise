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

from loguru import logger

from ..gpu.ffmpeg_capabilities import ffmpeg_binary
from ..processing.hwaccel import hwaccel_decode_args

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
    def cuda_index(self) -> int | None:
        """The CUDA device index of an NVIDIA GPU ("cuda:1" is 1), None for any other kind."""
        if self.kind != "cuda":
            return None
        index = self.device.partition(":")[2]
        return int(index) if index.isdigit() else None

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
    """What one device's check found: the codec families it decodes exactly as the CPU does."""

    families: frozenset[str]
    detail: str
    at: float


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
_counts = {"gpu": 0, "cpu": 0, "fallbacks": 0}
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


def _showinfo_frames(ffmpeg: str, clip: Path, pix: str, args: tuple[str, ...]) -> list[tuple[Any, ...]]:
    """Every frame's timestamp, format, size, interlacing, colour declarations and plane checksums."""
    # The reader's own decode options; ``format`` first on a hardware decode, as the reader builds it.
    graph = ("format=" + pix + "," if args else "") + "showinfo"
    command = [
        ffmpeg, "-nostdin", "-nostats", "-hide_banner", "-loglevel", "level+info", "-xerror", "-err_detect", "explode",
        "-threads", "1", *args, "-i", str(clip), "-map", "0:v:0", "-an", "-sn", "-dn", "-vf", graph,
        "-fps_mode", "passthrough", "-f", "null", "-",
    ]  # fmt: skip
    try:
        done = subprocess.run(
            command, stdin=subprocess.DEVNULL, capture_output=True, timeout=CHECK_TIMEOUT_S, start_new_session=True
        )
    except subprocess.TimeoutExpired as exc:
        raise _CheckFailed(f"timed out after {CHECK_TIMEOUT_S:.0f} s") from exc
    except OSError as exc:
        raise _CheckFailed(f"cannot run FFmpeg: {exc}") from exc
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


def _check(accel: Accelerator, ffmpeg: str) -> Verdict:
    families: set[str] = set()
    problems: list[str] = []
    for family, (clip, pix) in REFERENCE.items():
        try:
            expected = _baseline(ffmpeg, family)
            actual = _showinfo_frames(ffmpeg, CLIPS_DIR / clip, pix, accel.args)
        except _CheckFailed as exc:
            problems.append(f"{family}: {exc}")
            continue
        if actual == expected:
            families.add(family)
        else:
            problems.append(f"{family}: decoded frames differ from the CPU's")
    return Verdict(frozenset(families), "; ".join(problems), _now())


def _announce(accel: Accelerator, verdict: Verdict) -> None:
    with _lock:
        if (accel.device, verdict.detail) in _logged:
            return
        _logged.add((accel.device, verdict.detail))
    if verdict.families:
        logger.info(
            "Reprise decodes on the GPU: {} matched the CPU frame for frame on the reference clips ({}). "
            "Streams of other codecs decode on the CPU.{}",
            accel.label,
            ", ".join(sorted(verdict.families)),
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


def _problem(text: str) -> None:
    global _last_problem
    with _lock:
        _last_problem = text[:300]


def record_cpu() -> None:
    with _lock:
        _counts["cpu"] += 1


def record_gpu(accel: Accelerator, family: str | None) -> None:
    with _lock:
        _counts["gpu"] += 1
        _failures[accel.device] = 0
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
        "windows": {"gpu": counts["gpu"], "cpu": counts["cpu"], "gpu_fallbacks": counts["fallbacks"]},
        "last_problem": problem,
    }


def reset() -> None:
    """Forget every check and counter (tests, and a GPU re-scan)."""
    global _last_problem
    with _lock:
        _gates.clear()
        _verdicts.clear()
        _baselines.clear()
        _withdrawn.clear()
        _failures.clear()
        _suspended_until.clear()
        _logged.clear()
        _counts.update(gpu=0, cpu=0, fallbacks=0)
        _last_problem = None
