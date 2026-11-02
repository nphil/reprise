"""Bounded, local review clips. The caller authorizes the version and audio track."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from ..gpu.ffmpeg_capabilities import ffmpeg_binary
from . import identity
from . import media as decoder
from .models import MediaVersion

MAX_PREVIEW_MS = 90_000
MAX_PREVIEW_BYTES = 256 * 1024 * 1024
ENCODE_TIMEOUT_S = 180.0
_ENCODER_VERSION = 1


@dataclass(frozen=True)
class PreviewArtifact:
    path: Path
    origin_ms: int
    duration_ms: int
    mime_type: str = "video/mp4"


def _current(media: MediaVersion) -> None:
    if not identity.is_current(media):
        raise decoder.DecodeError("Media changed since registration")


def _cancelled(cancel_check) -> None:
    if cancel_check and cancel_check():
        raise decoder.Cancelled("Preview cancelled")


def _key(media: MediaVersion, start_ms: int, end_ms: int, audio_stream_id: str, threads: int) -> str:
    fields = [
        media.id,
        media.canonical_path,
        media.size_bytes,
        media.mtime_ns,
        media.ctime_ns,
        media.device,
        media.inode,
        media.stream_signature,
        start_ms,
        end_ms,
        audio_stream_id,
        threads,
        _ENCODER_VERSION,
    ]
    return hashlib.sha256(json.dumps(fields, separators=(",", ":")).encode()).hexdigest()


def _run(command: list[str], path: Path, cancel_check, pause_check, *, size_limit: int) -> None:
    from ..markers.freeze import Freeze

    freeze = Freeze.of(pause_check)
    freeze.hold(cancel_check=cancel_check, name="preview")
    _cancelled(cancel_check)
    deadline = freeze.clock() + ENCODE_TIMEOUT_S
    with tempfile.TemporaryFile() as errors:
        proc = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errors, start_new_session=True
        )
        try:
            while proc.poll() is None:
                _cancelled(cancel_check)
                freeze.hold(proc, cancel_check=cancel_check, name="preview")
                _cancelled(cancel_check)
                if freeze.clock() >= deadline:
                    raise decoder.DecodeError("Preview encoding timed out")
                if path.stat().st_size > size_limit:
                    raise decoder.DecodeError("Preview exceeded size limit")
                time.sleep(0.1)
            _cancelled(cancel_check)
            if proc.returncode:
                errors.seek(0)
                raise decoder.DecodeError("Preview encoding failed: " + errors.read(512).decode("utf-8", "replace"))
            if path.stat().st_size == 0 or path.stat().st_size > size_limit:
                raise decoder.DecodeError("Preview output is empty or too large")
        finally:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait(timeout=5)


def _encode(command: list[str], path: Path, cancel_check, pause_check) -> None:
    _run(command, path, cancel_check, pause_check, size_limit=MAX_PREVIEW_BYTES)


def _probe(
    media: MediaVersion, cache_dir: Path, cancel_check, pause_check, ffmpeg_path
) -> tuple[int, list[tuple[str, int]]]:
    fd, name = tempfile.mkstemp(prefix=".reprise-probe-", suffix=".json", dir=cache_dir)
    os.close(fd)
    output = Path(name)
    try:
        _run(
            [decoder.paired_ffprobe(ffmpeg_path), "-v", "error", "-show_streams", "-of", "json", "-o",
             str(output), media.canonical_path],
            output,
            cancel_check,
            pause_check,
            size_limit=2 * 1024 * 1024,
        )
        info = json.loads(output.read_text())
        mapping = json.loads(media.stream_signature)["audio_streams"]
        streams = info["streams"]
        audio = {s["index"] for s in streams if s.get("codec_type") == "audio"}
        videos = [
            s["index"]
            for s in streams
            if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")
        ]
        tracks = [(str(item["plex_id"]) if item["plex_id"] is not None else "", item["index"]) for item in mapping]
        if (
            len(videos) != 1
            or not tracks
            or any(not tid or not isinstance(idx, int) or isinstance(idx, bool) for tid, idx in tracks)
            or len({tid for tid, _ in tracks}) != len(tracks)
            or len({idx for _, idx in tracks}) != len(tracks)
            or {idx for _, idx in tracks} != audio
        ):
            raise decoder.DecodeError("Ambiguous or invalid audio/video stream mapping")
        return videos[0], tracks
    except (KeyError, TypeError, ValueError) as exc:
        raise decoder.DecodeError("Invalid media stream information") from exc
    finally:
        output.unlink(missing_ok=True)


def build_preview(
    media: MediaVersion,
    *,
    start_ms: int,
    end_ms: int,
    audio_stream_id: str,
    cache_dir: Path,
    cancel_check=None,
    pause_check=None,
    ffmpeg_threads: int = 1,
    ffmpeg_path: str | None = None,
) -> PreviewArtifact:
    """Encode one selected track to a reusable H.264/AAC MP4, never altering its source.

    Times are file-relative milliseconds. The returned clip's zero corresponds to
    ``origin_ms``; playback compatibility and exact permissions remain the caller's responsibility.
    """
    if (
        isinstance(start_ms, bool)
        or isinstance(end_ms, bool)
        or not isinstance(start_ms, int)
        or not isinstance(end_ms, int)
        or not 0 <= start_ms < end_ms <= media.duration_ms
        or end_ms - start_ms > MAX_PREVIEW_MS
    ):
        raise ValueError("Invalid preview range")
    if not isinstance(audio_stream_id, str) or not audio_stream_id:
        raise ValueError("A selected audio stream is required")
    if isinstance(ffmpeg_threads, bool) or not isinstance(ffmpeg_threads, int) or not 1 <= ffmpeg_threads <= 4:
        raise ValueError("ffmpeg_threads must be between 1 and 4")
    ffmpeg_path = ffmpeg_path or ffmpeg_binary()
    cancel = cancel_check or (lambda: False)
    pause = pause_check or (lambda: False)
    _cancelled(cancel)
    _current(media)
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    video, tracks = _probe(media, cache_dir, cancel, pause, ffmpeg_path)
    matches = [track for track in tracks if track[0] == audio_stream_id]
    if len(matches) != 1:
        raise decoder.DecodeError("Selected audio stream is not mapped to this media version")
    _current(media)
    target = cache_dir / (_key(media, start_ms, end_ms, audio_stream_id, ffmpeg_threads) + ".mp4")
    try:
        cached = target.lstat()
    except FileNotFoundError:
        cached = None
    if cached is not None:
        if not stat.S_ISREG(cached.st_mode) or not 0 < cached.st_size <= MAX_PREVIEW_BYTES:
            raise decoder.DecodeError("Invalid cached preview")
        _cancelled(cancel)
        _current(media)
        return PreviewArtifact(target, start_ms, end_ms - start_ms)
    fd, name = tempfile.mkstemp(prefix=".reprise-preview-", suffix=".mp4", dir=cache_dir)
    os.close(fd)
    temp = Path(name)
    try:
        command = [
            ffmpeg_path,
            "-nostdin",
            "-v",
            "error",
            "-threads",
            str(ffmpeg_threads),
            "-i",
            media.canonical_path,
            "-ss",
            f"{start_ms / 1000:.3f}",
            "-t",
            f"{(end_ms - start_ms) / 1000:.3f}",
            "-map",
            f"0:{video}",
            "-map",
            f"0:{matches[0][1]}",
            "-sn",
            "-dn",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-threads",
            str(ffmpeg_threads),
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            "-f",
            "mp4",
            "-y",
            str(temp),
        ]
        _encode(command, temp, cancel, pause)
        _current(media)
        _cancelled(cancel)
        os.replace(temp, target)
        return PreviewArtifact(target, start_ms, end_ms - start_ms)
    finally:
        temp.unlink(missing_ok=True)
