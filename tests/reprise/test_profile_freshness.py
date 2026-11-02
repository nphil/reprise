"""Profile changes must not reuse previously approved detector decisions."""

import json
import sqlite3
from dataclasses import asdict, replace

import pytest

from media_preview_generator.markers.publishers.plex_db import LocalPlexDb
from media_preview_generator.markers.store import MarkerStore
from media_preview_generator.reprise import store as store_module
from media_preview_generator.reprise.admission import admit_references
from media_preview_generator.reprise.identity import media_version
from media_preview_generator.reprise.models import DecisionRange, Evidence, PlaybackIdentity
from media_preview_generator.reprise.policy import eligible_ranges
from media_preview_generator.reprise.publishing import PublicationConflict, ReprisePublisher
from media_preview_generator.reprise.service import RepriseService, _mapping_fingerprint
from media_preview_generator.reprise.store import RepriseStore
from media_preview_generator.reprise.verification_profile import PROFILE_FINGERPRINT, PROFILE_ID
from media_preview_generator.servers.registry import ServerRegistry
from scripts.plex_db_node_fixture import build


@pytest.fixture
def case(tmp_path, monkeypatch):
    library = tmp_path / "library"
    library.mkdir()
    media = library / "episode.mkv"
    reference_path = library / "reference.mkv"
    media.write_bytes(b"episode fixture")
    reference_path.write_bytes(b"reference fixture")
    folder = tmp_path / "plex"
    plex = build(folder, "fixture-machine")
    with sqlite3.connect(plex) as db:
        db.execute("INSERT INTO tags (id, tag, tag_type) VALUES (12, '', 12)")
        db.execute(
            "UPDATE media_parts SET file=?, size=?, duration=?, updated_at=? WHERE id=1",
            (str(media), media.stat().st_size, 100000, 101),
        )
        db.execute("UPDATE media_items SET duration=? WHERE id=1", (100000,))
    registry = ServerRegistry.from_settings(
        [
            {
                "id": "local",
                "type": "plex",
                "name": "fixture",
                "enabled": True,
                "url": "http://127.0.0.1:1",
                "auth": {},
                "server_identity": "fixture-machine",
                "output": {"plex_config_folder": str(folder)},
                "libraries": [{"id": "1", "name": "fixture", "enabled": True, "remote_paths": [str(library)]}],
            }
        ]
    )
    marker_path = tmp_path / "markers.db"
    marker = MarkerStore(str(marker_path))
    store = RepriseStore(marker)
    signature = json.dumps({"audio_streams": [{"plex_id": "1", "index": 1}], "start_time_ms": 0, "streams": []})
    version = media_version(str(media), 100000, signature)
    reference = media_version(str(reference_path), 100000, signature)
    identity = PlaybackIdentity(
        "fixture-machine", "1", "1", "1", "/library/parts/1/file.mkv", media.stat().st_size, 100000, "1"
    )
    ref_identity = replace(
        identity,
        rating_key="20",
        media_id="20",
        part_id="20",
        part_key="/library/parts/20/file.mkv",
        part_size_bytes=reference_path.stat().st_size,
    )
    cfg = registry.get_config("local")
    metadata = {
        "library_id": "1",
        "show_key": "show",
        "season": 1,
        "episode": 1,
        "remote_part_path": str(media),
        "mapping_fingerprint": _mapping_fingerprint(cfg, cfg.libraries[0]),
    }
    store.register_media(version, identity, server_id="local", metadata=metadata)
    store.register_media(
        reference,
        ref_identity,
        server_id="local",
        metadata={**metadata, "episode": 2, "remote_part_path": str(reference_path)},
    )
    [(_, provenance)] = admit_references(version, [reference], store)
    evidence = Evidence(
        "match",
        10000,
        20000,
        "audiovisual",
        "match",
        "same",
        "",
        reference.id,
        10000,
        20000,
        {
            "checked_audio_stream_ids": ["1"],
            "admission_provenance": provenance,
            "verification_profile": PROFILE_ID,
            "profile_fingerprint": PROFILE_FINGERPRINT,
        },
    )
    proposal = DecisionRange(
        "intro", "intro", "skip", 10000, 20000, "proposed", False, "detector", "same", "", ("match",), ("1",)
    )
    service = RepriseService(store, registry, None)
    publisher = ReprisePublisher(store, registry, service, writes_enabled=True)
    monkeypatch.setattr(publisher, "_database_for", lambda server_id, identity: LocalPlexDb(lambda: str(plex)))
    yield {
        "marker": marker,
        "marker_path": marker_path,
        "store": store,
        "registry": registry,
        "service": service,
        "publisher": publisher,
        "plex": plex,
        "identity": identity,
        "version": version,
        "evidence": evidence,
        "proposal": proposal,
    }
    if not marker._closed:
        marker.close()


