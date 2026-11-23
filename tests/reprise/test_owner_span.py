"""The owner's exact skip: the one intro skip and one credits skip the owner sets by hand for an episode.

It is stored with origin ``owner``, approved and locked, and in Plex it replaces every other skip of its kind: Plex's
own marker and the analysis's pieces included. Nothing the analysis could not confirm stops it; only a scene the owner
protected, a skip they set or locked, and Plex's own limits do. The first tests are pure (a result in, ranges out); the
rest run the real store, the real publisher on a real Plex SQLite file and the real HTTP routes.
"""

from dataclasses import asdict, replace

import pytest
from flask import Flask

from media_preview_generator.reprise.autosend import sure_runs
from media_preview_generator.reprise.models import (
    DecisionRange,
    Evidence,
    PlaybackIdentity,
    PublicationRefused,
    ReviewRefused,
    is_owner_span,
)
from media_preview_generator.reprise.policy import eligible_ranges
from media_preview_generator.reprise.report import classify
from media_preview_generator.reprise.review import plex_projection
from media_preview_generator.reprise.store import NEEDS_OWNER_SPAN_ACKNOWLEDGMENT, RevisionConflict
from media_preview_generator.reprise.verification_profile import PROFILE_FINGERPRINT, PROFILE_ID
from media_preview_generator.web.routes.api_reprise import create_reprise_blueprint
from tests.reprise.test_autosend import (
    CREDITS,
    DURATION,
    INTRO,
    T0,
    Lab,
    _marker_rows,
    _marker_times,
    _seed_marker,
    assert_declined,
    confirmed,
    detector_keep,
    piece,
    pure_result,
    suggestion,
)
from tests.reprise.test_plex_report import flags_of, get, marker, rng, season  # noqa: F401 - season is a fixture

NOTHING_TO_SEND = "Plex already has exactly the skip you set, so there is nothing to publish"
SET_BY_OWNER = "You set a skip yourself, so sending it is up to you."
ALWAYS_APPROVED = (
    "A skip you set is always approved and locked. Give only its type (intro or credits) and its start and end times."
)
TWO_OF_A_KIND = "Set one exact skip for the intro and one for the credits, not two of the same."
STAYS_A_SKIP = "A skip you set stays a skip: reject it, or set it again."
NO_KIND_OR_TIMES = "Say whether the skip is the intro or the credits, and where it starts and ends."
OUTSIDE_THE_FILE = "The skip must start before it ends, and both times must be inside the episode."
LAST_TWO_SECONDS = "The final two seconds cannot be projected safely to Plex"
CREDITS_START = "Plex cannot represent this credits start exactly"
ALREADY_SENT = "It was already sent to Plex by you."

IDENTITY = PlaybackIdentity("machine", "1", "2", "3", "/part", 8, DURATION, "1")
METRICS = {"verification_profile": PROFILE_ID, "profile_fingerprint": PROFILE_FINGERPRINT}


# ── builders ──────────────────────────────────────────────────────────────────────────────────────────────────────


def span(kind="intro", start=10_000, end=20_000, **fields):
    """The owner's exact skip as the store keeps it."""
    found = {
        "id": f"span-{kind}",
        "action": "skip",
        "review_status": "approved",
        "locked": True,
        "origin": "owner",
        "reason_code": "owner_span",
        "reason": "Set by you",
        "evidence_ids": (),
        "audio_stream_ids": ("1",),
    }
    return DecisionRange(**{"kind": kind, "start_ms": start, "end_ms": end, **found, **fields})


def window(rid, start, end, verdict="unknown"):
    """Evidence about a stretch: only a ``match`` (picture and sound compared with another episode) confirms it."""
    matched = verdict == "match"
    return Evidence(
        rid,
        start,
        end,
        "audiovisual",
        verdict,
        "x",
        "",
        "reference" if matched else None,
        start if matched else None,
        end if matched else None,
        {**METRICS, **({"checked_audio_stream_ids": ["1"]} if matched else {})},
    )


def result_of(*ranges, evidence=()):
    return replace(pure_result(*ranges), evidence=tuple(evidence))


def eligible(result, audio="1"):
    return [(r.kind, r.start_ms, r.end_ms) for r in eligible_ranges(result, audio)]


def projected(result, plex_now=None):
    ranges = plex_projection(result, IDENTITY, eligible_ranges(result, IDENTITY.audio_stream_id), plex_now)
    return [(r.kind, r.start_ms, r.end_ms) for r in ranges]


def set_span(kind, start, end, **more):
    """The edit the review screen sends when the owner sets an exact skip."""
    return {
        "range_id": None,
        "kind": kind,
        "start_ms": start,
        "end_ms": end,
        "review_status": "approved",
        "acknowledge_owner_span": True,
        **more,
    }


# ── what is eligible (pure) ───────────────────────────────────────────────────────────────────────────────────────


def test_an_exact_skip_is_eligible_whole_across_what_nothing_confirmed():
    result = result_of(
        span("intro", 5_000, 40_000),
        piece("keep", "intro", 25_000, 28_000, action="keep"),  # a scene the analysis itself would not skip
        piece("maybe", "intro", 30_000, 35_000, origin="plex"),  # a suggestion nothing confirmed
        evidence=[window("e-keep", 25_000, 28_000, "different"), window("e-maybe", 30_000, 35_000)],
    )

    assert eligible(result) == [("intro", 5_000, 40_000)]


def test_the_same_stretch_approved_without_the_owners_say_so_is_cut_around_what_is_unconfirmed():
    result = result_of(
        piece("a", "intro", 5_000, 40_000, review_status="approved"),
        piece("keep", "intro", 25_000, 28_000, action="keep"),
        evidence=[window("e-a", 5_000, 40_000, "match"), window("e-keep", 25_000, 28_000, "different")],
    )

    assert eligible(result) == [("intro", 5_000, 25_000), ("intro", 28_000, 40_000)]


