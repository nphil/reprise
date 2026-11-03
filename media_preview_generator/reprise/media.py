"""Bounded, cancellable local media decoding. No media is sent off-host.

Video checksums are over complete decoded frames, not resized thumbnails. Timestamp
values come from the decoder's presentation timestamps, never frame counts/FPS.
"""

from __future__ import annotations

import json
import os
import queue
import re
import select
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from fractions import Fraction

from loguru import logger

from ..gpu.ffmpeg_capabilities import ffmpeg_binary
from ..markers.probe import ffprobe_path_for
from . import acceleration
from .identity import is_current

MAX_OUTPUT = 128 * 1024 * 1024


class DecodeError(Exception):
    pass


class Cancelled(DecodeError):
    pass


def paired_ffprobe(ffmpeg_path):
    """Require the selected FFmpeg's sibling rather than a PATH fallback."""
    ffprobe = ffprobe_path_for(ffmpeg_path)
    ffmpeg = shutil.which(ffmpeg_path) or ffmpeg_path
    if os.path.dirname(os.path.abspath(ffprobe)) != os.path.dirname(os.path.abspath(ffmpeg)):
        raise DecodeError("Selected FFmpeg has no matching sibling ffprobe")
    return ffprobe


def check_current(media):
    if not is_current(media):
        raise DecodeError("Media changed since registration")


_DIAGNOSTIC = re.compile(r"^(?:\[[^\]\r\n]*\]\s*)?\[(?:warning|error|fatal|panic)\]")
_UNSIZED_SUBTITLE = re.compile(
    r"^\[[^\]\r\n]*\]\s*\[warning\]\s*Could not find codec parameters for stream (\d+) \(Subtitle: "
)


def is_fatal_diagnostic(line, decoded_stream):
    """Whether an FFmpeg log line fails a decode: every warning and error does, except the demuxer saying it could
    not size a subtitle stream this command never decodes (a Blu-ray rip's PGS subtitles)."""
    if not _DIAGNOSTIC.match(line):
        return False
    unsized = _UNSIZED_SUBTITLE.match(line)
    return not (unsized and int(unsized.group(1)) != decoded_stream)


def run(command, cancel_check, pause_check, limit=MAX_OUTPUT, *, reject_stderr=False, capture_stderr=False):
    """Capture stdout to a bounded temporary file and drain bounded diagnostics."""
    with tempfile.TemporaryFile() as output:
        proc = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.PIPE, start_new_session=True, bufsize=0
        )
        errors = bytearray()
        failed = threading.Event()

        def drain():
            line_size = 0
            while True:
                chunk = os.read(proc.stderr.fileno(), 4096)
                if not chunk:
                    return
                if len(errors) + len(chunk) > 16 * 1024 * 1024:
                    failed.set()
                    return
                for part in chunk.splitlines(keepends=True):
                    line_size += len(part)
                    if line_size > 65536:
                        failed.set()
                        return
                    if part.endswith((b"\n", b"\r")):
                        line_size = 0
                errors.extend(chunk)
                if reject_stderr:
                    failed.set()
                    return

        reader = threading.Thread(target=drain, name="reprise-diagnostics", daemon=True)
        paused = False
        try:
            reader.start()
            while proc.poll() is None:
                if cancel_check():
                    raise Cancelled("Analysis cancelled")
                if failed.is_set():
                    raise DecodeError("Decoder diagnostics failed or exceeded resource limit")
                want_pause = bool(pause_check())
                if want_pause != paused:
                    os.killpg(proc.pid, signal.SIGSTOP if want_pause else signal.SIGCONT)
                    paused = want_pause
                if output.tell() > limit:
                    raise DecodeError("Decode output exceeded resource limit")
                time.sleep(0.05)
            reader.join()
            if cancel_check():
                raise Cancelled("Analysis cancelled")
            if failed.is_set() or proc.returncode or (reject_stderr and errors):
                raise DecodeError("Media decoder failed: " + bytes(errors[:512]).decode("utf-8", "replace"))
            output.seek(0, os.SEEK_END)
            if output.tell() > limit:
                raise DecodeError("Decode output exceeded resource limit")
            output.seek(0)
            data = output.read()
            return (data, bytes(errors)) if capture_stderr else data
        finally:
            if proc.poll() is None:
                if paused:
                    os.killpg(proc.pid, signal.SIGCONT)
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            if reader.ident is not None:
                reader.join()
            proc.stderr.close()


def probe(media, cancel_check, pause_check, *, ffmpeg_path=None):
    check_current(media)
    selected = ffmpeg_path if ffmpeg_path is not None else ffmpeg_binary()
    raw = run(
        [paired_ffprobe(selected), "-v", "error", "-show_streams", "-show_format", "-of", "json", media.canonical_path],
        cancel_check,
        pause_check,
        2 * 1024 * 1024,
    )
    info = json.loads(raw)
    signature = json.loads(media.stream_signature)
    tracks = signature.get("audio_streams")
    if not isinstance(tracks, list) or not tracks:
        raise DecodeError("No verified audio track mapping")
    audio = {s["index"]: s for s in info["streams"] if s.get("codec_type") == "audio"}
    ids, indices, result = set(), set(), []
    for item in tracks:
        raw_id, idx = item.get("plex_id"), item.get("index")
        track_id = str(raw_id) if raw_id is not None else ""
        if not track_id or not isinstance(idx, int) or idx in indices or track_id in ids or idx not in audio:
            raise DecodeError("Ambiguous or invalid audio track mapping")
        s = audio[idx]
        rate, channels = int(s.get("sample_rate", 0)), int(s.get("channels", 0))
        if not 0 < rate <= 96000 or not 0 < channels <= 8:
            raise DecodeError("Unsupported audio format")
        ids.add(track_id)
        indices.add(idx)
        result.append((track_id, idx, rate, channels))
    if indices != set(audio):
        raise DecodeError("Not every audio stream has a verified Plex mapping")
    videos = [
        s
        for s in info["streams"]
        if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")
    ]
    if len(videos) != 1:
        raise DecodeError("Expected one unambiguous video stream")
    origin = Fraction(str(info.get("format", {}).get("start_time", "0")))
    return videos[0]["index"], result, origin


def video_frames(media, stream, start, end, cancel_check, pause_check, threads, *, ffmpeg_path=None):
    # With copyts, the output endpoint is on the normalized media timeline.
    # A duration limit can cut off a nonzero seek before any frames appear.
    command = [
        ffmpeg_path if ffmpeg_path is not None else ffmpeg_binary(),
        "-nostdin",
        "-v",
        "error",
        "-threads",
        str(threads),
        "-copyts",
        "-start_at_zero",
        "-ss",
        f"{start / 1000:.3f}",
        "-i",
        media.canonical_path,
        "-to",
        f"{end / 1000:.3f}",
        "-map",
        f"0:{stream}",
        "-an",
        "-sn",
        "-dn",
        "-fps_mode",
        "passthrough",
        "-enc_time_base",
        "-1",
        "-f",
        "framemd5",
        "-",
    ]
    lines = run(command, cancel_check, pause_check, 16 * 1024 * 1024).decode("ascii").splitlines()
    tb = None
    frames = []
    for line in lines:
        if line.startswith("#tb 0:"):
            tb = Fraction(line.split(":", 1)[1].strip())
        elif not line.startswith("#") and line.strip():
            if tb is None:
                raise DecodeError("Missing video time base")
            fields = [s.strip() for s in line.split(",")]
            pts = Fraction(int(fields[2])) * tb * 1000
            frames.append((pts, fields[5]))
    if not frames or any(a[0] >= b[0] for a, b in zip(frames, frames[1:], strict=False)):
        raise DecodeError("Missing or non-monotonic video presentation timestamps")
    return frames


