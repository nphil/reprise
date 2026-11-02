"""Small native FFmpeg fixtures exercise real seek and sample boundaries."""

import json
import subprocess
from fractions import Fraction

import pytest

from media_preview_generator.reprise.identity import media_version
from media_preview_generator.reprise.media import DecodeError, audio_pcm, probe, video_frames


def execute(*args):
    return subprocess.check_output(["ffmpeg", "-nostdin", "-v", "error", "-threads", "1", *args])


def assert_generated_video_format(path, pix="yuv420p"):
    info = json.loads(
        subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_streams",
                "-show_frames",
                "-show_entries",
                "stream=pix_fmt,chroma_location:frame=pix_fmt,chroma_location",
                "-of",
                "json",
                str(path),
            ]
        )
    )
    streams, frames = info.get("streams", []), info.get("frames", [])
    actual_streams = [(s.get("pix_fmt"), s.get("chroma_location")) for s in streams]
    actual_frames = {(f.get("pix_fmt"), f.get("chroma_location")) for f in frames}
    assert actual_streams == [(pix, "left")] and frames and actual_frames == {(pix, "left")}, (
        f"generated video fixture has unexpected pixel format/chroma: expected ({pix}, left), "
        f"streams={actual_streams}, frame_values={actual_frames}, frame_count={len(frames)}"
    )


def test_generated_video_fixture_precondition_reports_actual_format(tmp_path):
    path = tmp_path / "fixture.mkv"
    execute(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=64x48:rate=2:duration=1",
        "-c:v",
        "ffv1",
        "-chroma_sample_location",
        "left",
        "-y",
        str(path),
    )
    assert_generated_video_format(path)
    with pytest.raises(AssertionError, match="yuv422p.*yuv420p"):
        assert_generated_video_format(path, "yuv422p")


def assert_generated_audio_gap(path):
    info = json.loads(
        subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_frames",
                "-show_entries",
                "frame=pts_time,nb_samples",
                "-of",
                "json",
                str(path),
            ]
        )
    )
    frames = info["frames"]
    gaps = [
        Fraction(b["pts_time"]) - Fraction(a["pts_time"]) - Fraction(int(a["nb_samples"]), 48000)
        for a, b in zip(frames, frames[1:], strict=False)
    ]
    assert gaps and max(gaps) > Fraction(1, 10), f"generated audio gap missing: max={max(gaps, default=None)}"


@pytest.mark.parametrize("explicit", [False, True])
def test_selected_ffmpeg_and_sibling_probe_are_used_for_all_media_reads(
    tmp_path, monkeypatch, explicit, ac3_native_audio
):
    import shutil

    from media_preview_generator.reprise import media

    path = tmp_path / "paired.mkv"
    execute(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=64x48:rate=4:duration=2",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=400:sample_rate=48000:duration=2",
        "-c:v",
        "libx264",
        "-threads",
        "1",
        "-chroma_sample_location",
        "left",
        "-c:a",
        "pcm_s16le",
        "-y",
        str(path),
    )
    item = media_version(str(path), 2000, json.dumps({"audio_streams": [{"plex_id": "a", "index": 1}]}))
    video_path = tmp_path / "video.mp4"
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
        str(video_path),
    )
    native_video = media_version(str(video_path), 2000, "{}")
    bindir = tmp_path / "selected"
    bindir.mkdir()
    ffmpeg = bindir / "ffmpeg"
    ffprobe = bindir / "ffprobe"
    ffmpeg.symlink_to(shutil.which("ffmpeg"))
    ffprobe.symlink_to(shutil.which("ffprobe"))
    monkeypatch.setattr(media, "ffmpeg_binary", lambda: str(tmp_path / "wrong" / "ffmpeg") if explicit else str(ffmpeg))
    launched = []
    real_popen = subprocess.Popen

    def popen(command, *args, **kwargs):
        launched.append(command[0])
        return real_popen(command, *args, **kwargs)

    monkeypatch.setattr(media.subprocess, "Popen", popen)
    kw = {"ffmpeg_path": str(ffmpeg)} if explicit else {}

    def no():
        return False

    video, tracks, origin = media.probe(item, no, no, **kw)
    assert media.video_frames(item, video, 0, 1000, no, no, 1, **kw)
    assert media.audio_pcm(item, tracks[0], 0, 1000, origin, no, no, 1, **kw)
    with media.native_frames(native_video, 0, 0, 1000, no, no, 1, **kw) as reader:
        assert list(reader)
    with media.native_audio_chunks(ac3_native_audio, 0, 0, 1000, no, no, 1, **kw) as reader:
        assert list(reader)
    assert str(ffmpeg) in launched and str(ffprobe) in launched
    assert set(launched) == {str(ffmpeg), str(ffprobe)}


