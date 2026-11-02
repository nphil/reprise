"""Reference provenance is checked against real, active SQLite bindings."""

import pytest

from media_preview_generator.markers.store import MarkerStore
from media_preview_generator.reprise.admission import admit_references
from media_preview_generator.reprise.identity import media_version
from media_preview_generator.reprise.models import DecisionRange, Evidence, PlaybackIdentity
from media_preview_generator.reprise.policy import eligible_ranges
from media_preview_generator.reprise.store import RepriseStore
from media_preview_generator.reprise.verification_profile import PROFILE_FINGERPRINT, PROFILE_ID


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
    candidates = [same_episode, other_show, other_season, other_machine, unknown_episode, unknown_show, unknown_season, good]
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


def test_deduplicate_episodes_before_six_reference_limit(catalog):
    store, register = catalog
    target, _, _ = register("target", 1)
    alternatives = [register(f"alternate-{i}", 2)[0] for i in range(8)]
    others = [register(f"episode-{i}", i)[0] for i in range(3, 9)]
    result = admitted(store, target, alternatives + others)
    assert result == [alternatives[0], *others[:5]]


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


@pytest.mark.parametrize("changed_side", ["target", "reference"])
def test_persisted_approval_stales_when_binding_metadata_changes(catalog, changed_side):
    store, register = catalog
    target, target_identity, target_metadata = register("target", 1)
    reference, ref_identity, ref_metadata = register("reference", 2)
    [(_, provenance)] = admit_references(target, [reference], store)
    evidence = Evidence(
        "e", 0, 900, "audiovisual", "match", "match", "", reference.id, 0, 900,
        {"checked_audio_stream_ids": ["a"], "admission_provenance": provenance,
         "verification_profile": PROFILE_ID, "profile_fingerprint": PROFILE_FINGERPRINT},
    )
    proposal = DecisionRange("r", "intro", "skip", 0, 900, "proposed", False, "detector", "match", "", ("e",), ("a",))
    run = store.begin_analysis(target.id, "job", "signature")
    result = store.finish_analysis(run, (evidence,), (proposal,), target.id)
    store.save_review(target.id, result.result_revision, [{"range_id": "r", "review_status": "approved", "locked": True}], "owner")
    result = store.get_result(target.id)
    assert result.freshness == "current"
    assert eligible_ranges(result, "a")
    version, identity, metadata = (
        (target, target_identity, target_metadata) if changed_side == "target" else (reference, ref_identity, ref_metadata)
    )
    metadata["episode"] = 3
    store.register_media(version, identity, server_id="server", metadata=metadata)
    result = store.get_result(target.id)
    assert result.freshness == "stale"
    assert not eligible_ranges(result, "a")
    metadata["episode"] = 1 if changed_side == "target" else 2
    store.register_media(version, identity, server_id="server", metadata=metadata)
    restored = store.get_result(target.id)
    assert not eligible_ranges(restored, "a")
    assert not any(item.action == "skip" and item.review_status == "approved" for item in restored.ranges)
