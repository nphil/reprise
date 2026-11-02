"""Small native FFmpeg fixtures exercise real seek and sample boundaries."""

import json
import subprocess
from fractions import Fraction

import pytest

from media_preview_generator.reprise.identity import media_version
from media_preview_generator.reprise.media import DecodeError, audio_pcm, probe, video_frames


def execute(*args):
    return subprocess.check_output(["ffmpeg", "-nostdin", "-v", "error", "-threads", "1", *args])


@pytest.fixture(params=[0, 5], ids=["zero-origin", "offset-origin"])
def native_media(tmp_path, request):
    path = tmp_path / "native.mkv"
    execute(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=64x48:rate=24:duration=6",
        "-f",
        "lavfi",
        "-i",
        "aevalsrc=0.3*sin(2*PI*(200*t+13*t*t)):s=48000:d=6",
        "-filter:v",
        "settb=1/1000,setpts=PTS+375+floor(N/2)*5",
        "-fps_mode",
        "passthrough",
        "-enc_time_base",
        "1:1000",
        "-c:v",
        "ffv1",
        "-c:a",
        "pcm_s16le",
        "-output_ts_offset",
        str(request.param),
        "-y",
        str(path),
    )
    item = media_version(str(path), 7000, json.dumps({"audio_streams": [{"plex_id": "a", "index": 1}]}))
    return item


def timing(path):
    return json.loads(
        subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_frames",
                "-show_entries",
                "format=start_time:frame=best_effort_timestamp_time",
                "-of",
                "json",
                path,
            ]
        )
    )


def test_seek_vfr_offset_overlap_and_partial_eof(native_media):
    item = native_media
    info = timing(item.canonical_path)
    origin = Fraction(info["format"]["start_time"])
    expected = [(Fraction(frame["best_effort_timestamp_time"]) - origin) * 1000 for frame in info["frames"]]
    assert len({b - a for a, b in zip(expected, expected[1:], strict=False)}) > 1
    windows = [(0, 4000), (3000, 5000), (4500, 8000), (3123, 4871)]
    decoded = []
    for start, end in windows:
        frames = video_frames(item, 0, start, end, lambda: False, lambda: False, 1)
        assert [pts for pts, _ in frames] == [pts for pts in expected if start <= pts < end]
        decoded.append(dict(frames))
    for first, second in zip(decoded[:3], decoded[1:3], strict=False):
        overlap = first.keys() & second.keys()
        assert overlap
        assert all(first[pts] == second[pts] for pts in overlap)
    with pytest.raises(DecodeError, match="Missing or non-monotonic"):
        video_frames(item, 0, 8000, 9000, lambda: False, lambda: False, 1)


def test_audio_sample_bounds_and_eof(native_media):
    item = native_media
    _, tracks, origin = probe(item, lambda: False, lambda: False)
    full = execute("-i", item.canonical_path, "-map", "0:1", "-c:a", "pcm_s32le", "-f", "s32le", "-")
    assert len(full) == 6 * 48000 * 4
    for start, end in [(0, 1000), (123, 2123), (3000, 5000), (4000, 6000)]:
        result = audio_pcm(item, tracks[0], start, end, origin, lambda: False, lambda: False, 1)
        assert result == full[start * 48 * 4 : end * 48 * 4]
    with pytest.raises(DecodeError):
        audio_pcm(item, tracks[0], 5000, 7000, origin, lambda: False, lambda: False, 1)


def test_audio_gap_is_not_silently_filled(tmp_path):
    path = tmp_path / "gap.mka"
    execute(
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=48000:duration=6",
        "-af",
        r"aselect=not(between(t\,2\,2.3))",
        "-c:a",
        "pcm_s16le",
        "-y",
        str(path),
    )
    item = media_version(str(path), 6000, "{}")
    with pytest.raises(DecodeError, match="continuous sample clock"):
        audio_pcm(item, ("a", 0, 48000, 1), 1000, 4000, Fraction(0), lambda: False, lambda: False, 1)


