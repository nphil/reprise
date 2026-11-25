"""ntfy push alerts for Reprise's autopilot: one message per problem, never one per file.

``Alerts`` is only the sender plus its durable de-duplication state; callers decide what is a problem. A caller
asserts a problem with ``problem()`` on every periodic tick: the first call opens it and sends one alert, repeats are
silent, and a never-delivered alert (alerts switched off, or the server was down) is retried at most every ten minutes.
``resolved()`` closes it and sends a plain follow-up, but only if the original alert reached the owner.

State lives in two tables of the app database so it survives restarts. The ntfy token is read from a file in the
config folder at every send; it is never cached, stored or logged, and a send never raises into the caller.
"""

from __future__ import annotations

import os
import threading
import time

import requests
from loguru import logger

NTFY_URL = "https://ntfy.nateshome.net"
NTFY_TOPIC = "homelabber"
TOKEN_FILE = "ntfy-token"  # inside the config folder, i.e. /config/ntfy-token in the container

_RETRY_SECONDS = 600
_TIMEOUT_SECONDS = 10
_TITLE_LIMIT = 60
_MESSAGE_LIMIT = 1000
_LOG_KEEP = 50
_NO_TOKEN = "No ntfy token was found. It belongs in the file ntfy-token in Reprise's config folder."
_REFUSED = "The alert server refused the message (code {})."
_UNREACHABLE = "The alert server could not be reached."
_FIXED = "This is fixed now. Nothing more to do."


def _plain_title(title):
    """A title ntfy can carry in an HTTP header: ASCII only, at most 60 characters."""
    return str(title).encode("ascii", "replace").decode()[:_TITLE_LIMIT]


def _iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


