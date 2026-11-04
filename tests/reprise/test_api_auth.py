"""Real SQLite credentials, service and Flask HTTP boundary regressions."""

import json
import time
from dataclasses import asdict, replace

from flask import Flask

from media_preview_generator.markers.store import MarkerStore
from media_preview_generator.reprise.identity import media_version
from media_preview_generator.reprise.models import PlaybackIdentity
from media_preview_generator.reprise.service import RepriseService, _mapping_fingerprint
from media_preview_generator.reprise.store import RepriseStore
from media_preview_generator.servers.registry import ServerRegistry
from media_preview_generator.web.routes.api_reprise import create_reprise_blueprint


def test_client_auth_identity_rotation_and_no_owner_privilege(tmp_path, monkeypatch):
    monkeypatch.setenv("WEB_AUTH_TOKEN", "owner-test-only")
    monkeypatch.setattr("media_preview_generator.web.auth.is_auth_external", lambda: True)
    markers = MarkerStore(str(tmp_path / "markers.db"))
    store = RepriseStore(markers)
    service = RepriseService(store, ServerRegistry(), None)
    app = Flask(__name__)
    app.secret_key = "throwaway"
    app.register_blueprint(create_reprise_blueprint(service))
    client = app.test_client()
    media = tmp_path / "episode.mp4"
    media.write_bytes(b"1234567890")
    version = media_version(str(media), 10000, "{}")
    identity = PlaybackIdentity("machine", "11", "12", "13", "/library/parts/13/file", 10, 10000, "14")
    store.register_media(version, identity, server_id="server", metadata={"library_id": "library"})
    created = client.post("/api/reprise/clients", json={"name": "Player"}).get_json()
    token = created["secret"]
    headers = {"Authorization": "Bearer " + token}
    assert client.get("/api/v1/results").status_code == 401
    unresolved = client.get("/api/v1/results", headers=headers)
    assert unresolved.status_code == 200
    assert unresolved.headers["Cache-Control"] == "no-store"
    assert unresolved.json["resolution"] == "unresolved"
    assert unresolved.json["eligible_skip_ranges"] == []
    result = client.get("/api/v1/results", headers=headers, query_string=asdict(identity))
    assert result.status_code == 200
    assert result.json["eligible_skip_ranges"] == []
    assert str(media) not in result.get_data(as_text=True)
    assert client.get("/api/reprise/clients", headers=headers).status_code == 403
    assert client.post("/api/v1/analyses", headers=headers, json={"media_version_ids": [version.id]}).status_code == 403
    rotated = client.post("/api/reprise/clients/" + created["id"] + "/rotate").get_json()
    assert token != rotated["secret"]
    assert client.get("/api/v1/results", headers=headers).status_code == 401
    assert client.get("/api/reprise/clients", headers=headers).status_code == 403
    assert client.get("/api/v1/results", headers={"Authorization": "Bearer " + rotated["secret"]}).status_code == 200
    assert client.delete("/api/reprise/clients/" + rotated["id"]).status_code == 204
    assert client.get("/api/v1/results", headers={"Authorization": "Bearer " + rotated["secret"]}).status_code == 401
    markers.close()


def test_library_pages_hydrate_only_requested_rows_at_catalogue_scale(tmp_path, monkeypatch):
    monkeypatch.setattr("media_preview_generator.web.auth.is_auth_external", lambda: True)
    media = tmp_path / "episode.mp4"
    media.write_bytes(b"1234567890")
    base = media_version(str(media), 10000, "{}")
    registry = ServerRegistry.from_settings(
        [
            {
                "id": "server",
                "name": "Test",
                "enabled": True,
                "type": "plex",
                "url": "http://127.0.0.1:9",
                "auth": {},
                "server_identity": "machine",
                "libraries": [{"id": "library", "name": "TV", "enabled": True, "remote_paths": [str(tmp_path)]}],
            }
        ]
    )
    cfg = registry.get_config("server")
    common = {
        "library_id": "library",
        "remote_part_path": str(media),
        "mapping_fingerprint": _mapping_fingerprint(cfg, cfg.libraries[0]),
    }
    markers = MarkerStore(str(tmp_path / "markers.db"))
    store = RepriseStore(markers)
    with store.transaction() as c:
        for i in range(4239):
            version = replace(base, id=str(i))
            identity = PlaybackIdentity("machine", str(i), str(i), str(i), f"/part/{i}", 10, 10000, "a")
            metadata = {
                **common,
                "title": f"Episode {i}",
                "show_title": "Alpha" if i < 4000 else "Beta",
                "season": 1 if i < 4000 else 2,
                "episode": i,
            }
            c.execute("INSERT INTO reprise_media_versions VALUES (?,?)", (version.id, json.dumps(asdict(version))))
            c.execute(
                "INSERT INTO reprise_bindings VALUES (?,?,?,?,?,?,?,?,?,1)",
                (
                    "machine",
                    str(i),
                    str(i),
                    str(i),
                    "a",
                    version.id,
                    "server",
                    json.dumps(asdict(identity)),
                    json.dumps(metadata),
                ),
            )
            if i < 13:
                duplicate = replace(identity, audio_stream_id="b")
                c.execute(
                    "INSERT INTO reprise_bindings VALUES (?,?,?,?,?,?,?,?,?,1)",
                    (
                        "machine",
                        str(i),
                        str(i),
                        str(i),
                        "b",
                        version.id,
                        "server",
                        json.dumps(asdict(duplicate)),
                        json.dumps(metadata),
                    ),
                )
    service = RepriseService(store, registry, None)
    calls = []
    lookup = service._lookup

    def counted(identity):
        calls.append(identity)
        return lookup(identity)

    monkeypatch.setattr(service, "_lookup", counted)
    app = Flask(__name__)
    app.secret_key = "throwaway"
    app.register_blueprint(create_reprise_blueprint(service))
    client = app.test_client()
    started = time.perf_counter()
    first = client.get("/api/reprise/media")
    elapsed = time.perf_counter() - started
    assert first.status_code == 200
    assert len(first.json["items"]) == 50 and first.json["next_cursor"] == "50"
    assert first.json["shows"] == ["Alpha", "Beta"] and first.json["seasons"] == [1, 2]
    assert len(calls) == 50 and len(first.json["items"][0]["identities"]) == 2
    assert all(item["freshness"] == "current" and item["analysis_status"] == "missing" for item in first.json["items"])
    calls.clear()
    last = client.get("/api/reprise/media", query_string={"cursor": "4200"})
    assert last.status_code == 200 and len(last.json["items"]) == 39 and last.json["next_cursor"] is None
    assert len(calls) == 39 and last.json["items"][0]["id"] == "4200"
    calls.clear()
    filtered = client.get("/api/reprise/media", query_string={"show": "beta", "season": "2", "search": "4225"})
    assert filtered.status_code == 200 and [item["id"] for item in filtered.json["items"]] == ["4225"]
    assert filtered.json["seasons"] == [2] and len(calls) == 1
    print(f"catalogue=4239 versions, 4252 bindings; first page=50; elapsed={elapsed:.3f}s")
    markers.close()
