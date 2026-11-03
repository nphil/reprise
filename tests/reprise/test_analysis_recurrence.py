"""Synthetic media regressions: no private library media is involved."""

import json
import subprocess
from pathlib import Path

import pytest

from media_preview_generator.markers.store import MarkerStore
from media_preview_generator.reprise.analysis import analyze
from media_preview_generator.reprise.identity import media_version
from media_preview_generator.reprise.models import CandidateWindow, PlaybackIdentity
from media_preview_generator.reprise.store import RepriseStore

CHIRP = "0.3*sin(2*PI*(200*t+13*t*t))"  # aperiodic, so its sound lines up at exactly one lag


def chirp(expression=CHIRP):
    return f"aevalsrc={expression}:s=48000:d=7"


def make_video(
    path: Path,
    picture=None,
    changed_audio=False,
    filters=None,
    audio_filters=None,
    sound=None,
    source=None,
    layout="mono",
):
    vf = filters or (
        "null"
        if picture is None
        else f"drawbox=x=0:y=0:w=40:h=40:color=red:t=fill:enable='between(t,{picture[0]},{picture[1]})'"
    )
    if not sound:
        sound = (
            r"aevalsrc=if(between(t\,2.2\,2.3)\,0.3*sin(2*PI*900*t)\,0.3*sin(2*PI*440*t)):s=48000:d=7"
            if changed_audio
            else "aevalsrc=0.3*sin(2*PI*440*t):s=48000:d=7"
        )
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-threads",
            "1",
            "-f",
            "lavfi",
            "-i",
            source or "testsrc2=size=96x64:rate=10:duration=7",
            "-f",
            "lavfi",
            "-i",
            sound,
            "-vf",
            vf,
            "-map",
            "0:v",
            "-map",
            "1:a",
            "-fps_mode",
            "passthrough",
            "-enc_time_base",
            "1:1000",
            "-c:v",
            "ffv1",
            "-chroma_sample_location",
            "left",
            *(["-af", audio_filters] if audio_filters else []),
            "-c:a",
            "flac",
            "-channel_layout",
            layout,
            "-threads",
            "1",
            "-y",
            str(path),
        ],
        check=True,
    )
    return media_version(str(path), 7000, json.dumps({"audio_streams": [{"plex_id": "123", "index": 1}]}))


@pytest.fixture
def catalog(tmp_path):
    markers = MarkerStore(str(tmp_path / "markers.db"))
    store = RepriseStore(markers)

    def register(*versions):
        for episode, version in enumerate(versions, start=1):
            key = str(episode)
            identity = PlaybackIdentity(
                "machine", key, key, key, "/part/" + key, version.size_bytes, version.duration_ms, "123"
            )
            store.register_media(
                version,
                identity,
                server_id="server",
                metadata={"library_id": "library", "show_key": "show", "season": 1, "episode": episode},
            )
        return store

    yield register
    markers.close()


def run(target, reference, catalog, kind="intro"):
    return analyze(
        target,
        [CandidateWindow(kind, 1000, 6000, "test")],
        [reference],
        store=catalog(target, reference),
        cancel_check=lambda: False,
        pause_check=lambda: False,
        progress_callback=None,
        ffmpeg_threads=1,
    )


def skips(result):
    return [r for r in result.ranges if r.action == "skip"]


def overlaps(start, end, ranges):
    return any(r.start_ms < end and r.end_ms > start for r in ranges)


@pytest.mark.parametrize(
    "picture,changed_audio,kind",
    [
        ((2.0, 2.9), False, "intro"),  # changed pictures with identical music
        (None, True, "intro"),  # same pictures with new dialogue
        ((2.2, 2.3), False, "intro"),  # a very short inserted gag
        ((4.0, 4.3), False, "credits"),  # credit/post-credit footage
    ],
)
def test_changed_native_material_is_preserved(tmp_path, catalog, picture, changed_audio, kind):
    reference = make_video(tmp_path / "reference.mkv")
    target = make_video(tmp_path / "target.mkv", picture, changed_audio)
    result = run(target, reference, catalog, kind)
    assert result.status == "complete", result.reasons
    assert skips(result), result.reasons
    if picture is None:
        protected_start, protected_end = 2200, 2301
    else:
        protected_start = round(picture[0] * 1000)
        protected_end = round(picture[1] * 1000) + 100  # inclusive enabled frame has 100ms support
    assert not overlaps(protected_start, protected_end, skips(result))
    assert all(r.review_status == "proposed" for r in result.ranges)
    assert all(r.audio_stream_ids == ("123",) for r in skips(result))


