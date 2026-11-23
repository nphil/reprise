"""Automatic sending to Plex (``reprise/autosend.py``) against a real Plex SQLite file and the real publisher.

Only the clock, the alert post and, unless a test says otherwise, the daily backup are stand-ins. Every time is an
explicit ``now`` and nothing sleeps.
"""

import contextlib
import json
import os
import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import pytest
from loguru import logger

from media_preview_generator.markers.models import Marker, MarkerType
from media_preview_generator.markers.publishers import plex_db
from media_preview_generator.markers.store import MarkerStore
from media_preview_generator.reprise import autosend, plexbackup
from media_preview_generator.reprise.admission import admit_references
from media_preview_generator.reprise.alerts import TOKEN_FILE, Alerts
from media_preview_generator.reprise.autosend import AutoSender, label_of, sure_runs
from media_preview_generator.reprise.autostate import AutopilotState, Concerns
from media_preview_generator.reprise.identity import media_version
from media_preview_generator.reprise.models import AnalysisResult, DecisionRange, Evidence, PlaybackIdentity
from media_preview_generator.reprise.publishing import AUTO_ACTOR, ReprisePublisher
from media_preview_generator.reprise.report import episode_row
from media_preview_generator.reprise.service import RepriseService, _mapping_fingerprint
from media_preview_generator.reprise.store import RepriseStore, RevisionConflict
from media_preview_generator.reprise.verification_profile import POLICY_VERSION, PROFILE_FINGERPRINT, PROFILE_ID
from media_preview_generator.servers.registry import ServerRegistry
from scripts.plex_db_node_fixture import build

T0 = 1_699_963_200  # 2023-11-14 12:00:00 UTC
DURATION = 100_000  # every file runs 100 seconds
TOKEN = "tk_s3cret-TOKEN-value"
INTRO = "intro-10000"  # the range id that ``confirmed(ep, "intro", 10_000, ...)`` gives
CREDITS = "credits-80000"
TABLES = ("tags", "taggings", "metadata_items", "media_items", "media_parts", "media_streams")

NOTHING_SURE = "Nothing in it is confirmed for sure by picture and sound."
OWNER_NOTE = "You changed, locked or keep something in the intro, so it needs your approval."
ALREADY_APPROVED = "You already approved part of it, so sending it is up to you."
NOT_ELIGIBLE = "Approved skips are not fully eligible for this playback identity"
NOT_FINISHED = "An earlier change to Plex for it is not finished."
UNDONE = "A change sent to Plex for it was undone, so what happens next is yours to decide."
REFUSED_TITLE = "Plex refused an automatic change"
REFUSED_PAUSE = "Paused for a while: Plex refused an automatic change."
UNCERTAIN_PAUSE = "Paused for a while: Reprise can't tell whether Plex took a change."
UNCERTAIN_TITLE = "Check an automatic change in Plex"
UNCERTAIN_MESSAGE = (
    "Reprise can't tell whether Plex took the automatic change for Community S01E01. It paused automatic sending "
    "for 6 hours and will not repeat the change. Please check it in Reprise."
)
BACKUP_WAIT = "Waiting: Reprise could not back up Plex's database."


# ── stand-ins ─────────────────────────────────────────────────────────────────────────────────────────────────────


class Response:
    def __init__(self, status_code):
        self.status_code = status_code


class FakePost:
    """Stands in for the ntfy request: records every alert that would reach the phone."""

    def __init__(self):
        self.calls = []

    def __call__(self, url, *, data, headers, timeout):
        self.calls.append({"url": url, "data": data, "headers": dict(headers), "timeout": timeout})
        return Response(200)

    def titles(self):
        return [call["headers"]["Title"] for call in self.calls]

    def messages(self):
        return [call["data"].decode() for call in self.calls]


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now


class Backups:
    """Stands in for the daily Plex backup: records each call and what Plex's rows were at that moment."""

    def __init__(self, plex):
        self.plex, self.calls, self.error = plex, [], None

    def __call__(self, database, directory, *, now):
        self.calls.append({"database": database, "directory": directory, "now": now, "rows": _rows(self.plex)})
        if self.error is not None:
            raise self.error
        return os.path.join(directory, "backup.bak"), True


class Flaky:
    """Stands in for a publisher method: raises ``errors`` on its first calls, then does what the real one does."""

    def __init__(self, real, *errors):
        self.real, self.errors, self.calls = real, list(errors), 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return self.real(*args, **kwargs)


# ── Plex database helpers ─────────────────────────────────────────────────────────────────────────────────────────


@contextlib.contextmanager
def _plex(path):
    """A connection to a Plex database file that commits and closes afterwards."""
    with contextlib.closing(sqlite3.connect(path)) as conn, conn:
        yield conn


def _rows(path):
    with _plex(path) as db:
        rows = {table: db.execute(f"SELECT * FROM {table} ORDER BY id").fetchall() for table in TABLES}
        rows["sqlite_sequence"] = db.execute("SELECT * FROM sqlite_sequence ORDER BY name").fetchall()
        return rows


def _marker_rows(path, kind, item=1):
    with _plex(path) as db:
        return db.execute(
            "SELECT * FROM taggings WHERE metadata_item_id=? AND text=? ORDER BY id", (item, kind)
        ).fetchall()


def _marker_times(path, kind, item=1):
    with _plex(path) as db:
        return db.execute(
            "SELECT time_offset, end_time_offset FROM taggings WHERE metadata_item_id=? AND text=? ORDER BY id",
            (item, kind),
        ).fetchall()


def _payload(path, key):
    with _plex(path) as db:
        extra = db.execute("SELECT extra_data FROM media_parts WHERE id=1").fetchone()[0]
    return plex_db.decode_extra_data(extra)[0].get(key)


def _seed_marker(path, kind, start, end, index):
    """A Plex-native marker row plus its pv: part payload on episode 1, written the way the legacy writer does."""
    marker = Marker(MarkerType(kind), start, end, ("plex",))
    stored_start, stored_end, extra = plex_db._stored_row(marker, DURATION)
    key = "pv:intros" if kind == "intro" else "pv:credits"
    with _plex(path) as db:
        db.execute(
            "INSERT INTO taggings (metadata_item_id,tag_id,[index],text,time_offset,end_time_offset,extra_data) "
            "VALUES (1,12,?,?,?,?,?)",
            (index, kind, stored_start, stored_end, extra),
        )
        current = db.execute("SELECT extra_data FROM media_parts WHERE id=1").fetchone()[0]
        fields, url_form = plex_db.decode_extra_data(current)
        fields[key] = plex_db._part_payload(marker.type, [marker], DURATION)
        db.execute(
            "UPDATE media_parts SET extra_data=? WHERE id=1", (plex_db.encode_extra_data(fields, url_form=url_form),)
        )


# ── ranges and results ────────────────────────────────────────────────────────────────────────────────────────────


def _signature(audio):
    return json.dumps({"audio_streams": [{"plex_id": audio, "index": 1}], "start_time_ms": 0, "streams": []})


def _decision(rid, kind, start, end, evidence_id, audio, **fields):
    found = {
        "id": rid,
        "kind": kind,
        "action": "skip",
        "start_ms": start,
        "end_ms": end,
        "review_status": "proposed",
        "locked": False,
        "origin": "detector",
        "reason_code": "same",
        "reason": "",
        "evidence_ids": (evidence_id,),
        "audio_stream_ids": (audio,),
    }
    return DecisionRange(**{**found, **fields})


def piece(rid, kind="intro", start=10_000, end=20_000, **fields):
    """A range of a result that the detector confirmed, unless ``fields`` change it."""
    return _decision(rid, kind, start, end, f"e-{rid}", "1", **fields)


def pure_result(*ranges):
    return AnalysisResult("v", "a", 1, "complete", "current", "s", POLICY_VERSION, tuple(ranges), (), ())


def _metrics(checked, provenance):
    return {
        "checked_audio_stream_ids": list(checked),
        "admission_provenance": provenance,
        "verification_profile": PROFILE_ID,
        "profile_fingerprint": PROFILE_FINGERPRINT,
    }


