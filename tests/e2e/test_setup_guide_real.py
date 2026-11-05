"""E2E against the real backend: the Setup guide with nothing mocked.

``test_setup_guide.py`` mocks ``GET /api/reprise/setup-guide`` and checks what the page does with each answer. These
tests boot throwaway copies of the app (as ``test_nodes_real_backend.py`` does) and check the other half: that what the
real service reports, and where the real pages send a person, agree with it.

* A brand-new install. The account made through the real Welcome form opens the guide with nothing done and one button,
  and that button opens the setup wizard. Home keeps coming back to the guide until a required step is done.
* An install from before the guide existed (an account with no saved choice, Plex connected, libraries read). Signing in
  opens the Library, home never offers the guide, the guide page shows the three required steps done, and nothing is
  written to the account file.
* Plex connected but one media folder not shared. The step names that folder, its button opens that server's Path
  mappings tab on the real Connections page, and Skip for now stops home from opening the guide.
"""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Generator
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from .conftest import _start_app, get_free_port

pytestmark = pytest.mark.e2e

USERNAME = "e2e-owner"
PASSWORD = "e2e-owner-password"


class Copies:
    """Throwaway copies of the app, each on its own config folder; every one is stopped when the test ends."""

    def __init__(self) -> None:
        self._started: dict[str, subprocess.Popen] = {}

    def start(self, config: Path) -> str:
        port = get_free_port()
        url = f"http://localhost:{port}"
        self._started[url] = _start_app(str(config), port)
        return url

    def stop(self, url: str) -> None:
        proc = self._started.pop(url)
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)

    def stop_all(self) -> None:
        for url in list(self._started):
            self.stop(url)


@pytest.fixture
def copies() -> Generator[Copies, None, None]:
    running = Copies()
    yield running
    running.stop_all()


def _create_account(page: Page, url: str) -> None:
    """The first run: the Welcome form makes the one owner account and signs in."""
    page.goto(f"{url}/welcome")
    page.fill("#username", USERNAME)
    page.fill("#password", PASSWORD)
    page.fill("#confirm_password", PASSWORD)
    page.click("form[data-auth-form=create] button[type=submit]")


def _sign_in(page: Page, url: str) -> None:
    page.goto(f"{url}/login")
    page.fill("#username", USERNAME)
    page.fill("#password", PASSWORD)
    page.click("form button[type=submit]")


def _plex_server(media: Path) -> dict:
    """Home Plex as the wizard saves it: signed in, two libraries, Plex's /data/... paths mapped onto folders under ``media``."""
    return {
        "id": "plex-home",
        "name": "Home Plex",
        "type": "plex",
        "enabled": True,
        "url": "http://127.0.0.1:9",
        "auth": {"token": "saved-token"},
        "libraries": [
            {"id": "1", "name": "TV Shows", "remote_paths": ["/data/tv"], "enabled": True, "kind": "episode"},
            {"id": "2", "name": "Anime", "remote_paths": ["/data/anime"], "enabled": True, "kind": "episode"},
        ],
        "path_mappings": [
            {"remote_prefix": "/data/tv", "local_prefix": str(media / "tv")},
            {"remote_prefix": "/data/anime", "local_prefix": str(media / "anime")},
        ],
        "output": {},
    }


def _read_episodes_into_reprise(config: Path, media: Path) -> None:
    """Three episodes in Reprise's own database, as a finished library refresh leaves them (the app must be stopped)."""
    from media_preview_generator.markers.store import MarkerStore
    from media_preview_generator.reprise.identity import media_version
    from media_preview_generator.reprise.models import PlaybackIdentity
    from media_preview_generator.reprise.store import RepriseStore

    markers = MarkerStore(str(config / "markers.db"))
    try:
        store = RepriseStore(markers)
        for number in range(1, 4):
            path = media / "tv" / f"Show S01E0{number}.mkv"
            identity = PlaybackIdentity(
                "machine", str(number), "2", "3", f"/library/parts/{number}/file", path.stat().st_size, 5000, "4"
            )
            store.register_media(media_version(str(path), 5000, "{}"), identity, server_id="plex-home", metadata={})
    finally:
        markers.close()


