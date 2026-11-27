"""Season artwork is a bounded, owner-only read of the episode's configured Plex server."""

import io
import threading
from dataclasses import replace

import pytest
from flask import Flask, Response, jsonify, request
from PIL import Image
from werkzeug.serving import make_server

from media_preview_generator.markers.store import MarkerStore
from media_preview_generator.reprise import artwork
from media_preview_generator.reprise import service as reprise_service
from media_preview_generator.reprise.identity import media_version
from media_preview_generator.reprise.models import PlaybackIdentity
from media_preview_generator.reprise.store import RepriseStore
from media_preview_generator.servers.registry import ServerRegistry
from media_preview_generator.web.routes.api_reprise import create_reprise_blueprint

_SEASON_THUMB = "/library/metadata/20/thumb/1660000000"
_SHOW_THUMB = "/library/metadata/30/thumb/1660000001"
_METADATA_PATH = "/library/metadata/10"
_TRANSCODE_PATH = "/photo/:/transcode"


@pytest.fixture
def studio(tmp_path, monkeypatch):
    media = tmp_path / "episode.mkv"
    media.write_bytes(b"registered file contents, not video and never decoded")
    version = media_version(str(media), 6000, "{}")
    identity = PlaybackIdentity("machine", "10", "11", "12", "/library/parts/12/file", version.size_bytes, 6000, "13")
    part = {"id": 12, "key": identity.part_key + "?download=1", "file": str(media), "size": version.size_bytes}
    episode = {
        "type": "episode",
        "ratingKey": "10",
        "parentRatingKey": "20",
        "grandparentRatingKey": "30",
        "parentThumb": _SEASON_THUMB,
        "grandparentThumb": _SHOW_THUMB,
        "parentIndex": 1,
        "Media": [{"id": 11, "Part": [part]}],
    }
    with io.BytesIO() as buf:
        Image.new("RGB", (2, 3), "orchid").save(buf, format="PNG")
        poster = buf.getvalue()

    calls = []
    replies = {}
    plex = Flask("fixture-plex")

    @plex.get("/<path:path>")
    def answer(path):
        path = "/" + path
        query = request.args.to_dict()
        calls.append((path, query, request.headers.get("X-Plex-Token"), request.headers.get("Accept-Encoding")))
        if path in replies:
            return replies[path]()
        if path == _METADATA_PATH:
            return jsonify(MediaContainer={"Metadata": [episode], "librarySectionID": 1})
        if path == _TRANSCODE_PATH:
            return Response(poster, mimetype="image/png")
        return Response(status=404)

    server = make_server("127.0.0.1", 0, plex, threaded=True)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    registry = ServerRegistry.from_settings(
        [
            {
                "id": "plex",
                "type": "plex",
                "url": f"http://127.0.0.1:{server.server_port}",
                "auth": {"token": "fixture-not-a-real-token"},
                "server_identity": "machine",
                "libraries": [{"id": "1", "name": "TV", "remote_paths": [str(tmp_path)]}],
            }
        ]
    )
    markers = MarkerStore(str(tmp_path / "markers.db"))
    store = RepriseStore(markers)
    service = reprise_service.RepriseService(store, registry, None)
    cfg = registry.get_config("plex")
    saved = {
        "library_id": "1",
        "server_url": cfg.url,
        "title": "Study Group",
        "show_title": "Community",
        "show_key": "30",
        "season": 1,
        "episode": 1,
        "remote_part_path": str(media),
        "mapping_fingerprint": reprise_service._mapping_fingerprint(cfg, cfg.libraries[0]),
    }
    store.register_media(version, identity, server_id="plex", metadata=saved)

    monkeypatch.setattr("media_preview_generator.web.auth.is_auth_external", lambda: False)
    monkeypatch.setattr("media_preview_generator.web.auth.session_epoch", lambda: "poster-test")
    app = Flask(__name__)
    app.secret_key = "test-owner"
    app.register_blueprint(create_reprise_blueprint(service))
    owner = app.test_client()
    with owner.session_transaction() as session:
        session.update(authenticated=True, epoch="poster-test")

    try:
        yield owner, service, registry, version, identity, saved, episode, poster, calls, replies, media
    finally:
        server.shutdown()
        thread.join(timeout=1)
        server.server_close()
        markers.close()


