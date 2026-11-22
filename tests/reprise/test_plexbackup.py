"""The daily backup of Plex's database: online copy of a live WAL database, once per UTC day, last 7 kept."""

import collections
import contextlib
import os
import re
import shutil
import sqlite3
import stat
import threading
import types
from datetime import UTC, datetime

import pytest

from media_preview_generator.reprise import plexbackup
from media_preview_generator.reprise.plexbackup import BackupFailed, backup_dir, ensure_daily_backup, todays_backup

NOW = 1_700_000_000  # 2023-11-14 22:13:20 UTC
DAY = 86_400
FIRST_NAME = "com.plexapp.plugins.library.db.20231114T221320Z.bak"
Usage = collections.namedtuple("Usage", "total used free")


def _name(now):
    return "com.plexapp.plugins.library.db." + datetime.fromtimestamp(now, UTC).strftime("%Y%m%dT%H%M%SZ.bak")


def _count(path):
    with contextlib.closing(sqlite3.connect(path)) as conn:
        return conn.execute("SELECT count(*) FROM items").fetchone()[0]


@pytest.fixture
def plex(tmp_path):
    """A live Plex-like database: WAL mode, three committed rows that only the -wal file holds, writer still open."""
    folder = tmp_path / "plex"
    folder.mkdir()
    path = str(folder / "com.plexapp.plugins.library.db")
    writer = sqlite3.connect(path, isolation_level=None)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, title TEXT)")
    for title in ("one", "two", "three"):
        writer.execute("INSERT INTO items (title) VALUES (?)", (title,))
    yield path, writer
    writer.close()


@pytest.fixture
def directory(tmp_path):
    return backup_dir(str(tmp_path / "config"))


def test_backup_dir_is_inside_the_config_folder():
    assert backup_dir("/config") == os.path.join("/config", "plex-db-backups")


def test_backup_contains_the_rows_that_only_the_wal_file_holds(plex, directory, tmp_path):
    path, _writer = plex
    assert os.path.getsize(path + "-wal") > 0
    # A plain copy of the main file misses everything: the proof that the online backup is needed.
    shutil.copy(path, tmp_path / "naive.db")
    with pytest.raises(sqlite3.OperationalError):
        _count(str(tmp_path / "naive.db"))

    backup, created = ensure_daily_backup(path, directory, now=NOW)

    assert created is True
    assert _count(backup) == 3


def test_backup_has_no_uncommitted_rows_and_includes_later_commits(plex, directory):
    path, writer = plex
    writer.execute("BEGIN")
    writer.execute("INSERT INTO items (title) VALUES ('pending')")

    first, _ = ensure_daily_backup(path, directory, now=NOW)
    writer.execute("COMMIT")
    second, _ = ensure_daily_backup(path, directory, now=NOW + DAY)

    assert _count(first) == 3
    assert _count(second) == 4


def test_first_call_creates_a_named_private_single_file_backup(plex, directory):
    path, _writer = plex

    backup, created = ensure_daily_backup(path, directory, now=NOW)

    assert created is True
    assert backup == os.path.join(directory, FIRST_NAME)
    assert re.fullmatch(r"com\.plexapp\.plugins\.library\.db\.\d{8}T\d{6}Z\.bak", os.path.basename(backup))
    assert stat.S_IMODE(os.stat(backup).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(directory).st_mode) == 0o700
    assert os.listdir(directory) == [FIRST_NAME]


def test_second_call_the_same_utc_day_returns_the_same_file(plex, directory):
    path, _writer = plex
    first, _ = ensure_daily_backup(path, directory, now=NOW)
    mtime = os.stat(first).st_mtime_ns

    again, created = ensure_daily_backup(path, directory, now=NOW + 60)
    late, late_created = ensure_daily_backup(path, directory, now=NOW + 3600)  # still 2023-11-14 23:13

    assert (again, created) == (first, False)
    assert (late, late_created) == (first, False)
    assert os.stat(first).st_mtime_ns == mtime
    assert os.listdir(directory) == [FIRST_NAME]


