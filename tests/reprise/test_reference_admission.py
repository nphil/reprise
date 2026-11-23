"""Reference provenance is checked against real, active SQLite bindings."""

import hashlib
import json
from dataclasses import asdict

import pytest

from media_preview_generator.markers.store import MarkerStore
from media_preview_generator.reprise.admission import admit_references
from media_preview_generator.reprise.identity import media_version
from media_preview_generator.reprise.models import DecisionRange, Evidence, PlaybackIdentity
from media_preview_generator.reprise.policy import eligible_ranges
from media_preview_generator.reprise.store import PROVENANCE_REPAIR, RepriseStore, _json
from media_preview_generator.reprise.verification_profile import ADMISSION_REVISION, PROFILE_FINGERPRINT, PROFILE_ID


@pytest.fixture
def catalog(tmp_path):
    markers = MarkerStore(str(tmp_path / "markers.db"))
    store = RepriseStore(markers)
    counter = 0

    def register(name, episode=1, *, machine="machine", show="show", season=1, version=None, rating=None):
        nonlocal counter
        counter += 1
        if version is None:
            path = tmp_path / (name + ".mkv")
            path.write_bytes(name.encode())
            version = media_version(str(path), 1000, "timing")
        identity = PlaybackIdentity(
            machine, rating or name, str(counter), str(counter), "/part/" + str(counter), version.size_bytes, 1000, "a"
        )
        metadata = {"library_id": "library", "show_key": show, "season": season, "episode": episode}
        store.register_media(version, identity, server_id="server", metadata=metadata)
        return version, identity, metadata

    yield store, register
    markers.close()


def admitted(store, target, candidates):
    return [version for version, snapshot in admit_references(target, candidates, store) if snapshot]


def test_only_distinct_episodes_in_known_machine_scoped_show_and_season(catalog):
    store, register = catalog
    target, _, _ = register("target", 1)
    good, _, _ = register("good", 2)
    same_episode, _, _ = register("alternate", 1)
    other_show, _, _ = register("other-show", 3, show="different")
    other_season, _, _ = register("other-season", 3, season=2)
    other_machine, _, _ = register("other-machine", 3, machine="another-machine")
    unknown_episode, _, _ = register("unknown-episode", None)
    unknown_show, _, _ = register("unknown-show", 4, show=None)
    unknown_season, _, _ = register("unknown-season", 5, season=None)
    candidates = [
        same_episode,
        other_show,
        other_season,
        other_machine,
        unknown_episode,
        unknown_show,
        unknown_season,
        good,
    ]
    assert admitted(store, target, candidates) == [good]


@pytest.mark.parametrize("field", ["episode", "show", "season"])
def test_unknown_target_provenance_fails_closed(catalog, field):
    store, register = catalog
    values = {"episode": 1, "show": "show", "season": 1, "machine": "machine"}
    values[field] = None
    target, _, _ = register("target", **values)
    candidate, _, _ = register("candidate", 2)
    assert not admit_references(target, [candidate], store)


@pytest.mark.parametrize("side", ["target", "candidate"])
@pytest.mark.parametrize("conflict", ["episode", "show", "season", "machine", "rating"])
def test_every_active_alias_must_agree(catalog, side, conflict):
    store, register = catalog
    target, _, _ = register("target", 1, rating="target-rating")
    candidate, _, _ = register("candidate", 2, rating="candidate-rating")
    version = target if side == "target" else candidate
    values = {"episode": 1 if side == "target" else 2, "show": "show", "season": 1, "machine": "machine"}
    if conflict == "rating":
        rating = "candidate-rating" if side == "target" else "target-rating"
    else:
        rating = "extra-rating"
        values[conflict] = {"episode": 3, "show": "other", "season": 2, "machine": "other-machine"}[conflict]
    register("alias", version=version, rating=rating, **values)
    assert not admit_references(target, [candidate], store)


def test_hard_links_are_not_independent_references(catalog, tmp_path):
    store, register = catalog
    first = tmp_path / "first.mkv"
    first.write_bytes(b"the same file")
    second = tmp_path / "second.mkv"
    second.hardlink_to(first)
    target = media_version(str(first), 1000, "timing")
    candidate = media_version(str(second), 1000, "timing")
    register("target", 1, version=target)
    register("candidate", 2, version=candidate)
    assert target.id != candidate.id
    assert (target.device, target.inode) == (candidate.device, candidate.inode)
    assert not admit_references(target, [candidate], store)


def test_one_file_is_admitted_per_episode(catalog):
    store, register = catalog
    target, _, _ = register("target", 1)
    alternatives = [register(f"alternate-{i}", 2)[0] for i in range(8)]
    others = [register(f"episode-{i}", i)[0] for i in range(3, 9)]
    result = admitted(store, target, alternatives + others)
    assert result == [alternatives[0], *others]


