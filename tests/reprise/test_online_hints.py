"""Online intro/credits times: hints for where to search, and approve-only suggestions.

The media is synthetic and lossless (no mocks of the comparison itself): a 16 second "episode" whose sound and picture
repeat at another place in a longer reference. The databases' answers come from fake clients that count their calls.
"""

import json
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from media_preview_generator.markers.models import Candidate, MarkerType, MediaIds, Source
from media_preview_generator.markers.sources.online import LookupResult
from media_preview_generator.markers.store import MarkerStore
from media_preview_generator.reprise import analysis, online
from media_preview_generator.reprise.analysis import _hint_range, _with_online, analyze
from media_preview_generator.reprise.identity import media_version
from media_preview_generator.reprise.models import (
    AnalysisResult,
    CandidateWindow,
    OnlineWindow,
    PlaybackIdentity,
    ReviewRefused,
)
from media_preview_generator.reprise.policy import eligible_ranges
from media_preview_generator.reprise.service import RepriseService, _analysis_summary, _range_counts
from media_preview_generator.reprise.store import RepriseStore
from media_preview_generator.reprise.verification_profile import POLICY_VERSION
from tests.reprise.test_plex_gap_fill import SECONDS, episode, sound

pytestmark = pytest.mark.timeout(400)

CLIP = f"testsrc2=size=96x64:rate=10:duration={SECONDS}"
STREAMS = json.dumps({"audio_streams": [{"plex_id": "123", "index": 1}]})
INTRO = "intro"


def media(path: Path, lead: int = 0, clip: bool = True):
    """A lossless file: ``lead`` seconds of other picture and sound, then (when ``clip``) the 16 s clip whose sound is
    the shared chirp. With no lead it is the clip alone; with no clip it is only other content."""
    parts = []
    if lead or not clip:
        filler = lead if clip else SECONDS
        parts.append(
            (f"testsrc=size=96x64:rate=10:duration={filler}", f"aevalsrc=0.3*sin(2*PI*700*t):s=48000:d={filler}")
        )
    if clip:
        parts.append((CLIP, sound()))
    command = ["ffmpeg", "-nostdin", "-v", "error", "-threads", "1"]
    for video, audio in parts:
        command += ["-f", "lavfi", "-i", video, "-f", "lavfi", "-i", audio]
    chains = "".join(f"[{2 * n}:v]format=yuv420p,setparams=range=tv[v{n}];" for n in range(len(parts)))
    chains += "".join(f"[v{n}]" for n in range(len(parts))) + f"concat=n={len(parts)}:v=1:a=0[v];"
    chains += "".join(f"[{2 * n + 1}:a]" for n in range(len(parts))) + f"concat=n={len(parts)}:v=0:a=1[a]"
    command += ["-filter_complex", chains, "-map", "[v]", "-map", "[a]", "-fps_mode", "passthrough"]
    command += ["-enc_time_base", "1:1000", "-c:v", "ffv1", "-chroma_sample_location", "left", "-c:a", "flac"]
    command += ["-channel_layout", "mono", "-threads", "1", "-y", str(path)]
    subprocess.run(command, check=True)
    seconds = (lead + SECONDS) if clip and lead else SECONDS
    return media_version(str(path), seconds * 1000, STREAMS)


@pytest.fixture(scope="module")
def target(tmp_path_factory):
    return media(tmp_path_factory.mktemp("target") / "target.mkv")


@pytest.fixture(scope="module")
def far_reference(tmp_path_factory):
    """The clip again, 80 s into a longer episode: outside the 30 s the default search covers."""
    return media(tmp_path_factory.mktemp("far") / "far.mkv", lead=80)


@pytest.fixture(scope="module")
def unrelated(tmp_path_factory):
    return media(tmp_path_factory.mktemp("unrelated") / "unrelated.mkv", clip=False)


@pytest.fixture
def library(tmp_path):
    markers = MarkerStore(str(tmp_path / "markers.db"))
    store = RepriseStore(markers)

    def register(*versions):
        for number, version in enumerate(versions, start=1):
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
        return store

    yield register
    markers.close()


def compare(library, target_version, reference, windows, found=None, **extra):
    store = library(target_version, reference)
    draft = analyze(
        target_version,
        list(windows),
        [reference],
        store=store,
        cancel_check=lambda: False,
        pause_check=lambda: False,
        progress_callback=None,
        ffmpeg_threads=1,
        online=found,
        **extra,
    )
    assert draft.status == "complete", draft.reasons
    return draft, store


