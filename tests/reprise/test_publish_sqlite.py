"""Publication regression tests against isolated, real SQLite databases."""

import json
import sqlite3
from dataclasses import replace

import pytest

from media_preview_generator.markers.publishers.plex_db import LocalPlexDb
from media_preview_generator.markers.store import MarkerStore
from media_preview_generator.reprise.admission import admit_references
from media_preview_generator.reprise.identity import media_version
from media_preview_generator.reprise.models import DecisionRange, Evidence, PlaybackIdentity
from media_preview_generator.reprise.service import RepriseService, _mapping_fingerprint
from media_preview_generator.reprise.store import RepriseStore
from media_preview_generator.reprise.verification_profile import PROFILE_FINGERPRINT, PROFILE_ID
from media_preview_generator.servers.registry import ServerRegistry
from scripts.plex_db_node_fixture import build


def _rows(path):
    with sqlite3.connect(path) as db:
        rows = {
            table: db.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
            for table in ("tags", "taggings", "metadata_items", "media_items", "media_parts", "media_streams")
        }
        rows["sqlite_sequence"] = db.execute("SELECT * FROM sqlite_sequence ORDER BY name").fetchall()
        return rows


def _metadata(registry, path, episode=1):
    cfg = registry.get_config("local")
    return {
        "library_id": "1",
        "show_key": "show",
        "season": 1,
        "episode": episode,
        "remote_part_path": str(path),
        "mapping_fingerprint": _mapping_fingerprint(cfg, cfg.libraries[0]),
    }


@pytest.fixture
def case(tmp_path, monkeypatch):
    from media_preview_generator.reprise.publishing import ReprisePublisher

    media = tmp_path / "library" / "episode.mkv"
    media.parent.mkdir()
    media.write_bytes(b"private-media-fixture")
    reference = media.parent / "reference.mkv"
    reference.write_bytes(b"reference-fixture")
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
                "libraries": [{"id": "1", "name": "fixture", "enabled": True, "remote_paths": [str(media.parent)]}],
            }
        ]
    )
    marker_path = tmp_path / "markers.db"
    marker = MarkerStore(str(marker_path))
    store = RepriseStore(marker)
    signature = json.dumps({"audio_streams": [{"plex_id": "1", "index": 1}], "start_time_ms": 0, "streams": []})
    version = media_version(str(media), 100000, signature)
    ref_version = media_version(str(reference), 100000, signature)
    identity = PlaybackIdentity(
        "fixture-machine", "1", "1", "1", "/library/parts/1/file.mkv", media.stat().st_size, 100000, "1"
    )
    ref_identity = replace(
        identity,
        rating_key="20",
        media_id="20",
        part_id="20",
        part_key="/library/parts/20/file.mkv",
        part_size_bytes=reference.stat().st_size,
    )
    store.register_media(version, identity, server_id="local", metadata=_metadata(registry, media))
    store.register_media(ref_version, ref_identity, server_id="local", metadata=_metadata(registry, reference, 2))
    [(_, provenance)] = admit_references(version, [ref_version], store)
    evidence = Evidence(
        "match",
        10000,
        20000,
        "audiovisual",
        "match",
        "same",
        "",
        ref_version.id,
        10000,
        20000,
        {"checked_audio_stream_ids": ["1"], "admission_provenance": provenance,
         "verification_profile": PROFILE_ID, "profile_fingerprint": PROFILE_FINGERPRINT},
    )
    proposal = DecisionRange(
        "intro", "intro", "skip", 10000, 20000, "proposed", False, "detector", "same", "", ("match",), ("1",)
    )
    run = store.begin_analysis(version.id, "fixture-job", "fixture-signature")
    result = store.finish_analysis(run, (evidence,), (proposal,), version.id)
    result = store.save_review(
        version.id,
        result.result_revision,
        [{"range_id": "intro", "review_status": "approved", "locked": True}],
        "owner",
    )
    service = RepriseService(store, registry, None)

    def publisher_for(current_store, current_service):
        publisher = ReprisePublisher(current_store, registry, current_service, writes_enabled=True)
        monkeypatch.setattr(publisher, "_database_for", lambda server_id, identity: LocalPlexDb(lambda: str(plex)))
        return publisher

    publisher = publisher_for(store, service)
    yield {
        "plex": plex,
        "marker": marker,
        "marker_path": marker_path,
        "store": store,
        "service": service,
        "publisher": publisher,
        "publisher_for": publisher_for,
        "identity": identity,
        "version": version,
        "result": result,
    }
    if not marker._closed:
        marker.close()