def test_selected_ffmpeg_without_sibling_probe_fails_closed(tmp_path):
    import shutil

    from media_preview_generator.reprise import media

    selected = tmp_path / "ffmpeg"
    selected.symlink_to(shutil.which("ffmpeg"))
    assert shutil.which("ffprobe")
    with pytest.raises(DecodeError, match="no matching sibling"):
        media.paired_ffprobe(str(selected))


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
        "asetpts='PTS+if(gte(T,2),0.2/TB,0)'",
        "-c:a",
        "pcm_s16le",
        "-y",
        str(path),
    )
    assert_generated_audio_gap(path)
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
    assert_generated_video_format(path)
    item = media_version(str(path), 2000, "{}")
    with native_frames(item, 0, 0, 2000, lambda: False, lambda: False, 1) as reader:
        args = reader.proc.args
        assert args[args.index("-enc_time_base") + 1] == "1:1000"
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
        "1:1000",
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
    assert_generated_video_format(path)
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
    assert_generated_video_format(path, pix)
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
    assert_generated_video_format(path)
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
    assert_generated_video_format(path)
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
    assert_generated_video_format(path)
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
    assert_generated_video_format(path)
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
    assert_generated_video_format(path)
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


@pytest.mark.parametrize("offset", [0, 5])
def test_streamed_native_audio_global_grid_with_container_origin(tmp_path, offset):
    import numpy as np

    from media_preview_generator.reprise.media import native_audio_pcm

    path = tmp_path / "offset.mka"
    execute(
        "-f",
        "lavfi",
        "-i",
        "aevalsrc=0.1*sin(2*PI*377*t)|0.1*sin(2*PI*613*t):s=48000:d=3:c=stereo",
        "-c:a",
        "flac",
        "-output_ts_offset",
        str(offset),
        "-y",
        str(path),
    )
    item = media_version(str(path), 3000, "{}")
    result = native_audio_pcm(item, 0, 1000, 2000, lambda: False, lambda: False, 1)
    reference = np.frombuffer(execute("-i", str(path), "-c:a", "pcm_f64le", "-f", "f64le", "-"), dtype="<f8").reshape(
        -1, 2
    )
    assert result.support_start_ms == 1000 and result.support_end_ms == 2000
    assert np.array_equal(result.data, reference[48000:96000])