def audio_pcm(media, track, start, end, origin, cancel_check, pause_check, threads, *, ffmpeg_path=None):
    selected = ffmpeg_path if ffmpeg_path is not None else ffmpeg_binary()
    _, idx, rate, channels = track
    # Raw PCM has no timestamps; independently check the input timeline.
    # A start timestamp causes a seek, which can skip the first decoded frame.
    interval = f"%{float(origin + Fraction(end, 1000)):.6f}"
    cmd = [
        paired_ffprobe(selected),
        "-v",
        "error",
        "-select_streams",
        str(idx),
        "-read_intervals",
        interval,
        "-show_frames",
        "-show_entries",
        "frame=best_effort_timestamp_time,nb_samples",
        "-of",
        "json",
        media.canonical_path,
    ]
    parsed = json.loads(run(cmd, cancel_check, pause_check, 4 * 1024 * 1024))
    spans = []
    for frame in parsed.get("frames", []):
        try:
            p = Fraction(frame["best_effort_timestamp_time"]) - origin
            spans.append((p, p + Fraction(int(frame["nb_samples"]), rate)))
        except (KeyError, ValueError, ZeroDivisionError) as exc:
            raise DecodeError("Missing audio presentation timing") from exc
    cursor = Fraction(0)
    for lo, hi in spans:
        if abs(lo - cursor) > Fraction(1, 1000):
            raise DecodeError("Audio timestamps do not follow a continuous sample clock")
        cursor += hi - lo
    if not spans or cursor < Fraction(end, 1000) or (start * rate) % 1000 or (end * rate) % 1000:
        raise DecodeError("Audio timing does not continuously cover the window")
    # Sample-count trimming avoids losing/duplicating samples when container
    # timestamps have coarser precision than the audio sample clock.
    trim = f"atrim=start_sample={start * rate // 1000}:end_sample={end * rate // 1000},asetpts=PTS-STARTPTS"
    command = [
        selected,
        "-nostdin",
        "-v",
        "error",
        "-threads",
        str(threads),
        "-i",
        media.canonical_path,
        "-map",
        f"0:{idx}",
        "-vn",
        "-sn",
        "-dn",
        "-af",
        trim,
        "-c:a",
        "pcm_s32le",
        "-f",
        "s32le",
        "-",
    ]
    expected = (end - start) * rate * channels * 4 // 1000
    if expected > MAX_OUTPUT:
        raise DecodeError("Audio window exceeds resource limit")
    data = run(command, cancel_check, pause_check, min(MAX_OUTPUT, expected + rate * channels * 4))
    if len(data) != expected:
        raise DecodeError("Incomplete or discontinuous audio window")
    return data


@dataclass(frozen=True)
class NativePlane:
    data: memoryview
    width: int
    height: int
    stride_bytes: int
    sample_bytes: int


@dataclass(frozen=True)
class NativeFrame:
    # Planes retain their own immutable bytes and remain valid after advancing.
    pts_ms: Fraction
    planes: tuple[NativePlane, ...]
    width: int
    height: int
    pixel_format: str
    bit_depth: int
    color_range: str
    color_space: str
    color_primaries: str
    color_transfer: str
    chroma_location: str
    sample_aspect_ratio: Fraction
    interpretation_flags: tuple[str, ...]
    chroma_provenance: str = "audited-window-constant"


_NATIVE_FORMATS = {
    "yuv420p": (2, 2, 1),
    "yuv422p": (2, 1, 1),
    "yuv444p": (1, 1, 1),
    "yuv420p10le": (2, 2, 2),
    "yuv422p10le": (2, 1, 2),
    "yuv444p10le": (1, 1, 2),
}
_NATIVE_FRAME_LIMIT = 64 * 1024 * 1024
_NATIVE_PROTOCOL_TIMEOUT = 30.0  # Fail closed if paired decoder pipes stop making progress.


def _native_audit(media, stream, start, end, cancel, pause, threads, ffmpeg_path):
    """Verify one window-constant chroma location; this does not pair frames."""
    check_current(media)
    versions = []
    ffprobe = paired_ffprobe(ffmpeg_path)
    for binary in (ffmpeg_path, ffprobe):
        lines = run([binary, "-version"], cancel, pause, 65536, reject_stderr=True).decode("utf-8").splitlines()
        try:
            versions.append(
                (
                    lines[0].split()[2],
                    next(line for line in lines if line.startswith("configuration:")),
                    tuple(line for line in lines if line.startswith(("libavutil ", "libavcodec ", "libavformat "))),
                )
            )
        except (IndexError, StopIteration) as exc:
            raise DecodeError("Cannot verify FFmpeg build compatibility") from exc
    if versions[0] != versions[1]:
        raise DecodeError("FFmpeg and ffprobe builds differ")
    base = [
        ffprobe,
        "-v",
        "error",
        "-err_detect",
        "explode",
        "-threads",
        str(threads),
        "-select_streams",
        str(stream),
    ]
    info = json.loads(
        run(
            base + ["-show_streams", "-show_format", "-of", "json", media.canonical_path],
            cancel,
            pause,
            2 * 1024 * 1024,
            reject_stderr=True,
        )
    )
    try:
        selected = info["streams"][0]
        origin = Fraction(info["format"]["start_time"])
        tb = Fraction(selected["time_base"])
        chroma = selected.get("chroma_location")
        pix = selected["pix_fmt"]
        width, height = int(selected["width"]), int(selected["height"])
        sar = Fraction(selected["sample_aspect_ratio"].replace(":", "/"))
        stream_side_data = selected.get("side_data_list", [])
        tags = selected.get("tags", {})
        if not isinstance(stream_side_data, list) or not isinstance(tags, dict):
            raise DecodeError("Invalid stream interpretation metadata")
        if any(
            not isinstance(side, dict) or side.get("side_data_type") != "Display Matrix" for side in stream_side_data
        ):
            raise DecodeError("Unsupported stream interpretation side data")
        flags = tuple(
            sorted(
                {"display_matrix" for side in stream_side_data if side.get("side_data_type") == "Display Matrix"}
                | ({"rotation"} if "rotate" in tags else set())
            )
        )
    except (KeyError, IndexError, ValueError, ZeroDivisionError) as exc:
        raise DecodeError("Missing native video format or timing metadata") from exc
    if pix not in _NATIVE_FORMATS or chroma not in {"left", "center", "topleft", "top", "bottomleft", "bottom"}:
        raise DecodeError("Unsupported native pixel format or chroma location")
    if width <= 0 or height <= 0 or tb <= 0 or sar <= 0:
        raise DecodeError("Invalid native video dimensions or time base")
    dx, dy, sample = _NATIVE_FORMATS[pix]
    cw, ch = (width + dx - 1) // dx, (height + dy - 1) // dy
    size = (width * height + 2 * cw * ch) * sample
    if size > _NATIVE_FRAME_LIMIT:
        raise DecodeError("Native frame exceeds resource limit")
    # The start may seek to an earlier keyframe. Every selected decoded frame
    # must be covered by this independently decoded superset and be uniform.
    first = origin + Fraction(max(0, start - 2000), 1000)
    # ffprobe stops on packet timestamps, before delayed B-frames are emitted.
    # Decode additional packets beyond the output cap; membership stays exact.
    last = origin + Fraction(end + 4000, 1000)
    interval = f"{float(first):.6f}%{float(last):.6f}"
    command = base + [
        "-read_intervals",
        interval,
        "-show_frames",
        "-show_entries",
        "frame=pts,chroma_location,decode_error_flags,width,height,interlaced_frame,sample_aspect_ratio,crop_top,crop_bottom,crop_left,crop_right:frame_side_data=side_data_type",
        "-of",
        "json",
        media.canonical_path,
    ]
    parsed = json.loads(run(command, cancel, pause, 16 * 1024 * 1024, reject_stderr=True))
    if not isinstance(parsed.get("frames"), list) or len(parsed["frames"]) > 100000:
        raise DecodeError("Native video audit record limit exceeded or invalid")
    pts = set()
    try:
        for frame in parsed["frames"]:
            point = (int(frame["pts"]) * tb - origin) * 1000
            if point in pts or frame["chroma_location"] != chroma or int(frame.get("decode_error_flags", 0)):
                raise DecodeError("Ambiguous native video chroma audit")
            frame_sar = Fraction(frame["sample_aspect_ratio"].replace(":", "/"))
            side_data = frame.get("side_data_list", [])
            # Some ffprobe versions omit crop fields altogether. Reject any
            # reported crop, but do not claim omission proves uncropped input.
            if (
                frame_sar != sar
                or int(frame["interlaced_frame"]) != 0
                or (int(frame["width"]), int(frame["height"])) != (width, height)
                or any(int(frame.get(key, 0)) for key in ("crop_top", "crop_bottom", "crop_left", "crop_right"))
                or not isinstance(side_data, list)
                # FFmpeg 8 also attaches the stream display matrix to decoded
                # frames. Allow it only when the stream is already flagged;
                # consumers reject that flag rather than interpreting pixels.
                or any(
                    not isinstance(side, dict)
                    or not (
                        side.get("side_data_type") == "H.26[45] User Data Unregistered SEI message"
                        or (side.get("side_data_type") == "3x3 displaymatrix" and "display_matrix" in flags)
                    )
                    for side in side_data
                )
            ):
                raise DecodeError("Unsupported native frame interpretation metadata")
            pts.add(point)
    except (KeyError, TypeError, ValueError) as exc:
        raise DecodeError("Missing native video chroma audit metadata") from exc
    if not pts:
        raise DecodeError("Empty native video chroma audit")
    check_current(media)
    return (
        pix,
        width,
        height,
        sample,
        cw,
        ch,
        size,
        chroma,
        sar,
        flags,
        pts,
        tb,
        selected.get("codec_name"),
        selected.get("profile"),
    )


