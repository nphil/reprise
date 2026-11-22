"""Films in Reprise: a movie library is listed, registered and analysed on its own terms.

A film has no other episodes to compare with, so Reprise only ever suggests its credits (from Plex's marker, from an
online database, or both) and never approves them for the owner. These tests run real discovery, queueing, SQLite and
analysis against a fake Plex server and a generated clip; the online databases are fakes.
"""

import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
import requests

from media_preview_generator.markers.models import Candidate, MarkerType, MediaIds, Source
from media_preview_generator.markers.sources.online import LookupResult
from media_preview_generator.markers.store import MarkerStore
from media_preview_generator.reprise import analysis, online
from media_preview_generator.reprise.analysis import (
    ENDING_WINDOW,
    FALLBACK_WINDOW_MS,
    FILM_NOT_CONFIRMED,
    MOVIE_NOTE,
    NO_OTHER_EPISODES,
    NO_REFERENCES,
    OPENING_WINDOW,
)
from media_preview_generator.reprise.identity import media_version
from media_preview_generator.reprise.models import (
    AnalysisDraft,
    CandidateWindow,
    OnlineWindow,
    PlaybackIdentity,
    ReviewRefused,
)
from media_preview_generator.reprise.policy import eligible_ranges
from media_preview_generator.reprise.review import plex_projection
from media_preview_generator.reprise.service import RepriseService, _mapping_fingerprint, set_service
from media_preview_generator.reprise.store import RepriseStore
from media_preview_generator.servers.base import Library, ServerConfig, ServerType
from media_preview_generator.servers.registry import ServerRegistry
from media_preview_generator.web import jobs, scheduler
from media_preview_generator.web.job_gate import reset_job_gate
from media_preview_generator.web.settings_manager import get_settings_manager, reset_settings_manager
from tests.reprise.test_service_bindings import wait_for_queue

pytestmark = pytest.mark.real_job_async

CLIP_SECONDS = 40
DURATION_MS = CLIP_SECONDS * 1000
CREDITS = (32000, 39000)  # inside the last quarter of the clip, long enough to be a plausible credits roll
STREAMS = json.dumps({"audio_streams": [{"plex_id": "123", "index": 1}]})
LONG_FILM = 2 * 60 * 60 * 1000


# --- a fake Plex, a generated clip and a real Reprise service ---------------------------------------------------------


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    path = tmp_path_factory.mktemp("film-clip") / "clip.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=size=32x32:rate=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440",
            "-t",
            str(CLIP_SECONDS),
            "-threads",
            "1",
            "-c:v",
            "mpeg4",
            "-c:a",
            "aac",
            str(path),
        ],
        check=True,
        timeout=30,
    )
    return path


def marker(kind, start, end):
    return {"type": kind, "startTimeOffset": start, "endTimeOffset": end}


class World:
    """What the fake Plex serves: films in section 2 and episodes in section 1, plus a record of the listings asked
    for. A section only lists its items for the item type it holds (film 1, episode 4), as Plex does."""

    def __init__(self, clip, tv_dir, film_dir):
        self.clip, self.tv_dir, self.film_dir = clip, tv_dir, film_dir
        self.items = {}
        self.sections = {"1": ["4", []], "2": ["1", []]}
        self.external_ids = {}
        self.requests = []

    def _add(self, section, key, path, title, extra, markers):
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.clip, path)
        self.items[key] = {"file": path, "title": title, "extra": extra, "markers": list(markers)}
        self.sections[section][1].append(key)
        return path

    def add_film(self, key, title, *, year=None, markers=(), folder=None):
        directory = self.film_dir / folder if folder else self.film_dir
        extra = {"year": year} if year is not None else {}
        return self._add("2", key, directory / f"{title}.mp4", title, extra, markers)

    def add_episode(self, key, title, *, show="Show", show_key="900", season=1, number=1, markers=()):
        extra = {"grandparentTitle": show, "grandparentRatingKey": show_key, "parentIndex": season, "index": number}
        path = self.tv_dir / show / f"Season {season:02d}" / f"{show} - S{season:02d}E{number:02d}.mp4"
        return self._add("1", key, path, title, extra, markers)

    def listing(self, key):
        item = self.items[key]
        file, number = item["file"], int(key)
        part = {
            "id": number + 2000,
            "file": str(file),
            "size": file.stat().st_size,
            "key": f"/library/parts/{key}/file",
            "Stream": [{"streamType": 2, "id": number + 3000, "index": 1}],
        }
        media = [{"id": number + 1000, "duration": DURATION_MS, "Part": [part]}]
        return {"ratingKey": key, "title": item["title"], **item["extra"], "Media": media}

    def detail(self, key):
        found = {**self.listing(key), "duration": DURATION_MS}
        if self.items[key]["markers"]:
            found["Marker"] = self.items[key]["markers"]
        return found


