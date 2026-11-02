"""Emby marker capability, projection, read-back and legacy publication guard."""

from __future__ import annotations

from unittest.mock import create_autospec

import pytest

from media_preview_generator.markers.models import Marker, MarkerType
from media_preview_generator.markers.publishers.base import Capability, PublishError, Shown
from media_preview_generator.markers.publishers.emby import CREDITS_BEFORE_END_NOTE, EmbyMarkerPublisher
from media_preview_generator.markers.settings import load_server
from media_preview_generator.servers import EmbyServer
from media_preview_generator.servers.base import ServerType
from tests.markers.fakes import server_config

T = MarkerType
DUR = 1_321_472
INTRO = Marker(T.INTRO, 126_771, 157_068, ("chapters",))
CREDITS_TO_END = Marker(T.CREDITS, 1_295_324, DUR, ("chapters",))
CREDITS_EARLY = Marker(T.CREDITS, 1_250_000, 1_290_000, ("theintrodb", "server_markers"))
# Rows Emby's own intro detection (or another plugin) leaves: the plugin never wrote them.
EMBY_INTRO = [("IntroStart", 600_000_000, "Intro"), ("IntroEnd", 900_000_000, "Intro End")]


def _ready_server():
    server = create_autospec(EmbyServer, instance=True)
    server.get_bridge_info.return_value = {"installed": True, "version": "1.0.0.0", "features": ["markers"]}
    server.get_bridge_markers_access.return_value = "ok"
    server.bridge_catalog_listed.return_value = False
    return server