# A showinfo line carrying the hex payload of a frame's H.264/HEVC user-data SEI. Some encodes write one per frame
# (a 4.7 KB x264 SEI on every frame of one Blu-ray rip: 15 MB over a 97-s window), and the reader ignores them, so
# they do not count towards the diagnostics cap; every other line does, and every line keeps the per-line bound.
_SEI_PAYLOAD = re.compile(rb"^\[Parsed_showinfo_\d+ @ [^\]]+\] \[info\] (?:UUID=|User Data=)")


def _bounded_log_lines(pipe, total_limit=16 * 1024 * 1024, *, uncounted=None):
    """Read records with bounded lines and an optional total byte cap. Lines ``uncounted`` accepts are not charged
    to the cap (they still may not exceed the per-line bound), so a known, ignorable per-frame payload cannot trip
    it while a runaway diagnostic stream still does."""
    buffer = bytearray()
    total = 0
    while True:
        chunk = os.read(pipe.fileno(), 4096)
        if not chunk:
            if buffer:
                raise DecodeError("Truncated decoder diagnostic record")
            return
        buffer.extend(chunk)
        while True:
            index = buffer.find(b"\n")
            if index < 0:
                if len(buffer) > 65536:
                    raise DecodeError("Decoder diagnostic line exceeds resource limit")
                break
            if index > 65536:
                raise DecodeError("Decoder diagnostic line exceeds resource limit")
            line = bytes(buffer[:index])
            del buffer[: index + 1]
            if uncounted is None or not uncounted(line):
                total += index + 1
                if total_limit is not None and total > total_limit:
                    raise DecodeError("Decoder diagnostics exceed resource limit")
            yield line