class FakeAdapter:
    def __init__(self, url, libraries, world):
        self.url, self.libraries, self.world = url, libraries, world

    def list_libraries(self):
        return self.libraries

    def _plex_http(self, method, path, **kwargs):
        response = requests.request(method, self.url + path, timeout=20, **kwargs)
        response.raise_for_status()
        return response

    def get_external_ids(self, rating_key):
        return self.world.external_ids.get(str(rating_key))


@pytest.fixture
def film_library(tmp_path, monkeypatch, clip):
    tv_dir, film_dir = tmp_path / "tv", tmp_path / "films"
    tv_dir.mkdir()
    film_dir.mkdir()
    world = World(clip, tv_dir, film_dir)

    class PlexFixture(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            parsed = urlparse(self.path)
            path = parsed.path
            if path == "/":
                result = {"machineIdentifier": "machine"}
            elif path.startswith("/library/sections/") and path.endswith("/all"):
                section = path.split("/")[3]
                params = {name: values[0] for name, values in parse_qs(parsed.query).items()}
                world.requests.append((section, params))
                wanted, keys = world.sections[section]
                listed = keys if params.get("type") == wanted else []
                result = {"totalSize": len(listed), "Metadata": [world.listing(key) for key in listed]}
            else:
                result = {"Metadata": [world.detail(path.rsplit("/", 1)[-1])]}
            body = json.dumps({"MediaContainer": result}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    plex = ThreadingHTTPServer(("127.0.0.1", 0), PlexFixture)
    thread = threading.Thread(target=plex.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{plex.server_port}"
    shows = Library("1", "TV", (str(tv_dir),), kind="show")
    films = Library("2", "Movies", (str(film_dir),), kind="movie")
    config = ServerConfig(
        id="server",
        type=ServerType.PLEX,
        name="Plex",
        enabled=True,
        url=url,
        auth={},
        libraries=[shows, films],
        server_identity="machine",
    )
    registry = ServerRegistry()
    registry._configs["server"] = config
    registry._servers["server"] = FakeAdapter(url, [shows, films], world)
    markers = MarkerStore(str(tmp_path / "markers.db"))
    store = RepriseStore(markers)
    reset_settings_manager()
    settings = get_settings_manager(str(tmp_path))
    plex_config = tmp_path / "plex_config"
    (plex_config / "Media" / "localhost").mkdir(parents=True)
    settings.set(
        "media_servers",
        [
            {
                "id": "server",
                "type": "plex",
                "name": "Plex",
                "enabled": True,
                "url": url,
                "auth": {"token": "test-token"},
                "libraries": [
                    {"id": "1", "name": "TV", "remote_paths": [str(tv_dir)], "enabled": True},
                    {"id": "2", "name": "Movies", "remote_paths": [str(film_dir)], "enabled": True},
                ],
                "output": {"adapter": "plex_bundle", "plex_config_folder": str(plex_config)},
            }
        ],
    )
    settings.set(
        "worker_groups",
        [
            {
                "id": "local",
                "name": "Local",
                "enabled": True,
                "availability": {"mode": "always", "windows": []},
                "members": [
                    {"id": "cpu", "resource": "cpu", "device": None, "count": 1, "job_types": ["reprise", "previews"]}
                ],
            }
        ],
    )
    manager = jobs.JobManager(config_dir=str(tmp_path))
    monkeypatch.setattr(jobs, "_job_manager", manager)
    monkeypatch.setattr(online, "switch_on", lambda: False)  # no test reaches a real online database
    reset_job_gate()
    service = RepriseService(store, registry, manager)
    set_service(service)
    try:
        yield SimpleNamespace(
            world=world,
            config=config,
            shows=shows,
            films=films,
            url=url,
            root=tmp_path,
            store=store,
            manager=manager,
            service=service,
        )
    finally:
        wait_for_queue()
        plex.shutdown()
        thread.join()
        plex.server_close()
        manager.close()
        markers.close()
        reset_job_gate()
        reset_settings_manager()
        set_service(None)


def refresh(fx, library_id):
    job = fx.service.enqueue_refresh("server", library_id)
    wait_for_queue()
    assert fx.service.get_job(job["id"])["status"] == "completed"


def registered(fx):
    """The metadata Reprise stored for every registered file, by the file's path."""
    return {row["metadata"]["remote_part_path"]: row["metadata"] for row in fx.store.catalogue("server")}


def version_of(fx, path):
    rows = [row for row in fx.store.catalogue("server") if row["metadata"]["remote_part_path"] == str(path)]
    assert len(rows) == 1
    return fx.store.get_media(rows[0]["version_id"])


def analyse(fx, *versions):
    job = fx.service.enqueue_analysis([version.id for version in versions], principal="owner")
    wait_for_queue()
    done = fx.service.get_job(job["id"])
    assert done["status"] == "completed"
    return done


def analysed_film(fx, *, markers=(), year=2001):
    """One film registered and analysed: (version, identity, stored result, finished job)."""
    path = fx.world.add_film("201", "Name", year=year, markers=markers)
    refresh(fx, "2")
    film = version_of(fx, path)
    done = analyse(fx, film)
    return film, fx.service._bindings(film.id)[0][0], fx.store.get_result(film.id), done


class FilmDatabase:
    """A fake online database: answers every film with the same times and records what it was asked."""

    def __init__(self, found=(), source=Source.INTRODB, status="ok"):
        self.source, self.found, self.status, self.asked = source, list(found), status, []

    def lookup(self, ids, *, duration_ms, priority, cancel_check=None):
        self.asked.append((ids.kind, ids.tmdb, ids.imdb, ids.season, ids.episode, duration_ms, priority))
        if self.status != "ok":
            return LookupResult(self.status, detail=f"{self.source.value} {self.status}")
        if not self.found:
            return LookupResult("no_data")
        return LookupResult("ok", tuple(Candidate(MarkerType(k), s, e, self.source) for k, s, e in self.found))


MOVIE_ANSWER = {"kind": "movie", "tmdb": "14160", "imdb": "tt1049413"}


def online_with(fx, monkeypatch, database):
    """Give the service real online lookups that ask the fake database, and let Plex name the film's ids."""
    lookups = online.OnlineLookups(fx.store, {database.source: database})
    monkeypatch.setattr(fx.service, "_online_lookups", lambda: lookups)
    fx.world.external_ids["201"] = MOVIE_ANSWER


# --- the library listing ---------------------------------------------------------------------------------------------


def test_a_film_library_is_listed_by_film_and_an_episode_library_by_episode(film_library):
    fx = film_library
    fx.world.add_film("201", "Name", year=2001)
    fx.world.add_episode("101", "Pilot")
    refresh(fx, "2")
    refresh(fx, "1")
    asked = [(section, params["type"]) for section, params in fx.world.requests]
    assert set(asked) == {("2", "1"), ("1", "4")}
    paging = {"X-Plex-Container-Start": "0", "X-Plex-Container-Size": "200"}
    assert all(params.items() >= paging.items() for _, params in fx.world.requests)
    assert set(registered(fx)) == {str(fx.world.items["201"]["file"]), str(fx.world.items["101"]["file"])}


def test_listing_a_library_returns_its_rows_without_registering_anything(film_library):
    fx = film_library
    fx.world.add_film("201", "First", year=2001)
    fx.world.add_film("202", "Second", year=2002)
    config, library, machine, rows = fx.service.list_library("server", "2")
    assert (config.id, library.id, library.kind, machine) == ("server", "2", "movie", "machine")
    assert [row["ratingKey"] for row in rows] == ["201", "202"]
    assert [params["type"] for _, params in fx.world.requests] == ["1"]
    assert fx.store.catalogue("server") == []


def test_local_file_gives_the_real_path_of_a_file_in_the_library_and_nothing_for_others(film_library):
    fx = film_library
    film = fx.world.add_film("201", "Name", year=2001)
    episode = fx.world.add_episode("101", "Pilot")
    elsewhere = fx.root / "elsewhere" / "x.mp4"
    elsewhere.parent.mkdir()
    shutil.copyfile(film, elsewhere)
    config, library, _, rows = fx.service.list_library("server", "2")
    remote = rows[0]["Media"][0]["Part"][0]["file"]
    assert fx.service.local_file(config, library, remote) == os.path.realpath(film)
    assert fx.service.local_file(config, library, str(fx.world.film_dir / "gone.mp4")) is None
    assert fx.service.local_file(config, library, str(elsewhere)) is None
    assert fx.service.local_file(config, library, str(episode)) is None  # an episode is not in the film library


# --- what the scan registers -----------------------------------------------------------------------------------------


def test_scanning_a_film_library_registers_each_film_with_its_title_and_no_show(film_library):
    fx = film_library
    with_year = fx.world.add_film("201", "Name", year=2001, markers=[marker("credits", *CREDITS)])
    without_year = fx.world.add_film("202", "Plain")
    refresh(fx, "2")
    stored = registered(fx)
    fingerprint = _mapping_fingerprint(fx.config, fx.films)
    expected_common = {
        "library_id": "2",
        "server_url": fx.url,
        "mapping_fingerprint": fingerprint,
        "show_title": "",
        "show_key": None,
        "season": None,
        "episode": None,
        "kind": "movie",
    }
    assert stored[str(with_year)] == {
        **expected_common,
        "title": "Name (2001)",
        "remote_part_path": str(with_year),
        "candidates": [{"kind": "credits", "start_ms": CREDITS[0], "end_ms": CREDITS[1], "source": "plex"}],
    }
    assert stored[str(without_year)] == {
        **expected_common,
        "title": "Plain",
        "remote_part_path": str(without_year),
        "candidates": [],
    }


def test_a_film_keeps_only_plexs_credits_marker_and_drops_an_intro_marker(film_library):
    fx = film_library
    both = fx.world.add_film(
        "201", "Both", year=2001, markers=[marker("intro", 1000, 9000), marker("credits", *CREDITS)]
    )
    intro_only = fx.world.add_film("202", "Intro", year=2002, markers=[marker("intro", 1000, 9000)])
    refresh(fx, "2")
    stored = registered(fx)
    assert [(c["kind"], c["start_ms"], c["end_ms"]) for c in stored[str(both)]["candidates"]] == [("credits", *CREDITS)]
    assert stored[str(intro_only)]["candidates"] == []


def test_an_episode_library_scan_is_unchanged_and_never_marked_as_a_film(film_library):
    fx = film_library
    path = fx.world.add_episode(
        "103", "Pilot", number=3, markers=[marker("intro", 1000, 9000), marker("credits", *CREDITS)]
    )
    refresh(fx, "1")
    metadata = registered(fx)[str(path)]
    assert set(metadata) == {
        "library_id",
        "server_url",
        "title",
        "remote_part_path",
        "mapping_fingerprint",
        "show_title",
        "show_key",
        "season",
        "episode",
        "candidates",
    }
    assert (metadata["title"], metadata["show_title"], metadata["show_key"]) == ("Pilot", "Show", "900")
    assert (metadata["season"], metadata["episode"], metadata["library_id"]) == (1, 3, "1")
    assert metadata["mapping_fingerprint"] == _mapping_fingerprint(fx.config, fx.shows)
    assert [(c["kind"], c["start_ms"], c["end_ms"], c["source"]) for c in metadata["candidates"]] == [
        ("intro", 1000, 9000, "plex"),
        ("credits", *CREDITS, "plex"),
    ]


# --- analysing a film ------------------------------------------------------------------------------------------------


def test_a_films_plex_credits_marker_becomes_one_unapproved_suggestion(film_library):
    film, _, result, done = analysed_film(film_library, markers=[marker("credits", *CREDITS)])
    assert result.analysis_status == "complete" and result.freshness == "current"
    assert [
        (r.kind, r.action, r.origin, r.review_status, r.start_ms, r.end_ms, r.suggested_by) for r in result.ranges
    ] == [("credits", "skip", "plex", "proposed", *CREDITS, ("Plex",))]
    assert not any(r.kind == "intro" or r.origin == "detector" for r in result.ranges)
    assert result.reasons[0] == MOVIE_NOTE
    assert NO_REFERENCES not in result.reasons and NO_OTHER_EPISODES not in result.reasons
    assert any(
        reason.startswith("Plex suggested the credits (from 0:32 to 0:39).") and FILM_NOT_CONFIRMED in reason
        for reason in result.reasons
    )
    assert done["outcome"]["state"] == "suggested"


def test_a_films_suggestion_names_who_suggested_it_in_the_evidence_as_unconfirmed(film_library):
    _, _, result, _ = analysed_film(film_library, markers=[marker("credits", *CREDITS)])
    (evidence,) = result.evidence
    assert evidence.verdict == "unknown" and evidence.reason_code == "plex_marker_unconfirmed"
    assert (evidence.start_ms, evidence.end_ms) == CREDITS and evidence.reference_version_id is None
    assert result.ranges[0].evidence_ids == (evidence.id,)


def test_the_evidence_for_a_films_plex_marker_does_not_say_episodes_were_compared(film_library):
    """Intended behaviour: nothing was compared for a film, so its evidence must not claim a comparison failed."""
    _, _, result, _ = analysed_film(film_library, markers=[marker("credits", *CREDITS)])
    (evidence,) = result.evidence
    assert "comparing episodes" not in evidence.reason


def test_a_films_suggestion_is_not_eligible_to_skip_until_the_owner_approves_it(film_library):
    fx = film_library
    film, identity, result, _ = analysed_film(fx, markers=[marker("credits", *CREDITS)])
    assert eligible_ranges(result, identity.audio_stream_id) == ()
    assert fx.service.eligible_ranges(result, identity) == ()
    answer = fx.service.lookup(identity)
    assert answer["eligible_skip_ranges"] == []
    assert [r["kind"] for r in answer["proposed_ranges"]] == ["credits"]
    assert "unapproved_or_unverified" in answer["reasons"]


def test_a_films_suggestion_cannot_be_published_or_approved_without_a_deliberate_approval(film_library):
    fx = film_library
    film, identity, result, _ = analysed_film(fx, markers=[marker("credits", *CREDITS)])
    with pytest.raises(ValueError, match="not fully eligible"):
        plex_projection(result, identity, ())
    approve = [{"range_id": result.ranges[0].id, "review_status": "approved"}]
    with pytest.raises(ReviewRefused, match="deliberate approval"):
        fx.store.save_review(film.id, result.result_revision, approve, "owner")
    assert eligible_ranges(fx.store.get_result(film.id), identity.audio_stream_id) == ()


def test_a_film_without_a_plex_marker_or_online_times_has_nothing_to_suggest(film_library):
    _, _, result, done = analysed_film(film_library)
    assert result.analysis_status == "complete"
    assert result.ranges == () and result.evidence == ()  # no stand-in windows are protected as keeps either
    assert result.reasons[0] == MOVIE_NOTE
    assert NO_REFERENCES not in result.reasons and NO_OTHER_EPISODES not in result.reasons
    assert done["outcome"]["state"] == "nothing_found"
    assert done["outcome"]["message"] == MOVIE_NOTE


@pytest.fixture
def captured(monkeypatch):
    """Replace the analysis with one that records what it was given."""
    calls = []

    def capture(media, candidates, references, **options):
        calls.append(
            SimpleNamespace(media=media, candidates=list(candidates), references=list(references), options=options)
        )
        return AnalysisDraft((), (), "complete", ())

    monkeypatch.setattr(analysis, "analyze", capture)
    return calls


def test_a_film_is_analysed_with_no_stand_in_windows_and_no_other_films_to_compare_with(film_library, captured):
    fx = film_library
    first = fx.world.add_film("201", "First", year=2001, markers=[marker("credits", *CREDITS)])
    second = fx.world.add_film("202", "Second", year=2002)
    refresh(fx, "2")
    analyse(fx, version_of(fx, first))
    analyse(fx, version_of(fx, second))
    one, two = captured
    assert one.candidates == [CandidateWindow("credits", *CREDITS, "plex")]
    assert two.candidates == []  # nothing stands in for the opening or the ending of a film
    assert one.references == two.references == []
    assert one.options["movie"] is True and two.options["movie"] is True
    assert one.options["online"] == {}


def test_an_episode_still_gets_stand_in_windows_and_its_season_mates_as_references(film_library, captured):
    fx = film_library
    first = fx.world.add_episode("101", "Pilot", number=1)
    second = fx.world.add_episode("102", "Second", number=2)
    fx.world.add_film("201", "Name", year=2001, markers=[marker("credits", *CREDITS)])
    refresh(fx, "1")
    refresh(fx, "2")
    target, mate = version_of(fx, first), version_of(fx, second)
    analyse(fx, target)
    (call,) = captured
    middle = target.duration_ms // 2
    assert call.candidates == [
        CandidateWindow("intro", 0, min(FALLBACK_WINDOW_MS, middle), OPENING_WINDOW),
        CandidateWindow(
            "credits", max(middle, target.duration_ms - FALLBACK_WINDOW_MS), target.duration_ms, ENDING_WINDOW
        ),
    ]
    assert [reference.id for reference in call.references] == [mate.id]  # the film in the same server is not one
    assert call.options["movie"] is False


# --- online suggestions ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/m/Movies/Up (2009) {tmdb-14160}/Up (2009).mkv", MediaIds("movie", tmdb="14160")),
        ("/m/Movies/Up (2009)/Up (2009) [tmdb-14160].mkv", MediaIds("movie", tmdb="14160")),
        ("/m/Movies/Heat (1995)/Heat (1995) {imdb-tt0113277}.mkv", MediaIds("movie", imdb="tt0113277")),
    ],
)
def test_a_films_ids_come_from_the_tags_in_its_names_without_asking_the_server(path, expected):
    asked = []
    assert online.film_ids(path, lambda: asked.append(path)) == expected
    assert asked == []


def test_a_film_without_tags_takes_its_ids_from_the_servers_answer():
    ids = online.film_ids(
        "/m/Movies/Toy Story (1995)/Toy Story (1995).mkv",
        lambda: {"kind": "movie", "tmdb": "862", "imdb": "tt0114709"},
    )
    assert ids == MediaIds("movie", tmdb="862", imdb="tt0114709")


def _explode():
    raise RuntimeError("the server fell over")


@pytest.mark.parametrize(
    "answer",
    [
        lambda: {"kind": "episode", "tmdb": "1396", "season": 1, "episode": 1},
        lambda: {"kind": "show", "tmdb": "1396"},
        lambda: {},
        lambda: None,
        _explode,
    ],
    ids=["an-episode", "a-show", "empty", "nothing", "failing"],
)
def test_an_item_the_server_does_not_know_as_a_film_is_not_a_film(answer):
    ids = online.film_ids("/m/Movies/Toy Story (1995)/Toy Story (1995).mkv", answer)
    assert ids.kind == "unknown" and ids.tmdb is None and ids.imdb is None


def test_a_file_named_like_an_episode_is_not_a_film_by_its_path():
    asked = []

    def fetch():
        asked.append(True)
        return None

    path = "/m/TV/Show {tvdb-81189}/Season 01/Show - S01E02.mkv"
    assert online.film_ids(path, fetch).kind == "unknown"
    assert asked == [True]  # the path gave no film, so the server was asked, and it knew none


@pytest.fixture
def make_lookups(tmp_path):
    markers = MarkerStore(str(tmp_path / "lookups.db"))
    store = RepriseStore(markers)

    def make(*databases):
        return online.OnlineLookups(store, {database.source: database for database in databases})

    yield make
    markers.close()


def ask(lookups, ids, duration):
    return lookups.windows(ids, duration_ms=duration, frame_rate=None, plex=[], priority=3)


def test_online_times_for_a_film_are_asked_by_the_films_own_ids(make_lookups):
    database = FilmDatabase([("credits", 6_900_000, LONG_FILM)])
    found = ask(make_lookups(database), MediaIds("movie", tmdb="14160", imdb="tt1049413"), LONG_FILM)
    assert [(w.kind, w.start_ms, w.end_ms, w.sources) for w in found] == [
        ("credits", 6_900_000, LONG_FILM, ("IntroDB",))
    ]
    assert database.asked == [("movie", "14160", "tt1049413", None, None, LONG_FILM, 3)]


def test_online_times_for_an_episode_are_asked_by_its_season_and_episode_as_before(make_lookups):
    database = FilmDatabase([("intro", 30000, 90000), ("credits", 2_300_000, 2_400_000)])
    ids = MediaIds("episode", None, "tt0903747", None, 1, 1)
    found = ask(make_lookups(database), ids, 2_400_000)
    assert [(w.kind, w.start_ms, w.end_ms) for w in found] == [
        ("intro", 30000, 90000),
        ("credits", 2_300_000, 2_400_000),
    ]
    assert database.asked == [("episode", None, "tt0903747", 1, 1, 2_400_000, 3)]


def test_an_item_of_unknown_kind_is_not_asked_about(make_lookups):
    database = FilmDatabase([("credits", 6_900_000, LONG_FILM)])
    assert ask(make_lookups(database), MediaIds(), LONG_FILM) == ()
    assert database.asked == []


class Server:
    """What Plex says about an item's external ids, counting the times it is asked."""

    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def get_external_ids(self, rating_key):
        self.calls.append(rating_key)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


class Registry:
    def __init__(self, server):
        self.server = server

    def get(self, server_id):
        return self.server


class Job:
    id, priority = "job", 3


class Manager:
    @staticmethod
    def is_cancellation_requested(job_id):
        return False


@pytest.fixture
def register_film(tmp_path):
    """Register one file with a store and a service that has no Plex, only a stand-in for the server's ids."""
    markers = MarkerStore(str(tmp_path / "markers.db"))
    store = RepriseStore(markers)

    def register(server, *, folder="Up (2009)", name="Up (2009).mkv", metadata=None):
        path = tmp_path / "library" / folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"film")
        version = media_version(str(path), LONG_FILM, STREAMS)
        identity = PlaybackIdentity("machine", "1", "2", "3", "/part/3", version.size_bytes, LONG_FILM, "123")
        if metadata is None:
            metadata = {
                "kind": "movie",
                "show_key": None,
                "season": None,
                "episode": None,
                "candidates": [{"kind": "credits", "start_ms": 6_900_000, "end_ms": LONG_FILM, "source": "plex"}],
            }
        store.register_media(version, identity, server_id="server", metadata=metadata)
        service = RepriseService(store, Registry(server), Manager())
        return service, version

    register.store = store
    yield register
    markers.close()


