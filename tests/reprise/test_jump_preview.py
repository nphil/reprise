"""The on-demand jump player reads only authorized, unskipped video and audio."""

import json
import shutil
import struct
import subprocess

import pytest
from flask import Flask

from media_preview_generator.markers.store import MarkerStore
from media_preview_generator.reprise import jump_preview
from media_preview_generator.reprise import service as reprise_service
from media_preview_generator.reprise.identity import media_version
from media_preview_generator.reprise.models import PlaybackIdentity
from media_preview_generator.reprise.store import RepriseStore
from media_preview_generator.servers.registry import ServerRegistry
from media_preview_generator.web.routes.api_reprise import create_reprise_blueprint

_DURATION = 6000


@pytest.fixture
def app(tmp_path, monkeypatch):
    registry = ServerRegistry.from_settings(
        [
            {
                "id": "server",
                "name": "Test",
                "enabled": True,
                "type": "plex",
                "url": "http://127.0.0.1:9",
                "auth": {},
                "server_identity": "machine",
                "libraries": [{"id": "library", "name": "TV", "enabled": True, "remote_paths": [str(tmp_path)]}],
            }
        ]
    )
    monkeypatch.setattr("media_preview_generator.web.auth.is_auth_external", lambda: False)
    monkeypatch.setattr("media_preview_generator.web.auth.session_epoch", lambda: "jump-preview-test")
    markers = MarkerStore(str(tmp_path / "markers.db"))
    store = RepriseStore(markers)
    service = reprise_service.RepriseService(store, registry, None)
    flask_app = Flask(__name__)
    flask_app.secret_key = "throwaway"
    flask_app.register_blueprint(create_reprise_blueprint(service))
    owner = flask_app.test_client()
    with owner.session_transaction() as session:
        session.update(authenticated=True, epoch="jump-preview-test")

    def register(path, streams):
        version = media_version(str(path), _DURATION, json.dumps({"streams": streams}))
        identity = PlaybackIdentity(
            "machine", "1", "2", "3", "/library/parts/3/file", version.size_bytes, _DURATION, "4"
        )
        cfg = registry.get_config("server")
        metadata = {
            "library_id": "library",
            "remote_part_path": str(path),
            "mapping_fingerprint": reprise_service._mapping_fingerprint(cfg, cfg.libraries[0]),
        }
        store.register_media(version, identity, server_id="server", metadata=metadata)
        return f"/api/reprise/media/{version.id}/jump-preview"

    yield owner, register
    markers.close()


def _run(*args, data=None):
    return subprocess.run(args, input=data, capture_output=True, timeout=10, check=True).stdout


@pytest.fixture
def make_video(tmp_path, monkeypatch):
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("FFmpeg and ffprobe required")
    monkeypatch.setattr(reprise_service, "ffmpeg_binary", lambda: ffmpeg)

    def make(*, sound=True):
        path = tmp_path / "episode.mkv"
        # At 8 frames per second every frame has its own grey value. No keyframe at either skip boundary.
        command = [ffmpeg, "-v", "error", "-f", "lavfi", "-i", "nullsrc=s=64x36:r=8:d=6,geq=lum=16+N*4:cb=128:cr=128"]
        if sound:
            # Track 1 is silent; track 2 is default and audibly louder *inside* the skip. Neither should leak through.
            command += [
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=44100:cl=mono:d=6",
                "-f",
                "lavfi",
                "-i",
                r"aevalsrc=0.1*(1+4*between(t\,2.525\,3.475))*sin(2*PI*880*t):d=6",
                "-map",
                "0:v",
                "-map",
                "1:a",
                "-map",
                "2:a",
                "-disposition:a:0",
                "0",
                "-disposition:a:1",
                "default",
                "-c:a",
                "pcm_s16le",
            ]
        _run(
            *command,
            "-c:v",
            "libx264",
            "-threads",
            "1",
            "-crf",
            "0",
            "-g",
            "100",
            "-output_ts_offset",
            "1.25",
            str(path),
        )
        streams = json.loads(_run(ffprobe, "-v", "error", "-show_streams", "-of", "json", str(path)))["streams"]
        return path, streams, ffmpeg

    return make


def _clip(owner, url, start, end):
    response = owner.get(url, query_string={"start_ms": start, "end_ms": end})
    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.mimetype == "video/mp4"
    assert response.headers["Cache-Control"] == "no-store"
    return response.data