def test_an_exact_skip_replaces_every_other_skip_of_its_kind_and_leaves_the_other_kind_alone():
    result = result_of(
        span("intro", 5_000, 40_000),
        piece("a", "intro", 10_000, 20_000, review_status="approved"),
        piece("plex", "intro", 45_000, 50_000, origin="plex", review_status="approved"),
        piece("c", "credits", 80_000, 90_000, review_status="approved"),
        evidence=[
            window("e-a", 10_000, 20_000, "match"),
            window("e-plex", 45_000, 50_000),
            window("e-c", 80_000, 90_000, "match"),
        ],
    )

    assert eligible(result) == [("intro", 5_000, 40_000), ("credits", 80_000, 90_000)]


@pytest.mark.parametrize("status", ["proposed", "rejected"])
def test_an_exact_skip_that_is_not_approved_replaces_nothing(status):
    result = result_of(
        span("intro", 5_000, 40_000, review_status=status),
        piece("a", "intro", 10_000, 20_000, review_status="approved"),
        evidence=[window("e-a", 10_000, 20_000, "match")],
    )

    assert eligible(result) == [("intro", 10_000, 20_000)]


def test_an_exact_skip_is_eligible_only_for_its_audio_tracks_and_a_current_complete_analysis():
    result = result_of(span("intro", 5_000, 40_000))

    assert eligible(result) == [("intro", 5_000, 40_000)]
    assert eligible(result, "2") == []
    assert eligible(replace(result, freshness="stale")) == []
    assert eligible(replace(result, analysis_status="failed")) == []
    assert eligible(replace(result, policy_version="an older one")) == []


PROTECTED = [
    pytest.param(
        piece("k", "intro", 22_000, 26_000, action="keep", origin="manual"), "a scene you protected", id="owner's keep"
    ),
    pytest.param(
        piece("k", "intro", 22_000, 26_000, action="keep", review_status="approved"),
        "a scene you protected",
        id="approved keep",
    ),
    pytest.param(
        piece("k", "intro", 22_000, 26_000, action="keep", locked=True), "a scene you protected", id="locked keep"
    ),
    pytest.param(
        piece("k", "credits", 22_000, 26_000, locked=True),
        "a skip you set or locked",
        id="locked skip of the other kind",
    ),
    pytest.param(span("credits", 22_000, 26_000), "a skip you set or locked", id="exact skip of the other kind"),
]


@pytest.mark.parametrize(("blocker", "what"), PROTECTED)
def test_what_the_owner_protected_or_locked_stops_an_exact_skip(blocker, what):
    result = result_of(span("intro", 5_000, 40_000), blocker)

    # Never silently covered, and a send is refused in a plain sentence (the store refuses to save the clash at all).
    assert [p for p in eligible(result) if p[0] == "intro" and p[1] < 26_000 and 22_000 < p[2]] == []
    with pytest.raises(PublicationRefused) as refused:
        projected(result)
    assert str(refused.value) == (
        f"The intro skip you set would cover {what} (from 0:22 to 0:26). Change the skip, or remove that first."
    )


NOT_PROTECTED = [
    pytest.param(piece("k", "intro", 22_000, 26_000, action="keep"), id="a keep only the analysis made"),
    pytest.param(
        piece("k", "intro", 22_000, 26_000, action="keep", origin="manual", review_status="rejected"),
        id="a scene the owner stopped protecting",
    ),
    pytest.param(
        piece("k", "intro", 22_000, 26_000, locked=True, review_status="approved"), id="a locked skip of the same kind"
    ),
    pytest.param(piece("k", "credits", 22_000, 26_000), id="the analysis's own credits skip"),
]


@pytest.mark.parametrize("other", NOT_PROTECTED)
def test_nothing_else_stops_an_exact_skip(other):
    result = result_of(span("intro", 5_000, 40_000), other)

    assert eligible(result) == [("intro", 5_000, 40_000)]
    assert projected(result) == [("intro", 5_000, 40_000)]


# ── what is sent to Plex (pure) ───────────────────────────────────────────────────────────────────────────────────


def test_a_skip_that_differs_from_plexs_marker_is_sent_and_an_identical_one_has_nothing_to_send():
    result = result_of(span("intro", 10_000, 22_000))

    assert projected(result, {("intro", 10_000, 20_000)}) == [("intro", 10_000, 22_000)]
    assert projected(result, frozenset()) == [("intro", 10_000, 22_000)]
    with pytest.raises(PublicationRefused) as refused:
        projected(result, {("intro", 10_000, 22_000)})
    assert str(refused.value) == NOTHING_TO_SEND


def test_what_plex_holds_now_decides_over_what_the_analysis_once_saw():
    result = result_of(span("intro", 10_000, 20_000), piece("plex", "intro", 10_000, 20_000, origin="plex"))

    # Plex is taken to hold what it suggested when nobody has read it ...
    with pytest.raises(PublicationRefused) as refused:
        projected(result)
    assert str(refused.value) == NOTHING_TO_SEND
    # ... and when somebody has, its marker was edited or removed since: the skip is a change after all.
    assert projected(result, {("intro", 10_000, 22_000)}) == [("intro", 10_000, 20_000)]
    assert projected(result, frozenset()) == [("intro", 10_000, 20_000)]


def test_only_the_kind_that_differs_from_plex_is_sent():
    both = result_of(span("intro", 10_000, 20_000), span("credits", 80_000, 95_000))
    assert projected(both, {("intro", 10_000, 20_000), ("credits", 80_000, 90_000)}) == [("credits", 80_000, 95_000)]

    mixed = result_of(
        span("intro", 10_000, 20_000),
        piece("c", "credits", 80_000, 90_000, review_status="approved"),
        evidence=[window("e-c", 80_000, 90_000, "match")],
    )
    assert projected(mixed, {("intro", 10_000, 20_000)}) == [("credits", 80_000, 90_000)]