def film_windows(register_film, server, database, **options):
    service, version = register_film(server, **options)
    lookups = online.OnlineLookups(register_film.store, {database.source: database})
    return service._online_windows(lookups, version, service._bindings(version.id), [], Job), version


def test_the_service_keeps_only_the_credits_of_a_film_and_asks_for_it_once(register_film):
    server = Server(MOVIE_ANSWER)
    database = FilmDatabase([("intro", 30000, 90000), ("credits", 6_900_000, LONG_FILM)])
    found, version = film_windows(register_film, server, database)
    assert found == {version.id: (OnlineWindow("credits", 6_900_000, LONG_FILM, ("IntroDB",)),)}
    assert server.calls == ["1"]
    assert database.asked == [("movie", "14160", "tt1049413", None, None, LONG_FILM, 3)]


def test_a_film_tagged_in_its_folder_is_asked_for_without_asking_the_server(register_film):
    server = Server(MOVIE_ANSWER)
    database = FilmDatabase([("credits", 6_900_000, LONG_FILM)])
    found, version = film_windows(register_film, server, database, folder="Up (2009) {tmdb-14160}")
    assert list(found) == [version.id] and server.calls == []
    assert database.asked == [("movie", "14160", None, None, None, LONG_FILM, 3)]


@pytest.mark.parametrize("answer", [None, {}, {"kind": "episode", "tmdb": "1"}, RuntimeError("down")])
def test_a_film_the_server_cannot_name_is_not_asked_about_online(register_film, answer):
    database = FilmDatabase([("credits", 6_900_000, LONG_FILM)])
    found, _ = film_windows(register_film, Server(answer), database)
    assert found == {} and database.asked == []