def test_a_different_utc_day_creates_a_new_backup(plex, directory):
    path, _writer = plex
    first, _ = ensure_daily_backup(path, directory, now=NOW)

    second, created = ensure_daily_backup(path, directory, now=NOW + DAY)

    assert created is True
    assert second != first
    assert os.path.basename(second) == _name(NOW + DAY)
    assert sorted(os.listdir(directory)) == sorted([os.path.basename(first), os.path.basename(second)])


def test_the_day_changes_at_utc_midnight(plex, directory):
    path, _writer = plex
    midnight = NOW - NOW % DAY + DAY  # 2023-11-15 00:00:00 UTC
    first, _ = ensure_daily_backup(path, directory, now=midnight - 1)

    same, same_created = ensure_daily_backup(path, directory, now=midnight - 2)
    next_day, next_created = ensure_daily_backup(path, directory, now=midnight)

    assert (same, same_created) == (first, False)
    assert next_created is True
    assert next_day != first


def test_keep_prunes_the_oldest_and_leaves_other_files_alone(plex, directory):
    path, _writer = plex
    os.makedirs(directory)
    others = [
        "notes.txt",
        "com.plexapp.plugins.library.db.bak",
        "com.plexapp.plugins.library.db.20200101T000000Z.bak.old",
        ".partial-abc",
        "com.plexapp.plugins.library.db.2019-01-01.bak",
    ]
    for name in others:
        with open(os.path.join(directory, name), "w") as handle:
            handle.write("keep me")

    made = [ensure_daily_backup(path, directory, now=NOW + i * DAY, keep=3)[0] for i in range(5)]

    assert sorted(os.listdir(directory)) == sorted([os.path.basename(p) for p in made[2:]] + others)
    for name in others:
        with open(os.path.join(directory, name)) as handle:
            assert handle.read() == "keep me"


def test_the_default_keeps_seven(plex, directory):
    path, _writer = plex
    for i in range(9):
        ensure_daily_backup(path, directory, now=NOW + i * DAY)

    assert sorted(os.listdir(directory)) == [_name(NOW + i * DAY) for i in range(2, 9)]


def test_pruning_never_deletes_the_backup_just_created(plex, directory):
    path, _writer = plex
    for i in range(1, 4):
        ensure_daily_backup(path, directory, now=NOW + i * DAY)

    # A clock that went backwards: the new backup is the oldest by name, yet it must survive.
    old, created = ensure_daily_backup(path, directory, now=NOW, keep=2)

    assert created is True
    assert os.path.exists(old)


def test_a_missing_source_fails_and_leaves_nothing(tmp_path, directory):
    with pytest.raises(BackupFailed) as caught:
        ensure_daily_backup(str(tmp_path / "nope.db"), directory, now=NOW)

    assert str(caught.value).endswith(".")
    assert not os.path.exists(directory) or os.listdir(directory) == []


def test_a_corrupt_source_fails_and_leaves_no_partial_file(tmp_path, directory):
    garbage = tmp_path / "com.plexapp.plugins.library.db"
    garbage.write_bytes(b"this is not a database " * 400)

    with pytest.raises(BackupFailed, match="could not be read"):
        ensure_daily_backup(str(garbage), directory, now=NOW)

    assert os.listdir(directory) == []


def test_a_copy_that_fails_its_own_check_is_thrown_away(plex, directory, monkeypatch):
    path, _writer = plex
    monkeypatch.setattr(plexbackup, "_quick_check_ok", lambda conn: False)

    with pytest.raises(BackupFailed, match="failed its own check"):
        ensure_daily_backup(path, directory, now=NOW)

    assert os.listdir(directory) == []


def test_not_enough_disk_space_fails_and_creates_nothing(plex, directory, monkeypatch):
    path, _writer = plex
    monkeypatch.setattr(plexbackup.shutil, "disk_usage", lambda _path: Usage(10**9, 10**9, 0))

    with pytest.raises(BackupFailed, match="not enough free disk space"):
        ensure_daily_backup(path, directory, now=NOW)

    assert os.listdir(directory) == []


