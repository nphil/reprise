"""Phone notifications with buttons: signed single-use button addresses, what is said and when (batching, rate limit,
quiet hours, no repeats), and the buttons themselves. The phone service is a fake ``post`` and time is a fake clock;
the buttons run against a real store and Plex database (the ``Lab`` of the automatic sender) where it matters."""

import json
import re
from datetime import datetime
from types import SimpleNamespace

import pytest
from flask import Flask
from loguru import logger

from media_preview_generator.markers.store import MarkerStore
from media_preview_generator.reprise import approvals, localvision, nudges, plexbackup
from media_preview_generator.reprise.alerts import NTFY_TOPIC, NTFY_URL, TOKEN_FILE, Alerts
from media_preview_generator.reprise.models import ReviewRefused
from media_preview_generator.reprise.nudges import ACTION_APPROVE, ACTION_UNDO, Notifier, TokenError
from media_preview_generator.reprise.service import NOT_SURE_ANY_MORE
from media_preview_generator.reprise.store import RepriseStore
from media_preview_generator.web.routes.api_reprise_notify import create_notify_blueprint
from tests.reprise.test_alerts import TOKEN, Clock, FakePost
from tests.reprise.test_approvals import SURE_CREDITS, analyse, ranges_of
from tests.reprise.test_autosend import DURATION, Lab, _marker_times

SECRET = "webhook-secret-one"
BASE = "https://reprise.nateshome.net"


def at(hour, minute=0, day=15):
    """Epoch seconds of a local time on a fixed day (quiet hours are read in the app's local time)."""
    return int(datetime(2026, 11, day, hour, minute).timestamp())


def group(sure=(), needs=0):
    """What ``approvals._scan`` says one season holds: ``sure`` is ``(version id, episode)`` pairs."""
    return {
        "sure": [{"media_version_id": v, "episode": e, "kind": "credits"} for v, e in sure],
        "needs": [{"media_version_id": f"look-{i}", "episode": 50 + i, "kind": "intro"} for i in range(needs)],
        "ready": [],
    }


class Settings:
    def __init__(self):
        self.values = {}

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value):
        self.values[key] = value


@pytest.fixture
def env(tmp_path, monkeypatch):
    marker = MarkerStore(str(tmp_path / "markers.db"))
    store = RepriseStore(marker)
    (tmp_path / TOKEN_FILE).write_text(f"{TOKEN}\n")
    settings = Settings()
    service = SimpleNamespace(
        store=store,
        registry=SimpleNamespace(settings=settings),
        publisher=SimpleNamespace(writes_enabled=True),
        _setting=lambda key, default: settings.get(key, default),
    )
    post, clock = FakePost(), Clock(at(12))
    alerts = Alerts(store, config_dir=str(tmp_path), post=post, clock=clock)
    world = SimpleNamespace(groups={}, auto=[], secret=SECRET, calls=[], sent=None, undone=None)

    def make():
        return Notifier(
            service, alerts, clock=clock, secret=lambda: world.secret, scan=lambda _s: world.groups, every=0
        )

    def recent_automatic(_service, *, since=None, limit=50):
        return [e for e in world.auto if since is None or e["sent_at"] >= since][:limit]

    def send(_service, show, season, scope, *, to_plex=True, now=None, only=None):
        world.calls.append(("send", show, season, scope, to_plex, sorted(only)))
        return world.sent

    def undo(_service, changes):
        world.calls.append(("undo", [c["change_id"] for c in changes]))
        return world.undone

    monkeypatch.setattr(approvals, "recent_automatic", recent_automatic, raising=False)
    monkeypatch.setattr(approvals, "send", send)
    monkeypatch.setattr(approvals, "undo", undo)
    monkeypatch.delenv("REPRISE_PUBLIC_URL", raising=False)
    yield SimpleNamespace(
        store=store,
        dir=tmp_path,
        service=service,
        settings=settings,
        post=post,
        clock=clock,
        world=world,
        notifier=make(),
        make=make,
    )
    marker.close()


@pytest.fixture
def logs():
    lines = []
    sink = logger.add(lines.append, level="DEBUG")
    yield lines
    logger.remove(sink)


def titles(env):
    return [call["headers"]["Title"] for call in env.post.calls]


def buttons(call):
    """``[(kind, label, url, options...)]`` of a sent message's ``Actions`` header."""
    return [[part.strip() for part in action.split(", ")] for action in call["headers"]["Actions"].split("; ")]


def token_of(call):
    return buttons(call)[0][2].rsplit("/", 1)[1]


def auto_entry(change_id, episode, *, show="Community", season=2, kinds=("credits",), sent_at=None):
    return {
        "change_id": change_id,
        "media_version_id": f"v-{change_id}",
        "show": show,
        "season": season,
        "episode": episode,
        "label": f"S{season:02d}E{episode:02d}",
        "kinds": list(kinds),
        "sent_at": at(11, 30) if sent_at is None else sent_at,
        "span_ids": [f"span-{change_id}"],
        "origin": "sure",
    }


# ── tokens ───────────────────────────────────────────────────────────────────────────────────────────────────────