def _frames(ffmpeg, clip):
    pixels = _run(ffmpeg, "-v", "error", "-i", "pipe:0", "-vf", "crop=2:2", "-f", "rawvideo", "pipe:1", data=clip)
    return [round((luma - 16) / 4) for luma in pixels[::6]]


def test_preview_splices_the_two_windows_without_skipped_frames_or_audio(app, make_video):
    owner, register = app
    path, streams, ffmpeg = make_video()
    clip = _clip(owner, register(path, streams), 2525, 3475)
    frames = _frames(ffmpeg, clip)
    assert list(dict.fromkeys(frames)) == [*range(5, 21), *range(28, 44)]
    assert 92 <= len(frames) <= 98  # about four seconds at 24 fps
    pcm = _run(ffmpeg, "-v", "error", "-i", "pipe:0", "-vn", "-f", "s16le", "pipe:1", data=clip)
    assert 1500 < max(abs(sample) for (sample,) in struct.iter_unpack("<h", pcm)) < 10000


@pytest.mark.parametrize(
    ("start", "end", "frames"),
    [(0, 3475, range(28, 44)), (2525, _DURATION, range(5, 21)), (2525, 10_000, range(5, 21))],
)
def test_preview_with_only_one_side_and_no_audio(app, make_video, start, end, frames):
    owner, register = app
    path, streams, ffmpeg = make_video(sound=False)
    clip = _clip(owner, register(path, streams), start, end)
    found = _frames(ffmpeg, clip)
    assert list(dict.fromkeys(found)) == list(frames)
    assert 44 <= len(found) <= 49


def test_preview_does_not_lengthen_context_at_the_ends_of_the_file(app, make_video):
    owner, register = app
    path, streams, ffmpeg = make_video(sound=False)
    clip = _clip(owner, register(path, streams), 225, 5875)
    frames = _frames(ffmpeg, clip)
    assert list(dict.fromkeys(frames)) == [0, 1, 47]
    assert 7 <= len(frames) <= 10  # 350 ms at 24 fps


def test_only_the_owner_can_request_a_registered_medias_preview(app, tmp_path, monkeypatch):
    owner, register = app
    path = tmp_path / "episode.mp4"
    path.write_bytes(b"original")
    url = register(path, [{"codec_type": "video", "index": 0}])
    query = "?start_ms=2000&end_ms=3500"
    player = owner.post("/api/reprise/clients", json={"name": "Player"}).json["secret"]

    def unexpected_encoder(*args, **kwargs):
        pytest.fail("Rejected requests must not run FFmpeg")

    monkeypatch.setattr(jump_preview, "_render", unexpected_encoder)
    assert owner.application.test_client().get(url + query).status_code == 401
    assert owner.get(url + query, headers={"Authorization": f"Bearer {player}"}).status_code == 403
    assert owner.head(url + query).status_code == 405
    assert owner.get("/api/reprise/media/unknown/jump-preview" + query).status_code == 404
    assert owner.get(url + "?start_ms=0&end_ms=6000").status_code == 404
    for malformed in (
        "?start_ms=-1&end_ms=12",
        "?start_ms=10&end_ms=10",
        "?start_ms=6000&end_ms=6001",
        "?end_ms=3500",
        query + "&start_ms=1000",
        query + "&path=/elsewhere.mp4",
    ):
        assert owner.get(url + malformed).status_code == 400
    path.write_bytes(b"replaced with a different file")
    assert owner.get(url + query).status_code == 404


def test_busy_and_timed_out_encodes_fail_without_holding_a_slot(app, tmp_path, monkeypatch):
    owner, register = app
    path = tmp_path / "episode.mp4"
    path.write_bytes(b"original")
    url = register(path, [{"codec_type": "video", "index": 0}]) + "?start_ms=2000&end_ms=3500"
    assert jump_preview._SLOTS.acquire(blocking=False)
    assert jump_preview._SLOTS.acquire(blocking=False)
    try:
        assert owner.get(url).status_code == 503
    finally:
        jump_preview._SLOTS.release()
        jump_preview._SLOTS.release()

    def timeout(command, *, timeout, **_):
        assert timeout == 15
        raise subprocess.TimeoutExpired(command, timeout)

    monkeypatch.setattr(jump_preview.subprocess, "run", timeout)
    for _ in range(3):
        assert owner.get(url).status_code == 404
