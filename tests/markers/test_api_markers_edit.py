"""The Inspector editor API saves and unlocks legacy markers without publishing."""

from __future__ import annotations

import os

import pytest

from media_preview_generator.markers.decide import DecisionStatus
from media_preview_generator.markers.models import FileIdentity, Marker, MarkerType
from media_preview_generator.markers.store import LOCKED_BY_USER, UNLOCKED_PENDING, get_marker_store
from tests.markers.conftest import api_headers as _api_headers
from tests.markers.fakes import (
    EDITOR_DURATION_MS,
    save_markers,
)

T = MarkerType
DUR = EDITOR_DURATION_MS
_save = save_markers


@pytest.fixture
def only_plex_and_emby(servers):
    """The two servers that can't show a recap or a preview, so those types have no owner that shows them."""
    from media_preview_generator.web.settings_manager import get_settings_manager

    entries = [e for e in servers if e["id"] != "jf-1"]
    get_settings_manager().set("media_servers", entries)
    return entries


class TestSaveRefusals:
    def test_a_body_that_is_not_an_object_is_refused(self, client, servers):
        resp = client.post("/api/markers/item/markers", headers=_api_headers(), json=[1])
        assert resp.status_code == 400

    def test_a_request_with_neither_path_nor_item_is_refused(self, client, servers):
        resp = client.post("/api/markers/item/markers", headers=_api_headers(), json={"markers": []})
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "Give path, or server_id and item_id"

    def test_a_path_outside_every_server_library_is_refused(self, client, servers, tmp_path):
        resp = _save(client, str(tmp_path / "outside.mkv"), [{"type": "intro", "start_ms": 0, "end_ms": 5_000}])
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "Path is not a file inside any server library"

    def test_an_unknown_server_id_is_a_404(self, client, servers):
        resp = client.post(
            "/api/markers/item/markers",
            headers=_api_headers(),
            json={"server_id": "nope", "item_id": "1", "markers": []},
        )
        assert resp.status_code == 404

    def test_an_item_id_that_is_not_shaped_like_that_servers_ids_is_refused(self, client, servers):
        resp = client.post(
            "/api/markers/item/markers",
            headers=_api_headers(),
            json={"server_id": "plex-1", "item_id": "abc", "markers": []},
        )
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "item_id isn't an item id this server uses"

    def test_a_file_that_was_never_analysed_is_refused_with_a_reason_the_ui_can_act_on(self, client, servers, episode):
        resp = _save(client, episode, [{"type": "intro", "start_ms": 0, "end_ms": 5_000}])
        assert resp.status_code == 409
        assert resp.get_json()["reason"] == "not_analysed"

    @pytest.mark.parametrize(
        ("markers", "expected"),
        [
            ([], "markers must be a non-empty list of {type, start_ms, end_ms}"),
            ("intro", "markers must be a non-empty list of {type, start_ms, end_ms}"),
            ([{"start_ms": 0, "end_ms": 5}], "type must be one of intro, credits, recap, preview"),
            ([{"type": "chapter", "start_ms": 0, "end_ms": 5}], "type must be one of intro, credits, recap, preview"),
            (
                [{"type": "intro", "start_ms": 0, "end_ms": 5}, {"type": "intro", "start_ms": 1, "end_ms": 6}],
                "markers has intro twice",
            ),
            (
                [{"type": "intro", "start_ms": "0", "end_ms": 5}],
                "intro: start_ms must be a whole number of milliseconds",
            ),
            (
                [{"type": "intro", "start_ms": True, "end_ms": 5}],
                "intro: start_ms must be a whole number of milliseconds",
            ),
        ],
    )
    def test_a_markers_list_it_cannot_save_from_is_refused(self, client, servers, known, episode, markers, expected):
        resp = _save(client, episode, markers)
        assert resp.status_code == 400
        assert resp.get_json()["error"] == expected

    @pytest.mark.parametrize(
        ("start", "end", "expected"),
        [
            (-1, 5_000, "inside the file"),
            (DUR, DUR, "inside the file"),
            (0, DUR + 2_001, "inside the file"),
            (30_000, 30_000, "end after it starts"),
            (30_000, 29_999, "end after it starts"),
        ],
    )
    def test_the_two_bounds_a_user_marker_keeps(self, client, servers, known, episode, start, end, expected):
        """Inside the file, and ending after it starts -- those two, and no others."""
        resp = _save(client, episode, [{"type": "intro", "start_ms": start, "end_ms": end}])
        assert resp.status_code == 400
        assert expected in resp.get_json()["error"]

    def test_a_file_that_changed_on_disk_is_refused_before_anything_is_saved(self, client, servers, known, episode):
        """Its stored duration describes the old file, so the bounds would be checked against the wrong length."""
        with open(episode, "wb") as fh:
            fh.write(b"y" * 300)
        resp = _save(client, episode, [{"type": "intro", "start_ms": 0, "end_ms": 5_000}])
        assert resp.status_code == 409
        assert resp.get_json()["reason"] == "file_changed"
        assert get_marker_store().get_markers(known.id) == {}

    @pytest.mark.parametrize("mtype", ["recap", "preview"])
    def test_a_type_is_saved_even_if_no_server_can_show_it(self, client, only_plex_and_emby, known, episode, mtype):
        resp = _save(client, episode, [{"type": mtype, "start_ms": 1_000, "end_ms": 20_000}])
        assert resp.status_code == 200
        assert get_marker_store().get_markers(known.id)[T(mtype)].locked is True


