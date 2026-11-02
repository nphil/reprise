"""PlexMarkerPublisher against a real SQLite file with Plex 1.43's schema."""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.markers.decide import FileLimits
from media_preview_generator.markers.models import Marker, MarkerType
from media_preview_generator.markers.publishers import plex_db
from media_preview_generator.markers.publishers.base import (
    Capability,
    DatabaseBusyError,
    MarkerPublisher,
    PublishError,
    Shown,
    cancellable_waits,
)
from media_preview_generator.markers.publishers.plex_db import (
    LocalPlexDb,
    PlexMarkerPublisher,
    encode_extra_data,
    merge_part_extra_data,
    plex_db_path,
)
from media_preview_generator.markers.settings import ServerMarkersSettings
from media_preview_generator.nodes.server import winplatform
from media_preview_generator.servers.base import ServerConfig, ServerType

T = MarkerType
FIX = Path(__file__).resolve().parents[1] / "fixtures" / "markers"
DUR = 1_320_000
INTRO = Marker(T.INTRO, 11_000, 37_000, ("chapters",))
CREDITS_FINAL = Marker(T.CREDITS, 1_299_000, DUR, ("chapters",))
CREDITS_NONFINAL = Marker(T.CREDITS, 1_200_000, 1_250_000, ("theintrodb", "skipdb"))
NATIVE_INTROS = (
    '{"MediaPartMarkersArray":{"attributeName":"intros","version":5,'
    '"MediaPartMarker":[{"startTimeOffset":990,"endTimeOffset":29306}]}}'
)
NATIVE_CREDITS = (
    '{"MediaPartMarkersArray":{"attributeName":"credits","version":4,'
    '"MediaPartMarker":[{"startTimeOffset":1154521,"endTimeOffset":1188521}]}}'
)


def _plex_json(d: dict) -> str:
    # Plex stores compact JSON; the capability sample's LIKE '%"pv:intros":"{%' only matches that form.
    return json.dumps(d, separators=(",", ":"))


BOTH_NATIVE = _plex_json({"pv:credits": NATIVE_CREDITS, "pv:intros": NATIVE_INTROS, "url": "z"})
INTROS_V6_VALUE = NATIVE_INTROS.replace('"version":5', '"version":6')
INTROS_V6 = _plex_json({"pv:intros": INTROS_V6_VALUE})
# What we store for INTRO / CREDITS_FINAL (credits start = served - 2000 ms).
INTRO_PAYLOAD = (
    '{"MediaPartMarkersArray":{"attributeName":"intros","version":5,'
    '"MediaPartMarker":[{"startTimeOffset":11000,"endTimeOffset":37000}]}}'
)
CREDITS_FINAL_PAYLOAD = (
    '{"MediaPartMarkersArray":{"attributeName":"credits","version":4,'
    '"MediaPartMarker":[{"startTimeOffset":1297000,"endTimeOffset":1320000,"final":true}]}}'
)
INTRO_ROW_EXTRA = '{"pv:version":"5","url":"pv%3Aversion=5"}'
CREDITS_ROW_EXTRA = '{"pv:version":"4","url":"pv%3Aversion=4"}'
CREDITS_FINAL_ROW_EXTRA = '{"pv:final":"1","pv:version":"4","url":"pv%3Afinal=1&pv%3Aversion=4"}'
_JOURNAL_MODE = "delete"
PLEX_VERSION = "1.43.4.10903-e5521bd8c"
ON_WINDOWS = sys.platform == "win32"
# What a test's database folder reports as its filesystem: the fake mount table's root, or the Windows volume's own.
LOCAL_FS = "ntfs" if ON_WINDOWS else "ext4"
# These tests write a Linux mount table (/proc/self/mountinfo) to stand for the disk the database is on. Windows has
# none: there the drive type decides, and TestWindowsDrives refuses the same setups through it.
linux_mount_table = pytest.mark.skipif(
    ON_WINDOWS, reason="simulates a Linux mount table; Windows decides by drive type (TestWindowsDrives)"
)
windows_only = pytest.mark.skipif(not ON_WINDOWS, reason="Windows drive types and locks")


@pytest.fixture(autouse=True, params=["delete", "wal"])
def journal_mode(request, monkeypatch):
    """Every DB test runs on both journal modes: SQLite's default and WAL, which Plex uses."""
    monkeypatch.setitem(globals(), "_JOURNAL_MODE", request.param)
    return request.param


@pytest.fixture(autouse=True)
def short_wait_slice(monkeypatch):
    """Lock waits poll every 50 ms, not 1 s: a timeout or cancel is noticed that much sooner, so tests can wait less."""
    monkeypatch.setattr(plex_db, "WAIT_SLICE_S", 0.05)


# TestLockDomain overrides plex_holds_the_database and uses the real probe.
pytestmark = pytest.mark.usefixtures("plex_holds_the_database")


def _writes(log: list[str]) -> list[str]:
    return [sql for sql in log if sql.split(None, 1)[0].upper() in {"INSERT", "UPDATE", "DELETE", "REPLACE"}]


@contextlib.contextmanager
def _committed_once_the_publisher_asks_for_the_lock(locker: sqlite3.Connection):
    """Commit ``locker``'s open write (another program holding Plex's write lock) as soon as the publisher asks for
    that lock (``LocalPlexDb._begin_write``): after the publisher's read before the lock, and before it holds the lock,
    however slow the machine. A timer let a slow start read after the commit, so the lock was never waited for."""
    asked = threading.Event()
    begin_write = LocalPlexDb._begin_write

    def asking(conn, deadline):
        asked.set()
        begin_write(conn, deadline)

    def commit():
        asked.wait()
        locker.execute("COMMIT")
        locker.close()

    committer = threading.Thread(target=commit, daemon=True)
    with patch.object(LocalPlexDb, "_begin_write", staticmethod(asking)):
        committer.start()
        try:
            yield
        finally:
            asked.set()  # a publisher that never asked: the other program's write still lands
            committer.join(timeout=30)


def _make_db(
    folder: Path,
    *,
    tag_row: str | None = "''",
    parts=(("/data/tv/S01E01.mkv", None),),
    metadata_item_id=7,
    journal_mode: str | None = None,
):
    db = Path(plex_db_path(str(folder)))
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db)
    conn.execute(f"PRAGMA journal_mode={journal_mode or _JOURNAL_MODE}")
    # The schema is 48 CREATE TABLE/INDEX statements; each fsyncs its own autocommit
    # transaction by default. synchronous=OFF plus one BEGIN...COMMIT around the load
    # collapses that to a single fsync. Test DBs are throwaway, so durability doesn't
    # matter here -- this only touches how the fixture loads the schema, not the
    # journal mode or sync setting the publisher tests under it are exercising.
    conn.execute("PRAGMA synchronous=OFF")
    conn.executescript("BEGIN;\n" + (FIX / "plex_schema_1_43.sql").read_text() + "\nCOMMIT;")
    conn.execute("INSERT INTO metadata_items (id, metadata_type, title) VALUES (?, 4, 'Ep')", (metadata_item_id,))
    if tag_row is not None:
        conn.execute("INSERT INTO tags (id, tag, tag_type) VALUES (562, NULL, 12)")
        if tag_row == "''":
            conn.execute("INSERT INTO tags (id, tag, tag_type) VALUES (563, '', 12)")
    for n, (file, extra) in enumerate(parts, start=1):
        conn.execute("INSERT INTO media_items (id, metadata_item_id) VALUES (?, ?)", (n, metadata_item_id))
        conn.execute(
            "INSERT INTO media_parts (id, media_item_id, file, extra_data) VALUES (?, ?, ?, ?)", (n, n, file, extra)
        )
    conn.commit()
    conn.close()
    return db


def _mountinfo(tmp_path, fs="ext4", extra_lines=""):
    p = tmp_path / "mountinfo"
    p.write_text(f"22 1 259:2 / / rw - {fs} /dev/root rw\n{extra_lines}")
    return str(p)


def _publisher(
    tmp_path,
    folder,
    *,
    enabled=True,
    confirmed="2026-09-13T00:00:00+00:00",
    fs="ext4",
    plex_pass=True,
    sibling_markers=None,
    mappings=None,
    libraries=None,
    mountinfo_lines="",
    mountinfo_path=None,
    settings_provider=None,
    redetect="restore",
):
    server = MagicMock()
    server.get_server_status.return_value = (
        None if plex_pass is None else {"plex_pass": plex_pass, "version": PLEX_VERSION}
    )
    server.get_marker_detection_prefs.return_value = {"intro": "never", "credits": "never"}
    cfg = ServerConfig(
        id="plex-1",
        type=ServerType.PLEX,
        name="Plex",
        enabled=True,
        url="http://p",
        auth={},
        output={"plex_config_folder": str(folder)},
        path_mappings=mappings or [],
        libraries=list(libraries or []),
    )
    settings = ServerMarkersSettings(enabled, None, confirmed, redetect)
    return PlexMarkerPublisher(
        server,
        cfg,
        settings,
        sibling_markers=sibling_markers,
        settings_provider=settings_provider,
        mountinfo_path=mountinfo_path or _mountinfo(tmp_path, fs, mountinfo_lines),
    )


def _rows(db, sql, *params):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _served(db, item=7):
    """What Plex serves for the item: its intro/credits rows on the marker tag, in served times (start order)."""
    rows = _rows(
        db,
        "SELECT text, time_offset, end_time_offset, t.extra_data FROM taggings t JOIN tags g ON g.id = t.tag_id "
        "WHERE t.metadata_item_id=? AND g.tag_type=12 AND g.tag='' AND t.text IN ('intro','credits') "
        "ORDER BY t.time_offset",
        item,
    )
    out = []
    for text, start, end, extra in rows:
        mtype = MarkerType.INTRO if text == "intro" else MarkerType.CREDITS
        out.append((mtype, *plex_db._served_times(mtype, int(start), int(end), plex_db._row_is_final(extra))))
    return out