def confirmed(ep, kind, start, end, *, checked=None, **fields):
    """What the detector stores for a run it confirmed by comparing ``ep`` with another episode: (evidence, range)."""
    rid = f"{kind}-{start}"
    evidence = Evidence(
        f"e-{rid}",
        start,
        end,
        "audiovisual",
        "match",
        "same",
        "",
        ep.reference.id,
        start,
        end,
        _metrics([ep.audio] if checked is None else checked, ep.provenance),
    )
    return [evidence], _decision(rid, kind, start, end, evidence.id, ep.audio, **fields)


def suggestion(ep, kind, start, end, origin="plex"):
    """A suggestion nothing confirmed, as the analysis stores it: Plex's own marker or an online database's times."""
    plex = origin == "plex"
    rid = f"{origin}-{kind}"
    evidence = Evidence(
        f"e-{rid}",
        start,
        end,
        "audiovisual",
        "unknown",
        "plex_marker_unconfirmed" if plex else "online_times_unconfirmed",
        "",
        None,
        None,
        None,
        _metrics([], None),
    )
    found = _decision(
        rid,
        kind,
        start,
        end,
        evidence.id,
        ep.audio,
        origin=origin,
        reason_code="plex_suggestion" if plex else "online_suggestion",
        suggested_by=("Plex",) if plex else ("IntroDB",),
    )
    return [evidence], found


def detector_keep(ep, kind, start, end):
    """A scene the analysis itself marks to keep (the episodes differ there)."""
    rid = f"keep-{start}"
    evidence = Evidence(
        f"e-{rid}",
        start,
        end,
        "audiovisual",
        "different",
        "different",
        "",
        ep.reference.id,
        start,
        end,
        _metrics([ep.audio], ep.provenance),
    )
    return [evidence], _decision(rid, kind, start, end, evidence.id, ep.audio, action="keep", reason_code="different")


# ── one real app ──────────────────────────────────────────────────────────────────────────────────────────────────


class Lab:
    """A real Reprise store, publisher and Plex database file with an automatic sender on top."""

    def __init__(self, tmp_path, monkeypatch):
        self.dir = tmp_path
        self.library = tmp_path / "library"
        self.library.mkdir()
        folder = tmp_path / "plex"
        self.plex = build(folder, "fixture-machine")
        with _plex(self.plex) as db:
            db.execute("INSERT INTO tags (id, tag, tag_type) VALUES (12, '', 12)")
        libraries = [{"id": "1", "name": "fixture", "enabled": True, "remote_paths": [str(self.library)]}]
        self.registry = ServerRegistry.from_settings(
            [
                {
                    "id": "local",
                    "type": "plex",
                    "name": "fixture",
                    "enabled": True,
                    "url": "http://127.0.0.1:1",
                    "auth": {},
                    "server_identity": "fixture-machine",
                    "output": {"plex_config_folder": str(folder)},
                    "libraries": libraries,
                }
            ]
        )
        self.marker = MarkerStore(str(tmp_path / "markers.db"))
        self.store = RepriseStore(self.marker)
        self.service = RepriseService(self.store, self.registry, None)
        self.publisher = ReprisePublisher(self.store, self.registry, self.service, writes_enabled=True)
        monkeypatch.setattr(
            self.publisher, "_database_for", lambda server_id, identity: plex_db.LocalPlexDb(lambda: str(self.plex))
        )
        self.service.publisher = self.publisher
        (tmp_path / TOKEN_FILE).write_text(f"{TOKEN}\n")
        self.post, self.clock, self.switch = FakePost(), Clock(), {"on": True}
        self.alerts = Alerts(self.store, config_dir=str(tmp_path), post=self.post, clock=self.clock)
        self.state = AutopilotState(self.store)
        self.concerns = Concerns(self.alerts, self.state)
        self.backups = Backups(self.plex)
        self.sender = self.new_sender()
        self._references = {}

    def new_sender(self, **options):
        """A sender on the same database (a restart, when it replaces the old one); ``backup=None`` is the real one."""
        settings = {
            "config_dir": str(self.dir),
            "enabled": lambda: self.switch["on"],
            "clock": self.clock,
            "backup": self.backups,
        }
        return AutoSender(self.service, self.concerns, self.state, **{**settings, **options})

    def sweep(self, now, sender=None):
        self.clock.now = now
        return (sender or self.sender).sweep(now)

    def rows(self):
        return _rows(self.plex)

    def query(self, sql, *params):
        with self.store.marker_store._reading() as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def attempts(self, ep):
        return self.query(
            "SELECT revision, outcome, detail, tries, change_id, retry_after FROM reprise_autopilot_attempts "
            "WHERE version_id=? ORDER BY revision",
            ep.vid,
        )

    def applied(self):
        return self.query(
            "SELECT COUNT(*) AS n, COUNT(DISTINCT media_version_id) AS episodes FROM reprise_publication_changes "
            "WHERE state='applied'"
        )[0]

    def _metadata(self, path, season, episode, **extra):
        cfg = self.registry.get_config("local")
        return {
            "library_id": "1",
            "show_key": "show",
            "show_title": "Community",
            "season": season,
            "episode": episode,
            "title": f"Episode {episode}",
            "remote_part_path": str(path),
            "mapping_fingerprint": _mapping_fingerprint(cfg, cfg.libraries[0]),
            **extra,
        }

    def _reference(self, season):
        """The other episode of ``season`` every target is compared with; it needs no rows in Plex."""
        if season not in self._references:
            path = self.library / f"reference-s{season}.mkv"
            path.write_bytes(f"reference-fixture-{season}".encode())
            key = str(20 + season)
            version = media_version(str(path), DURATION, _signature("1"))
            identity = PlaybackIdentity(
                "fixture-machine", key, key, key, f"/library/parts/{key}/file.mkv", path.stat().st_size, DURATION, "1"
            )
            self.store.register_media(version, identity, server_id="local", metadata=self._metadata(path, season, 99))
            self._references[season] = version
        return self._references[season]

    def episode(self, number=1, season=1, **meta):
        """Register episode ``number``: its file, its rows in Plex and its binding. ``meta`` adds stored metadata
        (``kind="movie"``). The analysis comes from ``analyse``, after this, so the binding never changes under it."""
        path = self.library / f"episode-{number}.mkv"
        path.write_bytes(f"private-media-fixture-{number}".encode())
        size = path.stat().st_size
        with _plex(self.plex) as db:
            if number == 1:
                db.execute(
                    "UPDATE media_parts SET file=?, size=?, duration=?, updated_at=? WHERE id=1",
                    (str(path), size, DURATION, 101),
                )
                db.execute("UPDATE media_items SET duration=? WHERE id=1", (DURATION,))
            else:
                db.execute(
                    "INSERT INTO metadata_items (id, library_section_id, metadata_type, title) VALUES (?, 1, 4, ?)",
                    (number, f"Episode {number}"),
                )
                db.execute(
                    "INSERT INTO media_items (id, metadata_item_id, duration) VALUES (?, ?, ?)",
                    (number, number, DURATION),
                )
                db.execute(
                    "INSERT INTO media_parts (id, media_item_id, file, size, duration, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, 101)",
                    (number, number, str(path), size, DURATION),
                )
                db.execute(
                    'INSERT INTO media_streams (id, stream_type_id, "index", media_part_id, media_item_id) '
                    "VALUES (?, 2, 1, ?, ?)",
                    (number, number, number),
                )
        audio = str(number)
        version = media_version(str(path), DURATION, _signature(audio))
        identity = PlaybackIdentity(
            "fixture-machine", audio, audio, audio, f"/library/parts/{number}/file.mkv", size, DURATION, audio
        )
        reference = self._reference(season)
        metadata = self._metadata(path, season, number, **meta)
        self.store.register_media(version, identity, server_id="local", metadata=metadata)
        [(_, provenance)] = admit_references(version, [reference], self.store)
        return SimpleNamespace(
            number=number,
            path=path,
            version=version,
            vid=version.id,
            identity=identity,
            audio=audio,
            reference=reference,
            provenance=provenance,
        )

    def refresh(self, ep, season=1, **seen):
        """A library refresh reads ``ep`` from Plex again and registers its binding with what Plex says now: what it
        said before with ``seen`` on top (its markers as ``candidates``, a new ``title``, another ``library_id``)."""
        metadata = self._metadata(ep.path, season, ep.number, **seen)
        self.store.register_media(ep.version, ep.identity, server_id="local", metadata=metadata)

    def replace_file(self, ep):
        """An upgrade replaces ``ep``'s file while Plex's part keeps its ids: the new file is registered under the
        same binding. Returns the new version."""
        ep.path.write_bytes(ep.path.read_bytes() + b"-upgraded")
        version = media_version(str(ep.path), DURATION, _signature(ep.audio))
        identity = replace(ep.identity, part_size_bytes=version.size_bytes)
        self.store.register_media(version, identity, server_id="local", metadata=self._metadata(ep.path, 1, ep.number))
        return version

    def analyse(self, ep, *parts):
        """Store a finished analysis of ``ep`` from ``(evidence, range)`` parts. Nothing is reviewed or approved."""
        run = self.store.begin_analysis(ep.vid, "job", "signature")
        evidence = [e for found, _ in parts for e in found]
        result = self.store.finish_analysis(run, evidence, [r for _, r in parts], ep.vid)
        return result.result_revision

    def revision(self, ep):
        return self.store.get_result(ep.vid).result_revision

    def ranges(self, ep):
        return {r.id: r for r in self.store.get_result(ep.vid).ranges}

    def owner_review(self, ep, edits):
        """What the owner does in the review screen."""
        return self.store.save_review(ep.vid, self.revision(ep), edits, "owner")

    def prepare_as_owner(self, ep, rid=INTRO):
        """The owner approves a range and prepares its change by hand, as the report's Send button does."""
        result = self.owner_review(ep, [{"range_id": rid, "review_status": "approved"}])
        return self.publisher.prepare(ep.vid, result.result_revision, ep.identity, "owner")

    def start_a_send(self, ep, revision, ids=(INTRO,)):
        """What an automatic send had done when the process died after preparing: approved, prepared, ``working``."""
        self.store.auto_approve(ep.vid, revision, list(ids))
        change = self.publisher.prepare(ep.vid, revision + 1, ep.identity, AUTO_ACTOR)
        self.sender._record(ep.vid, revision, "working", now=T0, range_ids=list(ids))
        return change