def skips(draft):
    return [r for r in draft.ranges if r.action == "skip"]


def suggestions(draft):
    return [r for r in draft.ranges if r.origin in ("plex", "online")]


def window(kind, start, end, *names, dissent=()):
    return OnlineWindow(kind, start, end, tuple(names), tuple(dissent))


PLEX = CandidateWindow(INTRO, 1000, 15000, "plex")


# --- hints -----------------------------------------------------------------------------------------------------------


def test_an_online_position_finds_an_intro_outside_the_default_search_that_strict_checks_confirm(
    library, target, far_reference
):
    plain, _ = compare(library, target, far_reference, [PLEX])
    assert not [r for r in plain.ranges if r.origin == "detector" and r.action == "skip"]
    assert [r.origin for r in plain.ranges] == ["plex"]  # nothing was found 80 s away: Plex's marker is a suggestion

    found = {
        target.id: (window(INTRO, 1000, 15000, "IntroDB"),),
        far_reference.id: (window(INTRO, 80000, 94000, "IntroDB"),),
    }
    hinted, _ = compare(library, target, far_reference, [PLEX], found)
    confirmed = [r for r in hinted.ranges if r.origin == "detector" and r.action == "skip"]
    assert confirmed and confirmed[0].start_ms >= 1000 and confirmed[-1].end_ms <= 15000
    assert sum(r.end_ms - r.start_ms for r in confirmed) >= 7000  # at least half of the marker, by sound and picture
    assert not suggestions(hinted)
    # it is the strict check that decided: evidence names the reference and the far-away offset (80 s) it matched at
    matches = [e for e in hinted.evidence if e.verdict == "match"]
    assert matches and all(e.reference_version_id == far_reference.id for e in matches)
    assert all(abs(e.metrics["reference_offset_ms"] - 80000) < 500 for e in matches if not e.metrics["bridged"])
    assert any("showed where to look in another episode" in reason for reason in hinted.reasons)


def test_a_hint_is_not_searched_once_the_expected_stretch_is_confirmed(
    library, target, far_reference, tmp_path, monkeypatch
):
    near = media(tmp_path / "near.mkv")  # the same clip in the same place: the usual neighbourhood confirms it
    scanned = []
    real = analysis._scan

    def spy(item, stream, start, end, *args, **kwargs):
        scanned.append((item.id, start, end))
        return real(item, stream, start, end, *args, **kwargs)

    monkeypatch.setattr(analysis, "_scan", spy)
    found = {
        target.id: (window(INTRO, 1000, 15000, "IntroDB"),),
        near.id: (window(INTRO, 1000, 15000, "IntroDB"),),
        far_reference.id: (window(INTRO, 80000, 94000, "IntroDB"),),
    }
    store = library(target, near, far_reference)
    draft = analyze(
        target,
        [PLEX],
        [near, far_reference],
        store=store,
        cancel_check=lambda: False,
        pause_check=lambda: False,
        progress_callback=None,
        ffmpeg_threads=1,
        online=found,
    )
    assert draft.status == "complete", draft.reasons
    assert [r for r in draft.ranges if r.origin == "detector" and r.action == "skip"], (draft.reasons, scanned)
    assert any(item == far_reference.id and start == 0 for item, start, _ in scanned)  # the usual neighbourhood
    assert not any(item == far_reference.id and start >= 40000 for item, start, _ in scanned)  # no hinted search
    assert not any("showed where to look" in reason for reason in draft.reasons)


def failing_scan(monkeypatch, item_id, when):
    """Make the scan of one file raise the decoder error a file whose audio ends before its picture gives."""
    real = analysis._scan

    def scan(item, stream, start, end, *args, **kwargs):
        if item.id == item_id and when(start, end):
            raise analysis.decoder.DecodeError("Native audio decoder did not cover the window")
        return real(item, stream, start, end, *args, **kwargs)

    monkeypatch.setattr(analysis, "_scan", scan)


def test_an_online_window_that_cannot_be_compared_stays_a_suggestion_and_fails_nothing(
    library, target, unrelated, monkeypatch
):
    opening = CandidateWindow(INTRO, 0, 8000, analysis.OPENING_WINDOW)
    found = {target.id: (window(INTRO, 2000, 12000, "SkipDB"),)}
    failing_scan(monkeypatch, target.id, lambda start, end: end == 14000)  # the online window and its context
    draft, _ = compare(library, target, unrelated, [opening], found)
    assert draft.status == "complete"
    [suggestion] = suggestions(draft)
    assert (suggestion.origin, suggestion.start_ms, suggestion.end_ms) == ("online", 2000, 12000)
    assert analysis.ONLINE_NOT_COMPARED.format(kind=INTRO) in draft.reasons
    assert analysis.UNCOMPARABLE_MEDIA not in draft.reasons


