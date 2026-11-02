"""Saving legacy editor markers must not publish or queue a publication."""

from unittest.mock import Mock

from media_preview_generator.markers import pipeline, triggers
from media_preview_generator.markers.models import MarkerType
from media_preview_generator.markers.store import get_marker_store
from tests.markers.fakes import save_markers


def test_save_and_resave_never_publish_or_queue(client, servers, known, episode, monkeypatch):
    publisher = Mock()
    queue = Mock()
    monkeypatch.setattr(pipeline, "publisher_for", publisher)
    monkeypatch.setattr(triggers, "create_intro_credits_job", queue)

    for start in (60_000, 65_000):
        response = save_markers(client, episode, [{"type": "intro", "start_ms": start, "end_ms": 90_000}])
        assert response.status_code == 200
        assert response.get_json()["servers"] == []
        assert response.get_json()["queued_job_id"] is None
        assert get_marker_store().get_locked(known.id)[MarkerType.INTRO].start_ms == start

    publisher.assert_not_called()
    queue.assert_not_called()
