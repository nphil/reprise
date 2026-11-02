"""JellyfinMarkerPublisher against an autospec'd JellyfinServer (Bridge plugin HTTP contract)."""

from __future__ import annotations

from unittest.mock import create_autospec

import pytest

from media_preview_generator.markers.models import Marker, MarkerType
from media_preview_generator.markers.publishers.base import Capability, PublishError
from media_preview_generator.markers.publishers.jellyfin import (
    JellyfinMarkerPublisher,
    bridge_key,
    core_key,
    segment_times,
)
from media_preview_generator.markers.settings import ServerMarkersSettings
from media_preview_generator.servers.base import ServerConfig, ServerType
from media_preview_generator.servers.jellyfin import JellyfinServer

T = MarkerType
CFG = ServerConfig(id="jf-1", type=ServerType.JELLYFIN, name="JF", enabled=True, url="http://j", auth={})
MISSING = "/definitely/not/here.mkv"
READY_INFO = {"installed": True, "version": "10.11.1.0", "features": ["trickplay", "markers"]}
INTRO = {"type": "Intro", "startTicks": 1_267_710_000, "endTicks": 1_570_680_000}
INTRO_MARKER = Marker(T.INTRO, 126_771, 157_068, ("a",))


def _server():
    server = create_autospec(JellyfinServer, instance=True)
    server.get_bridge_info.return_value = READY_INFO
    server.get_bridge_markers_access.return_value = "ok"
    server.get_media_segments.return_value = []
    server.get_bridge_marker_state.return_value = {"segments": [], "fileSize": None, "stale": False}
    return server


def _pub(server, enabled=True):
    return JellyfinMarkerPublisher(server, CFG, ServerMarkersSettings(enabled, None, None, "restore"))


def _core(*segments, other_provider=()):
    """Core /MediaSegments rows for bridge-shaped segments (plus rows another provider serves)."""
    rows = [
        {"Id": f"id{i}", "ItemId": "abc", "Type": s["type"], "StartTicks": s["startTicks"], "EndTicks": s["endTicks"]}
        for i, s in enumerate([*segments, *other_provider])
    ]
    return rows


@pytest.mark.parametrize(
    ("enabled", "info", "access", "state"),
    [
        pytest.param(False, READY_INFO, "ok", Capability.DISABLED, id="disabled"),
        pytest.param(True, None, "ok", Capability.UNREACHABLE, id="unreachable"),
        pytest.param(
            True, {"installed": False, "version": None, "features": []}, "ok", Capability.NEEDS_PLUGIN, id="no-plugin"
        ),
        pytest.param(
            True,
            {"installed": True, "version": "10.11.0.3", "features": []},
            "ok",
            Capability.PLUGIN_OUTDATED,
            id="no-markers-feature",
        ),
        pytest.param(True, READY_INFO, "ok", Capability.READY, id="ready-10.11"),
        pytest.param(True, {**READY_INFO, "version": "12.0.1.0"}, "ok", Capability.READY, id="ready-12.0"),
        pytest.param(True, READY_INFO, "forbidden", Capability.MISCONFIGURED, id="not-admin"),
        pytest.param(True, READY_INFO, "unauthorized", Capability.MISCONFIGURED, id="credentials-rejected"),
        pytest.param(True, READY_INFO, None, Capability.UNREACHABLE, id="access-unknown"),
        pytest.param(True, READY_INFO, "teapot", Capability.UNREACHABLE, id="access-unexpected-value"),
    ],
)
def test_capability_matrix(enabled, info, access, state):
    server = _server()
    server.get_bridge_info.return_value = info
    server.get_bridge_markers_access.return_value = access
    report = _pub(server, enabled).capability()
    assert report.state is state
    if state is Capability.READY:
        assert report.details == {
            "plugin_version": info["version"],
            "can_show": ["intro", "credits", "recap", "preview"],
        }
    if state is Capability.DISABLED:
        # Turned off: the server isn't contacted at all.
        server.get_bridge_info.assert_not_called()
    if state in (Capability.UNREACHABLE, Capability.NEEDS_PLUGIN, Capability.PLUGIN_OUTDATED) and access == "ok":
        server.get_bridge_markers_access.assert_not_called()


def test_capability_outdated_messages():
    server = _server()
    server.get_bridge_info.return_value = {"installed": True, "version": "10.11.0.3", "features": []}
    report = _pub(server).capability()
    assert report.details == {"plugin_version": "10.11.0.3"}
    assert "10.11.0.3" in report.message

    server.get_bridge_info.return_value = {"installed": True, "version": None, "features": ["trickplay"]}
    assert "unknown" in _pub(server).capability().message


@pytest.mark.parametrize(
    ("access", "message_part", "not_in_message"),
    [
        pytest.param("forbidden", "administrator rights", "reconnect", id="403"),
        pytest.param("unauthorized", "rejected this server's credentials; reconnect it", "administrator", id="401"),
    ],
)
def test_capability_access_messages(access, message_part, not_in_message):
    server = _server()
    server.get_bridge_markers_access.return_value = access
    report = _pub(server).capability()
    assert message_part in report.message
    assert not_in_message not in report.message


def test_publisher_identity():
    assert JellyfinMarkerPublisher.name == "jellyfin_bridge"
    assert JellyfinMarkerPublisher.supported_types == frozenset(MarkerType)
    # The plugin may have stored markers before an error, so a failed write leaves the server state unknown.
    assert JellyfinMarkerPublisher.atomic_writes is False


@pytest.mark.parametrize("markers, previous", [([INTRO_MARKER], []), ([], [INTRO_MARKER]), ([], None), ([], [])])
def test_legacy_write_is_rejected_before_server_access(markers, previous):
    server = _server()
    with pytest.raises(PublishError):
        _pub(server).write("abc", markers, previous=previous, duration_ms=None, canonical_path=MISSING)
    assert server.mock_calls == []


def test_segment_keys():
    assert bridge_key(INTRO) == ("Intro", 1_267_710_000, 1_570_680_000)
    assert core_key(_core(INTRO)[0]) == ("Intro", 1_267_710_000, 1_570_680_000)
    assert bridge_key({}) == (None, None, None) == core_key({})


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        pytest.param(("Intro", 1_267_710_000, 1_570_680_000), (T.INTRO, 126_771, 157_068), id="intro"),
        pytest.param(("Outro", 10_000, 29_999), (T.CREDITS, 1, 2), id="outro-floors"),
        pytest.param(("Recap", 0, 10_000), (T.RECAP, 0, 1), id="recap"),
        pytest.param(("Preview", 20_000, 30_000), (T.PREVIEW, 2, 3), id="preview"),
        pytest.param(("Commercial", 1, 2), None, id="commercial"),
        pytest.param(("intro", 1, 2), None, id="wrong-case"),
        pytest.param((None, 1, 2), None, id="no-type"),
        pytest.param(("Intro", "1", 2), None, id="string-start"),
        pytest.param(("Intro", 1, 2.0), None, id="float-end"),
        pytest.param(("Intro", True, 2), None, id="bool-start"),
        pytest.param(("Intro", 1, None), None, id="missing-end"),
        pytest.param((["Intro"], 1, 2), None, id="unhashable-type"),
    ],
)
def test_segment_times(args, expected):
    assert segment_times(*args) == expected
