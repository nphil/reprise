"""Per-frame scan hints, kept per file generation so a season is not decoded once per neighbour.

Comparing an episode with its three nearest neighbours scans a window of each neighbour (a minute and a half of frames:
decode, ffprobe audit, descriptor and spatial information for every frame), and the same file is a neighbour of three
or four other episodes whose windows overlap its own. This keeps what a scan produced, the hints ``analysis._scan``
returns, and answers a later scan from it when the stored frames cover the window.

It is exact, not approximate: a hint is a pure function of one decoded frame, so the frames of a sub-window are the
stored frames whose timestamps fall in it (``tests/reprise/test_scan_cache.py`` checks this against real decodes), and a
cache hit is still followed by the generation check every scan ends with. An entry belongs to one file generation
(``MediaVersion.id``: path, size, modification time, duration and stream layout), one video stream and one
verification-profile fingerprint; a different generation of the same path replaces it, a profile change drops it, and
the whole cache stays under ``max_bytes`` (least recently used entries go first). Any storage error turns the cache off
for the process: the scan then runs as it always did.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from fractions import Fraction

import numpy as np
from loguru import logger

from .verification_profile import PROFILE_FINGERPRINT

FORMAT = 1  # the serialisation below
DEFAULT_MAX_BYTES = 256 * 1024 * 1024
DESCRIPTOR_SIZE = 48  # 3 planes x 4 x 4 tiles

_INT64 = 1 << 63


def _encode(value):
    if isinstance(value, Fraction):
        return {"f": [value.numerator, value.denominator]}
    if isinstance(value, tuple | list):
        return [_encode(v) for v in value]
    return value


def _decode(value):
    if isinstance(value, dict):
        return Fraction(*value["f"])
    if isinstance(value, list):
        return tuple(_decode(v) for v in value)
    return value


class ScanCache:
    """A SQLite file of scan results; safe to share between the analysis threads."""

    def __init__(self, path, max_bytes=DEFAULT_MAX_BYTES):
        self.path, self.max_bytes = path, max_bytes
        self._lock = threading.Lock()
        self._db = None
        self._failed = False
        self.hits = self.misses = 0

    # -- storage ----------------------------------------------------------------------------------------------------

    def _connection(self):
        if self._db is None:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            db = sqlite3.connect(self.path, check_same_thread=False)
            db.execute(
                "CREATE TABLE IF NOT EXISTS scans (version_id TEXT NOT NULL, stream INTEGER NOT NULL,"
                " fingerprint TEXT NOT NULL, format INTEGER NOT NULL, path TEXT NOT NULL, lo INTEGER NOT NULL,"
                " hi INTEGER NOT NULL, declarations TEXT NOT NULL, unspecified TEXT NOT NULL, frames BLOB NOT NULL,"
                " used REAL NOT NULL, PRIMARY KEY (version_id, stream, fingerprint, format, lo))"
            )
            # What no current run could use: another profile's hints, another serialisation.
            db.execute("DELETE FROM scans WHERE fingerprint != ? OR format != ?", (PROFILE_FINGERPRINT, FORMAT))
            db.commit()
            self._db = db
        return self._db

    def _fail(self, exc):
        if not self._failed:
            logger.warning("Reprise scan cache turned off: {}", exc)
        self._failed = True

    # -- API --------------------------------------------------------------------------------------------------------

    def lookup(self, item, stream, start, end):
        """(hints, declarations, unspecified) exactly as a scan of [start, end) returns them, or None."""
        if self._failed:
            return None
        key = (item.id, stream, PROFILE_FINGERPRINT, FORMAT)
        try:
            with self._lock:
                db = self._connection()
                row = db.execute(
                    "SELECT lo, hi, declarations, unspecified, frames FROM scans WHERE version_id=? AND stream=?"
                    " AND fingerprint=? AND format=? AND lo<=? AND hi>=? ORDER BY hi - lo LIMIT 1",
                    (*key, start, end),
                ).fetchone()
                if row is None:
                    self.misses += 1
                    return None
                db.execute(
                    "UPDATE scans SET used=? WHERE version_id=? AND stream=? AND fingerprint=? AND format=? AND lo=?",
                    (time.time(), *key, row[0]),
                )
                db.commit()
            hints = _frames(row[4], start, end)
            self.hits += 1
            return hints, _decode(json.loads(row[2])), list(json.loads(row[3]))
        except (sqlite3.Error, OSError, ValueError, KeyError) as exc:
            self._fail(exc)
            return None

    def store(self, item, stream, start, end, hints, declarations, unspecified):
        """Keep the result of a completed scan of [start, end); overlapping or touching entries of the same generation
        and declarations merge into one."""
        if self._failed or declarations is None or any(not start <= h[0] < end for h in hints):
            return  # nothing to keep, or a frame outside the window: not a result the window's slice could reproduce
        try:
            key = (item.id, stream, PROFILE_FINGERPRINT, FORMAT)
            declared = json.dumps(_encode(declarations))
            with self._lock:
                db = self._connection()
                # Another generation of this file is stale for good.
                db.execute("DELETE FROM scans WHERE path=? AND version_id != ?", (item.canonical_path, item.id))
                lo, hi, frames = start, end, hints
                for row in db.execute(
                    "SELECT lo, hi, frames FROM scans WHERE version_id=? AND stream=? AND fingerprint=? AND format=?"
                    " AND declarations=? AND lo<=? AND hi>=?",
                    (*key, declared, end, start),
                ).fetchall():
                    # The stored frames of an overlapping entry are the scan's frames there: the union is exact.
                    known = {h[0] for h in frames}
                    frames = sorted(
                        [*frames, *(h for h in _frames(row[2], row[0], row[1]) if h[0] not in known)],
                        key=lambda h: h[0],
                    )
                    lo, hi = min(lo, row[0]), max(hi, row[1])
                    db.execute(
                        "DELETE FROM scans WHERE version_id=? AND stream=? AND fingerprint=? AND format=? AND lo=?",
                        (*key, row[0]),
                    )
                block = _pack(frames)
                if block is None:
                    db.rollback()
                    return
                db.execute(
                    "INSERT OR REPLACE INTO scans VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (*key, item.canonical_path, lo, hi, declared, json.dumps(list(unspecified)), block, time.time()),
                )
                self._trim(db)
                db.commit()
        except (sqlite3.Error, OSError, ValueError, OverflowError) as exc:
            self._fail(exc)

    def _trim(self, db):
        total = db.execute("SELECT COALESCE(SUM(LENGTH(frames)), 0) FROM scans").fetchone()[0]
        if total <= self.max_bytes:
            return
        # Least recently used first, down to three quarters of the bound so a full cache is not trimmed every scan.
        for rowid, size in db.execute("SELECT rowid, LENGTH(frames) FROM scans ORDER BY used").fetchall():
            if total <= self.max_bytes * 0.75:
                break
            db.execute("DELETE FROM scans WHERE rowid=?", (rowid,))
            total -= size

    def size(self):
        with self._lock:
            return self._connection().execute("SELECT COALESCE(SUM(LENGTH(frames)), 0) FROM scans").fetchone()[0]

    def clear(self):
        with self._lock:
            db = self._connection()
            db.execute("DELETE FROM scans")
            db.commit()

    def close(self):
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None


def _pack(hints):
    """The hints as one blob: n, then timestamps (numerator, denominator int64), descriptors float32, std float64."""
    if any(abs(h[0].numerator) >= _INT64 or h[0].denominator >= _INT64 for h in hints):
        return None
    count = len(hints)
    times = np.array([(h[0].numerator, h[0].denominator) for h in hints], dtype=np.int64).reshape(count, 2)
    descriptors = np.array([h[1] for h in hints], dtype=np.float32).reshape(count, DESCRIPTOR_SIZE)
    spread = np.array([h[2] for h in hints], dtype=np.float64)
    return np.int64(count).tobytes() + times.tobytes() + descriptors.tobytes() + spread.tobytes()


def _frames(blob, start, end):
    """The hints of a packed blob whose timestamp is in [start, end)."""
    count = int(np.frombuffer(blob, dtype=np.int64, count=1)[0])
    at = 8
    times = np.frombuffer(blob, dtype=np.int64, count=2 * count, offset=at).reshape(count, 2)
    at += times.nbytes
    descriptors = np.frombuffer(blob, dtype=np.float32, count=count * DESCRIPTOR_SIZE, offset=at).reshape(
        count, DESCRIPTOR_SIZE
    )
    at += descriptors.nbytes
    spread = np.frombuffer(blob, dtype=np.float64, count=count, offset=at)
    hints = []
    for i in range(count):
        pts = Fraction(int(times[i, 0]), int(times[i, 1]))
        if start <= pts < end:
            hints.append((pts, descriptors[i].copy(), float(spread[i])))
    return hints


# The cache the analysis uses: unset (no caching) until the service gives it a place to live.
cache: ScanCache | None = None


def configure(path, max_bytes=DEFAULT_MAX_BYTES):
    """Use (or stop using, with None) a cache file; the same path keeps the cache already open."""
    global cache
    if path is None:
        cache = None
    elif cache is None or cache.path != path or cache.max_bytes != max_bytes:
        cache = ScanCache(path, max_bytes)
