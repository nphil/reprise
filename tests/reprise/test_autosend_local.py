"""Sending the local vision model's High-confidence skips to Plex by itself (``LocalModelSender``), the history of what
Reprise sent by itself (``approvals.recent_automatic``) and its undo. Real store, publisher and Plex database file (the
``Lab`` of the automatic sender); only the clock, the alert post and the daily backup are stand-ins."""

import time
from types import SimpleNamespace

import pytest
from flask import Flask

from media_preview_generator.reprise import approvals, autosend, localvision, plexbackup
from media_preview_generator.reprise.autosend import LocalModelSender
from media_preview_generator.reprise.models import PublicationRefused
from media_preview_generator.reprise.publishing import AUTO_ACTOR
from media_preview_generator.reprise.store import RevisionConflict
from media_preview_generator.web.routes.api_reprise import create_reprise_blueprint
from tests.reprise.test_approvals import SURE_CREDITS, SURE_INTRO, analyse, owner_spans
from tests.reprise.test_autosend import (
    DURATION,
    T0,
    Flaky,
    Lab,
    _marker_times,
    _seed_marker,
    confirmed,
    detector_keep,
    sure_intro,
)

CREDITS_SPAN = {
    "range_id": None,
    "kind": "credits",
    "start_ms": 80_000,
    "end_ms": DURATION,
    "review_status": "approved",
    "acknowledge_owner_span": True,
}
FAILED_TITLE = "An automatic send didn't work"


@pytest.fixture
def lab(tmp_path, monkeypatch):
    app = Lab(tmp_path, monkeypatch)
    app.service.job_manager = SimpleNamespace(config_dir=str(tmp_path))
    monkeypatch.setattr(plexbackup, "ensure_daily_backup", app.backups)
    app.kinds = ["credits"]
    app.local = LocalModelSender(
        app.service,
        app.concerns,
        app.state,
        config_dir=str(tmp_path),
        kinds=lambda: tuple(app.kinds),
        clock=app.clock,
        backup=app.backups,
    )
    yield app
    if not app.marker._closed:
        app.marker.close()


def local_attempts(lab, ep):
    return lab.query(
        "SELECT revision, outcome, detail, tries FROM reprise_autopilot_local_attempts WHERE version_id=? "
        "ORDER BY revision",
        ep.vid,
    )


def last_detail(lab, ep):
    return local_attempts(lab, ep)[-1]["detail"]


def credits_episode(lab, number=1, *others, confidence=localvision.HIGH):
    ep = lab.episode(number)
    analyse(lab, ep, ("credits", 80_000, DURATION, confidence), others=[o(ep) for o in others])
    return ep


def assert_untouched(lab, ep, before):
    """Nothing was sent, no skip of the owner's was made and the suggestion still waits."""
    assert lab.rows() == before
    assert lab.publisher.list_changes(ep.vid) == []
    assert owner_spans(lab, ep) == []
    assert [r.review_status for r in lab.store.get_result(ep.vid).ranges if r.kind == "credits"] == ["proposed"]


# ── what it sends ─────────────────────────────────────────────────────────────────────────────────────────────────


def test_a_high_confidence_ending_is_sent_as_the_owners_exact_skip_by_reprise(lab):
    ep = credits_episode(lab)

    assert lab.local.sweep(T0) == 1

    assert _marker_times(lab.plex, "credits") == [(78_000, DURATION)]
    [change] = lab.publisher.list_changes(ep.vid)
    assert (change["state"], change["actor"]) == ("applied", AUTO_ACTOR)
    [span] = owner_spans(lab, ep)
    assert (span.kind, span.start_ms, span.end_ms) == ("credits", 80_000, DURATION)
    assert [a["outcome"] for a in local_attempts(lab, ep)] == ["sent", "sent"]  # the revision it saw and the one after
    assert lab.post.calls == []


def test_nothing_is_sent_while_the_switch_is_off(lab):
    ep = credits_episode(lab)
    before = lab.rows()
    lab.kinds.clear()

    assert lab.local.sweep(T0) == 0

    assert_untouched(lab, ep, before)
    assert local_attempts(lab, ep) == []
    assert lab.backups.calls == []


def test_sending_to_plex_being_off_stops_it_too(lab):
    ep = credits_episode(lab)
    before = lab.rows()
    lab.publisher.writes_enabled = False

    assert lab.local.sweep(T0) == 0

    assert_untouched(lab, ep, before)
    assert lab.local.blocked(T0).startswith("Sending to Plex is switched off")