class NativeFrameReader:
    """Bounded stream; accept results only after normal exhaustion and context exit.

    Frames own immutable payloads. The first frame at/after request_end_ms is
    lookahead, not part of the half-open request. termination is boundary,
    eof or lookahead_cap only after all output was checked. Abandoning the
    iterator leaves termination unset and kills/reaps the decoder on exit.
    """

    def __init__(self, media, stream, start, end, cancel, pause, threads, ffmpeg_path, accel=None, audit=None):
        if not (0 <= start < end) or threads < 1:
            raise DecodeError("Invalid native frame window")
        self.media, self.stream = media, stream
        self.request_start_ms, self.request_end_ms = start, end
        self.cancel, self.pause, self.threads = cancel, pause, threads
        self.ffmpeg_path = ffmpeg_path if ffmpeg_path is not None else ffmpeg_binary()
        self.termination = None
        self.observed_end = None
        self.proc = None
        self.worker = None
        self.stop = threading.Event()
        self.events = queue.Queue(maxsize=2)
        self.paused = False
        self.pre_count = 0
        self.pre_in_cap_count = 0
        self.post_count = 0
        self.worker_error = None
        self.reached = False
        self.previous = None
        self.color = None
        self.seen_pts = set()
        # The GPU decode to try (acceleration.Accelerator) and whether this stream actually uses it: a codec,
        # profile or size the hardware is not verified for decodes on the CPU exactly as before.
        self.accel = accel
        self.accelerated = False
        self.showinfo = {"0": "0", "2": "2"}  # FFmpeg's showinfo instance numbers -> the reader's pre/post labels
        self.audit = audit  # _native_audit's answer when a wrapper already ran it: its repeats must not run it again

    def __enter__(self):
        (
            self.pix,
            self.width,
            self.height,
            self.sample,
            self.cw,
            self.ch,
            self.size,
            self.chroma,
            self.sar,
            self.interpretation_flags,
            self.audit_pts,
            self.time_base,
            codec,
            profile,
        ) = self.audit or _native_audit(
            self.media,
            self.stream,
            self.request_start_ms,
            self.request_end_ms,
            self.cancel,
            self.pause,
            self.threads,
            self.ffmpeg_path,
        )
        check_current(self.media)
        cap = (self.request_end_ms + 2000) / 1000
        self.codec, self.profile = codec, profile
        graph = f"showinfo,trim=end={cap:.3f},showinfo"
        hardware = []
        if self.accel is not None and acceleration.eligible(
            self.accel, self.ffmpeg_path, codec, profile, self.pix, self.width, self.height
        ):
            # Same command, GPU decoder: FFmpeg downloads each surface and the native-format conversion runs ahead of
            # both showinfo taps (they number 1 and 3 then), so every GPU frame meets the CPU frame's checks.
            self.accelerated = True
            hardware = list(self.accel.args)
            graph = f"format={self.pix},{graph}"
            self.showinfo = {"1": "0", "3": "2"}
        command = [
            self.ffmpeg_path,
            "-nostdin",
            "-nostats",
            "-hide_banner",
            "-loglevel",
            "level+info",
            "-xerror",
            "-err_detect",
            "explode",
            "-threads",
            str(self.threads),
            "-copyts",
            "-start_at_zero",
            "-noautorotate",
            *hardware,
            "-ss",
            f"{self.request_start_ms / 1000:.3f}",
            "-i",
            self.media.canonical_path,
            "-map",
            f"0:{self.stream}",
            "-an",
            "-sn",
            "-dn",
            "-vf",
            graph,
            "-fps_mode",
            "passthrough",
            "-enc_time_base",
            f"{self.time_base.numerator}:{self.time_base.denominator}",
            "-pix_fmt",
            self.pix,
            "-f",
            "rawvideo",
            "-",
        ]
        self.proc = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            bufsize=0,
        )
        self.worker = threading.Thread(target=self._metadata, name="reprise-frame-metadata", daemon=True)
        try:
            self.worker.start()
        except BaseException:
            os.killpg(self.proc.pid, signal.SIGKILL)
            self.proc.wait()
            self.proc.stdout.close()
            self.proc.stderr.close()
            raise
        return self

    def _put(self, event):
        while not self.stop.is_set():
            try:
                self.events.put(event, timeout=0.05)
                return
            except queue.Full:
                continue

    def _metadata(self):
        bases = {}
        pending = {}
        side_data = {"0": False, "2": False}
        try:
            for raw in _bounded_log_lines(self.proc.stderr, uncounted=_SEI_PAYLOAD.match):
                if self.stop.is_set():
                    return
                line = raw.decode("utf-8", "replace")
                if is_fatal_diagnostic(line, self.stream):
                    raise DecodeError("Native decoder reported a diagnostic")
                source = re.match(r"^\[Parsed_showinfo_(\d+)\s+@[^]]+\]\s+\[info\]\s?(.*)$", line)
                if not source or source.group(1) not in self.showinfo:
                    if line.startswith("[Parsed_showinfo_"):
                        raise DecodeError("Malformed native showinfo record")
                    continue
                number, text = self.showinfo[source.group(1)], source.group(2)
                text = text.strip()
                if text.startswith("config in time_base:"):
                    match = re.fullmatch(r"config in time_base:\s*(\d+/\d+),\s*frame_rate:\s*\d+/\d+", text)
                    if not match or number in bases:
                        raise DecodeError("Invalid native video filter time base")
                    bases[number] = Fraction(match.group(1))
                    if bases[number] <= 0:
                        raise DecodeError("Invalid native video filter time base")
                elif text.startswith("config out time_base:"):
                    if not re.fullmatch(r"config out time_base:\s*\d+/\d+,\s*frame_rate:\s*\d+/\d+", text):
                        raise DecodeError("Invalid native video output time base")
                elif text.startswith("n:"):
                    match = re.match(
                        r"n:\s*(\d+)\s+pts:\s*(-?\d+).*?\bfmt:(\S+).*?\bsar:(\S+).*?\bs:(\d+)x(\d+)\s+i:(\S+).*?plane_checksum:\[([^]]+)\]",
                        text,
                    )
                    if not match or number in pending or number not in bases:
                        raise DecodeError("Malformed native frame metadata")
                    ordinal, pts, pix, sar, width, height, interlace, checks = match.groups()
                    if Fraction(sar) != self.sar or interlace != "P":
                        raise DecodeError("Dynamic or interlaced native video")
                    expected = self.pre_count if number == "0" else self.post_count
                    if int(ordinal) != expected or expected >= 100000:
                        raise DecodeError("Missing or excessive native frame metadata")
                    if number == "0":
                        self.pre_count += 1
                        if Fraction(int(pts)) * bases[number] * 1000 < self.request_end_ms + 2000:
                            self.pre_in_cap_count += 1
                    else:
                        if bases.get("0") != bases["2"]:
                            raise DecodeError("Native filter time bases differ")
                        self.post_count += 1
                    pending[number] = (
                        Fraction(int(pts)) * bases[number] * 1000,
                        pix,
                        int(width),
                        int(height),
                        checks.split(),
                    )
                    side_data[number] = False
                elif text.startswith("color_range:"):
                    match = re.fullmatch(
                        r"color_range:(\S+) color_space:(\S+) color_primaries:(\S+) color_trc:(\S+)", text
                    )
                    if number not in pending or not match:
                        raise DecodeError("Missing native frame color metadata")
                    if number == "2":
                        self._put((pending[number], match.groups()))
                    del pending[number]
                    side_data[number] = False
                elif number in pending and text.startswith("side data - "):
                    side_data[number] = True
                elif number in pending and side_data[number] and (text.startswith(("UUID=", "User Data=")) or not text):
                    continue
                else:
                    raise DecodeError("Unrecognized native showinfo record")
            if pending:
                raise DecodeError("Incomplete native frame metadata")
        except Exception as exc:
            self.worker_error = exc
        finally:
            self._put(None)

    def _signal(self, sig):
        try:
            os.killpg(self.proc.pid, sig)
        except ProcessLookupError:
            pass

    def _control(self):
        if self.cancel():
            raise Cancelled("Analysis cancelled")
        wanted = bool(self.pause())
        was_paused = wanted
        if self.proc.poll() is None and wanted != self.paused:
            self._signal(signal.SIGSTOP if wanted else signal.SIGCONT)
            self.paused = wanted
        while wanted:
            time.sleep(0.05)
            if self.cancel():
                raise Cancelled("Analysis cancelled")
            wanted = bool(self.pause())
        if self.paused and self.proc.poll() is None:
            self._signal(signal.SIGCONT)
            self.paused = False
        return was_paused

    def _read_frame(self):
        payload = bytearray()
        fd = self.proc.stdout.fileno()
        last_progress = time.monotonic()
        while len(payload) < self.size:
            if self._control():
                last_progress = time.monotonic()
            if self.worker_error:
                check_current(self.media)
                raise DecodeError("Native frame metadata failed") from self.worker_error
            if not select.select([fd], [], [], 0.05)[0]:
                if time.monotonic() - last_progress > _NATIVE_PROTOCOL_TIMEOUT:
                    raise DecodeError("Native frame payload stalled")
                continue
            chunk = os.read(fd, min(1024 * 1024, self.size - len(payload)))
            if not chunk:
                raise DecodeError("Truncated native frame payload")
            payload.extend(chunk)
            last_progress = time.monotonic()
        return bytes(payload)

    def _wait_for_exit(self):
        deadline = time.monotonic() + _NATIVE_PROTOCOL_TIMEOUT
        fd = self.proc.stdout.fileno()
        eof = False
        while True:
            if self._control():
                deadline = time.monotonic() + _NATIVE_PROTOCOL_TIMEOUT
            if not eof and select.select([fd], [], [], 0.05)[0]:
                if os.read(fd, 1):
                    raise DecodeError("Native decoder returned extra data")
                eof = True
            if eof and self.proc.poll() is not None:
                if self.proc.returncode:
                    raise DecodeError("Native decoder failed")
                return
            if time.monotonic() > deadline:
                raise DecodeError("Native decoder did not terminate")
            if eof:
                time.sleep(0.05)

    def __iter__(self):
        return self

    def __next__(self):
        import zlib

        if self.termination is not None:
            raise StopIteration
        stalled_since = None

        while True:
            if self._control():
                stalled_since = None
            try:
                event = self.events.get(timeout=0.05)
            except queue.Empty:
                if self.worker_error:
                    check_current(self.media)
                    raise DecodeError("Native frame metadata failed") from self.worker_error
                if stalled_since is None:
                    stalled_since = time.monotonic()
                elif time.monotonic() - stalled_since > _NATIVE_PROTOCOL_TIMEOUT:
                    raise DecodeError("Native frame metadata stalled") from None
                continue
            if event is None:
                if self.worker_error:
                    check_current(self.media)
                    raise DecodeError("Native frame metadata failed") from self.worker_error
                self._wait_for_exit()
                if self.pre_in_cap_count != self.post_count:
                    raise DecodeError("Native frame count mismatch")
                expected = {pts for pts in self.audit_pts if self.request_start_ms <= pts < self.request_end_ms + 2000}
                if self.seen_pts != expected:
                    raise DecodeError("Native frame timeline does not match chroma audit")
                check_current(self.media)
                self.termination = (
                    "boundary" if self.reached else ("lookahead_cap" if self.pre_count > self.post_count else "eof")
                )
                raise StopIteration
            (pts, pix, width, height, checks), color = event
            if (pix, width, height) != (self.pix, self.width, self.height):
                raise DecodeError("Dynamic or unsupported native video format")
            if len(checks) != 3 or pts not in self.audit_pts or (self.previous is not None and pts <= self.previous):
                raise DecodeError("Missing, unaudited or non-monotonic native frame timestamp")
            if self.color is not None and color != self.color:
                raise DecodeError("Dynamic native video color metadata")
            self.color = color
            self.previous = pts
            self.seen_pts.add(pts)
            data = self._read_frame()
            sizes = (
                self.width * self.height * self.sample,
                self.cw * self.ch * self.sample,
                self.cw * self.ch * self.sample,
            )
            planes = []
            offset = 0
            for size, width, height, checksum in zip(
                sizes, (self.width, self.cw, self.cw), (self.height, self.ch, self.ch), checks, strict=True
            ):
                view = memoryview(data)[offset : offset + size]
                if zlib.adler32(view, 0) != int(checksum, 16):
                    raise DecodeError("Native frame payload and metadata do not match")
                planes.append(NativePlane(view, width, height, width * self.sample, self.sample))
                offset += size
            if self.reached:
                continue
            if pts >= self.request_end_ms:
                self.reached = True
                self.observed_end = pts
            return NativeFrame(
                pts,
                tuple(planes),
                self.width,
                self.height,
                self.pix,
                8 if self.sample == 1 else 10,
                *color,
                self.chroma,
                self.sar,
                self.interpretation_flags,
            )

    def __exit__(self, exc_type, exc, traceback):
        self.stop.set()
        if self.proc is not None:
            if self.proc.poll() is None:
                if self.paused:
                    self._signal(signal.SIGCONT)
                self._signal(signal.SIGKILL)
            self.proc.wait()
            if self.worker is not None:
                self.worker.join()
            self.proc.stdout.close()
            self.proc.stderr.close()
        if exc_type is None:
            if self.termination is None:
                raise DecodeError("Native frame verification was not exhausted")
            check_current(self.media)
        return False


