"""Inspector plans and publisher deadline settings for existing markers."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from media_preview_generator.markers import inspect
from media_preview_generator.markers.decide import DecisionStatus, TypeDecision
from media_preview_generator.markers.models import Candidate, FileIdentity, Marker, MarkerType, Source
from media_preview_generator.markers.publishers import plex_db
from media_preview_generator.markers.store import MarkerStore
from media_preview_generator.servers.base import ServerType
from tests.markers.fakes import FakeRegistry, server_config
from tests.markers.test_plex_db_publisher import _make_db

T = MarkerType
DUR = 1_320_000
INTRO = Marker(T.INTRO, 11_000, 37_000, ("chapters",))

pytestmark = pytest.mark.usefixtures("plex_holds_the_database")


@pytest.fixture
def store(tmp_path):
    s = MarkerStore(str(tmp_path / "markers.db"), clock=lambda: datetime(2026, 9, 14, tzinfo=UTC))
    yield s
    s.close()


class TestTheInspectorSaysItBeforeTheSave:
    PATH = "/media/tv/Show/Season 01/Show - S01E01.mkv"

    def _file(self, store, *, locked):
        rec = store.upsert_file(
            FileIdentity(self.PATH, 100, 200), duration_ms=DUR, season_key="/media/tv/Show", is_movie=False
        )
        store.replace_evidence(
            rec.id, Source.CHAPTERS, [Candidate(T.INTRO, 11_000, 37_000, Source.CHAPTERS, origin="Opening")]
        )
        store.save_decisions(
            rec.id,
            {
                T.INTRO: TypeDecision(T.INTRO, DecisionStatus.DECIDED, INTRO, None, "chapters"),
                T.CREDITS: TypeDecision(T.CREDITS, DecisionStatus.NO_EVIDENCE, None, None, "nothing found"),
                T.RECAP: TypeDecision(T.RECAP, DecisionStatus.NO_EVIDENCE, None, None, "nothing found"),
                T.PREVIEW: TypeDecision(T.PREVIEW, DecisionStatus.DISABLED, None, None, "not detected"),
            },
            settings_fingerprint="f",
        )
        if locked:
            store.save_user_markers(rec.id, [INTRO], settings_fingerprint="f")
        return rec

    @pytest.mark.parametrize(
        ("locked", "plan", "reason"),
        [
            (
                True,
                "will_replace",
                "This server is set to keep Plex's own markers. Your locked marker replaces them anyway.",
            ),
            (False, "keeps_plex", "Keeping Plex's intro"),
        ],
        ids=["locked", "unlocked"],
    )
    def test_the_plan_lane_says_what_a_save_will_do_to_plexs_own_markers(
        self, store, monkeypatch, locked, plan, reason
    ):
        monkeypatch.setattr(inspect, "publisher_for", lambda *a, **kw: None)
        self._file(store, locked=locked)
        markers = {
            "enabled": True,
            "library_ids": None,
            "plex": {"db_write_confirmed_at": "2026-09-13T00:00:00+00:00", "on_plex_redetect": "keep_plex"},
        }
        cfg = server_config("plex", ServerType.PLEX, root="/media/tv", markers=markers)
        registry = FakeRegistry({"plex": cfg})
        registry.get("plex").get_markers.return_value = [
            {"type": "intro", "start_ms": 990, "end_ms": 29_306, "final": False}
        ]

        payload = inspect.item_payload(self.PATH, registry=registry, store=store)

        row = next(r for r in payload["servers"] if r["server_id"] == "plex")
        assert (row["plan"], row["plan_reason"]) == (plan, reason)

    @pytest.mark.parametrize(
        ("server_type", "vendor"), [(ServerType.PLEX, "Plex"), (ServerType.EMBY, "Emby")], ids=["plex", "emby"]
    )
    @pytest.mark.parametrize("locked", [True, False], ids=["locked", "unlocked"])
    @pytest.mark.parametrize("waiting_on_versions", [False, True], ids=["now", "waiting-on-versions"])
    def test_every_vendor_and_lane_says_it(self, server_type, vendor, locked, waiting_on_versions):
        """The sentence is composed in two places (the plan lane and the versions-waiting lane) and reads the vendor,
        so every cell of vendor × locked × lane gets a row. Emby also folds its credits note into the same string."""
        wanted = [replace(INTRO, locked=locked)]
        embys_own = [{"type": "intro", "start_ms": 990, "end_ms": 29_306}]

        plan, reason = inspect._plan(
            server_type=server_type,
            off_reason="",
            wanted=wanted,
            ours=(),
            current=embys_own,
            waiting_on_versions=waiting_on_versions,
            duration_ms=DUR,
            keep_own=True,
        )

        override = f"This server is set to keep {vendor}'s own markers. Your locked marker replaces them anyway."
        if waiting_on_versions:
            # The versions-waiting lane keeps its own reason and the override follows it as its own sentence.
            assert plan == "waiting"
            assert reason == (
                f"versions don't agree yet. {override}"
                if locked
                else f"versions don't agree yet; keeping {vendor}'s intro"
            )
        elif locked:
            assert (plan, reason) == ("will_replace", override)
        else:
            assert (plan, reason) == (f"keeps_{server_type.value}", f"Keeping {vendor}'s intro")


class TestPublishNowBoundsPlexsDatabaseWait:
    def test_a_job_leaves_the_database_wait_where_it_was(self, tmp_path):
        from media_preview_generator.markers.publishers.factory import publisher_for

        folder = tmp_path / "Plex Media Server"
        _make_db(folder, journal_mode="delete")
        cfg = server_config("plex-1", ServerType.PLEX, root="/")
        cfg.output = {"plex_config_folder": str(folder)}

        pub = publisher_for(MagicMock(), cfg, ui_details=False)
        shortened = publisher_for(MagicMock(), cfg, ui_details=False, db_timeout_s=3.0)

        assert pub._db_deadline() - shortened._db_deadline() == pytest.approx(plex_db.BUSY_TIMEOUT_S - 3.0, abs=0.05)