class Alerts:
    """Sends de-duplicated ntfy alerts and remembers which problems are open and which were delivered."""

    def __init__(
        self, store, *, config_dir, enabled=lambda: True, post=None, clock=time.time, url=NTFY_URL, topic=NTFY_TOPIC
    ):
        self.store = store
        self._token_path = os.path.join(config_dir, TOKEN_FILE)
        self._enabled = enabled
        self._post = post or requests.post
        self._clock = clock
        self._endpoint = f"{url.rstrip('/')}/{topic}"
        self._lock = threading.Lock()  # one send at a time, so two ticks cannot both alert the same problem
        with store.transaction() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS reprise_alerts (
                key TEXT PRIMARY KEY, state TEXT NOT NULL, title TEXT NOT NULL, message TEXT NOT NULL,
                opened_at INTEGER NOT NULL, sent_at INTEGER, resolved_at INTEGER,
                failures INTEGER NOT NULL DEFAULT 0, next_try INTEGER NOT NULL DEFAULT 0)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS reprise_alert_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT, at INTEGER NOT NULL, kind TEXT NOT NULL,
                key TEXT NOT NULL, title TEXT NOT NULL)""")

    def problem(self, key, title, message):
        """Assert that a problem exists. Returns True only if an alert was actually sent by this call."""
        title = _plain_title(title)
        with self._lock:
            now = int(self._clock())
            row = self._row(key)
            if row is not None and row["state"] == "open":
                if row["sent_at"] is not None or not self._enabled() or now < row["next_try"]:
                    return False
            else:
                with self.store.transaction() as conn:
                    conn.execute(
                        "INSERT INTO reprise_alerts(key,state,title,message,opened_at) VALUES (?,?,?,?,?) "
                        "ON CONFLICT(key) DO UPDATE SET state='open', title=excluded.title, message=excluded.message, "
                        "opened_at=excluded.opened_at, sent_at=NULL, resolved_at=NULL, failures=0, next_try=0",
                        (key, "open", title, message, now),
                    )
                if not self._enabled():
                    return False
            sent = self._send(title, message, "high", "warning") is None
            with self.store.transaction() as conn:
                if sent:
                    conn.execute(
                        "UPDATE reprise_alerts SET sent_at=?, failures=0, title=?, message=? WHERE key=?",
                        (now, title, message, key),
                    )
                    self._log(conn, now, "problem", key, title)
                else:
                    conn.execute(
                        "UPDATE reprise_alerts SET failures=failures+1, next_try=? WHERE key=?",
                        (now + _RETRY_SECONDS, key),
                    )
            return sent

    def resolved(self, key, message=None):
        """Close an open problem; sends a follow-up only if its alert was delivered. True if one was sent."""
        with self._lock:
            now = int(self._clock())
            row = self._row(key)
            if row is None or row["state"] != "open":
                return False
            with self.store.transaction() as conn:
                conn.execute("UPDATE reprise_alerts SET state='resolved', resolved_at=? WHERE key=?", (now, key))
            if row["sent_at"] is None or not self._enabled():
                return False
            title = _plain_title(f"Resolved: {row['title']}")
            if self._send(title, message or _FIXED, "default", "white_check_mark") is not None:
                return False
            with self.store.transaction() as conn:
                self._log(conn, now, "resolved", key, title)
            return True

    def is_open(self, key):
        """Whether this problem is currently open."""
        row = self._row(key)
        return row is not None and row["state"] == "open"

    def open_keys(self):
        """Keys of all open problems, sorted."""
        with self.store.marker_store._reading() as conn:
            rows = conn.execute("SELECT key FROM reprise_alerts WHERE state='open' ORDER BY key").fetchall()
        return [row["key"] for row in rows]

    def last(self):
        """The most recent alert actually sent (problem, resolved or test), or None."""
        with self.store.marker_store._reading() as conn:
            row = conn.execute("SELECT at,kind,key,title FROM reprise_alert_log ORDER BY id DESC LIMIT 1").fetchone()
        if row is None:
            return None
        return {"kind": row["kind"], "key": row["key"], "title": row["title"], "at": _iso(row["at"])}

    def test(self):
        """Send a test alert even if alerts are switched off, to check the setup. Returns a plain sentence."""
        with self._lock:
            title = "Test alert from Reprise"
            failure = self._send(title, "If you can read this, Reprise alerts work.", "default", "bell")
            if failure is not None:
                return {"sent": False, "message": failure}
            with self.store.transaction() as conn:
                self._log(conn, int(self._clock()), "test", "test", title)
            return {"sent": True, "message": "Test alert sent. Check your phone."}

    def _row(self, key):
        with self.store.marker_store._reading() as conn:
            return conn.execute("SELECT * FROM reprise_alerts WHERE key=?", (key,)).fetchone()

    @staticmethod
    def _log(conn, now, kind, key, title):
        conn.execute("INSERT INTO reprise_alert_log(at,kind,key,title) VALUES (?,?,?,?)", (now, kind, key, title))
        conn.execute(
            "DELETE FROM reprise_alert_log WHERE id NOT IN (SELECT id FROM reprise_alert_log ORDER BY id DESC LIMIT ?)",
            (_LOG_KEEP,),
        )

    def has_token(self):
        """Whether the ntfy token file holds a token (it is read now, never kept)."""
        return bool(self._read_token())

    def notify(self, title, message, *, priority="default", tag=None, actions=None, click=None):
        """Send one message that is no problem alert (a heads-up with buttons) through the same token, server and
        topic. ``actions`` is ntfy's ``Actions`` header value, ``click`` the address a tap on the message opens. No
        de-duplication or bookkeeping happens here: the caller decides what to send and when. Returns None on success,
        else a plain reason for the owner; never raises."""
        extra = {}
        if actions:
            extra["Actions"] = actions
        if click:
            extra["Click"] = click
        with self._lock:
            return self._send(_plain_title(title), message, priority, tag, extra)

    def _read_token(self):
        try:
            with open(self._token_path, encoding="utf-8") as handle:
                return handle.read().strip()
        except (OSError, ValueError):
            return ""

    def _send(self, title, message, priority, tag, extra=None):
        """POST one message. Returns None on success, else a plain reason for the owner; never raises."""
        token = self._read_token()
        if not token:
            logger.warning("ntfy alert not sent: no token file")
            return _NO_TOKEN
        headers = {"Authorization": f"Bearer {token}", "Title": title, "Priority": priority}
        if tag:
            headers["Tags"] = tag
        headers.update(extra or {})
        try:
            response = self._post(
                self._endpoint,
                data=message[:_MESSAGE_LIMIT].encode("utf-8"),
                headers=headers,
                timeout=_TIMEOUT_SECONDS,
            )
            code = response.status_code
            accepted = 200 <= code < 300
        except Exception as exc:  # deliberately broad: a failed alert must never break the caller
            logger.warning("ntfy alert not sent: {}", type(exc).__name__)
            return _UNREACHABLE
        if not accepted:
            logger.warning("ntfy alert not sent: HTTP {}", code)
            return _REFUSED.format(code)
        return None