class _FallbackFrames:
    """The native frame reader on the first usable GPU, with the CPU as the repeat.

    A decode that fails on the GPU, at the start or half way, is repeated on the CPU, which also settles whose fault
    it was: a file the CPU cannot decode either fails with the CPU's error as without a GPU and costs the device
    nothing, and the window is not spent again on a second GPU first. Frames already handed to the consumer are never
    delivered twice: the repeat's first frames must equal them (timestamp and plane checksums) and are skipped, so the
    consumer sees one decode whichever decoder finished it. A repeat that disagrees fails the analysis, stops using
    the device involved for that codec, and leaves the CPU to decode the next window.
    """

    def __init__(self, media, stream, start_ms, end_ms, cancel, pause, threads, ffmpeg_path, attempts):
        self._args = (media, stream, start_ms, end_ms, cancel, pause, threads, ffmpeg_path)
        self._attempts = [*attempts, None]
        self._step = 0
        self._reader = None
        self._delivered = []  # (timestamp, plane checksums) of every frame already handed on
        self._replay = 0  # delivered frames the current reader must still reproduce
        self._failed = []  # (device, reason) of the GPU decodes that did not finish this window
        self._audit = None
        self._closed = False

    def __getattr__(self, name):
        # Only reached for names this wrapper lacks: the reader's own (proc, termination, observed_end, ...).
        reader = self.__dict__.get("_reader")
        if reader is None:
            raise AttributeError(name)
        return getattr(reader, name)

    def _open(self):
        reader = NativeFrameReader(*self._args, accel=self._attempts[self._step], audit=self._audit)
        reader.__enter__()  # a failure here is the file's (spawn) and would be the CPU's too
        self._reader = reader

    def __enter__(self):
        # The audit decodes the whole window in ffprobe once; the repeat reuses it. It also names the stream's codec,
        # so only a device verified for that codec is tried, the best one first.
        audit = self._audit = _native_audit(*self._args)
        pix, width, height, codec, profile = audit[0], audit[1], audit[2], audit[12], audit[13]
        ffmpeg = self._args[7]
        usable = [
            a
            for a in self._attempts
            if a is not None and acceleration.eligible(a, ffmpeg, codec, profile, pix, width, height)
        ]
        self._attempts = [*usable[:1], None]
        self._open()
        return self

    def __iter__(self):
        return self

    def _disagree(self, why):
        reader = self._reader
        family = acceleration.family_of(reader.codec, reader.profile, reader.pix, reader.width, reader.height)
        for accel in [a for a, _ in self._failed] + ([reader.accel] if reader.accelerated else []):
            acceleration.withdraw(accel, self._args[7], family, why)
        raise DecodeError("GPU and CPU decodes of the same window disagree: " + why)

    def _retry(self, exc):
        failed = self._attempts[self._step]
        self._failed.append((failed, str(exc)))
        reader, self._reader = self._reader, None
        reader.__exit__(type(exc), exc, exc.__traceback__)  # kills and reaps the failed decoder
        self._step += 1  # the one GPU is followed by the CPU
        logger.warning(
            "Reprise decode on {} failed ({}); repeating the window on the CPU", failed.label, str(exc)[:200]
        )
        self._replay = len(self._delivered)
        self._open()

    def __next__(self):
        import zlib

        while True:
            try:
                frame = next(self._reader)
            except StopIteration:
                if self._replay:
                    self._disagree("the repeat ended before reproducing the frames already delivered")
                raise
            except Cancelled:
                raise
            except DecodeError as exc:
                if not self._reader.accelerated:
                    raise
                self._retry(exc)
                continue
            seen = (frame.pts_ms, tuple(zlib.adler32(plane.data) for plane in frame.planes))
            if self._replay:
                if seen != self._delivered[len(self._delivered) - self._replay]:
                    self._disagree("a repeated frame differs from the one already delivered")
                self._replay -= 1
                continue
            self._delivered.append(seen)
            return frame

    def __exit__(self, exc_type, exc, traceback):
        reader = self._reader
        if reader is None or self._closed:
            return False
        self._closed = True
        result = reader.__exit__(exc_type, exc, traceback)
        if exc_type is None:
            # Finished and exhausted. Whoever completed it, the devices that failed first are at fault, not the file.
            if reader.accelerated:
                acceleration.record_gpu(
                    reader.accel,
                    acceleration.family_of(reader.codec, reader.profile, reader.pix, reader.width, reader.height),
                )
            else:
                acceleration.record_cpu()
            for accel, reason in self._failed:
                acceleration.record_fallback(accel, reason)
        return result


def native_frames(media, stream, start_ms, end_ms, cancel_check, pause_check, threads, *, ffmpeg_path=None):
    selected = ffmpeg_path if ffmpeg_path is not None else ffmpeg_binary()
    attempts = acceleration.plan(selected)
    if not attempts:
        acceleration.record_cpu()
        return NativeFrameReader(media, stream, start_ms, end_ms, cancel_check, pause_check, threads, selected)
    return _FallbackFrames(media, stream, start_ms, end_ms, cancel_check, pause_check, threads, selected, attempts)


@dataclass(frozen=True)
class NativeAudio:
    # Immutable, interleaved native channels as normalized float64 samples.
    data: object
    sample_rate: int
    channel_layout: str
    channel_ids: tuple[str, ...]
    support_start_ms: Fraction
    support_end_ms: Fraction
    declarations: tuple[tuple[str, tuple[str, ...]], ...]


_AUDIO_LAYOUTS = {
    "mono": ("FC",),
    "stereo": ("FL", "FR"),
    "2.1": ("FL", "FR", "LFE"),
    "3.0": ("FL", "FR", "FC"),
    "4.0": ("FL", "FR", "FC", "BC"),
    "quad": ("FL", "FR", "BL", "BR"),
    "5.0": ("FL", "FR", "FC", "BL", "BR"),
    "5.0(side)": ("FL", "FR", "FC", "SL", "SR"),
    "5.1": ("FL", "FR", "FC", "LFE", "BL", "BR"),
    "5.1(side)": ("FL", "FR", "FC", "LFE", "SL", "SR"),
    "7.1": ("FL", "FR", "FC", "LFE", "BL", "BR", "SL", "SR"),
}


