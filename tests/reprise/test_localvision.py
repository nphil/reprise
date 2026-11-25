"""The optional local vision check: where frames say the credits or titles are, and how it joins a draft as advice.

The model is never called: ``scene_at`` and ``judge`` are replaced with scripted answers.
"""

import json

import pytest

from media_preview_generator.reprise import localvision, service
from media_preview_generator.reprise.analysis import with_local_model
from media_preview_generator.reprise.models import AnalysisDraft, DecisionRange, MediaVersion

DURATION = 1440000


def script(story_until, credits_until, tail="preview"):
    """A frame's scene by time: story, then credits, then ``tail``."""

    def scene_at(ms):
        return "story" if ms < story_until else "credits" if ms < credits_until else tail

    return scene_at


def test_credits_start_where_the_frames_turn_to_credits_and_run_to_the_end_over_a_preview():
    span = localvision.survey("credits", 1200000, DURATION, DURATION, script(1335000, 1425000))
    assert span is not None and abs(span[0] - 1335000) <= 1000 and span[1] == DURATION


def test_credits_stop_at_their_own_end_when_story_follows():
    span = localvision.survey("credits", 1200000, DURATION, DURATION, script(1250000, 1350000, tail="story"))
    assert span is not None and abs(span[0] - 1250000) <= 1000 and abs(span[1] - 1350000) <= 1000


def test_credits_that_stop_within_a_preview_of_the_end_run_on_to_it_and_a_noisy_frame_or_two_is_bridged():
    def scene_at(ms):
        return "story" if ms < 1335000 or 1360000 <= ms < 1368000 else "credits" if ms < 1415000 else "story"

    span = localvision.survey("credits", 1260000, DURATION, DURATION, scene_at)
    assert span is not None and abs(span[0] - 1335000) <= 1000 and span[1] == DURATION


def test_the_intro_is_the_first_run_of_title_frames():
    def scene_at(ms):
        return "credits" if 20000 <= ms < 110000 else "story"

    span = localvision.survey("intro", 0, 200000, DURATION, scene_at)
    assert span is not None and abs(span[0] - 20000) <= 1000 and abs(span[1] - 110000) <= 1000


def test_no_credits_frames_means_no_span_and_one_stray_frame_is_ignored():
    assert localvision.survey("credits", 1200000, DURATION, DURATION, lambda ms: "story") is None
    stray = lambda ms: "credits" if ms == 1209000 else "story"  # noqa: E731
    assert localvision.survey("credits", 1200000, DURATION, DURATION, stray) is None


def test_a_busy_model_ends_the_check_quietly(monkeypatch):
    cfg = localvision.Config(True)
    monkeypatch.setattr(localvision, "model_ready", lambda cfg: True)
    monkeypatch.setattr(localvision, "judge", lambda cfg, jpeg: None)
    assert localvision.check(cfg, lambda ms: b"jpeg", DURATION, {"credits": [(1335000, DURATION)]}) == {}
    monkeypatch.setattr(localvision, "model_ready", lambda cfg: False)
    assert (
        localvision.check(cfg, lambda ms: pytest.fail("no frame should be read"), DURATION, {"credits": [(1, 2)]}) == {}
    )


def test_the_verdict_keeps_every_frame_asked_about(monkeypatch):
    cfg = localvision.Config(True)
    monkeypatch.setattr(localvision, "model_ready", lambda cfg: True)
    monkeypatch.setattr(localvision, "judge", lambda cfg, jpeg: ("credits" if int(jpeg) >= 1335000 else "story", False))
    verdict = localvision.check(cfg, lambda ms: str(ms).encode(), DURATION, {"credits": [(1320000, DURATION)]})[
        "credits"
    ]
    assert abs(verdict.start_ms - 1335000) <= 1000 and verdict.end_ms == DURATION
    assert verdict.model == cfg.model and [t for t, _, _ in verdict.frames] == sorted(t for t, _, _ in verdict.frames)


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


def verdict(kind="credits", start=1335000, end=DURATION):
    return localvision.Verdict(kind, start, end, ((1330000, "story", False), (1340000, "credits", True)), "qwen3-vl-4b")


def join(*ranges, verdicts=None):
    draft = AnalysisDraft((), tuple(ranges), "complete", ())
    return with_local_model(draft, MEDIA, verdicts or {"credits": verdict()})


def test_a_new_verdict_is_an_approve_only_suggestion_with_its_frames_as_evidence():
    out = join()
    [suggestion] = out.ranges
    assert (suggestion.origin, suggestion.review_status, suggestion.locked, suggestion.suggested_by) == (
        "online",
        "proposed",
        False,
        ("Local model",),
    )
    assert (
        suggestion.reason
        == "Suggested by a local vision model that looked at frames of the episode — not confirmed by Reprise."
    )
    [evidence] = out.evidence
    assert evidence.metrics["local_model"]["frames"][1] == {"at_ms": 1340000, "scene": "credits", "overlay_text": True}
    assert evidence.verdict == "unknown"


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
