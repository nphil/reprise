"""The generic probe talks HTTP to the actual blueprint, service and database."""

import json
import os
import subprocess
import sys
from pathlib import Path
from threading import Thread

from flask import Flask
from werkzeug.serving import make_server

from media_preview_generator.markers.store import MarkerStore
from media_preview_generator.reprise.service import RepriseService
from media_preview_generator.reprise.store import RepriseStore
from media_preview_generator.servers.registry import ServerRegistry
from media_preview_generator.web.routes.api_reprise import create_reprise_blueprint


def test_probe_over_http_against_real_store(tmp_path):
    markers = MarkerStore(str(tmp_path / "markers.db"))
    store = RepriseStore(markers)
    secret = store.create_client("HTTP probe")["secret"]
    app = Flask(__name__)
    app.secret_key = "throwaway"
    app.register_blueprint(create_reprise_blueprint(RepriseService(store, ServerRegistry(), None)))
    server = make_server("127.0.0.1", 0, app)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    probe = Path(__file__).resolve().parents[2] / "tools" / "reprise_probe.py"
    try:
        run = subprocess.run(
            [
                sys.executable,
                str(probe),
                f"http://127.0.0.1:{server.server_port}",
                "--plex-machine-identifier",
                "machine",
                "--rating-key",
                "1",
                "--media-id",
                "2",
                "--part-id",
                "3",
                "--part-key",
                "/library/parts/3/file",
                "--part-size-bytes",
                "10",
                "--duration-ms",
                "5000",
                "--audio-stream-id",
                "4",
            ],
            env={**os.environ, "REPRISE_TOKEN": secret},
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert run.returncode == 0, run.stderr
        result = json.loads(run.stdout)
        assert result["resolution"] == "unresolved"
        assert result["eligible_skip_ranges"] == []
        assert secret not in run.stdout + run.stderr
    finally:
        server.shutdown()
        thread.join()
        markers.close()


def test_verified_stream_ranges_and_replaced_media(tmp_path, monkeypatch):
    from media_preview_generator.reprise.identity import media_version
    from media_preview_generator.reprise.models import PlaybackIdentity
    from media_preview_generator.reprise.service import _mapping_fingerprint
    from media_preview_generator.servers.base import Library, ServerConfig, ServerType

    monkeypatch.setattr("media_preview_generator.web.auth.is_auth_external", lambda: True)
    markers = MarkerStore(str(tmp_path / "markers.db"))
    store = RepriseStore(markers)
    registry = ServerRegistry()
    registry._configs["server"] = ServerConfig(
        id="server",
        type=ServerType.PLEX,
        name="Plex",
        enabled=True,
        url="http://127.0.0.1",
        auth={},
        libraries=[Library("library", "TV", (str(tmp_path),))],
        server_identity="machine",
    )
    path = tmp_path / "episode.mp4"
    path.write_bytes(b"0123456789")
    version = media_version(str(path), 5000, "{}")
    identity = PlaybackIdentity("machine", "1", "2", "3", "/library/parts/3/file", 10, 5000, "4")
    config = registry.get_config("server")
    metadata = {
        "library_id": "library",
        "server_url": config.url,
        "remote_part_path": str(path),
        "mapping_fingerprint": _mapping_fingerprint(config, config.libraries[0]),
    }
    store.register_media(version, identity, server_id="server", metadata=metadata)
    app = Flask(__name__)
    app.secret_key = "throwaway"
    app.register_blueprint(create_reprise_blueprint(RepriseService(store, registry, None)))
    client = app.test_client()
    url = "/api/reprise/media/" + version.id + "/stream"
    assert client.get(url).data == b"0123456789"
    partial = client.get(url, headers={"Range": "bytes=2-5"})
    assert partial.status_code == 206 and partial.data == b"2345"
    assert partial.headers["Content-Range"] == "bytes 2-5/10"
    assert client.head(url, headers={"Range": "bytes=2-5"}).headers["Content-Length"] == "4"
    assert client.get(url, headers={"Range": "bytes=99-"}).status_code == 416
    path.write_bytes(b"replacement")
    assert client.get(url).status_code == 404
    markers.close()