def test_only_high_is_sent_and_a_medium_suggestion_is_left_alone(lab):
    ep = credits_episode(lab, confidence=localvision.MEDIUM)
    before = lab.rows()

    assert lab.local.sweep(T0) == 0

    assert_untouched(lab, ep, before)
    assert last_detail(lab, ep) == "The AI model isn't sure about any skip for it."


def test_a_film_is_never_sent(lab):
    ep = lab.episode(1, kind="movie")
    analyse(lab, ep, SURE_CREDITS)
    before = lab.rows()

    assert lab.local.sweep(T0) == 0

    assert_untouched(lab, ep, before)
    assert last_detail(lab, ep) == "Films are never sent to Plex automatically."


def test_an_episode_the_owner_already_set_a_skip_for_is_left_alone(lab):
    ep = credits_episode(lab)
    lab.owner_review(ep, [CREDITS_SPAN])
    before = lab.rows()

    assert lab.local.sweep(T0) == 0

    assert lab.rows() == before and lab.publisher.list_changes(ep.vid) == []
    assert [(s.start_ms, s.end_ms) for s in owner_spans(lab, ep)] == [(80_000, DURATION)]  # theirs, untouched
    assert local_attempts(lab, ep)[-1]["outcome"] == "declined"


def test_a_skip_the_owner_took_back_is_never_set_again(lab):
    ep = credits_episode(lab)
    result = lab.owner_review(ep, [CREDITS_SPAN])
    [span] = [r for r in result.ranges if r.origin == "owner"]
    lab.owner_review(ep, [{"range_id": span.id, "review_status": "rejected"}])
    before = lab.rows()

    assert lab.local.sweep(T0) == 0

    assert lab.rows() == before and lab.publisher.list_changes(ep.vid) == []
    assert owner_spans(lab, ep) == []
    assert local_attempts(lab, ep)[-1]["outcome"] == "declined"


def test_a_suggestion_the_owner_turned_down_is_never_sent(lab):
    ep = credits_episode(lab)
    [suggested] = [r for r in lab.store.get_result(ep.vid).ranges if r.kind == "credits"]
    lab.owner_review(ep, [{"range_id": suggested.id, "review_status": "rejected"}])
    before = lab.rows()

    assert lab.local.sweep(T0) == 0

    assert lab.rows() == before and lab.publisher.list_changes(ep.vid) == []
    assert local_attempts(lab, ep)[-1]["outcome"] == "declined"


def test_a_scene_to_keep_inside_the_ending_stops_it(lab):
    ep = credits_episode(lab, 1, lambda e: detector_keep(e, "credits", 90_000, 95_000))
    before = lab.rows()

    assert lab.local.sweep(T0) == 0

    assert lab.rows() == before and lab.publisher.list_changes(ep.vid) == []
    assert owner_spans(lab, ep) == []
    assert "kept, locked or decided" in last_detail(lab, ep)


def test_a_locked_range_inside_the_ending_stops_it(lab):
    ep = credits_episode(lab, 1, lambda e: confirmed(e, "credits", 85_000, 90_000))
    lab.owner_review(ep, [{"range_id": "credits-85000", "locked": True}])
    before = lab.rows()

    assert lab.local.sweep(T0) == 0

    assert lab.rows() == before and lab.publisher.list_changes(ep.vid) == []
    assert owner_spans(lab, ep) == []
    assert "kept, locked or decided" in last_detail(lab, ep)


def test_an_approved_skip_of_the_owner_elsewhere_in_the_episode_stops_it(lab):
    ep = lab.episode(1)
    analyse(lab, ep, SURE_CREDITS, SURE_INTRO)
    [intro] = [r for r in lab.store.get_result(ep.vid).ranges if r.kind == "intro"]
    lab.owner_review(
        ep, [{"range_id": intro.id, "review_status": "approved", "acknowledge_suggestion": True}]
    )  # a send would carry it along
    before = lab.rows()

    assert lab.local.sweep(T0) == 0

    assert lab.rows() == before and lab.publisher.list_changes(ep.vid) == []
    assert last_detail(lab, ep) == "You already approved something in it, so sending it is up to you."