class TestExtraDataEncoding:
    def test_rebuilding_native_extra_data_is_byte_identical(self):
        native = (FIX / "plex_part_extra_data_native.json").read_text().strip()
        d = json.loads(native)
        d.pop("url")
        assert encode_extra_data(d) == native

    def test_merge_writes_both_keys_and_rebuilds_url(self):
        native = (FIX / "plex_part_extra_data_native.json").read_text().strip()
        out = json.loads(merge_part_extra_data(native, [INTRO, CREDITS_FINAL], {T.INTRO, T.CREDITS}, DUR))
        assert out["pv:intros"] == (
            '{"MediaPartMarkersArray":{"attributeName":"intros","version":5,'
            '"MediaPartMarker":[{"startTimeOffset":11000,"endTimeOffset":37000}]}}'
        )
        assert out["pv:credits"] == (
            '{"MediaPartMarkersArray":{"attributeName":"credits","version":4,'
            '"MediaPartMarker":[{"startTimeOffset":1297000,"endTimeOffset":1320000,"final":true}]}}'
        )
        assert out["ma:container"] == "mkv" and out["pv:deepAnalysisDate"] == "1651985592"
        assert list(out) == sorted(k for k in out if k != "url") + ["url"]
        rebuilt = dict(out)
        rebuilt.pop("url")
        assert json.loads(encode_extra_data(rebuilt))["url"] == out["url"]
        assert "pv%3Aintros=%7B%22MediaPartMarkersArray" in out["url"]

    @pytest.mark.parametrize(
        ("existing", "wanted", "managed", "previous", "intros", "credits"),
        [
            (None, [INTRO], {T.INTRO}, [], "set", "absent"),
            ("", [CREDITS_FINAL], {T.CREDITS}, [], "absent", "set"),
            (BOTH_NATIVE, [INTRO], {T.INTRO}, [], "set", "keep"),
            # We removed our intro: the key goes, so Plex's own detection may analyse the part again.
            (_plex_json({"pv:intros": INTRO_PAYLOAD, "url": "z"}), [], {T.INTRO}, [INTRO], "absent", "absent"),
            # Plex re-detected since we published: its intro stays.
            (BOTH_NATIVE, [], {T.INTRO}, [INTRO], "native", "keep"),
            (_plex_json({"pv:intros": "", "url": "z"}), [INTRO], {T.INTRO}, [], "set", "absent"),  # cleared earlier
            (BOTH_NATIVE, [INTRO, CREDITS_NONFINAL], {T.INTRO, T.CREDITS}, [], "set", "set"),
        ],
        ids=["add-intro", "add-credits", "keep-other-type", "remove-ours", "keep-redetected", "over-empty", "both"],
    )
    def test_merge_matrix(self, existing, wanted, managed, previous, intros, credits):
        out = json.loads(merge_part_extra_data(existing, wanted, managed, DUR, previous=previous))
        expect = {
            "set": lambda v: v.startswith('{"MediaPartMarkersArray"') and v not in (NATIVE_INTROS, NATIVE_CREDITS),
            "absent": lambda v: v is None,
            "keep": lambda v: v == NATIVE_CREDITS,
            "native": lambda v: v == NATIVE_INTROS,
        }
        assert expect[intros](out.get("pv:intros"))
        assert expect[credits](out.get("pv:credits"))
        assert "url" in out

    def test_non_final_credits_shifts_end_and_omits_final(self):
        out = json.loads(merge_part_extra_data(None, [CREDITS_NONFINAL], {T.CREDITS}, DUR))
        marker = json.loads(out["pv:credits"])["MediaPartMarkersArray"]["MediaPartMarker"]
        assert marker == [{"startTimeOffset": 1_198_000, "endTimeOffset": 1_252_000}]

    @pytest.mark.parametrize(
        "existing",
        [
            "{not json",
            "[1, 2]",
            INTROS_V6,  # the type we write
            _plex_json({"pv:credits": NATIVE_CREDITS.replace('"version":4', '"version":5')}),  # a type we only keep
            _plex_json({"pv:credits": NATIVE_CREDITS.replace('"credits","version"', '"outros","version"')}),
            _plex_json({"pv:intros": "not json"}),
        ],
        ids=["not-json", "not-object", "intro-v6", "credits-v5-kept", "wrong-attribute", "marker-value-not-json"],
    )
    def test_unknown_existing_data_raises_unsupported_schema(self, existing):
        with pytest.raises(PublishError) as ei:
            merge_part_extra_data(existing, [INTRO], {T.INTRO}, DUR)
        assert ei.value.state is Capability.UNSUPPORTED_SCHEMA


# A real part Plex 1.43.4's one-time CreditsFinalAttributeMigration rewrote in its older URL-encoded form (first
# database optimize): our non-final credits there got "final":1.
URL_FORM_PART = (FIX / "plex_part_extra_data_url_form.txt").read_text().strip()
# The same part as this app wrote it before the migration: the credits entry without "final".
URL_FORM_PART_NOT_FINAL = URL_FORM_PART.replace("%2C%22final%22%3A1%7D%5D", "%7D%5D")
URL_INTRO = Marker(T.INTRO, 127_000, 214_000, ("chapters",))
URL_CREDITS = Marker(T.CREDITS, 1_326_000, 1_415_000, ("chapters",))
URL_DUR = 1_422_004
INTRO_PAYLOAD_URL = (
    "%7B%22MediaPartMarkersArray%22%3A%7B%22attributeName%22%3A%22intros%22%2C%22version%22%3A5%2C"
    "%22MediaPartMarker%22%3A%5B%7B%22startTimeOffset%22%3A11000%2C%22endTimeOffset%22%3A37000%7D%5D%7D%7D"
)
CREDITS_NONFINAL_PAYLOAD = (
    '{"MediaPartMarkersArray":{"attributeName":"credits","version":4,'
    '"MediaPartMarker":[{"startTimeOffset":1198000,"endTimeOffset":1252000}]}}'
)


class TestUrlFormExtraData:
    """Plex's older URL-encoded extra_data (``k=v&...``, sorted keys, no ``url`` field) is read and written back as is."""

    def test_rebuilding_plex_s_url_form_is_byte_identical(self):
        fields, url_form = plex_db.decode_extra_data(URL_FORM_PART)
        assert url_form is True
        assert fields["pv:credits"] == (
            '{"MediaPartMarkersArray":{"attributeName":"credits","version":4,'
            '"MediaPartMarker":[{"startTimeOffset":1324000,"endTimeOffset":1417000,"final":1}]}}'
        )
        assert sorted(fields) == ["ma:container", "ma:videoProfile", "pv:chapters", "pv:credits", "pv:intros"]
        assert encode_extra_data(fields, url_form=True) == URL_FORM_PART

    def test_json_form_keeps_its_url_field_and_is_not_url_form(self):
        native = (FIX / "plex_part_extra_data_native.json").read_text().strip()
        fields, url_form = plex_db.decode_extra_data(native)
        assert url_form is False and fields == json.loads(native)

    def test_our_changed_credits_are_written_back_in_the_part_s_url_form(self):
        # The lab's refused parts: Plex flagged our non-final credits final; "Use ours" writes our flag back.
        out = merge_part_extra_data(
            URL_FORM_PART, [URL_INTRO, URL_CREDITS], {T.INTRO, T.CREDITS}, URL_DUR, previous=[URL_INTRO, URL_CREDITS]
        )
        assert out == URL_FORM_PART_NOT_FINAL

    def test_a_final_1_entry_serving_the_wanted_final_credits_is_left_byte_for_byte(self):
        # Plex writes "final":1 as well as true; either way the entry serves its stored end.
        final_credits = Marker(T.CREDITS, 1_326_000, 1_417_000, ("chapters",))
        out = merge_part_extra_data(URL_FORM_PART, [URL_INTRO, final_credits], {T.INTRO, T.CREDITS}, 1_418_000)
        assert out == URL_FORM_PART

    @pytest.mark.parametrize("final", [True, 1], ids=["true", "1"])
    def test_both_final_spellings_read_as_final(self, final):
        value = json.dumps(
            {
                "MediaPartMarkersArray": {
                    "attributeName": "credits",
                    "version": 4,
                    "MediaPartMarker": [{"startTimeOffset": 1_297_000, "endTimeOffset": DUR, "final": final}],
                }
            },
            separators=(",", ":"),
        )
        assert plex_db._part_entries(T.CREDITS, value) == [(1_299_000, DUR, True)]

    @pytest.mark.parametrize("url_form", [False, True], ids=["json", "url-form"])
    @pytest.mark.parametrize("other_keys", [True, False], ids=["other-keys", "no-other-keys"])
    @pytest.mark.parametrize(
        ("marker", "key", "payload"),
        [
            (INTRO, "pv:intros", INTRO_PAYLOAD),
            (CREDITS_FINAL, "pv:credits", CREDITS_FINAL_PAYLOAD),
            (CREDITS_NONFINAL, "pv:credits", CREDITS_NONFINAL_PAYLOAD),
        ],
        ids=["intro", "credits-final", "credits-not-final"],
    )
    def test_merge_keeps_the_part_s_form_and_its_other_keys(self, url_form, other_keys, marker, key, payload):
        # Plex's own marker of the same type is on the part; without other keys it is the part's only key.
        native = NATIVE_INTROS if key == "pv:intros" else NATIVE_CREDITS
        others = {"ma:container": "mkv", "pv:chapters": '{"Chapters":{"Chapter":[]}}'} if other_keys else {}
        existing = encode_extra_data({**others, key: native}, url_form=url_form)
        out = merge_part_extra_data(existing, [marker], {marker.type}, DUR)
        assert out == encode_extra_data({**others, key: payload}, url_form=url_form)
        assert out.startswith("{") is not url_form

    def test_url_form_output_literal(self):
        out = merge_part_extra_data("ma%3Acontainer=mkv", [INTRO], {T.INTRO}, DUR)
        assert out == f"ma%3Acontainer=mkv&pv%3Aintros={INTRO_PAYLOAD_URL}"

    @pytest.mark.parametrize(
        ("other_keys", "expected"),
        [(True, "ma%3Acontainer=mkv"), (False, "")],
        ids=["other-keys-stay", "nothing-left"],
    )
    def test_removing_ours_from_a_url_form_part(self, other_keys, expected):
        fields = {"pv:intros": INTRO_PAYLOAD, **({"ma:container": "mkv"} if other_keys else {})}
        out = merge_part_extra_data(encode_extra_data(fields, url_form=True), [], {T.INTRO}, DUR, previous=[INTRO])
        assert out == expected

    @pytest.mark.parametrize(
        "existing",
        [
            "ma%3Acontainer",  # no "="
            "pv%3Aintros=x&ma%3Acontainer=mkv",  # keys not sorted
            "ma%3acontainer=mkv",  # lower-case escape
            "ma:container=mkv",  # ":" not escaped
            "ma%3Acontainer=a+b",  # "+" Plex escapes as %2B
            "ma%3Acontainer=%FF",  # not UTF-8
            "ma%3Acontainer=mkv&ma%3Acontainer=avi",  # a key twice
            "ma%3Acontainer=mkv&url=x",  # a url field belongs to the JSON form
            f"pv%3Aintros={urllib.parse.quote(INTROS_V6_VALUE, safe='')}",  # the untested marker version
        ],
        ids=[
            "no-equals",
            "unsorted",
            "lower-case",
            "unescaped",
            "plus",
            "not-utf8",
            "duplicate",
            "url-key",
            "intro-v6",
        ],
    )
    def test_anything_plex_would_not_write_is_still_unsupported(self, existing):
        with pytest.raises(PublishError) as ei:
            merge_part_extra_data(existing, [INTRO], {T.INTRO}, DUR)
        assert ei.value.state is Capability.UNSUPPORTED_SCHEMA

    @pytest.mark.parametrize(
        ("extra", "final"),
        [
            (CREDITS_FINAL_ROW_EXTRA, True),
            (CREDITS_ROW_EXTRA, False),
            ("pv%3Afinal=1&pv%3Aversion=4", True),
            ("pv%3Aversion=4", False),
            (None, False),
            ("not readable", False),
        ],
        ids=["json-final", "json-not-final", "url-final", "url-not-final", "none", "unreadable"],
    )
    def test_taggings_final_flag_in_either_form(self, extra, final):
        assert plex_db._row_is_final(extra) is final