@pytest.fixture
def lab(tmp_path, monkeypatch):
    app = Lab(tmp_path, monkeypatch)
    yield app
    if not app.marker._closed:
        app.marker.close()


@pytest.fixture
def logs():
    lines = []
    sink = logger.add(lines.append, level="DEBUG")
    yield lines
    logger.remove(sink)


def assert_declined(lab, ep, before, detail):
    """The episode was left for the owner with this reason, and Plex, the journal and the backups were not touched."""
    assert lab.rows() == before
    assert lab.publisher.list_changes(ep.vid) == []
    assert lab.backups.calls == []
    last = lab.attempts(ep)[-1]
    assert (last["outcome"], last["detail"]) == ("declined", detail)


def sure_intro(lab, number=1):
    """Episode ``number`` with an intro the detector confirmed and nothing else."""
    ep = lab.episode(number)
    lab.analyse(ep, confirmed(ep, "intro", 10_000, 20_000))
    return ep


# ── which results qualify (pure) ──────────────────────────────────────────────────────────────────────────────────


def test_one_confirmed_skip_is_a_run():
    found = piece("a")
    assert sure_runs(pure_result(found), frozenset(), ()) == ({"intro": [found]}, [])


def test_two_pieces_that_touch_exactly_are_one_run_in_order():
    first, second = piece("a", start=10_000, end=15_000), piece("b", start=15_000, end=20_000)
    assert sure_runs(pure_result(second, first), frozenset(), ()) == ({"intro": [first, second]}, [])


def test_a_one_millisecond_gap_declines_that_kind_with_a_note():
    credits = piece("c", "credits", 80_000, 95_000)
    result = pure_result(piece("a", start=10_000, end=14_999), piece("b", start=15_000, end=20_000), credits)
    runs, notes = sure_runs(result, frozenset(), ())
    assert runs == {"credits": [credits]}
    assert notes == ["The confirmed intro has gaps in it, so it needs your approval."]


@pytest.mark.parametrize("origin", ["plex", "online"])
def test_a_suggestion_never_qualifies(origin):
    who = ("Plex",) if origin == "plex" else ("IntroDB",)
    suggested = piece("s", origin=origin, reason_code=f"{origin}_suggestion", suggested_by=who)
    assert sure_runs(pure_result(suggested), frozenset(), ()) == ({}, [])


def test_a_bridged_piece_does_not_qualify():
    bridged = piece("m", start=14_000, end=16_000, reason_code="native_signal_bridged")
    assert sure_runs(pure_result(bridged), frozenset(), ()) == ({}, [])


def test_a_hole_that_was_only_bridged_between_confirmed_pieces_is_a_gap():
    left, right = piece("a", start=10_000, end=14_000), piece("b", start=16_000, end=20_000)
    bridged = piece("m", start=14_000, end=16_000, reason_code="native_signal_bridged")
    runs, notes = sure_runs(pure_result(left, bridged, right), frozenset(), ())
    assert runs == {}
    assert notes == ["The confirmed intro has gaps in it, so it needs your approval."]


@pytest.mark.parametrize(
    ("extra", "decided", "keeps"),
    [
        pytest.param([], frozenset(), ((15_000, 16_000),), id="a_keep_inside_the_run"),
        pytest.param([], frozenset(), ((5_000, 12_000),), id="a_keep_over_its_start"),
        pytest.param(
            [piece("lock", start=18_000, end=25_000, origin="manual", locked=True)],
            frozenset(),
            (),
            id="a_locked_range",
        ),
        pytest.param(
            [piece("mine", "credits", 19_000, 30_000, origin="manual")],
            frozenset({"mine"}),
            (),
            id="a_range_the_owner_decided",
        ),
    ],
)
def test_something_the_owner_did_inside_the_span_declines_that_kind(extra, decided, keeps):
    runs, notes = sure_runs(pure_result(piece("a"), *extra), decided, keeps)
    assert runs == {}
    assert notes == [OWNER_NOTE]


@pytest.mark.parametrize(
    ("extra", "decided", "keeps"),
    [
        pytest.param([], frozenset(), ((5_000, 10_000),), id="a_keep_that_ends_where_the_run_starts"),
        pytest.param([], frozenset(), ((20_000, 25_000),), id="a_keep_that_starts_where_the_run_ends"),
        pytest.param([], frozenset(), ((30_000, 40_000),), id="a_keep_elsewhere"),
        pytest.param(
            [piece("lock", start=30_000, end=40_000, origin="manual", locked=True)],
            frozenset(),
            (),
            id="a_lock_elsewhere",
        ),
        pytest.param(
            [piece("mine", "credits", 60_000, 70_000, origin="manual")],
            frozenset({"mine"}),
            (),
            id="a_decision_elsewhere",
        ),
    ],
)
def test_something_the_owner_did_outside_the_span_does_not_stop_the_run(extra, decided, keeps):
    found = piece("a")
    assert sure_runs(pure_result(found, *extra), decided, keeps) == ({"intro": [found]}, [])


def test_any_approved_skip_declines_everything():
    approved = piece("x", "credits", 80_000, 95_000, origin="manual", review_status="approved")
    assert sure_runs(pure_result(piece("a"), approved), frozenset(), ()) == ({}, [ALREADY_APPROVED])


def test_a_confirmed_intro_with_only_suggested_credits_gives_only_the_intro():
    found = piece("a")
    plex_credits = piece("c", "credits", 80_000, 95_000, origin="plex", reason_code="plex_suggestion")
    assert sure_runs(pure_result(found, plex_credits), frozenset(), ()) == ({"intro": [found]}, [])