def test_identical_timeline_produces_proposals(tmp_path, catalog):
    reference = make_video(tmp_path / "reference.mkv")
    target = make_video(tmp_path / "target.mkv")
    result = run(target, reference, catalog)
    assert result.status == "complete", result.reasons
    assert overlaps(3000, 4000, skips(result)), result.reasons


def test_cancelled_analysis_never_returns_partial_ranges(tmp_path, catalog):
    item = make_video(tmp_path / "one.mkv")
    result = analyze(
        item,
        [CandidateWindow("intro", 1000, 6000, "test")],
        [],
        store=catalog(item),
        cancel_check=lambda: True,
        pause_check=lambda: False,
        progress_callback=None,
        ffmpeg_threads=1,
    )
    assert result.status == "cancelled"
    assert not result.ranges and not result.evidence


def test_reordered_pictures_do_not_form_a_false_run(tmp_path, catalog):
    def boxes(first, second):
        return (
            f"drawbox=x=0:y=0:w=40:h=40:color={first}:t=fill:enable='between(t,2,2.9)',"
            f"drawbox=x=0:y=0:w=40:h=40:color={second}:t=fill:enable='between(t,4,4.9)'"
        )

    reference = make_video(tmp_path / "reference.mkv", filters=boxes("red", "blue"))
    target = make_video(tmp_path / "target.mkv", filters=boxes("blue", "red"))
    result = run(target, reference, catalog)
    assert result.status == "complete", result.reasons
    assert not overlaps(2000, 3000, skips(result))
    assert not overlaps(4000, 5000, skips(result))


def test_nonzero_video_seek_keeps_file_presentation_time(tmp_path, catalog):
    reference = make_video(tmp_path / "reference.mkv")
    target = make_video(tmp_path / "target.mkv")
    result = analyze(
        target,
        [CandidateWindow("credits", 3000, 6000, "test")],
        [reference],
        store=catalog(target, reference),
        cancel_check=lambda: False,
        pause_check=lambda: False,
        progress_callback=None,
        ffmpeg_threads=1,
    )
    assert result.status == "complete", result.reasons
    assert overlaps(4000, 5000, skips(result)), result.reasons
    assert all(3000 <= r.start_ms < r.end_ms <= 6000 for r in skips(result))


@pytest.mark.parametrize("offset,vfr,start,end", [(0, False, 3000, 5000), (5, True, 3000, 5000), (5, True, 5000, 9000)])
def test_video_seek_uses_absolute_normalized_presentation_times(tmp_path, offset, vfr, start, end):
    from fractions import Fraction

    from media_preview_generator.reprise.media import video_frames

    original = make_video(tmp_path / "original.mkv", filters="settb=1/1000,setpts=PTS+floor(N/2)*20" if vfr else None)
    path = Path(original.canonical_path)
    if offset:
        path = tmp_path / "offset.mkv"
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-i",
                original.canonical_path,
                "-map",
                "0",
                "-c",
                "copy",
                "-output_ts_offset",
                str(offset),
                "-y",
                str(path),
            ],
            check=True,
        )
    item = media_version(str(path), 9000, "{}")
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
                "format=start_time:frame=best_effort_timestamp_time",
                "-of",
                "json",
                str(path),
            ]
        )
    )
    origin = Fraction(info["format"]["start_time"])
    expected = [
        (Fraction(frame["best_effort_timestamp_time"]) - origin) * 1000
        for frame in info["frames"]
        if start <= (Fraction(frame["best_effort_timestamp_time"]) - origin) * 1000 < end
    ]
    frames = video_frames(item, 0, start, end, lambda: False, lambda: False, 1)
    assert expected
    assert [pts for pts, _ in frames] == expected


def test_actual_vfr_video_preserves_short_visual_change(tmp_path, catalog):
    vfr = r"settb=1/1000,setpts=PTS+floor(N/2)*20"
    gag = "drawbox=x=0:y=0:w=40:h=40:color=red:t=fill:enable='between(t,2.2,2.3)'," + vfr
    reference = make_video(tmp_path / "reference.mkv", filters=vfr)
    target = make_video(tmp_path / "target.mkv", filters=gag)
    probe = subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "frame=best_effort_timestamp_time",
            "-of",
            "json",
            target.canonical_path,
        ]
    )
    times = [round(float(f["best_effort_timestamp_time"]) * 1000) for f in json.loads(probe)["frames"]]
    assert len({b - a for a, b in zip(times, times[1:], strict=False)}) > 1
    result = run(target, reference, catalog)
    assert result.status == "complete", result.reasons
    assert skips(result), result.reasons
    assert not overlaps(2420, 2640, skips(result))  # actual VFR presentation support of changed frames


