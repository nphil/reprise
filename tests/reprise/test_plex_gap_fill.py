"""Holes between two confirmed pieces of a Plex marker are filled when short; nothing else ever is.

Real synthetic media, no mocks: a 16-second episode whose sound differs from the reference's over chosen stretches.
Each differing stretch vetoes itself plus the 250 ms change guard on both sides, so a stretch of ``w`` seconds
leaves a hole of ``w + 0.6`` seconds between the two confirmed runs around it.
"""

from dataclasses import replace

import pytest

from media_preview_generator.markers.store import MarkerStore
from media_preview_generator.reprise import analysis
from media_preview_generator.reprise.analysis import _bridges, analyze
from media_preview_generator.reprise.models import AnalysisResult, CandidateWindow, PlaybackIdentity
from media_preview_generator.reprise.policy import eligible_ranges
from media_preview_generator.reprise.store import RepriseStore
from media_preview_generator.reprise.verification_profile import (
    POLICY_VERSION,
    PROFILE_ID,
    PROFILE_SPEC,
)
from tests.reprise.test_analysis_recurrence import CHIRP, make_video

SECONDS = 16
DIFFERENT = "0.3*sin(2*PI*(300*t+9*t*t))"
SOURCE = f"testsrc2=size=96x64:rate=10:duration={SECONDS}"
PLEX = CandidateWindow("intro", 1000, 15000, "plex")
pytestmark = pytest.mark.timeout(150)  # addopts has --timeout=30; each case decodes 16 s of media several times


def sound(differing=()):
    expression = CHIRP
    for start, end in differing:
        expression = rf"if(between(t\,{start}\,{end})\,{DIFFERENT}\,{expression})"
    return f"aevalsrc={expression}:s=48000:d={SECONDS}"


def episode(path, differing=()):
    return replace(make_video(path, sound=sound(differing), source=SOURCE), duration_ms=SECONDS * 1000)


@pytest.fixture(scope="module")
def reference(tmp_path_factory):
    return episode(tmp_path_factory.mktemp("reference") / "reference.mkv")


@pytest.fixture
def compare(tmp_path, reference):
    markers = MarkerStore(str(tmp_path / "markers.db"))
    store = RepriseStore(markers)

    def run(differing, windows=(PLEX,), owner_keeps=()):
        target = episode(tmp_path / "target.mkv", differing)
        for number, version in enumerate((target, reference), start=1):
            key = str(number)
            identity = PlaybackIdentity(
                "machine", key, key, key, "/part/" + key, version.size_bytes, version.duration_ms, "123"
            )
            store.register_media(
                version,
                identity,
                server_id="server",
                metadata={"library_id": "library", "show_key": "show", "season": 1, "episode": number},
            )
        draft = analyze(
            target,
            list(windows),
            [reference],
            store=store,
            cancel_check=lambda: False,
            pause_check=lambda: False,
            progress_callback=None,
            ffmpeg_threads=1,
            owner_keeps=owner_keeps,
        )
        assert draft.status == "complete", draft.reasons
        return draft

    yield run
    markers.close()


def filled(draft):
    return [e for e in draft.evidence if e.metrics["bridged"]]


def skips(draft):
    return [r for r in draft.ranges if r.action == "skip"]


def test_the_profile_is_v7_and_says_how_far_a_gap_may_be_filled():
    assert PROFILE_ID == "native-signal-review-first-v7"
    assert PROFILE_SPEC["bridge"]["max_gap_ms"] == 3000
    assert PROFILE_SPEC["bridge"]["suggested_window"] is True