def test_both_kinds_confirmed_gives_both():
    intro, credits = piece("a"), piece("c", "credits", 80_000, 95_000)
    assert sure_runs(pure_result(credits, intro), frozenset(), ()) == ({"intro": [intro], "credits": [credits]}, [])


@pytest.mark.parametrize(
    ("meta", "label"),
    [
        ({"show_title": "Community", "season": 1, "episode": 2}, "Community S01E02"),
        ({"show_title": "Community", "season": 10, "episode": 12, "title": "Pilot"}, "Community S10E12"),
        ({"show_title": "Community", "title": "Pilot"}, "Pilot"),
        ({"show_title": "Community"}, "Community"),
        ({"show_title": "Community", "season": "1", "episode": 2, "title": "Pilot"}, "Pilot"),
        ({"title": "Pilot", "season": 1, "episode": 2}, "Pilot"),
        ({}, "an episode"),
    ],
)
def test_label_of_names_an_episode_the_way_a_person_would(meta, label):
    assert label_of(meta) == label


# ── the dry run, on its own ───────────────────────────────────────────────────────────────────────────────────────


def _projection_inputs(kind, start, end, *, tracks=("1",), checked=("1",), reviewed=("1",)):
    """A result with one confirmed run, the audio tracks Plex knows the file by, and the run grouped by its kind."""
    evidence = Evidence(
        "e", start, end, "audiovisual", "match", "same", "", "other-episode", start, end, _metrics(checked, None)
    )
    found = piece("r", kind, start, end, evidence_ids=("e",), audio_stream_ids=tuple(reviewed))
    result = AnalysisResult("v", "a", 1, "complete", "current", "s", POLICY_VERSION, (found,), (evidence,), ())
    bindings = [(PlaybackIdentity("m", "1", "1", "1", "/p", 1, DURATION, track), "local", {}) for track in tracks]
    return result, bindings, {kind: [found]}


def test_the_dry_run_accepts_a_run_every_audio_track_confirmed():
    result, bindings, runs = _projection_inputs(
        "intro", 10_000, 20_000, tracks=("1", "2"), checked=("1", "2"), reviewed=("1", "2")
    )
    AutoSender._shape(result, bindings, runs, ["intro"])


def test_the_dry_run_refuses_a_run_one_audio_track_was_not_reviewed():
    result, bindings, runs = _projection_inputs("intro", 10_000, 20_000, tracks=("1", "2"))
    with pytest.raises(ValueError, match="not fully eligible"):
        AutoSender._shape(result, bindings, runs, ["intro"])


def test_the_dry_run_refuses_a_run_whose_sound_was_never_compared():
    result, bindings, runs = _projection_inputs("intro", 10_000, 20_000, checked=())
    with pytest.raises(ValueError, match="not fully eligible"):
        AutoSender._shape(result, bindings, runs, ["intro"])


@pytest.mark.parametrize(
    ("kind", "start", "end", "error"),
    [
        ("intro", 0, 20_000, None),
        ("credits", 2_000, 50_000, None),
        ("credits", 1_999, 50_000, "Plex cannot represent this credits start exactly"),
        ("credits", 80_000, 97_999, None),
        ("credits", 80_000, 98_000, "The final two seconds cannot be projected safely to Plex"),
    ],
)
def test_the_dry_run_edges_of_what_plex_can_take(kind, start, end, error):
    result, bindings, runs = _projection_inputs(kind, start, end)
    if error is None:
        AutoSender._shape(result, bindings, runs, [kind])
    else:
        with pytest.raises(ValueError) as refused:
            AutoSender._shape(result, bindings, runs, [kind])
        assert str(refused.value) == error


# ── a sure intro, end to end ──────────────────────────────────────────────────────────────────────────────────────


def test_a_sure_intro_is_sent_to_plex_and_recorded_as_automatic(lab):
    ep = sure_intro(lab)
    before = lab.rows()

    assert lab.sweep(T0) == 1

    after = lab.rows()
    assert after["taggings"] != before["taggings"]
    assert _marker_times(lab.plex, "intro") == [(10_000, 20_000)]
    [change] = lab.publisher.list_changes(ep.vid)
    assert (change["state"], change["actor"]) == ("applied", AUTO_ACTOR)
    intro = lab.ranges(ep)[INTRO]
    assert (intro.review_status, intro.locked) == ("approved", False)
    assert lab.store.owner_decided_ids(ep.vid) == frozenset()
    assert lab.query("SELECT COUNT(*) AS n FROM reprise_manual_overrides WHERE version_id=?", ep.vid) == [{"n": 0}]
    assert lab.publisher.automatically_published([ep.vid]) == {ep.vid}
    assert lab.publisher.automatic_hold(ep.vid) == "It was already sent to Plex automatically."
    assert [(a["revision"], a["outcome"], a["change_id"]) for a in lab.attempts(ep)] == [
        (1, "sent", change["id"]),
        (2, "sent", change["id"]),
    ]

    assert lab.sweep(T0 + 60) == 0  # a second pass finds nothing left to do

    assert lab.rows() == after
    assert len(lab.publisher.list_changes(ep.vid)) == 1
    assert [a["outcome"] for a in lab.attempts(ep)] == ["sent", "sent"]


def test_the_report_marks_an_automatic_send(lab):
    ep = sure_intro(lab)
    assert lab.sweep(T0) == 1
    item = {
        "id": ep.vid,
        "server_id": "local",
        "show_title": "Community",
        "season": 1,
        "episode": 1,
        "title": "Episode 1",
        "duration_ms": DURATION,
    }
    current, why = lab.service._lookup(ep.identity)

    row = episode_row(
        item,
        lab.store.get_result(ep.vid),
        current,
        why,
        None,
        (),
        lab.publisher.published_versions([ep.vid])[ep.vid],
        ep.vid in lab.publisher.automatically_published([ep.vid]),
    )

    codes = [flag["code"] for flag in row["flags"]]
    assert row["published"] is True and row["published_automatically"] is True
    assert "published_auto" in codes and "published" not in codes


def test_the_confirmed_intro_is_replaced_and_plexs_own_credits_stay_exactly_as_they_are(lab):
    ep = lab.episode(1)
    _seed_marker(lab.plex, "credits", 80_000, 95_000, 0)
    lab.analyse(ep, confirmed(ep, "intro", 10_000, 20_000), suggestion(ep, "credits", 80_000, 95_000))
    credits_before = _marker_rows(lab.plex, "credits"), _payload(lab.plex, "pv:credits")
    assert credits_before[0] and credits_before[1]

    assert lab.sweep(T0) == 1

    assert (_marker_rows(lab.plex, "credits"), _payload(lab.plex, "pv:credits")) == credits_before
    assert _marker_times(lab.plex, "intro") == [(10_000, 20_000)]
    ranges = lab.ranges(ep)
    assert ranges[INTRO].review_status == "approved"
    assert ranges["plex-credits"].review_status == "proposed"


def test_the_confirmed_credits_are_replaced_and_plexs_own_intro_stays_exactly_as_it_is(lab):
    ep = lab.episode(1)
    _seed_marker(lab.plex, "intro", 10_000, 20_000, 1)  # Plex numbers its rows by text, so credits come first
    lab.analyse(ep, confirmed(ep, "credits", 80_000, 95_000), suggestion(ep, "intro", 10_000, 20_000))
    intro_before = _marker_rows(lab.plex, "intro"), _payload(lab.plex, "pv:intros")
    assert intro_before[0] and intro_before[1]

    assert lab.sweep(T0) == 1

    assert (_marker_rows(lab.plex, "intro"), _payload(lab.plex, "pv:intros")) == intro_before
    assert len(_marker_rows(lab.plex, "credits")) == 1
    ranges = lab.ranges(ep)
    assert ranges[CREDITS].review_status == "approved"
    assert ranges["plex-intro"].review_status == "proposed"


def test_two_touching_confirmed_pieces_are_sent_as_one_marker(lab):
    ep = lab.episode(1)
    lab.analyse(ep, confirmed(ep, "intro", 10_000, 15_000), confirmed(ep, "intro", 15_000, 20_000))

    assert lab.sweep(T0) == 1

    assert _marker_times(lab.plex, "intro") == [(10_000, 20_000)]
    assert {r.review_status for r in lab.ranges(ep).values()} == {"approved"}
    assert len(lab.publisher.list_changes(ep.vid)) == 1