def _connected_install(
    copies: Copies, page: Page, tmp_path: Path, *, anime_folder_shared: bool, episodes_read: bool, older_account: bool
) -> tuple[str, Path]:
    """Make the owner account through the real form, then give the install a connected Plex, as the wizard would have.

    The account file and settings are read when the app starts, so the first copy is stopped, the files are written and
    a second copy starts on the same folder. Returns that copy's address and its config folder.
    """
    config = tmp_path / "config"
    config.mkdir()
    media = tmp_path / "media"
    (media / "tv").mkdir(parents=True)
    for number in range(1, 4):
        (media / "tv" / f"Show S01E0{number}.mkv").write_bytes(b"x" * (10 + number))
    if anime_folder_shared:
        (media / "anime").mkdir()
        (media / "anime" / "Anime S01E01.mkv").write_bytes(b"x")

    first = copies.start(config)
    _create_account(page, first)
    expect(page).to_have_url(f"{first}/setup-guide")
    copies.stop(first)
    time.sleep(0.3)

    settings_file = config / "settings.json"
    settings = json.loads(settings_file.read_text()) if settings_file.exists() else {}
    settings.update({"setup_complete": True, "media_servers": [_plex_server(media)]})
    settings_file.write_text(json.dumps(settings))
    if episodes_read:
        _read_episodes_into_reprise(config, media)
    if older_account:
        auth_file = config / "auth.json"
        auth = json.loads(auth_file.read_text())
        auth.pop("setup_guide", None)  # an account made before the guide existed has no saved choice
        auth["onboarding"] = {"state": "dismissed"}
        auth_file.write_text(json.dumps(auth))
    page.context.clear_cookies()
    return copies.start(config), config


class TestBrandNewInstall:
    def test_the_new_account_opens_the_guide_with_nothing_done_and_one_button(
        self, copies: Copies, page: Page, tmp_path: Path
    ) -> None:
        config = tmp_path / "config"
        config.mkdir()
        url = copies.start(config)
        _create_account(page, url)

        expect(page).to_have_url(f"{url}/setup-guide")
        expect(page.locator("#sgGroups")).not_to_have_attribute("aria-busy", "true")
        expect(page.locator("#sgProgress")).to_have_text("0 of 3 required steps done")
        expect(page.locator("#repriseSetupGuide li[data-step-id='connect_plex']")).to_have_attribute(
            "data-state", "todo"
        )
        for step_id in ("libraries_and_folders", "refresh_library"):
            expect(page.locator(f"#repriseSetupGuide li[data-step-id='{step_id}']")).to_have_attribute(
                "data-state", "blocked"
            )
        expect(page.locator("#sgRequired .sg-action.primary")).to_have_count(1)
        expect(page.get_by_role("button", name="Skip for now")).to_be_hidden()
        expect(page.locator("#repriseTour")).to_have_attribute("data-auto", "false")
        expect(page.locator("#repriseTour")).not_to_have_attribute("open", "")

    def test_step_one_opens_the_setup_wizard_and_home_comes_back_to_the_guide(
        self, copies: Copies, page: Page, tmp_path: Path
    ) -> None:
        config = tmp_path / "config"
        config.mkdir()
        url = copies.start(config)
        _create_account(page, url)
        expect(page.locator("#sgGroups")).not_to_have_attribute("aria-busy", "true")

        page.get_by_role("link", name="Connect Plex").click()
        expect(page).to_have_url(f"{url}/setup")

        page.goto(f"{url}/")
        expect(page).to_have_url(f"{url}/setup-guide")