def test_a_film_with_no_online_credits_has_no_windows(register_film):
    found, _ = film_windows(register_film, Server(MOVIE_ANSWER), FilmDatabase([("intro", 30000, 90000)]))
    assert found == {}


def test_an_online_lookup_that_breaks_gives_no_windows_instead_of_an_error(register_film):
    class Exploding:
        sources = (Source.INTRODB,)

        def windows(self, ids, **kwargs):
            raise RuntimeError("the database fell over")

    service, version = register_film(Server(MOVIE_ANSWER))
    assert service._online_windows(Exploding(), version, service._bindings(version.id), [], Job) == {}
    assert service._online_windows(None, version, service._bindings(version.id), [], Job) == {}


def test_an_episode_is_still_asked_for_with_its_intro_and_credits(register_film):
    metadata = {
        "show_key": "show",
        "season": 1,
        "episode": 1,
        "candidates": [],
    }
    episode = {"kind": "episode", "imdb": "tt0903747", "tmdb": "1396", "tvdb": "81189", "season": 1, "episode": 1}
    database = FilmDatabase([("intro", 30000, 90000), ("credits", 6_900_000, LONG_FILM)])
    found, version = film_windows(
        register_film, Server(episode), database, folder="Show", name="Show - S01E01.mkv", metadata=metadata
    )
    assert [w.kind for w in found[version.id]] == ["intro", "credits"]
    assert database.asked == [("episode", "1396", "tt0903747", 1, 1, LONG_FILM, 3)]


