"""Plex's own markers are suggestions: never eligible to skip until the owner approves that exact range."""

import pytest

from media_preview_generator.markers.store import MarkerStore
from media_preview_generator.reprise.identity import media_version
from media_preview_generator.reprise.models import DecisionRange, Evidence, PlaybackIdentity, ReviewRefused
from media_preview_generator.reprise.policy import eligible_ranges
from media_preview_generator.reprise.review import plex_projection
from media_preview_generator.reprise.store import NEEDS_ACKNOWLEDGMENT, RepriseStore
from media_preview_generator.reprise.verification_profile import PROFILE_FINGERPRINT, PROFILE_ID
from tests.reprise.test_api_http import registered_episode

METRICS = {"verification_profile": PROFILE_ID, "profile_fingerprint": PROFILE_FINGERPRINT}
ACKNOWLEDGED = {"acknowledge_plex_suggestion": True}


def suggestion(name, kind, start, end, audio=("a",)):
    reason = "Suggested by Plex — not confirmed by Reprise."
    return DecisionRange(
        name,
        kind,
        "skip",
        start,
        end,
        "proposed",
        False,
        "plex",
        "plex_suggestion",
        reason,
        (name + "-evidence",),
        audio,
    )


def unconfirmed(name, start, end):
    return Evidence(
        name, start, end, "audiovisual", "unknown", "plex_marker_unconfirmed", "", None, None, None, METRICS
    )


@pytest.fixture
def episode(tmp_path):
    markers = MarkerStore(str(tmp_path / "markers.db"))
    store = RepriseStore(markers)
    path = tmp_path / "episode.mkv"
    path.write_bytes(b"episode")
    version = media_version(str(path), 10000, "timing")
    identity = PlaybackIdentity("machine", "1", "2", "3", "/part/3", version.size_bytes, 10000, "a")
    store.register_media(version, identity, server_id="local", metadata={"show_key": "show", "season": 1, "episode": 1})

    def analyze(ranges, evidence):
        run = store.begin_analysis(version.id, "job", "signature")
        return store.finish_analysis(run, evidence, ranges, version.id, ["Plex suggested the intro."])

    yield store, version, identity, analyze
    markers.close()


def intro_only(analyze):
    return analyze([suggestion("intro", "intro", 1000, 4000)], [unconfirmed("intro-evidence", 1000, 4000)])


def test_a_plex_suggestion_is_not_eligible_until_that_range_is_approved_deliberately(episode):
    store, version, _, analyze = episode
    result = intro_only(analyze)
    assert eligible_ranges(result, "a") == ()
    approve = [{"range_id": "intro", "review_status": "approved"}]
    with pytest.raises(ReviewRefused, match="deliberate approval"):
        store.save_review(version.id, result.result_revision, approve, "owner")
    assert eligible_ranges(store.get_result(version.id), "a") == ()  # a refused review saves nothing
    approved = store.save_review(version.id, result.result_revision, [{**approve[0], **ACKNOWLEDGED}], "owner")
    assert [(r.kind, r.start_ms, r.end_ms, r.origin) for r in eligible_ranges(approved, "a")] == [
        ("intro", 1000, 4000, "plex")
    ]
    assert eligible_ranges(approved, "another-audio-track") == ()


def test_every_plex_range_needs_its_own_acknowledgment_and_a_change_needs_it_again(episode):
    store, version, _, analyze = episode
    result = analyze(
        [suggestion("intro", "intro", 1000, 4000), suggestion("credits", "credits", 6000, 9000)],
        [unconfirmed("intro-evidence", 1000, 4000), unconfirmed("credits-evidence", 6000, 9000)],
    )
    both = [
        {"range_id": "intro", "review_status": "approved", **ACKNOWLEDGED},
        {"range_id": "credits", "review_status": "approved"},
    ]
    with pytest.raises(ReviewRefused):  # one acknowledgment does not cover the other range; nothing is saved
        store.save_review(version.id, result.result_revision, both, "owner")
    assert eligible_ranges(store.get_result(version.id), "a") == ()
    both[1].update(ACKNOWLEDGED)
    approved = store.save_review(version.id, result.result_revision, both, "owner")
    assert len(eligible_ranges(approved, "a")) == 2
    # approved and unchanged: a client that sends the range back as it is needs no new acknowledgment
    unchanged = [
        {"range_id": "intro", "review_status": "approved"},
        {"range_id": "credits", "review_status": "approved"},
    ]
    kept = store.save_review(version.id, approved.result_revision, unchanged, "owner")
    assert len(eligible_ranges(kept, "a")) == 2
    # moving an approved range is a new decision about different content
    unchanged[0]["end_ms"] = 5000
    with pytest.raises(ReviewRefused):
        store.save_review(version.id, kept.result_revision, unchanged, "owner")
    assert [r.end_ms for r in eligible_ranges(store.get_result(version.id), "a")] == [4000, 9000]


def test_keeps_and_other_unconfirmed_content_still_veto_an_approved_plex_suggestion(episode):
    store, version, _, analyze = episode
    result = analyze(
        [suggestion("intro", "intro", 1000, 6000)],
        [unconfirmed("intro-evidence", 1000, 6000), unconfirmed("elsewhere", 3000, 3500)],
    )
    edits = [
        {"range_id": "intro", "review_status": "approved", **ACKNOWLEDGED},
        {"range_id": None, "action": "keep", "kind": "intro", "start_ms": 2000, "end_ms": 2500},
    ]
    approved = store.save_review(version.id, result.result_revision, edits, "owner")
    assert [(r.start_ms, r.end_ms) for r in eligible_ranges(approved, "a")] == [
        (1000, 2000),
        (2500, 3000),
        (3500, 6000),
    ]


def test_plex_is_never_given_its_own_suggestions_back_to_publish(episode):
    store, version, identity, analyze = episode
    result = intro_only(analyze)
    approved = store.save_review(
        version.id,
        result.result_revision,
        [{"range_id": "intro", "review_status": "approved", **ACKNOWLEDGED}],
        "owner",
    )
    with pytest.raises(ValueError, match="already in Plex"):
        plex_projection(approved, identity, eligible_ranges(approved, "a"))


def test_the_review_api_asks_for_a_deliberate_approval_then_serves_the_eligible_range(tmp_path, monkeypatch):
    client, version, _, markers = registered_episode(tmp_path, monkeypatch)
    store = RepriseStore(markers)
    run = store.begin_analysis(version.id, "job", "signature")
    result = store.finish_analysis(
        run, [unconfirmed("intro-evidence", 1000, 4000)], [suggestion("intro", "intro", 1000, 4000, ("4",))], version.id
    )
    url = "/api/reprise/media/" + version.id
    body = {"expected_revision": result.result_revision, "edits": [{"range_id": "intro", "review_status": "approved"}]}
    refused = client.patch(url + "/review", json=body)
    assert (refused.status_code, refused.get_json()) == (400, {"error": NEEDS_ACKNOWLEDGMENT})
    assert not client.get(url + "/result").get_json()["eligible_skip_ranges"]
    body["edits"][0].update(ACKNOWLEDGED)
    assert client.patch(url + "/review", json=body).status_code == 200
    eligible = client.get(url + "/result").get_json()["eligible_skip_ranges"]
    assert [(r["start_ms"], r["end_ms"], r["origin"]) for r in eligible] == [(1000, 4000, "plex")]
    markers.close()