def _prepare(c):
    return c["publisher"].prepare(c["version"].id, c["result"].result_revision, c["identity"], "owner")["id"]


def test_prepare_apply_reopen_undo_restores_every_row(case):
    c = case
    before = _rows(c["plex"])
    change = _prepare(c)
    assert _rows(c["plex"]) == before
    c["publisher"].apply(change, "owner")
    after = _rows(c["plex"])
    assert after["taggings"] != before["taggings"]
    assert after["media_parts"] != before["media_parts"]
    c["marker"].close()
    reopened = MarkerStore(str(c["marker_path"]))
    try:
        store = RepriseStore(reopened)
        service = RepriseService(store, c["service"].registry, None)
        publisher = c["publisher_for"](store, service)
        publisher.undo(change, "owner")
        assert _rows(c["plex"]) == before
    finally:
        reopened.close()


def test_external_edit_after_prepare_refuses_apply(case):
    c = case
    change = _prepare(c)
    with sqlite3.connect(c["plex"]) as db:
        db.execute("UPDATE media_parts SET extra_data='external-edit' WHERE id=1")
    edited = _rows(c["plex"])
    with pytest.raises((ValueError, RuntimeError)):
        c["publisher"].apply(change, "owner")
    assert _rows(c["plex"]) == edited


def test_external_edit_after_apply_refuses_undo(case):
    c = case
    change = _prepare(c)
    c["publisher"].apply(change, "owner")
    with sqlite3.connect(c["plex"]) as db:
        db.execute("UPDATE taggings SET time_offset=time_offset+1 WHERE metadata_item_id=1")
    edited = _rows(c["plex"])
    with pytest.raises((ValueError, RuntimeError)):
        c["publisher"].undo(change, "owner")
    assert _rows(c["plex"]) == edited


def test_recovery_classifies_committed_write_without_replaying(case, monkeypatch):
    c = case
    change = _prepare(c)
    original = c["publisher"]._set_state

    def lose_ack(change_id, state, actor):
        if state == "applied":
            raise RuntimeError("fixture crash after Plex commit")
        return original(change_id, state, actor)

    monkeypatch.setattr(c["publisher"], "_set_state", lose_ack)
    with pytest.raises(RuntimeError, match="fixture crash"):
        c["publisher"].apply(change, "owner")
    committed = _rows(c["plex"])
    c["marker"].close()
    reopened = MarkerStore(str(c["marker_path"]))
    try:
        store = RepriseStore(reopened)
        service = RepriseService(store, c["service"].registry, None)
        publisher = c["publisher_for"](store, service)
        assert publisher.recover(change)["status"] == "applied"
        assert _rows(c["plex"]) == committed
    finally:
        reopened.close()


def test_recovery_marks_unknown_external_change_uncertain(case, monkeypatch):
    c = case
    change = _prepare(c)
    original = c["publisher"]._set_state

    def lose_ack(change_id, state, actor):
        if state == "applied":
            raise RuntimeError("fixture crash after Plex commit")
        return original(change_id, state, actor)

    monkeypatch.setattr(c["publisher"], "_set_state", lose_ack)
    with pytest.raises(RuntimeError, match="fixture crash"):
        c["publisher"].apply(change, "owner")
    with sqlite3.connect(c["plex"]) as db:
        db.execute("UPDATE taggings SET time_offset=time_offset+1 WHERE metadata_item_id=1")
    edited = _rows(c["plex"])
    monkeypatch.setattr(c["publisher"], "_set_state", original)
    assert c["publisher"].recover(change)["status"] == "uncertain"
    assert _rows(c["plex"]) == edited