def test_a_hinted_search_that_fails_is_only_noted(library, target, far_reference, monkeypatch):
    found = {
        target.id: (window(INTRO, 1000, 15000, "IntroDB"),),
        far_reference.id: (window(INTRO, 80000, 94000, "IntroDB"),),
    }
    failing_scan(monkeypatch, far_reference.id, lambda start, end: start >= 40000)  # only where the hint points
    draft, _ = compare(library, target, far_reference, [PLEX], found)
    assert draft.status == "complete" and [r.origin for r in draft.ranges] == ["plex"]
    assert analysis.ONLINE_HINT_FAILED.format(kind=INTRO) in draft.reasons


def test_a_plex_window_that_cannot_be_compared_still_fails_the_comparison_as_before(
    library, target, unrelated, monkeypatch
):
    failing_scan(monkeypatch, unrelated.id, lambda start, end: start == 0)  # where Plex's marker is searched
    draft, _ = compare(library, target, unrelated, [PLEX], {target.id: (window(INTRO, 1000, 15000, "IntroDB"),)})
    assert analysis.COMPARISON_FAILED in draft.reasons or analysis.COMPARISON_UNREADABLE in draft.reasons
    assert [r.origin for r in suggestions(draft)] == ["plex"]


def test_a_hint_never_confirms_what_the_checks_do_not(library, target, unrelated):
    found = {target.id: (window(INTRO, 1000, 15000, "IntroDB"),), unrelated.id: (window(INTRO, 2000, 9000, "IntroDB"),)}
    draft, _ = compare(library, target, unrelated, [PLEX], found)
    assert not [r for r in draft.ranges if r.origin == "detector" and r.action == "skip"]
    assert not any("showed where to look" in reason for reason in draft.reasons)


def test_without_online_data_the_result_is_exactly_what_it_was(library, target, far_reference):
    baseline, _ = compare(library, target, far_reference, [PLEX])
    for found in ({}, {"someone-else": (window(INTRO, 1000, 15000, "IntroDB"),)}):
        again, _ = compare(library, target, far_reference, [PLEX], found)
        assert (again.status, again.reasons, again.ranges, again.evidence) == (
            baseline.status,
            baseline.reasons,
            baseline.ranges,
            baseline.evidence,
        )


@pytest.mark.parametrize(
    "online_target,online_reference,expected",
    [
        # aligned by how far the reference's intro sits from the target's, then the usual radius around it
        (window(INTRO, 1000, 15000, "A"), window(INTRO, 80000, 94000, "A"), (48000, 125000)),
        # the target has no online position: around the reference's own
        (None, window(INTRO, 80000, 94000, "A"), (50000, 124000)),
        # a reference with no position gives no hint
        (window(INTRO, 1000, 15000, "A"), None, None),
        # the other kind says nothing about an intro
        (window(INTRO, 1000, 15000, "A"), window("credits", 80000, 94000, "A"), None),
    ],
)
def test_where_a_hint_points(online_target, online_reference, expected):
    at = (online_target,) if online_target else ()
    there = (online_reference,) if online_reference else ()
    assert _hint_range(CandidateWindow(INTRO, 1000, 15000, "plex"), at, there, 125000) == expected


# --- windows ---------------------------------------------------------------------------------------------------------


def test_online_times_become_a_window_and_the_stand_in_opening_window_gives_way_to_them():
    opening = CandidateWindow(INTRO, 0, 90000, analysis.OPENING_WINDOW)
    windows, names, notes = _with_online([opening], (window(INTRO, 30000, 60000, "IntroDB"),))
    assert [(w.start_ms, w.end_ms, w.source) for w in windows] == [
        (0, 30000, analysis.OPENING_WINDOW),
        (30000, 60000, "online"),
        (60000, 90000, analysis.OPENING_WINDOW),
    ]
    assert names == {CandidateWindow(INTRO, 30000, 60000, "online"): ("IntroDB",)} and notes == []


