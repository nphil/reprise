"""The "To approve" queue: what waits for the owner, the one-click approve-and-send of the sure ones, deciding one at a
time, and an undo for each of them. Real store, publisher and Plex database file (the ``Lab`` of the automatic sender)."""

from types import SimpleNamespace

import pytest
from flask import Flask

from media_preview_generator.reprise import approvals, localvision, plexbackup
from media_preview_generator.reprise.analysis import with_local_model
from media_preview_generator.reprise.models import AnalysisDraft, ReviewRefused
from media_preview_generator.reprise.store import RevisionConflict
from media_preview_generator.web.routes.api_reprise import create_reprise_blueprint
from tests.reprise.test_autosend import DURATION, Flaky, Lab, _marker_times, suggestion

SURE_CREDITS = ("credits", 80_000, DURATION, localvision.HIGH)
SURE_INTRO = ("intro", 5_000, 20_000, localvision.HIGH)


@pytest.fixture
def lab(tmp_path, monkeypatch):
    app = Lab(tmp_path, monkeypatch)
    app.service.job_manager = SimpleNamespace(config_dir=str(tmp_path))
    monkeypatch.setattr(plexbackup, "ensure_daily_backup", app.backups)
    monkeypatch.setattr(
        approvals, "read_plex_markers", lambda server, keys: ({k: app.plex_markers.get(k, []) for k in keys}, None)
    )
    monkeypatch.setattr(approvals, "_hidden_kinds", lambda server, library: set())
    app.plex_markers = {}
    yield app
    if not app.marker._closed:
        app.marker.close()


def verdict(kind, start, end, confidence):
    return localvision.Verdict(
        kind,
        start,
        end,
        ((start, "credits", True),),
        "qwen3-vl-4b",
        confidence,
        ("Two other episodes show the same pictures.",),
        "pictures",
    )


def analyse(lab, ep, *local, others=()):
    """Store an analysis of ``ep`` holding the local model's ``(kind, start, end, confidence)`` suggestions and the
    other ``(evidence, range)`` parts."""
    draft = AnalysisDraft(tuple(e for found, _ in others for e in found), tuple(r for _, r in others), "complete", ())
    verdicts = {kind: verdict(kind, start, end, confidence) for kind, start, end, confidence in local}
    draft = with_local_model(draft, lab.store.get_media(ep.vid), verdicts)
    run = lab.store.begin_analysis(ep.vid, "job", "signature")
    lab.store.finish_analysis(run, list(draft.evidence), list(draft.ranges), ep.vid, list(draft.reasons))
    return ep


def ranges_of(lab, ep, kind):
    return [r for r in lab.store.get_result(ep.vid).ranges if r.kind == kind]


def owner_spans(lab, ep):
    return [r for r in lab.store.get_result(ep.vid).ranges if r.origin == "owner" and r.review_status == "approved"]


def counts(queue, show="Community", season=1):
    [found] = [
        s for entry in queue["shows"] if entry["show"] == show for s in entry["seasons"] if s["season"] == season
    ]
    return {key: found[key] for key in ("sure", "needs", "ready")}


# ── what is waiting ───────────────────────────────────────────────────────────────────────────────────────────────


def test_only_high_confidence_local_suggestions_are_sure_and_plexs_own_marker_is_not_waiting_at_all(lab):
    analyse(lab, lab.episode(1), SURE_INTRO, ("credits", 80_000, DURATION, localvision.MEDIUM))
    analyse(lab, lab.episode(2), ("intro", 6_000, 21_000, localvision.MEDIUM))
    third = lab.episode(3)
    analyse(lab, third, others=[suggestion(third, "intro", 10_000, 20_000, origin="plex")])
    analyse(lab, lab.episode(4), ("credits", 80_000, DURATION, localvision.LOW))
    analyse(lab, lab.episode(5, season=2), SURE_CREDITS)

    queue = approvals.queue(lab.service)

    assert counts(queue) == {"sure": 1, "needs": 3, "ready": 0}
    assert counts(queue, season=2) == {"sure": 1, "needs": 0, "ready": 0}
    assert queue["totals"] == {"sure": 2, "needs": 3, "ready": 0, "seasons": 2}
    assert [(s["label"], s["needs"]) for s in queue["shows"][0]["seasons"]] == [("Season 1", 3), ("Season 2", 0)]
    assert queue["sending"] == {"available": True, "enabled": True, "message": None}