@pytest.mark.parametrize(
    ("kind", "start", "end", "limit"),
    [("intro", 90_000, 98_000, LAST_TWO_SECONDS), ("credits", 1_999, 50_000, CREDITS_START)],
)
def test_plexs_own_limits_still_refuse_an_exact_skip(kind, start, end, limit):
    with pytest.raises(ValueError, match=limit):
        projected(result_of(span(kind, start, end)))


def test_a_result_that_is_not_complete_and_current_sends_nothing():
    with pytest.raises(ValueError, match="not complete and current"):
        projected(replace(result_of(span("intro", 10_000, 20_000)), freshness="stale"))


# ── automatic sending (pure) ──────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("status", ["approved", "proposed", "rejected"])
@pytest.mark.parametrize("kind", ["intro", "credits"])
def test_automatic_sending_leaves_an_episode_with_an_exact_skip_to_the_owner(kind, status):
    result = pure_result(span(kind, 5_000, 40_000, review_status=status), piece("a"))

    assert sure_runs(result, frozenset(), ()) == ({}, [SET_BY_OWNER])


# ── the report (pure) ─────────────────────────────────────────────────────────────────────────────────────────────


def test_a_kind_with_an_exact_skip_is_not_compared_with_plex_at_all():
    ranges = (
        span("intro", 100_000, 130_000),
        rng("intro", 120_000, 125_000),
        rng("intro", 90_000, 95_000, origin="plex", suggested_by=("Plex",)),
        rng("intro", 110_000, 112_000, action="keep"),
    )

    found = flags_of(plex=[marker("intro", 60_000, 140_000)], ranges=ranges)

    assert list(found) == [("owner_span", "intro")]
    flag = found[("owner_span", "intro")]
    assert flag["severity"] == "info"
    assert flag["text"] == "Set by you: the intro skip (1:40 to 2:10)"


def test_a_plex_marker_for_a_kind_the_owner_set_is_not_missing_or_early_or_late():
    ranges = (span("intro", 100_000, 130_000),)

    assert list(flags_of(plex=[], ranges=ranges)) == [("owner_span", "intro")]
    assert list(flags_of(plex=[marker("intro", 130_000, 400_000)], ranges=ranges)) == [("owner_span", "intro")]


def test_the_other_kind_is_still_compared_with_plex():
    ranges = (span("intro", 100_000, 130_000), rng("credits", 1_200_000, 1_280_000))

    found = flags_of(plex=[marker("intro", 60_000, 140_000), marker("credits", 1_150_000, 1_290_000)], ranges=ranges)

    assert set(found) == {("owner_span", "intro"), ("plex_starts_early", "credits"), ("plex_ends_late", "credits")}


def test_plex_cutting_into_a_scene_the_owner_protected_is_still_reported_beside_an_exact_skip():
    keep = rng("intro", 135_000, 150_000, action="keep", origin="manual")

    found = flags_of(plex=[marker("intro", 100_000, 145_000)], ranges=(span("intro", 100_000, 130_000), keep))

    assert set(found) == {("owner_span", "intro"), ("plex_cuts_into_keep", "intro")}


def test_an_exact_skip_that_was_turned_down_is_not_reported():
    ranges = (span("intro", 100_000, 130_000, review_status="rejected"), rng("intro", 100_000, 130_000))

    assert flags_of(plex=[marker("intro", 100_000, 130_000)], ranges=ranges) == {}


def test_the_flag_is_information_and_comes_before_what_was_sent():
    flags = classify(
        status="complete",
        freshness="current",
        stale=None,
        plex=[],
        ranges=(span("intro", 100_000, 130_000),),
        duration_ms=DURATION,
        published=True,
        automatic=False,
    )

    assert [(f["code"], f["severity"]) for f in flags] == [("owner_span", "info"), ("published", "good")]


# ── setting it (the real store) ───────────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def lab(tmp_path, monkeypatch):
    app = Lab(tmp_path, monkeypatch)
    yield app
    if not app.marker._closed:
        app.marker.close()


def owner_spans(lab, ep):
    return [r for r in lab.store.get_result(ep.vid).ranges if is_owner_span(r)]


def eligible_now(lab, ep):
    return eligible(lab.store.get_result(ep.vid), ep.audio)


def send(lab, ep):
    """What Send does: prepare the change for the episode as it is, then apply it."""
    change = lab.publisher.prepare(ep.vid, lab.revision(ep), ep.identity, "owner")
    lab.publisher.apply(change["id"], "owner")
    return change


def refused_send(lab, ep):
    """The sentence Send refuses with; Plex and the journal are exactly as they were."""
    before, journal = lab.rows(), lab.publisher.list_changes(ep.vid)
    with pytest.raises(PublicationRefused) as refused:
        lab.publisher.prepare(ep.vid, lab.revision(ep), ep.identity, "owner")
    assert lab.rows() == before
    assert lab.publisher.list_changes(ep.vid) == journal
    return str(refused.value)


def episode(lab, *parts):
    ep = lab.episode(1)
    lab.analyse(ep, *(part(ep) for part in parts))
    return ep


def intro_found(ep):
    return confirmed(ep, "intro", 10_000, 20_000)


