"""The Plex-versus-Reprise report: how each episode is classified, the numbers behind the summary, the HTTP shape and
the size of the plain text. Reprise's side comes from a real store; Plex is a fake that answers like its API does."""

import json
from types import SimpleNamespace

import pytest
from flask import Flask

from media_preview_generator.markers.store import MarkerStore
from media_preview_generator.reprise.analysis import _no_repeat_found
from media_preview_generator.reprise.identity import media_version
from media_preview_generator.reprise.models import DecisionRange, Evidence, PlaybackIdentity
from media_preview_generator.reprise.publishing import ReprisePublisher
from media_preview_generator.reprise.report import (
    PLEX_BATCH,
    TEXT_LIMIT,
    TOLERANCE_MS,
    _comparisons,
    _verdict,
    classify,
    clock,
    render_text,
    seconds_text,
)
from media_preview_generator.reprise.service import RepriseService, _mapping_fingerprint
from media_preview_generator.reprise.store import RepriseStore
from media_preview_generator.reprise.uniqueness import COMPARISON_KEY
from media_preview_generator.reprise.verification_profile import PROFILE_FINGERPRINT, PROFILE_ID
from media_preview_generator.servers.base import Library, ServerConfig, ServerType
from media_preview_generator.servers.registry import ServerRegistry
from media_preview_generator.web.routes.api_reprise import create_reprise_blueprint

DURATION = 1_300_000
METRICS = {"verification_profile": PROFILE_ID, "profile_fingerprint": PROFILE_FINGERPRINT}


def rng(kind, start, end, *, action="skip", origin="detector", review="proposed", terminal=False, suggested_by=()):
    return DecisionRange(
        f"{kind}-{action}-{start}",
        kind,
        action,
        start,
        end,
        review,
        False,
        origin,
        "plex_suggestion" if origin == "plex" else "recurring",
        "",
        (f"{kind}-{action}-{start}-evidence",),
        ("a",),
        terminal,
        suggested_by,
    )


def marker(kind, start, end):
    return {"kind": kind, "start_ms": start, "end_ms": end}


def flags_of(**kwargs):
    base = dict(
        status="complete", freshness="current", stale=None, plex=[], ranges=(), duration_ms=DURATION, published=False
    )
    return {(f["code"], f["kind"]): f for f in classify(**{**base, **kwargs})}


# ── classification ───────────────────────────────────────────────────────────────────────────────────────────────


def test_a_plex_skip_that_matches_reprise_raises_no_flag():
    skip = rng("intro", 100_000, 130_000)
    assert flags_of(plex=[marker("intro", 100_000, 130_000)], ranges=(skip,)) == {}


def test_confirmed_pieces_that_touch_are_compared_as_one_run():
    pieces = (rng("intro", 100_000, 105_000), rng("intro", 105_000, 106_000), rng("intro", 106_000, 130_000))
    assert flags_of(plex=[marker("intro", 100_000, 130_000)], ranges=pieces) == {}


def test_rounding_inside_the_tolerance_is_not_flagged_but_one_second_more_is():
    skip = rng("intro", 100_000, 130_000)
    inside = marker("intro", 100_000 - TOLERANCE_MS, 130_000 + TOLERANCE_MS)
    assert flags_of(plex=[inside], ranges=(skip,)) == {}
    outside = marker("intro", 100_000 - TOLERANCE_MS - 1, 130_000 + TOLERANCE_MS + 1)
    assert {code for code, _ in flags_of(plex=[outside], ranges=(skip,))} == {"plex_starts_early", "plex_ends_late"}


def test_plex_starting_early_and_ending_late_name_the_seconds_and_the_kind():
    skip = rng("credits", 1_200_000, 1_280_000)
    found = flags_of(plex=[marker("credits", 1_148_000, 1_287_500)], ranges=(skip,))
    early, late = found[("plex_starts_early", "credits")], found[("plex_ends_late", "credits")]
    assert (early["seconds"], late["seconds"]) == (52.0, 7.5)
    assert early["text"] == "Skips 52 s of story before the credits"
    assert late["text"] == "Skips 7.5 s of story after the credits"
    assert early["severity"] == late["severity"] == "problem"


def test_plex_later_or_shorter_than_reprise_is_not_story_loss():
    skip = rng("intro", 100_000, 130_000)
    assert flags_of(plex=[marker("intro", 110_000, 125_000)], ranges=(skip,)) == {}