def test_online_times_that_agree_with_plex_name_both_and_add_no_window():
    plex = CandidateWindow(INTRO, 1000, 15000, "plex")
    for agreeing in ((3000, 19000), (3500, 16000), (1000, 15000)):  # each end within 5 s of Plex's
        windows, names, notes = _with_online([plex], (window(INTRO, *agreeing, "IntroDB"),))
        assert windows == [plex] and names == {plex: ("Plex", "IntroDB")} and notes == []
    windows, names, _ = _with_online([plex], (window(INTRO, 2000, 14000, "IntroDB", "SkipDB"),))
    assert windows == [plex] and names == {plex: ("Plex", "IntroDB", "SkipDB")}
    windows, names, notes = _with_online([plex], (window(INTRO, 3000, 21000, "IntroDB"),))  # 6 s apart at the end
    assert windows == [plex] and names == {plex: ("Plex",)} and len(notes) == 1


def test_online_times_that_overlap_a_different_plex_marker_are_not_added_but_noted():
    plex = CandidateWindow(INTRO, 1000, 15000, "plex")
    windows, names, notes = _with_online([plex], (window(INTRO, 9000, 30000, "IntroDB"),))
    assert windows == [plex] and names == {plex: ("Plex",)}
    assert notes == ["IntroDB put the intro from 0:09 to 0:30, which differs from Plex's marker; only Plex's was used."]
    elsewhere, names, _ = _with_online([plex], (window(INTRO, 40000, 60000, "IntroDB"),))
    assert [(w.start_ms, w.source) for w in elsewhere] == [(1000, "plex"), (40000, "online")]
    assert names[CandidateWindow(INTRO, 40000, 60000, "online")] == ("IntroDB",)


def test_sources_that_disagree_are_named_in_a_note():
    windows, names, notes = _with_online([], (window(INTRO, 1000, 9000, "SkipDB", dissent=("IntroDB",)),))
    assert notes == ["IntroDB gave other times for the intro than SkipDB; SkipDB's were used."]
    assert len(windows) == 1


# --- suggestions -----------------------------------------------------------------------------------------------------


def test_online_times_nothing_confirmed_are_an_approve_only_suggestion(library, target, unrelated):
    opening = CandidateWindow(INTRO, 0, 8000, analysis.OPENING_WINDOW)
    found = {target.id: (window(INTRO, 2000, 12000, "IntroDB"),)}
    draft, store = compare(library, target, unrelated, [opening], found)
    [suggestion] = suggestions(draft)
    assert (suggestion.origin, suggestion.reason_code, suggestion.suggested_by) == (
        "online",
        "online_suggestion",
        ("IntroDB",),
    )
    assert (suggestion.action, suggestion.review_status, suggestion.locked) == ("skip", "proposed", False)
    assert (suggestion.start_ms, suggestion.end_ms) == (2000, 12000)
    assert suggestion.reason == "Suggested by IntroDB — not confirmed by Reprise."
    [evidence] = [e for e in draft.evidence if e.id in suggestion.evidence_ids]
    assert evidence.verdict == "unknown" and evidence.metrics["candidate_source"] == "online"
    assert evidence.metrics["suggested_by"] == ["IntroDB"]
    assert any(reason.startswith("IntroDB suggested the intro (from 0:02 to 0:12).") for reason in draft.reasons)
    assert not any("detector" == r.origin and r.action == "skip" for r in draft.ranges)

    run = store.begin_analysis(target.id, "job", "signature")
    result = store.finish_analysis(run, draft.evidence, draft.ranges, target.id, draft.reasons)
    assert eligible_ranges(result, "123") == ()
    approve = [{"range_id": suggestion.id, "review_status": "approved"}]
    with pytest.raises(ReviewRefused, match="deliberate approval"):
        store.save_review(target.id, result.result_revision, approve, "owner")
    approved = store.save_review(
        target.id, result.result_revision, [{**approve[0], "acknowledge_suggestion": True}], "owner"
    )
    assert [(r.start_ms, r.end_ms, r.origin) for r in eligible_ranges(approved, "123")] == [(2000, 12000, "online")]
    # a keep still vetoes it, as for any approved suggestion
    keep = {"range_id": None, "action": "keep", "kind": INTRO, "start_ms": 4000, "end_ms": 5000}
    kept = store.save_review(
        target.id,
        approved.result_revision,
        [{"range_id": suggestion.id, "review_status": "approved"}, keep],
        "owner",
    )
    assert [(r.start_ms, r.end_ms) for r in eligible_ranges(kept, "123")] == [(2000, 4000), (5000, 12000)]