def test_only_a_new_size_or_modification_time_makes_a_version_stale(tmp_path):
    from dataclasses import replace

    from media_preview_generator.reprise.media import DecodeError, check_current

    path = tmp_path / "source"
    path.write_bytes(b"example")
    version = media_version(str(path), 1000, "{}")
    check_current(version)
    check_current(replace(version, inode=version.inode + 1, device=version.device + 1))  # a mover pass, a reboot
    for changed in (
        replace(version, size_bytes=version.size_bytes + 1),
        replace(version, mtime_ns=version.mtime_ns + 1),
    ):
        with pytest.raises(DecodeError):
            check_current(changed)


def test_duplicate_candidates_have_unique_store_keys(tmp_path, catalog):
    reference = make_video(tmp_path / "reference.mkv")
    target = make_video(tmp_path / "target.mkv")
    window = CandidateWindow("intro", 1000, 6000, "test")
    result = analyze(
        target,
        [window, window],
        [reference],
        store=catalog(target, reference),
        cancel_check=lambda: False,
        pause_check=lambda: False,
        progress_callback=None,
        ffmpeg_threads=1,
    )
    assert result.status == "complete", result.reasons
    assert len({e.id for e in result.evidence}) == len(result.evidence)
    assert len({r.id for r in result.ranges}) == len(result.ranges)


def test_aac_sample_trim_keeps_the_complete_requested_window(tmp_path):
    from fractions import Fraction

    from media_preview_generator.reprise.media import audio_pcm

    path = tmp_path / "audio.mka"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=16",
            "-c:a",
            "aac",
            "-y",
            str(path),
        ],
        check=True,
    )
    info = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-show_format", "-of", "json", str(path)]))
    origin = Fraction(info["format"]["start_time"])
    item = media_version(str(path), 16000, "{}")
    data = audio_pcm(item, ("audio", 0, 48000, 1), 5000, 14000, origin, lambda: False, lambda: False, 1)
    assert len(data) == 9 * 48000 * 4


def analyze_windows(target, windows, catalog, references=()):
    return analyze(
        target,
        windows,
        list(references),
        store=catalog(target, *references),
        cancel_check=lambda: False,
        pause_check=lambda: False,
        progress_callback=None,
        ffmpeg_threads=1,
    )


def test_a_plex_marker_nothing_confirmed_is_a_suggestion_while_other_windows_stay_protected(tmp_path, catalog):
    target = make_video(tmp_path / "target.mkv")
    windows = [CandidateWindow("intro", 1000, 6000, "plex"), CandidateWindow("credits", 6000, 7000, "ending-window")]
    result = analyze_windows(target, windows, catalog)
    assert result.status == "complete", result.reasons
    (suggestion,) = [r for r in result.ranges if r.origin == "plex"]
    assert (suggestion.kind, suggestion.action, suggestion.start_ms, suggestion.end_ms) == ("intro", "skip", 1000, 6000)
    assert (suggestion.review_status, suggestion.locked, suggestion.audio_stream_ids) == ("proposed", False, ("123",))
    assert suggestion.reason_code == "plex_suggestion" and suggestion.reason.startswith("Suggested by Plex")
    # the window Plex has no marker for keeps its ordinary protection
    assert [(r.kind, r.action) for r in result.ranges if r.origin == "detector"] == [("credits", "keep")]
    assert any("Plex suggested the intro (from 0:01 to 0:06)" in reason for reason in result.reasons)


def test_a_plex_marker_that_a_comparison_confirms_is_an_ordinary_proposal(tmp_path, catalog):
    reference = make_video(tmp_path / "reference.mkv")
    target = make_video(tmp_path / "target.mkv")
    result = analyze_windows(target, [CandidateWindow("intro", 1000, 6000, "plex")], catalog, [reference])
    assert result.status == "complete", result.reasons
    assert overlaps(3000, 4000, skips(result)), result.reasons
    assert not [r for r in result.ranges if r.origin == "plex"]


def test_a_plex_marker_is_fitted_to_the_file_and_one_that_does_not_fit_is_ignored(tmp_path, catalog):
    target = make_video(tmp_path / "target.mkv")
    windows = [CandidateWindow("intro", 6000, 7001, "plex"), CandidateWindow("credits", 3000, 9000, "plex")]
    result = analyze_windows(target, windows, catalog)
    assert result.status == "complete", result.reasons
    assert [(r.kind, r.start_ms, r.end_ms) for r in result.ranges] == [("intro", 6000, 7000)]
    assert "Plex's credits marker does not fit this file, so it was ignored." in result.reasons