def test_a_file_registered_as_an_episode_is_not_asked_for_as_a_film(register_film):
    metadata = {"show_key": "show", "season": 1, "episode": 1, "candidates": []}
    database = FilmDatabase([("credits", 6_900_000, LONG_FILM)])
    found, _ = film_windows(
        register_film, Server(MOVIE_ANSWER), database, folder="Show", name="Pilot.mkv", metadata=metadata
    )
    assert found == {} and database.asked == []


def test_plex_and_an_online_database_agreeing_on_a_films_credits_are_one_suggestion(film_library, monkeypatch):
    fx = film_library
    database = FilmDatabase([("credits", *CREDITS)])
    online_with(fx, monkeypatch, database)
    _, _, result, done = analysed_film(fx, markers=[marker("credits", *CREDITS)])
    assert database.asked and database.asked[0][:5] == ("movie", "14160", "tt1049413", None, None)
    assert [(r.kind, r.origin, r.review_status, r.suggested_by) for r in result.ranges] == [
        ("credits", "plex", "proposed", ("Plex", "IntroDB"))
    ]
    assert any(
        reason.startswith("Plex and IntroDB suggested the credits") and FILM_NOT_CONFIRMED in reason
        for reason in result.reasons
    )
    (evidence,) = result.evidence
    assert evidence.reason == (
        "Plex and IntroDB put the credits here, but a film has no other episode to confirm it against."
    )
    assert done["outcome"]["state"] == "suggested"