def _url(version):
    return f"/api/reprise/media/{version.id}/poster"


def test_old_episode_has_real_owner_private_artwork_without_refreshing(studio, monkeypatch):
    owner, _, _, version, _, _, _, poster, calls, _, _ = studio
    monkeypatch.setattr(reprise_service, "ffmpeg_binary", lambda: pytest.fail("Browsing must never decode video"))
    row = owner.get("/api/reprise/media").json["items"][0]
    assert not calls  # Browsing the saved list does not contact Plex.
    assert row["poster_url"] == _url(version)
    response = owner.get(row["poster_url"])
    assert response.status_code == 200
    assert response.data == poster and response.mimetype == "image/png"
    assert response.headers["Cache-Control"] == "private, max-age=300"
    assert set(response.vary) == {"Cookie", "Authorization", "X-Auth-Token"}
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert len(calls) == 2
    assert calls[0] == (_METADATA_PATH, {}, "fixture-not-a-real-token", "identity")
    assert calls[1] == (
        _TRANSCODE_PATH,
        {"url": _SEASON_THUMB, "width": "96", "height": "144", "minSize": "1"},
        "fixture-not-a-real-token",
        "identity",
    )
    assert b"fixture-not-a-real-token" not in response.data
    assert not any("fixture-not-a-real-token" in value for _, value in response.headers.items())


def test_unknown_inactive_stale_and_non_owner_requests_do_not_contact_plex(studio):
    owner, service, _, version, identity, _, _, _, calls, _, media = studio
    url = _url(version)
    player = owner.post("/api/reprise/clients", json={"name": "Player"}).json["secret"]
    assert owner.application.test_client().get(url).status_code == 401
    assert owner.get(url, headers={"Authorization": f"Bearer {player}"}).status_code == 403
    assert owner.get(url + "?url=/status/sessions").status_code == 400
    assert owner.head(url).status_code == 405
    assert owner.get("/api/reprise/media/unknown/poster").status_code == 404
    media.write_bytes(b"replaced")
    assert owner.get(url).status_code == 404
    service.store.reconcile_library("plex", "1", [], keep_rating_keys=frozenset())
    assert not service.store.resolve_binding(identity)
    assert owner.get(url).status_code == 404
    assert not calls


def test_films_omit_poster_links_and_audio_aliases_share_one_poster(studio):
    owner, service, _, version, identity, saved, _, _, calls, _, _ = studio
    service.store.register_media(version, replace(identity, audio_stream_id="14"), server_id="plex", metadata=saved)
    assert owner.get("/api/reprise/media").json["items"][0]["poster_url"] == _url(version)
    assert owner.get(_url(version)).status_code == 200
    assert len(calls) == 2
    service.store.reconcile_library("plex", "1", [])
    service.store.register_media(version, identity, server_id="plex", metadata={**saved, "kind": "movie"})
    assert "poster_url" not in owner.get("/api/reprise/media").json["items"][0]
    assert owner.get(_url(version)).status_code == 404
    assert len(calls) == 2


@pytest.mark.parametrize(
    "parent_thumb", [None, "https://plex.invalid/library/metadata/20/thumb", "/library/metadata/99/thumb"]
)
def test_show_fallback_only_uses_verified_relative_metadata_paths(studio, parent_thumb):
    owner, _, _, version, _, _, episode, poster, calls, _, _ = studio
    episode["parentThumb"] = parent_thumb
    assert owner.get(_url(version)).data == poster
    assert [query["url"] for path, query, *_ in calls if path == _TRANSCODE_PATH] == [_SHOW_THUMB]


