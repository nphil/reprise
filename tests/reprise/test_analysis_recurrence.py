"""Synthetic media regressions: no private library media is involved."""
import json
import subprocess
from pathlib import Path

import pytest

from media_preview_generator.reprise.analysis import analyze
from media_preview_generator.reprise.identity import media_version
from media_preview_generator.reprise.models import CandidateWindow


def make_video(path: Path, picture=None, changed_audio=False, filters=None):
    vf = filters or ('null' if picture is None else f"drawbox=x=0:y=0:w=40:h=40:color=red:t=fill:enable='between(t,{picture[0]},{picture[1]})'")
    if changed_audio:
        sound = r'aevalsrc=if(between(t\,2.2\,2.3)\,0.3*sin(2*PI*900*t)\,0.3*sin(2*PI*440*t)):s=48000:d=7'
    else:
        sound = 'aevalsrc=0.3*sin(2*PI*440*t):s=48000:d=7'
    subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-threads', '1', '-f', 'lavfi', '-i', 'testsrc2=size=96x64:rate=10:duration=7', '-f', 'lavfi', '-i', sound, '-vf', vf, '-map', '0:v', '-map', '1:a', '-fps_mode', 'passthrough', '-enc_time_base', '1:1000', '-c:v', 'ffv1', '-c:a', 'pcm_s16le', '-y', str(path)], check=True)
    return media_version(str(path), 7000, json.dumps({'audio_streams': [{'plex_id': '123', 'index': 1}]}))


def run(target, reference, kind='intro'):
    return analyze(target, [CandidateWindow(kind, 1000, 6000, 'test')], [reference], cancel_check=lambda: False, pause_check=lambda: False, progress_callback=None, ffmpeg_threads=1)


def skips(result):
    return [r for r in result.ranges if r.action == 'skip']


def overlaps(start, end, ranges):
    return any(r.start_ms < end and r.end_ms > start for r in ranges)


@pytest.mark.parametrize('picture,changed_audio,kind', [
    ((2.0, 2.9), False, 'intro'),  # changed pictures with identical music
    (None, True, 'intro'),          # same pictures with new dialogue
    ((2.2, 2.3), False, 'intro'),   # a very short inserted gag
    ((4.0, 4.3), False, 'credits'), # credit/post-credit footage
])
def test_changed_second_is_preserved(tmp_path, picture, changed_audio, kind):
    reference = make_video(tmp_path / 'reference.mkv')
    target = make_video(tmp_path / 'target.mkv', picture, changed_audio)
    result = run(target, reference, kind)
    assert result.status == 'complete', result.reasons
    assert skips(result), result.reasons
    altered_second = 4000 if kind == 'credits' else 2000
    assert not overlaps(altered_second, altered_second + 1000, skips(result))
    assert all(r.review_status == 'proposed' for r in result.ranges)
    assert all(r.audio_stream_ids == ('123',) for r in skips(result))


def test_identical_timeline_produces_proposals(tmp_path):
    reference = make_video(tmp_path / 'reference.mkv')
    target = make_video(tmp_path / 'target.mkv')
    result = run(target, reference)
    assert result.status == 'complete', result.reasons
    assert overlaps(3000, 4000, skips(result)), result.reasons


def test_cancelled_analysis_never_returns_partial_ranges(tmp_path):
    item = make_video(tmp_path / 'one.mkv')
    result = analyze(item, [CandidateWindow('intro', 1000, 6000, 'test')], [], cancel_check=lambda: True, pause_check=lambda: False, progress_callback=None, ffmpeg_threads=1)
    assert result.status == 'cancelled'
    assert not result.ranges and not result.evidence


def test_reordered_pictures_do_not_form_a_false_run(tmp_path):
    def boxes(first, second):
        return (f"drawbox=x=0:y=0:w=40:h=40:color={first}:t=fill:enable='between(t,2,2.9)',"
                f"drawbox=x=0:y=0:w=40:h=40:color={second}:t=fill:enable='between(t,4,4.9)'")
    reference = make_video(tmp_path / 'reference.mkv', filters=boxes('red', 'blue'))
    target = make_video(tmp_path / 'target.mkv', filters=boxes('blue', 'red'))
    result = run(target, reference)
    assert result.status == 'complete', result.reasons
    assert not overlaps(2000, 3000, skips(result))
    assert not overlaps(4000, 5000, skips(result))


def test_nonzero_video_seek_keeps_file_presentation_time(tmp_path):
    reference = make_video(tmp_path / 'reference.mkv')
    target = make_video(tmp_path / 'target.mkv')
    result = analyze(target, [CandidateWindow('credits', 3000, 6000, 'test')], [reference], cancel_check=lambda: False, pause_check=lambda: False, progress_callback=None, ffmpeg_threads=1)
    assert result.status == 'complete', result.reasons
    assert overlaps(4000, 5000, skips(result)), result.reasons
    assert all(3000 <= r.start_ms < r.end_ms <= 6000 for r in skips(result))


def test_variable_presentation_times_do_not_hide_a_changed_frame():
    from fractions import Fraction
    from media_preview_generator.reprise.recurrence import matching_second
    timestamps = [Fraction(0)]
    for i in range(35):
        timestamps.append(timestamps[-1] + (80 if i % 2 else 120))
    original = [(pts, f'frame-{i}') for i, pts in enumerate(timestamps)]
    changed = [(pts, 'gag' if 1400 <= pts < 1600 else digest) for pts, digest in original]
    assert matching_second(original, original, 1000, 0)
    assert not matching_second(original, changed, 1000, 0)


def test_actual_vfr_video_preserves_short_visual_change(tmp_path):
    vfr = r'settb=1/1000,setpts=PTS+floor(N/2)*20'
    gag = "drawbox=x=0:y=0:w=40:h=40:color=red:t=fill:enable='between(t,2.2,2.3)'," + vfr
    reference = make_video(tmp_path / 'reference.mkv', filters=vfr)
    target = make_video(tmp_path / 'target.mkv', filters=gag)
    probe = subprocess.check_output(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'frame=best_effort_timestamp_time', '-of', 'json', target.canonical_path])
    times = [round(float(f['best_effort_timestamp_time']) * 1000) for f in json.loads(probe)['frames']]
    assert len({b-a for a, b in zip(times, times[1:])}) > 1
    result = run(target, reference)
    assert result.status == 'complete', result.reasons
    assert skips(result), result.reasons
    assert not overlaps(2000, 3000, skips(result))


def test_inode_mismatch_rejected(tmp_path):
    from dataclasses import replace
    from media_preview_generator.reprise.media import DecodeError, check_current
    path = tmp_path / 'source'
    path.write_bytes(b'example')
    version = media_version(str(path), 1000, '{}')
    check_current(version)
    with pytest.raises(DecodeError):
        check_current(replace(version, inode=version.inode + 1))


def test_duplicate_candidates_have_unique_store_keys(tmp_path):
    reference = make_video(tmp_path / 'reference.mkv')
    target = make_video(tmp_path / 'target.mkv')
    window = CandidateWindow('intro', 1000, 6000, 'test')
    result = analyze(target, [window, window], [reference], cancel_check=lambda: False, pause_check=lambda: False, progress_callback=None, ffmpeg_threads=1)
    assert result.status == 'complete', result.reasons
    assert len({e.id for e in result.evidence}) == len(result.evidence)
    assert len({r.id for r in result.ranges}) == len(result.ranges)