class TestInstallFromBeforeTheGuideExisted:
    def test_signing_in_opens_the_library_and_home_never_offers_the_guide(
        self, copies: Copies, page: Page, tmp_path: Path
    ) -> None:
        url, _config = _connected_install(
            copies, page, tmp_path, anime_folder_shared=True, episodes_read=True, older_account=True
        )
        _sign_in(page, url)
        expect(page).to_have_url(f"{url}/reprise")

        page.goto(f"{url}/")
        expect(page).to_have_url(f"{url}/reprise")

    def test_the_guide_page_shows_the_required_steps_done_and_writes_nothing(
        self, copies: Copies, page: Page, tmp_path: Path
    ) -> None:
        url, config = _connected_install(
            copies, page, tmp_path, anime_folder_shared=True, episodes_read=True, older_account=True
        )
        _sign_in(page, url)
        auth_before = (config / "auth.json").read_bytes()
        settings_before = (config / "settings.json").read_bytes()
        posts: list[str] = []
        page.on("request", lambda request: posts.append(request.url) if request.method == "POST" else None)

        page.goto(f"{url}/setup-guide")
        expect(page.locator("#sgGroups")).not_to_have_attribute("aria-busy", "true")
        expect(page.locator("#sgProgress")).to_have_text("3 of 3 required steps done")
        for step_id in ("connect_plex", "libraries_and_folders", "refresh_library"):
            expect(page.locator(f"#repriseSetupGuide li[data-step-id='{step_id}']")).to_have_attribute(
                "data-state", "done"
            )
        expect(page.locator("#repriseSetupGuide li[data-step-id='refresh_library']")).to_contain_text("3 episode files")
        expect(page.locator("#sgDone")).to_be_visible()

        page.wait_for_timeout(500)  # a late save, if the page made one, would have gone out by now
        assert posts == []
        assert (config / "auth.json").read_bytes() == auth_before
        assert (config / "settings.json").read_bytes() == settings_before


class TestPlexConnectedButAFolderIsNotShared:
    def test_the_step_names_the_folder_and_its_button_opens_that_servers_path_mappings(
        self, copies: Copies, page: Page, tmp_path: Path
    ) -> None:
        url, _config = _connected_install(
            copies, page, tmp_path, anime_folder_shared=False, episodes_read=False, older_account=False
        )
        _sign_in(page, url)
        expect(page).to_have_url(f"{url}/setup-guide")  # a required step is still open, so home keeps opening the guide
        expect(page.locator("#sgGroups")).not_to_have_attribute("aria-busy", "true")

        folders = page.locator("#repriseSetupGuide li[data-step-id='libraries_and_folders']")
        expect(folders).to_have_attribute("data-state", "todo")
        expect(folders).to_contain_text("Anime")
        expect(folders).to_contain_text("/data/anime")
        expect(folders).to_contain_text(str(tmp_path / "media" / "anime"))
        expect(page.locator("#repriseSetupGuide li[data-step-id='refresh_library']")).to_have_attribute(
            "data-state", "blocked"
        )

        folders.get_by_role("link", name="Fix media folders").click()
        expect(page.locator("#edit-tab-paths")).to_be_visible(timeout=15000)
        expect(page).to_have_url(f"{url}/servers")  # the two parameters are dropped once the dialog is open
        # The account is new, so the first-run tour is still waiting; it must not open on top of the dialog just opened.
        expect(page.locator("#repriseTour")).to_have_attribute("data-auto", "false")
        expect(page.locator("#repriseTour")).not_to_have_attribute("open", "")

    def test_skip_for_now_goes_to_the_library_and_home_stops_opening_the_guide(
        self, copies: Copies, page: Page, tmp_path: Path
    ) -> None:
        url, config = _connected_install(
            copies, page, tmp_path, anime_folder_shared=False, episodes_read=False, older_account=False
        )
        _sign_in(page, url)
        expect(page).to_have_url(f"{url}/setup-guide")
        expect(page.locator("#sgGroups")).not_to_have_attribute("aria-busy", "true")

        page.get_by_role("button", name="Skip for now").click()
        expect(page).to_have_url(f"{url}/reprise")
        # The tour's turn: the Library is the page it explains.
        expect(page.locator("#repriseTour")).to_have_attribute("open", "")
        assert json.loads((config / "auth.json").read_text())["setup_guide"]["state"] == "dismissed"

        page.goto(f"{url}/")
        expect(page).to_have_url(f"{url}/reprise")
        page.goto(f"{url}/setup-guide")  # still one click away from the sidebar
        expect(page.locator("#sgDismissed")).to_be_visible()
