"""The setup guide's seven steps: what each says for a fresh install, a half-finished one and a working one.

The builder reads saved settings, a few folders, one count in Reprise's own database and in-memory state, so these
tests build real services, registries and folders in ``tmp_path`` and fake only the GPU probe, the job list and the
settings object.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from media_preview_generator.markers.store import MarkerStore
from media_preview_generator.reprise import acceleration, setup_guide
from media_preview_generator.reprise.identity import media_version
from media_preview_generator.reprise.models import PlaybackIdentity
from media_preview_generator.reprise.service import RepriseService
from media_preview_generator.reprise.store import RepriseStore
from media_preview_generator.servers.registry import ServerRegistry

BASE = "http://nas.local:8080"
NO_GPU = {"enabled": True, "active": False, "label": "CPU (no GPU found)", "devices": []}
STEPS = [
    "connect_plex",
    "libraries_and_folders",
    "refresh_library",
    "gpu",
    "online_lookup",
    "webhooks",
    "send_to_plex",
]


class Settings:
    """The two settings the guide reads."""

    def __init__(self, **values):
        self.values = values

    def get(self, key, default=None):
        return self.values.get(key, default)


class Jobs:
    def __init__(self, *jobs):
        self.jobs = list(jobs)

    def get_all_jobs(self):
        return self.jobs


def refresh_job(percent=40.0, status="running"):
    return SimpleNamespace(
        kind="reprise",
        config={"refresh_request": {"server_id": "plex-home", "library_id": "1"}},
        status=SimpleNamespace(value=status),
        progress=SimpleNamespace(percent=percent),
    )


def plex_entry(**over):
    entry = {
        "id": "plex-home",
        "name": "Home Plex",
        "enabled": True,
        "type": "plex",
        "url": "http://127.0.0.1:9",
        "auth": {"token": "saved-token"},
        "libraries": [],
        "path_mappings": [],
        "output": {},
        "server_identity": None,
    }
    entry.update(over)
    return entry


def library(name="TV Shows", library_id="1", paths=("/data/tv",), enabled=True):
    return {"id": library_id, "name": name, "remote_paths": list(paths), "enabled": enabled, "kind": "episode"}


@pytest.fixture
def make_service(tmp_path):
    markers = []

    def build(*entries, jobs=None, writes_enabled=False):
        marker_store = MarkerStore(str(tmp_path / f"markers{len(markers)}.db"))
        markers.append(marker_store)
        service = RepriseService(
            RepriseStore(marker_store), ServerRegistry.from_settings(list(entries)), jobs or Jobs()
        )
        service.publisher = SimpleNamespace(writes_enabled=writes_enabled)
        return service

    yield build
    for marker_store in markers:
        marker_store.close()


@pytest.fixture(autouse=True)
def no_gpu(monkeypatch):
    monkeypatch.setattr(acceleration, "status", lambda: dict(NO_GPU))


def status(service, *, state="pending", wizard=True, seen=None, **settings):
    return setup_guide.build_status(
        service,
        Settings(**settings),
        base_url=BASE,
        guide_state=state,
        wizard_complete=wizard,
        seen_webhooks=set() if seen is None else seen,
    )


def step(payload, step_id):
    return next(s for s in payload["steps"] if s["id"] == step_id)


def register_episode(service, tmp_path, name="episode.mkv"):
    path = tmp_path / name
    path.write_bytes(name.encode())
    size = path.stat().st_size
    identity = PlaybackIdentity("machine", name, "2", "3", f"/library/parts/{name}", size, 5000, "4")
    service.store.register_media(media_version(str(path), 5000, "{}"), identity, server_id="plex-home", metadata={})


class TestFreshInstall:
    def test_seven_steps_in_order_three_required(self, make_service):
        payload = status(make_service())

        assert [s["id"] for s in payload["steps"]] == STEPS
        assert [s["number"] for s in payload["steps"]] == [1, 2, 3, 4, 5, 6, 7]
        assert [s["required"] for s in payload["steps"]] == [True, True, True, False, False, False, False]
        assert (payload["required_total"], payload["required_done"], payload["all_required_done"]) == (3, 0, False)
        assert payload["state"] == "pending"
        assert payload["auto_open"] is True

    def test_step_one_is_the_next_thing_and_opens_the_wizard(self, make_service):
        connect = step(status(make_service(), wizard=False), "connect_plex")

        assert connect["state"] == "todo" and connect["done"] is False
        assert connect["action"] == {"label": "Connect Plex", "href": "/setup"}
        assert "isn't connected to a Plex server yet" in connect["reason"]

    def test_once_the_wizard_is_done_connecting_opens_the_add_dialog(self, make_service):
        assert step(status(make_service(), wizard=True), "connect_plex")["action"]["href"] == "/servers?add=plex"

    def test_the_steps_that_need_plex_wait_and_have_no_button(self, make_service):
        payload = status(make_service())

        for step_id, reason in (
            ("libraries_and_folders", "Connect Plex first."),
            ("refresh_library", "Finish the steps above first."),
            ("send_to_plex", "Connect Plex first."),
        ):
            waiting = step(payload, step_id)
            assert (waiting["state"], waiting["action"], waiting["reason"]) == ("blocked", None, reason)

    def test_optional_steps_that_need_nothing_are_never_blocked(self, make_service):
        payload = status(make_service())

        assert {step(payload, s)["state"] for s in ("gpu", "online_lookup", "webhooks")} <= {"done", "todo"}

    def test_a_skipped_or_finished_guide_never_asks_to_open(self, make_service):
        assert status(make_service(), state="dismissed")["auto_open"] is False
        assert status(make_service(), state="completed")["auto_open"] is False


class TestPlexConnection:
    def test_a_signed_in_enabled_server_is_done_and_named(self, make_service):
        connect = step(status(make_service(plex_entry())), "connect_plex")

        assert connect["state"] == "done" and connect["reason"] == "Connected to Home Plex."
        assert connect["data"]["servers"] == [{"id": "plex-home", "name": "Home Plex"}]

    def test_two_servers_are_named_in_plain_words(self, make_service):
        two = make_service(plex_entry(), plex_entry(id="cabin", name="Cabin Plex"))

        assert step(status(two), "connect_plex")["reason"] == "Connected to Home Plex and Cabin Plex."

    @pytest.mark.parametrize(
        ("over", "words"),
        [
            ({"enabled": False}, "switched off"),
            ({"url": ""}, "no address saved"),
            ({"auth": {}}, "isn't signed in to it any more"),
        ],
    )
    def test_a_server_that_is_added_but_unusable_says_what_is_wrong(self, make_service, over, words):
        connect = step(status(make_service(plex_entry(**over))), "connect_plex")

        assert connect["state"] == "todo" and words in connect["reason"]
        assert connect["action"] == {"label": "Open Connections", "href": "/servers"}

    def test_a_jellyfin_server_alone_is_not_plex(self, make_service):
        jelly = {**plex_entry(id="jf", name="Jelly"), "type": "jellyfin", "auth": {"api_key": "k"}}

        assert step(status(make_service(jelly)), "connect_plex")["state"] == "todo"


class TestLibrariesAndFolders:
    def test_no_library_chosen_opens_the_libraries_tab(self, make_service):
        chosen = step(status(make_service(plex_entry(libraries=[library(enabled=False)]))), "libraries_and_folders")

        assert chosen["state"] == "todo" and "No library is chosen yet" in chosen["reason"]
        assert chosen["action"] == {"label": "Choose libraries", "href": "/servers?edit=plex-home&tab=libraries"}

    def test_a_missing_mount_is_named_with_where_reprise_looks_and_what_to_share(self, make_service, tmp_path):
        mount = tmp_path / "media" / "tv"
        entry = plex_entry(
            libraries=[library(paths=["/data/tv/Anime"])],
            path_mappings=[{"remote_prefix": "/data/tv", "local_prefix": str(mount)}],
        )

        folders = step(status(make_service(entry)), "libraries_and_folders")

        assert folders["state"] == "todo"
        assert folders["reason"].startswith("Reprise can't see the folder your library uses.")
        (line,) = folders["details"]
        assert "TV Shows: Plex calls this folder /data/tv/Anime." in line
        assert f"looks for it at {mount}/Anime and finds nothing there" in line
        assert f"Share the folder that holds it with Reprise at {mount}, read-only." in line
        assert folders["action"] == {"label": "Fix media folders", "href": "/servers?edit=plex-home&tab=paths"}
        assert folders["help"]["href"].endswith("#share-your-media-folders-with-reprise")
        assert folders["data"]["problems"] == 1

    def test_a_folder_with_no_mapping_says_to_share_it_at_the_same_path_or_map_it(self, make_service):
        entry = plex_entry(libraries=[library(paths=["/definitely/not/here"])])

        (line,) = step(status(make_service(entry)), "libraries_and_folders")["details"]

        assert "Reprise looks for the same path and finds nothing there" in line
        assert "Share it with Reprise at /definitely/not/here, read-only" in line
        assert "add a path mapping" in line

    def test_a_shared_mount_without_the_folder_inside_it_points_at_the_name(self, make_service, tmp_path):
        mount = tmp_path / "media"
        (mount / "other").mkdir(parents=True)
        entry = plex_entry(
            libraries=[library(paths=["/data/tv"])],
            path_mappings=[{"remote_prefix": "/data/tv", "local_prefix": str(mount / "tv")}],
        )
        (mount / "tv").mkdir()
        (mount / "tv" / "something.txt").write_text("x")
        entry["libraries"][0]["remote_paths"] = ["/data/tv/Anime"]

        (line,) = step(status(make_service(entry)), "libraries_and_folders")["details"]

        assert "is shared, but that folder isn't inside it" in line

    def test_a_folder_that_is_there_but_empty_is_called_out(self, make_service, tmp_path):
        (tmp_path / "tv").mkdir()
        entry = plex_entry(libraries=[library(paths=[str(tmp_path / "tv")])])

        folders = step(status(make_service(entry)), "libraries_and_folders")

        assert "is there but empty" in folders["details"][0]
        assert folders["data"]["folders"][0]["issue"] == "empty"

    def test_every_chosen_folder_visible_is_done_and_names_the_libraries(self, make_service, tmp_path):
        for name in ("tv", "anime"):
            (tmp_path / name).mkdir()
            (tmp_path / name / "show.mkv").write_text("x")
        entry = plex_entry(
            libraries=[
                library("TV Shows", "1", [str(tmp_path / "tv")]),
                library("Anime", "2", [str(tmp_path / "anime")]),
                library("Movies", "3", ["/nowhere"], enabled=False),
            ]
        )

        folders = step(status(make_service(entry)), "libraries_and_folders")

        assert folders["state"] == "done"
        assert folders["reason"] == "2 libraries chosen: TV Shows and Anime. Reprise can see all 2 of their folders."
        assert folders["action"] is None and folders["details"] == []

    def test_part_of_the_folders_missing_counts_the_missing_ones(self, make_service, tmp_path):
        (tmp_path / "tv").mkdir()
        (tmp_path / "tv" / "show.mkv").write_text("x")
        entry = plex_entry(libraries=[library(paths=[str(tmp_path / "tv"), "/gone/one", "/gone/two"])])

        folders = step(status(make_service(entry)), "libraries_and_folders")

        assert folders["reason"].startswith("Reprise can't see 2 of the 3 folders your libraries use.")
        assert len(folders["details"]) == 2

    def test_the_mapping_wins_over_the_plex_path_when_either_could_exist(self, make_service, tmp_path):
        (tmp_path / "real").mkdir()
        (tmp_path / "real" / "show.mkv").write_text("x")
        entry = plex_entry(
            libraries=[library(paths=["/data/tv"])],
            path_mappings=[{"remote_prefix": "/data/tv", "local_prefix": str(tmp_path / "real")}],
        )

        assert step(status(make_service(entry)), "libraries_and_folders")["state"] == "done"

    def test_blocked_until_plex_is_connected(self, make_service):
        assert step(status(make_service()), "libraries_and_folders")["state"] == "blocked"


@pytest.fixture
def visible_library(tmp_path):
    (tmp_path / "tv").mkdir()
    (tmp_path / "tv" / "show.mkv").write_text("x")
    return plex_entry(libraries=[library(paths=[str(tmp_path / "tv")])])


class TestRefreshLibrary:
    def test_ready_to_refresh_once_the_earlier_steps_are_done(self, make_service, visible_library):
        refresh = step(status(make_service(visible_library)), "refresh_library")

        assert refresh["state"] == "todo"
        assert refresh["action"] == {"label": "Refresh library", "href": "/reprise?refresh=1"}
        assert "doesn't analyze anything" in refresh["reason"]

    def test_waits_while_an_earlier_step_is_open(self, make_service):
        assert step(status(make_service(plex_entry())), "refresh_library")["state"] == "blocked"

    def test_shows_progress_while_a_refresh_runs(self, make_service, visible_library):
        service = make_service(visible_library, jobs=Jobs(refresh_job(41.6)))

        refresh = step(status(service), "refresh_library")

        assert refresh["state"] == "working" and refresh["progress"] == {"percent": 42}
        assert refresh["action"] is None

    def test_a_finished_refresh_job_is_not_running(self, make_service, visible_library):
        service = make_service(visible_library, jobs=Jobs(refresh_job(100, status="completed")))

        assert step(status(service), "refresh_library")["state"] == "todo"

    def test_done_counts_the_files_read_from_plex(self, make_service, visible_library, tmp_path):
        service = make_service(visible_library)
        register_episode(service, tmp_path, "one.mkv")
        register_episode(service, tmp_path, "two.mkv")

        refresh = step(status(service), "refresh_library")

        assert refresh["state"] == "done" and refresh["reason"] == "Reprise has read 2 episode files from Plex."
        assert refresh["data"]["episode_files"] == 2

    def test_a_single_file_is_not_pluralised(self, make_service, visible_library, tmp_path):
        service = make_service(visible_library)
        register_episode(service, tmp_path)

        assert step(status(service), "refresh_library")["reason"] == "Reprise has read 1 episode file from Plex."

    def test_all_three_required_done_closes_the_guide_and_stops_asking(self, make_service, visible_library, tmp_path):
        service = make_service(visible_library)
        register_episode(service, tmp_path)

        payload = status(service)

        assert (payload["required_done"], payload["all_required_done"], payload["auto_open"]) == (3, True, False)
        assert setup_guide.required_open(service, True) is False

    def test_the_count_walks_no_catalogue_and_parses_no_document(self, make_service, visible_library, tmp_path):
        service = make_service(visible_library)
        register_episode(service, tmp_path)
        service.store = MagicMock(wraps=service.store)

        status(service)

        service.store.media_file_count.assert_called_once()
        service.store.list_media.assert_not_called()
        service.store.catalogue.assert_not_called()


class TestGpu:
    @pytest.mark.parametrize(
        ("probe", "state", "words"),
        [
            (
                {
                    "enabled": True,
                    "active": True,
                    "label": "GPU: NVIDIA Tesla P40 (NVDEC decode)",
                    "devices": [{"name": "NVIDIA Tesla P40", "method": "NVDEC", "state": "ready"}],
                },
                "done",
                "Analysis uses GPU: NVIDIA Tesla P40 (NVDEC decode).",
            ),
            (NO_GPU, "todo", "No graphics card was found"),
            (
                {
                    "enabled": False,
                    "active": False,
                    "label": "CPU (GPU decode is turned off)",
                    "devices": [{"name": "NVIDIA Tesla P40", "method": "NVDEC", "state": "off"}],
                },
                "todo",
                "GPU decoding is turned off",
            ),
            (
                {
                    "enabled": True,
                    "active": False,
                    "label": "CPU (checking GPU decode)",
                    "devices": [{"name": "NVIDIA Tesla P40", "method": "NVDEC", "state": "checking"}],
                },
                "working",
                "is testing it now",
            ),
            (
                {
                    "enabled": True,
                    "active": False,
                    "label": "CPU (GPU decode unavailable)",
                    "devices": [{"name": "Intel UHD", "method": "VAAPI", "state": "unavailable"}],
                },
                "todo",
                "can't use it",
            ),
        ],
    )
    def test_what_is_found_and_whether_it_is_used(self, make_service, monkeypatch, probe, state, words):
        monkeypatch.setattr(acceleration, "status", lambda: probe)

        gpu = step(status(make_service()), "gpu")

        assert gpu["state"] == state and words in gpu["reason"]

    def test_each_device_is_listed_with_its_state(self, make_service, monkeypatch):
        probe = {
            "enabled": True,
            "active": True,
            "label": "GPU: A (NVDEC decode)",
            "devices": [
                {"name": "A", "method": "NVDEC", "state": "ready"},
                {"name": "B", "method": "VAAPI", "state": "unavailable"},
            ],
        }
        monkeypatch.setattr(acceleration, "status", lambda: probe)

        details = step(status(make_service()), "gpu")["details"]

        assert details[0] == "A (NVDEC decode): ready"
        assert details[1].startswith("B (VAAPI decode): can't decode your video exactly as the processor does")

    def test_none_found_links_to_the_gpu_guide_and_has_no_button(self, make_service):
        gpu = step(status(make_service()), "gpu")

        assert gpu["action"] is None
        assert gpu["help"] == {
            "label": "How to add a graphics card",
            "href": "https://github.com/nphil/reprise#optional-gpu-decoding",
        }

    def test_a_probe_that_raises_costs_one_step_not_the_page(self, make_service, monkeypatch):
        def boom():
            raise RuntimeError("nvidia-smi hung")

        monkeypatch.setattr(acceleration, "status", boom)

        payload = status(make_service())

        assert step(payload, "gpu")["state"] == "todo"
        assert "couldn't check this just now" in step(payload, "gpu")["reason"]
        assert len(payload["steps"]) == 7 and step(payload, "connect_plex")["state"] == "todo"


class TestOnlineLookup:
    def test_on_by_default_and_says_what_is_sent(self, make_service):
        service = make_service()
        service.online_lookups_enabled = lambda: True

        lookup = step(status(service), "online_lookup")

        assert lookup["state"] == "done" and lookup["required"] is False
        assert "sends only the show's id, the season and episode numbers and the file's length" in lookup["reason"]
        assert lookup["action"] == {"label": "Change in Library", "href": "/reprise"}

    def test_off_says_so_and_stays_a_choice(self, make_service):
        service = make_service()
        service.online_lookups_enabled = lambda: False

        lookup = step(status(service), "online_lookup")

        assert lookup["state"] == "todo" and lookup["reason"].startswith("Off.")
        assert lookup["data"] == {"enabled": False}


class TestWebhooks:
    def test_the_addresses_are_built_from_where_the_owner_opened_reprise(self, make_service):
        hooks = step(status(make_service()), "webhooks")

        assert hooks["values"] == [
            {"label": "Sonarr address", "value": f"{BASE}/api/webhooks/sonarr"},
            {"label": "Radarr address", "value": f"{BASE}/api/webhooks/radarr"},
        ]
        assert hooks["action"] == {"label": "Open Automation", "href": "/automation#section-webhooks-sonarr-radarr"}

    def test_the_secret_itself_is_never_in_the_answer_only_where_to_find_it(self, make_service):
        hooks = step(status(make_service(), webhook_secret="s3cret-value-123"), "webhooks")

        assert "s3cret-value-123" not in repr(hooks)
        assert any("Settings, then Connections, then Webhook secret" in line for line in hooks["details"])

    def test_not_connected_is_optional_and_says_review_only_can_leave_events_off(self, make_service):
        hooks = step(status(make_service()), "webhooks")

        assert hooks["state"] == "todo" and hooks["required"] is False
        assert "Skip this if you don't use Sonarr or Radarr" in hooks["reason"]
        assert any("leave its import and upgrade events off" in line for line in hooks["details"])

    @pytest.mark.parametrize(
        ("seen", "reason"),
        [
            ({"sonarr"}, "Sonarr has already contacted Reprise, so the connection works."),
            ({"radarr", "plex"}, "Radarr has already contacted Reprise, so the connection works."),
            ({"sonarr", "radarr"}, "Sonarr and Radarr have already contacted Reprise, so the connection works."),
        ],
    )
    def test_a_sender_that_has_called_makes_it_done(self, make_service, seen, reason):
        hooks = step(status(make_service(), seen=seen), "webhooks")

        assert hooks["state"] == "done" and hooks["reason"] == reason

    def test_plex_or_custom_senders_alone_do_not_count(self, make_service):
        assert step(status(make_service(), seen={"plex", "custom"}), "webhooks")["state"] == "todo"

    def test_switched_off_webhooks_are_said_out_loud(self, make_service):
        hooks = step(status(make_service(), webhook_enabled=False), "webhooks")

        assert hooks["data"]["enabled"] is False
        assert any("switched off under Automation" in line for line in hooks["details"])


def plex_folder(tmp_path, *, database=True, preferences="machine-1"):
    folder = tmp_path / "plexcfg"
    folder.mkdir(exist_ok=True)
    if database:
        (folder / "Plug-in Support" / "Databases").mkdir(parents=True, exist_ok=True)
        (folder / "Plug-in Support" / "Databases" / "com.plexapp.plugins.library.db").write_bytes(b"")
    if preferences:
        (folder / "Preferences.xml").write_text(f'<Preferences ProcessedMachineIdentifier="{preferences}"/>')
    return folder


class TestSendToPlex:
    def send(self, make_service, entry, **kwargs):
        return step(status(make_service(entry, **kwargs)), "send_to_plex")

    def test_it_is_optional_and_review_only_is_the_stated_default(self, make_service):
        sending = self.send(make_service, plex_entry())

        assert sending["required"] is False
        assert sending["reason"].startswith("Review only is the default, so Reprise changes nothing in Plex.")

    def test_without_plex_it_waits(self, make_service):
        assert step(status(make_service()), "send_to_plex")["state"] == "blocked"

    def test_on_is_done_and_says_confirmation_is_still_needed(self, make_service):
        sending = self.send(make_service, plex_entry(), writes_enabled=True)

        assert sending["state"] == "done"
        assert sending["reason"] == "Sending to Plex is on. Every change still needs your confirmation."
        assert sending["action"] == {"label": "Open Sharing", "href": "/reprise/sharing"}

    def test_no_folder_saved_says_to_set_it_and_share_the_whole_folder(self, make_service):
        sending = self.send(make_service, plex_entry())

        assert sending["state"] == "todo"
        assert "the Plex config folder isn't set in Reprise" in sending["details"][0]
        assert sending["values"] == [
            {"label": "Share Plex's whole config folder at", "value": "/plex"},
            {"label": "Environment variable", "value": "PLEX_CONFIG_FOLDER=/plex"},
        ]
        assert sending["help"]["href"].endswith("#turn-on-sending-to-plex")

    def test_a_folder_that_is_not_mounted_names_it(self, make_service, tmp_path):
        folder = tmp_path / "not-mounted"

        sending = self.send(make_service, plex_entry(output={"plex_config_folder": str(folder)}))

        assert sending["details"][0].startswith(f"Home Plex: {folder} isn't there inside Reprise.")
        assert sending["values"][0]["value"] == str(folder)

    def test_only_the_preview_folder_shared_is_the_common_review_only_layout(self, make_service, tmp_path):
        folder = tmp_path / "plexcfg"
        (folder / "Media" / "localhost").mkdir(parents=True)

        sending = self.send(make_service, plex_entry(output={"plex_config_folder": str(folder)}))

        (line,) = sending["details"]
        assert "Plex's database isn't in it" in line and "just Media/localhost" in line
        assert str(folder / "Plug-in Support" / "Databases" / "com.plexapp.plugins.library.db") in line

    def test_a_database_without_preferences_cannot_be_told_from_another_plex(self, make_service, tmp_path):
        folder = plex_folder(tmp_path, preferences=None)

        sending = self.send(make_service, plex_entry(output={"plex_config_folder": str(folder)}))

        assert "Preferences.xml isn't in" in sending["details"][0]

    def test_a_folder_that_belongs_to_another_plex_is_refused(self, make_service, tmp_path):
        folder = plex_folder(tmp_path, preferences="someone-else")

        sending = self.send(
            make_service, plex_entry(output={"plex_config_folder": str(folder)}, server_identity="machine-1")
        )

        assert "belongs to a different Plex server" in sending["details"][0]

    def test_a_read_only_share_says_to_share_it_read_write(self, make_service, tmp_path, monkeypatch):
        folder = plex_folder(tmp_path)
        monkeypatch.setattr(setup_guide.os, "access", lambda path, mode: False)

        sending = self.send(make_service, plex_entry(output={"plex_config_folder": str(folder)}))

        assert "isn't allowed to write to it" in sending["details"][0]
        assert "PUID 99 and PGID 100" in sending["details"][0]

    def test_a_network_share_is_refused_with_the_reason(self, make_service, tmp_path, monkeypatch):
        folder = plex_folder(tmp_path)
        monkeypatch.setattr(setup_guide, "filesystem_type", lambda path: "nfs4")

        sending = self.send(make_service, plex_entry(output={"plex_config_folder": str(folder)}))

        assert "on a network share (nfs4)" in sending["details"][0]

    def test_when_everything_is_in_place_it_says_so_and_asks_for_a_backup(self, make_service, tmp_path):
        folder = plex_folder(tmp_path)

        sending = self.send(
            make_service, plex_entry(output={"plex_config_folder": str(folder)}, server_identity="machine-1")
        )

        assert sending["state"] == "todo" and sending["details"] == []
        assert (
            "Reprise can see Plex's database" in sending["reason"]
            and "Back up Plex's database first" in sending["reason"]
        )
        assert sending["data"]["servers"][0]["problem"] is None

    def test_looking_never_opens_or_changes_plex_s_files(self, make_service, tmp_path):
        folder = plex_folder(tmp_path)
        database = folder / "Plug-in Support" / "Databases" / "com.plexapp.plugins.library.db"
        before = (sorted(os.listdir(database.parent)), database.stat().st_mtime_ns, database.stat().st_size)

        self.send(make_service, plex_entry(output={"plex_config_folder": str(folder)}))

        assert (sorted(os.listdir(database.parent)), database.stat().st_mtime_ns, database.stat().st_size) == before


class TestSwitchAndGuideAgree:
    """The Sharing switch refuses with the sentence the guide shows: one check, one vocabulary."""

    @pytest.mark.parametrize("layout", ["unset", "missing", "partial"])
    def test_the_refusal_names_the_same_folder_problem(self, make_service, tmp_path, layout):
        output = {}
        if layout == "missing":
            output = {"plex_config_folder": str(tmp_path / "absent")}
        elif layout == "partial":
            (tmp_path / "plexcfg" / "Media" / "localhost").mkdir(parents=True)
            output = {"plex_config_folder": str(tmp_path / "plexcfg")}
        service = make_service(plex_entry(output=output))
        guide_line = step(status(service), "send_to_plex")["details"][0]

        with pytest.raises(ValueError) as refused:
            service.set_writes_enabled(True)

        assert str(refused.value).startswith("Sending to Plex needs Plex's database, and Reprise can't see it: ")
        assert str(refused.value).endswith(guide_line.removeprefix("Home Plex: "))
        assert service.publisher.writes_enabled is False

    def test_no_plex_server_at_all_still_refuses_in_plain_words(self, make_service):
        service = make_service()

        with pytest.raises(ValueError, match="Reprise can't see it: no Plex server is connected."):
            service.set_writes_enabled(True)