def test_a_season_with_most_to_decide_comes_first_and_one_with_nothing_left_is_not_listed(lab):
    analyse(lab, lab.episode(1, season=1), SURE_CREDITS)
    analyse(lab, lab.episode(2, season=2), ("intro", 6_000, 21_000, localvision.MEDIUM))
    analyse(lab, lab.episode(3, season=3), SURE_INTRO)
    lab.episode(4, season=4)  # nothing found there

    seasons = [s["label"] for s in approvals.queue(lab.service)["shows"][0]["seasons"]]

    assert seasons == ["Season 2", "Season 1", "Season 3"]  # the decision first, then the sure ones in library order


def test_the_best_candidate_for_a_kind_is_the_one_shown_and_a_weaker_guess_never_hides_a_stronger(lab):
    ep = lab.episode(1)
    analyse(
        lab,
        ep,
        ("credits", 80_000, DURATION, localvision.MEDIUM),
        others=[suggestion(ep, "credits", 70_000, DURATION, origin="online")],
    )

    [item] = approvals.season_plan(lab.service, "Community", 1)["needs"]

    assert (item["kind"], item["start_ms"], item["how_sure"]) == ("credits", 80_000, "medium")
    assert len(item["range_ids"]) == 2  # the other source's guess is settled with it


def test_an_episode_the_owner_approved_is_waiting_to_be_sent_until_it_is_sent(lab):
    ep = analyse(lab, lab.episode(1), ("intro", 6_000, 21_000, localvision.MEDIUM))
    approvals.decide(lab.service, ep.vid, lab.revision(ep), "intro", "skip", start_ms=6_000, end_ms=21_000)

    assert counts(approvals.queue(lab.service)) == {"sure": 0, "needs": 0, "ready": 1}

    approvals.send(lab.service, "Community", 1, "approved")

    assert approvals.queue(lab.service)["totals"] == {"sure": 0, "needs": 0, "ready": 0, "seasons": 0}


def published_then_reanalysed(lab, number=1):
    """An episode whose credits skip the owner approved and Reprise sent, after which the analysis ran again: the result
    moved on to a new revision, so nothing in the journal matches it any more, yet Plex serves exactly what was sent
    (``lab.plex_markers`` is Plex's answer to the queue, the real Plex file is what a send reads)."""
    ep = analyse(lab, lab.episode(number), SURE_CREDITS)
    approvals.send(lab.service, "Community", 1, "sure")
    lab.plex_markers[str(number)] = [{"kind": "credits", "start_ms": 80_000, "end_ms": DURATION}]
    analyse(lab, ep, ("credits", 82_000, DURATION, localvision.HIGH))  # a different answer: a new revision
    return ep


EMPTY_QUEUE = {"sure": 0, "needs": 0, "ready": 0, "seasons": 0}


def test_a_skip_plex_already_serves_is_not_waiting_to_be_sent_even_after_the_analysis_ran_again(lab):
    ep = published_then_reanalysed(lab)
    assert [r.review_status for r in owner_spans(lab, ep)] == ["approved"]  # still the owner's skip, never undone

    assert approvals.queue(lab.service)["totals"] == EMPTY_QUEUE


def test_an_approved_skip_that_differs_from_plex_even_slightly_still_waits_to_be_sent(lab):
    published_then_reanalysed(lab)
    lab.plex_markers["1"] = [{"kind": "credits", "start_ms": 80_400, "end_ms": DURATION}]  # a few tenths of a second

    assert counts(approvals.queue(lab.service)) == {"sure": 0, "needs": 0, "ready": 1}


def test_when_plex_cannot_be_read_nothing_is_taken_out_of_the_queue(lab, monkeypatch):
    published_then_reanalysed(lab)
    analyse(lab, lab.episode(2), ("intro", 5_000, 20_000, localvision.MEDIUM))
    monkeypatch.setattr(approvals, "read_plex_markers", lambda server, keys: ({}, "Plex did not answer"))

    assert counts(approvals.queue(lab.service)) == {"sure": 0, "needs": 1, "ready": 1}