def test_a_credits_marker_that_runs_to_the_container_end_is_not_late():
    skip = rng("credits", 1_200_000, DURATION, terminal=True)
    assert flags_of(plex=[marker("credits", 1_200_000, DURATION + 40_000)], ranges=(skip,)) == {}
    ends_at_sound = rng("credits", 1_200_000, 1_290_000)
    found = flags_of(plex=[marker("credits", 1_200_000, DURATION + 40_000)], ranges=(ends_at_sound,))
    assert found[("plex_ends_late", "credits")]["seconds"] == 10.0  # Plex's end is cut at the file's end first


def test_plex_cutting_into_a_scene_the_owner_protected_reports_the_overlap_once_across_scenes():
    keeps = (
        rng("credits", 1_230_000, 1_240_000, action="keep", origin="manual"),
        rng("credits", 1_236_000, 1_250_000, action="keep", review="approved"),
    )
    found = flags_of(plex=[marker("credits", 1_200_000, 1_245_000)], ranges=keeps)
    cut = found[("plex_cuts_into_keep", "credits")]
    assert cut["seconds"] == 15.0  # 1_230_000..1_245_000: the two scenes overlap, so it is counted once
    assert cut["text"] == "Plex's credits skip cuts 15 s out of a scene you protected"
    assert cut["severity"] == "problem" and ("plex_covers_unconfirmed", "credits") not in found
    assert flags_of(plex=[marker("credits", 1_200_000, 1_230_500)], ranges=keeps) == {}  # half a second: tolerated


def test_a_rejected_protection_is_not_protected():
    keep = rng("credits", 1_230_000, 1_240_000, action="keep", review="rejected")
    assert flags_of(plex=[marker("credits", 1_200_000, 1_245_000)], ranges=(keep,)) == {}


def test_reprise_found_a_skip_plex_does_not_have():
    skip = rng("intro", 100_000, 130_000)
    found = flags_of(
        plex=[marker("credits", 1_200_000, 1_280_000)], ranges=(skip, rng("credits", 1_200_000, 1_280_000))
    )
    assert list(found) == [("plex_missing", "intro")]
    assert found[("plex_missing", "intro")]["text"] == "Plex has no intro skip, Reprise found one"


def test_a_kind_plex_hides_in_the_library_is_not_called_missing():
    skip = rng("intro", 100_000, 130_000)
    assert flags_of(plex=[], ranges=(skip,), unknown_kinds=frozenset({"intro"})) == {}


def test_plex_that_could_not_be_read_flags_nothing_about_plex():
    skip = rng("intro", 100_000, 130_000)
    assert flags_of(plex=None, ranges=(skip,)) == {}


def test_only_suggestions_means_unconfirmed_and_a_plex_suggestion_is_not_a_confirmed_skip():
    suggestion = rng("intro", 100_000, 130_000, origin="plex", suggested_by=("Plex",))
    found = flags_of(plex=[marker("intro", 100_000, 130_000)], ranges=(suggestion,))
    assert list(found) == [("unconfirmed", None)]
    assert found[("unconfirmed", None)]["text"] == "Reprise could not confirm the intro"
    confirmed_too = flags_of(
        plex=[marker("intro", 100_000, 130_000)], ranges=(suggestion, rng("credits", 1_200_000, 1_280_000))
    )
    assert ("unconfirmed", None) not in confirmed_too


def test_a_rejected_range_is_left_out():
    skip = rng("intro", 100_000, 130_000, review="rejected")
    assert flags_of(plex=[], ranges=(skip,)) == {}


@pytest.mark.parametrize(
    ("status", "code", "text"),
    [
        ("missing", "not_analyzed", "Not analyzed yet"),
        ("failed", "failed", "Analysis failed"),
        ("cancelled", "failed", "Analysis was cancelled"),
        ("unsupported", "failed", "Reprise can't analyze this file"),
    ],
)
def test_episodes_without_a_result_are_flagged_and_not_compared(status, code, text):
    found = flags_of(
        status=status,
        freshness="unavailable",
        plex=[marker("intro", 0, 5000)],
        ranges=(rng("intro", 100_000, 130_000),),
    )
    assert list(found) == [(code, None)]
    assert found[(code, None)]["text"] == text


def test_a_running_analysis_raises_no_flag():
    assert flags_of(status="running", freshness="current") == {}


