"""What Reprise's automatic work remembers between passes, and across restarts: a few small facts in the app database.

Three small tables: loose facts (when it last looked at Plex, which problems it is watching), how often it has tried
the same thing (so a file that keeps failing is not tried every minute), and how much each show was watched (the order
the backfill works in).
"""

from __future__ import annotations

import json


class AutopilotState:
    """Facts, try counts and plays of the automatic work, kept in the app database."""

    def __init__(self, store):
        self.store = store
        with store.transaction() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS reprise_autopilot_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS reprise_autopilot_tries ("
                "kind TEXT NOT NULL, key TEXT NOT NULL, tries INTEGER NOT NULL, at REAL NOT NULL, "
                "PRIMARY KEY (kind, key))"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS reprise_autopilot_plays ("
                "server_id TEXT NOT NULL, library_id TEXT NOT NULL, show_key TEXT NOT NULL, title TEXT NOT NULL, "
                "plays INTEGER NOT NULL, last_viewed INTEGER NOT NULL, PRIMARY KEY (server_id, library_id, show_key))"
            )

    # ── loose facts ───────────────────────────────────────────────────────────────────────────────────────────────

    def get(self, key, default=None):
        with self.store.marker_store._reading() as conn:
            row = conn.execute("SELECT value FROM reprise_autopilot_state WHERE key=?", (key,)).fetchone()
        return default if row is None else json.loads(row["value"])

    def put(self, key, value):
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO reprise_autopilot_state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value, separators=(",", ":"))),
            )

    def drop(self, key):
        with self.store.transaction() as conn:
            conn.execute("DELETE FROM reprise_autopilot_state WHERE key=?", (key,))

    def items(self, prefix):
        """``[(key, value)]`` of every fact whose key starts with ``prefix``."""
        with self.store.marker_store._reading() as conn:
            rows = conn.execute(
                "SELECT key,value FROM reprise_autopilot_state WHERE substr(key,1,?)=? ORDER BY key",
                (len(prefix), prefix),
            ).fetchall()
        return [(row["key"], json.loads(row["value"])) for row in rows]

    # ── how often something was tried ─────────────────────────────────────────────────────────────────────────────

    def all_tries(self, kind):
        """``{key: (tries, last try)}`` for one kind of thing that is tried."""
        with self.store.marker_store._reading() as conn:
            rows = conn.execute("SELECT key,tries,at FROM reprise_autopilot_tries WHERE kind=?", (kind,)).fetchall()
        return {row["key"]: (row["tries"], row["at"]) for row in rows}

    def bump(self, kind, key, now):
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO reprise_autopilot_tries VALUES (?,?,1,?) "
                "ON CONFLICT(kind,key) DO UPDATE SET tries=tries+1, at=excluded.at",
                (kind, key, now),
            )

    def prune_tries(self, before):
        """Forget tries older than ``before``: what they were about has long since either worked or been given up."""
        with self.store.transaction() as conn:
            conn.execute("DELETE FROM reprise_autopilot_tries WHERE at<?", (before,))

    # ── how much each show was watched ────────────────────────────────────────────────────────────────────────────

    def set_plays(self, server_id, library_id, shows):
        """Replace what is known of one library: ``shows`` is ``{show key: (title, plays, last viewed)}``."""
        with self.store.transaction() as conn:
            conn.execute(
                "DELETE FROM reprise_autopilot_plays WHERE server_id=? AND library_id=?", (server_id, library_id)
            )
            conn.executemany(
                "INSERT INTO reprise_autopilot_plays VALUES (?,?,?,?,?,?)",
                [
                    (server_id, library_id, key, title, plays, viewed)
                    for key, (title, plays, viewed) in shows.items()
                    if plays > 0
                ],
            )

    def plays(self):
        """``{(server id, library id, show key): (plays, last viewed)}`` for every show that was watched."""
        with self.store.marker_store._reading() as conn:
            rows = conn.execute("SELECT server_id,library_id,show_key,plays,last_viewed FROM reprise_autopilot_plays")
            return {
                (row["server_id"], row["library_id"], row["show_key"]): (row["plays"], row["last_viewed"])
                for row in rows.fetchall()
            }


class Concerns:
    """The problems Reprise is watching. A problem is raised once and then kept in the database until it is resolved;
    every pass asserts the open ones again, which is how an alert that could not be delivered is retried."""

    _PREFIX = "concern:"

    def __init__(self, alerts, state):
        self.alerts, self.state = alerts, state

    def raise_problem(self, key, title, message):
        """Open (or update the text of) a problem; the owner is alerted once, however often this is called."""
        if self.state.get(self._PREFIX + key) != [title, message]:
            self.state.put(self._PREFIX + key, [title, message])
        self.alerts.problem(key, title, message)

    def resolve(self, key, message=None):
        """Close a problem and tell the owner it is fixed (only if they were told about it). Nothing happens when the
        problem is not open."""
        if self.state.get(self._PREFIX + key) is None:
            return
        self.state.drop(self._PREFIX + key)
        self.alerts.resolved(key, message)

    def is_open(self, key):
        return self.state.get(self._PREFIX + key) is not None

    def open_keys(self, prefix=""):
        return [key[len(self._PREFIX) :] for key, _ in self.state.items(self._PREFIX + prefix)]

    def refresh(self):
        """Assert every open problem again."""
        for key, (title, message) in self.state.items(self._PREFIX):
            self.alerts.problem(key[len(self._PREFIX) :], title, message)