def test_native_audio_rejects_gap_and_unknown_layout(tmp_path):
    from media_preview_generator.reprise.media import native_audio_pcm

    path = tmp_path / "gap.mka"
    execute(
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=48000:duration=3",
        "-af",
        "asetpts='PTS+if(gte(T,1),0.2/TB,0)'",
        "-c:a",
        "flac",
        "-y",
        str(path),
    )
    assert_generated_audio_gap(path)
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
    help_text = subprocess.check_output(["ffmpeg", "-hide_banner", "-h", "full"], stderr=subprocess.STDOUT)
    if b"-display_rotation" in help_text:
        execute("-display_rotation", "180", "-i", str(original), "-c", "copy", "-y", str(rotated))
    else:
        execute("-i", str(original), "-c", "copy", "-metadata:s:v:0", "rotate=180", "-y", str(rotated))
    details = json.loads(
        subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_streams",
                "-show_entries",
                "stream_side_data=side_data_type,rotation",
                "-of",
                "json",
                str(rotated),
            ]
        )
    )
    side_data = details["streams"][0].get("side_data_list", [])
    assert any(s.get("side_data_type") == "Display Matrix" and abs(s.get("rotation", 0)) == 180 for s in side_data), (
        f"generated rotation fixture lacks 180-degree display matrix: {side_data}"
    )
    assert_generated_video_format(rotated)
    item = media_version(str(rotated), 2000, "{}")
    with native_frames(item, 0, 0, 1000, lambda: False, lambda: False, 1) as reader:
        frames = list(reader)
    actual = b"".join(p.data for p in frames[0].planes)
    unrotated = execute(
        "-noautorotate", "-i", str(rotated), "-frames:v", "1", "-pix_fmt", "yuv420p", "-f", "rawvideo", "-"
    )
    assert actual == unrotated
    assert frames[0].interpretation_flags == ("display_matrix",)


@pytest.mark.parametrize(
    "stream_matrix,frame_type,allowed",
    [
        (True, "3x3 displaymatrix", True),
        (False, "3x3 displaymatrix", False),
        (True, "unknown frame interpretation", False),
    ],
)
def test_frame_display_matrix_needs_stream_rejection_flag(tmp_path, monkeypatch, stream_matrix, frame_type, allowed):
    from media_preview_generator.reprise import media

    path = tmp_path / "matrix.mkv"
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
    assert_generated_video_format(path)
    item = media_version(str(path), 2000, "{}")
    real_run = media.run

    def inject(command, *args, **kwargs):
        data = real_run(command, *args, **kwargs)
        if "-show_streams" in command:
            parsed = json.loads(data)
            if stream_matrix:
                parsed["streams"][0]["side_data_list"] = [{"side_data_type": "Display Matrix"}]
            return json.dumps(parsed).encode()
        if "-show_frames" in command:
            parsed = json.loads(data)
            parsed["frames"][0].setdefault("side_data_list", []).append({"side_data_type": frame_type})
            return json.dumps(parsed).encode()
        return data

    monkeypatch.setattr(media, "run", inject)
    if allowed:
        with media.native_frames(item, 0, 0, 1000, lambda: False, lambda: False, 1) as reader:
            frames = list(reader)
        assert frames and all(frame.interpretation_flags == ("display_matrix",) for frame in frames)
        from media_preview_generator.reprise.recurrence import video_metrics

        assert video_metrics(frames[0], frames[0]) is None
    else:
        with pytest.raises(DecodeError, match="Unsupported native frame interpretation metadata"):
            with media.native_frames(item, 0, 0, 1000, lambda: False, lambda: False, 1):
                pass


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
    assert_generated_video_format(path)
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
    assert_generated_video_format(path)
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
        if "-vf" in command:
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
    assert_generated_video_format(path)
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
        if "-vf" in command:
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
    assert_generated_video_format(path)
    item = media_version(str(path), 2000, "{}")
    real_popen = media.subprocess.Popen
    script = "import os,time; os.close(2); " + ("os.write(1,b'x'*1048576); " if extra else "") + "time.sleep(10)"

    def replacement(command, *args, **kwargs):
        if "-vf" in command:
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
    assert_generated_video_format(path)
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


@pytest.fixture
def ac3_native_audio(tmp_path):
    path = tmp_path / "surround.mp4"
    signals = "|".join(f"0.1*sin(2*PI*{300 + 75 * i}*t)" for i in range(6))
    execute(
        "-f",
        "lavfi",
        "-i",
        f"aevalsrc={signals}:s=48000:d=2:c=5.1(side)",
        "-c:a",
        "ac3",
        "-b:a",
        "384k",
        "-y",
        str(path),
    )
    return media_version(str(path), 2000, "{}")


