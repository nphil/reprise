"""The owner's autopilot switches and test alert over the real blueprint, against a small fake autopilot."""

import json

import pytest
from flask import Flask

from media_preview_generator.markers.store import MarkerStore
from media_preview_generator.reprise.service import RepriseService
from media_preview_generator.reprise.store import RepriseStore
from media_preview_generator.servers.registry import ServerRegistry
from media_preview_generator.web.routes.api_reprise import create_reprise_blueprint
from tests.auth_helpers import sign_in

NOT_AVAILABLE = "Automatic work is not available right now."
STATUS = {
    "queue_length": 7,
    "last_reconcile": "2026-11-22T10:00:00Z",
    "last_alert": {"kind": "problem", "title": "Plex is not answering", "at": "2026-11-22T09:00:00Z"},
    "auto_send_blocked": "Sending to Plex is switched off.",
}


class FakeAutopilot:
    """The contract the settings page relies on."""

    def __init__(self):
        self.values = {"auto_send": False, "backfill": True, "alerts": False}
        self.updates = []
        self.test_alerts = 0

    def settings(self):
        return dict(self.values)

    def update_settings(self, values):
        if set(values) - set(self.values) or any(type(v) is not bool for v in values.values()):
            raise ValueError("Invalid setting")
        self.updates.append(dict(values))
        self.values.update(values)

    def status(self):
        return dict(STATUS)

    def send_test_alert(self):
        self.test_alerts += 1
        return {"sent": True, "message": "A test alert was sent to your phone."}


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A client on the real blueprint with the owner signed in, plus the service the fake autopilot hangs on."""
    monkeypatch.setattr("media_preview_generator.web.auth.is_auth_external", lambda: True)
    # The hardware and online-lookup answers come from settings and ffmpeg probes that are not under test here.
    monkeypatch.setattr("media_preview_generator.reprise.acceleration.status", lambda: {"label": "CPU"})
    monkeypatch.setattr("media_preview_generator.reprise.acceleration.vram_cap_mb", lambda: 4096)
    markers = MarkerStore(str(tmp_path / "markers.db"))
    service = RepriseService(RepriseStore(markers), ServerRegistry(), None)
    online = {"on": False}
    service.online_lookups_enabled = lambda: online["on"]
    service.set_online_lookups_enabled = lambda enabled: online.update(on=enabled)
    service.autopilot = FakeAutopilot()
    app = Flask(__name__)
    app.secret_key = "throwaway"
    app.register_blueprint(create_reprise_blueprint(service))
    yield app.test_client(), service, online
    markers.close()


def test_get_includes_the_switches_and_the_status(env):
    client, _service, _online = env
    answer = client.get("/api/reprise/settings")
    assert answer.status_code == 200
    assert answer.json["autopilot"] == {"auto_send": False, "backfill": True, "alerts": False, "status": STATUS}


def test_get_without_an_autopilot_says_null(env):
    client, service, _online = env
    service.autopilot = None
    answer = client.get("/api/reprise/settings")
    assert answer.status_code == 200
    assert answer.json["autopilot"] is None
    del service.autopilot  # an older service that never had the attribute
    assert client.get("/api/reprise/settings").json["autopilot"] is None


@pytest.mark.parametrize("key", ["auto_send", "backfill", "alerts"])
def test_each_switch_reaches_the_autopilot_alone(env, key):
    client, service, _online = env
    answer = client.patch("/api/reprise/settings", json={key: True})
    assert answer.status_code == 200
    assert service.autopilot.updates == [{key: True}]
    assert answer.json["autopilot"][key] is True


def test_several_switches_are_applied_together(env):
    client, service, _online = env
    assert client.patch("/api/reprise/settings", json={"auto_send": True, "alerts": True}).status_code == 200
    assert service.autopilot.updates == [{"auto_send": True, "alerts": True}]


@pytest.mark.parametrize("value", ["yes", 1, 0, None, "true", [True]])
def test_a_switch_that_is_not_a_real_boolean_is_refused_and_nothing_changes(env, value):
    client, service, _online = env
    before = service.autopilot.settings()
    answer = client.patch("/api/reprise/settings", json={"backfill": value})
    assert answer.status_code == 400
    assert service.autopilot.updates == []
    assert service.autopilot.settings() == before


def test_a_bad_switch_stops_the_good_one_sent_with_it(env):
    client, service, online = env
    answer = client.patch("/api/reprise/settings", json={"online_lookups": True, "alerts": "yes"})
    assert answer.status_code == 400
    assert service.autopilot.updates == []
    assert online["on"] is False


def test_an_unknown_key_is_still_refused(env):
    client, service, _online = env
    for body in ({"auto_send": True, "colour": "red"}, {"colour": "red"}, {}):
        assert client.patch("/api/reprise/settings", json=body).status_code == 400
    assert service.autopilot.updates == []


def test_the_new_switches_without_an_autopilot_get_a_plain_answer(env):
    client, service, online = env
    service.autopilot = None
    for key in ("auto_send", "backfill", "alerts"):
        answer = client.patch("/api/reprise/settings", json={key: True})
        assert answer.status_code == 400
        assert answer.json == {"error": NOT_AVAILABLE}
    # Nothing sent alongside them is applied either.
    assert client.patch("/api/reprise/settings", json={"online_lookups": True, "alerts": True}).status_code == 400
    assert online["on"] is False


def test_the_older_settings_still_work_with_and_without_an_autopilot(env):
    client, service, online = env
    answer = client.patch("/api/reprise/settings", json={"online_lookups": True})
    assert answer.status_code == 200
    assert answer.json["online_lookups"] is True
    assert service.autopilot.updates == [], "a PATCH without the new keys never touches the autopilot"
    service.autopilot = None
    answer = client.patch("/api/reprise/settings", json={"online_lookups": False})
    assert answer.status_code == 200
    assert answer.json["online_lookups"] is False
    assert answer.json["autopilot"] is None
    assert online["on"] is False
    assert client.patch("/api/reprise/settings", json={"online_lookups": "on"}).status_code == 400


def test_the_test_alert_returns_the_autopilot_s_answer(env):
    client, service, _online = env
    answer = client.post("/api/reprise/alerts/test")
    assert answer.status_code == 200
    assert answer.json == {"sent": True, "message": "A test alert was sent to your phone."}
    assert service.autopilot.test_alerts == 1


def test_the_test_alert_without_an_autopilot_says_so(env):
    client, service, _online = env
    service.autopilot = None
    answer = client.post("/api/reprise/alerts/test")
    assert answer.status_code == 400
    assert answer.json == {"error": NOT_AVAILABLE}


def test_nobody_signed_out_can_read_the_switches_or_send_a_test_alert(env, monkeypatch):
    client, service, _online = env
    monkeypatch.setattr("media_preview_generator.web.auth.is_auth_external", lambda: False)
    assert client.get("/api/reprise/settings").status_code == 401
    assert client.patch("/api/reprise/settings", json={"alerts": True}).status_code == 401
    assert client.post("/api/reprise/alerts/test").status_code == 401
    assert service.autopilot.updates == []
    assert service.autopilot.test_alerts == 0


def test_the_real_app_starts_automatic_mode_and_its_sharing_page_holds_the_panel(tmp_path, monkeypatch):
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
    app.config["WTF_CSRF_ENABLED"] = False
    monkeypatch.setattr(limiter, "enabled", False)
    try:
        client = sign_in(app.test_client())
        response = client.get("/reprise/sharing")
        assert response.status_code == 200
        html = response.get_data(as_text=True)
        shown = client.get("/api/reprise/settings").json["autopilot"]
        changed = client.patch("/api/reprise/settings", json={"auto_send": False, "alerts": False}).json["autopilot"]
        stored = settings_manager.get_settings_manager(str(tmp_path))
    finally:
        settings_manager.reset_settings_manager()
    # Everything is on until the owner turns a switch off; each switch is kept in its own setting.
    assert (shown["auto_send"], shown["backfill"], shown["alerts"]) == (True, True, True)
    # A new install has not allowed sending to Plex yet, and the page says so.
    assert {k: v for k, v in shown["status"].items() if k != "notifications"} == {
        "queue_length": 0,
        "last_reconcile": None,
        "last_alert": None,
        "auto_send_blocked": "Sending to Plex is switched off, so nothing is sent.",
    }
    assert shown["status"]["notifications"]["enabled"] is True
    assert (changed["auto_send"], changed["backfill"], changed["alerts"]) == (False, True, False)
    assert changed["status"]["auto_send_blocked"] is None  # nothing to report while the switch is off
    assert (stored.get("reprise_auto_send"), stored.get("reprise_alerts_enabled")) == (False, False)
    for landmark in ("rpAutoSend", "rpBackfill", "rpAlerts", "rpTestAlert", "rpAlertResult", "rpAutoStatus"):
        assert f'id="{landmark}"' in html, landmark
    # The panel follows the Plex switch and stays hidden until the settings say there is an autopilot.
    assert html.index('id="rpWritesEnabled"') < html.index('id="rpAutoPanel"') < html.index('id="rpClientForm"')
    assert '<section class="rp-panel rp-auto" id="rpAutoPanel" aria-labelledby="rpAutoTitle" hidden>' in html