class TestSaveAccepts:
    @pytest.mark.parametrize(
        ("markers", "kind"),
        [
            ([{"type": "intro", "start_ms": 0, "end_ms": 2_000}], "a 2 s intro (under the 3 s minimum)"),
            ([{"type": "intro", "start_ms": 900_000, "end_ms": 950_000}], "an intro past the first 35 %"),
            ([{"type": "credits", "start_ms": 10_000, "end_ms": 60_000}], "credits in the first minute"),
            ([{"type": "intro", "start_ms": 0, "end_ms": 400_000}], "an intro over the 300 s cap"),
        ],
    )
    def test_the_bounds_that_only_catch_a_wrong_source_do_not_apply_to_the_user(
        self, client, servers, known, episode, markers, kind
    ):
        resp = _save(client, episode, markers)
        assert resp.status_code == 200, kind
        assert resp.get_json()["markers"][markers[0]["type"]]["locked"] is True

    @pytest.mark.parametrize("end", [None, "missing"])
    def test_no_end_means_the_marker_runs_to_the_end_of_the_file(self, client, servers, known, episode, end):
        entry = {"type": "credits", "start_ms": 1_290_000}
        if end is None:
            entry["end_ms"] = None
        resp = _save(client, episode, [entry])
        assert resp.status_code == 200
        assert resp.get_json()["markers"]["credits"]["end_ms"] == DUR

    def test_an_end_a_couple_of_seconds_past_the_file_is_clamped_like_any_candidate(
        self, client, servers, known, episode
    ):
        resp = _save(client, episode, [{"type": "credits", "start_ms": 1_290_000, "end_ms": DUR + 2_000}])
        assert resp.status_code == 200
        assert resp.get_json()["markers"]["credits"]["end_ms"] == DUR


class TestSaveStores:
    def test_adjusting_is_locking(self, client, servers, known, episode):
        resp = _save(client, episode, [{"type": "intro", "start_ms": 5_000, "end_ms": 35_000}])
        assert resp.status_code == 200
        store = get_marker_store()
        assert store.get_markers(known.id)[T.INTRO].locked is True
        assert store.get_decisions(known.id)[T.INTRO].status is DecisionStatus.DECIDED
        assert resp.get_json()["servers"] == []
        assert resp.get_json()["queued_job_id"] is None