def test_ac3_native_declarations_and_channels(ac3_native_audio):
    import numpy as np

    from media_preview_generator.reprise.media import native_audio_pcm

    item = ac3_native_audio
    decoded = native_audio_pcm(item, 0, 0, 1000, lambda: False, lambda: False, 1)
    reference = np.frombuffer(
        execute("-i", item.canonical_path, "-c:a", "pcm_f64le", "-f", "f64le", "-"), dtype="<f8"
    ).reshape(-1, 6)
    assert decoded.data.shape == (48000, 6)
    assert np.array_equal(decoded.data, reference[:48000])
    declarations = dict(decoded.declarations)
    assert set(declarations) <= {"audio_service_initial", "audio_service_per_frame", "matrix_encoding", "downmix"}
    for key in ("audio_service_initial", "audio_service_per_frame"):
        if key in declarations:
            assert declarations[key] == ("Main Audio Service",)
    if "matrix_encoding" in declarations:
        assert declarations["matrix_encoding"] == ("none",)
    assert declarations["downmix"][0] == "unknown"


def with_test_audio_declarations(lines):
    """Insert known records into real decoder logs for cross-version fault injection."""
    output = []
    frames = 0
    for line in lines:
        if b"side data - matrix encoding:" in line or b"side data - audio service type:" in line:
            continue
        output.append(line)
        if b"Parsed_ashowinfo_" in line and b" n:" in line:
            frames += 1
            prefix = line.split(b" n:", 1)[0] + b" "
            if frames == 1:
                output.append(prefix + b"side data - audio service type: Main Audio Service\n")
            output.append(prefix + b"side data - matrix encoding: none\n")
    assert frames >= 2
    return output


def intercept_audio_records(monkeypatch, transform):
    from media_preview_generator.reprise import media

    original = media._AudioLines.next_line
    pending = {}

    def altered(self, *args, **kwargs):
        if "-af" not in self.proc.args:
            return original(self, *args, **kwargs)
        if self not in pending:
            lines = []
            while True:
                line = original(self, *args, **kwargs)
                if line is None:
                    break
                lines.append(line + b"\n")
            pending[self] = iter(transform(lines))
        return next(pending[self], None)

    monkeypatch.setattr(media._AudioLines, "next_line", altered)


def test_sequential_native_audio_chunks_are_provisional_and_exact(ac3_native_audio):
    import numpy as np

    from media_preview_generator.reprise.media import native_audio_chunks

    item = ac3_native_audio
    with native_audio_chunks(item, 0, 125, 1125, lambda: False, lambda: False, 1, chunk_samples=4800) as reader:
        chunks = list(reader)
        assert reader.declarations is None
    assert reader.declarations is not None
    assert len(chunks) == 10
    assert all(not chunk.data.flags.writeable and chunk.data.shape == (4800, 6) for chunk in chunks)
    assert [chunk.support_start_ms for chunk in chunks] == list(range(125, 1125, 100))
    assert chunks[-1].support_end_ms == 1125
    reference = np.frombuffer(
        execute("-i", item.canonical_path, "-c:a", "pcm_f64le", "-f", "f64le", "-"), dtype="<f8"
    ).reshape(-1, 6)
    assert np.array_equal(np.concatenate([chunk.data for chunk in chunks]), reference[6000:54000])


def test_sequential_audio_abandon_and_consumer_error_invalidate(ac3_native_audio):
    from media_preview_generator.reprise.media import native_audio_chunks

    with pytest.raises(DecodeError, match="not exhausted"):
        with native_audio_chunks(
            ac3_native_audio, 0, 0, 1500, lambda: False, lambda: False, 1, chunk_samples=4800
        ) as reader:
            next(reader)
    assert reader.declarations is None
    with pytest.raises(RuntimeError, match="consumer"):
        with native_audio_chunks(
            ac3_native_audio, 0, 0, 1500, lambda: False, lambda: False, 1, chunk_samples=4800
        ) as reader:
            next(reader)
            raise RuntimeError("consumer")
    assert reader.declarations is None