def test_an_asked_for_skip_is_ready_only_for_what_plex_does_not_have(lab):
    ep = published_then_reanalysed(lab)
    approvals.decide(lab.service, ep.vid, lab.revision(ep), "intro", "skip", start_ms=6_000, end_ms=21_000)

    [item] = approvals.season_plan(lab.service, "Community", 1)["ready"]

    assert [(s["kind"], s["change"]) for s in item["skips"]] == [("intro", "new")]  # the credits skip is in Plex


def test_a_suggestion_plex_already_has_is_nothing_to_look_at_but_a_different_one_is(lab):
    for number in (1, 2, 3):
        analyse(lab, lab.episode(number), ("intro", 5_000, 20_000, localvision.MEDIUM))
    lab.plex_markers = {
        "1": [{"kind": "intro", "start_ms": 5_042, "end_ms": 19_960}],  # Plex found the same one
        "2": [{"kind": "intro", "start_ms": 9_000, "end_ms": 24_000}],  # Plex has a different one
    }

    plan = approvals.season_plan(lab.service, "Community", 1)

    assert [(i["episode"], i["change"]) for i in plan["needs"]] == [(2, "moved"), (3, "new")]
    assert counts(approvals.queue(lab.service)) == {"sure": 0, "needs": 2, "ready": 0}


def test_a_season_whose_skips_plex_all_has_is_not_listed_at_all(lab):
    for number in (1, 2):
        ep = lab.episode(number)
        analyse(lab, ep, ("intro", 5_000, 20_000, localvision.MEDIUM), SURE_CREDITS)
        lab.plex_markers[str(number)] = [
            {"kind": "intro", "start_ms": 5_000, "end_ms": 20_000},
            {"kind": "credits", "start_ms": 80_000, "end_ms": DURATION},
        ]

    assert approvals.queue(lab.service)["totals"] == EMPTY_QUEUE


def test_pieces_that_touch_are_one_skip_and_pieces_with_a_gap_or_of_another_kind_are_not():
    piece = lambda kind, start, end: SimpleNamespace(kind=kind, start_ms=start, end_ms=end)  # noqa: E731

    skips = approvals._joined(
        [piece("intro", 0, 5), piece("intro", 5, 9), piece("credits", 9, 12), piece("intro", 10, 14), piece("x", 1, 2)]
    )

    assert [(s["kind"], s["start_ms"], s["end_ms"]) for s in skips] == [
        ("intro", 0, 9),
        ("credits", 9, 12),
        ("intro", 10, 14),
    ]


# ── one decision at a time ────────────────────────────────────────────────────────────────────────────────────────


def test_skip_sets_the_owners_own_exact_skip_and_its_undo_puts_the_suggestion_back(lab):
    ep = analyse(lab, lab.episode(1), ("intro", 6_000, 21_000, localvision.MEDIUM))

    done = approvals.decide(lab.service, ep.vid, lab.revision(ep), "intro", "skip", start_ms=7_500, end_ms=22_000)

    [span] = owner_spans(lab, ep)
    assert (span.kind, span.start_ms, span.end_ms, span.locked) == ("intro", 7_500, 22_000, True)
    assert done["revision"] == lab.revision(ep) and done["span_id"] == span.id
    assert counts(approvals.queue(lab.service)) == {"sure": 0, "needs": 0, "ready": 1}

    approvals.decide(lab.service, ep.vid, done["revision"], **done["undo"])

    assert owner_spans(lab, ep) == []
    assert counts(approvals.queue(lab.service)) == {"sure": 0, "needs": 1, "ready": 0}


def test_keep_turns_the_suggestion_down_and_its_undo_asks_for_it_again(lab):
    ep = analyse(lab, lab.episode(1), ("credits", 80_000, DURATION, localvision.LOW))

    done = approvals.decide(lab.service, ep.vid, lab.revision(ep), "credits", "keep")

    assert [r.review_status for r in ranges_of(lab, ep, "credits")] == ["rejected"]
    assert approvals.queue(lab.service)["totals"]["needs"] == 0

    approvals.decide(lab.service, ep.vid, done["revision"], **done["undo"])

    assert [r.review_status for r in ranges_of(lab, ep, "credits")] == ["proposed"]
    assert approvals.queue(lab.service)["totals"]["needs"] == 1


