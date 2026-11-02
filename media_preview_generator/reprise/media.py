"""Bounded, cancellable local media decoding. No media is sent off-host.

Video checksums are over complete decoded frames, not resized thumbnails. Timestamp
values come from the decoder's presentation timestamps, never frame counts/FPS.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
import time
from fractions import Fraction

from .identity import is_current

MAX_OUTPUT = 128 * 1024 * 1024


class DecodeError(Exception):
    pass


class Cancelled(DecodeError):
    pass


def check_current(media):
    if not is_current(media):
        raise DecodeError('Media changed since registration')


def run(command, cancel_check, pause_check, limit=MAX_OUTPUT):
    """Capture to a bounded temporary file so a stopped child cannot block on a pipe."""
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output, stderr=errors, start_new_session=True)
        paused = False
        try:
            while proc.poll() is None:
                if cancel_check():
                    raise Cancelled('Analysis cancelled')
                want_pause = bool(pause_check())
                if want_pause != paused:
                    os.killpg(proc.pid, signal.SIGSTOP if want_pause else signal.SIGCONT)
                    paused = want_pause
                if output.tell() > limit:
                    raise DecodeError('Decode output exceeded resource limit')
                time.sleep(.05)
            if cancel_check():
                raise Cancelled('Analysis cancelled')
            if proc.returncode:
                errors.seek(0)
                raise DecodeError('Media decoder failed: ' + errors.read(512).decode('utf-8', 'replace'))
            output.seek(0, os.SEEK_END)
            if output.tell() > limit:
                raise DecodeError('Decode output exceeded resource limit')
            output.seek(0)
            return output.read()
        finally:
            if proc.poll() is None:
                if paused:
                    os.killpg(proc.pid, signal.SIGCONT)
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()


def probe(media, cancel_check, pause_check):
    check_current(media)
    raw = run(['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', media.canonical_path], cancel_check, pause_check, 2 * 1024 * 1024)
    info = json.loads(raw)
    signature = json.loads(media.stream_signature)
    tracks = signature.get('audio_streams')
    if not isinstance(tracks, list) or not tracks:
        raise DecodeError('No verified audio track mapping')
    audio = {s['index']: s for s in info['streams'] if s.get('codec_type') == 'audio'}
    ids, indices, result = set(), set(), []
    for item in tracks:
        raw_id, idx = item.get('plex_id'), item.get('index')
        track_id = str(raw_id) if raw_id is not None else ''
        if not track_id or not isinstance(idx, int) or idx in indices or track_id in ids or idx not in audio:
            raise DecodeError('Ambiguous or invalid audio track mapping')
        s = audio[idx]
        rate, channels = int(s.get('sample_rate', 0)), int(s.get('channels', 0))
        if not 0 < rate <= 96000 or not 0 < channels <= 8:
            raise DecodeError('Unsupported audio format')
        ids.add(track_id); indices.add(idx)
        result.append((track_id, idx, rate, channels))
    if indices != set(audio):
        raise DecodeError('Not every audio stream has a verified Plex mapping')
    videos = [s for s in info['streams'] if s.get('codec_type') == 'video' and not s.get('disposition', {}).get('attached_pic')]
    if len(videos) != 1:
        raise DecodeError('Expected one unambiguous video stream')
    origin = Fraction(str(info.get('format', {}).get('start_time', '0')))
    return videos[0]['index'], result, origin


def video_frames(media, stream, start, end, cancel_check, pause_check, threads):
    command = ['ffmpeg', '-nostdin', '-v', 'error', '-threads', str(threads), '-copyts', '-start_at_zero', '-ss', f'{start/1000:.3f}', '-i', media.canonical_path, '-t', f'{(end-start)/1000:.3f}', '-map', f'0:{stream}', '-an', '-sn', '-dn', '-fps_mode', 'passthrough', '-enc_time_base', '-1', '-f', 'framemd5', '-']
    lines = run(command, cancel_check, pause_check, 16 * 1024 * 1024).decode('ascii').splitlines()
    tb = None
    frames = []
    for line in lines:
        if line.startswith('#tb 0:'):
            tb = Fraction(line.split(':', 1)[1].strip())
        elif not line.startswith('#') and line.strip():
            if tb is None:
                raise DecodeError('Missing video time base')
            fields = [s.strip() for s in line.split(',')]
            pts = Fraction(int(fields[2])) * tb * 1000
            frames.append((pts, fields[5]))
    if not frames or any(a[0] >= b[0] for a, b in zip(frames, frames[1:])):
        raise DecodeError('Missing or non-monotonic video presentation timestamps')
    return frames


def audio_pcm(media, track, start, end, origin, cancel_check, pause_check, threads):
    _, idx, rate, channels = track
    # Raw PCM has no timestamps; independently check the input timeline.
    interval = f'{float(origin):.6f}%{float(origin + Fraction(end, 1000)):.6f}'
    cmd = ['ffprobe', '-v', 'error', '-select_streams', str(idx), '-read_intervals', interval, '-show_frames', '-show_entries', 'frame=best_effort_timestamp_time,nb_samples', '-of', 'json', media.canonical_path]
    parsed = json.loads(run(cmd, cancel_check, pause_check, 4 * 1024 * 1024))
    spans = []
    for frame in parsed.get('frames', []):
        try:
            p = Fraction(frame['best_effort_timestamp_time']) - origin
            spans.append((p, p + Fraction(int(frame['nb_samples']), rate)))
        except (KeyError, ValueError, ZeroDivisionError) as exc:
            raise DecodeError('Missing audio presentation timing') from exc
    cursor = Fraction(0)
    for lo, hi in spans:
        if abs(lo - cursor) > Fraction(1, 1000):
            raise DecodeError('Audio timestamps do not follow a continuous sample clock')
        cursor += hi - lo
    if not spans or cursor < Fraction(end, 1000) or (start * rate) % 1000 or (end * rate) % 1000:
        raise DecodeError('Audio timing does not continuously cover the window')
    # Sample-count trimming avoids losing/duplicating samples when container
    # timestamps have coarser precision than the audio sample clock.
    trim = f'atrim=start_sample={start*rate//1000}:end_sample={end*rate//1000},asetpts=PTS-STARTPTS'
    command = ['ffmpeg', '-nostdin', '-v', 'error', '-threads', str(threads), '-i', media.canonical_path, '-map', f'0:{idx}', '-vn', '-sn', '-dn', '-af', trim, '-t', f'{(end-start)/1000:.3f}', '-c:a', 'pcm_s32le', '-f', 's32le', '-']
    expected = (end-start) * rate * channels * 4 // 1000
    if expected > MAX_OUTPUT:
        raise DecodeError('Audio window exceeds resource limit')
    data = run(command, cancel_check, pause_check, min(MAX_OUTPUT, expected + rate * channels * 4))
    if len(data) != expected:
        raise DecodeError('Incomplete or discontinuous audio window')
    return data