class TestSavedSettingsBeforeEveryWrite:
    """With a ``settings_provider`` the switch and the confirmation are read at each check, not when the job started."""

    def test_without_a_provider_the_settings_given_at_creation_count(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        _make_db(folder)
        assert _publisher(tmp_path, folder, enabled=False).capability().state is Capability.DISABLED
        on = _publisher(
            tmp_path, folder, enabled=False, settings_provider=lambda: ServerMarkersSettings(True, None, "t", "restore")
        )
        assert on.capability().state is Capability.READY


class TestCapability:
    @pytest.mark.parametrize(
        ("kwargs", "db", "state"),
        [
            ({"enabled": False}, True, Capability.DISABLED),
            ({"confirmed": None}, True, Capability.NEEDS_CONFIRMATION),
            ({}, False, Capability.MISCONFIGURED),
            pytest.param({"fs": "nfs4"}, True, Capability.NEEDS_LOCAL_DB, marks=linux_mount_table),
            ({"plex_pass": False}, True, Capability.NEEDS_PASS),
            ({}, "no-tag", Capability.NEEDS_PLEX_DETECTION_ONCE),
            ({}, True, Capability.READY),
            ({"plex_pass": None}, True, Capability.READY),  # Plex unreachable right now: DB checks still pass
        ],
    )
    def test_matrix(self, tmp_path, kwargs, db, state):
        folder = tmp_path / "Plex Media Server"
        if db:
            _make_db(folder, tag_row=None if db == "no-tag" else "''")
        report = _publisher(tmp_path, folder, **kwargs).capability()
        assert report.state is state, report.message
        if state is Capability.READY:
            assert report.details["fs_type"] == LOCAL_FS and report.details["db_path"].endswith(
                "com.plexapp.plugins.library.db"
            )
            # Plex unreachable: prefs aren't fetched (no second connection attempt just for UI details).
            unreachable = kwargs.get("plex_pass", True) is None
            expected = {"intro": None, "credits": None} if unreachable else {"intro": "never", "credits": "never"}
            assert report.details["detection"] == expected

    def test_empty_config_folder_is_misconfigured(self, tmp_path):
        pub = _publisher(tmp_path, "")
        assert pub.capability().state is Capability.MISCONFIGURED

    def test_unknown_marker_version_anywhere_in_the_library_blocks_writes(self, tmp_path):
        # The library-wide sample runs here (cached per job), not on every write.
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        conn = sqlite3.connect(db)
        conn.execute("INSERT INTO metadata_items (id, metadata_type, title) VALUES (8, 4, 'Other')")
        conn.execute("INSERT INTO media_items (id, metadata_item_id) VALUES (50, 8)")
        conn.execute(
            "INSERT INTO media_parts (id, media_item_id, file, extra_data) VALUES (50, 50, '/data/tv/o.mkv', ?)",
            (INTROS_V6,),
        )
        conn.commit()
        conn.close()
        report = _publisher(tmp_path, folder).capability()
        assert report.state is Capability.UNSUPPORTED_SCHEMA and "version 6" in report.message


def _insert_taggings(db, *rows):
    """Insert (metadata_item_id, tag_id, index, text, start, end, extra_data) rows; returns their ids."""
    return [
        _exec(
            db,
            "INSERT INTO taggings (metadata_item_id, tag_id, [index], text, time_offset, end_time_offset, thumb_url, "
            "created_at, extra_data) VALUES (?, ?, ?, ?, ?, ?, '', 1, ?)",
            *row,
        )
        for row in rows
    ]


def _part_markers(db, part_id, key):
    value = json.loads(_rows(db, "SELECT extra_data FROM media_parts WHERE id=?", part_id)[0][0]).get(key)
    return value if not value else json.loads(value)["MediaPartMarkersArray"]["MediaPartMarker"]


def _part_payload_for(marker):
    return json.loads(merge_part_extra_data(None, [marker], {marker.type}, DUR))[plex_db._PART_KEY[marker.type]]


def _set_part_extra(db, part_id, extra):
    _exec(db, "UPDATE media_parts SET extra_data=? WHERE id=?", extra, part_id)


def _exec(db, sql, *params):
    conn = sqlite3.connect(db)
    try:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _write_one(
    pub, markers, *, previous=(), duration_ms=DUR, path="/data/tv/S01E01.mkv", item_id="7", own_previous=None
):
    prior = None if previous is None else list(previous)
    kwargs = {} if own_previous is None else {"own_previous": list(own_previous)}
    return pub.write(item_id, list(markers), previous=prior, duration_ms=duration_ms, canonical_path=path, **kwargs)


class TestCapabilityDetails:
    def _db_with_other_part(self, tmp_path, extra):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        conn = sqlite3.connect(db)
        conn.execute("INSERT INTO media_items (id, metadata_item_id) VALUES (50, 8)")
        conn.execute(
            "INSERT INTO media_parts (id, media_item_id, file, extra_data) VALUES (50, 50, '/o.mkv', ?)", (extra,)
        )
        conn.commit()
        conn.close()
        return folder

    def test_unknown_credits_version_in_the_library_blocks_writes(self, tmp_path):
        folder = self._db_with_other_part(
            tmp_path, _plex_json({"pv:credits": NATIVE_CREDITS.replace('"version":4', '"version":5')})
        )
        report = _publisher(tmp_path, folder).capability()
        assert report.state is Capability.UNSUPPORTED_SCHEMA and "version 5" in report.message

    @pytest.mark.parametrize(
        "extra", [BOTH_NATIVE, _plex_json({"pv:intros": "", "pv:credits": ""}), 'not json but mentions "pv:intros":"{']
    )
    def test_tested_or_cleared_library_data_is_ready(self, tmp_path, extra):
        folder = self._db_with_other_part(tmp_path, extra)
        assert _publisher(tmp_path, folder).capability().state is Capability.READY

    def test_missing_column_is_unsupported_schema(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        conn = sqlite3.connect(db)
        conn.execute("ALTER TABLE media_parts RENAME COLUMN deleted_at TO removed_at")
        conn.commit()
        conn.close()
        report = _publisher(tmp_path, folder).capability()
        assert report.state is Capability.UNSUPPORTED_SCHEMA and "media_parts" in report.message

    def test_unreadable_database_is_misconfigured(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        db = Path(plex_db_path(str(folder)))
        db.parent.mkdir(parents=True)
        db.write_bytes(b"this is not a sqlite database" * 10)
        report = _publisher(tmp_path, folder).capability()
        assert report.state is Capability.MISCONFIGURED and "not a database" in report.message

    def test_ready_details_survive_detection_prefs_failing(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        _make_db(folder)
        pub = _publisher(tmp_path, folder, plex_pass=None)
        pub._server.get_marker_detection_prefs.side_effect = RuntimeError("down")
        report = pub.capability()
        assert report.state is Capability.READY
        assert report.details["detection"] == {"intro": None, "credits": None}
        assert report.details["plex_pass"] is None

    @linux_mount_table
    def test_network_share_report_names_the_filesystem(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        _make_db(folder)
        pub = _publisher(tmp_path, folder, fs="cifs")
        report = pub.capability()
        assert report.state is Capability.NEEDS_LOCAL_DB and "network share (cifs)" in report.message
        assert report.details["fs_type"] == "cifs"
        pub._server.get_server_status.assert_not_called()


_HOLDER = (
    "import sqlite3, sys\n"
    "conn = sqlite3.connect(sys.argv[1])\n"
    "conn.execute('SELECT COUNT(*) FROM taggings').fetchall()\n"
    "print('ready', flush=True)\n"
    "sys.stdin.read()\n"
)


@contextlib.contextmanager
def _another_process_holding(db):
    """A separate process with the DB open, like Plex: it holds SQLite's read lock on byte 128 of <db>-shm."""
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(db)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    )
    try:
        assert proc.stdout.readline().strip() == "ready"
        yield proc
    finally:
        proc.stdin.close()
        proc.wait(timeout=10)


class TestLockDomain:
    """The real lock probe: writes need proof that Plex's SQLite and ours share locks on the same file."""

    @pytest.fixture(autouse=True)
    def plex_holds_the_database(self):
        """Overrides the module fixture: no stand-in, the real probe runs."""

    @pytest.fixture(autouse=True)
    def journal_mode(self):
        """Overrides the module fixture: each test picks its journal mode explicitly."""

    def _db(self, tmp_path, journal="wal"):
        folder = tmp_path / "Plex Media Server"
        return folder, _make_db(folder, journal_mode=journal)

    @pytest.mark.parametrize(
        ("plex_pass", "state"),
        [(True, Capability.NEEDS_LOCAL_DB), (False, Capability.NEEDS_LOCAL_DB), (None, Capability.UNREACHABLE)],
        ids=["plex-reachable", "reachable-without-pass", "plex-unreachable"],
    )
    def test_no_holder_is_not_ready(self, tmp_path, plex_pass, state):
        folder, _db = self._db(tmp_path)
        report = _publisher(tmp_path, folder, plex_pass=plex_pass).capability()
        assert report.state is state, report.message
        assert report.details["lock_holder"] is False

    def test_a_rollback_journal_database_never_counts_as_held(self, tmp_path):
        # Plex runs WAL; without a -shm file there is nothing to prove the lock domain with.
        folder, db = self._db(tmp_path, journal="delete")
        with _another_process_holding(db):
            assert _publisher(tmp_path, folder).capability().state is Capability.NEEDS_LOCAL_DB

    @pytest.mark.skipif(ON_WINDOWS, reason="a POSIX lock rule; Windows locks belong to the handle that took them")
    def test_closing_any_descriptor_on_the_shm_drops_our_connection_s_lock(self, tmp_path):
        # Why every connection and the probe share one per-path lock: this is the kernel's POSIX-lock rule.
        _folder, db = self._db(tmp_path)
        checker = (
            "import fcntl, os, struct, sys\n"
            "fd = os.open(sys.argv[1], os.O_RDONLY)\n"
            "req = struct.pack('hhqqi', fcntl.F_WRLCK, 0, 128, 1, 0)\n"
            "print(struct.unpack('hhqqi', fcntl.fcntl(fd, fcntl.F_GETLK, req))[0] != fcntl.F_UNLCK)\n"
        )

        def held() -> bool:
            out = subprocess.run(
                [sys.executable, "-c", checker, f"{db}-shm"], capture_output=True, text=True, check=True
            )
            return out.stdout.strip() == "True"

        conn = sqlite3.connect(db)
        try:
            conn.execute("SELECT COUNT(*) FROM taggings").fetchall()
            assert held()
            os.close(os.open(f"{db}-shm", os.O_RDONLY))
            assert not held()
        finally:
            conn.close()

    @windows_only
    def test_closing_another_handle_on_the_shm_keeps_our_connection_s_lock(self, tmp_path):
        # Why the Windows probe may open and close the -shm while one of our connections is open elsewhere.
        _folder, db = self._db(tmp_path)
        checker = (
            "import sys\n"
            "from media_preview_generator.nodes.server import winplatform\n"
            "print(winplatform.dms_lock_held(sys.argv[1]))\n"
        )

        def held() -> bool:
            out = subprocess.run(
                [sys.executable, "-c", checker, f"{db}-shm"], capture_output=True, text=True, check=True
            )
            return out.stdout.strip() == "True"

        conn = sqlite3.connect(db)
        try:
            conn.execute("SELECT COUNT(*) FROM taggings").fetchall()
            assert held()
            os.close(os.open(f"{db}-shm", os.O_RDONLY))
            assert held()
        finally:
            conn.close()


class TestRemoval:
    """Removing a type we published removes only what is still ours."""

    def _published(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        pub = _publisher(tmp_path, folder)
        _write_one(pub, [INTRO, CREDITS_FINAL])
        return db, pub

    @pytest.mark.parametrize("duration_now", [DUR + 60_000, DUR - 60_000, None])
    def test_merge_removes_our_key_whatever_the_duration_is_now(self, duration_now):
        existing = _plex_json({"pv:credits": CREDITS_FINAL_PAYLOAD, "ma:container": "mkv"})
        out = json.loads(merge_part_extra_data(existing, [], {T.CREDITS}, duration_now, previous=[CREDITS_FINAL]))
        assert out == {"ma:container": "mkv", "url": "ma%3Acontainer=mkv"}


class TestUrlFormParts:
    """Parts Plex rewrote in its URL-encoded form publish, read back and clean up like JSON ones."""

    MIGRATED_ROWS = (
        (7, 563, 0, "credits", 1_324_000, 1_417_000, CREDITS_FINAL_ROW_EXTRA),
        (7, 563, 1, "intro", 127_000, 214_000, INTRO_ROW_EXTRA),
    )

    def _migrated(self, tmp_path):
        """The lab's refused item: our markers after Plex's CreditsFinalAttributeMigration flagged the credits final."""
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder, parts=(("/data/tv/S01E01.mkv", URL_FORM_PART),))
        _insert_taggings(db, *self.MIGRATED_ROWS)
        return folder, db

    def test_plex_s_final_rows_read_back_as_replaced_until_written_again(self, tmp_path):
        # The migration's final flag moves the served credits end 2 s later than what we left there.
        folder, _db = self._migrated(tmp_path)
        assert _publisher(tmp_path, folder).shows("7", [URL_INTRO, URL_CREDITS]) is Shown.REPLACED

    @pytest.mark.parametrize(
        ("extra", "state"),
        [
            (URL_FORM_PART, Capability.READY),
            (f"pv%3Aintros={urllib.parse.quote(INTROS_V6_VALUE, safe='')}", Capability.UNSUPPORTED_SCHEMA),
        ],
        ids=["tested-versions", "intro-v6"],
    )
    def test_the_library_sample_reads_url_form_parts_too(self, tmp_path, extra, state):
        folder = tmp_path / "Plex Media Server"
        _make_db(folder, parts=(("/data/tv/S01E01.mkv", None), ("/data/tv/o.mkv", extra)))
        report = _publisher(tmp_path, folder).capability()
        assert report.state is state
        assert state is Capability.READY or "version 6" in report.message


class TestKeepPlexs:
    """``on_plex_redetect`` per type: a type Plex re-detected over ours stays Plex's while the server keeps them."""

    NATIVE_INTRO = (T.INTRO, 990, 29_306)

    @staticmethod
    def _plex_redetects_the_intro(db) -> None:
        conn = sqlite3.connect(db)
        try:
            conn.execute("DELETE FROM taggings WHERE text='intro'")
            conn.commit()
        finally:
            conn.close()
        _insert_taggings(db, (7, 563, 1, "intro", 990, 29_306, INTRO_ROW_EXTRA))
        _set_part_extra(db, 1, encode_extra_data({"pv:credits": CREDITS_FINAL_PAYLOAD, "pv:intros": NATIVE_INTROS}))

    # Plex's credits start and end after the 2498304 ms file does, so they can never fire.
    # Ours, from the credit text, start at 2464 s.
    BONES_DUR = 2_498_304
    BONES_LIMITS = FileLimits(BONES_DUR)
    OUR_BONES_CREDITS = Marker(T.CREDITS, 2_464_000, BONES_DUR, ("credits_text",))
    # Stored 2 s before where Plex serves them (Plex serves credits 2578083–2615648 ms).
    BONES_PLEX_CREDITS = (7, 563, 0, "credits", 2_576_083, 2_615_648, CREDITS_FINAL_ROW_EXTRA)
    SANE_PLEX_CREDITS = (7, 563, 0, "credits", 2_466_000, BONES_DUR, CREDITS_FINAL_ROW_EXTRA)

    @staticmethod
    def _tagging(row: tuple) -> plex_db._TaggingRow:
        _item, _tag, index, text, start, end, extra = row
        return plex_db._TaggingRow(1, index, text, start, end, None, extra)

    @pytest.mark.parametrize("kept_before", [frozenset(), frozenset({T.CREDITS})], ids=["first-write", "kept-before"])
    def test_plexs_marker_that_cant_be_right_is_not_kept(self, kept_before):
        rows = [self._tagging(self.BONES_PLEX_CREDITS)]
        wanted = [self.OUR_BONES_CREDITS]

        kept = plex_db._kept_types(rows, wanted, [], [], kept_before, True, limits=self.BONES_LIMITS)

        assert kept == frozenset()

    def test_plexs_marker_that_can_be_right_is_still_kept(self):
        rows = [self._tagging(self.SANE_PLEX_CREDITS)]

        kept = plex_db._kept_types(rows, [self.OUR_BONES_CREDITS], [], [], frozenset(), True, limits=self.BONES_LIMITS)

        assert kept == frozenset({T.CREDITS})

    def test_plexs_movie_credits_twenty_minutes_before_the_end_are_kept(self):
        # Only an impossible marker is refused: our 900 s movie cap is a rule for our own answers.
        movie_dur = 7_200_000
        row = self._tagging((7, 563, 0, "credits", movie_dur - 1_202_000, movie_dur, CREDITS_FINAL_ROW_EXTRA))
        ours = Marker(T.CREDITS, movie_dur - 600_000, movie_dur, ("credits_text",))

        kept = plex_db._kept_types([row], [ours], [], [], frozenset(), True, limits=FileLimits(movie_dur))

        assert kept == frozenset({T.CREDITS})

    def test_one_sane_row_keeps_the_type_beside_an_impossible_one(self):
        rows = [self._tagging(self.SANE_PLEX_CREDITS), self._tagging(self.BONES_PLEX_CREDITS)]

        kept = plex_db._kept_types(rows, [self.OUR_BONES_CREDITS], [], [], frozenset(), True, limits=self.BONES_LIMITS)

        assert kept == frozenset({T.CREDITS})

    def test_without_the_files_limits_plexs_marker_is_kept_as_before(self):
        # An app older than its agent sends no limits: nothing to check against.
        rows = [self._tagging(self.BONES_PLEX_CREDITS)]
        assert plex_db._kept_types(rows, [self.OUR_BONES_CREDITS], [], [], frozenset(), True) == {T.CREDITS}


class TestNotMadeForThisFile:
    """Plex's markers belong to the metadata item, so they outlive a file replacement (markers detected against the old Blu-ray files stay after Sonarr replaces them). A type's
    rows are stale when BOTH hold: no live part carries Plex's own detection record of them (``pv:intros`` /
    ``pv:credits`` holding those times), and every row was created before every live part was last updated (the
    file's mtime)."""

    BONES_DUR = 2_498_304
    MARKERS_AT = int(datetime(2025, 6, 17, 6, 46, 56, tzinfo=UTC).timestamp())
    PART_UPDATED = int(datetime(2026, 9, 23, 22, 49, 42, tzinfo=UTC).timestamp())
    BONES_INTRO = (7, 563, 0, "intro", 322_622, 354_901, INTRO_ROW_EXTRA)  # can be right: stale only
    BONES_CREDITS = (7, 563, 1, "credits", 2_578_083, 2_615_648, CREDITS_FINAL_ROW_EXTRA)  # past the end too
    OUR_INTRO = Marker(T.INTRO, 60_000, 95_000, ("season_audio",))
    INTRO_RECORD = (
        '{"MediaPartMarkersArray":{"attributeName":"intros","version":5,'
        '"MediaPartMarker":[{"startTimeOffset":322622,"endTimeOffset":354901}]}}'
    )
    OTHER_INTRO_RECORD = INTRO_RECORD.replace("322622", "300000")

    def _item(self, tmp_path, *rows, created_at=MARKERS_AT, updated_at=PART_UPDATED, record=None, parts=None):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder, **({"parts": parts} if parts else {}))
        ids = _insert_taggings(db, *rows)
        for row_id in ids:
            _exec(db, "UPDATE taggings SET created_at=? WHERE id=?", created_at, row_id)
        _exec(db, "UPDATE media_parts SET updated_at=?", updated_at)
        if record is not None:
            _set_part_extra(db, 1, encode_extra_data({"pv:intros": record}))
        return folder, db

    def _read(self, db) -> frozenset:
        return plex_db.LocalPlexDb(lambda: str(db)).read_item(7, deadline=time.monotonic() + 5).stale_types

    @pytest.mark.parametrize(
        ("changes", "stale"),
        [
            ({}, frozenset({T.INTRO})),  # no record, markers older than the file
            ({"record": INTRO_RECORD}, frozenset()),  # Plex's own record of them: made for this file
            ({"record": OTHER_INTRO_RECORD}, frozenset({T.INTRO})),  # a record of other times isn't theirs
            ({"created_at": PART_UPDATED + 3600}, frozenset()),  # tagged after the file changed: fresh
            ({"created_at": PART_UPDATED - 30}, frozenset()),  # within a minute of it: can't tell, fresh
            ({"updated_at": None}, frozenset()),  # the file's time unknown: fresh
            ({"created_at": None}, frozenset()),  # the rows' time unknown: fresh
            ({"record": '{"MediaPartMarkersArray":'}, frozenset()),  # a record this code can't read: fresh
        ],
        ids=[
            "bones-stale",
            "own-record",
            "record-of-other-times",
            "tagged-after-the-file",
            "within-a-minute",
            "no-file-time",
            "no-row-time",
            "unreadable-record",
        ],
    )
    def test_which_types_were_made_for_an_earlier_file(self, tmp_path, changes, stale):
        _folder, db = self._item(tmp_path, self.BONES_INTRO, **changes)
        assert self._read(db) == stale

    def test_final_credits_with_plexs_record_are_fresh(self, tmp_path):
        # The record is compared as stored (time_offset/end_time_offset against startTimeOffset/endTimeOffset), so a
        # row flagged final and an entry that isn't still match.
        record = (
            '{"MediaPartMarkersArray":{"attributeName":"credits","version":4,'
            '"MediaPartMarker":[{"startTimeOffset":2400000,"endTimeOffset":2498304}]}}'
        )
        row = (7, 563, 0, "credits", 2_400_000, 2_498_304, CREDITS_FINAL_ROW_EXTRA)
        _folder, db = self._item(tmp_path, row)
        _set_part_extra(db, 1, encode_extra_data({"pv:credits": record}))
        assert self._read(db) == frozenset()

    @pytest.mark.parametrize("updated_at", [0, -1], ids=["zero", "negative"])
    def test_a_file_time_of_zero_or_less_is_unknown(self, tmp_path, updated_at):
        _folder, db = self._item(tmp_path, self.BONES_INTRO, updated_at=updated_at, created_at=-100)
        assert self._read(db) == frozenset()

    def test_a_version_updated_before_the_markers_keeps_them_fresh(self, tmp_path):
        # Older than every live file, not just one: a version added later doesn't make the others' markers stale.
        parts = (("/data/tv/S01E01.mkv", None), ("/data/tv/S01E01.720p.mkv", None))
        _folder, db = self._item(tmp_path, self.BONES_INTRO, parts=parts)
        _exec(db, "UPDATE media_parts SET updated_at=? WHERE id=2", self.MARKERS_AT - 86_400)
        assert self._read(db) == frozenset()

    def test_each_type_is_judged_on_its_own(self, tmp_path):
        _folder, db = self._item(tmp_path, self.BONES_INTRO, self.BONES_CREDITS, record=self.INTRO_RECORD)
        assert self._read(db) == frozenset({T.CREDITS})

    def test_the_publisher_answers_for_the_pipeline(self, tmp_path):
        folder, _db = self._item(tmp_path, self.BONES_INTRO)
        pub = _publisher(tmp_path, folder, redetect="keep_plex")
        assert pub.types_not_made_for_file("7") == frozenset({T.INTRO})
        assert pub.types_not_made_for_file("not-a-rating-key") is None

    @pytest.mark.parametrize(
        ("stale", "unanswerable"),
        [(frozenset({T.INTRO}), False), (None, True)],
        ids=["read-answers", "agent-older-than-the-answer"],
    )
    def test_the_publisher_says_when_its_setup_can_never_tell(self, tmp_path, monkeypatch, stale, unanswerable):
        # An agent older than ``stale_types`` reads the item without it: the pipeline stops asking it for the job.
        folder, _db = self._item(tmp_path, self.BONES_INTRO)
        pub = _publisher(tmp_path, folder, redetect="keep_plex")
        monkeypatch.setattr(pub._db, "read_item", lambda key, *, deadline: plex_db.ItemRead(True, [], stale))
        assert pub.types_not_made_for_file("7") == stale
        assert pub.stale_types_unanswerable is unanswerable

    def test_a_busy_database_isnt_a_setup_that_can_never_tell(self, tmp_path, monkeypatch):
        monkeypatch.setattr(plex_db, "STALE_READ_WAIT_S", 0.3)
        folder, db = self._item(tmp_path, self.BONES_INTRO)
        pub = _publisher(tmp_path, folder, redetect="keep_plex")
        lock = plex_db._db_lock(str(db))
        lock.acquire()
        try:
            assert pub.types_not_made_for_file("7") is None
        finally:
            lock.release()
        assert pub.stale_types_unanswerable is False

    def test_the_publishers_read_gives_up_quickly_on_a_busy_database(self, tmp_path, monkeypatch):
        # Asked before detection, for every Plex-kept file: a busy database means "can't tell", never a 120 s wait.
        monkeypatch.setattr(plex_db, "STALE_READ_WAIT_S", 0.5)
        folder, db = self._item(tmp_path, self.BONES_INTRO)
        pub = _publisher(tmp_path, folder, redetect="keep_plex")
        lock = plex_db._db_lock(str(db))
        lock.acquire()  # another task of ours holds the database
        start = time.monotonic()
        try:
            answer = pub.types_not_made_for_file("7")
        finally:
            lock.release()
        assert answer is None
        assert time.monotonic() - start < 0.5 + plex_db.WAIT_SLICE_S + 0.5

    def test_a_shorter_db_timeout_bounds_the_read(self, tmp_path, monkeypatch):
        # The Inspector builds its publisher with a shorter db_timeout_s: a page load waits that long, not the job's.
        monkeypatch.setattr(plex_db, "STALE_READ_WAIT_S", 30.0)
        folder, db = self._item(tmp_path, self.BONES_INTRO)
        pub = _publisher(tmp_path, folder, redetect="keep_plex")
        pub._db_timeout_s = 0.3
        lock = plex_db._db_lock(str(db))
        lock.acquire()
        start = time.monotonic()
        try:
            answer = pub.types_not_made_for_file("7")
        finally:
            lock.release()
        assert answer is None
        assert time.monotonic() - start < 0.3 + plex_db.WAIT_SLICE_S + 0.5

    def test_the_publishers_read_stops_when_the_job_is_cancelled(self, tmp_path, monkeypatch):
        monkeypatch.setattr(plex_db, "STALE_READ_WAIT_S", 30.0)
        folder, db = self._item(tmp_path, self.BONES_INTRO)
        pub = _publisher(tmp_path, folder, redetect="keep_plex")
        cancelled, answers = threading.Event(), []

        def ask():
            with cancellable_waits(cancelled.is_set):
                answers.append(pub.types_not_made_for_file("7"))

        lock = plex_db._db_lock(str(db))
        lock.acquire()
        try:
            reader = threading.Thread(target=ask)
            reader.start()
            time.sleep(0.3)
            start = time.monotonic()
            cancelled.set()
            reader.join(10)
            assert time.monotonic() - start < plex_db.WAIT_SLICE_S + 0.5
        finally:
            lock.release()
        assert answers == [None]

    @linux_mount_table
    def test_the_publisher_cant_tell_without_its_database(self, tmp_path):
        folder, _db = self._item(tmp_path, self.BONES_INTRO)
        pub = _publisher(tmp_path, folder, redetect="keep_plex", fs="nfs4")
        assert pub.types_not_made_for_file("7") is None

    @windows_only
    def test_the_publisher_cant_tell_without_its_database_on_windows(self, tmp_path, monkeypatch):
        folder, _db = self._item(tmp_path, self.BONES_INTRO)
        monkeypatch.setattr(winplatform, "drive_type", lambda _path: winplatform.DRIVE_REMOTE)
        assert _publisher(tmp_path, folder, redetect="keep_plex").types_not_made_for_file("7") is None

    @pytest.mark.parametrize(
        ("wanted", "kept_before", "kept"),
        [
            ([OUR_INTRO], frozenset(), frozenset()),  # stale and ours decided: ours replaces Plex's
            # Production's commonest case: kept by an earlier write, stale now, and ours decided: ours replaces it.
            ([OUR_INTRO], frozenset({T.INTRO}), frozenset()),
            ([], frozenset({T.INTRO}), frozenset({T.INTRO})),  # stale and nothing of ours: Plex's, close is better
        ],
        ids=["ours-decided", "kept-before-and-ours-decided", "no-answer-of-ours"],
    )
    def test_kept_types_give_up_a_stale_type_only_for_an_answer_of_ours(self, wanted, kept_before, kept):
        row = plex_db._TaggingRow(1, 0, "intro", 322_622, 354_901, None, INTRO_ROW_EXTRA, self.MARKERS_AT)

        answer = plex_db._kept_types(
            [row], wanted, [], [], kept_before, True, limits=FileLimits(self.BONES_DUR), stale=frozenset({T.INTRO})
        )

        assert answer == kept

    def test_kept_types_keep_a_fresh_type(self):
        row = plex_db._TaggingRow(1, 0, "intro", 322_622, 354_901, None, INTRO_ROW_EXTRA, self.MARKERS_AT)
        kept = plex_db._kept_types(
            [row], [self.OUR_INTRO], [], [], frozenset(), True, limits=FileLimits(self.BONES_DUR)
        )
        assert kept == frozenset({T.INTRO})

    def _write(self, tmp_path, folder, markers, **kwargs):
        pub = _publisher(tmp_path, folder, redetect="keep_plex")
        written = pub.write(
            "7",
            markers,
            previous=[],
            duration_ms=self.BONES_DUR,
            canonical_path="/data/tv/S01E01.mkv",
            limits=FileLimits(self.BONES_DUR),
            **kwargs,
        )
        return pub, written


class TestVersionFiles:
    """The item's versions a write records (``last_item_files``) and the read-back compares with Plex's parts now."""

    A = "/data/tv/S01E01 - 1080p.mkv"
    B = "/data/tv/S01E01 - 2160p.mkv"

    @staticmethod
    def _add_part(db, part_id: int, path: str, *, proxy_type: int | None = None) -> None:
        _exec(db, "INSERT INTO media_items (id, metadata_item_id, proxy_type) VALUES (?, 7, ?)", part_id, proxy_type)
        _exec(db, "INSERT INTO media_parts (id, media_item_id, file) VALUES (?, ?, ?)", part_id, part_id, path)

    def test_different_versions_are_reported_whatever_the_rows_show(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        _make_db(folder, parts=((self.A, None),))
        pub = _publisher(tmp_path, folder)
        assert pub.shows("7", [INTRO], item_files=(self.A,)) is Shown.MISSING  # no rows at all
        assert pub.shows("7", [INTRO], item_files=(self.A, self.B)) is Shown.VERSIONS_CHANGED
        assert pub.shows("7", [], kept_types=frozenset({T.INTRO}), item_files=()) is Shown.VERSIONS_CHANGED


class TestSqliteErrors:
    @pytest.mark.parametrize(
        ("owner", "says", "never_says"),
        [
            (
                "998",
                "This app (user 1000) can't write /plex/db. Run it with the user that owns Plex's database (user 998).",
                "read-only",
            ),
            (
                "1000",
                "This app (user 1000) can't write /plex/db, though it runs as the user that owns Plex's database: the "
                "folder is read-only. Make it writable (in Docker, mount Plex's config folder without :ro).",
                "(user 1000).",
            ),
        ],
        ids=["another-user", "read-only-folder"],
    )
    def test_an_unwritable_database_names_the_fix_that_is_still_missing(self, owner, says, never_says):
        """A writer that already is the database's owner can't be fixed by running it as that user: the folder is
        read-only (a ``:ro`` mount)."""
        from media_preview_generator.markers.publishers.plex_db import unwritable_message

        details = {"unwritable": ["/plex/db"], "writer_uid": "1000", "db_owner_uid": owner}
        message = unwritable_message(details)
        assert message == says
        assert never_says not in message

    @pytest.mark.parametrize(
        ("code", "message", "state"),
        [
            (5, "database is locked", Capability.UNREACHABLE),
            (6, "database table is locked", Capability.UNREACHABLE),
            (517, "database is locked", Capability.UNREACHABLE),
            (8, "attempt to write a readonly database", Capability.MISCONFIGURED),
            (14, "unable to open database file", Capability.MISCONFIGURED),
            (3, "access permission denied", Capability.MISCONFIGURED),
            (10, "disk I/O error", Capability.MISCONFIGURED),
            (26, "file is not a database", Capability.MISCONFIGURED),
            (15, "locking protocol", Capability.UNREACHABLE),
            (13, "database or disk is full", Capability.MISCONFIGURED),
            (11, "database disk image is malformed", Capability.MISCONFIGURED),
            (None, "database or disk is full", Capability.MISCONFIGURED),
            (None, "database disk image is malformed", Capability.MISCONFIGURED),
            (None, "locking protocol", Capability.UNREACHABLE),
            (1, "no such column: x", Capability.UNSUPPORTED_SCHEMA),
            (None, "database is locked", Capability.UNREACHABLE),
            (None, "attempt to write a readonly database", Capability.MISCONFIGURED),
            (None, "something else", Capability.UNSUPPORTED_SCHEMA),
        ],
    )
    def test_sqlite_errors_map_to_capability_states(self, code, message, state):
        labels = {"database or disk is full": "Disk full", "database disk image is malformed": "Database damaged"}
        exc = sqlite3.OperationalError(message)
        if code is not None:
            exc.sqlite_errorcode = code
        err = plex_db.publish_error_from_sqlite(exc)
        assert isinstance(err, PublishError) and err.state is state and message in str(err)
        assert labels.get(message, "") in str(err)
        # Busy and locked (and the transient WAL race) are waits that ran out: a job tries the file again soon.
        assert isinstance(err, DatabaseBusyError) is (state is Capability.UNREACHABLE)

    @pytest.mark.parametrize("code", [11, None], ids=["sqlite-code", "message-only"])
    def test_a_damaged_database_is_said_without_naming_one_feature(self, code):
        # Library Health, loudness and chapters are refused with it too, not only marker writes.
        exc = sqlite3.DatabaseError("database disk image is malformed")
        if code is not None:
            exc.sqlite_errorcode = code
        assert str(plex_db.publish_error_from_sqlite(exc)) == (
            "Database damaged: Plex's database needs repairing before this app can use it "
            "(database disk image is malformed)"
        )


class TestSchemaGuards:
    def test_newest_parts_are_sampled_first(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        good = _plex_json({"pv:intros": NATIVE_INTROS})
        conn = sqlite3.connect(db)
        conn.execute("INSERT INTO media_items (id, metadata_item_id) VALUES (100, 8)")
        conn.executemany(
            "INSERT INTO media_parts (id, media_item_id, file, extra_data) VALUES (?, 100, ?, ?)",
            [(100 + n, f"/o{n}.mkv", good) for n in range(60)] + [(200, "/newest.mkv", INTROS_V6)],
        )
        conn.commit()
        conn.close()
        report = _publisher(tmp_path, folder).capability()
        assert report.state is Capability.UNSUPPORTED_SCHEMA and "version 6" in report.message

    def test_an_older_url_form_part_is_sampled_beside_the_newest_json_ones(self, tmp_path):
        # Plex's credits final migration rewrites parts where they are, so a URL-encoded part keeps its old id: it
        # must not fall out of the sample behind newer JSON parts.
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        good = _plex_json({"pv:intros": NATIVE_INTROS})
        conn = sqlite3.connect(db)
        conn.execute("INSERT INTO media_items (id, metadata_item_id) VALUES (100, 8)")
        conn.executemany(
            "INSERT INTO media_parts (id, media_item_id, file, extra_data) VALUES (?, 100, ?, ?)",
            [(100, "/old.mkv", encode_extra_data({"pv:intros": INTROS_V6_VALUE}, url_form=True))]
            + [(101 + n, f"/o{n}.mkv", good) for n in range(60)],
        )
        conn.commit()
        conn.close()
        report = _publisher(tmp_path, folder).capability()
        assert report.state is Capability.UNSUPPORTED_SCHEMA and "version 6" in report.message

    def test_plex_s_own_triggers_on_other_tables_are_fine(self, tmp_path):
        # Lab PMS 1.43.4 has fts4 triggers on metadata_items and tags only (review PROOF P5).
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        _exec(db, "CREATE TRIGGER fts4_like AFTER UPDATE ON metadata_items BEGIN SELECT 1; END")
        _exec(db, "CREATE TRIGGER fts4_tags_like AFTER UPDATE ON tags BEGIN SELECT 1; END")
        assert _publisher(tmp_path, folder).capability().state is Capability.READY

    @pytest.mark.parametrize(
        "existing",
        [_plex_json({"ma:x": 1}), _plex_json({"ma:y": True}), _plex_json({"pv:intros": None}), _plex_json({"url": 5})],
        ids=["int", "bool", "null-marker", "non-string-url"],
    )
    def test_non_string_extra_data_values_are_unsupported(self, existing):
        with pytest.raises(PublishError) as ei:
            merge_part_extra_data(existing, [INTRO], {T.INTRO}, DUR)
        assert ei.value.state is Capability.UNSUPPORTED_SCHEMA

    @pytest.mark.parametrize(("table", "column"), [("media_items", "deleted_at"), ("media_items", "proxy_type")])
    def test_media_items_columns_we_read_are_required(self, tmp_path, table, column):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        _exec(db, f"ALTER TABLE {table} RENAME COLUMN {column} TO renamed_{column}")
        report = _publisher(tmp_path, folder).capability()
        assert report.state is Capability.UNSUPPORTED_SCHEMA and column in report.message

    def test_detection_prefs_are_not_fetched_when_plex_is_unreachable(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        _make_db(folder)
        pub = _publisher(tmp_path, folder, plex_pass=None)
        report = pub.capability()
        assert report.state is Capability.READY
        assert report.details["detection"] == {"intro": None, "credits": None}
        pub._server.get_marker_detection_prefs.assert_not_called()


class _FastClock:
    """Stands in for ``time`` in plex_db: ``monotonic`` runs ``factor`` times faster, so waits end sooner."""

    def __init__(self, factor: int) -> None:
        self._factor = factor

    def monotonic(self) -> float:
        return time.monotonic() * self._factor

    def __getattr__(self, name: str):
        return getattr(time, name)


class TestLockTimeouts:
    def _paused_writer(self, tmp_path, monkeypatch):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        pub = _publisher(tmp_path, folder)
        inside, release = threading.Event(), threading.Event()
        real_begin = plex_db.LocalPlexDb._begin_write

        # Only the write connection is exclusive; the read before it is shared and wouldn't keep a second writer out.
        def paused_begin(conn, deadline):
            real_begin(conn, deadline)
            inside.set()
            release.wait(10)

        monkeypatch.setattr(plex_db.LocalPlexDb, "_begin_write", staticmethod(paused_begin))
        writer = threading.Thread(target=_write_one, args=(pub, [INTRO]))
        writer.start()
        assert inside.wait(5)
        return db, pub, writer, release

    @staticmethod
    def _four_items(folder):
        """Four episodes, each its own Plex item (7-10), as the four files production saw fail."""
        db = _make_db(folder, parts=(("/data/tv/S01E01.mkv", None),))
        conn = sqlite3.connect(db)
        for n in (2, 3, 4):
            conn.execute("INSERT INTO metadata_items (id, metadata_type, title) VALUES (?, 4, 'Ep')", (6 + n,))
            conn.execute("INSERT INTO media_items (id, metadata_item_id) VALUES (?, ?)", (n, 6 + n))
            conn.execute(
                "INSERT INTO media_parts (id, media_item_id, file) VALUES (?, ?, ?)", (n, n, f"/data/tv/S01E0{n}.mkv")
            )
        conn.commit()
        conn.close()
        return db, [(str(6 + n), f"/data/tv/S01E0{n}.mkv") for n in (1, 2, 3, 4)]

    def _write_all_while_plex_holds_its_write_lock(self, tmp_path, hold_s):
        """Four files published at once while another program holds Plex's write lock for ``hold_s``."""
        folder = tmp_path / "Plex Media Server"
        db, items = self._four_items(folder)
        publishers = [_publisher(tmp_path, folder) for _ in items]
        results: dict[str, object] = {}

        def publish(pub, item_id, path):
            try:
                results[item_id] = _write_one(pub, [INTRO], item_id=item_id, path=path)
            except PublishError as exc:
                results[item_id] = exc

        locker = sqlite3.connect(db, isolation_level=None, check_same_thread=False)
        locker.execute("BEGIN IMMEDIATE")  # e.g. a Kometa-style tool writing to Plex's database
        threads = [
            threading.Thread(target=publish, args=(pub, *item)) for pub, item in zip(publishers, items, strict=True)
        ]
        for thread in threads:
            thread.start()
        time.sleep(hold_s)
        locker.execute("ROLLBACK")
        locker.close()
        for thread in threads:
            thread.join(30)
        return db, results

    # Production: another program held Plex's write lock for 30.8 s against a 30 s wait; four files failed. Scaled
    # down 1:120 so the test takes seconds: the job wait keeps its ratio to the old one. A Windows runner commits in up
    # to ~0.7 s (four writes alone outlast the 1:120 wait), so there it is scaled 1:20.
    SCALE = 20 if ON_WINDOWS else 120
    OLD_WAIT_S = 30.0
    HELD_S = 30.8

    def test_a_job_waits_120_s_for_plex_s_database(self):
        assert plex_db.BUSY_TIMEOUT_S == 120.0 > self.HELD_S


class TestLockSerialisation:
    """The real probe against every kind of open connection (read, capability, write)."""

    @pytest.fixture(autouse=True)
    def plex_holds_the_database(self):
        """Overrides the module fixture: the real probe runs."""

    @pytest.fixture(autouse=True)
    def journal_mode(self):
        """Overrides the module fixture: WAL only, the probe needs a -shm file."""

    @pytest.mark.skipif(ON_WINDOWS, reason="fcntl; Windows has its own lock query (the test below)")
    def test_a_failing_lock_query_is_not_a_holder(self, tmp_path, monkeypatch):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder, journal_mode="wal")

        def failing_fcntl(*_args):
            raise OSError("no locks here")

        with _another_process_holding(db):
            monkeypatch.setattr(plex_db.fcntl, "fcntl", failing_fcntl)
            assert _publisher(tmp_path, folder).capability().state is Capability.NEEDS_LOCAL_DB

    @windows_only
    def test_lock_queries_that_cannot_answer_are_not_a_holder_on_windows(self, tmp_path, monkeypatch):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder, journal_mode="wal")
        with _another_process_holding(db):
            monkeypatch.setattr(winplatform, "restart_manager_holders", lambda _path: None)
            monkeypatch.setattr(winplatform, "dms_lock_held", lambda _path: None)
            assert _publisher(tmp_path, folder).capability().state is Capability.NEEDS_LOCAL_DB

    def test_probe_takes_the_database_lock_itself(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder, journal_mode="wal")
        lock = plex_db._db_lock(str(db))
        lock.acquire()
        try:
            start = time.monotonic()
            with pytest.raises(PublishError) as ei:
                plex_db.shm_lock_held_elsewhere(str(db), deadline=time.monotonic() + 0.2)
            assert ei.value.state is Capability.UNREACHABLE and time.monotonic() - start < 2.0
        finally:
            lock.release()

    def test_capability_reports_a_busy_database_lock_instead_of_raising(self, tmp_path, monkeypatch):
        monkeypatch.setattr(plex_db, "BUSY_TIMEOUT_S", 0.3)
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder, journal_mode="wal")
        lock = plex_db._db_lock(str(db))
        lock.acquire()
        try:
            report = _publisher(tmp_path, folder).capability()
        finally:
            lock.release()
        assert report.state is Capability.UNREACHABLE and "still using this Plex database" in report.message

    def test_a_failed_connect_leaves_no_open_connection_behind(self, tmp_path, monkeypatch):
        # Otherwise every later probe on this thread would refuse to run ("inside an open connection").
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder, journal_mode="wal")
        database = LocalPlexDb(lambda: str(db))
        original, calls = LocalPlexDb._connect, []

        def failing_once(self, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise sqlite3.OperationalError("unable to open database file")
            return original(self, **kwargs)

        monkeypatch.setattr(LocalPlexDb, "_connect", failing_once)
        with pytest.raises(sqlite3.OperationalError), database._database(read_only=True, deadline=time.monotonic() + 5):
            pass
        assert plex_db.shm_lock_held_elsewhere(str(db)) is False  # runs; no holder in this test

    def test_probe_inside_our_own_open_connection_is_refused(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder, journal_mode="wal")
        with LocalPlexDb(lambda: str(db))._database(read_only=True, deadline=time.monotonic() + 5):
            with pytest.raises(RuntimeError, match="open connection"):
                plex_db.shm_lock_held_elsewhere(str(db))

    def test_one_lock_for_every_path_to_the_same_database(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder, journal_mode="wal")
        dotted = f"{folder}/../{folder.name}/Plug-in Support/Databases/{db.name}"
        (tmp_path / "symlinked").symlink_to(folder)
        via_symlink = tmp_path / "symlinked" / "Plug-in Support" / "Databases" / db.name
        hardlink = tmp_path / "second-mount.db"  # same inode, like a second bind mount of the folder
        os.link(db, hardlink)
        lock = plex_db._db_lock(str(db))
        assert all(plex_db._db_lock(str(p)) is lock for p in (dotted, via_symlink, hardlink))
        missing = tmp_path / "missing" / "x.db"
        assert plex_db._db_lock(f"{tmp_path}/missing/../missing/x.db") is plex_db._db_lock(str(missing))
        assert plex_db._db_lock(str(missing)) is not lock


class TestPlexVersionDetail:
    @pytest.mark.parametrize(("plex_pass", "version"), [(True, PLEX_VERSION), (None, None)])
    def test_capability_reports_the_plex_version(self, tmp_path, plex_pass, version):
        folder = tmp_path / "Plex Media Server"
        _make_db(folder)
        report = _publisher(tmp_path, folder, plex_pass=plex_pass).capability()
        assert report.state is Capability.READY and report.details["plex_version"] == version


class TestItemDesiredSet:
    """Plex serves one marker set per item: write() computes it across every version (plex-item-publish-design.md)."""

    A, B = "/data/tv/E - 1080p.mkv", "/data/tv/E - 2160p.mkv"
    EARLY_CREDITS = Marker(T.CREDITS, 1_280_000, DUR, ("chapters",))  # 19 s earlier than CREDITS_FINAL

    def _item(self, tmp_path, files, decided, *, native_credits=False):
        folder = tmp_path / "Plex Media Server"
        extra = _plex_json({"pv:credits": NATIVE_CREDITS}) if native_credits else None
        db = _make_db(folder, parts=tuple((f, extra) for f in files))
        pub = _publisher(tmp_path, folder, sibling_markers=decided.get)
        return db, pub

    def _rows_and_parts(self, db):
        rows = _rows(
            db, "SELECT id, [index], text, time_offset, end_time_offset, extra_data FROM taggings ORDER BY [index]"
        )
        parts = [json.loads(r[0]) if r[0] else {} for r in _rows(db, "SELECT extra_data FROM media_parts ORDER BY id")]
        return rows, parts

    def test_only_plex_is_atomic(self):
        assert PlexMarkerPublisher.atomic_writes is True
        assert MarkerPublisher.atomic_writes is False


class TestRoundTwoLows:
    def test_pv_final_zero_is_not_final(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        _insert_taggings(
            db,
            (
                7,
                563,
                0,
                "credits",
                1_297_000,
                DUR,
                '{"pv:final":"0","pv:version":"4","url":"pv%3Afinal=0&pv%3Aversion=4"}',
            ),
        )
        assert _served(db) == [(T.CREDITS, 1_299_000, DUR - 2_000)]


class TestReadBackMany:
    """Check servers reads every published Plex item back: one connection per item, the lock proof once per call."""

    def _items(self, keys, ours=(INTRO,)):
        return [(str(k), list(ours), frozenset(), None) for k in keys]

    def test_one_connection_per_item_and_the_checks_once_per_call(self, tmp_path, monkeypatch):
        folder = tmp_path / "Plex Media Server"
        _make_db(folder)
        pub = _publisher(tmp_path, folder)
        opened, local_checks, schema_checks = [], [], []
        real_database, real_local, real_schema = (
            LocalPlexDb._database,
            PlexMarkerPublisher._local_checks,
            (LocalPlexDb._check_schema),
        )

        def counting_database(self, **kwargs):
            opened.append(kwargs["read_only"])
            return real_database(self, **kwargs)

        monkeypatch.setattr(LocalPlexDb, "_database", counting_database)
        monkeypatch.setattr(
            PlexMarkerPublisher, "_local_checks", lambda self, **kw: local_checks.append(1) or real_local(self, **kw)
        )
        monkeypatch.setattr(
            LocalPlexDb,
            "_check_schema",
            staticmethod(lambda conn: schema_checks.append(1) or real_schema(conn)),
        )
        out = pub.shows_many(self._items([7, 8, "x", 9, 10]))
        assert out == {"7": Shown.MISSING, "8": Shown.GONE, "x": None, "9": Shown.GONE, "10": Shown.GONE}
        assert opened == [True, True, True, True]  # no connection for an id that isn't a rating key
        assert (len(local_checks), len(schema_checks)) == (1, 1)

    @pytest.mark.parametrize(
        "problem", ["settings-off", "no-lock-holder", "schema"], ids=["off", "plex-stopped", "schema-changed"]
    )
    def test_nothing_is_read_when_the_database_cant_be_shared(self, tmp_path, monkeypatch, problem):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        pub = _publisher(tmp_path, folder, enabled=problem != "settings-off")
        if problem == "no-lock-holder":
            monkeypatch.setattr(plex_db, "shm_lock_held_elsewhere", lambda _db, **_kw: False)
        elif problem == "schema":
            _exec(db, "CREATE TRIGGER t AFTER INSERT ON taggings BEGIN SELECT 1; END")
        connected = []
        monkeypatch.setattr(LocalPlexDb, "_connect", lambda self, **kw: connected.append(kw) or sqlite3.connect(db))
        assert pub.shows_many(self._items(["7", "8"])) == {"7": None, "8": None}
        assert len(connected) == (1 if problem == "schema" else 0)  # a changed schema stops the call after one look

    @pytest.mark.parametrize(
        ("case", "missing"),
        [("item-there", False), ("item-deleted", True), ("not-a-rating-key", None), ("plex-stopped", None),
         ("database-busy", None)],
    )  # fmt: skip
    def test_item_missing_asks_plexs_database(self, tmp_path, monkeypatch, case, missing):
        folder = tmp_path / "Plex Media Server"
        _make_db(folder)  # item 7
        pub = _publisher(tmp_path, folder)
        item_id = {"item-deleted": "8", "not-a-rating-key": "x"}.get(case, "7")
        if case == "plex-stopped":
            monkeypatch.setattr(plex_db, "shm_lock_held_elsewhere", lambda _db, **_kw: False)
        elif case == "database-busy":

            def busy(self, **kwargs):
                raise PublishError("Another task is still using this Plex database",
                                   state=Capability.UNREACHABLE)  # fmt: skip

            monkeypatch.setattr(LocalPlexDb, "_database", busy)
        assert pub.item_missing(item_id) is missing

    def test_between_two_items_it_pauses_with_the_lock_free(self, tmp_path, monkeypatch):
        # A released threading.Lock can be taken again by the same thread before a waiting writer wakes (Python before
        # 3.13 hands it to no one in particular); the pause gives the writer the moment to take it.
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        pub = _publisher(tmp_path, folder)
        pauses = []
        real_sleep = time.sleep
        monkeypatch.setattr(
            plex_db.time,
            "sleep",
            lambda s: (
                pauses.append((s, plex_db._db_lock(str(db)).locked()))
                if s == plex_db.READ_BACK_PAUSE_S
                else real_sleep(s)
            ),
        )
        pub.shows_many(self._items([7, "x", 8, 9]))
        assert pauses == [(plex_db.READ_BACK_PAUSE_S, False)] * 2  # before items 8 and 9; none before the first read

    def test_a_cancel_stops_before_the_next_item(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        _make_db(folder)
        pub = _publisher(tmp_path, folder)
        answers = iter([False, False, True])
        out = pub.shows_many(self._items(["1", "2", "3", "4"]), cancel_check=lambda: next(answers))
        assert list(out) == ["1", "2"]

    def test_shows_is_a_read_back_of_one_item(self, tmp_path, monkeypatch):
        folder = tmp_path / "Plex Media Server"
        _make_db(folder)
        pub = _publisher(tmp_path, folder)
        many = MagicMock(return_value={"7": Shown.REPLACED})
        monkeypatch.setattr(pub, "shows_many", many)
        assert pub.shows("7", (INTRO,), kept_types={T.CREDITS}, item_files=("/a.mkv",)) is Shown.REPLACED
        assert many.call_args.args == ([("7", [INTRO], frozenset({T.CREDITS}), ("/a.mkv",))],)

    @pytest.mark.parametrize("ui_details", [True, False])
    def test_detection_settings_are_asked_only_for_the_edit_dialog(self, tmp_path, ui_details):
        folder = tmp_path / "Plex Media Server"
        _make_db(folder)
        pub = _publisher(tmp_path, folder)
        pub._ui_details = ui_details
        report = pub.capability()
        assert report.ready
        assert pub._server.get_marker_detection_prefs.called is ui_details
        assert report.details["detection"] == (
            {"intro": "never", "credits": "never"} if ui_details else {"intro": None, "credits": None}
        )


def _unc(path: Path) -> Path:
    """``path`` through this machine's administrative share: the same file, on a network drive."""
    drive, rest = os.path.splitdrive(str(path))
    unc = Path(rf"\\localhost\{drive[0]}${rest}")
    if not unc.is_dir():
        pytest.skip("the administrative share isn't reachable here")
    return unc


@windows_only
class TestWindowsDrives:
    """What the mount-table tests refuse on Linux, refused through the drive type on Windows."""

    def test_a_symlinked_config_folder_is_judged_by_where_it_points(self, tmp_path):
        nas = tmp_path / "nas"
        _make_db(nas / "Plex Media Server")
        (tmp_path / "config").symlink_to(_unc(nas), target_is_directory=True)
        report = _publisher(tmp_path, tmp_path / "config" / "Plex Media Server").capability()
        assert report.state is Capability.NEEDS_LOCAL_DB and report.details["fs_type"] == "network drive"

    def test_an_unwritable_database_names_the_windows_users(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        db.chmod(0o444)
        try:
            report = _publisher(tmp_path, folder).capability()
        finally:
            db.chmod(0o666)
        assert report.state is Capability.MISCONFIGURED
        assert os.environ["USERNAME"].lower() in report.details["writer_uid"].lower()
        assert "\\" in report.details["db_owner_uid"]


SHARED_FILE = "/data/tv/Show - S03E01-E02 - Two Parter.mkv"
OTHER_INTRO = Marker(T.INTRO, 50_000, 80_000, ("chapters",))
NATIVE_INTRO_ROW = (T.INTRO, 990, 29_306)


def _add_item(db, key, files, *, section=1, metadata_type=4):
    """Another item of the library: one media item and part per file (Plex's own copy of each part)."""
    _exec(
        db,
        "INSERT INTO metadata_items (id, metadata_type, title, library_section_id) VALUES (?, ?, 'Ep', ?)",
        key,
        metadata_type,
        section,
    )
    part_ids = []
    for file in files:
        media = _exec(db, "INSERT INTO media_items (metadata_item_id) VALUES (?)", key)
        part_ids.append(_exec(db, "INSERT INTO media_parts (media_item_id, file) VALUES (?, ?)", media, file))
    return part_ids


def _multi_episode_db(folder, episodes=(8,)):
    """Item 7 (part 1) and one item per further episode, each with its own copy of the one shared file's part."""
    db = _make_db(folder, parts=((SHARED_FILE, None),))
    _exec(db, "UPDATE metadata_items SET library_section_id=1 WHERE id=7")
    parts = {7: 1}
    for key in episodes:
        parts[key] = _add_item(db, key, [SHARED_FILE])[0]
    return db, parts


class _Records:
    """markers.db's item records as a job keeps them: the written item's, and each sibling's from ``last_siblings``."""

    def __init__(self) -> None:
        self.rows: dict[str, plex_db.ItemRecord] = {}

    def lookup(self, item_id: str) -> plex_db.ItemRecord | None:
        return self.rows.get(item_id)

    def write(self, pub, markers, *, previous=(), kept_types=None):
        record = self.rows.get("7")
        kept = (record.kept_types if record else frozenset()) if kept_types is None else kept_types
        ours = pub.write(
            "7",
            list(markers),
            previous=None if previous is None else list(previous),
            duration_ms=DUR,
            canonical_path=SHARED_FILE,
            kept_types=kept,
        )
        self.rows["7"] = plex_db.ItemRecord(list(ours), pub.last_kept_types, pub.last_item_files)
        for other, (other_ours, other_kept) in pub.last_siblings.items():
            self.rows[other] = plex_db.ItemRecord(list(other_ours), other_kept, pub.last_item_files)
        return ours

    def shown(self, pub):
        record = self.rows["7"]
        return pub.shows_many([("7", record.markers, record.kept_types, record.item_files)])["7"]


def _recording_publisher(tmp_path, folder, records, **kwargs):
    pub = _publisher(tmp_path, folder, **kwargs)
    pub._item_record = records.lookup
    return pub
