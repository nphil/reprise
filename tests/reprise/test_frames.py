"""The pictures of the approval screen: stills, thumbnail strips and cut points, read by a real ffmpeg."""

import io
import os
import subprocess
from types import SimpleNamespace

import pytest
from PIL import Image

from media_preview_generator.reprise import frames as frames_module
from media_preview_generator.reprise.frames import Frames


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    """Six seconds: three of a test pattern, then a hard cut to three of plain red."""
    path = tmp_path_factory.mktemp("clip") / "two-scenes.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=320x180:rate=10:duration=3",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:size=320x180:rate=10:duration=3",
            "-filter_complex",
            "[0:v][1:v]concat=n=2:v=1:a=0",
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(path),
        ],
        check=True,
        timeout=60,
    )
    stat = os.stat(path)
    return SimpleNamespace(
        id="v1", size_bytes=stat.st_size, mtime_ns=stat.st_mtime_ns, canonical_path=str(path), duration_ms=6000
    )


@pytest.fixture
def frames(tmp_path):
    return Frames("ffmpeg", str(tmp_path / "pictures"))


def size_of(data):
    assert data[:2] == b"\xff\xd8", "a JPEG starts with FFD8"
    return Image.open(io.BytesIO(data)).size


def test_a_still_is_a_jpeg_of_the_asked_width_and_is_kept(frames, clip, monkeypatch):
    assert size_of(frames.still(clip, 1000, 160))[0] == 160
    monkeypatch.setattr(frames, "_run", lambda *a, **k: pytest.fail("the second read must come from the cache"))
    assert size_of(frames.still(clip, 1000, 160))[0] == 160


def test_a_still_asked_past_the_end_is_the_last_picture(frames, clip):
    assert size_of(frames.still(clip, 99_000, 120))[0] == 120


def test_a_strip_is_one_picture_with_a_tile_for_each_step(frames, clip):
    width, height = size_of(frames.strip(clip, 0, 1000, 4, 120))
    assert (width, height) == (480, 68)


def test_a_strip_that_runs_past_the_end_keeps_its_width(frames, clip):
    assert size_of(frames.strip(clip, 4000, 1000, 4, 120))[0] == 480


def test_the_hard_cut_between_two_scenes_is_found_and_remembered(frames, clip, monkeypatch):
    [cut] = frames.cuts(clip, 1000, 5000)
    assert abs(cut - 3000) <= 150
    assert frames.cuts(clip, 0, 2000) == []  # one scene only: nothing to snap to
    monkeypatch.setattr(frames, "_run", lambda *a, **k: pytest.fail("the second look must come from memory"))
    assert frames.cuts(clip, 1000, 5000) == [cut]


@pytest.mark.parametrize(
    "ask",
    [
        lambda f, v: f.still(v, 0, 100),  # not one of the sizes
        lambda f, v: f.strip(v, 0, 1000, 25, 120),  # too many pictures
        lambda f, v: f.strip(v, 0, 100, 4, 120),  # too close together
        lambda f, v: f.cuts(SimpleNamespace(**{**vars(v), "duration_ms": 100_000}), 0, 31_000),  # too long a stretch
        lambda f, v: f.cuts(v, 3000, 3000),
    ],
)
def test_a_request_outside_the_limits_is_refused(frames, clip, ask):
    with pytest.raises(ValueError):
        ask(frames, clip)


def test_a_file_that_cannot_be_read_says_so_in_one_sentence(frames, clip):
    gone = SimpleNamespace(**{**vars(clip), "canonical_path": "/nonexistent/episode.mkv", "id": "gone"})
    with pytest.raises(OSError, match="Reprise could not read a picture from this episode."):
        frames.still(gone, 0, 120)


def test_the_cache_folder_never_grows_past_its_limit(tmp_path, clip, monkeypatch):
    monkeypatch.setattr(frames_module, "_PRUNE_EVERY", 1)
    small = Frames("ffmpeg", str(tmp_path / "pictures"), keep=3)
    for at in range(0, 6000, 1000):
        small.still(clip, at, 120)
    kept = [name for _, _, files in os.walk(small.directory) for name in files]
    assert len(kept) <= 3