def test_online_times_alone_are_an_unapproved_suggestion_for_a_film_and_an_intro_is_ignored(film_library, monkeypatch):
    fx = film_library
    online_with(fx, monkeypatch, FilmDatabase([("intro", 2000, 12000), ("credits", *CREDITS)]))
    _, identity, result, done = analysed_film(fx)
    assert [(r.kind, r.action, r.origin, r.review_status, r.suggested_by) for r in result.ranges] == [
        ("credits", "skip", "online", "proposed", ("IntroDB",))
    ]
    (evidence,) = result.evidence
    assert evidence.reason == "IntroDB put the credits here, but a film has no other episode to confirm it against."
    assert result.reasons[0] == MOVIE_NOTE
    assert eligible_ranges(result, identity.audio_stream_id) == ()
    assert done["outcome"]["state"] == "suggested"


def test_a_failing_online_lookup_does_not_fail_a_films_analysis(film_library, monkeypatch):
    class Exploding:
        sources = (Source.INTRODB,)

        def windows(self, ids, **kwargs):
            raise RuntimeError("the database fell over")

    fx = film_library
    monkeypatch.setattr(fx.service, "_online_lookups", lambda: Exploding())
    fx.world.external_ids["201"] = MOVIE_ANSWER
    _, _, result, done = analysed_film(fx, markers=[marker("credits", *CREDITS)])
    assert result.analysis_status == "complete"
    assert [(r.kind, r.origin, r.suggested_by) for r in result.ranges] == [("credits", "plex", ("Plex",))]
    assert done["outcome"]["state"] == "suggested"