def test_a_stale_result_is_flagged_with_its_own_label_and_never_compared_with_plex():
    stale = {"label": "File changed"}
    found = flags_of(freshness="stale", stale=stale, plex=[], ranges=(rng("intro", 100_000, 130_000),))
    assert list(found) == [("stale", None)]
    assert found[("stale", None)]["text"] == "File changed"


def test_published_is_an_information_flag_that_comes_last():
    skip = rng("intro", 100_000, 130_000)
    flags = classify(
        status="complete",
        freshness="current",
        stale=None,
        plex=[marker("intro", 50_000, 130_000)],
        ranges=(skip,),
        duration_ms=DURATION,
        published=True,
    )
    assert [f["code"] for f in flags] == ["plex_starts_early", "published"]
    assert flags[-1]["severity"] == "good"


def test_seconds_and_clock_read_like_a_player():
    assert [seconds_text(x) for x in (52_000, 7_500, 4_000, 120_400)] == ["52 s", "7.5 s", "4 s", "120 s"]
    assert [clock(x) for x in (0, 61_999, 1_276_500, 3_725_000)] == ["0:00", "1:01", "21:16", "1:02:05"]


# ── a season through the real store, service and HTTP ───────────────────────────────────────────────────────────


class FakePlex:
    """Answers ``/library/metadata/<keys>?includeMarkers=1`` the way Plex does, and records what it was asked."""

    def __init__(self, markers=None, hidden=frozenset(), fail=False):
        self.markers, self.hidden, self.fail, self.requests = markers or {}, hidden, fail, []

    def _plex_http(self, method, path, params=None):
        self.requests.append((method, path, params))
        if self.fail:
            raise RuntimeError("connection refused")
        keys = path.rsplit("/", 1)[1].split(",")
        metadata = [
            {
                "ratingKey": key,
                "Marker": [
                    {"type": kind, "startTimeOffset": start, "endTimeOffset": end, "id": 1}
                    for kind, start, end in self.markers.get(key, [])
                ]
                + [{"type": "commercial", "startTimeOffset": 1, "endTimeOffset": 2}],
            }
            for key in keys
            if key in self.markers or key in self.present
        ]
        return SimpleNamespace(json=lambda: {"MediaContainer": {"Metadata": metadata}})

    present = ()

    def hidden_marker_types(self, library_id):
        return self.hidden


def evidence(r, comparisons=None):
    metrics = {**METRICS, **({COMPARISON_KEY: comparisons[r.kind]} if r.kind in (comparisons or {}) else {})}
    return Evidence(
        r.evidence_ids[0], r.start_ms, r.end_ms, "audiovisual", "unknown", "x", "", None, None, None, metrics
    )


@pytest.fixture
def season(tmp_path, monkeypatch):
    """A show with several episodes in different states: build with ``add``, then use ``client`` and ``plex``."""
    monkeypatch.setattr("media_preview_generator.web.auth.is_auth_external", lambda: True)
    markers_db = MarkerStore(str(tmp_path / "markers.db"))
    store = RepriseStore(markers_db)
    registry = ServerRegistry()
    registry._configs["server"] = ServerConfig(
        id="server",
        type=ServerType.PLEX,
        name="Plex",
        enabled=True,
        url="http://127.0.0.1",
        auth={},
        libraries=[Library("library", "TV", (str(tmp_path),))],
        server_identity="machine",
    )
    config = registry.get_config("server")
    plex = FakePlex()
    registry._servers["server"] = plex
    service = RepriseService(store, registry, None)
    app = Flask(__name__)
    app.secret_key = "throwaway"
    app.register_blueprint(create_reprise_blueprint(service))
    made = {}

    def add(
        number,
        *,
        show="Community",
        season_number=3,
        ranges=None,
        plex_markers=None,
        status="complete",
        present=True,
        comparisons=None,
        owner_keeps=(),
    ):
        path = tmp_path / f"{show}-{season_number}-{number}.mkv"
        path.write_bytes(f"episode {show} {season_number} {number}".encode())
        key = f"{season_number}{number:02d}"
        version = media_version(str(path), DURATION, "{}")
        identity = PlaybackIdentity(
            "machine", key, key + "1", key + "2", f"/library/parts/{key}", path.stat().st_size, DURATION, "a"
        )
        store.register_media(
            version,
            identity,
            server_id="server",
            metadata={
                "library_id": "library",
                "server_url": config.url,
                "remote_part_path": str(path),
                "mapping_fingerprint": _mapping_fingerprint(config, config.libraries[0]),
                "show_title": show,
                "season": season_number,
                "episode": number,
                "title": f"Episode {number}",
            },
        )
        if plex_markers is not None:
            plex.markers[key] = plex_markers
        if present:
            plex.present = (*plex.present, key)
        if status == "complete":
            run = store.begin_analysis(version.id, "job", "signature")
            result = store.finish_analysis(
                run, [evidence(r, comparisons) for r in ranges or ()], list(ranges or ()), version.id, []
            )
            if owner_keeps:  # a keep the owner added: the analysis itself only ever proposes
                edits = [{"kind": k, "action": "keep", "start_ms": s, "end_ms": e} for k, s, e in owner_keeps]
                store.save_review(version.id, result.result_revision, edits, "owner")
        elif status == "failed":
            run = store.begin_analysis(version.id, "job", "signature")
            store.fail_analysis(run, "failed", "Media analysis failed")
        made[number] = version
        return version

    yield SimpleNamespace(
        add=add,
        client=app.test_client(),
        plex=plex,
        store=store,
        service=service,
        versions=made,
        markers=markers_db,
        registry=registry,
    )
    markers_db.close()


