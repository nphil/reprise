"""Read-back comparison and capabilities of publishers built by the factory."""

from __future__ import annotations

from unittest.mock import create_autospec

import pytest

from media_preview_generator.markers.models import Marker, MarkerType
from media_preview_generator.markers.publishers.factory import publisher_for
from media_preview_generator.servers.base import Library, ServerConfig, ServerType
from media_preview_generator.servers.emby import EmbyServer
from media_preview_generator.servers.jellyfin import JellyfinServer
from media_preview_generator.servers.plex import PlexServer

T = MarkerType


def _config(sid: str, stype: ServerType, root: str, **kwargs) -> ServerConfig:
    markers = {"enabled": True, "library_ids": None}
    if stype is ServerType.PLEX:
        markers["plex"] = {"db_write_confirmed_at": "2026-09-13T00:00:00+00:00", "on_plex_redetect": "restore"}
    return ServerConfig(
        id=sid,
        type=stype,
        name=sid.upper(),
        enabled=True,
        url=f"http://{sid}",
        auth={},
        libraries=[Library("1", "TV Shows", (root,))],
        markers=markers,
        **kwargs,
    )


@pytest.mark.parametrize(
    ("stype", "server_type", "atomic"),
    [
        (ServerType.PLEX, PlexServer, True),
        (ServerType.JELLYFIN, JellyfinServer, False),
        (ServerType.EMBY, EmbyServer, False),
    ],
)
def test_atomic_writes_is_declared(tmp_path, stype, server_type, atomic):
    cfg = _config(stype.value, stype, str(tmp_path))
    publisher = publisher_for(create_autospec(server_type, instance=True), cfg)
    assert publisher.atomic_writes is atomic


@pytest.mark.parametrize(
    ("served", "others_alongside", "shown"),
    [
        ({T.INTRO: [(1, 2)], T.CREDITS: [(5, 9)]}, False, "ours"),
        ({T.INTRO: [(1, 2)]}, False, "missing"),
        ({T.INTRO: [(1, 2)], T.CREDITS: [(4, 9)]}, False, "replaced"),
        ({T.INTRO: [(1, 2)], T.CREDITS: [(5, 9), (7, 8)]}, False, "replaced"),  # Plex: a row beside ours
        ({T.INTRO: [(1, 2)], T.CREDITS: [(5, 9), (7, 8)]}, True, "ours"),  # Jellyfin: another provider beside ours
        ({T.INTRO: [(1, 2)], T.CREDITS: [(7, 8)]}, True, "replaced"),
        ({}, True, "missing"),
    ],
)
def test_compare_shown_matrix(served, others_alongside, shown):
    from media_preview_generator.markers.publishers.base import compare_shown

    ours = [Marker(T.INTRO, 1, 2, ("a",)), Marker(T.CREDITS, 5, 9, ("a",))]
    assert compare_shown(ours, served, others_alongside=others_alongside).value == shown