def test_both_confirmed_kinds_are_sent_together_as_one_change(lab):
    ep = lab.episode(1)
    lab.analyse(ep, confirmed(ep, "intro", 10_000, 20_000), confirmed(ep, "credits", 80_000, 95_000))

    assert lab.sweep(T0) == 1

    assert _marker_times(lab.plex, "intro") == [(10_000, 20_000)]
    assert len(_marker_rows(lab.plex, "credits")) == 1
    assert len(lab.publisher.list_changes(ep.vid)) == 1
    assert {r.review_status for r in lab.ranges(ep).values()} == {"approved"}


# ── what is never sent ────────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("origin", ["plex", "online"])
def test_a_suggestion_alone_is_never_sent(lab, origin):
    ep = lab.episode(1)
    lab.analyse(ep, suggestion(ep, "intro", 10_000, 20_000, origin))
    before = lab.rows()

    assert lab.sweep(T0) == 0

    assert_declined(lab, ep, before, NOTHING_SURE)


def test_a_gap_inside_the_confirmed_pieces_is_never_sent(lab):
    ep = lab.episode(1)
    lab.analyse(ep, confirmed(ep, "intro", 10_000, 14_999), confirmed(ep, "intro", 15_000, 20_000))
    before = lab.rows()

    assert lab.sweep(T0) == 0

    assert_declined(lab, ep, before, "The confirmed intro has gaps in it, so it needs your approval.")


def test_a_keep_the_owner_added_inside_the_run_stops_it_being_sent(lab):
    ep = sure_intro(lab)
    lab.owner_review(ep, [{"kind": "intro", "action": "keep", "start_ms": 12_000, "end_ms": 14_000}])
    before = lab.rows()

    assert lab.sweep(T0) == 0

    assert_declined(lab, ep, before, OWNER_NOTE)


def test_a_range_the_owner_approved_is_left_to_the_owner(lab):
    ep = sure_intro(lab)
    lab.owner_review(ep, [{"range_id": INTRO, "review_status": "approved"}])
    before = lab.rows()

    assert lab.sweep(T0) == 0

    assert_declined(lab, ep, before, ALREADY_APPROVED)


def test_a_range_the_owner_locked_is_left_to_the_owner(lab):
    ep = sure_intro(lab)
    lab.owner_review(ep, [{"range_id": INTRO, "locked": True}])
    before = lab.rows()

    assert lab.sweep(T0) == 0

    assert_declined(lab, ep, before, NOTHING_SURE)


def test_a_film_is_never_sent(lab):
    ep = lab.episode(1, kind="movie")
    lab.analyse(ep, confirmed(ep, "intro", 10_000, 20_000))
    before = lab.rows()

    assert lab.sweep(T0) == 0

    assert_declined(lab, ep, before, "Films are never sent to Plex automatically.")


def test_a_special_is_never_sent(lab):
    ep = lab.episode(1, season=0)
    lab.analyse(ep, confirmed(ep, "intro", 10_000, 20_000))
    before = lab.rows()

    assert lab.sweep(T0) == 0

    assert_declined(lab, ep, before, "Specials are never sent to Plex automatically.")


def test_an_episode_the_owner_already_sent_is_left_alone(lab):
    ep = sure_intro(lab)
    change = lab.prepare_as_owner(ep)
    lab.publisher.apply(change["id"], "owner")
    written = lab.rows()

    assert lab.sweep(T0) == 0

    assert lab.rows() == written
    assert [(c["state"], c["actor"]) for c in lab.publisher.list_changes(ep.vid)] == [("applied", "owner")]
    assert lab.backups.calls == []
    assert lab.attempts(ep)[-1]["detail"] == "It was already sent to Plex by you."


def test_an_episode_whose_change_the_owner_undid_is_left_alone(lab):
    ep = sure_intro(lab)
    change = lab.prepare_as_owner(ep)
    lab.publisher.apply(change["id"], "owner")
    lab.publisher.undo(change["id"], "owner")
    undone = lab.rows()

    assert lab.sweep(T0) == 0

    assert lab.rows() == undone
    assert [c["state"] for c in lab.publisher.list_changes(ep.vid)] == ["undone"]
    assert lab.backups.calls == []
    assert lab.attempts(ep)[-1]["detail"] == UNDONE


def test_a_result_that_is_not_current_is_looked_at_again_later_and_nothing_is_written(lab):
    ep = sure_intro(lab)
    ep.path.write_bytes(ep.path.read_bytes() + b" changed after the analysis")
    assert lab.store.get_result(ep.vid).freshness == "stale"
    before = lab.rows()

    assert lab.sweep(T0) == 0

    assert lab.rows() == before
    assert lab.publisher.list_changes(ep.vid) == []
    assert lab.backups.calls == []
    [attempt] = lab.attempts(ep)
    assert (attempt["outcome"], attempt["detail"]) == ("retry", "Its analysis is not current yet.")
    assert attempt["retry_after"] == T0 + autosend.RECHECK_S


# ── the switches ──────────────────────────────────────────────────────────────────────────────────────────────────


def test_switching_automatic_sending_off_sends_and_records_nothing(lab):
    ep = sure_intro(lab)
    lab.switch["on"] = False
    before = lab.rows()

    assert lab.sender.blocked(T0) == "Automatic sending is switched off."
    assert lab.sweep(T0) == 0

    assert lab.rows() == before
    assert lab.attempts(ep) == [] and lab.publisher.list_changes(ep.vid) == [] and lab.backups.calls == []
    lab.switch["on"] = True
    assert lab.sender.blocked(T0 + 1) is None
    assert lab.sweep(T0 + 1) == 1


def test_switching_sending_to_plex_off_sends_and_records_nothing(lab):
    ep = sure_intro(lab)
    lab.publisher.writes_enabled = False
    before = lab.rows()

    assert lab.sender.blocked(T0) == "Sending to Plex is switched off, so nothing is sent."
    assert lab.sweep(T0) == 0

    assert lab.rows() == before
    assert lab.attempts(ep) == [] and lab.publisher.list_changes(ep.vid) == [] and lab.backups.calls == []
    lab.publisher.writes_enabled = True
    assert lab.sweep(T0 + 1) == 1
    assert _marker_times(lab.plex, "intro") == [(10_000, 20_000)]


# ── the backup ────────────────────────────────────────────────────────────────────────────────────────────────────


def test_plex_is_backed_up_before_the_first_automatic_write(lab):
    sure_intro(lab)
    before = lab.rows()

    assert lab.sweep(T0) == 1

    [call] = lab.backups.calls
    assert os.path.samefile(call["database"], lab.plex)
    assert call["directory"] == plexbackup.backup_dir(str(lab.dir))
    assert call["now"] == T0
    assert call["rows"] == before  # nothing in Plex had changed yet when the backup ran
    assert lab.rows() != before


def test_the_real_backup_makes_one_file_a_day_taken_before_the_writes(lab):
    first, second = sure_intro(lab, 1), sure_intro(lab, 2)
    markers_before = lab.rows()["taggings"]

    assert lab.sweep(T0, lab.new_sender(backup=None)) == 2

    folder = plexbackup.backup_dir(str(lab.dir))
    [name] = os.listdir(folder)
    assert name == "com.plexapp.plugins.library.db.20231114T120000Z.bak"
    with _plex(os.path.join(folder, name)) as backup:
        assert backup.execute("SELECT * FROM taggings ORDER BY id").fetchall() == markers_before
    assert lab.rows()["taggings"] != markers_before
    assert _marker_times(lab.plex, "intro", first.number) == [(10_000, 20_000)]
    assert _marker_times(lab.plex, "intro", second.number) == [(10_000, 20_000)]


