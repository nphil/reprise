from types import SimpleNamespace

import pytest
from loguru import logger

from media_preview_generator.markers.store import MarkerStore
from media_preview_generator.reprise.alerts import NTFY_TOPIC, NTFY_URL, TOKEN_FILE, Alerts
from media_preview_generator.reprise.store import RepriseStore

TOKEN = "tk_s3cret-TOKEN-value"


class Response:
    def __init__(self, status_code):
        self.status_code = status_code


class FakePost:
    """Records every call; ``outcome`` is a status code or an exception to raise."""

    def __init__(self):
        self.calls = []
        self.outcome = 200

    def __call__(self, url, *, data, headers, timeout):
        self.calls.append({"url": url, "data": data, "headers": dict(headers), "timeout": timeout})
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return Response(self.outcome)


class Clock:
    def __init__(self, now=1_700_000_000):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def env(tmp_path):
    marker = MarkerStore(str(tmp_path / "markers.db"))
    store = RepriseStore(marker)
    (tmp_path / TOKEN_FILE).write_text(f"  {TOKEN}\n")
    post, clock, switch = FakePost(), Clock(), {"on": True}

    def make():
        return Alerts(store, config_dir=str(tmp_path), enabled=lambda: switch["on"], post=post, clock=clock)

    yield SimpleNamespace(store=store, dir=tmp_path, post=post, clock=clock, switch=switch, make=make)
    marker.close()


@pytest.fixture
def logs():
    lines = []
    sink = logger.add(lines.append, level="DEBUG")
    yield lines
    logger.remove(sink)


def test_new_problem_sends_one_alert_and_repeat_is_silent(env):
    alerts = env.make()
    assert alerts.problem("plex", "Plex is down", "Plex does not answer.") is True
    assert alerts.problem("plex", "Plex is down", "Different text now.") is False
    assert len(env.post.calls) == 1
    call = env.post.calls[0]
    assert call["url"] == f"{NTFY_URL}/{NTFY_TOPIC}"
    assert call["data"] == b"Plex does not answer."
    assert call["timeout"] == 10
    assert call["headers"]["Title"] == "Plex is down"
    assert call["headers"]["Priority"] == "high"
    assert call["headers"]["Tags"] == "warning"
    assert alerts.is_open("plex") and alerts.open_keys() == ["plex"]


def test_open_keys_sorted_and_unknown_key_is_not_open(env):
    alerts = env.make()
    alerts.problem("b", "B", "b")
    alerts.problem("a", "A", "a")
    assert alerts.open_keys() == ["a", "b"]
    assert not alerts.is_open("c")
    assert alerts.resolved("c") is False
    assert len(env.post.calls) == 2


def test_resolved_follow_up_only_after_a_sent_alert(env):
    alerts = env.make()
    alerts.problem("plex", "Plex is down", "msg")
    assert alerts.resolved("plex") is True
    assert not alerts.is_open("plex")
    follow = env.post.calls[1]
    assert follow["headers"]["Title"] == "Resolved: Plex is down"
    assert follow["data"] == b"This is fixed now. Nothing more to do."
    assert follow["headers"]["Priority"] == "default"
    assert follow["headers"]["Tags"] == "white_check_mark"
    assert alerts.resolved("plex") is False
    assert len(env.post.calls) == 2


def test_resolved_uses_custom_message(env):
    alerts = env.make()
    alerts.problem("plex", "Plex is down", "msg")
    alerts.resolved("plex", "Plex is back.")
    assert env.post.calls[1]["data"] == b"Plex is back."


def test_no_follow_up_for_a_problem_that_was_never_sent(env):
    env.switch["on"] = False
    alerts = env.make()
    assert alerts.problem("plex", "Plex is down", "msg") is False
    env.switch["on"] = True
    assert alerts.resolved("plex") is False
    assert env.post.calls == []
    assert not alerts.is_open("plex")


def test_reopening_after_resolve_sends_again(env):
    alerts = env.make()
    alerts.problem("plex", "Plex is down", "first")
    alerts.resolved("plex")
    assert alerts.problem("plex", "Plex is down again", "second") is True
    assert alerts.is_open("plex")
    assert [c["data"] for c in env.post.calls] == [
        b"first",
        b"This is fixed now. Nothing more to do.",
        b"second",
    ]


def test_switched_off_sends_nothing_keeps_problem_and_sends_once_when_on(env):
    env.switch["on"] = False
    alerts = env.make()
    assert alerts.problem("plex", "Plex is down", "msg") is False
    assert alerts.problem("plex", "Plex is down", "msg") is False
    assert env.post.calls == [] and alerts.is_open("plex") and alerts.last() is None
    env.switch["on"] = True
    assert alerts.problem("plex", "Plex is down", "msg") is True
    assert alerts.problem("plex", "Plex is down", "msg") is False
    assert len(env.post.calls) == 1


def test_failed_send_is_retried_only_after_ten_minutes(env):
    alerts = env.make()
    env.post.outcome = 503
    assert alerts.problem("plex", "Plex is down", "msg") is False
    assert alerts.is_open("plex")
    env.clock.now += 599
    assert alerts.problem("plex", "Plex is down", "msg") is False
    assert len(env.post.calls) == 1
    env.clock.now += 1
    env.post.outcome = 200
    assert alerts.problem("plex", "Plex is down", "msg") is True
    assert len(env.post.calls) == 2
    env.clock.now += 5000
    assert alerts.problem("plex", "Plex is down", "msg") is False
    assert len(env.post.calls) == 2
    with env.store.marker_store._reading() as conn:
        assert conn.execute("SELECT failures FROM reprise_alerts").fetchone()["failures"] == 0