def test_the_older_acknowledgement_key_still_approves_an_online_suggestion(library, target, unrelated):
    found = {target.id: (window(INTRO, 2000, 12000, "SkipDB"),)}
    draft, store = compare(
        library, target, unrelated, [CandidateWindow(INTRO, 0, 8000, analysis.OPENING_WINDOW)], found
    )
    [suggestion] = suggestions(draft)
    run = store.begin_analysis(target.id, "job", "signature")
    result = store.finish_analysis(run, draft.evidence, draft.ranges, target.id, draft.reasons)
    approved = store.save_review(
        target.id,
        result.result_revision,
        [{"range_id": suggestion.id, "review_status": "approved", "acknowledge_plex_suggestion": True}],
        "owner",
    )
    assert len(eligible_ranges(approved, "123")) == 1


def test_plex_and_online_that_agree_are_one_suggestion_naming_both(library, target, unrelated):
    found = {target.id: (window(INTRO, 1500, 14500, "IntroDB"),)}
    draft, _ = compare(library, target, unrelated, [PLEX], found)
    [suggestion] = suggestions(draft)
    assert (suggestion.origin, suggestion.reason_code) == ("plex", "plex_suggestion")
    assert suggestion.suggested_by == ("Plex", "IntroDB")
    assert suggestion.reason == "Suggested by Plex and IntroDB — not confirmed by Reprise."
    assert (suggestion.start_ms, suggestion.end_ms) == (1000, 15000)
    assert any(reason.startswith("Plex and IntroDB suggested the intro") for reason in draft.reasons)


def test_plex_alone_reads_exactly_as_it_did(library, target, unrelated):
    draft, _ = compare(library, target, unrelated, [PLEX])
    [suggestion] = suggestions(draft)
    assert suggestion.suggested_by == ("Plex",) and suggestion.reason == "Suggested by Plex — not confirmed by Reprise."
    assert any(reason.startswith("Plex suggested the intro (from 0:01 to 0:15).") for reason in draft.reasons)


def test_a_different_online_answer_far_from_plexs_is_a_second_suggestion(library, target, unrelated):
    plex = CandidateWindow(INTRO, 1000, 4000, "plex")
    found = {target.id: (window(INTRO, 8000, 15000, "SkipDB"),)}
    draft, _ = compare(library, target, unrelated, [plex], found)
    assert sorted((r.origin, r.start_ms, r.end_ms, r.suggested_by) for r in suggestions(draft)) == [
        ("online", 8000, 15000, ("SkipDB",)),
        ("plex", 1000, 4000, ("Plex",)),
    ]


def test_the_half_confirmed_rule_and_gap_filling_apply_to_an_online_window(library, tmp_path):
    reference = episode(tmp_path / "reference.mkv")
    opening = CandidateWindow(INTRO, 1000, 15000, analysis.OPENING_WINDOW)
    found = lambda version: {version.id: (window(INTRO, 1000, 15000, "IntroDB"),)}  # noqa: E731
    # two short holes in the sound are filled: the window is confirmed, as Plex's marker would be
    bridged_target = episode(tmp_path / "bridged.mkv", [(5.0, 5.5), (8.0, 8.2)])
    draft, _ = compare(library, bridged_target, reference, [opening], found(bridged_target))
    holes = [e for e in draft.evidence if e.metrics["bridged"]]
    assert len(holes) == 2 and all(e.metrics["candidate_source"] == "online" for e in holes)
    assert "Filled 2 short gaps (longest 1.1 s) inside the intro IntroDB suggested." in draft.reasons
    assert not suggestions(draft)
    # a long difference leaves under half confirmed: the window is only a suggestion and the fragments are dropped
    broken = episode(tmp_path / "broken.mkv", [(3.0, 12.0)])
    draft, _ = compare(library, broken, reference, [opening], found(broken))
    [suggestion] = suggestions(draft)
    assert (suggestion.origin, suggestion.start_ms, suggestion.end_ms) == ("online", 1000, 15000)
    assert not [r for r in draft.ranges if r.origin == "detector" and r.action == "skip" and 1000 <= r.start_ms < 15000]
    assert any("less than half" in reason for reason in draft.reasons)


def test_a_failed_comparison_still_offers_the_online_times(library, target, unrelated):
    store = library(target, unrelated)
    found = {target.id: (window(INTRO, 2000, 12000, "IntroDB"),)}
    broken = replace(unrelated, canonical_path=str(Path(unrelated.canonical_path).with_name("gone.mkv")))
    draft = analyze(
        target,
        [CandidateWindow(INTRO, 0, 8000, analysis.OPENING_WINDOW)],
        [broken],
        store=store,
        cancel_check=lambda: False,
        pause_check=lambda: False,
        progress_callback=None,
        ffmpeg_threads=1,
        online=found,
    )
    assert draft.status == "complete" and [r.origin for r in suggestions(draft)] == ["online"]


