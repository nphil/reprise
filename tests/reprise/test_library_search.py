"""Library search: matches show and episode names across the whole catalogue, and stays fast to page."""

import pytest

from media_preview_generator.markers.store import MarkerStore
from media_preview_generator.reprise.identity import media_version
from media_preview_generator.reprise.models import PlaybackIdentity
from media_preview_generator.reprise.service import RepriseService, _mapping_fingerprint
from media_preview_generator.reprise.store import RepriseStore
from media_preview_generator.servers.registry import ServerRegistry

OTHER_EPISODES = 130  # more than one page of 100 that come BEFORE the Community rows in the catalogue
COMMUNITY_EPISODES = 120


@pytest.fixture
def library(tmp_path):
    folder = tmp_path / "library"
    folder.mkdir()
    markers = MarkerStore(str(tmp_path / "markers.db"))
    store = RepriseStore(markers)
    registry = ServerRegistry.from_settings(
        [
            {
                "id": "server",
                "name": "Test",
                "enabled": True,
                "type": "plex",
                "url": "http://127.0.0.1:9",
                "auth": {},
                "server_identity": "machine",
                "libraries": [{"id": "library", "name": "Library", "enabled": True, "remote_paths": [str(folder)]}],
            }
        ]
    )
    fingerprint = _mapping_fingerprint(registry.get_config("server"), registry.get_config("server").libraries[0])

    def add(number, show, title):
        path = folder / f"{number}.mp4"
        path.write_bytes(b"episode %d" % number)
        version = media_version(str(path), 1000, "{}")
        identity = PlaybackIdentity(
            "machine", str(number), "2", "3", f"/library/parts/{number}", path.stat().st_size, 1000, "4"
        )
        metadata = {
            "library_id": "library",
            "remote_part_path": str(path),
            "mapping_fingerprint": fingerprint,
            "show_title": show,
            "title": title,
            "season": 1 + number % 3,
            "episode": number,
        }
        store.register_media(version, identity, server_id="server", metadata=metadata)

    for n in range(OTHER_EPISODES):
        add(n, "Better Off Ted", f"Ted {n}")
    for n in range(OTHER_EPISODES, OTHER_EPISODES + COMMUNITY_EPISODES):
        add(n, "Community", f"Pilot {n}")
    add(10_000, "Scrubs", "My Community Service")
    yield RepriseService(store, registry, None), store
    markers.close()


def test_search_matches_the_show_name_in_any_case_and_partially_across_all_pages(library):
    service, _ = library
    for term in ("community", "COMMUNITY", "Commun", "ommunit"):
        first = service.list_media(search=term, limit=100)
        assert len(first["items"]) == 100 and first["next_cursor"] == "100"
        second = service.list_media(search=term, limit=100, cursor=first["next_cursor"])
        assert second["next_cursor"] is None
        found = first["items"] + second["items"]
        assert len(found) == COMMUNITY_EPISODES + 1 and len({i["id"] for i in found}) == len(found)
        assert {i["show_title"] for i in found} == {"Community", "Scrubs"}


def test_search_matches_an_episode_title_and_filters_combine(library):
    service, _ = library
    (hit,) = service.list_media(search="community service")["items"]
    assert (hit["show_title"], hit["title"]) == ("Scrubs", "My Community Service")
    assert service.list_media(search="no such thing")["items"] == []
    season = service.list_media(search="community", show="Community", season=2, limit=100)["items"]
    assert season and all(i["show_title"] == "Community" and i["season"] == 2 for i in season)
    assert service.list_media(search="community", show="Better Off Ted")["items"] == []


def test_an_unfiltered_page_still_lists_every_show_and_season(library):
    service, _ = library
    page = service.list_media(limit=50)
    assert len(page["items"]) == 50 and page["next_cursor"] == "50"
    assert page["shows"] == ["Better Off Ted", "Community", "Scrubs"] and page["seasons"] == [1, 2, 3]


def test_status_lookups_use_an_index_not_a_scan_of_every_binding(library):
    """Reading one episode's bindings scanned the whole bindings table (40 ms each at 4,000 episodes), so a page of
    100 analysed Community episodes took ~15 s and the search looked empty."""
    _, store = library
    version_id = store.catalogue()[0]["version_id"]
    with store.marker_store._reading() as c:
        plan = " ".join(
            row["detail"]
            for row in c.execute(
                "EXPLAIN QUERY PLAN SELECT identity,server_id,metadata FROM reprise_bindings WHERE version_id=? AND active=1",
                (version_id,),
            )
        )
    assert "USING INDEX reprise_bindings_version" in plan or "USING COVERING INDEX reprise_bindings_version" in plan