# --- films and episodes in one server --------------------------------------------------------------------------------


def test_a_scheduled_whole_library_job_registers_films_and_episodes_without_mixing_them(film_library, monkeypatch):
    fx = film_library
    episode = fx.world.add_episode("101", "Pilot", number=1)
    film = fx.world.add_film("201", "Name", year=2001)
    last_runs = []
    monkeypatch.setattr(scheduler, "get_schedule_manager", lambda: SimpleNamespace(_update_last_run=last_runs.append))
    scheduler.execute_scheduled_job("schedule-1", ["1", "2"], "Everything", {"job_kinds": ["reprise"]}, 2, "server")
    wait_for_queue()
    matching = [job for job in fx.manager.get_all_jobs() if job.kind == "reprise"]
    assert len(matching) == 2
    assert all(job.parent_schedule_id == "schedule-1" and job.status.value == "completed" for job in matching)
    assert last_runs == ["schedule-1"]
    stored = registered(fx)
    assert set(stored) == {str(episode), str(film)}
    film_meta = stored[str(film)]
    assert (film_meta["library_id"], film_meta["kind"], film_meta["show_title"]) == ("2", "movie", "")
    assert (stored[str(episode)]["library_id"], stored[str(episode)]["show_title"]) == ("1", "Show")
    assert "kind" not in stored[str(episode)]
    film_result = fx.store.get_result(version_of(fx, film).id)
    episode_result = fx.store.get_result(version_of(fx, episode).id)
    assert film_result.analysis_status == episode_result.analysis_status == "complete"
    assert film_result.ranges == () and film_result.reasons[0] == MOVIE_NOTE
    assert episode_result.reasons[0] == NO_OTHER_EPISODES  # the film is not another episode of the season
    assert {(r.kind, r.action) for r in episode_result.ranges} == {("intro", "keep"), ("credits", "keep")}