def test_every_other_episode_is_admitted_nearest_first_in_a_stable_order(catalog):
    store, register = catalog
    target, _, _ = register("target", 5)
    by_episode = {n: register(f"episode-{n}", n)[0] for n in (1, 2, 3, 4, 6, 7, 9, 10, 12)}
    # as near as an episode can be (the same number), but of another season, so never a reference
    next_season = register("next-season", 5, season=2)[0]
    candidates = [*by_episode.values(), next_season]
    # distance 1: episodes 4 and 6 (the lower number first on a tie); then 2: episodes 3 and 7; 4: episodes 1 and 9
    expected = [by_episode[n] for n in (4, 6, 3, 7, 2, 1, 9, 10, 12)]
    assert admitted(store, target, candidates) == expected
    assert admitted(store, target, candidates[::-1]) == expected


def test_fewer_than_three_other_episodes_are_all_used_nearest_first(catalog):
    store, register = catalog
    target, _, _ = register("target", 4)
    far, near = register("far", 9)[0], register("near", 3)[0]
    assert admitted(store, target, [far, near]) == [near, far]


def test_reference_binding_change_changes_snapshot(catalog):
    store, register = catalog
    target, _, _ = register("target", 1)
    candidate, identity, metadata = register("candidate", 2)
    original = admit_references(target, [candidate], store)
    assert len(original) == 1 and original[0][1]
    metadata["episode"] = 3
    store.register_media(candidate, identity, server_id="server", metadata=metadata)
    changed = admit_references(target, [candidate], store)
    assert len(changed) == 1 and changed[0][1] != original[0][1]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("library_id", "another-library", id="library"),
        pytest.param("remote_part_path", "/library/another.mkv", id="path"),
        pytest.param("mapping_fingerprint", "another-mapping", id="path_mapping"),
    ],
)
def test_what_identifies_the_episode_in_plex_changes_the_snapshot(catalog, field, value):
    store, register = catalog
    target, _, _ = register("target", 1)
    candidate, identity, metadata = register("candidate", 2)
    original = admit_references(target, [candidate], store)
    metadata[field] = value
    store.register_media(candidate, identity, server_id="server", metadata=metadata)
    changed = admit_references(target, [candidate], store)
    assert len(changed) == 1 and changed[0][1] != original[0][1]


def analysed(store, target, reference, provenance):
    """``target`` analysed against ``reference`` under ``provenance``, its one skip approved and locked by the owner."""
    evidence = Evidence(
        "e",
        0,
        900,
        "audiovisual",
        "match",
        "match",
        "",
        reference.id,
        0,
        900,
        {
            "checked_audio_stream_ids": ["a"],
            "admission_provenance": provenance,
            "verification_profile": PROFILE_ID,
            "profile_fingerprint": PROFILE_FINGERPRINT,
        },
    )
    proposal = DecisionRange("r", "intro", "skip", 0, 900, "proposed", False, "detector", "match", "", ("e",), ("a",))
    run = store.begin_analysis(target.id, "job", "signature")
    result = store.finish_analysis(run, (evidence,), (proposal,), target.id)
    store.save_review(
        target.id, result.result_revision, [{"range_id": "r", "review_status": "approved", "locked": True}], "owner"
    )
    return store.get_result(target.id)


def reviewed(store, register):
    """A target analysed against a reference with its one skip approved and locked: ``(target, reference, result)``,
    the two as ``register`` returned them."""
    target, reference = register("target", 1), register("reference", 2)
    [(_, provenance)] = admit_references(target[0], [reference[0]], store)
    result = analysed(store, target[0], reference[0], provenance)
    assert result.freshness == "current"
    assert eligible_ranges(result, "a")
    return target, reference, result


def test_persisted_approval_stales_when_the_targets_binding_changes(catalog):
    store, register = catalog
    (target, identity, metadata), _, _ = reviewed(store, register)
    metadata["episode"] = 3
    store.register_media(target, identity, server_id="server", metadata=metadata)
    result = store.get_result(target.id)
    assert result.freshness == "stale"
    assert not eligible_ranges(result, "a")
    metadata["episode"] = 1
    store.register_media(target, identity, server_id="server", metadata=metadata)
    restored = store.get_result(target.id)
    assert not eligible_ranges(restored, "a")
    assert not any(item.action == "skip" and item.review_status == "approved" for item in restored.ranges)