def _native_audio_side_record(text):
    """Parse only supported ashowinfo declarations, preserving printed tokens."""
    if text == "side data - audio service type: Main Audio Service":
        return "audio_service_initial", ("Main Audio Service",)
    if text == "side data - matrix encoding: none":
        return "matrix_encoding", ("none",)
    number = r"([01]\.\d{6})"
    match = re.fullmatch(
        r"side data - downmix: preferred downmix type - (unknown) Mix levels: center "
        + number
        + r" \("
        + number
        + r" ltrt\) - surround "
        + number
        + r" \("
        + number
        + r" ltrt\) - lfe "
        + number,
        text,
    )
    if not match or any(Fraction(value) > 1 for value in match.groups()[1:]):
        raise DecodeError("Unsupported native audio interpretation metadata")
    return "downmix", match.groups()


class _AudioLines:
    """Drain a child protocol through a bounded queue; output remains file-bounded."""

    def __init__(self, command, *, decoder, output=None):
        self.stop = threading.Event()
        self.events = queue.Queue(maxsize=16)
        self.proc = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=output if decoder else subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            bufsize=0,
        )
        self.paused = False
        self.error = None
        self.threads = []
        protocol = self.proc.stderr if decoder else self.proc.stdout

        def produce():
            try:
                for line in _bounded_log_lines(protocol, None):
                    if not self._put(line):
                        return
                self._put(None)
            except Exception as exc:
                self._put(exc)

        def reject_probe_errors():
            try:
                for _line in _bounded_log_lines(self.proc.stderr):
                    self.error = DecodeError("Native audio probe reported an error")
                    self._put(DecodeError("Native audio probe reported an error"))
                    return
            except Exception as exc:
                self.error = exc
                self._put(exc)

        self.threads.append(threading.Thread(target=produce, daemon=True))
        if not decoder:
            self.threads.append(threading.Thread(target=reject_probe_errors, daemon=True))
        started = []
        try:
            for thread in self.threads:
                thread.start()
                started.append(thread)
        except BaseException:
            self.threads = started
            self.close()
            raise

    def _put(self, event):
        while not self.stop.is_set():
            try:
                self.events.put(event, timeout=0.05)
                return True
            except queue.Full:
                pass
        return False

    def next_line(self, processes, cancel, pause, output=None, limit=0):
        deadline = time.monotonic() + 30
        while True:
            if cancel():
                raise Cancelled("Analysis cancelled")
            want_pause = bool(pause())
            if want_pause:
                deadline = time.monotonic() + 30
            elif time.monotonic() > deadline:
                raise DecodeError("Native audio metadata stalled")
            for process in processes:
                if process.proc.poll() is None and want_pause != process.paused:
                    try:
                        os.killpg(process.proc.pid, signal.SIGSTOP if want_pause else signal.SIGCONT)
                    except ProcessLookupError:
                        pass
                    process.paused = want_pause
            if want_pause:
                time.sleep(0.05)
                continue
            if output is not None and os.fstat(output.fileno()).st_size > limit:
                raise DecodeError("Decode output exceeded resource limit")
            try:
                event = self.events.get(timeout=0.05)
            except queue.Empty:
                continue
            if isinstance(event, Exception):
                raise DecodeError("Native audio streaming protocol failed") from event
            return event

    def finish(self, processes, cancel, pause, output=None, limit=0):
        deadline = time.monotonic() + 30
        while True:
            if cancel():
                raise Cancelled("Analysis cancelled")
            want_pause = bool(pause())
            if not want_pause and self.proc.poll() is not None:
                break
            if want_pause:
                deadline = time.monotonic() + 30
            elif time.monotonic() > deadline:
                raise DecodeError("Native audio subprocess did not terminate")
            for process in processes:
                if process.proc.poll() is None and want_pause != process.paused:
                    try:
                        os.killpg(process.proc.pid, signal.SIGSTOP if want_pause else signal.SIGCONT)
                    except ProcessLookupError:
                        pass
                    process.paused = want_pause
            if output is not None and os.fstat(output.fileno()).st_size > limit:
                raise DecodeError("Decode output exceeded resource limit")
            time.sleep(0.05)
        for thread in self.threads:
            while thread.is_alive():
                if cancel():
                    raise Cancelled("Analysis cancelled")
                if time.monotonic() > deadline:
                    raise DecodeError("Native audio diagnostics did not terminate")
                thread.join(timeout=0.05)
        if cancel():
            raise Cancelled("Analysis cancelled")
        if self.error is not None or self.proc.returncode:
            raise DecodeError("Native audio subprocess failed") from self.error

    def close(self):
        self.stop.set()
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.proc.wait()
        for thread in self.threads:
            thread.join(timeout=1)
        if self.proc.stdout is not None:
            self.proc.stdout.close()
        self.proc.stderr.close()


@dataclass(frozen=True)
class NativeAudioChunk:
    """Provisional native PCM; accept only after the reader context succeeds."""

    data: object
    sample_rate: int
    channel_layout: str
    channel_ids: tuple[str, ...]
    support_start_ms: Fraction
    support_end_ms: Fraction