def test_one_pass_sends_at_most_three_episodes_and_the_next_pass_the_rest(lab):
    episodes = [credits_episode(lab, n) for n in range(1, 5)]

    assert lab.local.sweep(T0) == autosend.SENDS_PER_PASS == 3
    assert lab.local.sweep(T0 + 1) == 1

    assert all(_marker_times(lab.plex, "credits", item=ep.number) == [(78_000, DURATION)] for ep in episodes)


def test_an_ending_the_sure_sender_already_sent_is_not_sent_a_second_time(lab):
    ep = sure_intro(lab)
    analyse(lab, ep, SURE_CREDITS, others=[confirmed(ep, "intro", 10_000, 20_000)])
    assert lab.sweep(T0) == 1  # the sender of sure matches sends the intro first
    before = lab.rows()

    assert lab.local.sweep(T0 + 1) == 0

    assert lab.rows() == before
    assert len(lab.publisher.list_changes(ep.vid)) == 1
    assert local_attempts(lab, ep)[-1]["outcome"] == "declined"


# ── intros ────────────────────────────────────────────────────────────────────────────────────────────────────────


def test_an_intro_is_never_sent_while_intros_are_not_safe(lab):
    lab.kinds[:] = ["credits", "intro"]
    ep = lab.episode(1)
    analyse(lab, ep, SURE_INTRO)
    before = lab.rows()

    assert autosend.INTRO_AUTOSEND_SAFE is False
    assert lab.local.sweep(T0) == 0

    assert lab.rows() == before and lab.publisher.list_changes(ep.vid) == []
    assert owner_spans(lab, ep) == []


def test_an_intro_is_sent_once_intros_are_safe_and_the_switch_is_on(lab, monkeypatch):
    monkeypatch.setattr(autosend, "INTRO_AUTOSEND_SAFE", True)
    lab.kinds[:] = ["intro"]
    ep = lab.episode(1)
    analyse(lab, ep, SURE_INTRO)

    assert lab.local.sweep(T0) == 1

    assert _marker_times(lab.plex, "intro") == [(5_000, 20_000)]
    assert [(s.kind, s.start_ms, s.end_ms) for s in owner_spans(lab, ep)] == [("intro", 5_000, 20_000)]


# ── when Plex does not take it ────────────────────────────────────────────────────────────────────────────────────


def test_a_refused_write_puts_everything_back_and_tells_the_owner_once(lab, monkeypatch):
    ep = credits_episode(lab)
    before = lab.rows()
    spy = Flaky(lab.publisher.apply, *(ValueError("Plex said no") for _ in range(10)))
    monkeypatch.setattr(lab.publisher, "apply", spy)

    assert lab.local.sweep(T0) == 0

    assert spy.calls == 1
    assert lab.rows() == before
    assert [c["state"] for c in lab.publisher.list_changes(ep.vid)] == ["not_applied"]
    assert owner_spans(lab, ep) == []
    assert [r.review_status for r in lab.store.get_result(ep.vid).ranges if r.kind == "credits"] == ["proposed"]
    assert all(r.origin != "owner" for r in lab.store.get_result(ep.vid).ranges)
    assert lab.post.titles() == ["Plex refused an automatic change"]  # Plex's refusal is told once, in its own words


def test_a_cut_off_send_is_taken_back_after_a_restart_and_the_owner_is_told(lab):
    ep = credits_episode(lab)
    before = lab.rows()
    revision = lab.revision(ep)
    [piece] = [r for r in lab.store.get_result(ep.vid).ranges if r.kind == "credits"]
    # What the sender had done when the process died after preparing: the skip set, a change prepared, "working".
    lab.local._record(ep.vid, revision, "working", now=T0, range_ids=["x"])
    _, new_revision = lab.local._approve(ep.vid, revision, [piece], T0)
    lab.publisher.prepare(ep.vid, new_revision, lab.service._bindings(ep.vid)[0][0], AUTO_ACTOR)
    assert [s.kind for s in owner_spans(lab, ep)] == ["credits"]

    restarted = LocalModelSender(
        lab.service,
        lab.concerns,
        lab.state,
        config_dir=str(lab.dir),
        kinds=lambda: ("credits",),
        clock=lab.clock,
        backup=lab.backups,
    )
    restarted.sweep(T0 + 10)

    assert lab.rows() == before
    assert owner_spans(lab, ep) == []
    assert [c["state"] for c in lab.publisher.list_changes(ep.vid)] == ["not_applied"]
    assert lab.post.titles() == [FAILED_TITLE]
    assert "put everything back" in lab.post.messages()[0]