def test_unreviewed_alternate_version_is_not_a_winner(case, tmp_path):
    c = case
    other = tmp_path / "library" / "alternate.mkv"
    other.write_bytes(b"alternate-version")
    with sqlite3.connect(c["plex"]) as db:
        db.execute("INSERT INTO media_items (id,metadata_item_id,duration) VALUES (2,1,100000)")
        db.execute(
            "INSERT INTO media_parts (id,media_item_id,file,size,duration,updated_at) VALUES (2,2,?,?,100000,102)",
            (str(other), other.stat().st_size),
        )
        db.execute(
            'INSERT INTO media_streams (id,media_item_id,media_part_id,stream_type_id,"index") VALUES (2,2,2,2,1)'
        )
    before = _rows(c["plex"])
    with pytest.raises((ValueError, RuntimeError)):
        _prepare(c)
    assert _rows(c["plex"]) == before


def test_stacked_parts_are_refused(case, tmp_path):
    c = case
    other = tmp_path / "library" / "second-part.mkv"
    other.write_bytes(b"stacked-part")
    with sqlite3.connect(c["plex"]) as db:
        db.execute(
            "INSERT INTO media_parts (id,media_item_id,file,size,duration,updated_at) VALUES (2,1,?,?,100000,102)",
            (str(other), other.stat().st_size),
        )
    before = _rows(c["plex"])
    with pytest.raises((ValueError, RuntimeError)):
        _prepare(c)
    assert _rows(c["plex"]) == before


def test_revision_change_after_prepare_refuses_apply(case):
    c = case
    change = _prepare(c)
    c["store"].save_review(
        c["version"].id, c["result"].result_revision, [{"range_id": "intro", "review_status": "rejected"}], "owner"
    )
    before = _rows(c["plex"])
    with pytest.raises((ValueError, RuntimeError)):
        c["publisher"].apply(change, "owner")
    assert _rows(c["plex"]) == before


def test_writes_off_does_not_block_review_and_refuses_apply(case):
    from media_preview_generator.reprise.publishing import ReprisePublisher

    c = case
    publisher = ReprisePublisher(c["store"], c["service"].registry, c["service"])
    assert publisher.writes_enabled is False
    change = _prepare(c)
    before = _rows(c["plex"])
    with pytest.raises((ValueError, RuntimeError)):
        publisher.apply(change, "owner")
    assert _rows(c["plex"]) == before
    result = c["store"].save_review(
        c["version"].id, c["result"].result_revision, [{"range_id": "intro", "review_status": "rejected"}], "owner"
    )
    assert result.result_revision > c["result"].result_revision


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE media_parts SET updated_at=updated_at+1 WHERE id=1",
        "UPDATE metadata_items SET title='Externally changed' WHERE id=1",
        "UPDATE media_streams SET codec='changed' WHERE id=1",
    ],
)
def test_prepared_scope_detects_external_metadata_changes(case, sql):
    c = case
    change = _prepare(c)
    with sqlite3.connect(c["plex"]) as db:
        db.execute(sql)
    edited = _rows(c["plex"])
    with pytest.raises((ValueError, RuntimeError)):
        c["publisher"].apply(change, "owner")
    assert _rows(c["plex"]) == edited


def test_unreviewed_shared_file_sibling_is_refused(case):
    c = case
    with sqlite3.connect(c["plex"]) as db:
        file_path, size = db.execute("SELECT file,size FROM media_parts WHERE id=1").fetchone()
        db.execute("INSERT INTO metadata_items (id,library_section_id,metadata_type,title) VALUES (2,1,4,'Sibling')")
        db.execute("INSERT INTO media_items (id,metadata_item_id,duration) VALUES (2,2,100000)")
        db.execute(
            "INSERT INTO media_parts (id,media_item_id,file,size,duration,updated_at) VALUES (2,2,?,?,100000,101)",
            (file_path, size),
        )
        db.execute(
            'INSERT INTO media_streams (id,media_item_id,media_part_id,stream_type_id,"index") VALUES (2,2,2,2,1)'
        )
    before = _rows(c["plex"])
    with pytest.raises((ValueError, RuntimeError)):
        _prepare(c)
    assert _rows(c["plex"]) == before