def test_an_exact_skip_is_saved_approved_and_locked_with_a_reason_of_its_own(lab):
    ep = episode(lab, intro_found)
    before = lab.revision(ep)

    result = lab.owner_review(ep, [set_span("intro", 8_000, 35_000)])

    [owned] = owner_spans(lab, ep)
    assert (owned.kind, owned.action, owned.start_ms, owned.end_ms) == ("intro", "skip", 8_000, 35_000)
    assert (owned.review_status, owned.locked, owned.origin) == ("approved", True, "owner")
    assert (owned.reason_code, owned.reason) == ("owner_span", "Set by you")
    assert owned.audio_stream_ids == (ep.audio,)
    assert result.result_revision == before + 1
    assert owned.id in lab.store.owner_decided_ids(ep.vid)
    assert lab.ranges(ep)[INTRO].review_status == "proposed"  # what the analysis found is left as it was
    assert eligible_now(lab, ep) == [("intro", 8_000, 35_000)]


@pytest.mark.parametrize(
    "extra", [{}, {"action": "skip"}, {"locked": True}, {"action": "skip", "review_status": "approved", "locked": True}]
)
def test_the_edit_may_state_what_an_exact_skip_always_is(lab, extra):
    ep = episode(lab, intro_found)

    lab.owner_review(ep, [{**set_span("intro", 8_000, 35_000), **extra}])

    assert eligible_now(lab, ep) == [("intro", 8_000, 35_000)]


@pytest.mark.parametrize(
    "extra",
    [
        {"review_status": "rejected"},
        {"review_status": "proposed"},
        {"locked": False},
        {"action": "keep"},
        {"reason": "my own words"},
        {"terminal": True},
    ],
)
def test_the_edit_cannot_make_an_exact_skip_anything_else(lab, extra):
    ep = episode(lab, intro_found)
    before = lab.store.get_result(ep.vid)

    with pytest.raises(ReviewRefused) as refused:
        lab.owner_review(ep, [{**set_span("intro", 8_000, 35_000), **extra}])

    assert str(refused.value) == ALWAYS_APPROVED
    assert lab.store.get_result(ep.vid) == before


def test_an_exact_skip_may_start_before_and_end_after_what_was_confirmed_and_cover_what_was_not(lab):
    ep = episode(
        lab,
        intro_found,
        lambda ep: suggestion(ep, "intro", 22_000, 24_000),
        lambda ep: detector_keep(ep, "intro", 26_000, 28_000),
    )

    lab.owner_review(ep, [set_span("intro", 8_000, 35_000)])

    assert eligible_now(lab, ep) == [("intro", 8_000, 35_000)]
    send(lab, ep)
    assert _marker_times(lab.plex, "intro") == [(8_000, 35_000)]


def test_a_scene_the_owner_protected_stops_an_exact_skip_and_nothing_is_saved(lab):
    ep = episode(lab, intro_found)
    lab.owner_review(ep, [{"kind": "intro", "action": "keep", "start_ms": 22_000, "end_ms": 26_000}])
    before = lab.store.get_result(ep.vid)

    with pytest.raises(ReviewRefused) as refused:
        lab.owner_review(ep, [set_span("intro", 8_000, 35_000)])

    assert str(refused.value) == (
        "The intro skip you set would cover a scene you protected (from 0:22 to 0:26). "
        "Change the skip, or remove that first."
    )
    assert lab.store.get_result(ep.vid) == before
    assert owner_spans(lab, ep) == []


def test_a_scene_to_protect_cannot_be_added_over_an_exact_skip_either(lab):
    ep = episode(lab, intro_found)
    lab.owner_review(ep, [set_span("intro", 8_000, 35_000)])
    before = lab.store.get_result(ep.vid)

    with pytest.raises(ReviewRefused, match="would cover a scene you protected"):
        lab.owner_review(ep, [{"kind": "intro", "action": "keep", "start_ms": 22_000, "end_ms": 26_000}])

    assert lab.store.get_result(ep.vid) == before


def test_a_locked_skip_of_the_other_kind_stops_it_but_one_of_the_same_kind_is_replaced(lab):
    ep = episode(lab, intro_found, lambda ep: confirmed(ep, "credits", 30_000, 40_000))
    lab.owner_review(ep, [{"range_id": "credits-30000", "locked": True}])

    with pytest.raises(ReviewRefused, match="a skip you set or locked"):
        lab.owner_review(ep, [set_span("intro", 8_000, 35_000)])

    lab.owner_review(ep, [{"range_id": INTRO, "locked": True}])
    lab.owner_review(ep, [set_span("intro", 8_000, 25_000)])
    assert eligible_now(lab, ep) == [("intro", 8_000, 25_000)]


def test_the_two_exact_skips_cannot_overlap(lab):
    ep = episode(lab, intro_found)
    lab.owner_review(ep, [set_span("intro", 8_000, 35_000)])

    with pytest.raises(ReviewRefused, match="would cover a skip you set or locked"):
        lab.owner_review(ep, [set_span("credits", 30_000, 50_000)])

    lab.owner_review(ep, [set_span("credits", 35_000, 50_000)])  # touching is fine
    assert eligible_now(lab, ep) == [("intro", 8_000, 35_000), ("credits", 35_000, 50_000)]


@pytest.mark.parametrize(
    ("kind", "start", "end", "sentence"),
    [
        ("intro", 90_000, 98_000, LAST_TWO_SECONDS),
        ("intro", 10_000, 99_000, LAST_TWO_SECONDS),
        ("credits", 1_999, 50_000, CREDITS_START),
        ("intro", 10_000, 100_001, OUTSIDE_THE_FILE),
        ("intro", 20_000, 20_000, OUTSIDE_THE_FILE),
        ("intro", 30_000, 20_000, OUTSIDE_THE_FILE),
        ("intro", -1, 5_000, OUTSIDE_THE_FILE),
        ("recap", 10_000, 20_000, NO_KIND_OR_TIMES),
        ("intro", 10_000.5, 20_000, NO_KIND_OR_TIMES),
        ("intro", "10000", 20_000, NO_KIND_OR_TIMES),
        ("intro", True, 20_000, NO_KIND_OR_TIMES),
        ("intro", None, 20_000, NO_KIND_OR_TIMES),
        (None, 10_000, 20_000, NO_KIND_OR_TIMES),
    ],
)
def test_an_exact_skip_must_be_a_real_skip_inside_what_plex_can_hold(lab, kind, start, end, sentence):
    ep = episode(lab, intro_found)
    before = lab.store.get_result(ep.vid)

    with pytest.raises(ReviewRefused) as refused:
        lab.owner_review(ep, [set_span(kind, start, end)])

    assert str(refused.value) == sentence
    assert lab.store.get_result(ep.vid) == before