def get(fixture, /, **query):
    return fixture.client.get("/api/reprise/report", query_string=query)


def build_community(season):
    intro = rng("intro", 100_000, 130_000)
    credits = rng("credits", 1_200_000, 1_280_000)
    season.add(
        1, ranges=[intro, credits], plex_markers=[("intro", 100_000, 130_000), ("credits", 1_200_000, 1_280_000)]
    )
    season.add(
        2,
        ranges=[intro, credits],
        owner_keeps=[("credits", 1_236_000, 1_250_000)],
        plex_markers=[("intro", 100_000, 130_000), ("credits", 1_200_000, 1_300_000)],
    )
    season.add(3, ranges=[intro, credits], plex_markers=[("intro", 60_000, 130_000)])
    season.add(
        4,
        ranges=[rng("intro", 100_000, 130_000, origin="plex", suggested_by=("Plex",))],
        plex_markers=[("intro", 100_000, 130_000)],
    )
    season.add(5, status="missing", plex_markers=[])
    season.add(6, status="failed", plex_markers=[])
    return season


def test_the_report_has_one_row_per_episode_in_order_and_flags_each_disagreement(season):
    build_community(season)
    answer = get(season, show="Community", season="3")
    assert answer.status_code == 200
    report = answer.get_json()
    assert (report["show"], report["season"], report["plex"]["status"]) == ("Community", 3, "read")
    rows = report["episodes"]
    assert [r["label"] for r in rows] == [f"S3E0{n}" for n in range(1, 7)]
    codes = {r["label"]: [(f["code"], f["kind"], f["seconds"]) for f in r["flags"]] for r in rows}
    assert codes == {
        "S3E01": [],
        "S3E02": [("plex_cuts_into_keep", "credits", 14.0), ("plex_ends_late", "credits", 20.0)],
        "S3E03": [("plex_starts_early", "intro", 40.0), ("plex_missing", "credits", None)],
        "S3E04": [("unconfirmed", None, None)],
        "S3E05": [("not_analyzed", None, None)],
        "S3E06": [("failed", None, None)],
    }
    assert [r["verdict"] for r in rows] == [
        "ok",
        "differs",
        "differs",
        "needs_attention",
        "needs_attention",
        "needs_attention",
    ]
    summary = report["summary"]
    assert summary["episodes"] == 6 and summary["analyzed"] == 4
    assert summary["counts"]["plex_cuts_into_keep"] == 1 and summary["counts"]["published"] == 0
    assert summary["phrases"]["plex_cuts_into_keep"] == "1 episode where Plex skips into a scene you protected"
    assert summary["headline"].startswith("1 episode where Plex skips into a scene you protected; ")
    assert summary["differs"] == 2 and summary["needs_attention"] == 3