def test_identically_reviewed_versions_publish_and_undo_together(case, tmp_path):
    c = case
    other = tmp_path / "library" / "alternate.mkv"
    other.write_bytes(b"alternate-version")
    with sqlite3.connect(c["plex"]) as db:
        db.execute("INSERT INTO media_items (id,metadata_item_id,duration) VALUES (2,1,100000)")
        db.execute(
            "INSERT INTO media_parts (id,media_item_id,file,size,duration,updated_at) VALUES (2,2,?,?,100000,102)",
            (str(other), other.stat().st_size),
        )
        db.execute(
            'INSERT INTO media_streams (id,media_item_id,media_part_id,stream_type_id,"index") VALUES (2,2,2,2,1)'
        )
    signature = json.dumps({"audio_streams": [{"plex_id": "2", "index": 1}], "start_time_ms": 0, "streams": []})
    version = media_version(str(other), 100000, signature)
    identity = replace(
        c["identity"],
        media_id="2",
        part_id="2",
        part_key="/library/parts/2/file.mkv",
        part_size_bytes=other.stat().st_size,
        audio_stream_id="2",
    )
    c["store"].register_media(version, identity, server_id="local", metadata=_metadata(c["service"].registry, other))
    source = c["result"].evidence[0]
    reference = c["store"].get_media(source.reference_version_id)
    [(_, provenance)] = admit_references(version, [reference], c["store"])
    evidence = replace(source, metrics={**source.metrics, "checked_audio_stream_ids": ["2"], "admission_provenance": provenance})
    proposal = replace(c["result"].ranges[0], review_status="proposed", locked=False, audio_stream_ids=("2",))
    run = c["store"].begin_analysis(version.id, "job-two", "signature-two")
    result = c["store"].finish_analysis(run, (evidence,), (proposal,), version.id)
    c["store"].save_review(
        version.id,
        result.result_revision,
        [{"range_id": proposal.id, "review_status": "approved", "locked": True}],
        "owner",
    )
    before = _rows(c["plex"])
    change = _prepare(c)
    c["publisher"].apply(change, "owner")
    with sqlite3.connect(c["plex"]) as db:
        parts = db.execute("SELECT id,extra_data FROM media_parts ORDER BY id").fetchall()
    assert len(parts) == 2 and all(value for _, value in parts)
    c["publisher"].undo(change, "owner")
    assert _rows(c["plex"]) == before


def test_recovery_classifies_interrupted_intent_before_plex_write(case):
    c = case
    change = _prepare(c)
    before = _rows(c["plex"])
    c["publisher"]._set_state(change, "applying", "owner")
    assert c["publisher"].recover(change)["status"] == "not_applied"
    assert _rows(c["plex"]) == before


def test_undo_refuses_unrelated_later_tagging_insertion(case):
    c = case
    change = _prepare(c)
    c["publisher"].apply(change, "owner")
    with sqlite3.connect(c["plex"]) as db:
        db.execute("INSERT INTO taggings (metadata_item_id,tag_id,text) VALUES (999,12,'intro')")
    edited = _rows(c["plex"])
    with pytest.raises((ValueError, RuntimeError)):
        c["publisher"].undo(change, "owner")
    assert _rows(c["plex"]) == edited


def test_unapproved_existing_marker_type_is_not_deleted(case):
    c = case
    with sqlite3.connect(c["plex"]) as db:
        db.execute(
            "INSERT INTO taggings (metadata_item_id,tag_id,[index],text,time_offset,end_time_offset,extra_data) VALUES (1,12,0,'credits',60000,80000,'')"
        )
    before = _rows(c["plex"])
    with pytest.raises((ValueError, RuntimeError)):
        _prepare(c)
    assert _rows(c["plex"]) == before


def test_part_path_must_match_the_reviewed_file(case, tmp_path):
    c = case
    other = tmp_path / "library" / "same-size-other.mkv"
    other.write_bytes(b"x" * c["identity"].part_size_bytes)
    with sqlite3.connect(c["plex"]) as db:
        db.execute("UPDATE media_parts SET file=? WHERE id=1", (str(other),))
    before = _rows(c["plex"])
    with pytest.raises((ValueError, RuntimeError)):
        _prepare(c)
    assert _rows(c["plex"]) == before