@pytest.mark.parametrize(("kind", "start", "end"), [("intro", 0, 97_999), ("credits", 2_000, 97_999)])
def test_the_edges_plex_can_hold_are_allowed(lab, kind, start, end):
    ep = episode(lab, intro_found)

    lab.owner_review(ep, [set_span(kind, start, end)])

    assert eligible_now(lab, ep) == [(kind, start, end)]


def test_an_exact_skip_needs_a_current_complete_analysis(lab):
    ep = episode(lab, intro_found)
    revision = lab.revision(ep)
    ep.path.write_bytes(ep.path.read_bytes() + b" changed after the analysis")

    with pytest.raises(RevisionConflict):
        lab.store.save_review(ep.vid, revision, [set_span("intro", 8_000, 35_000)], "owner")

    assert owner_spans(lab, ep) == []


def test_there_is_one_exact_skip_per_kind_and_a_new_one_replaces_the_old(lab):
    ep = episode(lab, intro_found)
    with pytest.raises(ReviewRefused) as refused:
        lab.owner_review(ep, [set_span("intro", 8_000, 25_000), set_span("intro", 30_000, 40_000)])
    assert str(refused.value) == TWO_OF_A_KIND
    assert owner_spans(lab, ep) == []

    lab.owner_review(ep, [set_span("intro", 8_000, 25_000), set_span("credits", 80_000, 95_000)])
    first = {r.kind: r for r in owner_spans(lab, ep)}
    lab.owner_review(ep, [set_span("intro", 9_000, 30_000)])
    now = {r.kind: r for r in owner_spans(lab, ep)}

    assert sorted(now) == ["credits", "intro"]
    assert (now["intro"].start_ms, now["intro"].end_ms) == (9_000, 30_000)
    assert now["intro"].id != first["intro"].id
    assert now["credits"] == first["credits"]
    decided = lab.store.owner_decided_ids(ep.vid)
    assert now["intro"].id in decided and now["credits"].id in decided
    assert first["intro"].id not in decided


def test_moving_an_exact_skip_needs_the_yes_again_and_leaving_it_alone_does_not(lab):
    ep = episode(lab, intro_found)
    lab.owner_review(ep, [set_span("intro", 8_000, 25_000)])
    [owned] = owner_spans(lab, ep)
    same = {"range_id": owned.id, "start_ms": 8_000, "end_ms": 25_000, "review_status": "approved"}

    lab.owner_review(ep, [same])
    assert lab.ranges(ep)[owned.id] == owned

    with pytest.raises(ReviewRefused) as refused:
        lab.owner_review(ep, [{**same, "end_ms": 26_000}])
    assert str(refused.value) == NEEDS_OWNER_SPAN_ACKNOWLEDGMENT

    lab.owner_review(ep, [{**same, "end_ms": 26_000, "acknowledge_owner_span": True}])
    assert lab.ranges(ep)[owned.id] == replace(owned, end_ms=26_000)


@pytest.mark.parametrize("ack", [False, "true", 1, "yes", None])
def test_a_new_exact_skip_needs_a_deliberate_yes(lab, ack):
    ep = episode(lab, intro_found)
    edit = {**set_span("intro", 8_000, 35_000), "action": "skip"}
    if ack is None:
        del edit["acknowledge_owner_span"]
    else:
        edit["acknowledge_owner_span"] = ack
    before = lab.store.get_result(ep.vid)

    with pytest.raises(ReviewRefused) as refused:
        lab.owner_review(ep, [edit])

    assert str(refused.value) == NEEDS_OWNER_SPAN_ACKNOWLEDGMENT
    assert lab.store.get_result(ep.vid) == before


def test_a_new_range_that_does_not_say_it_is_an_exact_skip_is_still_a_scene_to_protect(lab):
    ep = episode(lab, intro_found)

    lab.owner_review(ep, [{"kind": "intro", "start_ms": 22_000, "end_ms": 26_000}])  # no action: a scene to protect

    [keep] = [r for r in lab.store.get_result(ep.vid).ranges if r.action == "keep"]
    assert (keep.origin, keep.start_ms, keep.end_ms) == ("manual", 22_000, 26_000)
    with pytest.raises(ValueError, match="new manual ranges must protect content"):
        lab.owner_review(ep, [{"kind": "intro", "action": "hide", "start_ms": 1_000, "end_ms": 2_000}])


def test_rejecting_an_exact_skip_takes_it_out_of_use_and_it_stays_a_skip(lab):
    ep = episode(lab, intro_found)
    lab.owner_review(ep, [set_span("intro", 8_000, 35_000)])
    [owned] = owner_spans(lab, ep)

    lab.owner_review(ep, [{"range_id": owned.id, "review_status": "rejected"}])

    assert eligible_now(lab, ep) == []
    assert lab.ranges(ep)[owned.id].review_status == "rejected"
    assert owner_spans(lab, ep) != []
    with pytest.raises(ReviewRefused) as refused:
        lab.owner_review(ep, [{"range_id": owned.id, "review_status": "approved"}])  # approving again needs the yes
    assert str(refused.value) == NEEDS_OWNER_SPAN_ACKNOWLEDGMENT
    with pytest.raises(ReviewRefused) as refused:
        lab.owner_review(ep, [{"range_id": owned.id, "action": "keep"}])
    assert str(refused.value) == STAYS_A_SKIP