def test_a_failure_after_the_skip_was_made_leaves_no_skip_behind_and_names_the_episode(lab, monkeypatch):
    ep = credits_episode(lab)
    before = lab.rows()
    monkeypatch.setattr(lab.publisher, "prepare", Flaky(lab.publisher.prepare, PublicationRefused("Plex moved on")))

    assert lab.local.sweep(T0) == 0

    assert lab.rows() == before
    assert owner_spans(lab, ep) == []
    assert [r.review_status for r in lab.store.get_result(ep.vid).ranges if r.kind == "credits"] == ["proposed"]
    assert lab.post.titles() == [FAILED_TITLE]
    assert "Community S01E01" in lab.post.messages()[0]


def test_a_pick_plex_already_has_exactly_is_done_without_a_false_alert(lab):
    ep = credits_episode(lab)
    _seed_marker(lab.plex, "credits", 80_000, DURATION, 0)  # what Plex holds now is exactly what the model picked
    before = lab.rows()

    assert lab.local.sweep(T0) == 0

    assert lab.rows() == before and lab.publisher.list_changes(ep.vid) == []
    assert owner_spans(lab, ep) == []  # the skip it made for the send is gone again
    assert [r.review_status for r in lab.store.get_result(ep.vid).ranges if r.kind == "credits"] == ["proposed"]
    assert lab.post.calls == [] and not lab.concerns.open_keys()
    assert {a["outcome"] for a in local_attempts(lab, ep)} == {"already"}  # every revision it went through
    assert lab.state.get("send_pause") is None and lab.local.blocked(T0) is None
    seen = local_attempts(lab, ep)
    assert lab.local.sweep(T0 + 7 * 3600) == 0  # done: never looked at again
    assert local_attempts(lab, ep) == seen and lab.post.calls == []


def test_an_alert_about_a_failed_send_is_closed_once_plex_turns_out_to_have_the_skip(lab, monkeypatch):
    ep = credits_episode(lab)
    monkeypatch.setattr(lab.publisher, "prepare", Flaky(lab.publisher.prepare, PublicationRefused("Plex moved on")))
    assert lab.local.sweep(T0) == 0 and lab.post.titles() == [FAILED_TITLE]
    _seed_marker(lab.plex, "credits", 80_000, DURATION, 0)  # meanwhile the skip reached Plex some other way

    assert lab.local.sweep(T0 + autosend.FAILED_RETRY_S + 1) == 0

    assert not lab.concerns.open_keys()
    assert lab.post.titles() == [FAILED_TITLE, f"Resolved: {FAILED_TITLE}"]
    assert local_attempts(lab, ep)[-1]["outcome"] == "already"  # the earlier failures stay on their old revisions


def test_taking_the_skips_back_leaves_the_owners_own_alone(lab):
    ep = credits_episode(lab)
    lab.owner_review(ep, [CREDITS_SPAN])  # the owner's, made by "owner"
    revision = lab.revision(ep)

    assert lab.store.discard_owner_spans(ep.vid, revision, AUTO_ACTOR) is None
    assert [(s.kind, s.start_ms) for s in owner_spans(lab, ep)] == [("credits", 80_000)]
    with pytest.raises(RevisionConflict):
        lab.store.discard_owner_spans(ep.vid, revision - 1, "owner")


# ── what Reprise sent by itself ───────────────────────────────────────────────────────────────────────────────────


def test_the_history_lists_what_reprise_sent_newest_first_with_what_its_undo_needs(lab):
    first, second = credits_episode(lab, 1), credits_episode(lab, 2)
    lab.local.sweep(T0)
    first_change = lab.publisher.list_changes(first.vid)[0]["id"]

    found = approvals.recent_automatic(lab.service)

    assert {e["media_version_id"] for e in found} == {first.vid, second.vid}
    [entry] = [e for e in found if e["media_version_id"] == first.vid]
    assert entry == {
        "change_id": first_change,
        "media_version_id": first.vid,
        "show": "Community",
        "season": 1,
        "episode": 1,
        "label": "S01E01",
        "kinds": ["credits"],
        "sent_at": entry["sent_at"],
        "span_ids": [s.id for s in owner_spans(lab, first)],
        "origin": "local_model",
    }
    assert isinstance(entry["sent_at"], int | float) and len(entry["span_ids"]) == 1