# --- summaries -------------------------------------------------------------------------------------------------------


def test_summaries_and_counts_name_who_suggested():
    plex = analysis.DecisionRange(
        "p", INTRO, "skip", 1000, 5000, "proposed", False, "plex", "plex_suggestion", "", (), ()
    )
    both = replace(plex, id="b", origin="plex", suggested_by=("Plex", "IntroDB"))
    online_only = replace(
        plex, id="o", origin="online", reason_code="online_suggestion", kind="credits", suggested_by=("SkipDB",)
    )
    assert _analysis_summary([plex], [])[1].startswith("Plex suggested the intro, but Reprise could not confirm it")
    assert _analysis_summary([both], [])[1].startswith("Plex and IntroDB suggested the intro")
    assert _analysis_summary([plex, online_only], [])[1].startswith("Plex and SkipDB suggested the intro and credits")
    found = replace(plex, id="d", origin="detector", reason_code="native_signal_match")
    assert _analysis_summary([found, online_only], [])[1].endswith(
        "SkipDB also suggested the credits, which Reprise could not confirm."
    )
    counts = _range_counts([found, plex, both, online_only, replace(online_only, id="x", review_status="approved")])
    assert (counts["to_review"], counts["suggested"], counts["suggested_by"]) == (1, 3, ["Plex", "IntroDB", "SkipDB"])
    assert _range_counts([plex])["suggested_by"] == ["Plex"]


# --- lookups ---------------------------------------------------------------------------------------------------------


class Database:
    """A fake client: answers from a table by (season, episode) and counts what it was asked."""

    def __init__(self, source, answers=None, status="ok"):
        self.source, self.answers, self.status, self.asked = source, answers or {}, status, []

    def lookup(self, ids, *, duration_ms, priority, cancel_check=None):
        self.asked.append((ids.season, ids.episode, duration_ms, priority))
        if self.status != "ok":
            return LookupResult(self.status, detail=f"{self.source.value} {self.status}")
        found = self.answers.get((ids.season, ids.episode))
        if found is None:
            return LookupResult("no_data")
        return LookupResult("ok", tuple(Candidate(MarkerType(k), s, e, self.source) for k, s, e in found))


@pytest.fixture
def lookups(tmp_path):
    markers = MarkerStore(str(tmp_path / "markers.db"))
    store = RepriseStore(markers)
    clock = [datetime(2026, 1, 1, tzinfo=UTC)]

    def make(*databases, store=store):
        return online.OnlineLookups(store, {d.source: d for d in databases}, clock=lambda: clock[0])

    make.store, make.clock = store, clock
    yield make
    markers.close()


IDS = MediaIds("episode", None, "tt0903747", None, 1, 1)
LONG = 40 * 60 * 1000


def ask(lookup, ids=IDS, duration=LONG, frame_rate=None, plex=()):
    return lookup.windows(ids, duration_ms=duration, frame_rate=frame_rate, plex=plex, priority=3)


def test_each_episode_is_asked_once_and_remembered_across_runs(lookups):
    intro_db = Database(Source.INTRODB, {(1, 1): [("intro", 30000, 90000), ("credits", 2300000, LONG)]})
    skip_db = Database(Source.SKIPDB, {(1, 1): [("intro", 31000, 91000)]})
    run = lookups(intro_db, skip_db)
    found = ask(run)
    assert [(w.kind, w.start_ms, w.end_ms, w.sources) for w in found] == [
        (INTRO, 31000, 91000, ("SkipDB", "IntroDB")),
        ("credits", 2300000, LONG, ("IntroDB",)),
    ]
    assert ask(run) == found and len(intro_db.asked) == len(skip_db.asked) == 1
    # SkipDB is matched to the file's length, so it is asked again for a file of another length; IntroDB is not
    ask(run, duration=LONG + 60000)
    assert (len(intro_db.asked), len(skip_db.asked)) == (1, 2)
    # a later job asks nobody: both answers were stored
    later = lookups(intro_db, skip_db)
    assert ask(later) == found and (len(intro_db.asked), len(skip_db.asked)) == (1, 2)
    assert later.asked.network == 0 and later.asked.reused == 2