def test_sequential_audio_final_exit_honors_pause_and_cancel(ac3_native_audio):
    from media_preview_generator.reprise import media

    state = {"final": False, "pauses": 0, "cancel": False}

    def pause():
        if state["final"]:
            state["pauses"] += 1
            return state["pauses"] <= 2
        return False

    with media.native_audio_chunks(
        ac3_native_audio, 0, 0, 1000, lambda: state["cancel"], pause, 1, chunk_samples=4800
    ) as reader:
        assert list(reader)
        state["final"] = True
    assert state["pauses"] >= 3 and reader.declarations is not None

    state.update(final=False, cancel=False)
    with pytest.raises(media.Cancelled):
        with media.native_audio_chunks(
            ac3_native_audio, 0, 0, 1000, lambda: state["cancel"], pause, 1, chunk_samples=4800
        ) as reader:
            assert list(reader)
            state["cancel"] = True
    assert reader.declarations is None


def test_sequential_audio_rechecks_file_after_exhaustion(ac3_native_audio):
    from media_preview_generator.reprise.media import native_audio_chunks

    with pytest.raises(DecodeError, match="Media changed"):
        with native_audio_chunks(
            ac3_native_audio, 0, 0, 1000, lambda: False, lambda: False, 1, chunk_samples=4800
        ) as reader:
            assert list(reader)
            with open(ac3_native_audio.canonical_path, "ab") as output:
                output.write(b"changed")
    assert reader.declarations is None


def test_sequential_audio_late_fault_invalidates_provisional_chunks(ac3_native_audio, monkeypatch):
    from media_preview_generator.reprise.media import native_audio_chunks

    def alter(lines):
        lines.append(b"[error] injected late decoder diagnostic\n")
        return [line.rstrip(b"\n") for line in lines]

    intercept_audio_records(monkeypatch, alter)
    seen = []
    with pytest.raises(DecodeError, match="decoder reported"):
        with native_audio_chunks(
            ac3_native_audio, 0, 0, 1000, lambda: False, lambda: False, 1, chunk_samples=4800
        ) as reader:
            for chunk in reader:
                seen.append(chunk)
    assert seen and reader.declarations is None


