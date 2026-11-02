import sqlite3
import time

import pytest

from scripts.plex_db_node_fixture import build
from media_preview_generator.markers.publishers.base import PublishError
from media_preview_generator.markers.publishers.plex_db import LocalPlexDb


def test_legacy_plex_writer_refuses_before_touching_fixture(tmp_path):
    path = build(tmp_path / 'plex')
    with sqlite3.connect(path) as conn:
        before_parts = conn.execute('SELECT * FROM media_parts ORDER BY id').fetchall()
        before_markers = conn.execute('SELECT * FROM taggings ORDER BY id').fetchall()
    db = LocalPlexDb(lambda: str(path))
    with pytest.raises(PublishError, match='Legacy marker publication'):
        db.write_item(None, deadline=time.monotonic() + 3)
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT * FROM media_parts ORDER BY id').fetchall() == before_parts
        assert conn.execute('SELECT * FROM taggings ORDER BY id').fetchall() == before_markers


def test_plugin_mutation_transports_refuse_without_sending():
    from media_preview_generator.servers.emby import EmbyServer
    from media_preview_generator.servers.jellyfin import JellyfinServer

    with pytest.raises(PublishError, match="Legacy marker publication"):
        EmbyServer.put_emby_markers(None, "1", intro_start_ticks=1, intro_end_ticks=2,
                                    credits_start_ticks=None, file_size=8, replace_own=True)
    with pytest.raises(PublishError, match="Legacy marker publication"):
        EmbyServer.delete_emby_markers(None, "1")
    with pytest.raises(PublishError, match="Legacy marker publication"):
        JellyfinServer.put_bridge_markers(None, "1", [])
    with pytest.raises(PublishError, match="Legacy marker publication"):
        JellyfinServer.delete_bridge_markers(None, "1")