def test_a_signed_token_round_trips_and_is_plain_url_text():
    token, payload = nudges.sign(SECRET, "approve", {"ids": ["a"]}, now=1000)

    assert re.fullmatch(r"v1\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", token)
    assert nudges.verify(SECRET, token, now=1000 + 60) == payload
    assert (payload["a"], payload["p"], payload["e"]) == ("approve", {"ids": ["a"]}, 1000 + nudges.TOKEN_TTL_S)
    assert nudges.TOKEN_TTL_S == 6 * 3600
    assert SECRET not in token


def test_a_changed_payload_or_signature_or_another_secret_is_refused():
    token, _ = nudges.sign(SECRET, "approve", {"ids": ["a"]}, now=1000)
    version, body, signature = token.split(".")
    other, _ = nudges.sign(SECRET, "undo", {"ids": ["a"]}, now=1000)
    forged_body = other.split(".")[1]

    for bad in (
        f"{version}.{forged_body}.{signature}",  # another action's payload under this signature
        f"{version}.{body}.{signature[:-2]}AA",
        f"{version}.{body[:-1]}{'A' if body[-1] != 'A' else 'B'}.{signature}",
    ):
        with pytest.raises(TokenError) as refused:
            nudges.verify(SECRET, bad, now=1001)
        assert refused.value.status == 403
    with pytest.raises(TokenError) as refused:
        nudges.verify("a-rotated-secret", token, now=1001)
    assert refused.value.status == 403


@pytest.mark.parametrize("junk", ["", "abc", "v1.a.b.c", "v2.a.b", "v1..b", None, "v1.é.b", "x" * 5000])
def test_something_that_is_no_token_is_a_400(junk):
    with pytest.raises(TokenError) as refused:
        nudges.verify(SECRET, junk, now=1)
    assert refused.value.status == 400


def test_a_token_stops_working_when_it_expires():
    token, _ = nudges.sign(SECRET, "approve", {}, now=1000, ttl=60)

    assert nudges.verify(SECRET, token, now=1059)
    with pytest.raises(TokenError) as refused:
        nudges.verify(SECRET, token, now=1060)
    assert refused.value.status == 410


def test_a_signed_payload_that_is_not_the_shape_of_one_is_a_400():
    body = nudges._b64(json.dumps({"a": "x", "p": [], "n": "n", "e": 5}).encode())
    signed = f"v1.{body}"
    token = f"{signed}.{nudges._signature(SECRET, signed)}"

    with pytest.raises(TokenError) as refused:
        nudges.verify(SECRET, token, now=1)
    assert refused.value.status == 400


def test_a_button_works_once_and_a_second_press_does_nothing(env):
    notifier = env.notifier
    env.world.undone = {"restored": [{"media_version_id": "v", "label": "E1", "withdrawn": True}], "failed": []}
    token = notifier.issue(ACTION_UNDO, {"changes": [{"media_version_id": "v", "change_id": "c1"}]}, env.clock())

    first = notifier.act(token)
    second = notifier.act(token)

    assert first[0] == 200 and first[1]["ok"] is True
    assert second[0] == 410 and "already used" in second[1]["message"]
    assert env.world.calls == [("undo", ["c1"])]  # the second press ran nothing