def test_a_decision_leaves_everything_the_owner_approved_before_approved(lab):
    ep = analyse(
        lab, lab.episode(1), ("intro", 6_000, 21_000, localvision.MEDIUM), ("credits", 80_000, DURATION, "low")
    )
    approvals.decide(lab.service, ep.vid, lab.revision(ep), "intro", "skip", start_ms=6_000, end_ms=21_000)

    approvals.decide(lab.service, ep.vid, lab.revision(ep), "credits", "skip", start_ms=80_000, end_ms=DURATION)

    assert sorted(r.kind for r in owner_spans(lab, ep)) == ["credits", "intro"]


def test_a_decision_made_on_an_older_screen_is_refused_not_applied(lab):
    ep = analyse(lab, lab.episode(1), ("intro", 6_000, 21_000, localvision.MEDIUM))
    stale = lab.revision(ep)
    approvals.decide(lab.service, ep.vid, stale, "intro", "keep")

    with pytest.raises(RevisionConflict):
        approvals.decide(lab.service, ep.vid, stale, "intro", "skip", start_ms=6_000, end_ms=21_000)

    assert owner_spans(lab, ep) == []


@pytest.mark.parametrize(
    ("args", "sentence"),
    [
        ({"kind": "intro", "decision": "skip"}, "Say where the skip starts and ends."),
        ({"kind": "intro", "decision": "skip", "start_ms": 9, "end_ms": 9}, "Say where the skip starts and ends."),
        ({"kind": "recap", "decision": "keep"}, "Only an intro or a credits skip can be decided here."),
        ({"kind": "credits", "decision": "keep"}, "There is nothing left to decide for this one."),
        ({"kind": "intro", "decision": "maybe"}, "A decision is skip, keep or restore."),
    ],
)
def test_a_decision_that_makes_no_sense_says_what_is_missing(lab, args, sentence):
    ep = analyse(lab, lab.episode(1), ("intro", 6_000, 21_000, localvision.MEDIUM))

    with pytest.raises(ReviewRefused, match=sentence):
        approvals.decide(lab.service, ep.vid, lab.revision(ep), **args)


# ── what would change in Plex ─────────────────────────────────────────────────────────────────────────────────────


def test_a_season_says_in_plain_words_what_sending_would_change(lab):
    for number in (1, 2, 3):
        analyse(lab, lab.episode(number), SURE_CREDITS)
    lab.plex_markers = {
        "1": [{"kind": "credits", "start_ms": 68_000, "end_ms": DURATION}],  # ours starts 12 s later
        "2": [{"kind": "credits", "start_ms": 80_400, "end_ms": DURATION}],  # within a second: Plex has it already
    }  # episode 3 has none

    plan = approvals.season_plan(lab.service, "community", 1)

    assert [(i["episode"], i["change"]) for i in plan["sure"]] == [(1, "moved"), (3, "new")]  # nothing to decide on 2
    assert plan["changes"]["sure"]["lines"] == [
        "Credits skip is added on 1 episode where Plex has none.",
        "Credits skip starts about 12 s later on 1 episode.",
    ]
    assert plan["changes"]["sure"]["counts"] == {"new": 1, "same": 0, "moved": 1, "unknown": 0}
    assert plan["plex"]["status"] == "read"


def test_a_move_of_more_than_half_a_minute_is_called_out_and_an_unreadable_plex_is_said_to_be_unknown(lab, monkeypatch):
    analyse(lab, lab.episode(1), SURE_CREDITS)
    lab.plex_markers = {"1": [{"kind": "credits", "start_ms": 40_000, "end_ms": DURATION}]}

    lines = approvals.season_plan(lab.service, "Community", 1)["changes"]["sure"]["lines"]

    assert "1 episode move by more than 30 s: check one before you send." in lines

    monkeypatch.setattr(approvals, "read_plex_markers", lambda server, keys: ({}, "Plex did not answer"))
    plan = approvals.season_plan(lab.service, "Community", 1)

    assert plan["plex"] == {"status": "unavailable", "message": "Plex did not answer"}
    assert plan["sure"][0]["change"] == "unknown"
    assert plan["changes"]["sure"]["lines"] == [
        "Plex could not be read for 1 episode, so what changes there is unknown."
    ]


# ── approve and send ──────────────────────────────────────────────────────────────────────────────────────────────


