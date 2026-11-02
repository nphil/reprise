"""Small real-media checks for the local preview encoder."""
from __future__ import annotations

import json
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

from media_preview_generator.reprise import identity, media, preview


@pytest.fixture
def source(tmp_path):
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        pytest.skip('ffmpeg and ffprobe are required')
    path = tmp_path / 'source.mkv'
    subprocess.run([
        'ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=160x90:rate=10:duration=3',
        '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=3',
        '-f', 'lavfi', '-i', 'sine=frequency=880:sample_rate=48000:duration=3',
        '-map', '0:v', '-map', '1:a', '-map', '2:a', '-c:v', 'libx264', '-threads', '1',
        '-c:a', 'aac', '-output_ts_offset', '5', '-y', str(path),
    ], check=True, capture_output=True)
    signature = json.dumps({'audio_streams': [{'plex_id': 'first', 'index': 1}, {'plex_id': 'second', 'index': 2}]})
    return identity.media_version(str(path), 3000, signature)


def build(source, cache, **kwargs):
    values = dict(start_ms=500, end_ms=2000, audio_stream_id='second', cache_dir=cache,
                  cancel_check=lambda: False, pause_check=lambda: False)
    values.update(kwargs)
    return preview.build_preview(source, **values)


def test_real_preview_is_browser_compatible_and_selected_track(source, tmp_path):
    result = build(source, tmp_path / 'cache')
    assert (result.origin_ms, result.duration_ms, result.mime_type) == (500, 1500, 'video/mp4')
    data = json.loads(subprocess.check_output([
        'ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(result.path)
    ]))
    assert [s['codec_name'] for s in data['streams']] == ['h264', 'aac']
    assert abs(float(data['format']['start_time'])) < 0.1
    assert 1.35 <= float(data['format']['duration']) <= 1.7
    raw = subprocess.check_output([
        'ffmpeg', '-v', 'error', '-i', str(result.path), '-map', '0:a:0', '-f', 's16le',
        '-ac', '1', '-ar', '8000', '-'
    ])
    import numpy as np
    samples = np.frombuffer(raw, dtype='<i2').astype(float)
    freq = np.fft.rfftfreq(len(samples), 1 / 8000)[np.argmax(abs(np.fft.rfft(samples)))]
    assert abs(freq - 880) < 8
    assert build(source, tmp_path / 'cache').path == result.path


def test_stale_file_and_invalid_track_are_refused(source, tmp_path):
    with pytest.raises(media.DecodeError, match='Selected audio'):
        build(source, tmp_path / 'cache', audio_stream_id='missing')
    Path(source.canonical_path).touch()
    with pytest.raises(media.DecodeError, match='changed'):
        build(source, tmp_path / 'cache')


def test_invalid_ranges_and_threads_refused(source, tmp_path):
    for values in ({'start_ms': 10, 'end_ms': 10}, {'start_ms': -1}, {'end_ms': 3001}, {'ffmpeg_threads': 0}):
        with pytest.raises(ValueError):
            build(source, tmp_path / 'cache', **values)


def test_cancel_running_encoder_removes_partial_file(source, tmp_path, monkeypatch):
    original = preview._encode
    popen = subprocess.Popen
    started = False

    def encode(command, path, cancel_check, pause_check):
        def launch(*args, **kwargs):
            nonlocal started
            proc = popen(*args, **kwargs)
            started = True
            return proc
        monkeypatch.setattr(preview.subprocess, 'Popen', launch)
        return original(command, path, lambda: started, pause_check)

    monkeypatch.setattr(preview, '_encode', encode)
    cache = tmp_path / 'cache'
    with pytest.raises(media.Cancelled):
        build(source, cache)
    assert started and list(cache.iterdir()) == []


def test_pause_stops_and_resumes_running_encoder(source, tmp_path, monkeypatch):
    original = preview._encode
    popen = subprocess.Popen
    killpg = preview.os.killpg
    started = None
    sent = []

    def encode(command, path, cancel_check, pause_check):
        def launch(*args, **kwargs):
            nonlocal started
            proc = popen(*args, **kwargs)
            started = time.monotonic()
            return proc

        def record(pid, sig):
            sent.append(sig)
            return killpg(pid, sig)

        monkeypatch.setattr(preview.subprocess, 'Popen', launch)
        monkeypatch.setattr(preview.os, 'killpg', record)
        return original(command, path, cancel_check, lambda: started is not None and time.monotonic() - started < 0.3)

    monkeypatch.setattr(preview, '_encode', encode)
    assert build(source, tmp_path / 'cache').path.is_file()
    assert signal.SIGSTOP in sent and signal.SIGCONT in sent