@pytest.mark.parametrize("codec", ["ffv1", "libx264"])
def test_native_close_pts_preserve_every_frame_and_pixel(tmp_path, codec):
    from media_preview_generator.reprise.media import native_frames

    path = tmp_path / "close-pts.mkv"
    execute(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=64x48:rate=24:duration=2",
        "-vf",
        "settb=1/1000,setpts=PTS*0.4",
        "-fps_mode",
        "passthrough",
        "-enc_time_base",
        "1:1000",
        "-c:v",
        codec,
        "-threads:v",
        "1",
        "-chroma_sample_location",
        "left",
        "-y",
        str(path),
    )
    info = timing(str(path))
    origin = Fraction(info["format"]["start_time"])
    expected = [(Fraction(frame["best_effort_timestamp_time"]) - origin) * 1000 for frame in info["frames"]]
    assert len(expected) == 48
    assert all(0 < b - a < Fraction(1000, 24) for a, b in zip(expected, expected[1:], strict=False))
    item = media_version(str(path), 2000, "{}")
    with native_frames(item, 0, 0, 2000, lambda: False, lambda: False, 1) as reader:
        frames = list(reader)
        assert reader.termination == "eof"
    assert [frame.pts_ms for frame in frames] == expected
    actual = b"".join(plane.data for frame in frames for plane in frame.planes)
    original = execute(
        "-i",
        str(path),
        "-fps_mode",
        "passthrough",
        "-enc_time_base",
        "-1",
        "-pix_fmt",
        "yuv420p",
        "-f",
        "rawvideo",
        "-",
    )
    assert actual == original


def test_native_h264_b_frames_partial_windows(tmp_path):
    from media_preview_generator.reprise.media import native_frames

    path = tmp_path / "bframes.mp4"
    execute(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=64x48:rate=24:duration=12",
        "-c:v",
        "libx264",
        "-threads:v",
        "1",
        "-g",
        "48",
        "-bf",
        "3",
        "-chroma_sample_location",
        "left",
        "-y",
        str(path),
    )
    item = media_version(str(path), 12000, "{}")
    info = json.loads(
        subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_frames",
                "-show_entries",
                "frame=pts,pict_type:stream=time_base",
                "-of",
                "json",
                str(path),
            ]
        )
    )
    assert any(frame["pict_type"] == "B" for frame in info["frames"])
    time_base = Fraction(info["streams"][0]["time_base"]) * 1000
    expected = [frame["pts"] * time_base for frame in info["frames"]]
    decoded = []
    for start, end in ((0, 8000), (4000, 12000)):
        with native_frames(item, 0, start, end, lambda: False, lambda: False, 1) as reader:
            frames = list(reader)
        expected_window = [pts for pts in expected if start <= pts < end]
        later = next((pts for pts in expected if pts >= end), None)
        if later is not None:
            expected_window.append(later)
        assert [frame.pts_ms for frame in frames] == expected_window
        decoded.append({frame.pts_ms: b"".join(plane.data for plane in frame.planes) for frame in frames})
    overlap = decoded[0].keys() & decoded[1].keys()
    assert overlap and all(decoded[0][pts] == decoded[1][pts] for pts in overlap)