def finish(case, job="job", evidence=None, ranges=None):
    store, version = case["store"], case["version"]
    run = store.begin_analysis(version.id, job, "stable-signature")
    return store.finish_analysis(
        run,
        evidence if evidence is not None else (case["evidence"],),
        ranges if ranges is not None else (case["proposal"],),
        version.id,
    )


def approve(case):
    result = finish(case)
    return case["store"].save_review(
        case["version"].id,
        result.result_revision,
        [{"range_id": "intro", "review_status": "approved", "locked": True}],
        "owner",
    )


def approve_old_profile(case, monkeypatch, *, manual_keep=False):
    metrics = {
        **case["evidence"].metrics,
        "verification_profile": "old-profile",
        "profile_fingerprint": "old-fingerprint",
    }
    evidence = replace(case["evidence"], metrics=metrics)

    def is_old_profile(value):
        return (
            value.get("verification_profile") == "old-profile" and value.get("profile_fingerprint") == "old-fingerprint"
        )

    # Reproduce the previous writer's contract; the reader under test is unpatched.
    with monkeypatch.context() as previous:
        previous.setattr(store_module, "POLICY_VERSION", "old-policy")
        previous.setattr(store_module, "is_current_profile", is_old_profile)
        result = finish(case, evidence=(evidence,))
        edits = [{"range_id": "intro", "review_status": "approved", "locked": True}]
        if manual_keep:
            edits.append({"action": "keep", "start_ms": 30000, "end_ms": 35000, "locked": True})
        return case["store"].save_review(case["version"].id, result.result_revision, edits, "owner")


def test_old_persisted_approval_is_stale_for_lookup_review_and_publish(case, monkeypatch):
    approved = approve_old_profile(case, monkeypatch)
    case["marker"].close()
    reopened = MarkerStore(str(case["marker_path"]))
    try:
        store = RepriseStore(reopened)
        service = RepriseService(store, case["registry"], None)
        result = store.get_result(case["version"].id)
        assert result.freshness == "stale"
        assert any(r.review_status == "approved" for r in result.ranges)
        lookup = service.lookup(case["identity"])
        assert lookup["resolution"] == "resolved"
        assert lookup["freshness"] == "stale"
        assert lookup["eligible_skip_ranges"] == []
        # A lookup has no job manager to enqueue work through.
        with pytest.raises(ValueError, match="Analysis is unavailable or stale"):
            service.review(
                case["version"].id,
                approved.result_revision,
                [{"range_id": "intro", "review_status": "approved"}],
                "owner",
            )
        publisher = ReprisePublisher(store, case["registry"], service, writes_enabled=True)
        publisher._database_for = case["publisher"]._database_for
        with pytest.raises(PublicationConflict, match="no longer current"):
            publisher.prepare(case["version"].id, approved.result_revision, case["identity"], "owner")
    finally:
        reopened.close()