def test_approving_other_things_in_the_same_review_leaves_the_exact_skip_alone(lab):
    ep = episode(lab, intro_found, lambda ep: confirmed(ep, "credits", 80_000, 90_000))
    lab.owner_review(ep, [set_span("intro", 8_000, 35_000)])

    lab.owner_review(ep, [{"range_id": CREDITS, "review_status": "approved"}])

    assert eligible_now(lab, ep) == [("intro", 8_000, 35_000), ("credits", 80_000, 90_000)]


def test_the_library_has_nothing_left_to_review_in_a_kind_the_owner_set(lab):
    ep = episode(lab, intro_found, lambda ep: confirmed(ep, "credits", 80_000, 90_000))

    def counts():
        [item] = [i for i in lab.service.list_media()["items"] if i["id"] == ep.vid]
        return item["ranges"]

    assert (counts()["to_review"], counts()["approved"]) == (2, 0)

    lab.owner_review(ep, [set_span("intro", 8_000, 35_000)])

    assert (counts()["to_review"], counts()["approved"]) == (1, 1)


# ── sending it (the real publisher on a real Plex database) ───────────────────────────────────────────────────────


def test_a_skip_over_an_edited_plex_marker_replaces_it_exactly(lab):
    ep = lab.episode(1)
    _seed_marker(lab.plex, "intro", 10_000, 20_000, 0)
    lab.analyse(ep, suggestion(ep, "intro", 10_000, 20_000))
    lab.owner_review(ep, [set_span("intro", 10_000, 22_500)])

    send(lab, ep)

    assert _marker_times(lab.plex, "intro") == [(10_000, 22_500)]
    assert lab.ranges(ep)["plex-intro"].review_status == "proposed"  # Plex's suggestion is left as it was


def test_a_skip_identical_to_plexs_marker_has_nothing_to_send(lab):
    ep = lab.episode(1)
    _seed_marker(lab.plex, "intro", 10_000, 20_000, 0)
    lab.analyse(ep, suggestion(ep, "intro", 10_000, 20_000))
    lab.owner_review(ep, [set_span("intro", 10_000, 20_000)])

    assert refused_send(lab, ep) == NOTHING_TO_SEND
    assert lab.backups.calls == []


def test_the_skip_is_compared_with_the_marker_plex_holds_now_not_the_one_the_analysis_saw(lab):
    ep = lab.episode(1)
    _seed_marker(lab.plex, "intro", 10_000, 22_000, 0)  # edited in Plex since the analysis saw 10 s to 20 s
    lab.analyse(ep, suggestion(ep, "intro", 10_000, 20_000))
    lab.owner_review(ep, [set_span("intro", 10_000, 20_000)])

    send(lab, ep)

    assert _marker_times(lab.plex, "intro") == [(10_000, 20_000)]


def test_a_marker_the_analysis_never_saw_still_counts_as_what_plex_has(lab):
    ep = lab.episode(1)
    _seed_marker(lab.plex, "intro", 10_000, 20_000, 0)
    lab.analyse(ep, confirmed(ep, "intro", 30_000, 40_000))  # a re-analysis cut the suggestion away
    lab.owner_review(ep, [set_span("intro", 10_000, 20_000)])

    assert refused_send(lab, ep) == NOTHING_TO_SEND


def test_a_skip_that_is_already_in_plex_after_a_send_has_nothing_more_to_send(lab):
    ep = episode(lab, intro_found)
    lab.owner_review(ep, [set_span("intro", 8_000, 35_000)])
    send(lab, ep)

    assert refused_send(lab, ep) == NOTHING_TO_SEND


def test_credits_are_compared_with_plex_as_a_viewer_sees_them(lab):
    ep = lab.episode(1)
    _seed_marker(lab.plex, "credits", 80_000, 95_000, 0)  # Plex stores credits a little shifted from what it serves
    lab.analyse(ep, suggestion(ep, "credits", 80_000, 95_000))
    lab.owner_review(ep, [set_span("credits", 80_000, 95_000)])
    assert refused_send(lab, ep) == NOTHING_TO_SEND

    lab.owner_review(ep, [set_span("credits", 80_000, 96_000)])
    send(lab, ep)

    assert len(_marker_rows(lab.plex, "credits")) == 1
    assert refused_send(lab, ep) == NOTHING_TO_SEND


def test_a_kind_identical_to_plex_is_left_alone_when_the_other_kind_is_sent(lab):
    ep = lab.episode(1)
    _seed_marker(lab.plex, "intro", 10_000, 20_000, 1)  # Plex numbers its rows by text, so credits would come first
    lab.analyse(ep, suggestion(ep, "intro", 10_000, 20_000), confirmed(ep, "credits", 80_000, 90_000))
    lab.owner_review(ep, [set_span("intro", 10_000, 20_000), {"range_id": CREDITS, "review_status": "approved"}])
    intro = _marker_rows(lab.plex, "intro")

    send(lab, ep)

    assert _marker_rows(lab.plex, "intro") == intro
    assert len(_marker_rows(lab.plex, "credits")) == 1


def test_both_exact_skips_are_sent_together_and_replace_everything_in_their_kind(lab):
    ep = lab.episode(1)
    _seed_marker(lab.plex, "credits", 80_000, 95_000, 0)
    _seed_marker(lab.plex, "intro", 10_000, 20_000, 1)
    lab.analyse(ep, suggestion(ep, "intro", 10_000, 20_000), confirmed(ep, "credits", 80_000, 90_000))
    lab.owner_review(ep, [set_span("intro", 5_000, 30_000), set_span("credits", 75_000, 96_000)])

    change = send(lab, ep)

    assert _marker_times(lab.plex, "intro") == [(5_000, 30_000)]
    assert len(_marker_rows(lab.plex, "credits")) == 1
    assert [(c["id"], c["state"]) for c in lab.publisher.list_changes(ep.vid)] == [(change["id"], "applied")]