def test_a_row_carries_both_sides_and_the_plain_failure_reason(season):
    build_community(season)
    rows = {r["episode"]: r for r in get(season, show="Community", season="3").get_json()["episodes"]}
    two = rows[2]
    assert two["plex"]["markers"] == [marker("intro", 100_000, 130_000), marker("credits", 1_200_000, 1_300_000)]
    assert [(s["kind"], s["start_ms"], s["end_ms"], s["review_status"]) for s in two["reprise"]["skips"]] == [
        ("intro", 100_000, 130_000, "proposed"),
        ("credits", 1_200_000, 1_280_000, "proposed"),
    ]
    assert [(k["start_ms"], k["end_ms"]) for k in two["reprise"]["keeps"]] == [(1_236_000, 1_250_000)]
    assert (two["reprise"]["to_review"], two["analysis"]["status"], two["published"]) == (2, "complete", False)
    four = rows[4]
    assert four["reprise"]["skips"] == [] and four["reprise"]["suggestions"][0]["suggested_by"] == ["Plex"]
    assert rows[6]["analysis"]["reasons"] == ["Reprise couldn't read this file well enough to compare it."]
    assert rows[5]["summary"] == "Not analyzed yet."
    assert rows[1]["summary"] == "Plex and Reprise agree."
    assert rows[2]["summary"].startswith("Plex's credits skip cuts 14 s out of a scene you protected.")


def test_plex_is_asked_in_batches_with_markers_and_never_written(season):
    for number in range(1, PLEX_BATCH + 6):
        season.add(number, ranges=[], plex_markers=[])
    assert get(season, show="Community", season="3").status_code == 200
    assert [(m, p.rsplit("/", 1)[0], q) for m, p, q in season.plex.requests] == [
        ("get", "/library/metadata", {"includeMarkers": "1"})
    ] * 2
    assert [len(p.rsplit("/", 1)[1].split(",")) for _, p, _ in season.plex.requests] == [PLEX_BATCH, 5]


def test_publication_journal_marks_published_until_undone(season):
    version = season.add(1, ranges=[rng("intro", 100_000, 130_000)], plex_markers=[("intro", 100_000, 130_000)])
    other = season.add(2, ranges=[rng("intro", 100_000, 130_000)], plex_markers=[("intro", 100_000, 130_000)])
    undone = season.add(3, ranges=[rng("intro", 100_000, 130_000)], plex_markers=[("intro", 100_000, 130_000)])
    publisher = ReprisePublisher(season.store, season.registry, season.service)
    season.service.publisher = publisher

    def journal(change_id, media_id, state, created, revision):
        with season.store.transaction() as conn:
            conn.execute(
                "INSERT INTO reprise_publication_changes VALUES (?,?,?,?,?,?,?,?)",
                (change_id, media_id, state, "owner", "owner", created, created, json.dumps({"revision": revision})),
            )

    journal("a", version.id, "applied", 10, 4)
    journal("b", undone.id, "applied", 10, 2)
    journal("c", undone.id, "undone", 20, 2)
    journal("d", other.id, "prepared", 10, 1)
    assert publisher.published_versions([version.id, other.id, undone.id]) == {version.id: 4}
    rows = {r["episode"]: r for r in get(season, show="Community", season="3").get_json()["episodes"]}
    assert (rows[1]["published"], rows[1]["published_revision"]) == (True, 4)
    assert [f["code"] for f in rows[1]["flags"]] == ["published"] and rows[1]["flags"][0]["severity"] == "good"
    assert rows[2]["published"] is False and rows[3]["published"] is False
    assert get(season, show="Community", season="3").get_json()["summary"]["counts"]["published"] == 1


def test_plex_down_says_so_and_flags_nothing_about_plex(season):
    season.add(1, ranges=[rng("intro", 100_000, 130_000)], plex_markers=[])
    season.plex.fail = True
    report = get(season, show="Community", season="3").get_json()
    assert report["plex"]["status"] == "unavailable" and report["plex"]["message"] == "Plex could not be reached"
    assert report["episodes"][0]["plex"]["status"] == "unavailable" and report["episodes"][0]["flags"] == []
    assert report["summary"]["headline"].startswith("Plex could not be read")
    assert "P ?" in get(season, show="Community", season="3", format="text").get_data(as_text=True)


def test_a_library_that_hides_intro_markers_does_not_make_intros_missing(season):
    season.add(1, ranges=[rng("intro", 100_000, 130_000)], plex_markers=[])
    season.plex.hidden = frozenset({"intro"})
    report = get(season, show="Community", season="3").get_json()
    assert report["episodes"][0]["flags"] == [] and report["episodes"][0]["plex"]["unknown_kinds"] == ["intro"]