class TestUnlock:
    def _locked(self, client, episode):
        return _save(client, episode, [{"type": "intro", "start_ms": 5_000, "end_ms": 35_000}])

    def test_unlock_drops_the_lock_and_publishes_nothing(self, client, servers, known, episode):
        self._locked(client, episode)
        resp = client.delete(
            "/api/markers/item/markers", headers=_api_headers(), json={"path": episode, "types": ["intro"]}
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["unlocked"] == ["intro"]
        assert body["markers"] == {}
        assert body["decisions"]["intro"] == {"status": "no_evidence", "reason": UNLOCKED_PENDING}
        assert get_marker_store().get_locked(known.id) == {}

    def test_unlocking_a_type_that_was_not_locked_changes_nothing(self, client, servers, known, episode):
        self._locked(client, episode)
        resp = client.delete(
            "/api/markers/item/markers", headers=_api_headers(), json={"path": episode, "types": ["credits"]}
        )
        assert resp.status_code == 200
        assert resp.get_json()["unlocked"] == []
        assert set(get_marker_store().get_locked(known.id)) == {T.INTRO}

    def test_unlocking_one_type_leaves_the_others_locked(self, client, servers, known, episode):
        _save(
            client,
            episode,
            [
                {"type": "intro", "start_ms": 5_000, "end_ms": 35_000},
                {"type": "credits", "start_ms": 1_290_000, "end_ms": None},
            ],
        )
        resp = client.delete(
            "/api/markers/item/markers", headers=_api_headers(), json={"path": episode, "types": ["intro"]}
        )
        assert resp.get_json()["markers"]["credits"]["locked"] is True
        assert set(get_marker_store().get_locked(known.id)) == {T.CREDITS}

    @pytest.mark.parametrize("types", [None, [], "intro", ["chapter"], [1]])
    def test_a_types_list_it_cannot_read_is_refused(self, client, servers, known, episode, types):
        body = {"path": episode} if types is None else {"path": episode, "types": types}
        resp = client.delete("/api/markers/item/markers", headers=_api_headers(), json=body)
        assert resp.status_code == 400
        assert "types must be a non-empty list" in resp.get_json()["error"]

    def test_unlocking_a_file_that_was_never_analysed_is_refused(self, client, servers, episode):
        resp = client.delete(
            "/api/markers/item/markers", headers=_api_headers(), json={"path": episode, "types": ["intro"]}
        )
        assert resp.status_code == 409
        assert resp.get_json()["reason"] == "not_analysed"

    def test_a_path_outside_every_server_library_is_refused(self, client, servers, tmp_path):
        resp = client.delete(
            "/api/markers/item/markers",
            headers=_api_headers(),
            json={"path": str(tmp_path / "outside.mkv"), "types": ["intro"]},
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Adding a marker by hand
# ---------------------------------------------------------------------------

# The starting times the Inspector's Adjust puts down for a marker it adds (``inspector.js`` ``addRow``), in this
# route's body shape. ``end_ms: None`` is how "runs to the end of the file" is sent, which is what the credits seed
# turns the switch on for. These are a copy, not the source: ``TestAdjust`` in tests/e2e/test_inspector.py adds one
# through the page, so change both files together.
ADD_HEAD_MS = 30_000
ADD_TAIL_MS = {"credits": 60_000, "preview": 30_000}


def _seed(duration_ms: int, mtype: str, *, clamp: bool = True) -> dict:
    tail = ADD_TAIL_MS.get(mtype)
    if tail is None:
        return {"type": mtype, "start_ms": 0, "end_ms": ADD_HEAD_MS}
    start = duration_ms - tail
    return {"type": mtype, "start_ms": max(0, start) if clamp else start, "end_ms": None}


@pytest.fixture
def shorter_than_the_seed(app, media):
    """A 40 s file: shorter than the credits seed's own minute, so the clamp is all that keeps it legal."""
    path = media / "tv" / "Show" / "Short.mkv"
    path.write_bytes(b"x" * 50)
    st = os.stat(path)
    rec = get_marker_store().upsert_file(
        FileIdentity(str(path), st.st_size, st.st_mtime_ns), duration_ms=40_000, season_key=None, is_movie=False
    )
    return str(path), rec


class TestAddAMarkerByHand:
    """A type detection found nothing for goes through this same route: there is no add route, and none is needed."""

    @pytest.mark.parametrize("mtype", ["intro", "credits", "recap", "preview"])
    def test_a_type_with_no_stored_row_at_all_is_saved_and_reads_back(self, client, servers, known, episode, mtype):
        """The claim the feature rests on, proved against the database rather than read off the code."""
        store = get_marker_store()
        assert T(mtype) not in store.get_markers(known.id)
        assert T(mtype) not in store.get_decisions(known.id)

        entry = _seed(DUR, mtype)
        resp = _save(client, episode, [entry])
        assert resp.status_code == 200

        end = DUR if entry["end_ms"] is None else entry["end_ms"]
        stored = store.get_markers(known.id)[T(mtype)]
        assert (stored.start_ms, stored.end_ms, stored.locked) == (entry["start_ms"], end, True)
        decision = store.get_decisions(known.id)[T(mtype)]
        assert decision.status is DecisionStatus.DECIDED
        assert decision.reason == LOCKED_BY_USER
        answered = resp.get_json()["markers"][mtype]
        assert (answered["start_ms"], answered["end_ms"], answered["locked"]) == (entry["start_ms"], end, True)

    def test_the_request_body_is_the_same_one_an_adjusted_marker_sends(self, client, servers, known, episode):
        """No added/adjusted flag reaches the API: an added marker is a user marker like any other."""
        _save(client, episode, [_seed(DUR, "credits")])
        assert get_marker_store().get_locked(known.id) == {
            T.CREDITS: Marker(T.CREDITS, DUR - 60_000, DUR, ("user",), locked=True)
        }

    def test_adding_one_type_leaves_a_type_already_decided_alone(self, client, servers, known, episode):
        store = get_marker_store()
        store.save_user_markers(known.id, [Marker(T.INTRO, 11_000, 37_000, ("chapters",))], settings_fingerprint="fp")

        resp = _save(client, episode, [_seed(DUR, "credits")])
        assert resp.status_code == 200
        markers = store.get_markers(known.id)
        assert (markers[T.INTRO].start_ms, markers[T.INTRO].end_ms) == (11_000, 37_000)
        # Every stored marker comes back, not only the one just sent, so the Inspector keeps the untouched type.
        assert set(resp.get_json()["markers"]) == {"intro", "credits"}

    @pytest.mark.parametrize("mtype", ["credits", "preview"])
    def test_the_clamped_tail_seed_is_legal_on_a_file_shorter_than_the_seed(
        self, client, servers, shorter_than_the_seed, mtype
    ):
        path, rec = shorter_than_the_seed
        entry = _seed(40_000, mtype)
        assert entry["start_ms"] == max(0, 40_000 - ADD_TAIL_MS[mtype])

        resp = _save(client, path, [entry])
        assert resp.status_code == 200
        stored = get_marker_store().get_markers(rec.id)[T(mtype)]
        assert (stored.start_ms, stored.end_ms) == (entry["start_ms"], 40_000)

    def test_without_the_clamp_the_same_seed_would_be_refused(self, client, servers, shorter_than_the_seed):
        """Why ``max(0, ...)`` is in addSeed: refusalFor() checks ``start >= duration``, never ``start < 0``."""
        path, rec = shorter_than_the_seed
        unclamped = _seed(40_000, "credits", clamp=False)
        assert unclamped["start_ms"] == -20_000

        resp = _save(client, path, [unclamped])
        assert resp.status_code == 400
        assert "inside the file" in resp.get_json()["error"]
        assert get_marker_store().get_markers(rec.id) == {}