def test_audio_child_start_failure_reaps_process(monkeypatch):
    import sys
    import threading

    from media_preview_generator.reprise import media

    created = []
    real_popen = media.subprocess.Popen
    real_start = threading.Thread.start
    calls = []

    def capture(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        created.append(process)
        return process

    def fail_second(thread):
        calls.append(thread)
        if len(calls) == 2:
            raise RuntimeError("thread startup")
        return real_start(thread)

    monkeypatch.setattr(media.subprocess, "Popen", capture)
    monkeypatch.setattr(threading.Thread, "start", fail_second)
    with pytest.raises(RuntimeError, match="thread startup"):
        media._AudioLines([sys.executable, "-c", "import time; time.sleep(60)"], decoder=False)
    assert len(created) == 1 and created[0].poll() is not None


def test_audio_pause_is_barrier_before_queued_record(monkeypatch):
    import sys

    from media_preview_generator.reprise import media

    child = media._AudioLines([sys.executable, "-c", "import time; time.sleep(60)"], decoder=False)
    child.events.put(b"ready")
    calls = []

    def pause():
        calls.append(True)
        return len(calls) < 3

    try:
        assert child.next_line([child], lambda: False, pause) == b"ready"
        assert len(calls) >= 3
    finally:
        child.close()


def test_streamed_audio_stall_is_bounded_and_reaped(monkeypatch):
    import sys
    import time
    from types import SimpleNamespace

    from media_preview_generator.reprise import media

    child = media._AudioLines([sys.executable, "-c", "import time; time.sleep(60)"], decoder=False)
    clock = iter((0, 31))
    monkeypatch.setattr(media, "time", SimpleNamespace(monotonic=lambda: next(clock), sleep=time.sleep))
    try:
        with pytest.raises(DecodeError, match="stalled"):
            child.next_line([child], lambda: False, lambda: False)
    finally:
        child.close()
    assert child.proc.poll() is not None


def test_streamed_native_audio_cancel_reaps_both_children(ac3_native_audio, monkeypatch):
    from media_preview_generator.reprise import media

    created = []
    original_init = media._AudioLines.__init__

    def capture(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        created.append(self.proc)

    monkeypatch.setattr(media._AudioLines, "__init__", capture)
    with pytest.raises(media.Cancelled):
        media.native_audio_pcm(ac3_native_audio, 0, 0, 1000, lambda: len(created) == 2, lambda: False, 1)
    assert len(created) == 2 and all(process.poll() is not None for process in created)


@pytest.mark.parametrize("fault", ["tail_error", "malformed_frame", "timestamp_mismatch"])
def test_streamed_native_audio_rejects_protocol_faults_and_reaps(ac3_native_audio, monkeypatch, fault):
    from media_preview_generator.reprise import media

    created = []
    original_init = media._AudioLines.__init__

    def capture(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        created.append(self.proc)

    def alter(lines):
        if fault == "tail_error":
            lines.append(b"[error] injected terminal decoder error\n")
        else:
            i = next(i for i, line in enumerate(lines) if b"Parsed_ashowinfo_" in line and b" n:" in line)
            if fault == "malformed_frame":
                lines[i] = lines[i].replace(b" n:", b" malformed:", 1)
            else:
                lines[i] = lines[i].replace(b"pts:0", b"pts:9999", 1)
        return [line.rstrip(b"\n") for line in lines]

    monkeypatch.setattr(media._AudioLines, "__init__", capture)
    intercept_audio_records(monkeypatch, alter)
    with pytest.raises(DecodeError):
        media.native_audio_pcm(ac3_native_audio, 0, 0, 1000, lambda: False, lambda: False, 1)
    assert len(created) == 2 and all(process.poll() is not None for process in created)


@pytest.mark.parametrize(
    "fault",
    [
        "valid",
        "duplicate",
        "missing",
        "dynamic",
        "dynamic_valid",
        "unknown",
        "malformed",
        "late_service",
        "before_frame",
        "order",
        "filter_id",
    ],
)
def test_ac3_native_rejects_bad_declarations(ac3_native_audio, monkeypatch, fault):
    from media_preview_generator.reprise import media

    def altered(lines):
        lines = with_test_audio_declarations(lines)
        matrices = [i for i, line in enumerate(lines) if b"side data - matrix encoding:" in line]
        services = [i for i, line in enumerate(lines) if b"side data - audio service type:" in line]
        assert len(matrices) >= 2 and len(services) == 1
        idx = matrices[1]
        if fault == "duplicate":
            lines.insert(idx, lines[idx])
        elif fault == "missing":
            lines.pop(idx)
        elif fault == "dynamic":
            lines[idx] = lines[idx].replace(b"matrix encoding: none", b"matrix encoding: dolby")
        elif fault == "dynamic_valid":
            idx = next(i for i, line in enumerate(lines) if i > matrices[1] and b"side data - downmix:" in line)
            lines[idx] = lines[idx].replace(b"0.594604", b"0.500000")
        elif fault == "unknown":
            lines[idx] = lines[idx].replace(b"matrix encoding: none", b"unknown declaration: value")
        elif fault == "malformed":
            lines[idx] = lines[idx].replace(b"matrix encoding: none", b"downmix: nan")
        elif fault == "late_service":
            lines.insert(idx, lines[services[0]])
        elif fault == "order":
            following = next(i for i, line in enumerate(lines) if i > idx and b"side data - downmix:" in line)
            lines[idx], lines[following] = lines[following], lines[idx]
        elif fault == "filter_id":
            lines[idx] = lines[idx].replace(b"Parsed_ashowinfo_1", b"Parsed_ashowinfo_9")
        elif fault == "before_frame":
            service = lines.pop(services[0])
            first_frame = next(i for i, line in enumerate(lines) if b"Parsed_ashowinfo_" in line and b" n:" in line)
            lines.insert(first_frame, service)
        return [line.rstrip(b"\n") for line in lines]

    intercept_audio_records(monkeypatch, altered)
    if fault == "valid":
        decoded = media.native_audio_pcm(ac3_native_audio, 0, 0, 1000, lambda: False, lambda: False, 1)
        assert dict(decoded.declarations)["audio_service_initial"] == ("Main Audio Service",)
        assert dict(decoded.declarations)["matrix_encoding"] == ("none",)
    else:
        with pytest.raises(DecodeError):
            media.native_audio_pcm(ac3_native_audio, 0, 0, 1000, lambda: False, lambda: False, 1)


@pytest.mark.parametrize(
    ("declaration", "other"),
    [
        (None, None),
        ((), ()),
        ((("matrix_encoding", ("none",)),), (("matrix_encoding", ("none",)),)),
        ((("matrix_encoding", ("none",)),), ()),
        ((("matrix_encoding", ("none",)),), (("matrix_encoding", ("different",)),)),
        (
            (("audio_service_initial", ("Main Audio Service",)),),
            (("audio_service_per_frame", ("Main Audio Service",)),),
        ),
    ],
)
def test_native_audio_comparison_requires_identical_declarations(ac3_native_audio, monkeypatch, declaration, other):
    from media_preview_generator.reprise import analysis, media

    original_reader = media.native_audio_chunks
    original_verifier = analysis.verify.AudioVerifier
    profiles = iter((declaration, other))
    finished = []

    class DeclaredReader:
        def __init__(self, inner, profile):
            self.inner = inner
            self.profile = profile
            self.declarations = None

        def __enter__(self):
            self.inner.__enter__()
            return self

        def __iter__(self):
            return iter(self.inner)

        def __exit__(self, exc_type, exc, tb):
            result = self.inner.__exit__(exc_type, exc, tb)
            if exc_type is None:
                self.declarations = self.profile
            return result

    class TrackingVerifier(original_verifier):
        def finish(self):
            finished.append(True)
            return super().finish()

    monkeypatch.setattr(
        media,
        "native_audio_chunks",
        lambda *args, **kwargs: DeclaredReader(original_reader(*args, **kwargs), next(profiles)),
    )
    monkeypatch.setattr(analysis.verify, "AudioVerifier", TrackingVerifier)
    item = ac3_native_audio
    args = (item, item, [("a", 0, 48000, 6)], [("b", 0, 48000, 6)], 0, 1000, 0, lambda: False, lambda: False, 1)
    if declaration is not None and other == declaration:
        analysis._audio_support(*args)
        assert finished == [True]
    else:
        with pytest.raises(DecodeError, match="declarations differ"):
            analysis._audio_support(*args)
        assert not finished


@pytest.mark.parametrize("repeat", [True, False])
def test_ac3_per_frame_service_scope_and_absent_matrix(ac3_native_audio, monkeypatch, repeat):
    from media_preview_generator.reprise import media

    def altered(lines):
        lines = with_test_audio_declarations(lines)
        service = next(line for line in lines if b"side data - audio service type:" in line)
        output = []
        count = 0
        for line in lines:
            if b"side data - matrix encoding:" in line or b"side data - audio service type:" in line:
                continue
            output.append(line)
            if b"Parsed_ashowinfo_" in line and b" n:" in line:
                count += 1
                if repeat or count <= 2:
                    output.append(service)
        assert count > 2
        return [line.rstrip(b"\n") for line in output]

    intercept_audio_records(monkeypatch, altered)
    if repeat:
        result = media.native_audio_pcm(ac3_native_audio, 0, 0, 1000, lambda: False, lambda: False, 1)
        declarations = dict(result.declarations)
        assert declarations["audio_service_per_frame"] == ("Main Audio Service",)
        assert "audio_service_initial" not in declarations and "matrix_encoding" not in declarations
        assert declarations["downmix"][0] == "unknown"
    else:
        with pytest.raises(DecodeError, match="ambiguous scope"):
            media.native_audio_pcm(ac3_native_audio, 0, 0, 1000, lambda: False, lambda: False, 1)