def test_a_changed_reference_stales_only_the_comparison_and_sets_nothing_back(catalog):
    store, register = catalog
    (target, _, _), (reference, identity, metadata), before = reviewed(store, register)
    metadata["episode"] = 3
    store.register_media(reference, identity, server_id="server", metadata=metadata)
    result = store.get_result(target.id)
    assert result.freshness == "stale"
    assert not eligible_ranges(result, "a")
    assert (result.ranges, result.result_revision) == (before.ranges, before.result_revision)  # nothing set back
    metadata["episode"] = 2
    store.register_media(reference, identity, server_id="server", metadata=metadata)
    assert store.get_result(target.id) == before  # the comparison holds again, and the approval with it
    assert eligible_ranges(store.get_result(target.id), "a")


PLEX_HOLDS = [{"kind": "intro", "start_ms": 1000, "end_ms": 9000, "source": "plex"}]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("candidates", PLEX_HOLDS, id="markers"),
        pytest.param("title", "Another title", id="title"),
        pytest.param("show_title", "Another show", id="show_title"),
        pytest.param("server_url", "http://plex:32400", id="address"),
    ],
)
def test_what_plex_changes_by_itself_changes_neither_the_snapshot_nor_a_result(catalog, field, value):
    store, register = catalog
    (target, target_identity, target_metadata), (reference, identity, metadata), before = reviewed(store, register)
    snapshot = admit_references(target, [reference], store)
    store.register_media(reference, identity, server_id="server", metadata={**metadata, field: value})
    store.register_media(target, target_identity, server_id="server", metadata={**target_metadata, field: value})
    assert admit_references(target, [reference], store) == snapshot
    assert store.get_result(target.id) == before  # as current and as approved, at the same revision


def whole_metadata_stamp(store, target, reference):
    """The provenance as an earlier version stamped it, when every field of a binding's metadata counted."""

    def snapshot(version):
        return sorted(
            json.dumps([asdict(i), server, metadata], sort_keys=True, separators=(",", ":"))
            for i, server, metadata in store.get_bindings(version.id)
        )

    payload = [ADMISSION_REVISION, target.id, snapshot(target), reference.id, snapshot(reference)]
    return hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()


def stamped_as_before(store, target, reference, identity, metadata):
    """``target``'s reviewed comparison with ``reference`` carrying the whole-metadata stamp of an earlier version, with
    Plex's markers in the reference's binding. Returns that stamp and the one the comparison carries now."""
    store.register_media(reference, identity, server_id="server", metadata={**metadata, "candidates": PLEX_HOLDS})
    legacy = whole_metadata_stamp(store, target, reference)
    [(_, current)] = admit_references(target, [reference], store)
    assert legacy != current
    with store.transaction() as c:
        evidence = json.loads(
            c.execute(
                "SELECT data FROM reprise_evidence_windows WHERE version_id=? AND id='e'", (target.id,)
            ).fetchone()["data"]
        )
        evidence["metrics"]["admission_provenance"] = legacy
        c.execute(
            "UPDATE reprise_evidence_windows SET data=? WHERE version_id=? AND id='e'", (_json(evidence), target.id)
        )
        c.execute("DELETE FROM reprise_repairs WHERE name=?", (PROVENANCE_REPAIR,))
    return legacy, current


def test_a_comparison_stamped_before_plexs_own_changes_were_left_out_keeps_reading_current(catalog):
    store, register = catalog
    (target, _, _), (reference, identity, metadata), before = reviewed(store, register)
    legacy, current = stamped_as_before(store, target, reference, identity, metadata)
    assert store.get_result(target.id).freshness == "stale"  # the stamp is of a formula nothing computes any more

    again = RepriseStore(store.marker_store)  # the next start-up

    after = again.get_result(target.id)
    assert after.freshness == "current"
    assert eligible_ranges(after, "a")
    assert after.evidence[0].metrics["admission_provenance"] == current
    assert (after.ranges, after.result_revision) == (before.ranges, before.result_revision)  # only the stamp changed
    with again.transaction() as c:
        assert c.execute("SELECT 1 FROM reprise_repairs WHERE name=?", (PROVENANCE_REPAIR,)).fetchone()


def test_a_comparison_whose_episodes_changed_since_it_was_stamped_still_reads_stale_after_the_repair(catalog):
    store, register = catalog
    (target, _, _), (reference, identity, metadata), _ = reviewed(store, register)
    legacy, _ = stamped_as_before(store, target, reference, identity, metadata)
    store.register_media(reference, identity, server_id="server", metadata={**metadata, "candidates": []})

    again = RepriseStore(store.marker_store)

    after = again.get_result(target.id)
    assert after.freshness == "stale"  # nobody knows what it was compared with: analyzing again settles it
    assert after.evidence[0].metrics["admission_provenance"] == legacy
