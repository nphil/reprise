"""A re-analysis keeps what the owner locked and the new run's pieces never overlap it; results stored with such an
overlap by an earlier version are repaired once at start-up."""

from dataclasses import asdict

import pytest

from media_preview_generator.markers.store import MarkerStore
from media_preview_generator.reprise.identity import media_version
from media_preview_generator.reprise.models import DecisionRange, Evidence, PlaybackIdentity
from media_preview_generator.reprise.store import LOCK_REPAIR, RepriseStore, _json
from media_preview_generator.reprise.uniqueness import COMPARISON_KEY
from media_preview_generator.reprise.verification_profile import PROFILE_FINGERPRINT, PROFILE_ID

DURATION = 400_000
METRICS = {"verification_profile": PROFILE_ID, "profile_fingerprint": PROFILE_FINGERPRINT}


@pytest.fixture
def episode(tmp_path):
    path = tmp_path / "episode.mkv"
    path.write_bytes(b"original")
    marker = MarkerStore(str(tmp_path / "markers.db"))
    store = RepriseStore(marker)
    version = media_version(str(path), DURATION, "timing")
    identity = PlaybackIdentity("machine", "1", "2", "3", "/part/3", 8, DURATION, "a")
    store.register_media(version, identity, server_id="local", metadata={"show_key": "show", "season": 1, "episode": 1})
    yield marker, store, version
    marker.close()


def piece(name, kind, start, end, *, action="skip", origin="detector", metrics=None):
    return (
        DecisionRange(
            name,
            kind,
            action,
            start,
            end,
            "proposed",
            False,
            origin,
            "native_signal_match" if action == "skip" else "native_signal_unknown",
            "",
            (name + "-evidence",),
            ("a",) if action == "skip" else (),
        ),
        Evidence(
            name + "-evidence",
            start,
            end,
            "audiovisual",
            "unknown",
            "native_signal_unknown",
            "",
            None,
            None,
            None,
            {**METRICS, **(metrics or {})},
        ),
    )


def analyse(store, version, job, signature, pieces):
    run = store.begin_analysis(version.id, job, signature)
    return store.finish_analysis(run, [e for _, e in pieces], [r for r, _ in pieces], version.id)


def lock_all(store, version, result, names):
    return store.save_review(
        version.id,
        result.result_revision,
        [{"range_id": n, "review_status": "approved", "locked": True} for n in names],
        "owner",
    )


def spans(result, kind, action):
    return sorted((r.start_ms, r.end_ms) for r in result.ranges if r.kind == kind and r.action == action)


def assert_no_overlap(items):
    for (_, end), (start, _) in zip(items, items[1:], strict=False):
        assert end <= start, items


def first_run(store, version):
    """The earlier version's intro, in four confirmed pieces the owner approved and locked."""
    pieces = [
        piece("a", "intro", 221_000, 222_000),
        piece("b", "intro", 222_000, 224_000),
        piece("c", "intro", 224_000, 226_000),
        piece("d", "intro", 226_000, 238_000),
    ]
    return lock_all(store, version, analyse(store, version, "job1", "old", pieces), "abcd")


def test_a_reanalysis_cuts_the_new_pieces_back_to_what_lies_outside_a_lock(episode):
    _, store, version = episode
    locked = first_run(store, version)
    new = [
        piece("x", "intro", 221_000, 222_040),  # the same intro, a few milliseconds apart: wholly inside the lock
        piece("y", "intro", 222_040, 224_000),
        piece("z", "intro", 224_000, 226_500),
        piece("w", "intro", 226_500, 239_000),  # runs a second past the lock
        piece("k", "intro", 235_000, 238_000, action="keep"),  # unconfirmed stretch inside the lock
        piece("k2", "intro", 239_000, 240_000, action="keep"),  # beyond it
        piece("p", "intro", 150_000, 160_000, origin="plex"),  # a suggestion elsewhere
        piece("c1", "credits", 380_000, 390_000),  # another kind: nothing locked there
    ]
    result = analyse(store, version, "job2", "new", new)
    assert spans(result, "intro", "skip") == [
        (150_000, 160_000),
        (221_000, 222_000),
        (222_000, 224_000),
        (224_000, 226_000),
        (226_000, 238_000),
        (238_000, 239_000),
    ]
    assert_no_overlap(spans(result, "intro", "skip")[1:])
    assert spans(result, "intro", "keep") == [(239_000, 240_000)]
    assert spans(result, "credits", "skip") == [(380_000, 390_000)]
    kept = {r.id: r for r in result.ranges}
    for old in locked.ranges:
        # the lock is the owner's decision and stays exactly as it was (a skip falls back to proposed, as always)
        assert kept[old.id].locked and (kept[old.id].start_ms, kept[old.id].end_ms) == (old.start_ms, old.end_ms)
        assert kept[old.id].review_status == "proposed"
    assert kept["w"].start_ms == 238_000 and kept["w"].evidence_ids == ("w-evidence",)


