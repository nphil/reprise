"""A safe daily backup of Plex's database, taken before Reprise's first automatic write of each UTC day.

Plex keeps running while Reprise copies its database, and the database is in WAL mode (recent changes live in a
second file beside it), so a plain file copy could miss changes or catch a half-written page. The copy goes through
SQLite's online backup API on a read-only connection that holds the same process-wide lock as Reprise's own Plex
writer. It is written to a hidden temporary file, checked with ``PRAGMA quick_check`` and only then renamed to its
final name, so a backup that exists is always a finished, readable one. Only the newest :data:`KEEP` are kept.

Every expected failure is a :class:`BackupFailed` with a plain sentence; the caller must not write to Plex then.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import sqlite3
import threading
import time
import uuid
from datetime import UTC, datetime

from loguru import logger

from ..markers.publishers import plex_db

BACKUP_DIR_NAME = "plex-db-backups"
KEEP = 7

_DB_NAME = "com.plexapp.plugins.library.db"
_NAME = re.compile(rf"{re.escape(_DB_NAME)}\.(\d{{8}}T\d{{6}}Z)\.bak")
_STAMP = "%Y%m%dT%H%M%SZ"
# How long to wait for the process-wide lock of the Plex database before giving up.
_LOCK_WAIT_S = 120
_LEFTOVER_SUFFIXES = ("", "-journal", "-wal", "-shm")

_lock = threading.Lock()


class BackupFailed(Exception):
    """The backup could not be made; ``str(exc)`` is one plain sentence fit for the UI and a phone alert."""


def backup_dir(config_dir: str) -> str:
    """The folder inside Reprise's config folder that holds the backups."""
    return os.path.join(config_dir, BACKUP_DIR_NAME)


def _backups(directory: str) -> list[tuple[str, str]]:
    """The finished backups in ``directory`` as ``(timestamp, path)``, oldest first; other files are ignored."""
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    found = [(match.group(1), os.path.join(directory, name)) for name in names if (match := _NAME.fullmatch(name))]
    return sorted(found)


def todays_backup(directory: str, now: float | None = None) -> str | None:
    """Path of the newest finished backup made on ``now``'s UTC date, or None."""
    today = datetime.fromtimestamp(time.time() if now is None else now, UTC).strftime("%Y%m%d")
    same_day = [path for stamp, path in _backups(directory) if stamp.startswith(today)]
    return same_day[-1] if same_day else None


def _remove_partial(partial: str) -> None:
    for suffix in _LEFTOVER_SUFFIXES:
        with contextlib.suppress(FileNotFoundError):
            os.remove(partial + suffix)


# Plex's own SQLite build adds a full-text tokenizer and ICU collations that stock SQLite can't load. A check that needs
# one of them fails for that reason alone, not because the copy is damaged.
_NEEDS_PLEX_EXTENSION = ("unknown tokenizer", "no such collation sequence")


def _quick_check_ok(conn: sqlite3.Connection) -> bool:
    """Every ordinary table of the copy is sound.

    Each table is checked by name (structure, and its indexes). A table whose check needs one of Plex's own extensions
    is instead read in full without its indexes, which still proves every row of the copy is readable. Virtual tables
    and their shadow tables are left out: they can't be opened without Plex's tokenizer at all.
    """
    try:
        virtual = {
            name
            for (name,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND sql LIKE 'CREATE VIRTUAL%'"
            )
        }
        tables = [
            name
            for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            if name not in virtual and not any(name.startswith(v + "_") for v in virtual)
        ]
        if not tables:
            return False
        for name in tables:
            try:
                if conn.execute(f"PRAGMA quick_check({_quoted(name)})").fetchall() != [("ok",)]:
                    logger.warning("The backup of Plex's database failed its check on table {}", name)
                    return False
            except sqlite3.OperationalError as exc:
                if not any(text in str(exc) for text in _NEEDS_PLEX_EXTENSION):
                    raise
                conn.execute(f"SELECT count(*) FROM {_quoted(name)} NOT INDEXED").fetchone()
        return True
    except sqlite3.Error as exc:
        logger.warning("Checking the backup of Plex's database failed: {}", exc)
        return False


def _quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _check_free_space(db_path: str, directory: str) -> None:
    needed = os.path.getsize(db_path)
    with contextlib.suppress(FileNotFoundError):
        needed += os.path.getsize(db_path + "-wal")
    if shutil.disk_usage(directory).free < 2 * needed:
        logger.warning("Not enough free space in {} for a Plex backup ({} bytes needed)", directory, 2 * needed)
        raise BackupFailed("There is not enough free disk space to back up Plex's database.")


def _copy(db_path: str, partial: str) -> None:
    """Copy the database into the new file ``partial`` and make sure the copy is sound."""
    # Created by us with 0600 before SQLite opens it, so the backup is never readable by others, even briefly.
    os.close(os.open(partial, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    source = plex_db.LocalPlexDb(lambda: db_path)
    try:
        with source._database(read_only=True, deadline=time.monotonic() + _LOCK_WAIT_S) as src:
            dst = sqlite3.connect(partial)
            try:
                src.backup(dst)
                # The copy keeps Plex's WAL flag; a plain single-file backup is simpler to open and move.
                dst.execute("PRAGMA journal_mode=DELETE")
                sound = _quick_check_ok(dst)
            finally:
                dst.close()
    except (plex_db.PublishError, sqlite3.Error, OSError) as exc:
        logger.warning("Reading Plex's database for the backup failed: {}", exc)
        raise BackupFailed("Plex's database could not be read for the backup.") from exc
    if not sound:
        raise BackupFailed("The backup of Plex's database failed its own check, so it was thrown away.")


def _prune(directory: str, keep: int, newest: str) -> None:
    """Delete all but the newest ``keep`` backups; ``newest`` is never deleted."""
    for _stamp, path in _backups(directory)[: -max(keep, 1)]:
        if path != newest:
            with contextlib.suppress(OSError):
                os.remove(path)


def ensure_daily_backup(
    db_path: str, directory: str, *, now: float | None = None, keep: int = KEEP
) -> tuple[str, bool]:
    """Return ``(backup path, created)``: today's (UTC) backup, made now unless it already exists.

    A new backup is followed by pruning to the newest ``keep``.

    Raises:
        BackupFailed: The backup could not be made; nothing was left behind and older backups are untouched.
    """
    when = time.time() if now is None else now
    with _lock:
        existing = todays_backup(directory, when)
        if existing:
            return existing, False
        if not os.path.isfile(db_path):
            logger.warning("Plex's database {} does not exist, so it cannot be backed up", db_path)
            raise BackupFailed("Plex's database could not be found for the backup.")
        try:
            os.makedirs(directory, mode=0o700, exist_ok=True)
            _check_free_space(db_path, directory)
        except OSError as exc:
            logger.warning("The Plex backup folder {} is not usable: {}", directory, exc)
            raise BackupFailed("The backup folder could not be written.") from exc
        final = os.path.join(directory, f"{_DB_NAME}.{datetime.fromtimestamp(when, UTC).strftime(_STAMP)}.bak")
        partial = os.path.join(directory, f".partial-{uuid.uuid4().hex}")
        try:
            _copy(db_path, partial)
            os.replace(partial, final)
        except OSError as exc:
            logger.warning("Writing the Plex backup {} failed: {}", final, exc)
            raise BackupFailed("The backup folder could not be written.") from exc
        finally:
            _remove_partial(partial)
        _prune(directory, keep, final)
        return final, True