def sure_season(lab, episodes=(1, 2)):
    """Episodes with a sure credits skip each, and one with only a medium intro that must never be swept along."""
    sure = [analyse(lab, lab.episode(number), SURE_CREDITS) for number in episodes]
    unsure = analyse(lab, lab.episode(9), ("intro", 6_000, 21_000, localvision.MEDIUM))
    return sure, unsure


def test_sending_what_plex_already_has_says_so_plainly_writes_nothing_and_the_season_leaves_the_queue(lab):
    published_then_reanalysed(lab)
    before, backups = lab.rows(), len(lab.backups.calls)

    done = approvals.send(lab.service, "Community", 1, "approved")

    assert done["message"] == "Plex already has these skips. Nothing to send."
    assert done["sent"] == [] and done["failed"] == [] and done["approved"] == [] and done["stopped"] is None
    assert [(a["label"], a["kinds"]) for a in done["already"]] == [("S01E01", ["credits"])]
    assert lab.rows() == before and len(lab.backups.calls) == backups  # not even a backup was needed
    assert approvals.queue(lab.service)["totals"] == EMPTY_QUEUE


def test_a_send_that_finds_plex_already_has_the_skip_is_not_a_failure_even_if_plex_could_not_be_read_first(
    lab, monkeypatch
):
    published_then_reanalysed(lab)
    monkeypatch.setattr(approvals, "read_plex_markers", lambda server, keys: ({}, "Plex did not answer"))
    before = lab.rows()

    done = approvals.send(lab.service, "Community", 1, "approved")  # the publisher itself finds nothing to publish

    assert done["message"] == "Plex already has these skips. Nothing to send."
    assert done["failed"] == [] and done["sent"] == [] and [a["label"] for a in done["already"]] == ["S01E01"]
    assert lab.rows() == before


def test_with_one_episode_sent_and_one_already_there_the_answer_counts_both_and_has_no_message(lab):
    published_then_reanalysed(lab)  # episode 1: already in Plex
    ep = analyse(lab, lab.episode(2), SURE_CREDITS)
    approvals.decide(lab.service, ep.vid, lab.revision(ep), "credits", "skip", start_ms=80_000, end_ms=DURATION)

    done = approvals.send(lab.service, "Community", 1, "approved")

    assert [s["label"] for s in done["sent"]] == ["S01E02"]
    assert [a["label"] for a in done["already"]] == ["S01E01"] and done["message"] is None
    assert done["failed"] == []


def test_one_click_backs_plex_up_then_approves_and_sends_only_the_sure_ones(lab):
    sure, unsure = sure_season(lab)
    before = lab.rows()

    done = approvals.send(lab.service, "Community", 1, "sure")

    assert [a["label"] for a in done["approved"]] == ["S01E01", "S01E02"]
    assert [(s["label"], s["kinds"]) for s in done["sent"]] == [("S01E01", ["credits"]), ("S01E02", ["credits"])]
    assert done["failed"] == [] and done["stopped"] is None and done["backup"]["created"] is True
    assert lab.backups.calls[0]["rows"] == before  # the backup was taken before anything was written
    assert all(len(_marker_times(lab.plex, "credits", item=ep.number)) == 1 for ep in sure)
    assert owner_spans(lab, unsure) == [] and [r.review_status for r in ranges_of(lab, unsure, "intro")] == ["proposed"]
    assert counts(approvals.queue(lab.service)) == {"sure": 0, "needs": 1, "ready": 0}


def test_undo_gives_plex_its_old_markers_back_and_the_sure_ones_wait_again(lab):
    sure, _ = sure_season(lab)
    done = approvals.send(lab.service, "Community", 1, "sure")

    back = approvals.undo(
        lab.service, [{k: s[k] for k in ("media_version_id", "change_id", "span_ids", "label")} for s in done["sent"]]
    )

    assert [r["label"] for r in back["restored"]] == ["S01E01", "S01E02"] and back["failed"] == []
    assert all(_marker_times(lab.plex, "credits", item=ep.number) == [] for ep in sure)
    assert all(owner_spans(lab, ep) == [] for ep in sure)
    assert counts(approvals.queue(lab.service)) == {"sure": 2, "needs": 1, "ready": 0}


def test_sending_is_refused_while_plex_writes_are_off_and_nothing_is_approved(lab):
    sure, _ = sure_season(lab)
    lab.publisher.writes_enabled = False

    assert approvals.queue(lab.service)["sending"]["enabled"] is False
    with pytest.raises(ReviewRefused, match="Sending to Plex is turned off"):
        approvals.send(lab.service, "Community", 1, "sure")

    assert owner_spans(lab, sure[0]) == [] and lab.backups.calls == []