def test_a_missing_entry_is_asked_again_only_after_two_to_four_weeks(lookups):
    db = Database(Source.INTRODB)
    assert ask(lookups(db)) == () and len(db.asked) == 1
    lookups.clock[0] += timedelta(days=13)
    ask(lookups(db))
    assert len(db.asked) == 1
    lookups.clock[0] += timedelta(days=16)
    ask(lookups(db))
    assert len(db.asked) == 2


def test_a_refusal_is_not_remembered_and_a_used_up_budget_stops_the_run_asking(lookups):
    refused = Database(Source.INTRODB, status="unavailable")
    run = lookups(refused)
    assert ask(run) == ()
    ask(run, replace(IDS, episode=2))
    assert len(refused.asked) == 2 and ask(lookups(refused)) == () and len(refused.asked) == 3
    spent = Database(Source.SKIPDB, status="unavailable")
    spent.lookup = lambda ids, **kw: (
        spent.asked.append(ids.episode),
        LookupResult("unavailable", detail="SkipDB budget_exhausted"),
    )[1]
    run = lookups(spent)
    for episode_number in (1, 2, 3):
        ask(run, replace(IDS, episode=episode_number))
    assert spent.asked == [1]


def test_a_changed_parser_asks_again(lookups, monkeypatch):
    db = Database(Source.INTRODB, {(1, 1): [("intro", 30000, 90000)]})
    ask(lookups(db))
    monkeypatch.setitem(online.PARSER_VERSIONS, Source.INTRODB, online.PARSER_VERSIONS[Source.INTRODB] + 1)
    ask(lookups(db))
    assert len(db.asked) == 2


def test_a_series_theintrodb_does_not_know_is_paused_after_three_misses(lookups):
    db = Database(Source.THEINTRODB)
    run = lookups(db)
    for episode_number in (1, 2, 3, 4, 5):
        ask(run, replace(IDS, imdb=None, tmdb="1396", episode=episode_number))
    assert [asked[1] for asked in db.asked] == [1, 2, 3]  # then the series is left alone for a week


def test_a_lookup_that_raises_gives_no_times_and_breaks_nothing(lookups):
    class Broken:
        def lookup(self, *args, **kwargs):
            raise RuntimeError("secret-looking detail")

    assert ask(online.OnlineLookups(lookups.store, {Source.INTRODB: Broken()})) == ()


def test_times_from_the_other_speed_are_read_on_a_25_fps_files_clock(lookups):
    """IntroDB's times come from a film-rate release; Plex's marker for this 25 fps file says where they land."""
    duration = 2_400_000  # a 25 fps file: 4.2 % shorter than the film-rate release
    db = Database(Source.INTRODB, {(1, 1): [("credits", 2_300_000, 2_500_000)]})
    plex = (CandidateWindow("credits", 2_208_000, duration, "plex"),)
    [scaled] = ask(lookups(db), duration=duration, frame_rate=25.0, plex=plex)
    film_on_pal = (24000 / 1001) / 25
    assert (scaled.start_ms, scaled.end_ms, scaled.sources) == (
        round(2_300_000 * film_on_pal),
        round(2_500_000 * film_on_pal),
        ("IntroDB",),
    )  # read raw, 2 500 000 ms is past the end of a 2 400 000 ms file
    # at the film rate nothing is scaled; and a duration-matched SkipDB answer is never scaled
    [raw] = ask(lookups(db), duration=2_600_000, frame_rate=24000 / 1001)
    assert (raw.start_ms, raw.end_ms) == (2_300_000, 2_500_000)
    skip = Database(Source.SKIPDB, {(1, 1): [("credits", 2_208_000, duration)]})
    [exact] = ask(lookups(skip), duration=duration, frame_rate=25.0)
    assert (exact.start_ms, exact.end_ms) == (2_208_000, duration)


def test_implausible_times_are_left_out(lookups):
    db = Database(Source.INTRODB, {(1, 1): [("intro", 2_000_000, 2_100_000), ("credits", 100_000, 200_000)]})
    assert ask(lookups(db)) == ()  # an intro after 35 % of the file, credits before its last quarter


def test_credits_with_no_end_run_to_the_end_of_the_file_and_are_remembered_so(lookups):
    db = Database(Source.THEINTRODB, {(1, 1): [("credits", 2_300_000, None)]})
    [found] = ask(lookups(db))
    assert (found.kind, found.start_ms, found.end_ms, found.sources) == ("credits", 2_300_000, LONG, ("TheIntroDB",))
    [again] = ask(lookups(db))  # from what was stored, with the end still open
    assert again == found and len(db.asked) == 1