def test_reviewed_shared_file_sibling_changes_and_restores_together(case):
    c = case
    with sqlite3.connect(c["plex"]) as db:
        file_path, size = db.execute("SELECT file,size FROM media_parts WHERE id=1").fetchone()
        db.execute("INSERT INTO metadata_items (id,library_section_id,metadata_type,title) VALUES (2,1,4,'Sibling')")
        db.execute("INSERT INTO media_items (id,metadata_item_id,duration) VALUES (2,2,100000)")
        db.execute(
            "INSERT INTO media_parts (id,media_item_id,file,size,duration,updated_at) VALUES (2,2,?,?,100000,101)",
            (file_path, size),
        )
        db.execute(
            "INSERT INTO media_streams (id,media_item_id,media_part_id,stream_type_id,[index]) VALUES (2,2,2,2,1)"
        )
    signature = json.dumps({"audio_streams": [{"plex_id": "2", "index": 1}], "start_time_ms": 0, "streams": []})
    version = media_version(file_path, 100000, signature)
    identity = replace(
        c["identity"],
        rating_key="2",
        media_id="2",
        part_id="2",
        part_key="/library/parts/2/file.mkv",
        audio_stream_id="2",
    )
    c["store"].register_media(
        version, identity, server_id="local", metadata=_metadata(c["service"].registry, version.canonical_path)
    )
    source = c["result"].evidence[0]
    reference = c["store"].get_media(source.reference_version_id)
    [(_, provenance)] = admit_references(version, [reference], c["store"])
    evidence = replace(source, metrics={**source.metrics, "checked_audio_stream_ids": ["2"], "admission_provenance": provenance})
    proposal = replace(c["result"].ranges[0], review_status="proposed", locked=False, audio_stream_ids=("2",))
    run = c["store"].begin_analysis(version.id, "sibling-job", "sibling-signature")
    result = c["store"].finish_analysis(run, (evidence,), (proposal,), version.id)
    c["store"].save_review(
        version.id,
        result.result_revision,
        [{"range_id": proposal.id, "review_status": "approved", "locked": True}],
        "owner",
    )
    before = _rows(c["plex"])
    change = _prepare(c)
    c["publisher"].apply(change, "owner")
    with sqlite3.connect(c["plex"]) as db:
        assert {r[0] for r in db.execute("SELECT metadata_item_id FROM taggings WHERE tag_id=12")} == {1, 2}
        assert all(r[0] for r in db.execute("SELECT extra_data FROM media_parts"))
    c["publisher"].undo(change, "owner")
    assert _rows(c["plex"]) == before


def test_recovery_classifies_committed_undo_without_replaying(case, monkeypatch):
    c = case
    before = _rows(c["plex"])
    change = _prepare(c)
    c["publisher"].apply(change, "owner")
    original = c["publisher"]._set_state

    def lose_ack(change_id, state, actor):
        if state == "undone":
            raise RuntimeError("fixture crash after undo commit")
        return original(change_id, state, actor)

    monkeypatch.setattr(c["publisher"], "_set_state", lose_ack)
    with pytest.raises(RuntimeError, match="fixture crash"):
        c["publisher"].undo(change, "owner")
    assert _rows(c["plex"]) == before
    c["marker"].close()
    reopened = MarkerStore(str(c["marker_path"]))
    try:
        store = RepriseStore(reopened)
        service = RepriseService(store, c["service"].registry, None)
        publisher = c["publisher_for"](store, service)
        assert publisher.recover(change)["status"] == "undone"
        assert _rows(c["plex"]) == before
    finally:
        reopened.close()


def test_writes_disabled_refuses_undo_without_touching_database(case):
    from media_preview_generator.reprise.publishing import ReprisePublisher

    c = case
    change = _prepare(c)
    c["publisher"].apply(change, "owner")
    committed = _rows(c["plex"])
    disabled = ReprisePublisher(c["store"], c["service"].registry, c["service"])
    with pytest.raises((ValueError, RuntimeError)):
        disabled.undo(change, "owner")
    assert _rows(c["plex"]) == committed
    assert c["publisher"]._load(change)["state"] == "applied"