class FakeEmby:
    def __init__(self):
        self.server = _ready_server()
        self.rows = [("Chapter", 0, "Chapter 1")]
        self.server.get_chapter_markers.side_effect = lambda item_id, **kwargs: [
            {"marker_type": t, "start_ms": ticks // 10_000, "name": name} for t, ticks, name in self.rows
        ]
        self.server.get_emby_marker_state.return_value = {
            "intro_start_ticks": None,
            "intro_end_ticks": None,
            "credits_start_ticks": None,
            "file_size": None,
            "stale": False,
        }


def _publisher(fake, setting="restore", **kwargs):
    cfg = server_config("emby-1", ServerType.EMBY, root="/")
    cfg.markers["emby"] = {"on_emby_redetect": setting}
    return EmbyMarkerPublisher(fake.server, cfg, load_server(cfg.markers, "emby"), **kwargs)


@pytest.fixture
def emby():
    fake = FakeEmby()
    fake.publisher = _publisher(fake)
    return fake


class TestCapability:
    @pytest.mark.parametrize(
        ("setup", "state"),
        [
            (lambda s: setattr(s.get_bridge_info, "return_value", None), Capability.UNREACHABLE),
            (lambda s: setattr(s.get_bridge_info, "return_value", {"installed": False, "version": None, "features": []}), Capability.NEEDS_PLUGIN),
            (lambda s: setattr(s.get_bridge_info, "return_value", {"installed": True, "version": "0.9", "features": []}), Capability.PLUGIN_OUTDATED),
            (lambda s: setattr(s.get_bridge_markers_access, "return_value", "unauthorized"), Capability.MISCONFIGURED),
            (lambda s: setattr(s.get_bridge_markers_access, "return_value", "forbidden"), Capability.MISCONFIGURED),
            (lambda s: setattr(s.get_bridge_markers_access, "return_value", None), Capability.UNREACHABLE),
            (lambda s: None, Capability.READY),
        ],
    )  # fmt: skip
    def test_matrix(self, emby, setup, state):
        setup(emby.server)
        report = emby.publisher.capability()
        assert report.state is state
        if state is Capability.READY:
            assert report.details == {"plugin_version": "1.0.0.0", "can_show": ["intro", "credits"]}
        if state is Capability.NEEDS_PLUGIN:
            assert report.message == "Install the Media Preview Bridge for Emby plugin"
            assert report.details == {"catalog_listed": False}
        if state is Capability.PLUGIN_OUTDATED:
            assert report.details == {"plugin_version": "0.9"}

    def test_switched_off_is_disabled_without_asking_emby(self):
        server = create_autospec(EmbyServer, instance=True)
        cfg = server_config("emby-1", ServerType.EMBY, markers={"enabled": False, "library_ids": None})
        report = EmbyMarkerPublisher(server, cfg, load_server(cfg.markers, "emby")).capability()
        assert report.state is Capability.DISABLED and server.get_bridge_info.call_count == 0


class TestCreditsNote:
    @pytest.mark.parametrize(
        ("markers", "duration", "note"),
        [
            ([INTRO, CREDITS_TO_END], DUR, ""),
            ([Marker(T.CREDITS, 1_295_000, DUR - 2_000, ())], DUR, ""),
            ([Marker(T.CREDITS, 1_295_000, DUR - 2_001, ())], DUR, CREDITS_BEFORE_END_NOTE),
            ([INTRO, CREDITS_EARLY], DUR, CREDITS_BEFORE_END_NOTE),
            ([CREDITS_EARLY], None, ""),  # unknown duration: can't tell
            ([INTRO], DUR, ""),
        ],
    )  # fmt: skip
    def test_note_names_credits_that_end_before_the_file(self, emby, markers, duration, note):
        assert emby.publisher.projection_note(markers, duration_ms=duration) == note

    def test_every_decided_credits_marker_is_sent(self, emby):
        # Emby always gets the credits start, even when a scene follows the credits.
        assert emby.publisher.project([CREDITS_EARLY, INTRO, Marker(T.RECAP, 0, 30_000, ())]) == [INTRO, CREDITS_EARLY]


@pytest.mark.parametrize("markers, previous", [([INTRO], []), ([], [INTRO]), ([], None), ([], [])])
def test_legacy_write_is_rejected_before_server_access(emby, markers, previous):
    with pytest.raises(PublishError):
        emby.publisher.write("42", markers, previous=previous, duration_ms=DUR, canonical_path="/media.mkv")
    assert emby.server.mock_calls == []


class TestShows:
    def test_ours_missing_and_replaced(self, emby):
        emby.rows += [
            ("IntroStart", INTRO.start_ms * 10_000, "Intro"),
            ("IntroEnd", INTRO.end_ms * 10_000, "Intro End"),
            ("CreditsStart", CREDITS_TO_END.start_ms * 10_000, "Credits"),
        ]
        assert emby.publisher.shows("42", [INTRO, CREDITS_TO_END]) is Shown.OURS
        emby.rows = [("Chapter", 0, "Chapter 1"), *EMBY_INTRO, ("CreditsStart", 12_953_240_000, "Credits")]
        assert emby.publisher.shows("42", [INTRO, CREDITS_TO_END]) is Shown.REPLACED
        emby.rows = [("Chapter", 0, "Chapter 1")]
        assert emby.publisher.shows("42", [INTRO]) is Shown.MISSING

    def test_credits_are_compared_by_their_start(self, emby):
        emby.rows.append(("CreditsStart", CREDITS_EARLY.start_ms * 10_000, "Credits"))
        assert emby.publisher.shows("42", [CREDITS_EARLY]) is Shown.OURS
        assert emby.publisher.shows("42", [Marker(T.CREDITS, 1_250_001, DUR, ())]) is Shown.REPLACED

    def test_a_second_intro_beside_ours_is_not_ours(self, emby):
        emby.rows += [
            ("IntroStart", INTRO.start_ms * 10_000, "Intro"),
            ("IntroEnd", INTRO.end_ms * 10_000, "Intro End"),
        ]
        emby.rows = sorted([*emby.rows, *EMBY_INTRO], key=lambda r: r[1])
        assert emby.publisher.shows("42", [INTRO]) is Shown.MISSING

    def test_kept_types_whose_plugin_store_cant_be_read_are_unknown(self, emby):
        emby.rows += EMBY_INTRO
        publisher = _publisher(emby, "keep_emby")
        emby.server.get_emby_marker_state.side_effect = lambda item_id: None
        assert publisher.shows("42", [], kept_types=frozenset({T.INTRO})) is None

    def test_unreadable_chapters_are_unknown(self, emby):
        emby.server.get_chapter_markers.side_effect = None
        emby.server.get_chapter_markers.return_value = None
        assert emby.publisher.shows("42", [INTRO]) is None