def test_a_failed_backup_blocks_every_write_alerts_once_and_recovers_after_the_retry_time(lab):
    ep = sure_intro(lab)
    lab.backups.error = plexbackup.BackupFailed("The backup folder could not be written.")
    before = lab.rows()

    assert lab.sweep(T0) == 0

    assert lab.rows() == before
    assert lab.publisher.list_changes(ep.vid) == []
    assert lab.attempts(ep) == []
    assert lab.concerns.is_open(autosend.BACKUP_KEY)
    assert lab.post.titles() == ["Plex backup failed"]
    assert lab.post.messages() == [
        "Reprise could not back up Plex's database, so it sent nothing to Plex. "
        "The backup folder could not be written. It will try again soon."
    ]
    assert lab.sender.blocked(T0 + 60) == BACKUP_WAIT

    assert lab.sweep(T0 + 60) == 0  # within BACKUP_RETRY_S: no new try, nothing more said
    assert len(lab.backups.calls) == 1 and len(lab.post.calls) == 1

    lab.backups.error = None
    assert lab.sweep(T0 + autosend.BACKUP_RETRY_S) == 1

    assert len(lab.backups.calls) == 2
    assert lab.post.titles() == ["Plex backup failed", "Resolved: Plex backup failed"]
    assert lab.post.messages()[1] == "Plex's database was backed up again. Automatic sending works again."
    assert not lab.concerns.is_open(autosend.BACKUP_KEY)
    assert _marker_times(lab.plex, "intro") == [(10_000, 20_000)]


def test_an_unexpected_backup_error_gives_a_plain_message_without_its_raw_text(lab):
    ep = sure_intro(lab)
    lab.backups.error = RuntimeError("disk exploded at /secret/place")
    before = lab.rows()

    assert lab.sweep(T0) == 0

    assert lab.rows() == before
    assert lab.attempts(ep) == []
    assert lab.post.messages() == [
        "Reprise could not back up Plex's database, so it sent nothing to Plex. "
        "Plex's database could not be backed up. It will try again soon."
    ]


# ── Plex refuses ──────────────────────────────────────────────────────────────────────────────────────────────────


def _refusing(lab, monkeypatch, times=10):
    """Plex refuses every write: ``apply`` raises before it touches anything."""
    spy = Flaky(lab.publisher.apply, *(ValueError("Plex said no") for _ in range(times)))
    monkeypatch.setattr(lab.publisher, "apply", spy)
    return spy


def test_a_refused_write_changes_nothing_pauses_sending_and_alerts_once(lab, monkeypatch):
    ep = sure_intro(lab)
    before = lab.rows()
    spy = _refusing(lab, monkeypatch)

    assert lab.sweep(T0) == 0

    assert spy.calls == 1
    assert lab.rows() == before
    assert [c["state"] for c in lab.publisher.list_changes(ep.vid)] == ["not_applied"]
    assert lab.ranges(ep)[INTRO].review_status == "proposed"  # the approval was taken back
    assert [(a["revision"], a["outcome"], a["tries"], a["detail"]) for a in lab.attempts(ep)] == [
        (1, "failed", 1, "Plex refused it: Plex said no."),
        (3, "failed", 1, "Plex refused it: Plex said no."),
    ]
    assert lab.post.titles() == [REFUSED_TITLE]
    assert lab.post.messages() == [
        "Plex refused a change Reprise tried to send automatically for Community S01E01. Nothing in Plex was "
        "changed. Reprise paused automatic sending for 30 minutes."
    ]
    assert lab.concerns.is_open(autosend.REFUSED_KEY)
    assert lab.sender.blocked(T0 + 1) == REFUSED_PAUSE
    assert lab.state.get("send_refusals") == 1


def test_a_refused_episode_waits_six_hours_is_given_up_after_three_failures_and_the_pause_grows(lab, monkeypatch):
    ep = sure_intro(lab)
    spy = _refusing(lab, monkeypatch)
    lab.sweep(T0)

    # The pause is over but the retry is not due: the failure is kept on the episode's new revision too.
    assert lab.sweep(T0 + autosend.REFUSED_PAUSE_S + 1) == 0
    assert spy.calls == 1

    second = T0 + autosend.FAILED_RETRY_S
    assert lab.sweep(second) == 0
    assert spy.calls == 2
    assert lab.state.get("send_pause") == {"until": second + autosend.REFUSED_PAUSE_S, "why": REFUSED_PAUSE}
    assert lab.attempts(ep)[-1]["outcome"] == "failed" and lab.attempts(ep)[-1]["tries"] == 2

    third = second + autosend.FAILED_RETRY_S
    assert lab.sweep(third) == 0
    assert spy.calls == autosend.FAILED_TRIES
    assert lab.state.get("send_refusals") == autosend.REPEATED_REFUSALS
    assert lab.state.get("send_pause") == {"until": third + autosend.REPEATED_REFUSAL_PAUSE_S, "why": REFUSED_PAUSE}
    last = lab.attempts(ep)[-1]
    assert (last["outcome"], last["tries"]) == ("declined", autosend.FAILED_TRIES)
    assert len(lab.post.calls) == 1  # still the one alert

    assert lab.sweep(third + autosend.REPEATED_REFUSAL_PAUSE_S + autosend.FAILED_RETRY_S) == 0
    assert spy.calls == autosend.FAILED_TRIES  # declined for good: never tried again


def test_a_send_plex_accepts_after_a_refusal_posts_the_resolved_follow_up(lab, monkeypatch):
    ep = sure_intro(lab)
    spy = Flaky(lab.publisher.apply, ValueError("Plex said no"))
    monkeypatch.setattr(lab.publisher, "apply", spy)
    assert lab.sweep(T0) == 0
    assert lab.concerns.is_open(autosend.REFUSED_KEY)

    assert lab.sweep(T0 + autosend.FAILED_RETRY_S) == 1

    assert spy.calls == 2
    assert not lab.concerns.is_open(autosend.REFUSED_KEY)
    assert lab.state.get("send_refusals") == 0
    assert lab.post.titles() == [REFUSED_TITLE, f"Resolved: {REFUSED_TITLE}"]
    assert lab.post.messages()[1] == "Plex accepted an automatic change again. Nothing more to do."
    assert _marker_times(lab.plex, "intro") == [(10_000, 20_000)]
    assert [(a["revision"], a["outcome"]) for a in lab.attempts(ep)] == [(1, "failed"), (3, "sent"), (4, "sent")]


# ── the write fails after it started ──────────────────────────────────────────────────────────────────────────────


def test_a_write_that_reached_plex_before_the_failure_counts_as_sent(lab, monkeypatch):
    ep = sure_intro(lab)
    real = lab.publisher._set_state

    def lose_the_acknowledgement(change_id, state, actor):
        if state == "applied":
            raise RuntimeError("fixture crash after Plex commit")
        return real(change_id, state, actor)

    monkeypatch.setattr(lab.publisher, "_set_state", lose_the_acknowledgement)

    assert lab.sweep(T0) == 1

    assert _marker_times(lab.plex, "intro") == [(10_000, 20_000)]
    assert lab.ranges(ep)[INTRO].review_status == "approved"  # not taken back: Plex has it
    assert [c["state"] for c in lab.publisher.list_changes(ep.vid)] == ["applied"]
    assert [a["outcome"] for a in lab.attempts(ep)] == ["sent", "sent"]
    assert lab.post.calls == []
    assert lab.state.get("send_refusals", 0) == 0