def test_short_gaps_inside_a_plex_marker_are_filled_and_recorded(compare):
    draft = compare([(5.0, 5.5), (8.0, 8.2)])
    holes = filled(draft)
    assert len(holes) == 2
    for hole in holes:
        m = hole.metrics
        assert (m["bridge_start_ms"], m["bridge_end_ms"]) == (hole.start_ms, hole.end_ms)
        assert m["bridge_length_ms"] == hole.end_ms - hole.start_ms <= 3000
        assert hole.verdict == "match" and hole.reason_code == "native_signal_bridged"
        assert hole.reference_version_id and hole.reference_start_ms is None and m["reference_offset_ms"] is None
    assert [(h.start_ms <= 5250 <= h.end_ms, h.start_ms <= 8100 <= h.end_ms) for h in holes] == [
        (True, False),
        (False, True),
    ]
    # the skip is one unbroken stretch from the marker's first confirmed instant to its last
    pieces = sorted((r.start_ms, r.end_ms) for r in skips(draft))
    assert all(a[1] == b[0] for a, b in zip(pieces, pieces[1:], strict=False))
    assert pieces[0][0] == 1000 and pieces[-1][1] <= 15000
    assert all(r.review_status == "proposed" and r.origin == "detector" for r in draft.ranges)
    assert "Filled 2 short gaps (longest 1.1 s) inside Plex's intro." in draft.reasons
    assert all(e.metrics["verification_profile"] == PROFILE_ID for e in draft.evidence)


def test_one_filled_gap_is_named_in_the_singular(compare):
    draft = compare([(5.0, 5.5)])
    assert "Filled 1 short gap (1.1 s) inside Plex's intro." in draft.reasons


def test_an_approved_filled_gap_is_eligible_like_any_confirmed_piece(compare):
    draft = compare([(5.0, 5.5)])
    approved = tuple(replace(r, review_status="approved") if r.action == "skip" else r for r in draft.ranges)
    result = AnalysisResult("m", "a", 1, "complete", "current", "s", POLICY_VERSION, approved, draft.evidence, ())
    eligible = eligible_ranges(result, "123")
    assert eligible[0].start_ms == 1000
    assert all(a.end_ms == b.start_ms for a, b in zip(eligible, eligible[1:], strict=False))
    assert any(r.start_ms <= 5250 < r.end_ms for r in eligible)
    # a keep the owner adds later still wins over the filled gap
    kept = replace(approved[0], id="owner-keep", action="keep", start_ms=5000, end_ms=5200, origin="manual")
    held = eligible_ranges(replace(result, ranges=approved + (kept,)), "123")
    assert not any(r.start_ms < 5200 and r.end_ms > 5000 for r in held)


@pytest.mark.parametrize(
    "width,hole_seconds,is_filled",
    [
        (2.4, 3.0, True),  # a hole of exactly three seconds is still filled
        (2.5, 3.1, False),
        (2.9, 3.5, False),
    ],
)
def test_a_gap_is_filled_up_to_three_seconds_and_not_beyond(compare, width, hole_seconds, is_filled):
    draft = compare([(5.0, round(5.0 + width, 1))])
    if is_filled:
        (hole,) = filled(draft)
        assert hole.end_ms - hole.start_ms == round(hole_seconds * 1000)
    else:
        assert not filled(draft)
        assert not any(reason.startswith("Filled") for reason in draft.reasons)
        # the unconfirmed stretch stays a protected keep, whatever was confirmed around it
        assert any(r.action == "keep" and r.start_ms <= 5000 and r.end_ms >= 5000 + width * 1000 for r in draft.ranges)
        assert not any(
            r.action == "skip" and r.start_ms < 5000 + width * 1000 and r.end_ms > 5000 for r in draft.ranges
        )


def test_a_gap_that_reaches_outside_the_plex_marker_is_not_filled(compare):
    # the sound differs around 8 s, where the Plex intro ends and an ordinary window begins
    windows = (CandidateWindow("intro", 1000, 8000, "plex"), CandidateWindow("credits", 8000, 15000, "test"))
    draft = compare([(7.7, 8.1)], windows)
    assert not filled(draft)
    assert any(r.action == "keep" and r.start_ms <= 7600 and r.end_ms >= 8000 for r in draft.ranges)
    assert not any(r.action == "skip" and r.start_ms < 8000 and r.end_ms > 7500 for r in draft.ranges)
    assert not any(reason.startswith("Filled") for reason in draft.reasons)