@pytest.mark.parametrize("change_library", ["disabled", "different_root"])
def test_undo_refuses_when_library_scope_changes(case, tmp_path, change_library):
    c = case
    change = _prepare(c)
    c["publisher"].apply(change, "owner")
    committed = _rows(c["plex"])
    cfg = c["service"].registry.get_config("local")
    library = cfg.libraries[0]
    if change_library == "disabled":
        cfg.libraries[0] = replace(library, enabled=False)
    else:
        cfg.libraries[0] = replace(library, remote_paths=(str(tmp_path / "other"),))
    with pytest.raises((ValueError, RuntimeError)):
        c["publisher"].undo(change, "owner")
    assert _rows(c["plex"]) == committed


def test_nonfinal_credits_projection_and_existing_row_restore(case):
    from media_preview_generator.markers.publishers.plex_db import decode_extra_data

    c = case
    original_evidence = c["result"].evidence[0]
    credits_evidence = replace(
        original_evidence,
        id="credits-match",
        start_ms=60000,
        end_ms=80000,
        reference_start_ms=60000,
        reference_end_ms=80000,
    )
    credits = DecisionRange(
        "credits",
        "credits",
        "skip",
        60000,
        80000,
        "proposed",
        False,
        "detector",
        "same",
        "",
        ("credits-match",),
        ("1",),
    )
    run = c["store"].begin_analysis(c["version"].id, "credits-job", "credits-signature")
    result = c["store"].finish_analysis(run, (original_evidence, credits_evidence), (credits,), c["version"].id)
    result = c["store"].save_review(
        c["version"].id,
        result.result_revision,
        [{"range_id": "credits", "review_status": "approved", "locked": True}],
        "owner",
    )
    with sqlite3.connect(c["plex"]) as db:
        db.execute(
            "INSERT INTO taggings (metadata_item_id,tag_id,[index],text,time_offset,end_time_offset,extra_data) VALUES (1,12,0,'credits',48000,72000,'pv%3Aversion=4')"
        )
    before = _rows(c["plex"])
    prepared = c["publisher"].prepare(c["version"].id, result.result_revision, c["identity"], "owner")
    assert {"type": "credits", "start_ms": 60000, "end_ms": 80000, "rating_key": "1"} in prepared["after_markers"]
    c["publisher"].apply(prepared["id"], "owner")
    with sqlite3.connect(c["plex"]) as db:
        row = db.execute(
            "SELECT time_offset,end_time_offset,extra_data FROM taggings WHERE metadata_item_id=1 AND text='credits'"
        ).fetchone()
    assert row[:2] == (58000, 82000)
    assert "pv:final" not in decode_extra_data(row[2])[0]
    c["publisher"].undo(prepared["id"], "owner")
    assert _rows(c["plex"]) == before


@pytest.mark.parametrize("stage", ["prepare", "apply"])
def test_publication_refuses_changed_library_mapping(case, tmp_path, stage):
    c = case
    change = _prepare(c) if stage == "apply" else None
    before = _rows(c["plex"])
    cfg = c["service"].registry.get_config("local")
    cfg.libraries[0] = replace(cfg.libraries[0], remote_paths=(str(tmp_path / "other-root"),))
    with pytest.raises((ValueError, RuntimeError)):
        if change is None:
            _prepare(c)
        else:
            c["publisher"].apply(change, "owner")
    assert _rows(c["plex"]) == before


@pytest.mark.parametrize("extra", ["pv%3Aversion=999", "not-valid-plex-marker"])
def test_unsupported_existing_marker_tagging_is_not_replaced(case, extra):
    c = case
    with sqlite3.connect(c["plex"]) as db:
        db.execute(
            "INSERT INTO taggings (metadata_item_id,tag_id,[index],text,time_offset,end_time_offset,extra_data) VALUES (1,12,0,'intro',10000,20000,?)",
            (extra,),
        )
    before = _rows(c["plex"])
    with pytest.raises((ValueError, RuntimeError)):
        _prepare(c)
    assert _rows(c["plex"]) == before