def test_the_history_is_newest_first_and_honours_since_and_limit(lab):
    one, two = credits_episode(lab, 1), credits_episode(lab, 2)
    lab.local.sweep(T0)
    with lab.store.transaction() as conn:
        conn.execute(
            "UPDATE reprise_publication_changes SET updated_at=? WHERE media_version_id=?", (T0 - 10_000, one.vid)
        )
        conn.execute("UPDATE reprise_publication_changes SET updated_at=? WHERE media_version_id=?", (T0, two.vid))

    assert [e["media_version_id"] for e in approvals.recent_automatic(lab.service)] == [two.vid, one.vid]
    assert [e["media_version_id"] for e in approvals.recent_automatic(lab.service, since=T0 - 1)] == [two.vid]
    assert [e["media_version_id"] for e in approvals.recent_automatic(lab.service, limit=1)] == [two.vid]


def test_the_history_holds_only_what_reprise_sent_and_a_sure_match_has_no_skips_to_withdraw(lab):
    mine = credits_episode(lab, 1)
    lab.local.sweep(T0)
    theirs = lab.episode(2)
    analyse(lab, theirs, SURE_CREDITS)
    lab.owner_review(theirs, [CREDITS_SPAN])
    lab.publisher.prepare(theirs.vid, lab.revision(theirs), theirs.identity, "owner")
    sure = sure_intro(lab, 3)
    lab.sweep(T0 + 1)  # the sender of sure matches

    found = {e["media_version_id"]: e for e in approvals.recent_automatic(lab.service)}

    assert set(found) == {mine.vid, sure.vid}
    assert (found[sure.vid]["origin"], found[sure.vid]["span_ids"], found[sure.vid]["kinds"]) == ("sure", [], ["intro"])
    assert found[mine.vid]["origin"] == "local_model"


def test_an_undone_change_leaves_the_history_and_undo_puts_everything_back(lab):
    ep = credits_episode(lab)
    before = lab.rows()
    lab.local.sweep(T0)
    [entry] = approvals.recent_automatic(lab.service)

    back = approvals.undo(lab.service, [{k: entry[k] for k in ("media_version_id", "change_id", "span_ids", "label")}])

    assert back["failed"] == [] and [r["label"] for r in back["restored"]] == ["S01E01"]
    assert lab.rows() == before
    assert owner_spans(lab, ep) == []
    assert approvals.recent_automatic(lab.service) == []
    lab.local.sweep(T0 + 3600)  # what was undone is never sent again
    assert lab.rows() == before and approvals.recent_automatic(lab.service) == []


def test_the_history_is_empty_without_a_publisher(lab):
    lab.service.publisher = None
    assert approvals.recent_automatic(lab.service) == []


@pytest.fixture
def client(lab, monkeypatch):
    monkeypatch.setattr("media_preview_generator.web.auth.is_auth_external", lambda: True)
    app = Flask(__name__)
    app.secret_key = "throwaway"
    app.register_blueprint(create_reprise_blueprint(lab.service))
    return app.test_client()


def test_the_history_route_returns_the_entries_the_undo_route_takes_back(client, lab):
    ep = credits_episode(lab)
    lab.local.sweep(T0)

    answer = client.get("/api/reprise/approvals/automatic")

    [entry] = answer.get_json()["sent"]
    assert answer.status_code == 200 and entry["media_version_id"] == ep.vid and entry["origin"] == "local_model"
    undone = client.post(
        "/api/reprise/approvals/undo",
        json={"changes": [{k: entry[k] for k in ("media_version_id", "change_id", "span_ids", "label")}]},
    )
    assert undone.status_code == 200 and len(undone.get_json()["restored"]) == 1
    assert client.get("/api/reprise/approvals/automatic").get_json() == {"sent": []}


def test_the_history_route_only_looks_back_as_far_as_asked(client, lab):
    credits_episode(lab)
    lab.local.sweep(T0)
    with lab.store.transaction() as conn:  # sent three days ago
        conn.execute("UPDATE reprise_publication_changes SET updated_at=?", (time.time() - 3 * 86400,))

    assert len(client.get("/api/reprise/approvals/automatic?days=7").get_json()["sent"]) == 1
    assert client.get("/api/reprise/approvals/automatic?days=2").get_json() == {"sent": []}


@pytest.mark.parametrize("days", ["0", "31", "x", "1.5"])
def test_a_bad_number_of_days_is_a_400(client, lab, days):
    assert client.get(f"/api/reprise/approvals/automatic?days={days}").status_code == 400
