"""Keyboard and mobile journeys for the approved UX polish."""

from __future__ import annotations

import re
from urllib.parse import quote

import pytest
from playwright.sync_api import Page, expect

from . import _inspector_fixtures as fx
from ._mocks import (
    capture_settings_save,
    mock_plex_libraries,
    mock_settings_backups,
    mock_settings_get,
    mock_setup_status,
    mock_system_status,
)
from .conftest import expect_modal_shown, watch_modal_shown
from .test_intro_credits_server_tab import _mock_server_page, _plex_server, _status
from .test_wizard_step3_paths import _drive_to_step3

pytestmark = pytest.mark.e2e
PHONE = {"width": 390, "height": 844}


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


@pytest.fixture
def settings_page(authed_page: Page, app_url: str) -> Page:
    mock_settings_get(authed_page)
    mock_settings_backups(authed_page)
    mock_setup_status(authed_page, complete=True, plex_authenticated=True)
    mock_system_status(authed_page)
    capture_settings_save(authed_page)
    authed_page.goto(f"{app_url}/settings")
    expect(authed_page.locator("#gpuConfigList .gpu-tuning-threads")).to_have_count(2)
    return authed_page


def test_first_tab_skips_navigation_and_moves_focus_to_main(settings_page: Page) -> None:
    settings_page.keyboard.press("Tab")
    skip = settings_page.get_by_role("link", name="Skip to main content")
    expect(skip).to_be_focused()
    expect(skip).to_be_visible()
    settings_page.keyboard.press("Enter")
    expect(settings_page.get_by_role("main")).to_be_focused()


def test_mobile_settings_picker_has_a_nodes_entry_that_opens_the_nodes_page(settings_page: Page) -> None:
    page = settings_page
    page.set_viewport_size(PHONE)
    page.locator("#settingsMobileSection").select_option(label="Nodes")
    expect(page).to_have_url(re.compile(r"/nodes$"))


def test_settings_extra_guidance_can_be_opened_and_closed_with_keyboard(settings_page: Page) -> None:
    info = settings_page.get_by_role("button", name="Explain auto-requeue interrupted jobs")
    hint = settings_page.locator("label[for='autoRequeueOnRestart'] + .sr-hint")
    assert len(hint.inner_text()) <= 90, "the row keeps one short line; the rest lives in the (i)"
    modal = settings_page.locator("#globalInfoModal")
    expect(modal).to_be_hidden()
    watch_modal_shown(settings_page, "globalInfoModal")
    info.focus()
    settings_page.keyboard.press("Enter")
    expect_modal_shown(settings_page, "globalInfoModal")
    expect(settings_page.locator("#globalInfoBody")).to_contain_text("Use Reprocess to run a failed job again")
    settings_page.keyboard.press("Escape")
    expect(modal).to_be_hidden()


def test_compact_settings_help_keeps_full_guidance_in_its_dialog(settings_page: Page) -> None:
    page = settings_page
    page.get_by_role("button", name="Explain auto-requeue interrupted jobs").click()
    expect(page.locator("#globalInfoModal")).to_be_visible()
    expect(page.locator("#globalInfoBody")).to_contain_text("Max requeue age")
    expect(page.locator("#globalInfoBody")).to_contain_text("Paused jobs are exempt")


def test_server_section_selector_tracks_tabs_when_viewport_changes(authed_page: Page, app_url: str) -> None:
    page = authed_page
    server = _plex_server()
    _mock_server_page(page, server, _status(server, "ready"))
    page.set_viewport_size(PHONE)
    page.goto(f"{app_url}/servers")
    watch_modal_shown(page, "editServerModal")
    page.locator(f'.edit-server-btn[data-id="{server["id"]}"]').click()
    expect_modal_shown(page, "editServerModal")
    selector = page.locator("#editServerSectionSelect")
    selector.select_option(label="Libraries")
    expect(page.locator("#edit-tab-libraries")).to_be_visible()
    selector.select_option(label="Processing")
    expect(page.locator("#edit-tab-loudness")).to_be_visible()

    page.set_viewport_size({"width": 1440, "height": 900})
    page.locator('#editServerModal [data-bs-target="#edit-tab-libraries"]').click()
    page.set_viewport_size(PHONE)
    expect(selector.locator("option:checked")).to_have_text("Libraries")
    expect(page.locator("#edit-tab-libraries")).to_be_visible()


def test_setup_progress_name_updates_after_forward_and_back(wizard_page: Page, app_url_wizard: str) -> None:
    page = wizard_page
    mock_plex_libraries(page)
    capture_settings_save(page)
    mock_setup_status(page, complete=False)
    _drive_to_step3(page, app_url_wizard)
    progress = page.get_by_role("progressbar")
    expect(progress).to_have_accessible_name("Setup progress: step 3 of 4, Paths")
    expect(progress).to_have_attribute("aria-valuenow", "3")
    page.locator("#step3Back").click()
    expect(progress).to_have_accessible_name("Setup progress: step 2 of 4, Connect")
    expect(progress).to_have_attribute("aria-valuenow", "2")
    expect(progress.locator('[data-step="2"] span')).to_have_text("Connect")
    expect(page.locator("#setupProgressCurrent")).to_have_text("Step 2 of 4 · Connect")
    page.set_viewport_size({"width": 768, "height": 900})
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")


def test_mobile_inspector_jumps_only_offer_sections_present_in_the_file(authed_page: Page, app_url: str) -> None:
    page = authed_page
    api = fx.InspectorApi()
    file, item = fx.checked_film()
    api.add(file, item, fx.default_kinds(file["canonical_path"]))
    fx.install(page, api)
    page.set_viewport_size(PHONE)
    page.goto(f"{app_url}/inspector?path={quote(fx.FILM)}")
    expect(page.locator("#inspEvidence")).to_be_visible()
    nav = page.get_by_role("navigation", name="Inspector sections")
    expect(nav.get_by_role("link", name="Loudness")).to_be_hidden()
    nav.get_by_role("link", name="Servers").click()
    expect(page.locator("#inspServers")).to_be_in_viewport()