def test_only_a_plex_marker_may_be_too_long_to_compare(tmp_path, catalog):
    from dataclasses import replace

    target = replace(make_video(tmp_path / "target.mkv"), duration_ms=400000)
    long_marker = analyze_windows(target, [CandidateWindow("credits", 1000, 250000, "plex")], catalog)
    assert long_marker.status == "complete", long_marker.reasons
    (suggestion,) = long_marker.ranges
    assert (suggestion.origin, suggestion.start_ms, suggestion.end_ms) == ("plex", 1000, 250000)
    assert any("longer than the 3 minutes Reprise can compare" in reason for reason in long_marker.reasons)
    long_guess = analyze_windows(target, [CandidateWindow("intro", 0, 250000, "opening-window")], catalog)
    assert long_guess.status == "failed"


def test_a_plex_marker_is_still_offered_when_the_episode_cannot_be_compared(tmp_path, catalog):
    reference = make_video(tmp_path / "reference.mkv")
    # a 200 ms hole in the sound's timeline: no exact comparison is possible, whatever Plex says about the intro
    target = make_video(tmp_path / "target.mkv", audio_filters="asetpts='PTS+if(gte(T,2),0.2/TB,0)'")
    marker = [CandidateWindow("intro", 1000, 6000, "plex")]
    result = analyze_windows(target, marker, catalog, [reference])
    assert result.status == "complete", result.reasons
    (suggestion,) = result.ranges
    assert (suggestion.origin, suggestion.action, suggestion.start_ms, suggestion.end_ms) == (
        "plex",
        "skip",
        1000,
        6000,
    )
    assert result.reasons[0] == "This episode can't be compared exactly with the others, so nothing could be confirmed."
    assert any("discontinuous" in reason for reason in result.reasons)  # the technical detail comes last
    # with no Plex marker the same failure is still a failure: there is nothing to offer instead
    guess = [CandidateWindow("intro", 1000, 6000, "opening-window")]
    assert analyze_windows(target, guess, catalog, [reference]).status == "failed"


def matches(result):
    return [e for e in result.evidence if e.verdict == "match"]


@pytest.mark.parametrize(
    "delayed,shift_ms,confirmed",
    [("target", 162, True), ("reference", 162, True), ("target", 400, False)],
)
def test_sound_out_of_step_with_the_picture_is_found_within_the_search_window_and_refused_beyond_it(
    tmp_path, catalog, delayed, shift_ms, confirmed
):
    from fractions import Fraction

    delay = f"adelay={shift_ms}:all=1"
    reference = make_video(
        tmp_path / "reference.mkv", sound=chirp(), audio_filters=delay if delayed == "reference" else None
    )
    target = make_video(tmp_path / "target.mkv", sound=chirp(), audio_filters=delay if delayed == "target" else None)
    result = run(target, reference, catalog)
    assert result.status == "complete", result.reasons
    if not confirmed:
        assert not skips(result) and not matches(result), result.ranges
        assert any(reason.startswith("Compared with 1 other episode") for reason in result.reasons)
        return
    assert overlaps(3000, 4000, skips(result)), result.reasons
    expected = Fraction(shift_ms if delayed == "reference" else -shift_ms)
    assert matches(result)
    for evidence in matches(result):
        assert evidence.metrics["reference_offset_ms"] == 0.0
        assert evidence.metrics["sound_offset_ms"] == float(expected)
        assert evidence.metrics["sound_offset_rational_ms"] == str(expected)


def test_a_unique_scene_stays_protected_at_a_found_sound_offset(tmp_path, catalog):
    reference = make_video(tmp_path / "reference.mkv", sound=chirp())
    unique = rf"if(between(t\,3\,4)\,0.3*sin(2*PI*(900*t+40*t*t))\,{CHIRP})"
    target = make_video(tmp_path / "target.mkv", sound=chirp(unique), audio_filters="adelay=162:all=1")
    result = run(target, reference, catalog)
    assert result.status == "complete", result.reasons
    assert skips(result), result.reasons
    assert not overlaps(3162, 4162, skips(result))  # the scene plays 162 ms later in the target
    assert all(e.metrics["sound_offset_ms"] == -162.0 for e in matches(result))


def test_a_different_sound_is_never_confirmed_whatever_the_picture_says(tmp_path, catalog):
    reference = make_video(tmp_path / "reference.mkv", sound=chirp())
    target = make_video(tmp_path / "target.mkv", sound=chirp("0.3*sin(2*PI*(300*t+9*t*t))"))
    result = run(target, reference, catalog)
    assert result.status == "complete", result.reasons
    assert not skips(result) and not matches(result), result.ranges