def test_with_sending_off_the_sure_ones_can_still_be_approved_and_the_approval_taken_back(lab):
    sure, _ = sure_season(lab)
    lab.publisher.writes_enabled = False

    done = approvals.send(lab.service, "Community", 1, "sure", to_plex=False)

    assert len(done["approved"]) == 2 and done["sent"] == [] and done["backup"] is None
    assert counts(approvals.queue(lab.service)) == {"sure": 0, "needs": 1, "ready": 2}
    approvals.undo(lab.service, done["approved"])
    assert counts(approvals.queue(lab.service)) == {"sure": 2, "needs": 1, "ready": 0}


def test_without_a_backup_nothing_is_approved_and_nothing_is_written(lab):
    sure, _ = sure_season(lab)
    before = lab.rows()
    lab.backups.error = plexbackup.BackupFailed("The backup folder could not be written.")

    with pytest.raises(ReviewRefused) as refused:
        approvals.send(lab.service, "Community", 1, "sure")

    assert str(refused.value) == (
        "Nothing was sent. Reprise could not back up Plex's database first. The backup folder could not be written."
    )
    assert lab.rows() == before and all(owner_spans(lab, ep) == [] for ep in sure)


def test_one_episode_plex_refuses_is_reported_and_the_rest_are_still_sent(lab, monkeypatch):
    sure, _ = sure_season(lab)
    monkeypatch.setattr(lab.publisher, "prepare", Flaky(lab.publisher.prepare, ValueError("Plex rows changed")))

    done = approvals.send(lab.service, "Community", 1, "sure")

    assert [s["label"] for s in done["sent"]] == ["S01E02"]
    assert [(f["label"], f["reason"]) for f in done["failed"]] == [("S01E01", "Plex rows changed.")]
    assert counts(approvals.queue(lab.service))["ready"] == 1  # the refused one stays approved, waiting to be sent


def test_a_plex_that_cannot_be_reached_stops_the_batch_and_says_what_was_not_tried(lab, monkeypatch):
    sure_season(lab, (1, 2, 3))
    monkeypatch.setattr(lab.publisher, "prepare", Flaky(lab.publisher.prepare, OSError("connection refused")))

    done = approvals.send(lab.service, "Community", 1, "sure")

    assert done["sent"] == [] and done["stopped"] == "Plex could not be reached."
    assert [f["reason"] for f in done["failed"]] == [
        "Plex could not be reached.",
        "Not tried: sending stopped before it.",
        "Not tried: sending stopped before it.",
    ]


def test_sending_asks_for_something_that_exists(lab):
    sure_season(lab)

    for show, season, scope, sentence in (
        ("Community", 1, "everything", "Say whether to approve the sure ones or to send the ones you approved."),
        ("Community", 7, "sure", "There is nothing sure left to approve in this season."),
        ("Community", 1, "approved", "Nothing in this season is waiting to be sent."),
    ):
        with pytest.raises(ReviewRefused, match=sentence):
            approvals.send(lab.service, show, season, scope)


# ── over HTTP ─────────────────────────────────────────────────────────────────────────────────────────────────────


class Pictures:
    """Stands in for ffmpeg: the same three questions, answered from memory."""

    def __init__(self):
        self.asked = []

    def still(self, version, at_ms, width):
        self.asked.append(("still", at_ms, width))
        return b"\xff\xd8still"

    def strip(self, version, start_ms, step_ms, count, width):
        self.asked.append(("strip", start_ms, step_ms, count, width))
        return b"\xff\xd8strip"

    def cuts(self, version, start_ms, end_ms):
        self.asked.append(("cuts", start_ms, end_ms))
        if end_ms == 12_345:
            raise OSError("Reprise could not read the pictures around this time.")
        return [start_ms + 500]


@pytest.fixture
def client(lab, monkeypatch):
    monkeypatch.setattr("media_preview_generator.web.auth.is_auth_external", lambda: True)
    app = Flask(__name__)
    app.secret_key = "throwaway"
    app.register_blueprint(create_reprise_blueprint(lab.service))
    lab.service._frames = Pictures()
    return app.test_client()