class NativeAudioChunkReader:
    """One-pass bounded PCM stream; successful full context exit commits evidence."""

    def __init__(self, media, stream_index, start_ms, end_ms, cancel, pause, threads, chunk_samples, ffmpeg_path):
        if not isinstance(chunk_samples, int) or chunk_samples <= 0 or chunk_samples > MAX_OUTPUT // 8:
            raise DecodeError("Invalid native audio chunk size")
        self.args = (media, stream_index, start_ms, end_ms, cancel, pause, threads)
        self.ffmpeg_path = ffmpeg_path if ffmpeg_path is not None else ffmpeg_binary()
        self.cancel, self.pause = cancel, pause
        self.chunk_samples = chunk_samples
        self.events = queue.Queue(maxsize=2)
        self.stop = threading.Event()
        self.output_abort = threading.Event()
        self.process_lock = threading.Lock()
        self.processes = []
        self.worker = None
        self.output_worker = None
        self.output_error = None
        self.output_count = 0
        self._declarations = None
        self.declarations = None
        self.exhausted = False
        self.exited = False

    def _put(self, value):
        while not self.stop.is_set() and (value[0] != "chunk" or not self.output_abort.is_set()):
            try:
                self.events.put(value, timeout=0.05)
                return
            except queue.Full:
                pass

    def _register(self, process):
        with self.process_lock:
            self.processes.append(process)
            if self.stop.is_set():
                try:
                    os.killpg(process.proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def __enter__(self):
        if self.worker is not None:
            raise DecodeError("Native audio stream cannot be reused")

        def work():
            try:
                result = _native_audio_stream(*self.args, self, self.ffmpeg_path)
                self._put(("done", result))
            except BaseException as exc:
                if self.output_error is not None and str(exc) in {
                    "Native audio subprocess failed",
                    "Native audio output failed",
                }:
                    exc = self.output_error
                self._put(("error", exc))

        self.worker = threading.Thread(target=work, name="reprise-audio-stream", daemon=True)
        self.worker.start()
        return self

    def __iter__(self):
        return self

    def __next__(self):
        if self.exhausted:
            raise StopIteration
        if self.worker is None or self.exited:
            raise DecodeError("Native audio stream is not active")
        while True:
            if self.cancel():
                raise Cancelled("Analysis cancelled")
            if self.pause():
                time.sleep(0.05)
                continue
            try:
                kind, value = self.events.get(timeout=0.05)
                break
            except queue.Empty:
                if not self.worker.is_alive():
                    raise DecodeError("Native audio stream ended without final verification") from None
        if kind == "error":
            raise value
        if kind == "done":
            self._declarations = value
            self.exhausted = True
            raise StopIteration
        return value

    def _start_output(self, decoder, expected, rate, layout, ids, start):
        import numpy as np

        channels = len(ids)
        chunk_bytes = self.chunk_samples * channels * 8
        if chunk_bytes > MAX_OUTPUT:
            raise DecodeError("Native audio chunk exceeds resource limit")

        def drain():
            try:
                buffer = bytearray()
                total = 0
                while True:
                    part = os.read(decoder.proc.stdout.fileno(), min(65536, expected - total + 1))
                    if not part:
                        break
                    total += len(part)
                    if total > expected:
                        raise DecodeError("Decode output exceeded resource limit")
                    buffer.extend(part)
                    while len(buffer) >= chunk_bytes:
                        payload = bytes(buffer[:chunk_bytes])
                        del buffer[:chunk_bytes]
                        samples = np.frombuffer(payload, dtype="<f8").reshape(-1, channels)
                        if not np.isfinite(samples).all():
                            raise DecodeError("Native audio contains nonfinite samples")
                        first = self.output_count
                        self.output_count += len(samples)
                        self._put(
                            (
                                "chunk",
                                NativeAudioChunk(
                                    samples,
                                    rate,
                                    layout,
                                    ids,
                                    (start + Fraction(first, rate)) * 1000,
                                    (start + Fraction(self.output_count, rate)) * 1000,
                                ),
                            )
                        )
                        if self.stop.is_set():
                            return
                if total != expected or len(buffer) % (channels * 8):
                    raise DecodeError("Native audio output does not cover the window")
                if buffer:
                    samples = np.frombuffer(bytes(buffer), dtype="<f8").reshape(-1, channels)
                    if not np.isfinite(samples).all():
                        raise DecodeError("Native audio contains nonfinite samples")
                    first = self.output_count
                    self.output_count += len(samples)
                    self._put(
                        (
                            "chunk",
                            NativeAudioChunk(
                                samples,
                                rate,
                                layout,
                                ids,
                                (start + Fraction(first, rate)) * 1000,
                                (start + Fraction(self.output_count, rate)) * 1000,
                            ),
                        )
                    )
            except BaseException as exc:
                self.output_error = exc
                try:
                    os.killpg(decoder.proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

        self.output_worker = threading.Thread(target=drain, name="reprise-audio-output", daemon=True)
        self.output_worker.start()

    def _finish_output(self, expected, channels):
        deadline = time.monotonic() + 30
        while self.output_worker.is_alive():
            if self.stop.is_set() or self.cancel():
                raise Cancelled("Analysis cancelled")
            if self.pause() or self.events.full():
                deadline = time.monotonic() + 30
            elif time.monotonic() > deadline:
                raise DecodeError("Native audio output did not terminate")
            self.output_worker.join(timeout=0.05)
        if self.output_error is not None:
            raise DecodeError("Native audio output failed") from self.output_error
        if self.output_count * channels * 8 != expected:
            raise DecodeError("Incomplete native audio window")

    def __exit__(self, exc_type, exc, tb):
        self.exited = True
        if exc_type is not None or not self.exhausted:
            self.stop.set()
            self.output_abort.set()
            with self.process_lock:
                for process in self.processes:
                    if process.proc.poll() is not None:
                        continue
                    try:
                        os.killpg(process.proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
        if self.worker is not None:
            self.worker.join()
        if exc_type is not None:
            return False
        if not self.exhausted or self.worker is None or self.worker.is_alive() or self._declarations is None:
            raise DecodeError("Native audio stream not exhausted or verified")
        while self.pause():
            if self.cancel():
                raise Cancelled("Analysis cancelled")
            time.sleep(0.05)
        if self.cancel():
            raise Cancelled("Analysis cancelled")
        check_current(self.args[0])
        if self.cancel():
            raise Cancelled("Analysis cancelled")
        self.declarations = self._declarations
        return False


def native_audio_chunks(
    media, stream_index, start_ms, end_ms, cancel_check, pause_check, threads, *, chunk_samples=480000, ffmpeg_path=None
):
    return NativeAudioChunkReader(
        media, stream_index, start_ms, end_ms, cancel_check, pause_check, threads, chunk_samples, ffmpeg_path
    )


def native_audio_pcm(media, stream_index, start_ms, end_ms, cancel_check, pause_check, threads, *, ffmpeg_path=None):
    """Collect a bounded native window after complete stream verification."""
    import numpy as np

    chunks = []
    with native_audio_chunks(
        media, stream_index, start_ms, end_ms, cancel_check, pause_check, threads, ffmpeg_path=ffmpeg_path
    ) as reader:
        size = 0
        for chunk in reader:
            size += chunk.data.nbytes
            if size > MAX_OUTPUT:
                raise DecodeError("Native audio window exceeds resource limit")
            chunks.append(chunk)
    if not chunks:
        raise DecodeError("Incomplete native audio window")
    data = chunks[0].data if len(chunks) == 1 else np.concatenate([chunk.data for chunk in chunks])
    data.setflags(write=False)
    return NativeAudio(
        data,
        chunks[0].sample_rate,
        chunks[0].channel_layout,
        chunks[0].channel_ids,
        chunks[0].support_start_ms,
        chunks[-1].support_end_ms,
        reader.declarations,
    )


def _native_audio_stream(media, stream_index, start_ms, end_ms, cancel_check, pause_check, threads, sink, ffmpeg_path):
    """Return all native channels on inward sample boundaries, or fail closed.

    Audit source timestamps at their native time base and check the very same
    FFmpeg decode's sample rate, channel layout, frame timing and count. A
    container timestamp can only attest continuity to its own tick precision.
    """
    original_cancel, original_pause = cancel_check, pause_check

    def cancel_check():
        return sink.stop.is_set() or original_cancel()

    def pause_check():
        return original_pause() or sink.events.full()

    start_ms, end_ms = Fraction(start_ms), Fraction(end_ms)
    if not (0 <= start_ms < end_ms) or threads < 1:
        raise DecodeError("Invalid native audio window")
    check_current(media)
    base = [
        paired_ffprobe(ffmpeg_path),
        "-v",
        "error",
        "-err_detect",
        "explode",
        "-threads",
        str(threads),
        "-select_streams",
        str(stream_index),
    ]
    info = json.loads(
        run(
            base + ["-show_streams", "-show_format", "-of", "json", media.canonical_path],
            cancel_check,
            pause_check,
            2 * 1024 * 1024,
            reject_stderr=True,
        )
    )
    try:
        stream = info["streams"][0]
        if stream["index"] != stream_index or stream["codec_type"] != "audio":
            raise DecodeError("Selected stream is not native audio")
        rate = int(stream["sample_rate"])
        layout = stream["channel_layout"]
        channels = int(stream["channels"])
        ids = _AUDIO_LAYOUTS[layout]
        tb = Fraction(stream["time_base"])
        origin = Fraction(info["format"]["start_time"])
    except (KeyError, IndexError, TypeError, ValueError, ZeroDivisionError) as exc:
        raise DecodeError("Missing or unsupported native audio layout or timing") from exc
    if not 0 < rate <= 192000 or len(ids) != channels or tb <= 0:
        raise DecodeError("Unsupported native audio rate or layout")
    audit_cmd = base + [
        "-read_intervals",
        f"%{float(origin + end_ms / 1000 + 1):.6f}",
        "-show_frames",
        "-show_entries",
        "frame=pts,nb_samples,channels,channel_layout,decode_error_flags:frame_side_data=",
        "-of",
        "compact=p=0",
        media.canonical_path,
    ]
    probe_stream = _AudioLines(audit_cmd, decoder=False)
    sink._register(probe_stream)
    processes = [probe_stream]
    decoder = None
    count = 0
    first = None
    separator_count = 0
    try:

        def audit_frames():
            nonlocal first, count, separator_count
            while True:
                line = probe_stream.next_line(processes, cancel_check, pause_check)
                if line is None:
                    return
                if not line:
                    separator_count += 1
                    if separator_count > 8:
                        raise DecodeError("Native audio probe protocol has excess separators")
                    continue
                separator_count = 0
                try:
                    fields = {}
                    for entry in line.decode("utf-8", "strict").rstrip("|").split("|"):
                        key, value = entry.split("=", 1)
                        if key in fields or key not in {
                            "pts",
                            "nb_samples",
                            "channels",
                            "channel_layout",
                            "decode_error_flags",
                        }:
                            raise ValueError("invalid frame field")
                        fields[key] = value
                    pts = int(fields["pts"]) * tb - origin
                    n = int(fields["nb_samples"])
                    if (
                        n <= 0
                        or int(fields["channels"]) != channels
                        or fields["channel_layout"] != layout
                        or int(fields.get("decode_error_flags", 0))
                    ):
                        raise DecodeError("Dynamic or invalid native audio format")
                    if first is None:
                        first = pts
                        if (first * rate).denominator != 1:
                            raise DecodeError("Native audio start is not on the media sample grid")
                    # Container timestamps are whole ticks while samples sit on an exact grid, and the first
                    # frame is itself a rounded observation, so a gapless stream may stray by one full tick
                    # (real MKV AC3 files alternate between 0 and -1 ms). A real gap or overlap is a whole frame.
                    if abs(pts - (first + Fraction(count, rate))) > tb:
                        raise DecodeError("Native audio timestamps are discontinuous")
                    count += n
                    yield pts, n
                except (KeyError, TypeError, ValueError, UnicodeError) as exc:
                    raise DecodeError("Missing native audio frame timing or layout") from exc

        audit = audit_frames()
        first_frame = next(audit, None)
        if first_frame is None or first is None:
            raise DecodeError("Missing native audio timing")
        start_fraction = (start_ms / 1000 - first) * rate
        first_sample = -(-start_fraction.numerator // start_fraction.denominator)
        end_fraction = (end_ms / 1000 - first) * rate
        last_sample = end_fraction.numerator // end_fraction.denominator
        if first_sample < 0 or last_sample <= first_sample:
            raise DecodeError("Native audio does not continuously cover the window")
        expected = (last_sample - first_sample) * channels * 8
        check_current(media)
        command = [
            ffmpeg_path,
            "-nostdin",
            "-nostats",
            "-hide_banner",
            "-loglevel",
            "level+info",
            "-xerror",
            "-err_detect",
            "explode",
            "-threads",
            str(threads),
            "-copyts",
            "-start_at_zero",
            "-i",
            media.canonical_path,
            "-map",
            f"0:{stream_index}",
            "-vn",
            "-sn",
            "-dn",
            "-af",
            f"asettb=1/{rate},ashowinfo,atrim=start_sample={first_sample}:end_sample={last_sample}",
            "-c:a",
            "pcm_f64le",
            "-f",
            "f64le",
            "-",
        ]
        decoder = _AudioLines(command, decoder=True, output=subprocess.PIPE)
        processes.append(decoder)
        sink._register(decoder)
        sink._start_output(decoder, expected, rate, layout, ids, first + Fraction(first_sample, rate))
        decoded = 0
        decoded_samples = 0
        sample_format = None
        frame_declarations = None
        uniform_declarations = None
        initial_service = None
        service_count = 0
        frame_has_service = False
        audio_filter_id = None
        other_bytes = 0

        def finish_frame():
            nonlocal uniform_declarations
            if frame_declarations is None:
                return
            if uniform_declarations is None:
                uniform_declarations = frame_declarations
            elif frame_declarations != uniform_declarations:
                raise DecodeError("Native audio frame declarations changed")

        while True:
            raw = decoder.next_line(processes, cancel_check, pause_check)
            if raw is None:
                break
            line = raw.decode("utf-8", "replace")
            if is_fatal_diagnostic(line, stream_index):
                raise DecodeError("Native audio decoder reported an error")
            if "Parsed_ashowinfo_" not in line:
                other_bytes += len(raw)
                if other_bytes > 16 * 1024 * 1024:
                    raise DecodeError("Native audio diagnostics exceed resource limit")
                continue
            source = re.fullmatch(r"\[Parsed_ashowinfo_(\d+)\s+@[^]]+\]\s+\[info\]\s?(.*)", line)
            if not source:
                raise DecodeError("Malformed native audio decoder metadata")
            if audio_filter_id is None:
                audio_filter_id = source.group(1)
            elif source.group(1) != audio_filter_id:
                raise DecodeError("Ambiguous native audio metadata source")
            text = source.group(2).strip()
            if text.startswith("side data - "):
                if frame_declarations is None:
                    raise DecodeError("Native audio declaration precedes its frame")
                name, value = _native_audio_side_record(text)
                if name == "audio_service_initial":
                    if frame_has_service or frame_declarations or (initial_service is None and decoded != 1):
                        raise DecodeError("Ambiguous native audio service declaration")
                    if initial_service is not None and value != initial_service:
                        raise DecodeError("Native audio service declaration changed")
                    initial_service = value
                    frame_has_service = True
                    service_count += 1
                else:
                    if name == "matrix_encoding" and "downmix" in frame_declarations:
                        raise DecodeError("Ambiguous native audio declaration order")
                    if name in frame_declarations:
                        raise DecodeError("Duplicate native audio frame declaration")
                    frame_declarations[name] = value
                continue
            finish_frame()
            frame_declarations = {}
            frame_has_service = False
            match = re.match(
                r"\bn:(\d+)\s+pts:(-?\d+).*?fmt:(\S+)\s+channels:(\d+)\s+chlayout:(\S+)\s+rate:(\d+)\s+nb_samples:(\d+)",
                text,
            )
            if not match:
                raise DecodeError("Malformed native audio decoder metadata")
            ordinal, pts, actual_format, actual_channels, actual_layout, actual_rate, n = match.groups()
            if sample_format is not None and actual_format != sample_format:
                raise DecodeError("Dynamic native audio sample format")
            sample_format = actual_format
            audited = first_frame if decoded == 0 else next(audit, None)
            if (
                audited is None
                or int(ordinal) != decoded
                or int(actual_channels) != channels
                or actual_layout != layout
                or int(actual_rate) != rate
                or int(n) != audited[1]
            ):
                raise DecodeError("Native audio decoder format or frame count changed")
            if abs(Fraction(int(pts), rate) - audited[0]) > tb / 2 + Fraction(1, rate):
                raise DecodeError("Native audio decoder timeline disagrees with audit")
            decoded += 1
            decoded_samples += int(n)
        finish_frame()
        decoder.finish(processes, cancel_check, pause_check)
        for _ in audit:
            pass
        probe_stream.finish(processes, cancel_check, pause_check)
        if not decoded or decoded_samples < last_sample or count < last_sample:
            raise DecodeError("Native audio decoder did not cover the window")
        sink._finish_output(expected, channels)
        declarations = dict(uniform_declarations or {})
        if initial_service is not None:
            if service_count not in (1, decoded):
                raise DecodeError("Native audio service declaration has ambiguous scope")
            scope = "audio_service_per_frame" if service_count > 1 else "audio_service_initial"
            declarations[scope] = initial_service
        check_current(media)
        return tuple(sorted(declarations.items()))
    except BaseException:
        sink.output_abort.set()
        raise
    finally:
        for process in reversed(processes):
            process.close()
        if sink.output_worker is not None and sink.output_worker.ident is not None:
            sink.output_worker.join(timeout=1)
