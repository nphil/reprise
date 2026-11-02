from dataclasses import replace

import pytest

from media_preview_generator.markers.store import MarkerStore
from media_preview_generator.reprise.admission import admit_references
from media_preview_generator.reprise.identity import media_version
from media_preview_generator.reprise.models import DecisionRange, Evidence, PlaybackIdentity
from media_preview_generator.reprise.policy import eligible_ranges
from media_preview_generator.reprise.store import RepriseStore, RevisionConflict
from media_preview_generator.reprise.verification_profile import PROFILE_FINGERPRINT, PROFILE_ID


def test_real_database_review_rescan_failure_and_replacement(tmp_path):
    path = tmp_path / "episode.mkv"
    path.write_bytes(b"original")
    marker = MarkerStore(str(tmp_path / "markers.db"))
    store = RepriseStore(marker)
    version = media_version(str(path), 1000, "timing")
    identity = PlaybackIdentity("machine", "1", "2", "3", "/part/3", 8, 1000, "a")
    metadata = {"show_key": "show", "season": 1, "episode": 1}
    store.register_media(version, identity, server_id="local", metadata=metadata)
    store.register_media(version, replace(identity, audio_stream_id="b"), server_id="local", metadata=metadata)
    ref_path = tmp_path / "reference.mkv"
    ref_path.write_bytes(b"reference")
    reference = media_version(str(ref_path), 1000, "timing")
    store.register_media(
        reference,
        PlaybackIdentity("machine", "4", "5", "6", "/part/6", 9, 1000, "a"),
        server_id="local",
        metadata={"show_key": "show", "season": 1, "episode": 2},
    )
    [(_, provenance)] = admit_references(version, [reference], store)
    assert store.resolve_binding(identity) == version
    assert len(store.get_bindings(version.id)) == 2
    skip = DecisionRange("r", "intro", "skip", 0, 900, "proposed", False, "detector", "match", "", ("match",), ("a",))
    unknown = Evidence(
        "e",
        400,
        500,
        "video",
        "unknown",
        "undecoded",
        "",
        None,
        None,
        None,
        {"verification_profile": PROFILE_ID, "profile_fingerprint": PROFILE_FINGERPRINT},
    )
    match = Evidence(
        "match",
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
    run = store.begin_analysis(version.id, "job", "signature")
    result = store.finish_analysis(run, (unknown, match), (skip,), version.id)
    result = store.save_review(
        version.id, result.result_revision, [{"range_id": "r", "review_status": "approved", "locked": True}], "owner"
    )
    assert [(r.start_ms, r.end_ms) for r in eligible_ranges(result, "a")] == [(0, 400), (500, 900)]
    assert not eligible_ranges(result, "b")
    with pytest.raises(RevisionConflict):
        store.save_review(version.id, result.result_revision - 1, [], "owner")
    with store.transaction() as conn:
        conn.execute("UPDATE reprise_decision_results SET policy_version=? WHERE version_id=?", ("old", version.id))
    assert store.get_result(version.id).freshness == "stale"
    with store.transaction() as conn:
        conn.execute(
            "UPDATE reprise_decision_results SET policy_version=? WHERE version_id=?",
            (result.policy_version, version.id),
        )
    run = store.begin_analysis(version.id, "job2", "signature")
    unchanged = store.finish_analysis(run, (unknown, match), (skip,), version.id)
    assert unchanged.result_revision == result.result_revision
    assert unchanged.ranges[0].locked
    store.register_media(version, identity, server_id="local", metadata={"mapping_fingerprint": "changed"})
    changed = store.get_result(version.id)
    assert changed.analysis_status == "failed"
    assert changed.result_revision > unchanged.result_revision
    assert not eligible_ranges(changed, "a")
    run = store.begin_analysis(version.id, "job3", "signature")
    store.fail_analysis(run, "cancelled", "cancelled")
    assert not eligible_ranges(store.get_result(version.id), "a")
    path.write_bytes(b"replaced file")
    replacement = media_version(str(path), 1000, "timing")
    store.register_media(replacement, replace(identity, part_size_bytes=13), server_id="local")
    assert store.get_result(version.id).freshness == "stale"
    assert store.get_result(replacement.id) is None
    revised_identity = replace(identity, part_size_bytes=13)
    store.register_media(replacement, revised_identity, server_id="local", metadata={"library_id": "7"})
    assert store.reconcile_library("local", "7", [revised_identity]) == 0
    assert store.reconcile_library("local", "7", []) == 1
    assert store.resolve_binding(revised_identity) is None
    marker.close()
    reopened = MarkerStore(str(tmp_path / "markers.db"))
    assert RepriseStore(reopened).get_result(version.id).analysis_status == "cancelled"
    reopened.close()


def test_client_rotation_persists_only_hash(tmp_path):
    marker = MarkerStore(str(tmp_path / "markers.db"))
    store = RepriseStore(marker)
    client = store.create_client("screen")
    assert store.authenticate_client(client["secret"]).name == "screen"
    rotated = store.rotate_client(client["id"])
    assert store.authenticate_client(client["secret"]) is None
    assert store.authenticate_client(rotated["secret"]).id == rotated["id"]
    store.revoke_client(rotated["id"])
    assert store.authenticate_client(rotated["secret"]) is None
    marker.close()
    assert client["secret"].encode() not in (tmp_path / "markers.db").read_bytes()


def test_changed_evidence_replaces_only_untouched_detector_proposals(tmp_path):
    path = tmp_path / "episode.mkv"
    path.write_bytes(b"original")
    marker = MarkerStore(str(tmp_path / "markers.db"))
    store = RepriseStore(marker)
    version = media_version(str(path), 10000, "timing")
    identity = PlaybackIdentity("machine", "1", "2", "3", "/part/3", 8, 10000, "a")
    store.register_media(version, identity, server_id="local", metadata={"show_key": "show", "season": 1, "episode": 1})
    ref_path = tmp_path / "reference.mkv"
    ref_path.write_bytes(b"reference")
    reference = media_version(str(ref_path), 10000, "timing")
    ref_identity = PlaybackIdentity("machine", "4", "5", "6", "/part/6", 9, 10000, "a")
    store.register_media(
        reference, ref_identity, server_id="local", metadata={"show_key": "show", "season": 1, "episode": 2}
    )
    [(_, provenance)] = admit_references(version, [reference], store)
    proposals = tuple(
        DecisionRange(
            name, "intro", "keep", i * 1000, (i + 1) * 1000, "proposed", False, "detector", "unknown", "", (), ()
        )
        for i, name in enumerate(("untouched", "reviewed", "locked", "edited"))
    )
    skip = DecisionRange(
        "skip-override",
        "intro",
        "skip",
        5000,
        6000,
        "proposed",
        False,
        "detector",
        "match",
        "",
        ("old-evidence",),
        ("a",),
    )
    run = store.begin_analysis(version.id, "job", "old")
    result = store.finish_analysis(run, (), (*proposals, skip), version.id)
    result = store.save_review(
        version.id,
        result.result_revision,
        [
            {"range_id": "reviewed", "review_status": "approved"},
            {"range_id": "locked", "locked": True},
            {"range_id": "edited", "reason": "Protect the unique gag"},
            {"range_id": "skip-override", "end_ms": 5500, "review_status": "approved"},
            {"action": "keep", "start_ms": 6000, "end_ms": 7000},
        ],
        "owner",
    )
    manual = next(r for r in result.ranges if r.origin == "manual")
    fresh = DecisionRange(
        "fresh", "intro", "skip", 0, 10000, "proposed", False, "detector", "match", "", ("new-evidence",), ("a",)
    )
    evidence = Evidence(
        "new-evidence",
        0,
        10000,
        "audiovisual",
        "match",
        "match",
        "",
        reference.id,
        0,
        10000,
        {
            "checked_audio_stream_ids": ["a"],
            "admission_provenance": provenance,
            "verification_profile": PROFILE_ID,
            "profile_fingerprint": PROFILE_FINGERPRINT,
        },
    )
    run = store.begin_analysis(version.id, "job2", "new")
    updated = store.finish_analysis(run, (evidence,), (fresh,), version.id)
    ranges = {r.id: r for r in updated.ranges}
    assert "untouched" not in ranges
    for rid in ("reviewed", "locked", "edited", manual.id):
        assert ranges[rid] == next(r for r in result.ranges if r.id == rid)
    assert ranges["skip-override"].end_ms == 5500
    assert ranges["skip-override"].review_status == "proposed"
    reviewed = store.save_review(
        version.id, updated.result_revision, [{"range_id": "fresh", "review_status": "approved"}], "owner"
    )
    assert [(r.start_ms, r.end_ms) for r in eligible_ranges(reviewed, "a")] == [(0, 1000), (4000, 6000), (7000, 10000)]
    marker.close()