def test_show_fallback_when_the_season_thumb_disappears(studio):
    owner, _, _, version, _, _, _, poster, calls, replies, _ = studio

    def reply():
        if request.args["url"] == _SEASON_THUMB:
            return Response(status=404)
        return Response(poster, mimetype="image/png")

    replies[_TRANSCODE_PATH] = reply
    assert owner.get(_url(version)).data == poster
    assert [query["url"] for path, query, *_ in calls if path == _TRANSCODE_PATH] == [_SEASON_THUMB, _SHOW_THUMB]


@pytest.mark.parametrize(
    "wrong", [{"ratingKey": "99"}, {"parentIndex": 9}, {"grandparentRatingKey": "99"}, {"Media": []}]
)
def test_metadata_must_still_match_registered_episode_and_part(studio, wrong):
    owner, _, _, version, _, _, episode, _, calls, _, _ = studio
    episode.update(wrong)
    assert owner.get(_url(version)).status_code == 404
    assert [path for path, *_ in calls] == [_METADATA_PATH]


def test_offline_plex_does_not_affect_local_browsing(studio):
    owner, _, _, version, _, _, _, _, calls, replies, _ = studio
    replies[_METADATA_PATH] = lambda: Response(status=503)
    assert owner.get(_url(version)).status_code == 404
    assert owner.get("/api/reprise/media").json["items"][0]["title"] == "Study Group"
    assert [path for path, *_ in calls] == [_METADATA_PATH]


@pytest.mark.parametrize("stage", [_METADATA_PATH, _TRANSCODE_PATH])
def test_redirects_are_never_followed_even_on_configured_plex(studio, stage):
    owner, _, _, version, _, _, episode, _, calls, replies, _ = studio
    episode.pop("grandparentThumb")
    replies[stage] = lambda: Response(status=302, headers={"Location": "/status/sessions"})
    response = owner.get(_url(version))
    assert response.status_code == 404 and response.headers["Cache-Control"] == "no-store"
    assert [path for path, *_ in calls] == ([_METADATA_PATH] if stage == _METADATA_PATH else [_METADATA_PATH, stage])


@pytest.mark.parametrize(
    "response",
    [
        lambda: Response(b"<svg />", mimetype="image/svg+xml"),
        lambda: Response(b"<svg />", mimetype="image/png"),
        lambda: Response(b"z" * (artwork._MAX_BYTES + 1), mimetype="image/png"),
        lambda: Response(iter([b"z" * (artwork._MAX_BYTES + 1)]), mimetype="image/png"),
        lambda: Response(b"z", mimetype="image/png", headers={"Content-Encoding": "gzip"}),
    ],
)
def test_non_image_compressed_and_oversize_upstream_bodies_are_not_served(studio, response):
    owner, _, _, version, _, _, episode, _, _, replies, _ = studio
    episode.pop("grandparentThumb")
    replies[_TRANSCODE_PATH] = response
    assert owner.get(_url(version)).status_code == 404


def test_busy_readers_omit_artwork_and_release_slots_after_timeout(studio, monkeypatch):
    owner, _, _, version, _, _, _, poster, calls, replies, _ = studio
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(artwork, "_SLOTS", slots)
    monkeypatch.setattr(artwork, "_IO_TIMEOUT_S", 0.15)
    assert slots.acquire(blocking=False)
    try:
        assert owner.get(_url(version)).status_code == 404
        assert not calls
    finally:
        slots.release()

    wait_for_test = threading.Event()

    def too_slow():
        wait_for_test.wait(2)
        return Response(status=503)

    replies[_METADATA_PATH] = too_slow
    try:
        assert owner.get(_url(version)).status_code == 404
    finally:
        wait_for_test.set()
    replies.clear()
    assert owner.get(_url(version)).data == poster