def test_the_queue_a_season_and_a_one_click_send_work_over_http(client, lab):
    sure, _ = sure_season(lab)

    queue = client.get("/api/reprise/approvals").get_json()
    plan = client.get("/api/reprise/approvals/season?show=Community&season=1").get_json()
    done = client.post("/api/reprise/approvals/send", json={"show": "Community", "season": 1, "scope": "sure"})

    assert queue["totals"]["sure"] == 2 and [i["label"] for i in plan["sure"]] == ["S01E01", "S01E02"]
    assert done.status_code == 200 and len(done.get_json()["sent"]) == 2
    changes = [
        {k: s[k] for k in ("media_version_id", "change_id", "span_ids", "label")} for s in done.get_json()["sent"]
    ]
    undone = client.post("/api/reprise/approvals/undo", json={"changes": changes})
    assert undone.status_code == 200 and len(undone.get_json()["restored"]) == 2


def test_a_decision_over_http_carries_its_own_undo(client, lab):
    ep = analyse(lab, lab.episode(1), ("intro", 6_000, 21_000, localvision.MEDIUM))
    body = {"media_version_id": ep.vid, "expected_revision": lab.revision(ep), "kind": "intro", "decision": "skip"}

    refused = client.post("/api/reprise/approvals/decide", json=body)
    done = client.post("/api/reprise/approvals/decide", json={**body, "start_ms": 6_000, "end_ms": 21_000})
    stale = client.post("/api/reprise/approvals/decide", json={**body, "start_ms": 6_000, "end_ms": 21_000})

    assert (refused.status_code, refused.get_json()) == (400, {"error": "Say where the skip starts and ends."})
    assert done.status_code == 200 and done.get_json()["undo"]["decision"] == "restore"
    assert stale.status_code == 409


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/reprise/approvals/send", {"show": "Community", "season": "1", "scope": "sure"}),
        ("/api/reprise/approvals/send", {"show": "Community", "season": 1, "scope": "sure", "to_plex": "yes"}),
        ("/api/reprise/approvals/send", {"show": "", "season": 1, "scope": "sure"}),
        ("/api/reprise/approvals/send", {"show": "Nope", "season": 1, "scope": "sure"}),
        ("/api/reprise/approvals/undo", {"changes": []}),
        ("/api/reprise/approvals/undo", {"changes": [{"media_version_id": 1}]}),
        ("/api/reprise/approvals/decide", {"media_version_id": "v", "kind": "intro"}),
        (
            "/api/reprise/approvals/decide",
            {"media_version_id": "v", "expected_revision": "1", "kind": "intro", "decision": "keep"},
        ),
    ],
)
def test_a_malformed_approval_request_is_a_400(client, lab, path, body):
    sure_season(lab)

    assert client.post(path, json=body).status_code == 400


def test_pictures_come_back_as_cacheable_jpegs_and_a_bad_ask_is_refused(client, lab):
    ep = analyse(lab, lab.episode(1), ("intro", 6_000, 21_000, localvision.MEDIUM))
    base = f"/api/reprise/media/{ep.vid}"

    still = client.get(f"{base}/frame?t_ms=6000&w=480")
    strip = client.get(f"{base}/strip?start_ms=4000&step_ms=1000&count=8&w=160")
    cuts = client.get(f"{base}/cuts?start_ms=5000&end_ms=9000")

    assert (still.status_code, still.mimetype, still.data) == (200, "image/jpeg", b"\xff\xd8still")
    assert still.headers["Cache-Control"] == "private, max-age=300"
    assert (strip.status_code, strip.mimetype) == (200, "image/jpeg")
    assert cuts.get_json() == {"cuts": [5500]}
    assert cuts.headers["Cache-Control"] == "no-store"
    assert lab.service._frames.asked == [
        ("still", 6000, 480),
        ("strip", 4000, 1000, 8, 160),
        ("cuts", 5000, 9000),
    ]
    for bad in ("frame?t_ms=-1", "frame?t_ms=abc", "frame", "strip?start_ms=1&step_ms=0&count=1", "cuts?start_ms=9"):
        assert client.get(f"{base}/{bad}").status_code == 400
    assert client.get("/api/reprise/media/nope/frame?t_ms=1").status_code == 404
    assert client.get(f"{base}/cuts?start_ms=5000&end_ms=12345").status_code == 502
