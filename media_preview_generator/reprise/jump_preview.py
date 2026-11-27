"""On-demand playback across a skip, from an already authorized open media file."""

import json
import math
import shutil
import subprocess
import tempfile
import threading
from fractions import Fraction

_CONTEXT_MS = 2000
_TIMEOUT_S = 15
_MAX_BYTES = 4 * 1024 * 1024
_SLOTS = threading.BoundedSemaphore(2)
_NICE = shutil.which("nice")
# Use only self-contained video demuxers, not playlists or other inputs that open a second path.
_CONTAINERS = "matroska,webm,mov,mp4,m4a,3gp,3g2,mj2,mpegts,mpeg,avi,asf,flv,ogg"
_SCALE = (
    "scale=w='max(2,trunc(min(640,360*dar)/2)*2)':h='max(2,trunc(min(360,640/dar)/2)*2)',"
    "setsar=1,pad=640:360:(ow-iw)/2:(oh-ih)/2,format=yuv420p"
)


class JumpPreviewBusy(RuntimeError):
    """There is no encoder slot free; don't queue work on an HTTP thread."""


def encode(file, version, start_ms, end_ms, ffmpeg):
    """Return H.264/AAC MP4 bytes with footage outside, never inside, the requested skip."""
    duration = version.duration_ms
    if not 0 <= start_ms < end_ms or start_ms >= duration:
        raise ValueError("Invalid skip range")
    end_ms = min(duration, end_ms)
    windows = [(max(0, start_ms - _CONTEXT_MS), start_ms, False), (end_ms, min(duration, end_ms + _CONTEXT_MS), True)]
    windows = [(start, stop, after) for start, stop, after in windows if start < stop]
    if not windows:
        raise FileNotFoundError("There is no footage outside this skip")

    streams = json.loads(version.stream_signature).get("streams", [])
    video = next(
        (s for s in streams if s["codec_type"] == "video" and not s.get("disposition", {}).get("attached_pic")), None
    )
    if video is None:
        raise FileNotFoundError("There is no video stream to preview")
    audio = [s for s in streams if s["codec_type"] == "audio"]
    track = next((s for s in audio if s.get("disposition", {}).get("default")), audio[0] if audio else None)
    # An audio packet's timestamp can be rounded by the container (e.g. Matroska's 1 ms clock). Leave one
    # tick at the cut so we never play samples that could actually be on the skipped side.
    selected = (track["index"], max(1, math.ceil(1000 * Fraction(track["time_base"])))) if track else None

    if not _SLOTS.acquire(blocking=False):
        raise JumpPreviewBusy
    try:
        with tempfile.TemporaryFile() as output:
            _render(file.fileno(), output.fileno(), windows, video["index"], selected, ffmpeg)
            data = output.read(_MAX_BYTES + 1)
        if not 0 < len(data) < _MAX_BYTES:
            raise OSError("Jump preview output is empty or too large")
        return data
    finally:
        _SLOTS.release()


def _render(source_fd, output_fd, windows, video, audio, ffmpeg):
    command = [
        *([_NICE, "-n", "10"] if _NICE else []),
        ffmpeg,
        "-nostdin",
        "-v",
        "error",
        "-filter_complex_threads",
        "1",
    ]
    inputs, filters = [], []
    for i, (start, stop, after) in enumerate(windows):
        length_ms = stop - start
        length = f"{length_ms / 1000:.3f}"
        command += [
            "-threads",
            "1",
            "-ss",
            f"{start / 1000:.3f}",
            "-t",
            length,
            "-protocol_whitelist",
            "file",
            "-format_whitelist",
            _CONTAINERS,
            "-i",
            f"/proc/self/fd/{source_fd}",
        ]
        # Discard out-of-window source frames before resampling, then cap each side again: a frame's display
        # time can straddle the right edge, which would push the next segment late and cut off its last frame.
        filters.append(
            f"[{i}:{video}]trim=start=0:end={length},fps=24:start_time=0:round=down:eof_action=pass,"
            f"trim=end={length},{_SCALE}[v{i}]"
        )
        inputs.append(f"[v{i}]")
        if audio is not None:
            index, guard_ms = audio
            left = min(length_ms, guard_ms) if after else 0
            right = length_ms if after else max(0, length_ms - guard_ms)
            filters.append(f"[{i}:{index}]atrim=start={left / 1000:.3f}:end={right / 1000:.3f}[a{i}]")
            inputs.append(f"[a{i}]")

    # Also works for one window: no filler is needed when the skip touches the start or the end of the file.
    outputs = "[v][a]" if audio is not None else "[v]"
    filters.append(f"{''.join(inputs)}concat=n={len(windows)}:v=1:a={int(audio is not None)}{outputs}")
    command += ["-filter_complex", ";".join(filters), "-map", "[v]"]
    if audio is not None:
        command += ["-map", "[a]", "-c:a", "aac", "-b:a", "128k", "-ac", "2"]
    command += [
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
        "-abort_on",
        "empty_output_stream",
        "-fps_mode",
        "passthrough",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-threads",
        "1",
        "-maxrate",
        "1800k",
        "-bufsize",
        "900k",
        "-t",
        str(2 * _CONTEXT_MS // 1000),
        "-fs",
        str(_MAX_BYTES),
        "-movflags",
        "+faststart",
        "-f",
        "mp4",
        "-y",
        f"/proc/self/fd/{output_fd}",
    ]
    try:
        subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_TIMEOUT_S,
            pass_fds=(source_fd, output_fd),
            check=True,
        )
    except subprocess.SubprocessError as exc:
        raise OSError("Reprise could not make a jump preview") from exc