def test_the_free_space_needed_is_twice_the_database_and_its_wal(plex, directory, monkeypatch):
    path, _writer = plex
    needed = 2 * (os.path.getsize(path) + os.path.getsize(path + "-wal"))

    monkeypatch.setattr(plexbackup.shutil, "disk_usage", lambda _path: Usage(0, 0, needed - 1))
    with pytest.raises(BackupFailed):
        ensure_daily_backup(path, directory, now=NOW)

    monkeypatch.setattr(plexbackup.shutil, "disk_usage", lambda _path: Usage(0, 0, needed))
    _backup, created = ensure_daily_backup(path, directory, now=NOW)
    assert created is True


def test_a_failed_backup_never_replaces_an_existing_good_one(plex, directory, tmp_path):
    path, _writer = plex
    good, _ = ensure_daily_backup(path, directory, now=NOW)
    with open(good, "rb") as handle:
        before = handle.read()
    garbage = tmp_path / "broken.db"
    garbage.write_bytes(b"x" * 8192)

    with pytest.raises(BackupFailed):
        ensure_daily_backup(str(garbage), directory, now=NOW + DAY)

    assert os.listdir(directory) == [os.path.basename(good)]
    with open(good, "rb") as handle:
        assert handle.read() == before
    assert todays_backup(directory, NOW + DAY) is None


def test_an_unwritable_backup_folder_fails_with_a_plain_sentence(plex, tmp_path):
    path, _writer = plex
    blocker = tmp_path / "config"
    blocker.write_text("a file where the folder should go")

    with pytest.raises(BackupFailed, match="The backup folder could not be written."):
        ensure_daily_backup(path, backup_dir(str(blocker)), now=NOW)


def test_the_source_database_is_not_modified(plex, directory):
    path, _writer = plex
    with open(path, "rb") as handle:
        main_before = handle.read()
    with open(path + "-wal", "rb") as handle:
        wal_before = handle.read()

    ensure_daily_backup(path, directory, now=NOW)

    with open(path, "rb") as handle:
        assert handle.read() == main_before
    with open(path + "-wal", "rb") as handle:
        assert handle.read() == wal_before
    with contextlib.closing(sqlite3.connect(path)) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]


def test_two_callers_at_once_make_one_backup(plex, directory):
    path, _writer = plex
    results = []

    def call():
        results.append(ensure_daily_backup(path, directory, now=NOW))

    threads = [threading.Thread(target=call) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(created for _path, created in results) == [False, False, False, True]
    assert {path for path, _created in results} == {os.path.join(directory, FIRST_NAME)}


def test_todays_backup_ignores_partial_and_other_files(tmp_path):
    folder = str(tmp_path)
    assert todays_backup(str(tmp_path / "missing"), NOW) is None
    for name in (
        ".partial-0123",
        FIRST_NAME + ".tmp",
        FIRST_NAME[:-4],
        "notes.txt",
        "com.plexapp.plugins.library.db.20231113T235959Z.bak",
        "com.plexapp.plugins.library.db.20231115T000000Z.bak",
    ):
        (tmp_path / name).write_text("x")
    assert todays_backup(folder, NOW) is None

    early = tmp_path / "com.plexapp.plugins.library.db.20231114T010000Z.bak"
    late = tmp_path / "com.plexapp.plugins.library.db.20231114T230000Z.bak"
    early.write_text("x")
    late.write_text("x")

    assert todays_backup(folder, NOW) == str(late)
    assert todays_backup(folder, NOW + DAY) == str(tmp_path / "com.plexapp.plugins.library.db.20231115T000000Z.bak")


def test_todays_backup_defaults_to_the_current_time(tmp_path, monkeypatch):
    monkeypatch.setattr(plexbackup, "time", types.SimpleNamespace(time=lambda: NOW))
    (tmp_path / FIRST_NAME).write_text("x")

    assert todays_backup(str(tmp_path)) == str(tmp_path / FIRST_NAME)