def test_repeated_failures_back_off_each_time(env):
    alerts = env.make()
    env.post.outcome = OSError("down")
    alerts.problem("plex", "Plex is down", "msg")
    env.clock.now += 600
    alerts.problem("plex", "Plex is down", "msg")
    env.clock.now += 599
    alerts.problem("plex", "Plex is down", "msg")
    assert len(env.post.calls) == 2
    with env.store.marker_store._reading() as conn:
        assert conn.execute("SELECT failures FROM reprise_alerts").fetchone()["failures"] == 2


def test_state_survives_a_new_alerts_on_the_same_store(env):
    env.make().problem("plex", "Plex is down", "msg")
    again = env.make()
    assert again.is_open("plex") and again.open_keys() == ["plex"]
    assert again.problem("plex", "Plex is down", "msg") is False
    assert again.last()["key"] == "plex"
    assert again.resolved("plex") is True
    assert len(env.post.calls) == 2


def test_last_reports_the_newest_sent_alert(env):
    alerts = env.make()
    assert alerts.last() is None
    alerts.problem("plex", "Plex is down", "msg")
    assert alerts.last() == {"kind": "problem", "key": "plex", "title": "Plex is down", "at": "2023-11-14T22:13:20Z"}
    env.clock.now += 60
    alerts.resolved("plex")
    assert alerts.last() == {
        "kind": "resolved",
        "key": "plex",
        "title": "Resolved: Plex is down",
        "at": "2023-11-14T22:14:20Z",
    }


def test_last_ignores_failed_sends(env):
    alerts = env.make()
    env.post.outcome = 500
    alerts.problem("plex", "Plex is down", "msg")
    assert alerts.last() is None


def test_log_keeps_only_the_newest_fifty(env):
    alerts = env.make()
    for n in range(60):
        alerts.problem(f"k{n}", f"Problem {n}", "msg")
    with env.store.marker_store._reading() as conn:
        assert conn.execute("SELECT COUNT(*) FROM reprise_alert_log").fetchone()[0] == 50
    assert alerts.last()["key"] == "k59"


def test_test_alert_works_while_disabled_and_is_logged_as_test(env):
    env.switch["on"] = False
    alerts = env.make()
    assert alerts.test() == {"sent": True, "message": "Test alert sent. Check your phone."}
    assert env.post.calls[0]["headers"]["Title"] == "Test alert from Reprise"
    last = alerts.last()
    assert last["kind"] == "test" and last["title"] == "Test alert from Reprise"
    assert alerts.open_keys() == []


def test_missing_or_empty_token_file_is_not_sent(env):
    alerts = env.make()
    expected = "No ntfy token was found. It belongs in the file ntfy-token in Reprise's config folder."
    (env.dir / TOKEN_FILE).unlink()
    assert alerts.test() == {"sent": False, "message": expected}
    assert alerts.problem("plex", "Plex is down", "msg") is False
    (env.dir / TOKEN_FILE).write_text("  \n")
    assert alerts.test() == {"sent": False, "message": expected}
    assert env.post.calls == []
    assert alerts.last() is None and alerts.is_open("plex")


def test_refused_and_unreachable_are_plain_failures_that_never_raise(env):
    alerts = env.make()
    env.post.outcome = 403
    assert alerts.test() == {"sent": False, "message": "The alert server refused the message (code 403)."}
    env.post.outcome = ConnectionError("boom")
    assert alerts.test() == {"sent": False, "message": "The alert server could not be reached."}
    assert alerts.problem("plex", "Plex is down", "msg") is False
    env.post.outcome = RuntimeError("anything")
    assert alerts.resolved("plex") is False
    assert alerts.last() is None


def test_token_never_appears_in_logs_results_or_tables(env, logs):
    alerts = env.make()
    results = [alerts.problem("plex", "Plex is down", "msg"), alerts.test(), alerts.last()]
    env.post.outcome = 401
    results += [alerts.test(), alerts.problem("other", "Other", "msg")]
    env.post.outcome = ConnectionError(f"Bearer {TOKEN}")
    results += [alerts.test()]
    env.post.outcome = 200
    results += [alerts.resolved("plex"), alerts.last(), alerts.open_keys()]
    tables = ("reprise_alerts", "reprise_alert_log")
    with env.store.marker_store._reading() as conn:
        dump = [tuple(row) for table in tables for row in conn.execute(f"SELECT * FROM {table}")]
    assert logs, "failures should be logged"
    for text in (*map(str, logs), str(results), str(dump), repr(alerts.__dict__)):
        assert TOKEN not in text


def test_authorization_header_is_bearer_and_token_is_read_fresh_each_send(env):
    alerts = env.make()
    alerts.test()
    (env.dir / TOKEN_FILE).write_text("tk_new-value\n")
    alerts.test()
    assert env.post.calls[0]["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert env.post.calls[1]["headers"]["Authorization"] == "Bearer tk_new-value"


def test_title_is_ascii_and_at_most_sixty_characters_and_body_is_truncated(env):
    alerts = env.make()
    alerts.problem("plex", "Caf\u00e9 \u2014 " + "x" * 100, "caf\u00e9 " + "y" * 2000)
    call = env.post.calls[0]
    title = call["headers"]["Title"]
    assert title.isascii() and len(title) == 60 and title.startswith("Caf? ? ")
    assert len(call["data"].decode("utf-8")) == 1000
    assert call["data"].decode("utf-8").startswith("caf\u00e9 ")