@pytest.mark.parametrize(
    "pix,dx,dy,sample",
    [
        ("yuv420p", 2, 2, 1),
        ("yuv422p", 2, 1, 1),
        ("yuv444p", 1, 1, 1),
        ("yuv420p10le", 2, 2, 2),
        ("yuv422p10le", 2, 1, 2),
        ("yuv444p10le", 1, 1, 2),
    ],
)
def test_native_planes_pts_and_boundary(tmp_path, pix, dx, dy, sample):
    from media_preview_generator.reprise.media import native_frames

    path = tmp_path / "frames.mkv"
    execute(
        "-filter_threads",
        "1",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=64x48:rate=4:duration=3",
        "-pix_fmt",
        pix,
        "-c:v",
        "ffv1",
        "-chroma_sample_location",
        "left",
        "-y",
        str(path),
    )
    item = media_version(str(path), 3000, "{}")
    with native_frames(item, 0, 1000, 2000, lambda: False, lambda: False, 1) as reader:
        frames = list(reader)
        assert reader.termination == "boundary"
        assert reader.observed_end == 2000
    assert [frame.pts_ms for frame in frames] == [1000, 1250, 1500, 1750, 2000]
    frame = frames[0]
    assert frame.pixel_format == pix
    assert frame.bit_depth == (8 if sample == 1 else 10)
    assert frame.chroma_location == "left"
    assert frame.chroma_provenance == "audited-window-constant"
    assert frame.sample_aspect_ratio == Fraction(1) and frame.interpretation_flags == ()
    assert [(p.width, p.height, p.stride_bytes, p.sample_bytes) for p in frame.planes] == [
        (64, 48, 64 * sample, sample),
        (64 // dx, 48 // dy, 64 // dx * sample, sample),
        (64 // dx, 48 // dy, 64 // dx * sample, sample),
    ]
    assert all(p.data.readonly and len(p.data) == p.stride_bytes * p.height for p in frame.planes)
    expected = execute("-i", str(path), "-pix_fmt", pix, "-f", "rawvideo", "-")
    frame_size = sum(len(p.data) for p in frame.planes)
    assert b"".join(p.data for p in frame.planes) == expected[4 * frame_size : 5 * frame_size]


def test_native_eof_abandon_and_cancel(tmp_path):
    from media_preview_generator.reprise.media import Cancelled, native_frames

    path = tmp_path / "frames.mkv"
    execute(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=64x48:rate=4:duration=2",
        "-c:v",
        "ffv1",
        "-chroma_sample_location",
        "left",
        "-y",
        str(path),
    )
    item = media_version(str(path), 2000, "{}")
    with native_frames(item, 0, 1000, 3000, lambda: False, lambda: False, 1) as reader:
        assert len(list(reader)) == 4
        assert reader.termination == "eof"
    with pytest.raises(DecodeError, match="not exhausted"):
        with native_frames(item, 0, 0, 1000, lambda: False, lambda: False, 1) as reader:
            next(reader)
            proc = reader.proc
    assert proc.poll() is not None
    assert reader.termination is None
    cancelled = False
    with pytest.raises(Cancelled):
        with native_frames(item, 0, 0, 1000, lambda: cancelled, lambda: False, 1) as reader:
            next(reader)
            proc = reader.proc
            cancelled = True
            next(reader)
    assert proc.poll() is not None


def test_native_vfr_nonzero_origin_and_lookahead_cap(tmp_path):
    from media_preview_generator.reprise.media import native_frames

    path = tmp_path / "sparse.mkv"
    execute(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=64x48:rate=1:duration=7",
        "-vf",
        r"select=eq(n\,0)+eq(n\,5)",
        "-fps_mode",
        "passthrough",
        "-c:v",
        "ffv1",
        "-chroma_sample_location",
        "left",
        "-output_ts_offset",
        "5",
        "-y",
        str(path),
    )
    item = media_version(str(path), 7000, "{}")
    with native_frames(item, 0, 0, 1000, lambda: False, lambda: False, 1) as reader:
        frames = list(reader)
        assert [frame.pts_ms for frame in frames] == [0]
        assert reader.termination == "lookahead_cap"
        assert reader.observed_end is None
        assert list(reader) == []


def test_native_rejects_missing_chroma_and_reaps_on_exception(tmp_path):
    from media_preview_generator.reprise.media import native_frames

    path = tmp_path / "unknown.mkv"
    execute("-f", "lavfi", "-i", "testsrc2=size=64x48:rate=4:duration=2", "-c:v", "ffv1", "-y", str(path))
    item = media_version(str(path), 2000, "{}")
    with pytest.raises(DecodeError, match="chroma"):
        with native_frames(item, 0, 0, 1000, lambda: False, lambda: False, 1):
            pass
    path = tmp_path / "known.mkv"
    execute(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=64x48:rate=4:duration=2",
        "-c:v",
        "ffv1",
        "-chroma_sample_location",
        "left",
        "-y",
        str(path),
    )
    item = media_version(str(path), 2000, "{}")
    with pytest.raises(RuntimeError, match="consumer"):
        with native_frames(item, 0, 0, 1000, lambda: False, lambda: False, 1) as reader:
            next(reader)
            proc = reader.proc
            raise RuntimeError("consumer")
    assert proc.poll() is not None
    assert reader.termination is None


@pytest.mark.parametrize("offset", [0, 5])
def test_native_vfr_seek_preserves_exact_timestamps(tmp_path, offset):
    from media_preview_generator.reprise.media import native_frames

    path = tmp_path / "vfr.mkv"
    execute(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=64x48:rate=10:duration=6",
        "-vf",
        "settb=1/1000,setpts=PTS+floor(N/2)*20",
        "-fps_mode",
        "passthrough",
        "-enc_time_base",
        "1:1000",
        "-c:v",
        "ffv1",
        "-chroma_sample_location",
        "left",
        "-output_ts_offset",
        str(offset),
        "-y",
        str(path),
    )
    item = media_version(str(path), 7000, "{}")
    info = timing(str(path))
    origin = Fraction(info["format"]["start_time"])
    times = [(Fraction(frame["best_effort_timestamp_time"]) - origin) * 1000 for frame in info["frames"]]
    expected = [point for point in times if 3000 <= point < 5000]
    expected.append(next(point for point in times if point >= 5000))
    with native_frames(item, 0, 3000, 5000, lambda: False, lambda: False, 1) as reader:
        assert [frame.pts_ms for frame in reader] == expected
        assert reader.termination == "boundary"


def test_native_pause_and_changed_file_fail_closed(tmp_path):
    from media_preview_generator.reprise.media import native_frames

    path = tmp_path / "changing.mkv"
    execute(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=64x48:rate=4:duration=2",
        "-c:v",
        "ffv1",
        "-chroma_sample_location",
        "left",
        "-y",
        str(path),
    )
    item = media_version(str(path), 2000, "{}")
    states = iter([True, True, False])

    def paused():
        return next(states, False)

    with native_frames(item, 0, 0, 1000, lambda: False, lambda: False, 1) as reader:
        reader.pause = paused
        assert next(reader).pts_ms == 0
        assert list(reader)
        assert reader.termination == "boundary"
    with pytest.raises(DecodeError, match="Media changed"):
        with native_frames(item, 0, 0, 1000, lambda: False, lambda: False, 1) as reader:
            next(reader)
            with path.open("ab") as output:
                output.write(b"changed")
            list(reader)


@pytest.mark.parametrize("rate,layout,channels", [(48000, "stereo", 2), (44100, "stereo", 2), (48000, "5.1", 6)])
def test_native_audio_all_channels_and_fractional_boundaries(tmp_path, rate, layout, channels):
    import numpy as np

    from media_preview_generator.reprise.media import native_audio_pcm

    path = tmp_path / "channels.mka"
    expressions = "|".join(f"0.1*sin(2*PI*{300 + 75 * i}*t)" for i in range(channels))
    execute("-f", "lavfi", "-i", f"aevalsrc={expressions}:s={rate}:d=3:c={layout}", "-c:a", "flac", "-y", str(path))
    item = media_version(str(path), 3000, "{}")
    start, end = Fraction(1, 3), Fraction(4501, 3)
    result = native_audio_pcm(item, 0, start, end, lambda: False, lambda: False, 1)
    first = -(-(start * rate / 1000).numerator // (start * rate / 1000).denominator)
    last = (end * rate / 1000).numerator // (end * rate / 1000).denominator
    reference = np.frombuffer(execute("-i", str(path), "-c:a", "pcm_f64le", "-f", "f64le", "-"), dtype="<f8").reshape(
        -1, channels
    )
    assert result.data.shape == (last - first, channels)
    assert not result.data.flags.writeable
    assert np.array_equal(result.data, reference[first:last])
    assert result.sample_rate == rate and result.channel_layout == layout
    assert len(result.channel_ids) == channels
    assert result.support_start_ms == Fraction(first * 1000, rate)
    assert result.support_end_ms == Fraction(last * 1000, rate)
    assert start <= result.support_start_ms < result.support_end_ms <= end
    with pytest.raises(DecodeError, match="cover"):
        native_audio_pcm(item, 0, 2500, 4000, lambda: False, lambda: False, 1)


def test_native_audio_rejects_gap_and_unknown_layout(tmp_path):
    from media_preview_generator.reprise.media import native_audio_pcm

    path = tmp_path / "gap.mka"
    execute(
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=48000:duration=3",
        "-af",
        r"aselect=not(between(t\,1\,1.3))",
        "-c:a",
        "flac",
        "-y",
        str(path),
    )
    item = media_version(str(path), 3000, "{}")
    with pytest.raises(DecodeError, match="discontinuous"):
        native_audio_pcm(item, 0, 0, 2500, lambda: False, lambda: False, 1)
    path = tmp_path / "unknown.mka"
    execute(
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2", "-c:a", "pcm_s16le", "-y", str(path)
    )
    item = media_version(str(path), 2000, "{}")
    with pytest.raises(DecodeError, match="layout"):
        native_audio_pcm(item, 0, 0, 1000, lambda: False, lambda: False, 1)


def test_native_audio_preserves_opposite_phase_channels(tmp_path):
    import numpy as np

    from media_preview_generator.reprise.media import native_audio_pcm

    results = []
    for amplitude in ("0.1", "0.2"):
        path = tmp_path / f"{amplitude}.mka"
        signal = f"trunc({amplitude}*sin(2*PI*440*t)*32768)/32768"
        execute(
            "-f", "lavfi", "-i", f"aevalsrc={signal}|-{signal}:s=48000:d=2:c=stereo", "-c:a", "flac", "-y", str(path)
        )
        item = media_version(str(path), 2000, "{}")
        results.append(native_audio_pcm(item, 0, 0, 1000, lambda: False, lambda: False, 1).data)
    assert np.array_equal(results[0].sum(axis=1), results[1].sum(axis=1))
    assert not np.array_equal(results[0], results[1])


def test_native_uses_unrotated_pixels(tmp_path):
    from media_preview_generator.reprise.media import native_frames

    original, rotated = tmp_path / "original.mp4", tmp_path / "rotated.mp4"
    execute(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=64x48:rate=4:duration=2",
        "-c:v",
        "libx264",
        "-threads",
        "1",
        "-chroma_sample_location",
        "left",
        "-y",
        str(original),
    )
    execute("-i", str(original), "-c", "copy", "-metadata:s:v:0", "rotate=180", "-y", str(rotated))
    item = media_version(str(rotated), 2000, "{}")
    with native_frames(item, 0, 0, 1000, lambda: False, lambda: False, 1) as reader:
        frames = list(reader)
    actual = b"".join(p.data for p in frames[0].planes)
    unrotated = execute(
        "-noautorotate", "-i", str(rotated), "-frames:v", "1", "-pix_fmt", "yuv420p", "-f", "rawvideo", "-"
    )
    assert actual == unrotated
    assert frames[0].interpretation_flags == ("display_matrix",)


@pytest.mark.parametrize("fault", ["missing-frame-fields", "zero-exit-error", "warning", "tail"])
def test_native_protocol_failure_never_commits(tmp_path, monkeypatch, fault):
    import signal

    from media_preview_generator.reprise import media

    path = tmp_path / "fault.mkv"
    execute(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=320x180:rate=4:duration=2",
        "-c:v",
        "ffv1",
        "-chroma_sample_location",
        "left",
        "-y",
        str(path),
    )
    item = media_version(str(path), 2000, "{}")
    original = media._bounded_log_lines

    def injected(pipe):
        inserted = False
        for line in original(pipe):
            if fault == "missing-frame-fields" and not inserted and b"Parsed_showinfo_2" in line and b" n:" in line:
                yield b"[Parsed_showinfo_2 @ 0x1] [info] pts:0 broken"
                inserted = True
            elif fault == "warning" and not inserted and b"Parsed_showinfo_2" in line and b" n:" in line:
                yield b"[decoder @ 0x1] [warning] injected diagnostic"
                inserted = True
            else:
                yield line
        if fault == "zero-exit-error":
            yield b"[error] injected diagnostic after successful decoder exit"
        elif fault == "tail":
            raise DecodeError("injected truncated final record")

    monkeypatch.setattr(media, "_bounded_log_lines", injected)
    old = signal.getsignal(signal.SIGALRM)

    def timeout_handler(signum, frame):
        raise TimeoutError("native decoder protocol stalled")

    signal.signal(signal.SIGALRM, timeout_handler)
    signal.alarm(8)
    try:
        with pytest.raises(DecodeError, match="metadata failed"):
            with media.native_frames(item, 0, 0, 1000, lambda: False, lambda: False, 1) as reader:
                proc = reader.proc
                list(reader)
        assert proc.poll() is not None
        assert reader.termination is None
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


def test_bounded_diagnostic_line_and_audit_stderr(tmp_path):
    import sys

    from media_preview_generator.reprise.media import _bounded_log_lines, run

    command = [sys.executable, "-c", "import sys; sys.stderr.write('x'*70000+'\\n')"]
    proc = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        with pytest.raises(DecodeError, match="line exceeds"):
            list(_bounded_log_lines(proc.stderr))
    finally:
        proc.kill()
        proc.wait()
        proc.stderr.close()
    with pytest.raises(DecodeError):
        run(command, lambda: False, lambda: False, reject_stderr=True)


def test_missing_complete_metadata_pair_times_out_and_reaps(tmp_path, monkeypatch):
    import sys
    import time

    from media_preview_generator.reprise import media

    path = tmp_path / "omitted.mkv"
    execute(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=320x180:rate=4:duration=2",
        "-c:v",
        "ffv1",
        "-chroma_sample_location",
        "left",
        "-y",
        str(path),
    )
    item = media_version(str(path), 2000, "{}")
    real_popen = media.subprocess.Popen
    lines = (
        "[Parsed_showinfo_0 @ 0x1] [info] config in time_base: 1/1000, frame_rate: 4/1\n"
        "[Parsed_showinfo_2 @ 0x1] [info] config in time_base: 1/1000, frame_rate: 4/1\n"
        "[Parsed_showinfo_0 @ 0x1] [info] n:0 pts:0 fmt:yuv420p sar:1/1 s:320x180 i:P plane_checksum:[0 0 0]\n"
        "[Parsed_showinfo_0 @ 0x1] [info] color_range:unknown color_space:unknown color_primaries:unknown color_trc:unknown\n"
    )
    script = f"import os,time; os.write(2,{lines.encode()!r}); os.write(1,b'x'*1048576); time.sleep(10)"

    def replacement(command, *args, **kwargs):
        if command[0] == "ffmpeg" and "-vf" in command:
            command = [sys.executable, "-u", "-c", script]
        return real_popen(command, *args, **kwargs)

    monkeypatch.setattr(media.subprocess, "Popen", replacement)
    monkeypatch.setattr(media, "_NATIVE_PROTOCOL_TIMEOUT", 0.3)
    before = time.monotonic()
    with pytest.raises(DecodeError, match="metadata stalled"):
        with media.native_frames(item, 0, 0, 1000, lambda: False, lambda: False, 1) as reader:
            proc = reader.proc
            list(reader)
    assert time.monotonic() - before < 8
    assert proc.poll() is not None and not reader.worker.is_alive()


def test_metadata_error_interrupts_partial_payload(tmp_path, monkeypatch):
    import sys
    import time

    from media_preview_generator.reprise import media

    path = tmp_path / "partial.mkv"
    execute(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=64x48:rate=4:duration=2",
        "-c:v",
        "ffv1",
        "-chroma_sample_location",
        "left",
        "-y",
        str(path),
    )
    item = media_version(str(path), 2000, "{}")
    real_popen = media.subprocess.Popen
    config = "config in time_base: 1/1000, frame_rate: 4/1"
    frame = "n:0 pts:0 pts_time:0 fmt:yuv420p sar:1/1 s:64x48 i:P plane_checksum:[0 0 0]"
    color = "color_range:unknown color_space:unknown color_primaries:unknown color_trc:unknown"
    lines = "".join(
        f"[Parsed_showinfo_{i} @ 0x1] [info] {text}\n"
        for i, text in ((0, config), (2, config), (0, frame), (0, color), (2, frame), (2, color))
    )
    script = (
        f"import os,time; os.write(2,{lines.encode()!r}); os.write(1,b'x'); "
        "time.sleep(0.2); os.write(2,b'[error] partial frame failure\\n'); time.sleep(10)"
    )

    def replacement(command, *args, **kwargs):
        if command[0] == "ffmpeg" and "-vf" in command:
            command = [sys.executable, "-u", "-c", script]
        return real_popen(command, *args, **kwargs)

    monkeypatch.setattr(media.subprocess, "Popen", replacement)
    before = time.monotonic()
    with pytest.raises(DecodeError, match="metadata failed"):
        with media.native_frames(item, 0, 0, 1000, lambda: False, lambda: False, 1) as reader:
            proc = reader.proc
            next(reader)
    assert time.monotonic() - before < 8
    assert proc.poll() is not None and not reader.worker.is_alive()


@pytest.mark.parametrize("extra", [True, False])
def test_stderr_eof_does_not_stall_decoder_cleanup(tmp_path, monkeypatch, extra):
    import sys
    import time

    from media_preview_generator.reprise import media

    path = tmp_path / "early-eof.mkv"
    execute(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=64x48:rate=4:duration=2",
        "-c:v",
        "ffv1",
        "-chroma_sample_location",
        "left",
        "-y",
        str(path),
    )
    item = media_version(str(path), 2000, "{}")
    real_popen = media.subprocess.Popen
    script = "import os,time; os.close(2); " + ("os.write(1,b'x'*1048576); " if extra else "") + "time.sleep(10)"

    def replacement(command, *args, **kwargs):
        if command[0] == "ffmpeg" and "-vf" in command:
            command = [sys.executable, "-u", "-c", script]
        return real_popen(command, *args, **kwargs)

    monkeypatch.setattr(media.subprocess, "Popen", replacement)
    monkeypatch.setattr(media, "_NATIVE_PROTOCOL_TIMEOUT", 0.3)
    before = time.monotonic()
    with pytest.raises(DecodeError, match="extra data|did not terminate"):
        with media.native_frames(item, 0, 0, 1000, lambda: False, lambda: False, 1) as reader:
            proc = reader.proc
            list(reader)
    assert time.monotonic() - before < 8
    assert proc.poll() is not None and not reader.worker.is_alive()


def test_native_rejects_actual_interlaced_frames(tmp_path):
    from media_preview_generator.reprise.media import native_frames

    path = tmp_path / "interlaced.mkv"
    execute(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=64x48:rate=8:duration=2",
        "-vf",
        "tinterlace=mode=interleave_top",
        "-c:v",
        "ffv1",
        "-chroma_sample_location",
        "left",
        "-y",
        str(path),
    )
    item = media_version(str(path), 2000, "{}")
    with pytest.raises(DecodeError, match="interpretation"):
        with native_frames(item, 0, 0, 1000, lambda: False, lambda: False, 1):
            pass


def test_native_audio_rejects_nonfinite_samples(tmp_path):
    from media_preview_generator.reprise.media import native_audio_pcm

    path = tmp_path / "nonfinite.mov"
    execute(
        "-f",
        "lavfi",
        "-i",
        r"aevalsrc=if(eq(n\,10)\,NAN\,0)|0|0|0|0|0:s=48000:d=1:c=5.1",
        "-c:a",
        "pcm_f64le",
        "-y",
        str(path),
    )
    item = media_version(str(path), 1000, "{}")
    with pytest.raises(DecodeError, match="nonfinite"):
        native_audio_pcm(item, 0, 0, 1000, lambda: False, lambda: False, 1)