def test_the_library_page_lists_a_film_by_its_title_with_no_show_or_season(film_library):
    fx = film_library
    fx.world.add_episode("103", "Pilot", number=3)
    film = fx.world.add_film("201", "Name", year=2001, markers=[marker("credits", *CREDITS)])
    refresh(fx, "1")
    refresh(fx, "2")
    analyse(fx, version_of(fx, film))
    page = fx.service.list_media(server_id="server")
    items = {item["title"]: item for item in page["items"]}
    assert set(items) == {"Name (2001)", "Pilot"}
    listed = items["Name (2001)"]
    assert (listed["show_title"], listed["season"], listed["episode"]) == ("", None, None)
    assert (items["Pilot"]["show_title"], items["Pilot"]["season"], items["Pilot"]["episode"]) == ("Show", 1, 3)
    assert page["shows"] == ["Show"] and page["seasons"] == [1]  # a film adds neither a show nor a season
    assert listed["analysis_status"] == "complete" and listed["reasons"][0] == MOVIE_NOTE
    counts = listed["ranges"]
    assert (counts["suggested"], counts["to_review"], counts["suggested_by"]) == (1, 0, ["Plex"])
    assert [item["title"] for item in fx.service.list_media(search="Name")["items"]] == ["Name (2001)"]
    assert [item["title"] for item in fx.service.list_media(show="Show")["items"]] == ["Pilot"]