def test_show_and_season_are_found_by_name_and_refusals_are_plain_sentences(season):
    season.add(1, ranges=[], plex_markers=[])
    season.add(1, show="Community Extras", season_number=1, ranges=[], plex_markers=[])
    season.add(2, show="Parks and Recreation", season_number=1, ranges=[], plex_markers=[])
    assert get(season, show="community").get_json()["show"] == "Community"  # exact, any case, beats "Community Extras"
    assert get(season, show="parks").get_json()["show"] == "Parks and Recreation"
    several = get(season, show="comm")
    assert several.status_code == 400 and "More than one show matches 'comm'" in several.get_json()["error"]
    assert get(season, show="Nope").status_code == 400
    assert "has not read a show called 'Nope'" in get(season, show="Nope").get_json()["error"]
    wrong = get(season, show="Community", season="9").get_json()["error"]
    assert wrong == "Reprise has no season 9 of Community. It has: 3."
    for query in (
        {},
        {"show": "Community", "season": "x"},
        {"show": "Community", "format": "xml"},
        {"show": "Community", "extra": "1"},
    ):
        assert get(season, **query).status_code == 400
    assert get(season, show="Community", season="").status_code == 200


def test_only_the_owner_may_read_the_report(season, monkeypatch):
    season.add(1, ranges=[], plex_markers=[])
    secret = season.store.create_client("Player")["secret"]
    denied = season.client.get(
        "/api/reprise/report", query_string={"show": "Community"}, headers={"Authorization": "Bearer " + secret}
    )
    assert denied.status_code == 403
    monkeypatch.setenv("WEB_AUTH_TOKEN", "owner-test-only")
    monkeypatch.setattr("media_preview_generator.web.auth.is_auth_external", lambda: False)
    assert season.client.get("/api/reprise/report", query_string={"show": "Community"}).status_code in (401, 403)


def test_report_response_is_read_only_and_not_cached(season):
    build_community(season)
    before = {n: season.store.get_result(v.id) for n, v in season.versions.items()}
    answer = get(season, show="Community", season="3")
    assert answer.headers["Cache-Control"] == "no-store"
    assert {n: season.store.get_result(v.id) for n, v in season.versions.items()} == before
    assert {method for method, _, _ in season.plex.requests} == {"get"}
    assert season.client.post("/api/reprise/report", query_string={"show": "Community"}).status_code == 405


# ── plain text ───────────────────────────────────────────────────────────────────────────────────────────────────


def test_the_text_report_reads_in_one_call(season):
    build_community(season)
    answer = get(season, show="Community", season="3", format="text")
    assert answer.status_code == 200 and answer.mimetype == "text/plain"
    text = answer.get_data(as_text=True)
    lines = text.splitlines()
    assert lines[0].startswith("Community S3: Plex vs Reprise, 6 episodes (")
    assert lines[1].startswith("1 episode where Plex skips into a scene you protected; ")
    assert lines[3:] == [
        "S3E01 P i1:40-2:10 c20:00-21:20 | R i1:40-2:10 c20:00-21:20 | OK",
        "S3E02 P i1:40-2:10 c20:00-21:40 | R i1:40-2:10 c20:00-21:20 | K 20:36-20:50 | CUTS_KEEP(c 14s) LATE(c 20s)",
        "S3E03 P i1:00-2:10 | R i1:40-2:10 c20:00-21:20 | EARLY(i 40s) NO_PLEX(c)",
        "S3E04 P i1:40-2:10 | R i1:40-2:10 | UNCONFIRMED",
        "S3E05 P - | R - | NOT_ANALYZED",
        "S3E06 [failed] P - | R - | FAILED",
    ]


def test_the_text_for_a_whole_show_is_one_line_per_season(season):
    build_community(season)
    season.add(1, season_number=2, ranges=[], plex_markers=[])
    lines = get(season, show="Community", format="text").get_data(as_text=True).splitlines()
    assert lines[2:] == [
        "S2: 1 episode; OK",
        "S3: 6 episodes; 1 CUTS_KEEP, 1 EARLY, 1 LATE, 1 NO_PLEX, 1 UNCONFIRMED, 1 FAILED, 1 NOT_ANALYZED",
        "Add season=N for one line per episode.",
    ]