def test_a_lock_in_the_middle_of_a_new_piece_leaves_both_sides_and_only_the_end_stays_terminal(episode):
    _, store, version = episode
    lock = piece("lock", "intro", 100_000, 110_000)
    lock_all(store, version, analyse(store, version, "job1", "old", [lock]), ["lock"])
    wide, evidence = piece("wide", "intro", 90_000, 120_000)
    result = analyse(store, version, "job2", "new", [(wide.__class__(**{**asdict(wide), "terminal": True}), evidence)])
    assert spans(result, "intro", "skip") == [(90_000, 100_000), (100_000, 110_000), (110_000, 120_000)]
    pieces = {(r.start_ms, r.end_ms): r for r in result.ranges}
    assert [pieces[s].terminal for s in ((90_000, 100_000), (110_000, 120_000))] == [False, True]
    assert len({r.id for r in result.ranges}) == 3


def test_an_unlocked_old_range_does_not_win_so_the_new_run_replaces_it(episode):
    _, store, version = episode
    old = [piece("a", "intro", 100_000, 110_000)]
    analyse(store, version, "job1", "old", old)
    result = analyse(store, version, "job2", "new", [piece("n", "intro", 101_000, 111_000)])
    assert spans(result, "intro", "skip") == [(101_000, 111_000)]


def overlapping_store(store, version):
    """What an earlier version stored: the locked pieces next to the new run's, overlapping."""
    locked = first_run(store, version)
    extra = [
        piece("x", "intro", 221_000, 222_040)[0],
        piece("w", "intro", 226_500, 239_000)[0],
        piece("k", "intro", 235_000, 238_000, action="keep")[0],
    ]
    approved = [
        r.__class__(**{**asdict(r), "review_status": "approved"}) if r.id == "w" else r for r in extra
    ]  # an approval of a piece that has to change
    with store.transaction() as c:
        for r in approved:
            c.execute("INSERT INTO reprise_ranges VALUES (?,?,?)", (version.id, r.id, _json(asdict(r))))
        c.execute("DELETE FROM reprise_repairs")
    return locked


def test_results_stored_with_an_overlap_are_repaired_once_at_startup(episode):
    marker, store, version = episode
    locked = overlapping_store(store, version)
    before = store.get_result(version.id)
    assert len(before.ranges) == len(locked.ranges) + 3
    again = RepriseStore(marker)
    after = again.get_result(version.id)
    assert after.result_revision == before.result_revision + 1
    assert spans(after, "intro", "skip") == [
        (221_000, 222_000),
        (222_000, 224_000),
        (224_000, 226_000),
        (226_000, 238_000),
        (238_000, 239_000),
    ]
    assert not [r for r in after.ranges if r.action == "keep"]
    by_id = {r.id: r for r in after.ranges}
    for old in locked.ranges:
        assert by_id[old.id] == old  # locks keep their approval and their bounds
    assert by_id["w"].review_status == "proposed"  # its approval was for a range that had to change
    # once: a second start changes nothing, and the repair is on record
    revision = after.result_revision
    RepriseStore(marker)
    assert again.get_result(version.id).result_revision == revision
    with again.transaction() as c:
        assert c.execute("SELECT 1 FROM reprise_repairs WHERE name=?", (LOCK_REPAIR,)).fetchone()


def test_the_repair_leaves_the_owners_own_ranges_alone(episode):
    marker, store, version = episode
    first_run(store, version)
    manual = piece("m", "intro", 226_000, 240_000, action="keep", origin="manual")[0]
    with store.transaction() as c:
        c.execute("INSERT INTO reprise_ranges VALUES (?,?,?)", (version.id, "m", _json(asdict(manual))))
        c.execute("DELETE FROM reprise_repairs")
    before = store.get_result(version.id)
    after = RepriseStore(marker).get_result(version.id)
    assert after.ranges == before.ranges and after.result_revision == before.result_revision


def test_what_the_comparison_found_refreshes_the_evidence_without_resetting_approvals(episode):
    _, store, version = episode
    first = analyse(store, version, "job1", "same", [piece("a", "intro", 100_000, 120_000)])
    approved = lock_all(store, version, first, ["a"])
    facts = {"kind": "intro", "episodes_searched": 9, "sound_matched": 0, "picture_matched": 0, "confirmed_ms": 0}
    again = analyse(
        store, version, "job2", "same", [piece("a", "intro", 100_000, 120_000, metrics={COMPARISON_KEY: facts})]
    )
    assert again.result_revision == approved.result_revision  # nothing the owner decided on changed
    assert [r.review_status for r in again.ranges] == ["approved"]
    assert again.evidence[0].metrics[COMPARISON_KEY] == facts