def test_sending_an_exact_skip_can_be_undone_row_for_row(lab):
    ep = lab.episode(1)
    _seed_marker(lab.plex, "intro", 10_000, 20_000, 0)
    lab.analyse(ep, suggestion(ep, "intro", 10_000, 20_000))
    before = lab.rows()
    lab.owner_review(ep, [set_span("intro", 8_000, 25_000)])
    change = send(lab, ep)
    assert lab.rows() != before

    lab.publisher.undo(change["id"], "owner")

    assert lab.rows() == before
    assert [c["state"] for c in lab.publisher.list_changes(ep.vid)] == ["undone"]


def test_an_exact_skip_set_where_plex_had_no_marker_is_removed_again_by_undo(lab):
    ep = episode(lab, intro_found)
    before = lab.rows()
    lab.owner_review(ep, [set_span("intro", 8_000, 35_000)])
    change = send(lab, ep)

    lab.publisher.undo(change["id"], "owner")

    assert lab.rows() == before


def test_a_second_exact_skip_replaces_the_first_in_plex_and_undo_brings_the_first_back(lab):
    ep = episode(lab, intro_found)
    lab.owner_review(ep, [set_span("intro", 8_000, 35_000)])
    send(lab, ep)
    first = lab.rows()
    lab.owner_review(ep, [set_span("intro", 9_000, 30_000)])

    second = send(lab, ep)

    assert _marker_times(lab.plex, "intro") == [(9_000, 30_000)]
    lab.publisher.undo(second["id"], "owner")
    assert lab.rows() == first


# ── automatic sending (the real sender) ───────────────────────────────────────────────────────────────────────────


def test_automatic_sending_never_touches_an_episode_with_an_exact_skip(lab):
    ep = episode(lab, intro_found, lambda ep: confirmed(ep, "credits", 80_000, 90_000))
    lab.owner_review(ep, [set_span("credits", 75_000, 95_000)])
    before = lab.rows()

    assert lab.sweep(T0) == 0

    assert_declined(lab, ep, before, SET_BY_OWNER)
    assert [(r.kind, r.start_ms, r.end_ms) for r in owner_spans(lab, ep)] == [("credits", 75_000, 95_000)]
    assert lab.ranges(ep)[INTRO].review_status == "proposed"  # nothing was approved in the owner's name either


def test_automatic_sending_never_overwrites_a_skip_the_owner_sent(lab):
    ep = episode(lab, intro_found)
    lab.owner_review(ep, [set_span("intro", 8_000, 35_000)])
    send(lab, ep)
    written = lab.rows()
    lab.analyse(ep, confirmed(ep, "intro", 40_000, 50_000))  # a new analysis finds an intro somewhere else

    assert lab.sweep(T0) == 0

    assert lab.rows() == written
    assert [(c["state"], c["actor"]) for c in lab.publisher.list_changes(ep.vid)] == [("applied", "owner")]
    assert lab.attempts(ep)[-1]["detail"] == ALREADY_SENT


# ── analysing again ───────────────────────────────────────────────────────────────────────────────────────────────


def test_an_exact_skip_survives_a_new_analysis_and_wins_over_what_it_finds(lab):
    ep = episode(lab, intro_found)
    lab.owner_review(ep, [set_span("intro", 15_000, 30_000)])
    [owned] = owner_spans(lab, ep)

    lab.analyse(ep, confirmed(ep, "intro", 12_000, 25_000), confirmed(ep, "credits", 80_000, 90_000))

    now = lab.ranges(ep)
    assert now[owned.id] == owned  # the same skip: id, times, approved, locked
    found = {(r.kind, r.start_ms, r.end_ms) for r in now.values() if r.origin == "detector"}
    assert found == {("intro", 12_000, 15_000), ("credits", 80_000, 90_000)}  # the new intro is cut back around it
    assert eligible_now(lab, ep) == [("intro", 15_000, 30_000)]


def test_an_exact_skip_survives_an_analysis_that_finds_the_same_things_again(lab):
    ep = episode(lab, intro_found)
    lab.owner_review(ep, [set_span("intro", 15_000, 30_000)])
    [owned] = owner_spans(lab, ep)
    revision = lab.revision(ep)

    assert lab.analyse(ep, intro_found(ep)) == revision

    assert lab.ranges(ep)[owned.id] == owned


def test_an_analysis_that_fails_leaves_the_exact_skip_approved_and_takes_the_approval_of_the_rest(lab):
    ep = episode(lab, intro_found, lambda ep: confirmed(ep, "credits", 80_000, 90_000))
    lab.owner_review(ep, [set_span("intro", 15_000, 30_000), {"range_id": CREDITS, "review_status": "approved"}])
    [owned] = owner_spans(lab, ep)

    run = lab.store.begin_analysis(ep.vid, "job", "signature")
    lab.store.fail_analysis(run, "failed", "Media analysis failed")

    now = lab.ranges(ep)
    assert now[owned.id] == owned
    assert now[CREDITS].review_status == "proposed"
    assert eligible_now(lab, ep) == []  # nothing is eligible, or sent, while the analysis is not complete

    lab.analyse(ep, intro_found(ep), confirmed(ep, "credits", 80_000, 90_000))
    assert eligible_now(lab, ep) == [("intro", 15_000, 30_000)]


# ── the report (the real store) ───────────────────────────────────────────────────────────────────────────────────