def test_two_presses_at_once_still_run_it_once(env):
    token = env.notifier.issue(ACTION_UNDO, {"changes": []}, env.clock())
    env.world.undone = {"restored": [], "failed": []}
    outcomes = []
    import threading

    def press():
        outcomes.append(env.notifier.act(token)[0])

    threads = [threading.Thread(target=press) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    assert sorted(outcomes) == [409] + [410] * 5
    assert len(env.world.calls) == 1


def test_a_token_for_one_action_cannot_be_turned_into_another(env):
    approve = env.notifier.issue(ACTION_APPROVE, {"show": "S", "season": 1, "ids": ["a"]}, env.clock())
    version, body, signature = approve.split(".")
    swapped = json.loads(nudges._unb64(body))
    swapped["a"] = ACTION_UNDO
    forged = f"{version}.{nudges._b64(json.dumps(swapped, sort_keys=True, separators=(',', ':')).encode())}.{signature}"

    assert env.notifier.act(forged)[0] == 403
    assert env.world.calls == []
    # Signed correctly by the real secret but for a different action than the stored one: refused as well.
    stored = json.loads(env.store.marker_store._conn.execute("SELECT params FROM reprise_notify_tokens").fetchone()[0])
    token, _ = nudges.sign(SECRET, ACTION_UNDO, {"d": nudges._digest(ACTION_UNDO, stored)}, now=env.clock())
    assert env.notifier.act(token)[0] == 410  # nothing was ever issued under it
    assert env.world.calls == []


def test_a_token_whose_stored_list_was_changed_is_refused(env):
    token = env.notifier.issue(ACTION_APPROVE, {"show": "S", "season": 1, "ids": ["a"]}, env.clock())
    with env.store.transaction() as conn:
        conn.execute(
            "UPDATE reprise_notify_tokens SET params=?", (json.dumps({"show": "S", "season": 1, "ids": ["a", "b"]}),)
        )

    assert env.notifier.act(token)[0] == 403
    assert env.world.calls == []


def test_rotating_the_webhook_secret_ends_every_outstanding_button(env):
    token = env.notifier.issue(ACTION_UNDO, {"changes": []}, env.clock())
    env.world.secret = "rotated-secret"

    status, body = env.notifier.act(token)

    assert status == 403 and body["ok"] is False
    assert env.world.calls == []


def test_an_expired_button_says_so_and_does_nothing(env):
    token = env.notifier.issue(ACTION_UNDO, {"changes": []}, env.clock())
    env.clock.now += nudges.TOKEN_TTL_S

    status, body = env.notifier.act(token)

    assert status == 410 and "expired" in body["message"]
    assert env.world.calls == []


def test_spent_and_expired_tokens_are_cleared_from_the_database(env):
    env.notifier.issue(ACTION_UNDO, {"changes": []}, env.clock())
    env.clock.now += nudges.TOKEN_TTL_S + 1
    env.notifier.issue(ACTION_UNDO, {"changes": []}, env.clock())

    assert env.store.marker_store._conn.execute("SELECT COUNT(*) FROM reprise_notify_tokens").fetchone()[0] == 1


# ── the message for a season ─────────────────────────────────────────────────────────────────────────────────────


def test_one_message_per_season_with_an_approve_button_bound_to_the_sure_ones(env):
    env.world.groups = {
        ("Community", 2): group(sure=[("a", 3), ("b", 5), ("c", 4)], needs=2),
        ("Parks and Recreation", 1): group(needs=1),
    }

    assert env.notifier.run() == 2
    sure, look = env.post.calls

    assert sure["url"] == f"{NTFY_URL}/{NTFY_TOPIC}"
    assert sure["headers"]["Title"] == "Community S2: 3 sure ones, 2 to look at"
    assert sure["data"].decode().splitlines()[1] == "Sure ones: E3, E4, E5. They are ready for you to approve."
    assert sure["headers"]["Click"] == f"{BASE}/reprise/approve"
    assert sure["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert sure["headers"]["Actions"].count(";") == 1
    [approve, view] = buttons(sure)
    assert approve[:2] == ["http", "Approve & send 3"] and approve[3:] == ["method=POST", "clear=true"]
    assert approve[2].startswith(f"{BASE}/api/reprise/notify/act/v1.") and SECRET not in approve[2]
    assert view == ["view", "Open", f"{BASE}/reprise/approve"]
    assert look["headers"]["Title"] == "Parks and Recreation S1: 1 to look at"
    assert buttons(look) == [["view", "Open", f"{BASE}/reprise/approve"]]  # nothing sure, so only Open


def test_the_approve_button_approves_exactly_the_listed_ids_and_asks_for_plex_when_it_is_on(env):
    env.world.groups = {("Community", 2): group(sure=[("a", 3), ("b", 5)])}
    env.world.sent = {"approved": [{"kinds": ["credits"]}] * 2, "sent": [{"kinds": ["credits"]}] * 2, "failed": []}
    env.notifier.run()

    status, body = env.notifier.act(token_of(env.post.calls[0]))

    assert (status, body["ok"], body["sent"]) == (200, True, 2)
    assert body["message"] == "Sent 2 sure ones to Plex for Community S2."
    assert env.world.calls == [("send", "Community", 2, "sure", True, ["a", "b"])]


def test_with_sending_off_the_button_only_approves_and_says_so_like_the_page(env):
    env.service.publisher.writes_enabled = False
    env.world.groups = {("Community", 2): group(sure=[("a", 3)])}
    env.world.sent = {"approved": [{"kinds": ["credits"]}], "sent": [], "failed": []}
    env.notifier.run()

    status, body = env.notifier.act(token_of(env.post.calls[0]))

    assert buttons(env.post.calls[0])[0][1] == "Approve 1"
    assert env.world.calls == [("send", "Community", 2, "sure", False, ["a"])]
    assert status == 200 and "Sending to Plex is off" in body["message"]


def test_a_listed_one_that_is_no_longer_sure_is_reported_and_the_rest_still_go(env):
    env.world.groups = {("Community", 2): group(sure=[("a", 3), ("b", 5)])}
    env.world.sent = {
        "approved": [{"kinds": ["credits"]}],
        "sent": [{"kinds": ["credits"]}],
        "failed": [{"media_version_id": "b", "reason": NOT_SURE_ANY_MORE}],
    }
    env.notifier.run()

    status, body = env.notifier.act(token_of(env.post.calls[0]))

    assert status == 200 and body["failed"] == 1 and NOT_SURE_ANY_MORE in body["message"]


def test_a_button_with_nothing_left_to_approve_says_so_in_plain_words(env, monkeypatch):
    env.world.groups = {("Community", 2): group(sure=[("a", 3)])}
    env.notifier.run()

    def nothing_left(*args, **kwargs):
        raise ReviewRefused("There is nothing sure left to approve in this season.")

    monkeypatch.setattr(approvals, "send", nothing_left)
    status, body = env.notifier.act(token_of(env.post.calls[0]))

    assert (status, body) == (409, {"ok": False, "message": "There is nothing sure left to approve in this season."})


def test_a_button_that_hits_an_unexpected_error_answers_plainly_and_logs_only_its_kind(env, monkeypatch, logs):
    env.world.groups = {("Community", 2): group(sure=[("a", 3)])}
    env.notifier.run()

    def boom(*args, **kwargs):
        raise RuntimeError(f"secret detail {TOKEN}")

    monkeypatch.setattr(approvals, "send", boom)
    status, body = env.notifier.act(token_of(env.post.calls[0]))

    assert status == 500 and "To approve page" in body["message"]
    assert not any(TOKEN in line or "secret detail" in line for line in logs)


def test_a_show_with_accents_gets_a_plain_title_and_a_long_name_never_loses_the_counts(env):
    env.world.groups = {
        ("Pokémon", 1): group(sure=[("a", 1)]),
        ("A" * 80, 1): group(sure=[("b", 1)], needs=12),
    }

    env.notifier.run()

    second, first = titles(env)  # same number of sure ones: by show name
    assert first == "Pokemon S1: 1 sure one" and second.endswith("S1: 1 sure one, 12 to look at")
    assert len(second) <= 60
    assert "Pokémon" in env.post.calls[1]["data"].decode()


def test_the_specials_season_and_a_long_episode_list_read_plainly(env):
    env.world.groups = {("Community", 0): group(sure=[(f"v{n}", n) for n in range(1, 13)])}

    env.notifier.run()

    assert titles(env) == ["Community Specials: 12 sure ones"]
    assert "E1, E2, E3, E4, E5, E6, E7, E8 and 4 more" in env.post.calls[0]["data"].decode()


def test_the_public_address_can_be_changed_by_the_environment(env, monkeypatch):
    monkeypatch.setenv("REPRISE_PUBLIC_URL", "https://example.test/")
    env.world.groups = {("Community", 2): group(sure=[("a", 3)])}

    env.notifier.run()

    assert buttons(env.post.calls[0])[1][2] == "https://example.test/reprise/approve"
    assert env.post.calls[0]["headers"]["Click"] == "https://example.test/reprise/approve"


def test_a_label_with_a_comma_is_quoted_for_the_actions_header():
    assert nudges._action_label("Approve, send") == '"Approve, send"'
    assert nudges._action_label("Approve & send 3") == "Approve & send 3"
    assert nudges._action_label("Prüfen") == "Prufen"


# ── repeats, limits and quiet hours ──────────────────────────────────────────────────────────────────────────────


def test_a_season_is_not_told_again_until_something_new_is_waiting_there(env):
    env.world.groups = {("Community", 2): group(sure=[("a", 3)], needs=1)}
    assert env.notifier.run() == 1

    assert env.notifier.run() == 0  # nothing changed
    env.clock.now += 3 * 3600
    env.world.groups = {("Community", 2): group(sure=[("a", 3)], needs=1)}
    assert env.notifier.run() == 0
    env.world.groups = {("Community", 2): group(needs=1)}  # the sure one was dealt with: nothing new to say
    assert env.notifier.run() == 0
    env.world.groups = {("Community", 2): group(sure=[("a", 3)], needs=1)}  # but it is back: now it is news again
    assert env.notifier.run() == 1
    env.clock.now += 3 * 3600
    env.world.groups = {("Community", 2): group(sure=[("a", 3), ("z", 9)], needs=1)}  # another sure one arrived
    assert env.notifier.run() == 1
    env.clock.now += 3 * 3600
    env.world.groups = {("Community", 2): group(sure=[("a", 3), ("z", 9)], needs=3)}  # more to look at
    assert env.notifier.run() == 1
    assert len(env.post.calls) == 4


def test_a_season_that_empties_and_fills_again_is_told_again(env):
    env.world.groups = {("Community", 2): group(sure=[("a", 3)])}
    assert env.notifier.run() == 1
    env.world.groups = {("Community", 2): group()}
    assert env.notifier.run() == 0
    env.world.groups = {("Community", 2): group(sure=[("a", 3)])}
    assert env.notifier.run() == 1


def test_a_season_with_nothing_waiting_is_never_told(env):
    env.world.groups = {("Community", 2): group()}

    assert env.notifier.run() == 0
    assert env.post.calls == []


def test_at_most_three_a_rolling_hour_and_the_rest_wait_for_a_later_pass(env):
    env.world.groups = {(f"Show {n}", 1): group(sure=[(f"v{n}", 1)]) for n in range(5)}

    assert env.notifier.run() == 3
    assert env.notifier.status()["waiting"] == 2 and env.notifier.status()["waiting_reason"] == "limit"
    env.clock.now += 1800
    assert env.notifier.run() == 0  # the fourth is deferred, not lost
    env.clock.now += 1801  # the first three are more than an hour old
    assert env.notifier.run() == 2
    assert len(env.post.calls) == 5 and len(set(titles(env))) == 5
    assert env.notifier.status()["waiting"] == 0


def test_the_hourly_limit_survives_a_restart(env):
    env.world.groups = {(f"Show {n}", 1): group(sure=[(f"v{n}", 1)]) for n in range(5)}
    env.notifier.run()

    restarted = env.make()

    assert restarted.run() == 0 and len(env.post.calls) == 3


def test_what_is_sent_first_is_the_most_useful_automatic_then_sure_then_look_ats(env):
    env.world.auto = [auto_entry("c1", 3)]
    env.world.groups = {
        ("Aaa", 1): group(needs=4),
        ("Bbb", 1): group(sure=[("b", 1)]),
        ("Ccc", 1): group(sure=[("c1", 1), ("c2", 2)]),
        ("Ddd", 1): group(sure=[("d", 1)]),
    }

    env.notifier.run()
    assert titles(env) == ["Reprise sent 1 ending to Plex", "Ccc S1: 2 sure ones", "Bbb S1: 1 sure one"]
    env.clock.now += 3601
    env.notifier.run()
    assert titles(env)[3:] == ["Ddd S1: 1 sure one", "Aaa S1: 4 to look at"]


def test_quiet_hours_that_wrap_midnight_hold_everything_back_until_they_end(env):
    env.world.groups = {("Community", 2): group(sure=[("a", 3)])}
    quiet = env.notifier.quiet_now

    assert [quiet(at(h, m)) for h, m in ((22, 0), (23, 30), (0, 0), (7, 59))] == [True] * 4
    assert [quiet(at(h, m)) for h, m in ((8, 0), (12, 0), (21, 59))] == [False] * 3

    env.clock.now = at(23, 0)
    assert env.notifier.run() == 0 and env.post.calls == []
    assert env.notifier.status()["waiting"] == 1 and env.notifier.status()["waiting_reason"] == "quiet_hours"
    env.clock.now = at(7, 59, day=16)
    assert env.notifier.run() == 0
    env.clock.now = at(8, 0, day=16)
    assert env.notifier.run() == 1  # nothing was dropped
    assert env.notifier.status()["waiting"] == 0


def test_quiet_hours_inside_one_day_and_equal_times_for_no_quiet_hours(env):
    env.notifier.update_settings({"quiet_from": "13:00", "quiet_until": "15:30"})
    quiet = env.notifier.quiet_now

    assert [quiet(at(h, m)) for h, m in ((12, 59), (13, 0), (15, 29), (15, 30), (23, 0))] == [
        False,
        True,
        True,
        False,
        False,
    ]
    env.notifier.update_settings({"quiet_from": "06:00", "quiet_until": "06:00"})
    assert not any(quiet(at(h)) for h in range(24))


def test_the_off_switch_sends_nothing_and_does_not_even_look(env):
    env.notifier.update_settings({"enabled": False})
    env.notifier._scan = lambda _s: pytest.fail("looked at the queue while off")
    env.world.auto = [auto_entry("c1", 3)]

    assert env.notifier.run() == 0
    assert env.post.calls == [] and env.notifier.status()["waiting"] == 0


def test_without_a_token_nothing_is_sent_and_the_warning_comes_once(env, logs):
    (env.dir / TOKEN_FILE).unlink()
    env.world.groups = {("Community", 2): group(sure=[("a", 3)])}

    assert [env.notifier.run() for _ in range(3)] == [0, 0, 0]

    assert env.post.calls == []
    assert sum("no ntfy token" in line for line in logs) == 1


def test_a_failed_send_keeps_the_message_for_the_next_pass_and_does_not_count(env):
    env.world.groups = {("Community", 2): group(sure=[("a", 3)])}
    env.post.outcome = 503
    assert env.notifier.run() == 0
    assert env.notifier.status()["last"] is None and env.notifier.status()["waiting"] == 1

    env.post.outcome = 200
    assert env.notifier.run() == 1
    assert titles(env) == ["Community S2: 1 sure one"] * 2


def test_a_pass_that_fails_inside_never_raises_and_warns_once_in_plain_words(env, logs):
    def broken(_service):
        raise RuntimeError(f"disk trouble {TOKEN}")

    env.notifier._scan = broken

    assert env.notifier.run() == 0
    assert sum("Phone notifications did not run" in line for line in logs) == 1
    assert not any(TOKEN in line for line in logs)


def test_a_pass_waits_for_its_turn_so_the_queue_is_not_read_every_minute(env):
    env.world.groups = {("Community", 2): group(sure=[("a", 3)])}
    slow = Notifier(
        env.service, env.notifier.alerts, clock=env.clock, secret=lambda: SECRET, scan=lambda _s: env.world.groups
    )

    assert slow.run() == 1
    slow._scan = lambda _s: pytest.fail("looked again too soon")
    env.clock.now += 60
    assert slow.run() == 0


# ── what Reprise sent by itself ──────────────────────────────────────────────────────────────────────────────────


def test_automatic_sends_are_told_in_one_message_with_an_undo_for_exactly_those(env):
    env.world.auto = [auto_entry("c5", 5), auto_entry("c3", 3, sent_at=at(11, 0))]
    env.world.undone = {"restored": [{}, {}], "failed": []}

    assert env.notifier.run() == 1
    [call] = env.post.calls

    assert call["headers"]["Title"] == "Reprise sent 2 endings to Plex"
    assert call["data"].decode().startswith("Reprise sent 2 endings to Plex automatically: Community S2 E3, E5.")
    [undo, view] = buttons(call)
    assert undo[:2] == ["http", "Undo"] and undo[3:] == ["method=POST", "clear=true"]
    assert view == ["view", "Open", f"{BASE}/reprise/approve"]
    status, body = env.notifier.act(token_of(call))
    assert (status, body["message"]) == (200, "Put 2 episodes back as they were in Plex.")
    assert env.world.calls == [("undo", ["c3", "c5"])]  # oldest first, as ``approvals.undo`` expects


def test_automatic_sends_already_told_are_not_told_again_and_new_ones_are(env):
    env.world.auto = [auto_entry("c1", 1)]
    assert env.notifier.run() == 1
    assert env.notifier.run() == 0
    env.world.auto = [auto_entry("c2", 2, kinds=("intro", "credits")), auto_entry("c1", 1)]
    env.clock.now += 60
    assert env.notifier.run() == 1
    assert titles(env)[1] == "Reprise sent 1 intro and 1 ending to Plex"
    assert env.world.auto[1]["change_id"] == "c1"


def test_sends_older_than_a_button_stays_good_are_not_news_on_the_first_pass(env):
    env.world.auto = [auto_entry("old", 1, sent_at=env.clock() - nudges.TOKEN_TTL_S - 5)]

    assert env.notifier.run() == 0


def test_when_nothing_provides_automatic_sends_yet_that_part_is_skipped(env, monkeypatch):
    monkeypatch.delattr(approvals, "recent_automatic")
    env.world.groups = {("Community", 2): group(sure=[("a", 3)])}

    assert env.notifier.run() == 1


def test_an_undo_that_cannot_put_everything_back_says_which(env):
    env.world.auto = [auto_entry("c1", 1)]
    env.world.undone = {"restored": [], "failed": [{"reason": "Plex could not be reached."}]}
    env.notifier.run()

    status, body = env.notifier.act(token_of(env.post.calls[0]))

    assert status == 409
    assert body["message"] == "Nothing was put back. 1 episode could not be put back: Plex could not be reached."


# ── settings ─────────────────────────────────────────────────────────────────────────────────────────────────────


def test_settings_start_on_with_quiet_hours_from_ten_at_night_to_eight_and_are_kept(env):
    assert env.notifier.settings() == {"enabled": True, "quiet_from": "22:00", "quiet_until": "08:00"}

    env.notifier.update_settings({"enabled": False, "quiet_from": "23:15"})

    assert env.settings.values == {"reprise_notify": False, "reprise_notify_quiet_from": "23:15"}
    assert env.make().settings() == {"enabled": False, "quiet_from": "23:15", "quiet_until": "08:00"}


@pytest.mark.parametrize(
    "values",
    [
        {},
        None,
        [],
        {"enabled": "yes"},
        {"enabled": 1},
        {"quiet_from": "25:00"},
        {"quiet_from": "7:00"},
        {"quiet_until": "08:60"},
        {"quiet_until": 800},
        {"quiet_from": "22:00:00"},
        {"unknown": True},
        {"enabled": True, "quiet_from": "bad"},
    ],
)
def test_settings_that_make_no_sense_are_refused_and_nothing_is_saved(env, values):
    with pytest.raises(ValueError):
        env.notifier.update_settings(values)

    assert env.settings.values == {}


def test_a_hand_edited_setting_that_is_not_a_time_reads_as_the_default(env):
    env.settings.values.update({"reprise_notify_quiet_from": "tea time", "reprise_notify_quiet_until": 7})

    assert env.notifier.settings()["quiet_from"] == "22:00" and env.notifier.settings()["quiet_until"] == "08:00"


def test_the_status_says_what_was_last_sent_and_the_test_alert_is_unaffected(env):
    env.world.groups = {("Community", 2): group(sure=[("a", 3)])}
    assert env.notifier.status()["last"] is None
    env.notifier.run()

    last = env.notifier.status()["last"]

    assert last["title"] == "Community S2: 1 sure one" and last["at"].endswith("Z")
    assert env.notifier.alerts.last() is None  # a phone notice is not a problem alert
    assert env.notifier.alerts.test()["sent"] is True
    assert env.post.calls[-1]["headers"]["Title"] == "Test alert from Reprise"
    assert "Actions" not in env.post.calls[-1]["headers"] and env.post.calls[-1]["headers"]["Tags"] == "bell"


# ── the routes ───────────────────────────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def client(env, monkeypatch):
    monkeypatch.setattr("media_preview_generator.web.auth.is_auth_external", lambda: True)
    env.service.autopilot = SimpleNamespace(notifier=env.notifier)
    app = Flask(__name__)
    app.secret_key = "throwaway"
    app.register_blueprint(create_notify_blueprint(env.service))
    return app.test_client()


def test_a_button_press_needs_no_cookie_no_header_and_no_csrf_token(client, env):
    env.world.undone = {"restored": [{}], "failed": []}
    token = env.notifier.issue(ACTION_UNDO, {"changes": [{"media_version_id": "v", "change_id": "c1"}]}, env.clock())

    answer = client.post(f"/api/reprise/notify/act/{token}")

    assert answer.status_code == 200
    assert answer.json == {
        "ok": True,
        "message": "Put 1 episode back as they were in Plex.",
        "restored": 1,
        "failed": 0,
    }
    assert answer.headers["Cache-Control"] == "no-store"
    assert client.post(f"/api/reprise/notify/act/{token}").status_code == 410
    assert env.world.calls == [("undo", ["c1"])]


@pytest.mark.parametrize(
    ("path", "status"), [("garbage", 400), ("v1.abc.def", 403), ("v9.abc.def", 400), ("v1.%C3%A9.def", 400)]
)
def test_a_bad_address_is_refused_with_plain_words(client, env, path, status):
    answer = client.post(f"/api/reprise/notify/act/{path}")

    assert answer.status_code == status and answer.json["ok"] is False and answer.json["message"]
    assert env.world.calls == []


def test_a_button_address_is_post_only(client, env):
    token = env.notifier.issue(ACTION_UNDO, {"changes": []}, env.clock())

    assert client.get(f"/api/reprise/notify/act/{token}").status_code == 405
    assert env.world.calls == []


def test_the_buttons_say_so_when_there_is_no_automatic_work(env, monkeypatch):
    app = Flask(__name__)
    env.service.autopilot = None
    app.register_blueprint(create_notify_blueprint(env.service))

    answer = app.test_client().post("/api/reprise/notify/act/v1.a.b")

    assert answer.status_code == 503 and answer.json["ok"] is False


def test_the_settings_route_reads_and_changes_them_and_refuses_nonsense(client, env):
    env.world.groups = {("Community", 2): group(sure=[("a", 3)])}
    env.clock.now = at(23)
    env.notifier.run()

    shown = client.get("/api/reprise/notifications").json
    changed = client.patch("/api/reprise/notifications", json={"enabled": False, "quiet_until": "06:30"})
    refused = client.patch("/api/reprise/notifications", json={"quiet_until": "6.30"})

    assert shown == {
        "enabled": True,
        "quiet_from": "22:00",
        "quiet_until": "08:00",
        "last": None,
        "waiting": 1,
        "waiting_reason": "quiet_hours",
    }
    assert changed.status_code == 200 and (changed.json["enabled"], changed.json["quiet_until"]) == (False, "06:30")
    assert refused.status_code == 400 and "like 22:00" in refused.json["error"]
    assert client.patch("/api/reprise/notifications", json=[1]).status_code == 400
    assert env.settings.values == {"reprise_notify": False, "reprise_notify_quiet_until": "06:30"}


# ── the real thing: a real store and Plex database ───────────────────────────────────────────────────────────────


@pytest.fixture
def lab(tmp_path, monkeypatch):
    from types import SimpleNamespace as NS

    app = Lab(tmp_path, monkeypatch)
    app.service.job_manager = NS(config_dir=str(tmp_path))
    monkeypatch.setattr(plexbackup, "ensure_daily_backup", app.backups)
    monkeypatch.setattr(
        approvals, "read_plex_markers", lambda server, keys: ({k: app.plex_markers.get(k, []) for k in keys}, None)
    )
    monkeypatch.setattr(approvals, "_hidden_kinds", lambda server, library: set())
    app.plex_markers = {}
    app.clock.now = at(12)
    yield app
    if not app.marker._closed:
        app.marker.close()


def sure_episodes(lab, numbers=(1, 2, 3)):
    return [analyse(lab, lab.episode(number), SURE_CREDITS) for number in numbers]


def test_send_with_a_list_only_touches_the_listed_sure_episodes(lab):
    one, two, three = sure_episodes(lab)

    done = approvals.send(lab.service, "Community", 1, "sure", only=[one.vid, two.vid])

    assert [s["label"] for s in done["sent"]] == ["S01E01", "S01E02"] and done["failed"] == []
    assert _marker_times(lab.plex, "credits", item=3) == []
    assert [r.review_status for r in ranges_of(lab, three, "credits")] == ["proposed"]


def test_a_listed_episode_that_stopped_being_sure_or_is_unknown_is_skipped_and_reported(lab):
    one, two, three = sure_episodes(lab)
    analyse(lab, two, ("credits", 80_000, DURATION, localvision.MEDIUM))  # a new analysis: now only a guess

    done = approvals.send(lab.service, "Community", 1, "sure", only=[one.vid, two.vid, "not-an-episode"])

    assert [s["label"] for s in done["sent"]] == ["S01E01"]
    reasons = {f["media_version_id"]: f["reason"] for f in done["failed"]}
    assert reasons == {two.vid: NOT_SURE_ANY_MORE, "not-an-episode": NOT_SURE_ANY_MORE}
    assert _marker_times(lab.plex, "credits", item=2) == [] and _marker_times(lab.plex, "credits", item=3) == []
    assert [r.review_status for r in ranges_of(lab, three, "credits")] == ["proposed"]


def test_a_list_with_nothing_sure_in_it_is_refused_and_nothing_is_approved(lab):
    one, two, three = sure_episodes(lab)

    with pytest.raises(ReviewRefused, match="nothing sure left"):
        approvals.send(lab.service, "Community", 1, "sure", only=["not-an-episode"])

    assert all([r.review_status for r in ranges_of(lab, ep, "credits")] == ["proposed"] for ep in (one, two, three))


def test_approve_confident_with_a_list_leaves_the_others_alone(lab):
    one, two, three = sure_episodes(lab)

    outcome = lab.service.approve_confident("Community", 1, only={two.vid})

    assert [a["id"] for a in outcome["approved"]] == [two.vid] and outcome["skipped"] == []
    assert [r.review_status for r in ranges_of(lab, one, "credits")] == ["proposed"]
    assert lab.service.approve_confident("Community", 1)["approved"][0]["id"] == one.vid  # no list: as before


def test_a_real_season_message_then_its_button_approves_and_sends_what_it_listed_then_it_is_spent(lab):
    ones = sure_episodes(lab, (1, 2))
    analyse(lab, lab.episode(9), ("intro", 6_000, 21_000, localvision.MEDIUM))
    notifier = Notifier(lab.service, lab.alerts, clock=lab.clock, secret=lambda: SECRET, every=0)

    assert notifier.run() == 1
    [call] = lab.post.calls
    assert call["headers"]["Title"] == "Community S1: 2 sure ones, 1 to look at"
    late = analyse(lab, lab.episode(4), SURE_CREDITS)  # a new sure one arrives after the message went out

    status, body = notifier.act(token_of(call))

    assert (status, body["message"]) == (200, "Sent 2 sure ones to Plex for Community S1.")
    assert all(len(_marker_times(lab.plex, "credits", item=ep.number)) == 1 for ep in ones)
    assert _marker_times(lab.plex, "credits", item=4) == []  # not in the message, so not touched
    assert [r.review_status for r in ranges_of(lab, late, "credits")] == ["proposed"]
    assert notifier.act(token_of(call))[0] == 410


def test_a_real_automatic_notice_and_its_undo_put_plex_back(lab, monkeypatch):
    ones = sure_episodes(lab, (1, 2))
    done = approvals.send(lab.service, "Community", 1, "sure")
    entries = [
        {
            "change_id": s["change_id"],
            "media_version_id": s["media_version_id"],
            "show": "Community",
            "season": 1,
            "episode": s["episode"],
            "label": s["label"],
            "kinds": s["kinds"],
            "sent_at": lab.clock.now - 60,
            "span_ids": s["span_ids"],
            "origin": "sure",
        }
        for s in reversed(done["sent"])  # newest first, as the contract says
    ]
    monkeypatch.setattr(approvals, "recent_automatic", lambda service, *, since=None, limit=50: entries, raising=False)
    notifier = Notifier(lab.service, lab.alerts, clock=lab.clock, secret=lambda: SECRET, every=0)

    assert notifier.run() == 1
    [call] = lab.post.calls
    assert call["data"].decode().startswith("Reprise sent 2 endings to Plex automatically: Community S1 E1, E2.")
    status, body = notifier.act(token_of(call))

    assert (status, body["message"]) == (200, "Put 2 episodes back as they were in Plex.")
    assert all(_marker_times(lab.plex, "credits", item=ep.number) == [] for ep in ones)
    assert notifier.act(token_of(call))[0] == 410


# ── the real app ─────────────────────────────────────────────────────────────────────────────────────────────────


def test_in_the_real_app_a_button_works_with_no_sign_in_and_csrf_on_and_a_new_secret_ends_it(tmp_path, monkeypatch):
    from media_preview_generator.web import auth, settings_manager
    from media_preview_generator.web.app import create_app
    from media_preview_generator.web.routes import clear_gpu_cache, limiter

    monkeypatch.setattr(auth, "AUTH_FILE", str(tmp_path / "auth.json"))
    monkeypatch.setattr(auth, "CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("WEB_AUTH_TOKEN", raising=False)
    settings_manager.reset_settings_manager()
    clear_gpu_cache()
    (tmp_path / "settings.json").write_text(json.dumps({"setup_complete": True}))
    monkeypatch.setattr("media_preview_generator.web.app._prewarm_caches", lambda: None)
    app = create_app(config_dir=str(tmp_path))
    app.config["TESTING"] = True
    assert app.config["WTF_CSRF_ENABLED"] is True
    monkeypatch.setattr(limiter, "enabled", False)
    try:
        notifier = app.extensions["reprise"].autopilot.notifier
        now = __import__("time").time()
        spent, rotated = (notifier.issue(ACTION_UNDO, {"changes": []}, now) for _ in range(2))
        client = app.test_client()

        first = client.post(f"/api/reprise/notify/act/{spent}")
        again = client.post(f"/api/reprise/notify/act/{spent}")
        get = client.get(f"/api/reprise/notify/act/{spent}")
        auth.regenerate_webhook_secret()
        after_rotation = client.post(f"/api/reprise/notify/act/{rotated}")
        settings_without_sign_in = client.get("/api/reprise/notifications")
        patch_without_csrf = client.patch("/api/reprise/notifications", json={"enabled": False})
    finally:
        settings_manager.reset_settings_manager()

    assert (first.status_code, first.json["message"]) == (409, "Nothing was put back.")  # it ran: no 400/401/403
    assert again.status_code == 410 and "already used" in again.json["message"]
    assert get.status_code == 405
    assert after_rotation.status_code == 403
    assert settings_without_sign_in.status_code == 401
    assert patch_without_csrf.status_code in (400, 401)
    assert ACTION_APPROVE == "approve"
