"""Webhook sender classification behavior."""

from __future__ import annotations

import json

from flask import Flask, request

from media_preview_generator.web.webhook_router import _classify_payload

# What each sender posts when set up as docs/guides.md and docs/multi-server.md describe, trimmed to
# the fields the router looks at. Plex posts multipart form data; the rest post JSON.
SENDER_BODIES: dict[str, dict] = {
    "Sonarr": {"eventType": "Download", "series": {"path": "/tv/Show"}, "episodeFile": {"path": "/tv/Show/a.mkv"}},
    "Radarr": {"eventType": "Download", "movie": {"folderPath": "/films/F"}, "movieFile": {"path": "/films/F/f.mkv"}},
    "Plex": {"event": "library.new", "Server": {"uuid": "plex-id"}, "Metadata": {"ratingKey": "1"}},
    "Emby": {"Event": "library.new", "Server": {"Id": "emby-id"}, "Item": {"Id": "1"}},
    "Jellyfin": {"NotificationType": "ItemAdded", "ServerId": "jellyfin-id", "ItemId": "1"},
    "Tdarr": {"file_path": "/films/F/f.mkv"},
    "FileFlows": {"file_path": "/films/F/f.mkv"},
}


def _kind_at_the_universal_url(sender: str) -> str:
    """How POST /api/webhooks/incoming classifies this sender's body ("unknown" if it can't)."""
    body = SENDER_BODIES[sender]
    if sender == "Plex":
        context = {"method": "POST", "data": {"payload": json.dumps(body)}, "content_type": "multipart/form-data"}
    else:
        context = {"method": "POST", "json": body}
    with Flask(__name__).test_request_context("/api/webhooks/incoming", **context):
        return _classify_payload(request)[0]


def test_the_universal_url_recognises_each_sender_it_should() -> None:
    # Pins the table below to the router, so a sender dropping out of /incoming fails here first.
    kinds = {sender: _kind_at_the_universal_url(sender) for sender in SENDER_BODIES}
    assert kinds == {
        "Sonarr": "sonarr",
        "Radarr": "radarr",
        "Plex": "plex",
        "Emby": "emby",
        "Jellyfin": "jellyfin",
        "Tdarr": "unknown",
        "FileFlows": "unknown",
    }

