from unittest.mock import MagicMock

import pytest

from media_preview_generator.markers.store import MarkerStore
from media_preview_generator.reprise.service import RepriseService
from media_preview_generator.reprise.store import RepriseStore
from media_preview_generator.servers.registry import ServerRegistry


def test_switch_refuses_at_once_and_says_why_when_plex_s_database_is_not_shared(tmp_path):
    """A review-only install shares only Plex's preview folder: the switch must explain, not walk the catalogue."""
    (tmp_path / "plex" / "Media" / "localhost").mkdir(parents=True)
    registry = ServerRegistry.from_settings(
        [
            {
                "id": "plex-home",
                "name": "Plex",
                "enabled": True,
                "type": "plex",
                "url": "http://127.0.0.1:9",
                "auth": {},
                "output": {"plex_config_folder": str(tmp_path / "plex")},
                "libraries": [],
            }
        ]
    )
    markers = MarkerStore(str(tmp_path / "markers.db"))
    service = RepriseService(RepriseStore(markers), registry, None)
    service.publisher = MagicMock(writes_enabled=False)
    service.store = MagicMock(wraps=service.store)
    with pytest.raises(ValueError, match="Reprise can't see it"):
        service.set_writes_enabled(True)
    service.store.list_media.assert_not_called()
    service.publisher.validate_target.assert_not_called()
    assert service.publisher.writes_enabled is False
    markers.close()