def test_reanalysis_after_profile_change_preserves_owner_lock_but_resets_skip(case, monkeypatch):
    approved = approve_old_profile(case, monkeypatch, manual_keep=True)
    keep = next(r for r in approved.ranges if r.origin == "manual" and r.action == "keep")
    updated = finish(case, "reanalyze")
    skip = next(r for r in updated.ranges if r.id == "intro")
    assert updated.freshness == "current"
    assert updated.result_revision > approved.result_revision
    assert skip.locked and skip.review_status == "proposed"
    assert keep in updated.ranges
    assert not case["service"].lookup(case["identity"])["eligible_skip_ranges"]


def test_unchanged_current_profile_reanalysis_preserves_approval(case):
    approved = approve(case)
    eligible = case["service"].lookup(case["identity"])["eligible_skip_ranges"]
    updated = finish(case, "reanalyze")
    assert updated.freshness == "current"
    assert updated.result_revision == approved.result_revision
    assert next(r for r in updated.ranges if r.id == "intro").review_status == "approved"
    assert case["service"].lookup(case["identity"])["eligible_skip_ranges"] == eligible
    assert eligible


def test_old_policy_with_no_evidence_is_stale_after_reopen(case):
    result = finish(case, evidence=(), ranges=())
    with case["store"].transaction() as db:
        db.execute(
            "UPDATE reprise_decision_results SET policy_version=? WHERE version_id=?",
            ("old-policy", case["version"].id),
        )
    case["marker"].close()
    reopened = MarkerStore(str(case["marker_path"]))
    try:
        restored = RepriseStore(reopened).get_result(case["version"].id)
        assert restored.result_revision == result.result_revision
        assert restored.evidence == ()
        assert restored.freshness == "stale"
    finally:
        reopened.close()


def test_mixed_persisted_profile_evidence_is_stale(case):
    approve(case)
    bad = replace(
        case["evidence"], id="old-match", metrics={**case["evidence"].metrics, "profile_fingerprint": "old-fingerprint"}
    )
    with case["store"].transaction() as db:
        db.execute(
            "INSERT INTO reprise_evidence_windows VALUES (?,?,?)", (case["version"].id, bad.id, json.dumps(asdict(bad)))
        )
    result = case["store"].get_result(case["version"].id)
    assert len(result.evidence) == 2
    assert result.freshness == "stale"
    assert case["service"].lookup(case["identity"])["eligible_skip_ranges"] == []


def test_policy_rejects_mixed_profiles_even_if_caller_claims_current(case):
    result = approve(case)
    assert eligible_ranges(result, "1")
    bad = replace(case["evidence"], id="old", metrics={**case["evidence"].metrics, "profile_fingerprint": "old"})
    assert not eligible_ranges(replace(result, evidence=(*result.evidence, bad)), "1")
    assert not eligible_ranges(replace(result, policy_version="old"), "1")


@pytest.mark.parametrize("invalid", ["missing_id", "missing_fingerprint", "wrong_id", "wrong_fingerprint", "mixed"])
def test_finish_rejects_missing_wrong_or_mixed_profile_evidence(case, invalid):
    valid = case["evidence"]
    metrics = dict(valid.metrics)
    if invalid == "missing_id":
        metrics.pop("verification_profile")
    elif invalid == "missing_fingerprint":
        metrics.pop("profile_fingerprint")
    elif invalid == "wrong_id":
        metrics["verification_profile"] = "old-profile"
    else:
        metrics["profile_fingerprint"] = "old-fingerprint"
    bad = replace(valid, id="bad" if invalid == "mixed" else valid.id, metrics=metrics)
    evidence = (valid, bad) if invalid == "mixed" else (bad,)
    store, version = case["store"], case["version"]
    run = store.begin_analysis(version.id, "bad-job", "stable-signature")
    with pytest.raises(ValueError, match="evidence verification profile is not current"):
        store.finish_analysis(run, evidence, (case["proposal"],), version.id)
    assert store.get_result(version.id).analysis_status != "complete"
    assert not case["service"].lookup(case["identity"])["eligible_skip_ranges"]
