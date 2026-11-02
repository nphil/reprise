"""Legacy publisher entry points must refuse all marker writes."""

from unittest.mock import Mock

import pytest

from media_preview_generator.markers.models import Marker, MarkerType
from media_preview_generator.markers.publishers.base import PublishError
from media_preview_generator.markers.publishers.emby import EmbyMarkerPublisher
from media_preview_generator.markers.publishers.jellyfin import JellyfinMarkerPublisher
from media_preview_generator.markers.publishers.plex_db import PlexMarkerPublisher


@pytest.mark.parametrize("publisher", [PlexMarkerPublisher, JellyfinMarkerPublisher, EmbyMarkerPublisher])
@pytest.mark.parametrize("markers", [[], [Marker(MarkerType.INTRO, 5_000, 35_000, ("user",))]])
def test_legacy_write_is_refused_before_touching_server(publisher, markers):
    target = Mock()
    with pytest.raises(PublishError):
        publisher.write(target, "item-1", markers, previous=None, duration_ms=60_000, canonical_path="/media/item.mkv")
    assert target.mock_calls == []