def test_the_switch_and_the_sources_decide_what_is_asked(tmp_path, monkeypatch):
    from media_preview_generator.web import settings_manager

    manager = settings_manager.SettingsManager(str(tmp_path / "config"))
    monkeypatch.setattr(settings_manager, "_settings_manager", manager)
    assert online.switch_on() is True  # on unless explicitly turned off: an existing install has it with no step
    assert set(online._sources_ready()) == {Source.INTRODB, Source.SKIPDB}  # TheIntroDB needs a key
    manager.set("markers", {"sources": [{"id": "theintrodb", "enabled": True, "api_key": "k" * 20}]})
    assert set(online._sources_ready()) == {Source.THEINTRODB, Source.INTRODB, Source.SKIPDB}
    online.set_switch(False)
    assert online.switch_on() is False and online._sources_ready() == {}


# --- the service -----------------------------------------------------------------------------------------------------


class Plexish:
    def __init__(self):
        self.calls = []

    def get_external_ids(self, rating_key):
        self.calls.append(rating_key)
        return {"kind": "episode", "imdb": "tt0903747", "tmdb": "1396", "tvdb": "81189", "season": 1, "episode": 1}


class Registry:
    def __init__(self, server):
        self.server = server

    def get(self, server_id):
        return self.server


class Recorder:
    sources = (Source.INTRODB,)

    def __init__(self):
        self.calls = []

    def windows(self, ids, **kwargs):
        self.calls.append((ids, kwargs))
        return (window(INTRO, 1000, 9000, "IntroDB"),)


def test_the_service_asks_for_the_show_once_and_each_episode_by_its_own_numbers(tmp_path):
    markers = MarkerStore(str(tmp_path / "markers.db"))
    store = RepriseStore(markers)
    versions = []
    for number in (1, 2):
        path = tmp_path / f"e{number}.mkv"
        path.write_bytes(b"episode" * number)
        version = media_version(str(path), 10000, STREAMS)
        identity = PlaybackIdentity("machine", str(number), "2", "3", "/part/3", version.size_bytes, 10000, "123")
        store.register_media(
            version,
            identity,
            server_id="server",
            metadata={
                "show_key": "show",
                "season": 1,
                "episode": number,
                "candidates": [{"kind": "intro", "start_ms": 1000, "end_ms": 9000, "source": "plex"}],
            },
        )
        versions.append(version)
    server = Plexish()

    class Job:
        id, priority = "job", 3

    class Manager:
        @staticmethod
        def is_cancellation_requested(job_id):
            return False

    service = RepriseService(store, Registry(server), Manager())
    recorder = Recorder()
    target, reference = versions
    found = service._online_windows(recorder, target, service._bindings(target.id), [reference], Job)
    assert set(found) == {target.id, reference.id} and server.calls == ["1"]
    (first_ids, first), (second_ids, second) = recorder.calls
    assert (first_ids.imdb, first_ids.season, first_ids.episode) == ("tt0903747", 1, 1)
    assert (second_ids.imdb, second_ids.season, second_ids.episode) == ("tt0903747", 1, 2)
    assert first["duration_ms"] == 10000 and first["priority"] == 3
    assert first["plex"] == [CandidateWindow("intro", 1000, 9000, "plex")]
    service._online_windows(recorder, target, service._bindings(target.id), [], Job)
    assert server.calls == ["1"]  # the show's ids are kept for the next episode of the job
    assert service._online_windows(None, target, service._bindings(target.id), [], Job) == {}
    markers.close()


def test_the_switch_is_part_of_the_settings_api(tmp_path, monkeypatch):
    from tests.reprise.test_api_http import registered_episode

    client, _, _, markers = registered_episode(tmp_path, monkeypatch)
    from media_preview_generator.web import settings_manager

    monkeypatch.setattr(
        settings_manager, "_settings_manager", settings_manager.SettingsManager(str(tmp_path / "config"))
    )
    assert client.get("/api/reprise/settings").get_json()["online_lookups"] is True
    off = client.patch("/api/reprise/settings", json={"online_lookups": False})
    assert (off.status_code, off.get_json()["online_lookups"]) == (200, False)
    assert client.get("/api/reprise/settings").get_json()["online_lookups"] is False
    assert client.patch("/api/reprise/settings", json={"online_lookups": "yes"}).status_code == 400
    markers.close()


def test_results_from_before_this_profile_are_stale():
    old = AnalysisResult("m", "a", 1, "complete", "current", "s", "2:" + "0" * 64, (), (), ())
    assert old.policy_version != POLICY_VERSION and eligible_ranges(old, "123") == ()
