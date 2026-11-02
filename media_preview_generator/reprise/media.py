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
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from fractions import Fraction

from .identity import is_current

MAX_OUTPUT = 128 * 1024 * 1024


class DecodeError(Exception):
    pass


class Cancelled(DecodeError):
    pass


def check_current(media):
    if not is_current(media):
        raise DecodeError("Media changed since registration")


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


def probe(media, cancel_check, pause_check):
    check_current(media)
    raw = run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", media.canonical_path],
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


def video_frames(media, stream, start, end, cancel_check, pause_check, threads):
    # With copyts, the output endpoint is on the normalized media timeline.
    # A duration limit can cut off a nonzero seek before any frames appear.
    command = [
        "ffmpeg",
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


def audio_pcm(media, track, start, end, origin, cancel_check, pause_check, threads):
    _, idx, rate, channels = track
    # Raw PCM has no timestamps; independently check the input timeline.
    interval = f"{float(origin):.6f}%{float(origin + Fraction(end, 1000)):.6f}"
    cmd = [
        "ffprobe",
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
        "ffmpeg",
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


def _native_audit(media, stream, start, end, cancel, pause, threads):
    """Verify one window-constant chroma location; this does not pair frames."""
    check_current(media)
    versions = []
    for binary in ("ffmpeg", "ffprobe"):
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
        "ffprobe",
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
                or any(
                    not isinstance(side, dict)
                    or side.get("side_data_type") != "H.26[45] User Data Unregistered SEI message"
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
    return pix, width, height, sample, cw, ch, size, chroma, sar, flags, pts, tb


def _bounded_log_lines(pipe):
    """Read diagnostics without allowing unbounded lines or total output."""
    buffer = bytearray()
    total = 0
    while True:
        chunk = os.read(pipe.fileno(), 4096)
        if not chunk:
            if buffer:
                raise DecodeError("Truncated decoder diagnostic record")
            return
        total += len(chunk)
        if total > 16 * 1024 * 1024:
            raise DecodeError("Decoder diagnostics exceed resource limit")
        buffer.extend(chunk)
        while True:
            index = buffer.find(b"\n")
            if index < 0:
                if len(buffer) > 65536:
                    raise DecodeError("Decoder diagnostic line exceeds resource limit")
                break
            if index > 65536:
                raise DecodeError("Decoder diagnostic line exceeds resource limit")
            yield bytes(buffer[:index])
            del buffer[: index + 1]


class NativeFrameReader:
    """Bounded stream; accept results only after normal exhaustion and context exit.

    Frames own immutable payloads. The first frame at/after request_end_ms is
    lookahead, not part of the half-open request. termination is boundary,
    eof or lookahead_cap only after all output was checked. Abandoning the
    iterator leaves termination unset and kills/reaps the decoder on exit.
    """

    def __init__(self, media, stream, start, end, cancel, pause, threads):
        if not (0 <= start < end) or threads < 1:
            raise DecodeError("Invalid native frame window")
        self.media, self.stream = media, stream
        self.request_start_ms, self.request_end_ms = start, end
        self.cancel, self.pause, self.threads = cancel, pause, threads
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
        ) = _native_audit(
            self.media, self.stream, self.request_start_ms, self.request_end_ms, self.cancel, self.pause, self.threads
        )
        check_current(self.media)
        cap = (self.request_end_ms + 2000) / 1000
        command = [
            "ffmpeg",
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
            f"showinfo,trim=end={cap:.3f},showinfo",
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
            for raw in _bounded_log_lines(self.proc.stderr):
                if self.stop.is_set():
                    return
                line = raw.decode("utf-8", "replace")
                if re.match(r"^(?:\[[^\]\r\n]*\]\s*)?\[(?:warning|error|fatal|panic)\]", line):
                    raise DecodeError("Native decoder reported a diagnostic")
                source = re.match(r"^\[Parsed_showinfo_([02])\s+@[^]]+\]\s+\[info\]\s?(.*)$", line)
                if not source:
                    if line.startswith("[Parsed_showinfo_"):
                        raise DecodeError("Malformed native showinfo record")
                    continue
                number, text = source.groups()
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


def native_frames(media, stream, start_ms, end_ms, cancel_check, pause_check, threads):
    return NativeFrameReader(media, stream, start_ms, end_ms, cancel_check, pause_check, threads)


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


def native_audio_pcm(media, stream_index, start_ms, end_ms, cancel_check, pause_check, threads):
    """Return all native channels on inward sample boundaries, or fail closed.

    Audit source timestamps at their native time base and check the very same
    FFmpeg decode's sample rate, channel layout, frame timing and count. A
    container timestamp can only attest continuity to its own tick precision.
    """
    import numpy as np

    start_ms, end_ms = Fraction(start_ms), Fraction(end_ms)
    if not (0 <= start_ms < end_ms) or threads < 1:
        raise DecodeError("Invalid native audio window")
    check_current(media)
    base = [
        "ffprobe",
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
    requested_samples = (end_ms - start_ms) * rate / 1000
    upper = -(-requested_samples.numerator // requested_samples.denominator)
    if upper * channels * 8 > MAX_OUTPUT:
        raise DecodeError("Native audio window exceeds resource limit")
    # Audit from the beginning: atrim's sample ordinals then refer to the
    # exact same decoded sample sequence. Read one extra second for a frame
    # that straddles the requested end; both metadata and output are bounded.
    audit_end = origin + end_ms / 1000 + 1
    interval = f"%{float(audit_end):.6f}"
    audit_cmd = base + [
        "-read_intervals",
        interval,
        "-show_frames",
        "-show_entries",
        "frame=pts,nb_samples,channels,channel_layout,decode_error_flags",
        "-of",
        "json",
        media.canonical_path,
    ]
    audit = json.loads(run(audit_cmd, cancel_check, pause_check, 16 * 1024 * 1024, reject_stderr=True))
    frames = audit.get("frames", [])
    if not isinstance(frames, list) or len(frames) > 100000:
        raise DecodeError("Native audio audit record limit exceeded or invalid")
    if not frames:
        raise DecodeError("Missing native audio timing")
    try:
        first = int(frames[0]["pts"]) * tb - origin
        if (first * rate).denominator != 1:
            raise DecodeError("Native audio start is not on the media sample grid")
        count = 0
        for frame in frames:
            pts, n = int(frame["pts"]) * tb - origin, int(frame["nb_samples"])
            if (
                n <= 0
                or int(frame["channels"]) != channels
                or frame["channel_layout"] != layout
                or int(frame.get("decode_error_flags", 0))
            ):
                raise DecodeError("Dynamic or invalid native audio format")
            # Quantization is inherent in container timestamps. The source PTS
            # must be the nearest tick to the uninterrupted native sample clock.
            if abs(pts - (first + Fraction(count, rate))) > tb / 2:
                raise DecodeError("Native audio timestamps are discontinuous")
            count += n
    except (KeyError, TypeError, ValueError) as exc:
        raise DecodeError("Missing native audio frame timing or layout") from exc
    start_fraction = (start_ms / 1000 - first) * rate
    first_sample = -(-start_fraction.numerator // start_fraction.denominator)
    end_fraction = (end_ms / 1000 - first) * rate
    last_sample = end_fraction.numerator // end_fraction.denominator
    if first_sample < 0 or last_sample <= first_sample or last_sample > count:
        raise DecodeError("Native audio does not continuously cover the window")
    expected = (last_sample - first_sample) * channels * 8
    if expected > MAX_OUTPUT:
        raise DecodeError("Native audio window exceeds resource limit")
    check_current(media)
    command = [
        "ffmpeg",
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
    data, messages = run(command, cancel_check, pause_check, expected, capture_stderr=True)
    if len(data) != expected:
        raise DecodeError("Incomplete native audio window")
    lines = messages.decode("utf-8", "replace").splitlines()
    if any(re.match(r"^(?:\[[^\]\r\n]*\]\s*)?\[(?:warning|error|fatal|panic)\]", line) for line in lines):
        raise DecodeError("Native audio decoder reported an error")
    decoded = []
    sample_format = None
    frame_declarations = None
    uniform_declarations = None
    initial_service = None
    service_count = 0
    frame_has_service = False
    audio_filter_id = None

    def finish_frame():
        nonlocal uniform_declarations
        if frame_declarations is None:
            return
        if uniform_declarations is None:
            uniform_declarations = frame_declarations
        elif frame_declarations != uniform_declarations:
            raise DecodeError("Native audio frame declarations changed")

    for line in lines:
        if "Parsed_ashowinfo_" not in line:
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
                if frame_has_service or frame_declarations or (initial_service is None and len(decoded) != 1):
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
        i = len(decoded)
        if (
            int(ordinal) != i
            or i >= len(frames)
            or int(actual_channels) != channels
            or actual_layout != layout
            or int(actual_rate) != rate
            or int(n) != int(frames[i]["nb_samples"])
        ):
            raise DecodeError("Native audio decoder format or frame count changed")
        raw_pts = int(frames[i]["pts"]) * tb - origin
        if abs(Fraction(int(pts), rate) - raw_pts) > tb / 2 + Fraction(1, rate):
            raise DecodeError("Native audio decoder timeline disagrees with audit")
        decoded.append(int(n))
    finish_frame()
    declarations = dict(uniform_declarations or {})
    if initial_service is not None:
        if service_count not in (1, len(decoded)):
            raise DecodeError("Native audio service declaration has ambiguous scope")
        scope = "audio_service_per_frame" if service_count > 1 else "audio_service_initial"
        declarations[scope] = initial_service
    if not decoded or sum(decoded) < last_sample:
        raise DecodeError("Native audio decoder did not cover the window")
    samples = np.frombuffer(data, dtype="<f8").reshape(-1, channels)
    if not np.isfinite(samples).all():
        raise DecodeError("Native audio contains nonfinite samples")
    check_current(media)
    return NativeAudio(
        samples,
        rate,
        layout,
        ids,
        (first + Fraction(first_sample, rate)) * 1000,
        (first + Fraction(last_sample, rate)) * 1000,
        tuple(sorted(declarations.items())),
    )