def test_a_change_nobody_can_classify_is_never_repeated_and_alerts_under_its_own_key(lab, monkeypatch):
    first, second = sure_intro(lab, 1), sure_intro(lab, 2)
    before = lab.rows()
    spy = Flaky(lab.publisher.apply, ValueError("Plex did not answer"))
    monkeypatch.setattr(lab.publisher, "apply", spy)
    monkeypatch.setattr(lab.publisher, "recover", lambda change_id, actor="recovery": {"state": "uncertain"})

    assert lab.sweep(T0) == 0

    assert lab.rows() == before
    [change] = lab.publisher.list_changes(first.vid)
    [attempt] = lab.attempts(first)
    assert (attempt["revision"], attempt["outcome"], attempt["change_id"]) == (1, "uncertain", change["id"])
    assert attempt["detail"] == "Reprise can't tell whether Plex took it."
    assert lab.attempts(second) == []  # sending paused before the next episode
    assert lab.ranges(first)[INTRO].review_status == "approved"  # nothing is undone
    assert lab.state.get("send_pause") == {"until": T0 + autosend.UNCERTAIN_PAUSE_S, "why": UNCERTAIN_PAUSE}
    assert lab.sender.blocked(T0 + 60) == UNCERTAIN_PAUSE
    key = f"{autosend.UNCERTAIN_KEY}:{first.vid}"
    assert lab.concerns.is_open(key) and not lab.concerns.is_open(autosend.REFUSED_KEY)
    assert lab.post.titles() == [UNCERTAIN_TITLE]
    assert lab.post.messages() == [UNCERTAIN_MESSAGE]

    # After the pause the other episode is sent; the first is never sent again, and its alert stays open.
    assert lab.sweep(T0 + autosend.UNCERTAIN_PAUSE_S) == 1

    assert spy.calls == 2  # one write for each episode, none repeated
    assert lab.attempts(first)[-1]["detail"] == NOT_FINISHED
    assert _marker_times(lab.plex, "intro", second.number) == [(10_000, 20_000)]
    assert lab.concerns.is_open(key) and lab.alerts.is_open(key)
    assert lab.post.titles() == [UNCERTAIN_TITLE]


# ── a restart in the middle of a send ─────────────────────────────────────────────────────────────────────────────


def test_after_a_restart_a_send_cut_off_before_plex_is_taken_back_and_recorded_as_failed(lab):
    ep = sure_intro(lab)
    before = lab.rows()
    change = lab.start_a_send(ep, 1)

    assert lab.sweep(T0 + 60, lab.new_sender()) == 0  # the finishing is not counted as a send

    assert lab.rows() == before
    assert [(c["id"], c["state"]) for c in lab.publisher.list_changes(ep.vid)] == [(change["id"], "not_applied")]
    assert lab.ranges(ep)[INTRO].review_status == "proposed"
    assert [(a["revision"], a["outcome"], a["tries"], a["detail"]) for a in lab.attempts(ep)] == [
        (1, "failed", 1, "It was cut off before it reached Plex."),
        (3, "failed", 1, "It was cut off before it reached Plex."),
    ]


def test_after_a_restart_a_send_that_reached_plex_is_recorded_as_sent(lab):
    ep = sure_intro(lab)
    change = lab.start_a_send(ep, 1)
    lab.publisher.apply(change["id"], AUTO_ACTOR)
    written = lab.rows()

    assert lab.sweep(T0 + 60, lab.new_sender()) == 0

    assert lab.rows() == written
    assert lab.ranges(ep)[INTRO].review_status == "approved"
    assert [(a["revision"], a["outcome"], a["change_id"]) for a in lab.attempts(ep)] == [
        (1, "sent", change["id"]),
        (2, "sent", change["id"]),
    ]
    assert lab.publisher.automatically_published([ep.vid]) == {ep.vid}


def test_after_a_restart_a_write_that_matches_neither_before_nor_after_is_uncertain(lab):
    ep = sure_intro(lab)
    change = lab.start_a_send(ep, 1)
    lab.publisher._set_state(change["id"], "applying", AUTO_ACTOR)
    with _plex(lab.plex) as db:
        db.execute("UPDATE metadata_items SET title='Renamed in Plex' WHERE id=1")
    changed = lab.rows()

    assert lab.sweep(T0 + 60, lab.new_sender()) == 0

    assert lab.rows() == changed  # Plex is only read
    [attempt] = lab.attempts(ep)
    assert (attempt["revision"], attempt["outcome"], attempt["change_id"]) == (1, "uncertain", change["id"])
    assert lab.ranges(ep)[INTRO].review_status == "approved"
    assert lab.state.get("send_pause") == {"until": T0 + 60 + autosend.UNCERTAIN_PAUSE_S, "why": UNCERTAIN_PAUSE}
    assert lab.post.titles() == [UNCERTAIN_TITLE]
    assert lab.concerns.is_open(f"{autosend.UNCERTAIN_KEY}:{ep.vid}")


# ── how many go in one pass ───────────────────────────────────────────────────────────────────────────────────────


def test_one_pass_sends_at_most_three_episodes_and_the_next_pass_sends_the_rest(lab):
    count = autosend.SENDS_PER_PASS + 1
    for number in range(1, count + 1):
        sure_intro(lab, number)

    assert lab.sweep(T0) == autosend.SENDS_PER_PASS
    assert lab.applied() == {"n": autosend.SENDS_PER_PASS, "episodes": autosend.SENDS_PER_PASS}

    assert lab.sweep(T0 + 60) == count - autosend.SENDS_PER_PASS
    assert lab.applied() == {"n": count, "episodes": count}

    assert lab.sweep(T0 + 120) == 0


# ── the dry run, with the real store and Plex ─────────────────────────────────────────────────────────────────────


def test_credits_starting_in_the_first_two_seconds_are_not_sent(lab):
    ep = lab.episode(1)
    lab.analyse(ep, confirmed(ep, "credits", 1_000, 50_000))
    before = lab.rows()

    assert lab.sweep(T0) == 0

    assert_declined(
        lab, ep, before, "The credits can't be sent exactly: Plex cannot represent this credits start exactly."
    )


def test_credits_ending_within_two_seconds_of_the_file_end_are_not_sent(lab):
    ep = lab.episode(1)
    lab.analyse(ep, confirmed(ep, "credits", 80_000, 99_000))
    before = lab.rows()

    assert lab.sweep(T0) == 0

    assert_declined(
        lab,
        ep,
        before,
        "The credits can't be sent exactly: The final two seconds cannot be projected safely to Plex.",
    )


def test_a_valid_intro_is_still_sent_when_the_credits_beside_it_cannot_be(lab):
    ep = lab.episode(1)
    lab.analyse(ep, confirmed(ep, "intro", 10_000, 20_000), confirmed(ep, "credits", 80_000, 99_000))

    assert lab.sweep(T0) == 1

    assert _marker_times(lab.plex, "intro") == [(10_000, 20_000)]
    assert _marker_rows(lab.plex, "credits") == []
    ranges = lab.ranges(ep)
    assert ranges[INTRO].review_status == "approved"
    assert ranges["credits-80000"].review_status == "proposed"


def test_a_run_whose_sound_was_never_compared_is_not_sent(lab):
    ep = lab.episode(1)
    lab.analyse(ep, confirmed(ep, "intro", 10_000, 20_000, checked=[]))
    before = lab.rows()

    assert lab.sweep(T0) == 0

    assert_declined(lab, ep, before, f"The intro can't be sent exactly: {NOT_ELIGIBLE}.")


def test_a_scene_the_analysis_itself_keeps_inside_the_run_stops_it_being_sent(lab):
    ep = lab.episode(1)
    lab.analyse(ep, confirmed(ep, "intro", 10_000, 20_000), detector_keep(ep, "intro", 12_000, 14_000))
    before = lab.rows()

    assert lab.sweep(T0) == 0

    assert_declined(lab, ep, before, f"The intro can't be sent exactly: {NOT_ELIGIBLE}.")


def test_audio_tracks_that_disagree_decline_the_kind(lab, monkeypatch):
    ep = sure_intro(lab)

    def disagree(result, bindings, runs, kinds):
        raise ValueError("the audio tracks do not agree on where it is")

    monkeypatch.setattr(lab.sender, "_shape", disagree)
    before = lab.rows()

    assert lab.sweep(T0) == 0

    assert_declined(lab, ep, before, "The intro can't be sent exactly: the audio tracks do not agree on where it is.")