def test_checking_the_picture_only_where_the_sound_matched_changes_nothing(tmp_path, catalog, monkeypatch):
    from media_preview_generator.reprise import analysis

    reference = make_video(tmp_path / "reference.mkv", sound=chirp())
    # the sound is the same only between 2.5 s and 4.5 s: the picture check is narrowed to that stretch
    partial = rf"if(between(t\,2.5\,4.5)\,{CHIRP}\,0.3*sin(2*PI*(300*t+9*t*t)))"
    target = make_video(tmp_path / "target.mkv", sound=chirp(partial))
    spans = []
    original = analysis._picture_span

    def recording(hints, lo, hi, audio):
        spans.append(original(hints, lo, hi, audio))
        return spans[-1]

    monkeypatch.setattr(analysis, "_picture_span", recording)
    narrowed = run(target, reference, catalog)
    assert narrowed.status == "complete" and skips(narrowed), narrowed.reasons
    assert spans and all(2000 <= float(lo) and float(hi) <= 5000 for lo, hi in spans), spans
    monkeypatch.setattr(analysis, "_picture_span", lambda hints, lo, hi, audio: (lo, hi))
    whole = run(target, reference, catalog)
    assert (whole.ranges, whole.evidence) == (narrowed.ranges, narrowed.evidence)


def surround(lfe, centre=CHIRP):
    """A 5.1 track: the chirp on every main channel (the centre as given) and ``lfe`` on the subwoofer channel."""
    return f"aevalsrc={CHIRP}|{CHIRP}|{centre}|{lfe}|{CHIRP}|{CHIRP}:c=5.1:s=48000:d=7"


def test_a_difference_only_in_the_subwoofer_channel_never_vetoes_but_a_main_channel_does(tmp_path, catalog):
    reference = make_video(tmp_path / "reference.mkv", sound=surround("0.3*sin(2*PI*40*t)"), layout="5.1")
    target = make_video(tmp_path / "target.mkv", sound=surround("0.3*sin(2*PI*55*t+1)"), layout="5.1")
    result = run(target, reference, catalog)
    assert result.status == "complete", result.reasons
    assert overlaps(3000, 4000, skips(result)), result.reasons
    assert matches(result)
    for evidence in matches(result):
        assert evidence.metrics["checked_channels"] == {"123": ["FL", "FR", "FC", "BL", "BR"]}
    centre = make_video(
        tmp_path / "centre.mkv",
        sound=surround("0.3*sin(2*PI*40*t)", centre="0.3*sin(2*PI*(300*t+9*t*t))"),
        layout="5.1",
    )
    refused = run(centre, reference, catalog)
    assert refused.status == "complete", refused.reasons
    assert not skips(refused) and not matches(refused), refused.ranges


@pytest.mark.parametrize(
    "shared,suggested",
    [((2.5, 4.5), True), ((2.0, 5.5), False)],  # about a quarter of the marker is confirmed, then well over half
)
def test_a_plex_marker_stays_a_suggestion_unless_half_of_it_is_confirmed(tmp_path, catalog, shared, suggested):
    reference = make_video(tmp_path / "reference.mkv", sound=chirp())
    partial = rf"if(between(t\,{shared[0]}\,{shared[1]})\,{CHIRP}\,0.3*sin(2*PI*(300*t+9*t*t)))"
    target = make_video(tmp_path / "target.mkv", sound=chirp(partial))
    result = analyze_windows(target, [CandidateWindow("intro", 1000, 6000, "plex")], catalog, [reference])
    assert result.status == "complete", result.reasons
    plex = [r for r in result.ranges if r.origin == "plex"]
    if suggested:
        assert [(r.kind, r.start_ms, r.end_ms, r.review_status) for r in plex] == [("intro", 1000, 6000, "proposed")]
        assert all(r.origin == "plex" for r in result.ranges)
        assert not matches(result)  # the confirmed fragment is dropped, not proposed on its own
        assert any("confirmed only" in reason and "less than half" in reason for reason in result.reasons)
        assert not any(reason.startswith("Compared with 1 other episode") for reason in result.reasons)
    else:
        assert not plex
        assert overlaps(3000, 4000, skips(result)), result.reasons
        assert [r.kind for r in result.ranges if r.action == "keep"]  # the unconfirmed ends stay protected
        assert not any("Plex suggested" in reason for reason in result.reasons)
