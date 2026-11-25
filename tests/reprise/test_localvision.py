"""The optional local vision check: where an episode's pictures (compared with its neighbours', then classified by a
model) put the intro and credits, how sure that is, and how it joins a draft as advice.

The model is never called: ``judge`` and ``model_ready`` are replaced with scripted answers, and the pictures are
scripted too (``Scripted``): shared footage is the same picture id in two episodes, everything else is unique.
"""

import io
import json
import zlib

import numpy as np
import pytest
from PIL import Image

from media_preview_generator.reprise import localvision, service
from media_preview_generator.reprise import visionmatch as vm
from media_preview_generator.reprise.analysis import with_local_model
from media_preview_generator.reprise.models import AnalysisDraft, DecisionRange, MediaVersion

DURATION = 1440000
CFG = localvision.Config(True)


def pattern(pid):
    """A picture's thumbnail: random, so two pictures never look alike and one picture always does."""
    if pid is None:
        return np.zeros(vm.THUMB_W * vm.THUMB_H, np.uint8)  # black
    return np.random.default_rng(pid).integers(20, 236, vm.THUMB_W * vm.THUMB_H).astype(np.uint8)


class Scripted:
    """An episode's pictures by time. ``shows[path]`` is ``[(start, end, shared_id_or_None, scene)]``: inside a segment a
    shared id means the picture a second after the segment's start is ``shared_id + seconds`` (the same in any episode
    that shows that footage, wherever it starts); None means pictures of its own. Anything outside the segments is
    story of its own; ``"black"`` as the id is a black card."""

    def __init__(self, shows):
        self.shows, self.scene_of, self.dense_reads = shows, {}, 0

    def _at(self, path, ms):
        for start, end, shared, scene in self.shows[path]:
            if start <= ms < end:
                if shared == "black":
                    return None, scene
                if shared is not None:
                    return shared + (ms - start) // 1000, scene
                return zlib.crc32(f"{path}:{ms // 1000}".encode()), scene
        return zlib.crc32(f"{path}:{ms // 1000}".encode()), "story"

    def _jpeg(self, path, ms):
        pid, scene = self._at(path, ms)
        out = io.BytesIO()
        picture = Image.fromarray(pattern(pid).reshape(vm.THUMB_H, vm.THUMB_W)).resize((320, 180), Image.NEAREST)
        picture.convert("RGB").save(out, "JPEG", quality=90)
        self.scene_of[out.getvalue()] = scene
        return out.getvalue()

    def keyframes(self, path, start_ms, end_ms):
        first = -(-max(0, start_ms) // 2000) * 2000
        return [(t, self._jpeg(path, t)) for t in range(first, end_ms + 1, 2000)]

    def gray(self, path, start_ms, end_ms):
        self.dense_reads += 1
        times = np.arange(start_ms, end_ms + 1, 1000 / vm.FPS)
        return b"".join(pattern(self._at(path, int(t))[0]).tobytes() for t in times)

    def still(self, path, at_ms):
        return self._jpeg(path, at_ms)


EPISODE = localvision.Media("/e.mkv", DURATION)
R1 = localvision.Media("/r1.mkv", 1430000)
R2 = localvision.Media("/r2.mkv", 1450000)
INTRO = (20000, 80000)  # the intro in the episode; it starts elsewhere in each neighbour, with the same footage
CREDITS = (1335000, 1425000)


def season(tail="preview", *, intro_in=("r1", "r2"), credits_in=("r1", "r2")):
    """The episode and two neighbours: a shared intro and shared credits, then ``tail`` (a preview or story of its own)
    after the credits. ``intro_in``/``credits_in`` say which neighbours really show them."""
    shows = {"/e.mkv": [(*INTRO, 1000, "credits"), (*CREDITS, 5000, "credits"), (CREDITS[1], DURATION, None, tail)]}
    shows["/r1.mkv"] = [
        (30000, 90000, 1000, "credits"),
        (1325000, 1415000, 5000, "credits"),
        (1415000, 1430000, None, "preview"),
    ]
    shows["/r2.mkv"] = [
        (10000, 70000, 1000, "credits"),
        (1345000, 1435000, 5000, "credits"),
        (1435000, 1450000, None, "preview"),
    ]
    for name, path in (("r1", "/r1.mkv"), ("r2", "/r2.mkv")):
        if name not in intro_in:
            shows[path] = [s for s in shows[path] if s[2] != 1000]
        if name not in credits_in:
            shows[path] = [s for s in shows[path] if s[2] != 5000]
    return Scripted(shows)


@pytest.fixture
def model(monkeypatch):
    """A loaded model that answers from what the scripted pictures are (``answers`` is the number of questions asked)."""
    state = {"asked": 0, "ready": True, "down": False, "source": None}
    monkeypatch.setattr(localvision, "model_ready", lambda cfg: state["ready"])

    def judge(cfg, jpeg):
        state["asked"] += 1
        return None if state["down"] else (state["source"].scene_of.get(jpeg, "story"), False)

    monkeypatch.setattr(localvision, "judge", judge)
    monkeypatch.setattr(localvision, "_CACHE", localvision.ReadCache())
    return state


def check(model, source, kinds=("intro", "credits"), refs=(R1, R2), **extra):
    model["source"] = source
    anchors = {"intro": [(0, 120000)], "credits": [(DURATION - 300000, DURATION)]}
    return localvision.check(CFG, source, EPISODE, {k: anchors[k] for k in kinds}, references=refs, **extra)


# --- the pictures decide the edges ---------------------------------------------------------------------------------


def test_the_intro_is_the_footage_the_neighbours_show_too_and_its_edges_are_exact(model):
    verdict = check(model, season(), ["intro"])["intro"]
    assert abs(verdict.start_ms - INTRO[0]) <= 500 and abs(verdict.end_ms - INTRO[1]) <= 500
    assert (verdict.method, verdict.confidence, model["asked"]) == ("pictures", localvision.HIGH, 0)


def test_credits_run_over_a_preview_to_the_end_of_the_file_but_never_over_story(model):
    kept = check(model, season("preview"), ["credits"])["credits"]
    assert abs(kept.start_ms - CREDITS[0]) <= 500 and kept.end_ms == DURATION and kept.confidence == localvision.HIGH
    assert model["asked"] > 0 and {scene for _, scene, _ in kept.frames} == {"preview"}
    left = check(model, season("story"), ["credits"])["credits"]
    assert abs(left.start_ms - CREDITS[0]) <= 500 and abs(left.end_ms - CREDITS[1]) <= 500  # the tag is not included


def test_credits_that_the_model_cannot_check_stop_at_the_shared_footage_and_are_not_high(model):
    model["down"] = True
    verdict = check(model, season("preview"), ["credits"])["credits"]
    assert abs(verdict.end_ms - CREDITS[1]) <= 500 and verdict.confidence != localvision.HIGH


def test_an_episode_that_shares_nothing_with_its_neighbours_gets_no_span(model):
    one_of_a_kind = season(intro_in=(), credits_in=())
    assert check(model, one_of_a_kind) == {}


def test_a_stretch_only_one_neighbour_shows_is_still_found_but_is_not_high_confidence(model):
    verdict = check(model, season(intro_in=("r1",)), ["intro"])["intro"]
    assert abs(verdict.start_ms - INTRO[0]) <= 500 and abs(verdict.end_ms - INTRO[1]) <= 500
    assert verdict.confidence == localvision.MEDIUM
    alone = check(model, season(), ["intro"], refs=(R1,))["intro"]
    assert alone.confidence == localvision.MEDIUM


def test_a_longer_run_that_only_one_neighbour_shows_is_not_taken_because_it_may_be_story(model):
    source = season()
    # twelve seconds before the intro that the episode and r2 both show (recap there, story here), and r1 does not
    source.shows["/e.mkv"] = [(8000, 20000, 988, "story"), (*INTRO, 1000, "credits")]
    source.shows["/r2.mkv"] = [(18000, 90000, 988, "story")]
    verdict = check(model, source, ["intro"])["intro"]
    assert abs(verdict.start_ms - INTRO[0]) <= 500 and abs(verdict.end_ms - INTRO[1]) <= 500
    assert verdict.confidence == localvision.MEDIUM and "narrower" in " ".join(verdict.reasons)


def test_chapter_marks_that_agree_are_said_so_and_ones_far_off_stop_it_being_high(model):
    agree = check(model, season(), ["intro"], chapters={"intro": (20500, 79500)})["intro"]
    assert agree.confidence == localvision.HIGH and any("chapter" in reason.lower() for reason in agree.reasons)
    far = check(model, season(), ["intro"], chapters={"intro": (100000, 160000)})["intro"]
    assert far.confidence == localvision.MEDIUM and any("chapter" in reason.lower() for reason in far.reasons)


def test_a_hint_from_another_source_is_corroboration_in_the_reasons(model):
    hints = {"intro": [localvision.Hint(20400, 79800, "Plex")]}
    verdict = check(model, season(), ["intro"], hints=hints)["intro"]
    assert any("Plex" in reason for reason in verdict.reasons)


def test_without_neighbours_the_models_labels_decide_and_it_is_only_a_low_confidence_suggestion(model):
    source = season()
    alone = check(model, source, ["credits"], refs=())["credits"]
    assert alone.method == "labels" and alone.confidence == localvision.LOW
    assert abs(alone.start_ms - CREDITS[0]) <= 4000 and alone.end_ms <= DURATION and alone.frames
    agreed = check(model, source, ["credits"], refs=(), hints={"credits": [localvision.Hint(*CREDITS, "Plex")]})
    assert agreed["credits"].confidence in (localvision.MEDIUM, localvision.LOW)
    assert agreed["credits"].confidence != localvision.HIGH


def test_a_busy_model_or_a_cancelled_check_ends_quietly(model):
    source = season()
    model["ready"] = False
    assert check(model, source) == {}
    assert source.dense_reads == 0
    model["ready"] = True
    assert check(model, source, cancel=lambda: True) == {}


# --- the building blocks ---------------------------------------------------------------------------------------------


def test_the_same_picture_matches_through_a_different_encode_and_other_pictures_do_not():
    a = vm.frames([0, 125, 250], np.stack([pattern(1), pattern(2), pattern(3)]).astype(np.float32))
    noisy = np.clip(np.stack([pattern(1), pattern(2), pattern(9)]).astype(np.float32) + 3, 0, 255)
    b = vm.frames([0, 125, 250], noisy)
    found = vm.alike(a, b)
    assert found.same[0] and found.same[1] and not found.same[2]
    assert vm.hard_cut(a, 1) and not vm.hard_cut(
        vm.frames([0, 125], np.stack([pattern(1), pattern(1)]).astype(np.float32)), 1
    )


# --- the model's answers and settings --------------------------------------------------------------------------------


def test_judge_reads_strict_json_and_retries_once(monkeypatch):
    answers = iter([OSError("busy"), {"choices": [{"message": {"content": '{"scene":"story","overlay_text":true}'}}]}])

    def fake(request, timeout):
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(localvision, "_open", fake)
    assert localvision.judge(localvision.Config(True), b"x") == ("story", True)
    monkeypatch.setattr(localvision, "_open", lambda r, t: {"choices": [{"message": {"content": '{"scene":"cat"}'}}]})
    assert localvision.judge(localvision.Config(True), b"x") is None


def test_only_a_model_that_is_already_loaded_is_used(monkeypatch):
    seen = []

    def fake(request, timeout):
        seen.append(request.full_url)
        return {"running": [{"model": "qwen3-vl-4b", "state": "ready"}, {"model": "other", "state": "ready"}]}

    monkeypatch.setattr(localvision, "_open", fake)
    assert localvision.model_ready(localvision.Config(True, "http://h:9292/v1", "qwen3-vl-4b"))
    assert not localvision.model_ready(localvision.Config(True, "http://h:9292/v1", "gemma4-e4b"))
    assert seen == ["http://h:9292/running"] * 2


@pytest.mark.parametrize(
    "url,model", [("ftp://h", "m"), ("http://h /v1", "m"), ("http://h/v1", ""), ("http://h/v1", "a b")]
)
def test_a_bad_address_or_model_is_refused(url, model):
    with pytest.raises(ValueError):
        localvision.set_config(True, url, model)


# --- joining a draft -------------------------------------------------------------------------------------------------

MEDIA = MediaVersion("m1", "/x.mkv", 1, 1, 1, DURATION, json.dumps({"audio_streams": [{"plex_id": "1", "index": 1}]}))


def rng(kind, start, end, origin, by=(), locked=False, action="skip"):
    return DecisionRange(
        f"{kind}{start}",
        kind,
        action,
        start,
        end,
        "approved" if origin == "owner" else "proposed",
        locked,
        origin,
        "r",
        "t",
        (),
        ("1",),
        False,
        by,
    )


def verdict(kind="credits", start=1335000, end=DURATION, confidence=localvision.HIGH, method="pictures"):
    return localvision.Verdict(
        kind,
        start,
        end,
        ((1330000, "story", False), (1340000, "credits", True)),
        "qwen3-vl-4b",
        confidence,
        ("Two other episodes show the same pictures.",),
        method,
    )


def join(*ranges, verdicts=None):
    draft = AnalysisDraft((), tuple(ranges), "complete", ())
    return with_local_model(draft, MEDIA, verdicts or {"credits": verdict()})


def test_a_new_verdict_is_an_approve_only_suggestion_with_its_confidence_and_reasons_as_evidence():
    out = join()
    [suggestion] = out.ranges
    assert (suggestion.origin, suggestion.review_status, suggestion.locked, suggestion.suggested_by) == (
        "online",
        "proposed",
        False,
        ("Local model",),
    )
    assert suggestion.reason == (
        "Suggested by an AI model that looked at the episode's pictures. "
        "Reprise didn't find it in other episodes. Confidence: high."
    )
    [evidence] = out.evidence
    assert evidence.metrics["local_model"]["confidence"] == "high"
    assert evidence.metrics["local_model"]["reasons"] == ["Two other episodes show the same pictures."]
    assert evidence.metrics["local_model"]["frames"][1] == {"at_ms": 1340000, "scene": "credits", "overlay_text": True}
    assert "Two other episodes show the same pictures." in evidence.reason and evidence.verdict == "unknown"
    assert "confidence high" in out.reasons[-1]


def test_labels_only_suggestions_say_how_many_frames_the_model_looked_at():
    [evidence] = join(verdicts={"credits": verdict(confidence=localvision.LOW, method="labels")}).evidence
    assert "looked at 2 frames" in evidence.reason and evidence.metrics["local_model"]["method"] == "labels"


def test_an_owner_span_or_a_finding_that_agrees_is_left_alone():
    owner = rng("credits", 1300000, DURATION, "owner", locked=True)
    assert join(owner).ranges == (owner,)
    found = rng("credits", 1334000, DURATION, "detector")
    assert join(found).ranges == (found,)


def test_agreeing_with_a_suggestion_only_adds_the_name_and_a_different_span_is_a_second_suggestion():
    chapters_says = rng("credits", 1335500, DURATION, "online", by=("Chapter marks",))
    [same] = join(chapters_says).ranges
    assert same.suggested_by == ("Chapter marks", "Local model") and (same.start_ms, same.end_ms) == (1335500, DURATION)
    plex_says = rng("credits", 1000000, 1100000, "plex")
    out = join(plex_says)
    assert out.ranges[0] == plex_says and out.ranges[1].suggested_by == ("Local model",)


def test_the_model_only_looks_where_the_comparison_left_something_open():
    confirmed = rng("credits", 1335000, DURATION, "detector")
    intro = rng("intro", 0, 90000, "detector")
    assert service._vision_anchors((confirmed, intro), DURATION, False) == {}
    short = rng("credits", 1335000, 1400000, "detector")
    assert service._vision_anchors((short, intro), DURATION, False) == {"credits": [(1335000, 1400000)]}
    pieces = (rng("intro", 0, 30000, "detector"), rng("intro", 40000, 90000, "detector"))
    assert service._vision_anchors((*pieces, confirmed), DURATION, False) == {"intro": [(0, 30000), (40000, 90000)]}
    suggested = rng("credits", 1300000, 1400000, "online")
    assert service._vision_anchors((suggested, intro), DURATION, False) == {"credits": [(1300000, 1400000)]}
    assert service._vision_anchors((), DURATION, True) == {"credits": [(DURATION - 300000, DURATION)]}
    assert service._vision_anchors((rng("credits", 1, 2, "owner"), intro), DURATION, False) == {}


def test_other_sources_times_become_hints_named_for_who_gave_them():
    hints = service._vision_hints(
        (
            rng("intro", 0, 90000, "plex"),
            rng("credits", 1300000, 1400000, "online", by=("Chapter marks", "Local model")),
            rng("credits", 1335000, DURATION, "detector"),
            rng("intro", 5, 9, "plex", action="keep"),
        )
    )
    assert [(h.start_ms, h.name) for h in hints["intro"]] == [(0, "Plex")]
    assert [h.name for h in hints["credits"]] == ["Chapter marks and AI model", "Reprise"]


def test_only_high_confidence_local_model_suggestions_are_up_for_bulk_approval():
    high = join(verdicts={"credits": verdict(confidence="high"), "intro": verdict("intro", 5000, 90000, "high")})
    medium = join(verdicts={"credits": verdict(confidence="medium")})
    assert [r.kind for r in service._confident_local(high.ranges, high.evidence)] == ["credits", "intro"]
    assert service._confident_local(medium.ranges, medium.evidence) == []
    owner = rng("intro", 4000, 90000, "owner", locked=True)
    mixed = join(owner, verdicts={"intro": verdict("intro", 5000, 90000, "high")})
    assert service._confident_local(mixed.ranges, mixed.evidence) == []  # the owner's own span wins
    plex = rng("credits", 1000000, 1100000, "plex")
    other = join(plex, verdicts={"credits": verdict(confidence="high")})
    assert [r.suggested_by for r in service._confident_local(other.ranges, other.evidence)] == [("Local model",)]


# --- settings and the season-wide approval over HTTP -------------------------------------------------------------------


def test_the_check_is_off_by_default_and_saved_through_the_settings_api(tmp_path, monkeypatch):
    from media_preview_generator.web import settings_manager
    from tests.reprise.test_api_http import registered_episode

    client, _, _, markers = registered_episode(tmp_path, monkeypatch)
    monkeypatch.setattr(
        settings_manager, "_settings_manager", settings_manager.SettingsManager(str(tmp_path / "config"))
    )
    first = client.get("/api/reprise/settings").get_json()["local_vision"]
    assert first == {"enabled": False, "base_url": localvision.DEFAULT_URL, "model": localvision.DEFAULT_MODEL}
    on = client.patch("/api/reprise/settings", json={"local_vision": {"enabled": True}})
    assert (on.status_code, on.get_json()["local_vision"]["enabled"]) == (200, True)
    named = client.patch("/api/reprise/settings", json={"local_vision": {"model": "gemma4-e4b"}})
    assert named.get_json()["local_vision"] == {**first, "enabled": True, "model": "gemma4-e4b"}
    for bad in ({"enabled": "yes"}, {"base_url": "file:///x"}, {"other": 1}, "on"):
        assert client.patch("/api/reprise/settings", json={"local_vision": bad}).status_code == 400
    markers.close()


def stored_season(tmp_path, monkeypatch, verdicts):
    """One registered episode of Show, season 1, whose stored analysis holds the local model's ``verdicts``."""
    from flask import Flask

    from media_preview_generator.markers.store import MarkerStore
    from media_preview_generator.reprise.identity import media_version
    from media_preview_generator.reprise.models import PlaybackIdentity
    from media_preview_generator.reprise.service import RepriseService, _mapping_fingerprint
    from media_preview_generator.reprise.store import RepriseStore
    from media_preview_generator.servers.base import Library, ServerConfig, ServerType
    from media_preview_generator.servers.registry import ServerRegistry
    from media_preview_generator.web.routes.api_reprise import create_reprise_blueprint

    monkeypatch.setattr("media_preview_generator.web.auth.is_auth_external", lambda: True)
    markers = MarkerStore(str(tmp_path / "markers.db"))
    store = RepriseStore(markers)
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
    path = tmp_path / "episode.mp4"
    path.write_bytes(b"0123456789")
    version = media_version(str(path), 60000, json.dumps({"audio_streams": [{"plex_id": "1", "index": 1}]}))
    identity = PlaybackIdentity("machine", "1", "2", "3", "/library/parts/3/file", 10, 60000, "4")
    config = registry.get_config("server")
    metadata = {
        "library_id": "library",
        "server_url": config.url,
        "remote_part_path": str(path),
        "mapping_fingerprint": _mapping_fingerprint(config, config.libraries[0]),
        "show_title": "Show",
        "season": 1,
        "episode": 3,
        "title": "Three",
    }
    store.register_media(version, identity, server_id="server", metadata=metadata)
    draft = with_local_model(AnalysisDraft((), (), "complete", ()), version, verdicts)
    run = store.begin_analysis(version.id, "job", "signature")
    store.finish_analysis(run, draft.evidence, draft.ranges, version.id, list(draft.reasons))
    app = Flask(__name__)
    app.secret_key = "throwaway"
    app.register_blueprint(create_reprise_blueprint(RepriseService(store, registry, None)))
    return app.test_client(), version, markers


def test_approving_a_seasons_confident_suggestions_makes_ordinary_owner_spans_and_sends_nothing(tmp_path, monkeypatch):
    sure = verdict("intro", 5000, 20000, "high")
    unsure = verdict("credits", 30000, 55000, "medium")
    client, version, markers = stored_season(tmp_path, monkeypatch, {"intro": sure, "credits": unsure})
    listed = client.get("/api/reprise/media").get_json()["items"][0]
    assert listed["confident"] == 1
    done = client.post("/api/reprise/approve-local-model", json={"show": "show", "season": 1})
    assert done.status_code == 200
    body = done.get_json()
    assert body["skipped"] == [] and [(a["kind"], a["start_ms"], a["end_ms"]) for a in body["approved"]] == [
        ("intro", 5000, 20000)
    ]
    ranges = client.get(f"/api/reprise/media/{version.id}/result").get_json()["ranges"]
    owner = [r for r in ranges if r["origin"] == "owner"]
    assert [(r["kind"], r["review_status"], r["locked"]) for r in owner] == [("intro", "approved", True)]
    credits = [r for r in ranges if r["kind"] == "credits"]
    assert [(r["origin"], r["review_status"]) for r in credits] == [
        ("online", "proposed")
    ]  # the unsure one stays a suggestion
    assert client.get("/api/reprise/media").get_json()["items"][0]["confident"] == 0
    again = client.post("/api/reprise/approve-local-model", json={"show": "Show", "season": 1}).get_json()
    assert again["approved"] == [] and again["skipped"] == []
    markers.close()


def test_the_approval_asks_for_a_show_and_a_season_and_refuses_a_show_it_does_not_know(tmp_path, monkeypatch):
    client, _, markers = stored_season(tmp_path, monkeypatch, {"intro": verdict("intro", 5000, 20000, "high")})
    for bad in (
        {},
        {"show": "Show"},
        {"show": "", "season": 1},
        {"show": "Show", "season": "1"},
        {"show": "Show", "season": 1, "x": 1},
    ):
        assert client.post("/api/reprise/approve-local-model", json=bad).status_code == 400
    assert client.post("/api/reprise/approve-local-model", json={"show": "Nope", "season": 1}).status_code == 400
    markers.close()