def test_a_range_the_owner_keeps_blocks_filling_across_it(compare):
    assert len(filled(compare([(5.0, 5.5)]))) == 1
    blocked = compare([(5.0, 5.5)], owner_keeps=[(5100, 5200)])
    assert not filled(blocked)
    assert not any(r.action == "skip" and r.start_ms < 5200 and r.end_ms > 5100 for r in blocked.ranges)
    # a keep elsewhere does not stop it
    assert len(filled(compare([(5.0, 5.5)], owner_keeps=[(12000, 12500)]))) == 1


def test_without_a_plex_marker_nothing_is_filled(compare):
    draft = compare([(5.0, 5.5)], (CandidateWindow("intro", 1000, 15000, "opening-window"),))
    assert not filled(draft)
    assert any(r.action == "keep" and r.start_ms <= 5000 and r.end_ms >= 5500 for r in draft.ranges)
    assert not any(reason.startswith("Filled") for reason in draft.reasons)


def test_filled_time_counts_toward_the_half_a_plex_marker_needs(compare, monkeypatch):
    # six differing stretches: confirmed pieces alone cover under half of the 14-second marker, the gaps between them the rest
    differing = [(start, start + 0.7) for start in (2, 4, 6, 8, 10, 12)]
    monkeypatch.setattr(analysis, "BRIDGE_MAX_GAP_MS", 0)
    unfilled = compare(differing)
    assert [r.origin for r in unfilled.ranges] == ["plex"] and unfilled.ranges[0].review_status == "proposed"
    assert any("confirmed only" in reason and "less than half" in reason for reason in unfilled.reasons)
    monkeypatch.undo()
    draft = compare(differing)
    assert len(filled(draft)) == 6
    assert not any(r.origin == "plex" for r in draft.ranges)
    assert skips(draft) and all(r.origin == "detector" for r in skips(draft))
    assert "Filled 6 short gaps (longest 1.3 s) inside Plex's intro." in draft.reasons
    assert not any("Plex suggested" in reason for reason in draft.reasons)


def run_of(start, end):
    return (start, end, None, None, [], {}, None, {})


@pytest.mark.parametrize(
    "runs,keeps,expected",
    [
        ([(1000, 2000), (5000, 6000)], [], [(2000, 5000)]),  # exactly 3.0 s
        ([(1000, 2000), (5001, 6000)], [], []),  # 3.001 s
        ([(1000, 2000), (2000, 3000)], [], []),  # no hole at all
        ([(1000, 2000), (3000, 4000), (5500, 6000), (7000, 8000)], [], [(2000, 3000), (4000, 5500), (6000, 7000)]),
        ([(1000, 2000), (3000, 4000)], [(2400, 2500)], []),  # an owner keep inside the hole
        ([(1000, 2000), (3000, 4000)], [(1500, 2001)], []),  # overlapping the hole's first millisecond
        ([(1000, 2000), (3000, 4000)], [(1000, 2000), (3000, 4000)], [(2000, 3000)]),  # keeps merely adjacent
        ([(500, 2000), (3000, 4000)], [], [(2000, 3000)]),  # a run reaching before the marker still bounds a hole
        ([(1000, 2000), (3000, 9500)], [], [(2000, 3000)]),
    ],
)
def test_which_holes_between_runs_inside_a_marker_may_be_filled(runs, keeps, expected):
    window = CandidateWindow("intro", 1000, 9000, "plex")
    holes = _bridges(window, [run_of(*r) for r in runs], keeps)
    assert [(lo, hi) for lo, hi, *_ in holes] == expected


def test_holes_touching_the_marker_edges_are_never_filled():
    window = CandidateWindow("intro", 2000, 8000, "plex")
    # a run ends before the marker begins, the next starts inside it; the hole starts outside the marker
    assert _bridges(window, [run_of(500, 1500), run_of(2500, 3000)], []) == []
    # the hole starts inside the marker and ends after it
    assert _bridges(window, [run_of(6000, 7000), run_of(8500, 9000)], []) == []
    # the marker's own first and last seconds, with no run on the far side, are not between two runs
    assert _bridges(window, [run_of(3000, 4000)], []) == []