def test_a_full_season_of_worst_case_rows_fits_the_text_limit(season):
    intro, credits = rng("intro", 100_000, 130_000), rng("credits", 1_200_000, 1_280_000)
    keeps = [("credits", 1_236_000 + 1000 * i, 1_237_000 + 1000 * i) for i in range(3)]
    for number in range(1, 25):
        season.add(
            number,
            ranges=[intro, credits],
            owner_keeps=keeps,
            plex_markers=[("intro", 10_000, 190_000), ("credits", 1_100_000, 1_300_000)],
        )
    text = get(season, show="Community", season="3", format="text").get_data(as_text=True)
    assert len(text.encode()) <= TEXT_LIMIT
    assert text.count("\nS3E") == 24 and "more episodes not shown" not in text
    assert "K 20:36-20:39 | CUTS_KEEP(c 3s) EARLY(c 100s) EARLY(i 90s) LATE(c 20s) LATE(i 60s)" in text


def test_the_text_never_exceeds_the_limit_however_many_episodes(season):
    for number in range(1, 80):
        season.add(number, ranges=[rng("intro", 100_000, 130_000)], plex_markers=[("intro", 10_000, 190_000)])
    report = get(season, show="Community", season="3").get_json()
    text = render_text(report)
    assert len(text.encode()) <= TEXT_LIMIT
    assert "more episodes not shown (text limit)." in text


# ── one of a kind, and keeps that are only unconfirmed ──────────────────────────────────────────────────────────


def test_a_keep_the_analysis_made_is_unconfirmed_and_never_called_a_protected_scene():
    keep = rng("intro", 102_000, 111_000, action="keep")  # S3E21: Plex's intro 1:28-1:51, an unconfirmed keep 1:42-1:51
    found = flags_of(plex=[marker("intro", 88_000, 111_000)], ranges=(keep,))
    assert set(found) == {("plex_covers_unconfirmed", "intro")}
    covers = found[("plex_covers_unconfirmed", "intro")]
    assert (covers["severity"], covers["seconds"]) == ("info", 9.0)
    assert covers["text"] == "Plex's intro skip covers 9 s Reprise could not confirm"
    assert _verdict("complete", list(found.values())) == "ok"  # nothing to fix: it is not a disagreement


@pytest.mark.parametrize(
    "keep,code,severity",
    [
        (rng("intro", 102_000, 111_000, action="keep", origin="manual"), "plex_cuts_into_keep", "problem"),
        (rng("intro", 102_000, 111_000, action="keep", review="approved"), "plex_cuts_into_keep", "problem"),
        (rng("intro", 102_000, 111_000, action="keep"), "plex_covers_unconfirmed", "info"),
    ],
)
def test_the_flag_and_its_severity_follow_who_decided_the_keep(keep, code, severity):
    found = flags_of(plex=[marker("intro", 88_000, 111_000)], ranges=(keep,))
    assert {c: f["severity"] for (c, _), f in found.items()} == {code: severity}


def facts(kind="intro", searched=9, sound=0, picture=0, confirmed_ms=0):
    return {
        "kind": kind,
        "episodes_searched": searched,
        "sound_matched": sound,
        "picture_matched": picture,
        "confirmed_ms": confirmed_ms,
    }


SUGGESTION = rng("intro", 100_000, 130_000, origin="plex", suggested_by=("Plex",))


def test_an_intro_that_none_of_nine_episodes_shares_is_one_of_a_kind_and_not_unconfirmed():
    found = flags_of(plex=[], ranges=(SUGGESTION,), comparisons={"intro": facts()})
    assert set(found) == {("one_of_a_kind", "intro")}
    flag = found[("one_of_a_kind", "intro")]
    assert flag["severity"] == "info"
    assert flag["text"] == "The intro is one of a kind: it matched none of 9 other episodes, so nothing is skipped"
    assert _verdict("complete", [flag]) == "ok"


def test_one_of_a_kind_is_said_about_its_own_kind_only():
    credits = rng("credits", 1_200_000, 1_280_000, origin="plex", suggested_by=("Plex",))
    found = flags_of(plex=[], ranges=(SUGGESTION, credits), comparisons={"intro": facts()})
    assert set(found) == {("one_of_a_kind", "intro"), ("unconfirmed", None)}
    assert found[("unconfirmed", None)]["text"] == "Reprise could not confirm the credits"