def test_when_both_kinds_together_do_not_project_exactly_only_the_first_is_sent(lab, monkeypatch):
    ep = lab.episode(1)
    lab.analyse(ep, confirmed(ep, "intro", 10_000, 20_000), confirmed(ep, "credits", 80_000, 95_000))
    real = AutoSender._shape

    def together_fails(result, bindings, runs, kinds):
        if len(kinds) > 1:
            raise ValueError("it would not be sent exactly as it was found")
        return real(result, bindings, runs, kinds)

    monkeypatch.setattr(lab.sender, "_shape", together_fails)

    assert lab.sweep(T0) == 1

    assert _marker_times(lab.plex, "intro") == [(10_000, 20_000)]
    assert _marker_rows(lab.plex, "credits") == []
    assert lab.ranges(ep)[CREDITS].review_status == "proposed"


# ── approving and taking back, in the store ───────────────────────────────────────────────────────────────────────


def _analysed(lab):
    ep = lab.episode(1)
    lab.analyse(ep, confirmed(ep, "intro", 10_000, 20_000), suggestion(ep, "credits", 80_000, 95_000))
    return ep


def test_auto_approve_approves_a_confirmed_range_and_records_no_owner_decision(lab):
    ep = _analysed(lab)

    result = lab.store.auto_approve(ep.vid, 1, [INTRO])

    assert result.result_revision == 2
    assert lab.ranges(ep)[INTRO].review_status == "approved"
    assert lab.store.owner_decided_ids(ep.vid) == frozenset()
    assert lab.query("SELECT COUNT(*) AS n FROM reprise_manual_overrides WHERE version_id=?", ep.vid) == [{"n": 0}]


def _suggested(lab, ep):
    return "plex-credits"


def _locked(lab, ep):
    lab.owner_review(ep, [{"range_id": INTRO, "locked": True}])
    return INTRO


def _noted(lab, ep):
    lab.owner_review(ep, [{"range_id": INTRO, "reason": "checked by hand"}])
    return INTRO


def _approved(lab, ep):
    lab.store.auto_approve(ep.vid, 1, [INTRO])
    return INTRO


def _unknown(lab, ep):
    return "no-such-range"


@pytest.mark.parametrize(
    "prepare", [_suggested, _locked, _noted, _approved, _unknown], ids=lambda f: f.__name__.strip("_")
)
def test_auto_approve_refuses_what_is_not_a_proposed_untouched_detector_skip(lab, prepare):
    ep = _analysed(lab)
    rid = prepare(lab, ep)
    revision, ranges = lab.revision(ep), lab.ranges(ep)

    with pytest.raises(RevisionConflict):
        lab.store.auto_approve(ep.vid, revision, [rid])

    assert lab.revision(ep) == revision and lab.ranges(ep) == ranges


def test_auto_approve_refuses_a_result_that_moved_on(lab):
    ep = _analysed(lab)
    with pytest.raises(RevisionConflict):
        lab.store.auto_approve(ep.vid, 7, [INTRO])
    assert lab.revision(ep) == 1


def test_auto_approve_refuses_an_empty_list(lab):
    ep = _analysed(lab)
    with pytest.raises(ValueError, match="no range to approve"):
        lab.store.auto_approve(ep.vid, 1, [])
    assert lab.revision(ep) == 1


def test_auto_unapprove_takes_back_only_the_ranges_it_is_given(lab):
    ep = lab.episode(1)
    lab.analyse(ep, confirmed(ep, "intro", 10_000, 20_000), confirmed(ep, "credits", 80_000, 95_000))
    lab.store.auto_approve(ep.vid, 1, [INTRO, CREDITS])

    assert lab.store.auto_unapprove(ep.vid, 2, [INTRO]) is True

    ranges = lab.ranges(ep)
    assert (ranges[INTRO].review_status, ranges[CREDITS].review_status) == ("proposed", "approved")
    assert lab.revision(ep) == 3
    assert lab.store.auto_unapprove(ep.vid, 3, [INTRO]) is False  # nothing left to take back: no new revision
    assert lab.revision(ep) == 3


def test_auto_unapprove_leaves_an_approval_the_owner_made_alone(lab):
    ep = lab.episode(1)
    lab.analyse(ep, confirmed(ep, "intro", 10_000, 20_000), confirmed(ep, "credits", 80_000, 95_000))
    lab.store.auto_approve(ep.vid, 1, [INTRO, CREDITS])
    lab.owner_review(ep, [{"range_id": CREDITS, "review_status": "approved", "locked": True}])
    revision = lab.revision(ep)

    assert lab.store.auto_unapprove(ep.vid, revision, [CREDITS]) is False

    assert lab.ranges(ep)[CREDITS].review_status == "approved" and lab.ranges(ep)[CREDITS].locked
    assert lab.revision(ep) == revision


def test_auto_unapprove_refuses_when_the_result_moved_on(lab):
    ep = _analysed(lab)
    lab.store.auto_approve(ep.vid, 1, [INTRO])
    lab.owner_review(ep, [{"kind": "intro", "action": "keep", "start_ms": 30_000, "end_ms": 31_000}])

    with pytest.raises(RevisionConflict):
        lab.store.auto_unapprove(ep.vid, 2, [INTRO])


# ── the journal, in the publisher ─────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("state", "hold", "unfinished"),
    [
        ("prepared", NOT_FINISHED, 0),
        ("applying", NOT_FINISHED, 1),
        ("undoing", NOT_FINISHED, 1),
        ("uncertain", NOT_FINISHED, 0),
        ("not_applied", None, 0),
    ],
)
def test_an_earlier_change_holds_the_episode_back_unless_it_never_reached_plex(lab, state, hold, unfinished):
    ep = sure_intro(lab)
    assert lab.publisher.automatic_hold(ep.vid) is None
    assert lab.publisher.unfinished_writes() == 0
    change = lab.prepare_as_owner(ep)

    lab.publisher._set_state(change["id"], state, "owner")

    assert lab.publisher.automatic_hold(ep.vid) == hold
    assert lab.publisher.unfinished_writes() == unfinished


def test_the_hold_says_who_sent_it_and_notices_an_undo(lab):
    ep = sure_intro(lab)
    change = lab.prepare_as_owner(ep)

    lab.publisher.apply(change["id"], "owner")
    assert lab.publisher.automatic_hold(ep.vid) == "It was already sent to Plex by you."

    lab.publisher.undo(change["id"], "owner")
    assert lab.publisher.automatic_hold(ep.vid) == UNDONE


def test_abandon_closes_a_prepared_change_and_refuses_any_other(lab):
    ep = sure_intro(lab)
    before = lab.rows()
    change = lab.prepare_as_owner(ep)

    lab.publisher.abandon(change["id"], "owner")

    assert [c["state"] for c in lab.publisher.list_changes(ep.vid)] == ["not_applied"]
    assert lab.rows() == before
    with pytest.raises(ValueError, match="required state"):
        lab.publisher.abandon(change["id"], "owner")


# ── secrets ───────────────────────────────────────────────────────────────────────────────────────────────────────


def _backup_folder_is_a_file(lab, monkeypatch):
    (lab.dir / plexbackup.BACKUP_DIR_NAME).write_text("a file where the folder should be")
    return lab.new_sender(backup=None)


def _plex_refuses(lab, monkeypatch):
    monkeypatch.setattr(lab.publisher, "apply", Flaky(lab.publisher.apply, ValueError("Plex said no")))
    return lab.sender


def _plex_is_uncertain(lab, monkeypatch):
    monkeypatch.setattr(lab.publisher, "apply", Flaky(lab.publisher.apply, ValueError("Plex did not answer")))
    monkeypatch.setattr(lab.publisher, "recover", lambda change_id, actor="recovery": {"state": "uncertain"})
    return lab.sender


@pytest.mark.parametrize(
    "scenario", [_backup_folder_is_a_file, _plex_refuses, _plex_is_uncertain], ids=lambda f: f.__name__.strip("_")
)
def test_alerts_and_logs_never_contain_the_token_or_a_path(lab, logs, monkeypatch, scenario):
    sure_intro(lab)
    sender = scenario(lab, monkeypatch)

    lab.sweep(T0, sender)

    assert len(lab.post.calls) == 1
    for call in lab.post.calls:
        text = call["headers"]["Title"] + "\n" + call["data"].decode()
        assert TOKEN not in text
        assert str(lab.dir) not in text
    assert not any(TOKEN in line for line in logs)