def test_the_report_names_the_skip_the_owner_set_in_the_row_and_the_text(season):  # noqa: F811
    version = season.add(1, ranges=[rng("intro", 100_000, 130_000)], plex_markers=[("intro", 60_000, 140_000)])
    revision = season.store.get_result(version.id).result_revision
    season.store.save_review(version.id, revision, [set_span("intro", 60_000, 140_000)], "owner")

    report = get(season, show="Community", season="3").get_json()

    [row] = report["episodes"]
    assert [(f["code"], f["kind"]) for f in row["flags"]] == [("owner_span", "intro")]
    assert row["verdict"] == "ok"
    assert row["summary"] == "Set by you: the intro skip (1:00 to 2:20)."
    assert [(s["kind"], s["start_ms"], s["end_ms"], s["origin"]) for s in row["reprise"]["owner_spans"]] == [
        ("intro", 60_000, 140_000, "owner")
    ]
    assert row["reprise"]["skips"] == [] and row["reprise"]["approved"] == 1 and row["reprise"]["to_review"] == 0
    assert report["summary"]["counts"]["owner_span"] == 1
    text = get(season, show="Community", season="3", format="text").get_data(as_text=True)
    assert "SET_BY_YOU(i)" in text and "O i1:00-2:20" in text and ", O skip you set" in text


# ── the routes ────────────────────────────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def client(lab, monkeypatch):
    monkeypatch.setattr("media_preview_generator.web.auth.is_auth_external", lambda: True)
    app = Flask(__name__)
    app.secret_key = "throwaway"
    app.register_blueprint(create_reprise_blueprint(lab.service))
    return app.test_client()


def patch(client, lab, ep, edits):
    return client.patch(
        f"/api/reprise/media/{ep.vid}/review", json={"expected_revision": lab.revision(ep), "edits": edits}
    )


def prepare(client, lab, ep):
    body = {"media_version_id": ep.vid, "expected_revision": lab.revision(ep), "identity": asdict(ep.identity)}
    return client.post("/api/reprise/publications/prepare", json=body)


def test_the_review_route_takes_an_exact_skip_and_lookups_then_offer_it_whole(client, lab):
    ep = episode(lab, intro_found)

    answer = patch(client, lab, ep, [set_span("intro", 8_000, 35_000)])

    assert answer.status_code == 200
    [owned] = [r for r in answer.get_json()["ranges"] if r["origin"] == "owner"]
    assert (owned["action"], owned["review_status"], owned["locked"], owned["reason"]) == (
        "skip",
        "approved",
        True,
        "Set by you",
    )
    served = client.get(f"/api/reprise/media/{ep.vid}/result").get_json()
    assert [(r["kind"], r["start_ms"], r["end_ms"]) for r in served["eligible_skip_ranges"]] == [
        ("intro", 8_000, 35_000)
    ]


def test_the_review_route_takes_the_short_form_of_the_edit_too(client, lab):
    ep = episode(lab, intro_found)
    short = {"range_id": None, "kind": "credits", "start_ms": 80_000, "end_ms": 95_000, "acknowledge_owner_span": True}

    assert patch(client, lab, ep, [short]).status_code == 200

    assert eligible_now(lab, ep) == [("credits", 80_000, 95_000)]


@pytest.mark.parametrize("ack", [None, False, "true", 1])
def test_the_review_route_asks_for_the_yes_in_a_plain_sentence(client, lab, ack):
    ep = episode(lab, intro_found)
    edit = set_span("intro", 8_000, 35_000)
    del edit["acknowledge_owner_span"]
    if ack is not None:
        edit["acknowledge_owner_span"] = ack
    before = lab.store.get_result(ep.vid)

    answer = patch(client, lab, ep, [edit])

    assert answer.status_code == 400
    assert answer.get_json() == {"error": NEEDS_OWNER_SPAN_ACKNOWLEDGMENT}
    assert lab.store.get_result(ep.vid) == before


def test_the_review_route_says_when_the_skip_cannot_be_what_it_asked_for(client, lab):
    ep = episode(lab, intro_found)

    contradiction = patch(client, lab, ep, [set_span("intro", 8_000, 35_000, review_status="rejected")])
    too_late = patch(client, lab, ep, [set_span("intro", 8_000, 99_000)])

    assert (contradiction.status_code, contradiction.get_json()) == (400, {"error": ALWAYS_APPROVED})
    assert (too_late.status_code, too_late.get_json()) == (400, {"error": LAST_TWO_SECONDS})


def test_the_review_route_still_refuses_what_is_not_an_edit(client, lab):
    ep = episode(lab, intro_found)

    for edits in ([{"range_id": None}], [{"range_id": None, "kind": "intro"}], [{"kind": "intro"}], ["intro"], [{}]):
        answer = patch(client, lab, ep, edits)
        assert (answer.status_code, answer.get_json()) == (400, {"error": "Invalid request or operation"}), edits


def test_the_publication_routes_say_nothing_to_publish_in_a_plain_sentence_and_send_what_differs(client, lab):
    ep = lab.episode(1)
    _seed_marker(lab.plex, "intro", 10_000, 20_000, 0)
    lab.analyse(ep, suggestion(ep, "intro", 10_000, 20_000))
    lab.owner_review(ep, [set_span("intro", 10_000, 20_000)])
    before = lab.rows()

    same = prepare(client, lab, ep)

    assert (same.status_code, same.get_json()) == (409, {"error": NOTHING_TO_SEND})
    assert lab.rows() == before

    lab.owner_review(ep, [set_span("intro", 10_000, 22_000)])
    change = prepare(client, lab, ep)
    assert change.status_code == 200
    assert client.post(f"/api/reprise/publications/{change.get_json()['id']}/apply").status_code == 200
    assert _marker_times(lab.plex, "intro") == [(10_000, 22_000)]
