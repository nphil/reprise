"""Every signed-in page shares the Reprise chrome, with tools grouped under Settings.

The inherited upstream top navbar, its Ko-fi/Star links and its "Media Preview Generator" titles must never come back
on any page reachable from the navigation.
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser

import pytest

from tests.auth_helpers import sign_in

# path -> (label of the highlighted Settings tab, its aria-current value). Nodes and Webhook activity are reached from
# Advanced and Automation, so they highlight those parents ("true" marks a parent, "page" the page itself).
SETTINGS_PAGES = {
    "/servers": ("Connections", "page"),
    "/reprise/sharing": ("Sharing", "page"),
    "/automation": ("Automation", "page"),
    "/settings": ("Advanced", "page"),
    "/dashboard": ("Jobs", "page"),
    "/logs": ("Logs", "page"),
    "/inspector": ("Inspector", "page"),
    "/library-health": ("Library health", "page"),
    "/nodes": ("Advanced", "true"),
    "/webhook-activity": ("Automation", "true"),
}
ALL_PAGES = ["/reprise", "/reprise/approve", "/setup-guide", *SETTINGS_PAGES]
TABS = [
    ("Connections", "/servers"),
    ("Sharing", "/reprise/sharing"),
    ("Automation", "/automation"),
    ("Advanced", "/settings"),
]
TOOLS = [
    ("Jobs", "/dashboard"),
    ("Logs", "/logs"),
    ("Inspector", "/inspector"),
    ("Library health", "/library-health"),
]


@pytest.fixture
def client(tmp_path, monkeypatch):
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
    yield sign_in(app.test_client())
    settings_manager.reset_settings_manager()


class _Nav(HTMLParser):
    """Collect links in the main navigation, Settings tabs, and Tools disclosure."""

    def __init__(self) -> None:
        super().__init__()
        self.main: list[dict] = []
        self.tabs: list[dict] = []
        self.tools: list[dict] = []
        self.tools_current = False
        self._zone: str | None = None
        self._link: dict | None = None

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        classes = a.get("class", "").split()
        if tag == "nav" and "rp-main-nav" in classes:
            self._zone = "main"
        elif tag == "nav" and "rp-subnav" in classes:
            self._zone = "tabs"
        elif tag == "button" and a.get("aria-controls") == "rpTools":
            assert a.get("aria-expanded") == "false"
            self.tools_current = "data-current" in a
        elif tag == "div" and a.get("id") == "rpTools":
            assert "hidden" in a
            self._zone = "tools"
        elif tag == "a" and self._zone:
            self._link = {"href": a.get("href", ""), "current": a.get("aria-current"), "text": ""}
            getattr(self, self._zone).append(self._link)

    def handle_endtag(self, tag):
        if tag == "a":
            self._link = None
        elif tag == "nav":
            self._zone = None

    def handle_data(self, data):
        if self._link is not None:
            self._link["text"] += data


def _nav(html: str) -> _Nav:
    parser = _Nav()
    parser.feed(html)
    for link in parser.main + parser.tabs + parser.tools:
        link["text"] = link["text"].strip()
    return parser


def _page(client, path: str) -> str:
    response = client.get(path)
    assert response.status_code == 200, path
    return response.get_data(as_text=True)


class TestNoInheritedNavbar:
    @pytest.mark.parametrize("path", ALL_PAGES)
    def test_page_uses_the_reprise_app_bar_not_the_upstream_navbar(self, client, path):
        html = _page(client, path)
        assert 'class="rp-appbar"' in html
        for upstream in ("navbar-brand", "navbar-toggler", 'id="navbarNav"', "nav-hover-menu", "offcanvas-xl"):
            assert upstream not in html, f"{path} still renders the upstream navbar piece {upstream!r}"
        assert '<nav class="navbar' not in html

    @pytest.mark.parametrize("path", ALL_PAGES)
    def test_page_links_nothing_to_the_upstream_project(self, client, path):
        html = _page(client, path).lower()
        for upstream in ("ko-fi.com", "stevezau/media_preview_generator/issues", "buy me a coffee", "star on github"):
            assert upstream not in html, f"{path} links to the upstream project ({upstream})"

    @pytest.mark.parametrize("path", ALL_PAGES)
    def test_title_names_reprise(self, client, path):
        title = re.search(r"<title>(.*?)</title>", _page(client, path), re.S).group(1).strip()
        assert title.endswith("Reprise"), title
        assert "Media Preview Generator" not in title

    @pytest.mark.parametrize("path", ALL_PAGES)
    def test_shared_stylesheet_loads_after_the_page_styles(self, client, path):
        head = re.search(r"<head>(.*?)</head>", _page(client, path), re.S).group(1)
        links = re.findall(r'<link[^>]+href="([^"]+\.css)"', head)
        shell = [i for i, href in enumerate(links) if href.endswith("css/reprise-shell.css")]
        assert len(shell) == 1 and shell[0] == len(links) - 1, links


class TestNavigation:
    @pytest.mark.parametrize("path", ALL_PAGES)
    def test_main_navigation_has_three_destinations(self, client, path):
        nav = _nav(_page(client, path))
        assert [(link["text"], link["href"]) for link in nav.main] == [
            ("Library", "/reprise"),
            ("Review", "/reprise/approve"),
            ("Settings", "/servers"),
        ]

    def test_the_library_marks_itself_current_and_has_no_tabs(self, client):
        nav = _nav(_page(client, "/reprise"))
        assert [(link["text"], link["current"]) for link in nav.main] == [
            ("Library", "page"),
            ("Review", None),
            ("Settings", None),
        ]
        assert nav.tabs == nav.tools == []

    def test_review_marks_itself_current_and_has_no_tabs(self, client):
        nav = _nav(_page(client, "/reprise/approve"))
        assert [(link["text"], link["current"]) for link in nav.main] == [
            ("Library", None),
            ("Review", "page"),
            ("Settings", None),
        ]
        assert nav.tabs == nav.tools == []

    def test_the_setup_guide_is_reached_from_help_not_the_main_navigation(self, client):
        nav = _nav(_page(client, "/setup-guide"))
        assert [(link["text"], link["current"]) for link in nav.main] == [
            ("Library", None),
            ("Review", None),
            ("Settings", None),
        ]
        assert nav.tabs == nav.tools == []

    @pytest.mark.parametrize(("path", "expected"), SETTINGS_PAGES.items())
    def test_settings_pages_show_the_same_tabs_and_tools_and_mark_their_own(self, client, path, expected):
        label, current = expected
        nav = _nav(_page(client, path))
        assert [(link["text"], link["href"]) for link in nav.tabs] == TABS
        assert [(link["text"], link["href"]) for link in nav.tools] == TOOLS
        assert nav.tools_current == (label in {name for name, _ in TOOLS})
        assert {link["text"]: link["current"] for link in nav.tabs + nav.tools if link["current"]} == {label: current}
        assert [link["current"] for link in nav.main] == [None, None, "true"]

    def test_pages_without_their_own_tab_stay_reachable_from_their_parent(self, client):
        # Nodes is linked from Advanced's own sidebar, Webhook activity from Automation's and from the Jobs shortcuts.
        assert 'href="/nodes"' in _page(client, "/settings")
        assert 'href="/webhook-activity"' in _page(client, "/automation")
        assert 'href="/webhook-activity"' in _page(client, "/dashboard")


class TestAppActions:
    def test_app_bar_keeps_the_safety_chip_notifications_theme_and_sign_out(self, client):
        html = _page(client, "/reprise")
        for landmark in (
            'id="rpSafety"',
            'id="notificationBellBtn"',
            'id="notificationBellBadge"',
            'id="notificationList"',
            'id="rpThemeBtn"',
            'id="navLogoutBtn"',
            'action="/logout"',
        ):
            assert landmark in html, landmark
        assert 'name="csrf_token"' in html

    def test_recent_activity_links_to_saved_history_and_has_server_notice_actions(self, client):
        html = _page(client, "/reprise")
        panel = html[html.index('id="rpActivity"') : html.index('id="rpThemeBtn"')]
        for landmark in (
            'id="notificationDismissAllBtn"',
            'id="notificationResetBtn"',
            'href="/dashboard"',
            'href="/reprise/approve#automatic-sends"',
        ):
            assert landmark in panel

    def test_the_safety_chip_never_claims_review_only_before_it_has_checked(self, client):
        html = _page(client, "/reprise")
        chip = re.search(r'id="rpSafety"[^>]*>(.*?)</a>', html, re.S).group(1)
        assert 'data-state="checking"' in html
        assert "Checking Plex status" in chip
        assert "Review only" not in chip

    def test_version_and_help_point_at_reprise(self, client):
        html = _page(client, "/reprise")
        assert 'href="https://github.com/nphil/reprise/releases/latest"' in html
        assert 'href="https://github.com/nphil/reprise/issues"' in html

    def test_help_has_the_setup_guide_and_tour(self, client):
        html = _page(client, "/reprise")
        start = html.index('id="rpHelp"')
        help_panel = html[start : html.index("</header>", start)]
        link = re.search(r'id="rpSetupGuideLink" href="/setup-guide"([^>]*)>(.*?)</a>', help_panel, re.S)
        assert link, "Help has no Setup guide link"
        assert "Setup guide" in link.group(2)
        assert "aria-current" not in link.group(1), "only the guide page itself marks the link current"
        assert help_panel.index('id="rpSetupGuideLink"') < help_panel.index("data-tour-open")

    def test_the_guide_marks_its_own_help_link_current(self, client):
        assert re.search(r'id="rpSetupGuideLink"[^>]*aria-current="page"', _page(client, "/setup-guide"))


class TestStandalonePages:
    def test_the_sign_out_confirmation_has_no_app_bar(self, client):
        html = _page(client, "/logout")
        assert 'class="rp-appbar"' not in html
        assert "rp-bare" in html


class TestReprisePages:
    def test_library_starts_with_episodes_and_keeps_technical_ids_in_file_details(self, client):
        html = _page(client, "/reprise")
        for landmark in (
            'id="rpLibraryList"',
            'id="rpLibraryTools"',
            'id="rpRangesTitle"',
            'id="rpAnalyze"',
            'id="rpFileDetails"',
        ):
            assert landmark in html, landmark
        # Plex ids, media/part/audio ids and the like live only inside the collapsed "File details".
        assert not re.search(r"Media \d+|part \d+|audio \d+", html)
        assert re.search(r'<details class="rp-disclosure" id="rpFileDetails">', html)

    def test_sharing_page_holds_the_plex_switch_and_the_api_clients(self, client):
        html = _page(client, "/reprise/sharing")
        for landmark in (
            'id="rpWritesEnabled"',
            'id="rpClientForm"',
            'id="rpClients"',
            "Everything starts switched off",
        ):
            assert landmark in html, landmark
        # The switch starts disabled until the settings call says publication is available.
        assert re.search(
            r'<input class="form-check-input" id="rpWritesEnabled" type="checkbox" role="switch" disabled>', html
        )

    def test_sharing_page_holds_the_phone_alerts_panel_after_the_automatic_one(self, client):
        html = _page(client, "/reprise/sharing")
        for landmark in ("rpNotifyOn", "rpQuietFrom", "rpQuietUntil", "rpNotifyStatus"):
            assert f'id="{landmark}"' in html, landmark
        # Hidden until the notifications call answers; it follows "Automatic work" and comes before the API clients.
        assert html.index('id="rpWritesEnabled"') < html.index('id="rpNotifyPanel"') < html.index('id="rpClientForm"')
        assert '<section class="rp-panel rp-notify" id="rpNotifyPanel" aria-labelledby="rpNotifyTitle" hidden>' in html
        assert "js/reprise-notify.js" in html


class TestSetupGuidePage:
    def test_the_page_is_the_frame_the_script_fills_in(self, client):
        html = _page(client, "/setup-guide")
        for landmark in (
            'id="repriseSetupGuide"',
            "<h1>Setup guide</h1>",
            'id="sgRequiredTitle">Required</h2>',
            'id="sgOptionalTitle">Optional</h2>',
            'id="sgRequired"',
            'id="sgLive" role="status" aria-live="polite"',
            'id="sgRecheck"',
            'id="sgSkip"',
            "css/pages/setup-guide.css",
            "js/setup-guide.js",
        ):
            assert landmark in html, landmark
        assert re.search(r'<ol class="sg-steps" id="sgOptional" role="list" start="4">', html)
        assert "Three steps get Reprise reading your library." in html

    def test_the_tour_opens_by_itself_on_the_library_only(self, client):
        from media_preview_generator.web import auth

        auth.set_onboarding_state(auth.ONBOARDING_PENDING)  # a brand-new account: the tour would open by itself
        assert 'data-auto="true"' in _page(client, "/reprise")
        # The guide and the pages its buttons open: the tour would sit on top of the dialog or form the button opened.
        for path in ("/setup-guide", "/servers", "/settings", "/reprise/sharing"):
            html = _page(client, path)
            assert 'data-auto="false"' in html, path
            assert 'id="repriseTour"' in html, f"{path}: still there to open by hand"
        auth.set_onboarding_state(auth.ONBOARDING_DISMISSED)
        assert 'data-auto="false"' in _page(client, "/reprise")

    def test_settings_offers_the_guide_next_to_the_tour(self, client):
        html = _page(client, "/settings")
        account = html[html.index('id="section-auth"') : html.index("js/account_panel.js")]
        assert re.search(
            r'<a class="btn btn-outline-secondary" href="/setup-guide">.*?Open the setup guide</a>', account, re.S
        )
        assert account.count("data-tour-open") == 1, "the tour button stays the only tour opener in the card"