@pytest.mark.parametrize(
    "found_facts,unique",
    [
        (facts(searched=2), False),  # too few episodes to say that none shares it
        (facts(searched=3), True),
        (facts(searched=9, confirmed_ms=1000), True),  # a second of a 30 s intro: chance, not a shared intro
        (facts(searched=9, confirmed_ms=3100), False),  # more than a sliver is a partial match: still a suggestion
        (facts(searched=9, confirmed_ms=2000), True),  # two seconds of a 30 s intro is still under a tenth of it
        (None, False),  # nothing recorded: could not compare, so not claimed
    ],
)
def test_one_of_a_kind_needs_enough_episodes_and_no_real_match(found_facts, unique):
    comparisons = {"intro": found_facts} if found_facts else {}
    found = flags_of(plex=[], ranges=(SUGGESTION,), comparisons=comparisons)
    assert (("one_of_a_kind", "intro") in found) is unique
    assert (("unconfirmed", None) in found) is not unique


def test_the_words_say_what_matched_and_how_much():
    sliver = flags_of(plex=[], ranges=(SUGGESTION,), comparisons={"intro": facts(confirmed_ms=1000)})
    assert sliver[("one_of_a_kind", "intro")]["text"] == (
        "The intro is one of a kind: only 1 s of it matched any of 9 other episodes, so nothing is skipped"
    )
    own = flags_of(plex=[], ranges=(SUGGESTION,), comparisons={"intro": facts(sound=4)})
    assert (
        "its sound matches 4 of 9 other episodes but its picture does not, so it is its own version"
        in own[("one_of_a_kind", "intro")]["text"]
    )


def test_a_confirmed_skip_of_the_kind_means_it_is_not_one_of_a_kind():
    confirmed = rng("intro", 100_000, 130_000)
    assert flags_of(plex=None, ranges=(confirmed,), comparisons={"intro": facts()}) == {}


@pytest.mark.parametrize("status", ["failed", "missing"])
def test_an_episode_that_was_not_compared_is_never_called_one_of_a_kind(status):
    found = flags_of(status=status, plex=[], ranges=(SUGGESTION,), comparisons={"intro": facts()})
    assert ("one_of_a_kind", "intro") not in found


def test_the_facts_come_from_the_evidence_and_an_old_result_from_its_note():
    recorded = SimpleNamespace(
        evidence=(Evidence("e", 0, 1, "audiovisual", "unknown", "x", "", None, None, None, {COMPARISON_KEY: facts()}),),
        reasons=(),
    )
    assert _comparisons(recorded) == {"intro": facts()}
    old = SimpleNamespace(evidence=(), reasons=(_no_repeat_found(9),))
    assert _comparisons(old) == {"intro": facts(), "credits": facts("credits")}
    assert _comparisons(SimpleNamespace(evidence=(), reasons=("Plex suggested the intro",))) == {}
    assert _comparisons(None) == {}


def test_the_report_says_so_in_the_row_the_summary_and_the_text(season):
    season.add(1, ranges=[SUGGESTION], plex_markers=[("intro", 100_000, 130_000)], comparisons={"intro": facts()})
    season.add(2, ranges=[rng("intro", 102_000, 111_000, action="keep")], plex_markers=[("intro", 88_000, 111_000)])
    report = get(season, show="Community", season="3").get_json()
    one, two = report["episodes"]
    assert [(f["code"], f["severity"]) for f in one["flags"]] == [("one_of_a_kind", "info")]
    assert one["verdict"] == "ok" and one["summary"].startswith("The intro is one of a kind: it matched none of 9")
    assert [(f["code"], f["severity"], f["seconds"]) for f in two["flags"]] == [
        ("plex_covers_unconfirmed", "info", 9.0)
    ]
    assert [k["protected"] for k in two["reprise"]["keeps"]] == [False]
    assert report["summary"]["counts"]["one_of_a_kind"] == 1
    text = get(season, show="Community", season="3", format="text").get_data(as_text=True)
    assert "S3E01 P i1:40-2:10 | R i1:40-2:10 | UNIQUE(i)" in text
    assert "U 1:42-1:51 | COVERS_UNCONFIRMED(i 9s)" in text and "U range Reprise could not confirm" in text


def test_a_sliver_is_at_most_a_tenth_of_the_suggested_range():
    short = rng("intro", 100_000, 110_000, origin="plex", suggested_by=("Plex",))
    for confirmed_ms, unique in ((1000, True), (1500, False)):
        found = flags_of(plex=[], ranges=(short,), comparisons={"intro": facts(confirmed_ms=confirmed_ms)})
        assert (("one_of_a_kind", "intro") in found) is unique
