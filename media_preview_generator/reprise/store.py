"""Reprise data in the existing MarkerStore database and transaction lifecycle."""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from dataclasses import asdict, replace
from datetime import datetime

from .admission import pair_provenance
from .identity import is_current
from .models import (
    SUGGESTION_ORIGINS,
    AnalysisResult,
    ClientPrincipal,
    DecisionRange,
    Evidence,
    MediaVersion,
    PlaybackIdentity,
    ReviewRefused,
)
from .verification_profile import POLICY_VERSION, is_current_profile

DEFAULT_SCOPES = ("results:read", "jobs:read")
ALLOWED_SCOPES = frozenset((*DEFAULT_SCOPES, "analysis:request"))
# Review keys, not stored: the client's deliberate "yes" to approving a range someone suggested (Plex, or an online
# database) and Reprise did not confirm. ``acknowledge_plex_suggestion`` is the key from before online suggestions
# existed and is still accepted for any suggestion.
SUGGESTION_ACKNOWLEDGMENT = "acknowledge_suggestion"
LEGACY_PLEX_ACKNOWLEDGMENT = "acknowledge_plex_suggestion"
NEEDS_ACKNOWLEDGMENT = (
    "A suggested range needs a deliberate approval: send acknowledge_suggestion: true with that range."
)


class RevisionConflict(Exception):
    pass


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _id():
    return uuid.uuid4().hex


def _generation(version_data):
    """What makes a version the version its ID names: everything but the change time, device number and inode,
    which move without the content (a permissions or mover pass, a reboot of the user-share mount)."""
    return {key: value for key, value in version_data.items() if key not in ("ctime_ns", "device", "inode")}


def _range(data):
    data = dict(data)
    data["evidence_ids"] = tuple(data["evidence_ids"])
    data["audio_stream_ids"] = tuple(data["audio_stream_ids"])
    data["suggested_by"] = tuple(data.get("suggested_by", ()))
    return DecisionRange(**data)


def _owner_decided(r, override_ids):
    """Whether a range is the owner's: made or locked by them, overridden by them, or a keep they have reviewed.
    A new analysis preserves these ranges."""
    return (
        r.origin == "manual"
        or r.locked
        or r.id in override_ids
        or (r.action == "keep" and r.review_status != "proposed")
    )


def _validate_range(r, duration):
    if (
        r.kind not in ("intro", "credits", "recap", "preview")
        or r.action not in ("skip", "keep")
        or r.review_status not in ("proposed", "approved", "rejected")
        or r.origin not in ("manual", "detector", *SUGGESTION_ORIGINS)
        or type(r.start_ms) is not int
        or type(r.end_ms) is not int
        or not 0 <= r.start_ms < r.end_ms <= duration
        or type(r.locked) is not bool
        or type(r.terminal) is not bool
    ):
        raise ValueError("invalid decision range")


class RepriseStore:
    def __init__(self, existing_marker_store):
        self.marker_store = existing_marker_store
        with self.transaction() as c:
            for sql in (
                "CREATE TABLE IF NOT EXISTS reprise_media_versions (id TEXT PRIMARY KEY, data TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS reprise_bindings (machine TEXT NOT NULL, rating_key TEXT NOT NULL, media_id TEXT NOT NULL, part_id TEXT NOT NULL, audio_id TEXT NOT NULL, version_id TEXT NOT NULL REFERENCES reprise_media_versions(id), server_id TEXT NOT NULL, identity TEXT NOT NULL, metadata TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(machine,rating_key,media_id,part_id,audio_id,version_id))",
                'CREATE TABLE IF NOT EXISTS reprise_analysis_runs (id TEXT PRIMARY KEY, version_id TEXT NOT NULL, job_id TEXT NOT NULL, signature TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL DEFAULT "")',
                "CREATE TABLE IF NOT EXISTS reprise_decision_results (version_id TEXT PRIMARY KEY, analysis_id TEXT NOT NULL, revision INTEGER NOT NULL, signature TEXT NOT NULL, policy_version TEXT NOT NULL, status TEXT NOT NULL, reasons TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS reprise_evidence_windows (version_id TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(version_id,id))",
                "CREATE TABLE IF NOT EXISTS reprise_ranges (version_id TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(version_id,id))",
                "CREATE TABLE IF NOT EXISTS reprise_analysis_proposals (version_id TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(version_id,id))",
                "CREATE TABLE IF NOT EXISTS reprise_manual_overrides (version_id TEXT NOT NULL, range_id TEXT NOT NULL, data TEXT NOT NULL, actor TEXT NOT NULL, quarantined INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(version_id,range_id))",
                "CREATE TABLE IF NOT EXISTS reprise_api_clients (id TEXT PRIMARY KEY, name TEXT NOT NULL, scopes TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE, revoked INTEGER NOT NULL DEFAULT 0)",
                "CREATE TABLE IF NOT EXISTS reprise_online_answers (source TEXT NOT NULL, lookup_key TEXT NOT NULL, status TEXT NOT NULL, candidates TEXT NOT NULL, parser_version INTEGER NOT NULL, fetched_at TEXT NOT NULL, PRIMARY KEY(source,lookup_key))",
            ):
                c.execute(sql)
            # Every status lookup asks for one version's bindings. Without this the answer was a scan of every binding
            # (about 40 ms at 4,000 episodes), which made a 100-row Library page cost seconds once rows had results.
            c.execute("CREATE INDEX IF NOT EXISTS reprise_bindings_version ON reprise_bindings(version_id, active)")

    def transaction(self):
        return self.marker_store._tx()

    @staticmethod
    def _invalidate_binding_change(c, version_id):
        affected = {version_id}
        affected.update(
            row["version_id"]
            for row in c.execute("SELECT version_id,data FROM reprise_evidence_windows")
            if json.loads(row["data"]).get("reference_version_id") == version_id
        )
        for affected_id in affected:
            if not c.execute("SELECT 1 FROM reprise_decision_results WHERE version_id=?", (affected_id,)).fetchone():
                continue
            c.execute("UPDATE reprise_manual_overrides SET quarantined=1 WHERE version_id=?", (affected_id,))
            for row in c.execute("SELECT id,data FROM reprise_ranges WHERE version_id=?", (affected_id,)).fetchall():
                item = _range(json.loads(row["data"]))
                if item.action == "skip" and item.review_status == "approved":
                    c.execute(
                        "UPDATE reprise_ranges SET data=? WHERE version_id=? AND id=?",
                        (_json(asdict(replace(item, review_status="proposed"))), affected_id, item.id),
                    )
            c.execute(
                "UPDATE reprise_decision_results SET policy_version=?,revision=revision+1 WHERE version_id=?",
                ("binding-changed", affected_id),
            )

    def register_media(
        self, version: MediaVersion, identity: PlaybackIdentity, *, server_id: str, metadata: dict | None = None
    ) -> MediaVersion:
        if (
            not all(
                (
                    version.id,
                    version.canonical_path,
                    version.stream_signature,
                    server_id,
                    identity.plex_machine_identifier,
                    identity.rating_key,
                    identity.media_id,
                    identity.part_id,
                    identity.part_key,
                )
            )
            or identity.part_size_bytes != version.size_bytes
            or identity.duration_ms != version.duration_ms
        ):
            raise ValueError("incomplete or inconsistent binding")
        binding = (identity.plex_machine_identifier, identity.rating_key, identity.media_id, identity.part_id)
        with self.transaction() as c:
            old = c.execute("SELECT data FROM reprise_media_versions WHERE id=?", (version.id,)).fetchone()
            if old and _generation(json.loads(old["data"])) != _generation(asdict(version)):
                raise ValueError("media version IDs are immutable")
            c.execute("INSERT OR IGNORE INTO reprise_media_versions VALUES (?,?)", (version.id, _json(asdict(version))))
            prior = c.execute(
                "SELECT metadata,identity,active,server_id FROM reprise_bindings WHERE machine=? AND rating_key=? AND media_id=? AND part_id=? AND audio_id=? AND version_id=?",
                (*binding, identity.audio_stream_id, version.id),
            ).fetchone()
            if old and (
                not prior
                or not prior["active"]
                or prior["server_id"] != server_id
                or prior["identity"] != _json(asdict(identity))
                or prior["metadata"] != _json(metadata or {})
            ):
                self._invalidate_binding_change(c, version.id)
            keys = ("remote_part_path", "mapping_fingerprint")
            if prior and (
                not prior["active"]
                or prior["server_id"] != server_id
                or any(json.loads(prior["metadata"]).get(k) != (metadata or {}).get(k) for k in keys)
            ):
                c.execute("UPDATE reprise_manual_overrides SET quarantined=1 WHERE version_id=?", (version.id,))
                for row in c.execute("SELECT id,data FROM reprise_ranges WHERE version_id=?", (version.id,)).fetchall():
                    item = _range(json.loads(row["data"]))
                    if item.action == "skip" and item.review_status == "approved":
                        c.execute(
                            "UPDATE reprise_ranges SET data=? WHERE version_id=? AND id=?",
                            (_json(asdict(replace(item, review_status="proposed"))), version.id, item.id),
                        )
                c.execute(
                    "UPDATE reprise_decision_results SET status=?,revision=revision+1,reasons=? WHERE version_id=?",
                    ("failed", _json(["Binding changed; reanalysis required"]), version.id),
                )
            old_ids = [
                r["version_id"]
                for r in c.execute(
                    "SELECT version_id FROM reprise_bindings WHERE machine=? AND rating_key=? AND media_id=? AND part_id=? AND active=1 AND version_id<>?",
                    (*binding, version.id),
                )
            ]
            for old_id in old_ids:
                self._invalidate_binding_change(c, old_id)
                c.execute("UPDATE reprise_manual_overrides SET quarantined=1 WHERE version_id=?", (old_id,))
            c.execute(
                "UPDATE reprise_bindings SET active=0 WHERE machine=? AND rating_key=? AND media_id=? AND part_id=? AND version_id<>?",
                (*binding, version.id),
            )
            c.execute(
                "INSERT INTO reprise_bindings VALUES (?,?,?,?,?,?,?,?,?,1) ON CONFLICT(machine,rating_key,media_id,part_id,audio_id,version_id) DO UPDATE SET server_id=excluded.server_id,identity=excluded.identity,metadata=excluded.metadata,active=1",
                (
                    *binding,
                    identity.audio_stream_id,
                    version.id,
                    server_id,
                    _json(asdict(identity)),
                    _json(metadata or {}),
                ),
            )
        return version

    def reconcile_library(self, server_id, library_id, seen_identities):
        """Deactivate absent bindings only after a complete authoritative scan."""
        seen = {_json(asdict(identity)) for identity in seen_identities}
        with self.transaction() as c:
            rows = c.execute(
                "SELECT rowid,version_id,identity,metadata FROM reprise_bindings WHERE server_id=? AND active=1",
                (server_id,),
            ).fetchall()
            removed = 0
            for row in rows:
                if (
                    str(json.loads(row["metadata"]).get("library_id")) == str(library_id)
                    and row["identity"] not in seen
                ):
                    c.execute("UPDATE reprise_bindings SET active=0 WHERE rowid=?", (row["rowid"],))
                    self._invalidate_binding_change(c, row["version_id"])
                    removed += 1
            return removed

    def get_media(self, media_version_id):
        with self.marker_store._reading() as c:
            row = c.execute("SELECT data FROM reprise_media_versions WHERE id=?", (media_version_id,)).fetchone()
        return MediaVersion(**json.loads(row["data"])) if row else None

    def get_bindings(self, media_version_id):
        with self.marker_store._reading() as c:
            return self._bindings_in_transaction(c, media_version_id)

    @staticmethod
    def _bindings_in_transaction(c, media_version_id):
        rows = c.execute(
            "SELECT identity,server_id,metadata FROM reprise_bindings WHERE version_id=? AND active=1",
            (media_version_id,),
        ).fetchall()
        return [
            (PlaybackIdentity(**json.loads(r["identity"])), r["server_id"], json.loads(r["metadata"])) for r in rows
        ]

    def get_binding(self, media_version_id):
        rows = self.get_bindings(media_version_id)
        return rows[0] if len(rows) == 1 else None

    def resolve_binding(self, identity: PlaybackIdentity):
        with self.marker_store._reading() as c:
            rows = c.execute(
                "SELECT b.identity,v.data FROM reprise_bindings b JOIN reprise_media_versions v ON v.id=b.version_id WHERE b.machine=? AND b.rating_key=? AND b.media_id=? AND b.part_id=? AND b.active=1",
                (identity.plex_machine_identifier, identity.rating_key, identity.media_id, identity.part_id),
            ).fetchall()
        matches = []
        for row in rows:
            saved = PlaybackIdentity(**json.loads(row["identity"]))
            if (saved.part_key, saved.part_size_bytes, saved.duration_ms, saved.audio_stream_id) == (
                identity.part_key,
                identity.part_size_bytes,
                identity.duration_ms,
                identity.audio_stream_id,
            ):
                matches.append(MediaVersion(**json.loads(row["data"])))
        return matches[0] if len(matches) == 1 else None

    def catalogue(self, server_id=None):
        """Every active binding's version id, server and Plex metadata in one pass, for Library filtering.

        Unlike ``list_media`` it never reads the media-version or identity documents, so filtering the whole
        catalogue costs one query and one metadata parse per row instead of three document parses."""
        with self.marker_store._reading() as c:
            rows = c.execute(
                "SELECT version_id,server_id,metadata FROM reprise_bindings WHERE active=1 AND (? IS NULL OR server_id=?) ORDER BY rowid",
                (server_id, server_id),
            ).fetchall()
        return [
            {"version_id": r["version_id"], "server_id": r["server_id"], "metadata": json.loads(r["metadata"])}
            for r in rows
        ]

    def list_media(self, server_id=None, cursor=None, limit=50):
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("invalid page size")
        with self.marker_store._reading() as c:
            rows = c.execute(
                "SELECT b.rowid,b.identity,b.server_id,b.metadata,v.data FROM reprise_bindings b JOIN reprise_media_versions v ON b.version_id=v.id WHERE b.active=1 AND b.rowid>? AND (? IS NULL OR b.server_id=?) ORDER BY b.rowid LIMIT ?",
                (int(cursor or 0), server_id, server_id, limit + 1),
            ).fetchall()
        return {
            "items": [
                {
                    "media_version": json.loads(r["data"]),
                    "identity": json.loads(r["identity"]),
                    "server_id": r["server_id"],
                    "metadata": json.loads(r["metadata"]),
                }
                for r in rows[:limit]
            ],
            "next_cursor": str(rows[limit - 1]["rowid"]) if len(rows) > limit else None,
        }

    def _result(self, c, media_version_id):
        r = c.execute("SELECT * FROM reprise_decision_results WHERE version_id=?", (media_version_id,)).fetchone()
        if not r:
            return None
        evidence = tuple(
            Evidence(**json.loads(x["data"]))
            for x in c.execute(
                "SELECT data FROM reprise_evidence_windows WHERE version_id=? ORDER BY id", (media_version_id,)
            )
        )
        ranges = tuple(
            _range(json.loads(x["data"]))
            for x in c.execute("SELECT data FROM reprise_ranges WHERE version_id=? ORDER BY id", (media_version_id,))
        )
        v = c.execute("SELECT data FROM reprise_media_versions WHERE id=?", (media_version_id,)).fetchone()
        freshness = (
            "current"
            if r["policy_version"] == POLICY_VERSION
            and all(is_current_profile(entry.metrics) for entry in evidence)
            and v
            and is_current(MediaVersion(**json.loads(v["data"])))
            and c.execute(
                "SELECT 1 FROM reprise_bindings WHERE version_id=? AND active=1", (media_version_id,)
            ).fetchone()
            else "stale"
        )
        if freshness == "current":
            target = MediaVersion(**json.loads(v["data"]))
            target_bindings = self._bindings_in_transaction(c, media_version_id)
            for entry in evidence:
                if entry.verdict.lower() != "match":
                    continue
                reference_id = entry.reference_version_id
                ref = c.execute(
                    "SELECT data FROM reprise_media_versions WHERE id=? AND EXISTS (SELECT 1 FROM reprise_bindings WHERE version_id=? AND active=1)",
                    (reference_id, reference_id),
                ).fetchone()
                snapshot = (
                    pair_provenance(
                        target,
                        target_bindings,
                        MediaVersion(**json.loads(ref["data"])),
                        self._bindings_in_transaction(c, reference_id),
                    )
                    if ref
                    else None
                )
                if not snapshot or entry.metrics.get("admission_provenance") != snapshot:
                    freshness = "stale"
                    break
        return AnalysisResult(
            media_version_id,
            r["analysis_id"],
            r["revision"],
            r["status"],
            freshness,
            r["signature"],
            r["policy_version"],
            ranges,
            evidence,
            tuple(json.loads(r["reasons"])),
        )

    def get_result(self, media_version_id):
        with self.marker_store._reading(snapshot=True) as c:
            return self._result(c, media_version_id)

    @staticmethod
    def _override_ids(c, media_version_id):
        return {
            row["range_id"]
            for row in c.execute(
                "SELECT range_id FROM reprise_manual_overrides WHERE version_id=? AND quarantined=0",
                (media_version_id,),
            )
        }

    def owner_keep_ranges(self, media_version_id):
        """The (start, end) of every range the owner keeps: added or locked by them, or a keep they reviewed. A new
        analysis preserves these, so it must not fill a gap across one."""
        with self.marker_store._reading(snapshot=True) as c:
            result = self._result(c, media_version_id)
            overrides = self._override_ids(c, media_version_id)
        return tuple(
            (r.start_ms, r.end_ms)
            for r in (result.ranges if result else ())
            if r.action == "keep" and _owner_decided(r, overrides)
        )

    def online_answer(self, source, lookup_key):
        """What an online source answered for one episode, as ``online.OnlineLookups`` stored it; None when never asked."""
        with self.marker_store._reading(snapshot=True) as c:
            row = c.execute(
                "SELECT status,candidates,parser_version,fetched_at FROM reprise_online_answers WHERE source=? AND lookup_key=?",
                (source, lookup_key),
            ).fetchone()
        if row is None:
            return None
        return {
            "status": row["status"],
            "candidates": row["candidates"],
            "parser_version": row["parser_version"],
            "fetched_at": datetime.fromisoformat(row["fetched_at"]),
        }

    def save_online_answer(self, source, lookup_key, status, candidates, parser_version, fetched_at):
        """Remember an online source's answer ("ok", or "no_data" for no entry) for one episode."""
        with self.transaction() as c:
            c.execute(
                "INSERT INTO reprise_online_answers VALUES (?,?,?,?,?,?) ON CONFLICT(source,lookup_key) DO UPDATE SET "
                "status=excluded.status,candidates=excluded.candidates,parser_version=excluded.parser_version,"
                "fetched_at=excluded.fetched_at",
                (source, lookup_key, status, candidates, parser_version, fetched_at.isoformat()),
            )

    def begin_analysis(self, media_version_id, job_id, signature):
        if not signature or not job_id:
            raise ValueError("job and signature required")
        run = _id()
        with self.transaction() as c:
            if not c.execute(
                "SELECT 1 FROM reprise_bindings WHERE version_id=? AND active=1", (media_version_id,)
            ).fetchone():
                raise KeyError(media_version_id)
            c.execute(
                "INSERT INTO reprise_analysis_runs(id,version_id,job_id,signature,status) VALUES (?,?,?,?,?)",
                (run, media_version_id, job_id, signature, "running"),
            )
            previous = self._result(c, media_version_id)
            if previous:
                c.execute(
                    "UPDATE reprise_decision_results SET status=?,analysis_id=? WHERE version_id=?",
                    ("running", run, media_version_id),
                )
            else:
                c.execute(
                    "INSERT INTO reprise_decision_results VALUES (?,?,?,?,?,?,?)",
                    (media_version_id, run, 0, signature, POLICY_VERSION, "running", "[]"),
                )
        return run

    def finish_analysis(self, analysis_id, evidence, ranges, expected_media_version, reasons=()):
        evidence, ranges, reasons = tuple(evidence), tuple(ranges), list(reasons)
        with self.transaction() as c:
            run = c.execute("SELECT * FROM reprise_analysis_runs WHERE id=?", (analysis_id,)).fetchone()
            if not run or run["status"] != "running" or run["version_id"] != expected_media_version:
                raise RevisionConflict("analysis is no longer current")
            version = c.execute(
                "SELECT data FROM reprise_media_versions WHERE id=?", (expected_media_version,)
            ).fetchone()
            if (
                not version
                or not is_current(MediaVersion(**json.loads(version["data"])))
                or not c.execute(
                    "SELECT 1 FROM reprise_bindings WHERE version_id=? AND active=1", (expected_media_version,)
                ).fetchone()
            ):
                raise RevisionConflict("media generation changed")
            duration = json.loads(version["data"])["duration_ms"]
            if len({e.id for e in evidence}) != len(evidence) or len({r.id for r in ranges}) != len(ranges):
                raise ValueError("duplicate evidence or range ID")
            for e in evidence:
                if not is_current_profile(e.metrics):
                    raise ValueError("evidence verification profile is not current")
                if (
                    type(e.start_ms) is not int
                    or type(e.end_ms) is not int
                    or not 0 <= e.start_ms < e.end_ms <= duration
                ):
                    raise ValueError("invalid evidence range")
            for r in ranges:
                _validate_range(r, duration)
                if r.review_status != "proposed" or r.origin not in ("detector", *SUGGESTION_ORIGINS) or r.locked:
                    raise ValueError("analysis produces proposals only")
            previous = self._result(c, expected_media_version)
            if not previous or previous.analysis_id != analysis_id or previous.analysis_status != "running":
                raise RevisionConflict("analysis superseded")

            def semantic_evidence(items):
                return sorted(_json({k: v for k, v in asdict(e).items() if k != "id"}) for e in items)

            def semantic_ranges(items):
                return sorted(
                    _json(
                        {
                            k: v
                            for k, v in asdict(r).items()
                            if k not in ("id", "evidence_ids", "review_status", "locked")
                        }
                    )
                    for r in items
                    if r.origin in ("detector", *SUGGESTION_ORIGINS)
                )

            baseline = tuple(
                _range(json.loads(row["data"]))
                for row in c.execute(
                    "SELECT data FROM reprise_analysis_proposals WHERE version_id=?", (expected_media_version,)
                )
            )
            same = (
                previous
                and previous.result_revision > 0
                and previous.policy_version == POLICY_VERSION
                and previous.freshness == "current"
                and previous.evidence_signature == run["signature"]
                and semantic_evidence(previous.evidence) == semantic_evidence(evidence)
                and semantic_ranges(baseline) == semantic_ranges(ranges)
            )
            if same:
                c.execute(
                    "UPDATE reprise_decision_results SET status=?,analysis_id=?,reasons=? WHERE version_id=?",
                    ("complete", analysis_id, _json(reasons), expected_media_version),
                )
            else:
                override_ids = self._override_ids(c, expected_media_version)
                preserved = [r for r in (previous.ranges if previous else ()) if _owner_decided(r, override_ids)]
                preserved = [replace(r, review_status="proposed") if r.action == "skip" else r for r in preserved]
                ids = {r.id for r in preserved}
                combined = preserved + [r for r in ranges if r.id not in ids]
                c.execute("DELETE FROM reprise_evidence_windows WHERE version_id=?", (expected_media_version,))
                c.execute("DELETE FROM reprise_ranges WHERE version_id=?", (expected_media_version,))
                c.execute("DELETE FROM reprise_analysis_proposals WHERE version_id=?", (expected_media_version,))
                for proposal in ranges:
                    c.execute(
                        "INSERT INTO reprise_analysis_proposals VALUES (?,?,?)",
                        (expected_media_version, proposal.id, _json(asdict(proposal))),
                    )
                for e in evidence:
                    c.execute(
                        "INSERT INTO reprise_evidence_windows VALUES (?,?,?)",
                        (expected_media_version, e.id, _json(asdict(e))),
                    )
                for r in combined:
                    c.execute(
                        "INSERT INTO reprise_ranges VALUES (?,?,?)", (expected_media_version, r.id, _json(asdict(r)))
                    )
                revision = (previous.result_revision if previous else 0) + 1
                c.execute(
                    "UPDATE reprise_decision_results SET analysis_id=?,revision=?,signature=?,policy_version=?,status=?,reasons=? WHERE version_id=?",
                    (
                        analysis_id,
                        revision,
                        run["signature"],
                        POLICY_VERSION,
                        "complete",
                        _json(reasons),
                        expected_media_version,
                    ),
                )
            c.execute("UPDATE reprise_analysis_runs SET status=? WHERE id=?", ("complete", analysis_id))
            return self._result(c, expected_media_version)

    def fail_analysis(self, analysis_id, status, reason):
        """Record why an analysis ended without a result: one sentence, or a headline followed by its details."""
        if status not in ("failed", "cancelled", "unsupported"):
            raise ValueError("invalid failure status")
        reasons = [reason] if isinstance(reason, str) else list(reason)
        with self.transaction() as c:
            run = c.execute("SELECT * FROM reprise_analysis_runs WHERE id=?", (analysis_id,)).fetchone()
            if not run or run["status"] != "running":
                raise RevisionConflict("analysis is no longer running")
            c.execute(
                "UPDATE reprise_analysis_runs SET status=?,reason=? WHERE id=?",
                (status, " | ".join(reasons), analysis_id),
            )
            current = self._result(c, run["version_id"])
            if current and current.analysis_id == analysis_id:
                for item in current.ranges:
                    if item.action == "skip" and item.review_status == "approved":
                        c.execute(
                            "UPDATE reprise_ranges SET data=? WHERE version_id=? AND id=?",
                            (_json(asdict(replace(item, review_status="proposed"))), run["version_id"], item.id),
                        )
                c.execute(
                    "UPDATE reprise_decision_results SET status=?,analysis_id=?,reasons=?,revision=revision+1 WHERE version_id=?",
                    (status, analysis_id, _json(reasons), run["version_id"]),
                )

    def save_review(self, media_version_id, expected_revision, edits, actor):
        with self.transaction() as c:
            result = self._result(c, media_version_id)
            if (
                not result
                or result.result_revision != expected_revision
                or result.analysis_status != "complete"
                or result.freshness != "current"
            ):
                raise RevisionConflict("result changed or is not current")
            version = MediaVersion(
                **json.loads(
                    c.execute("SELECT data FROM reprise_media_versions WHERE id=?", (media_version_id,)).fetchone()[
                        "data"
                    ]
                )
            )
            current = {r.id: r for r in result.ranges}
            explicit_approvals = {e.get("range_id") for e in edits if e.get("review_status") == "approved"}
            if edits:
                current = {
                    rid: replace(item, review_status="proposed")
                    if item.action == "skip" and item.review_status == "approved" and rid not in explicit_approvals
                    else item
                    for rid, item in current.items()
                }
            for edit in edits:
                edit = dict(edit)
                supplied_id = edit.pop("range_id", None)
                acknowledged = edit.pop(SUGGESTION_ACKNOWLEDGMENT, None)
                legacy_acknowledged = edit.pop(LEGACY_PLEX_ACKNOWLEDGMENT, None)
                if supplied_id is not None and supplied_id not in current:
                    raise RevisionConflict("range no longer exists")
                if supplied_id is None and edit.get("action", "keep") != "keep":
                    raise ValueError("new manual ranges must protect content")
                rid = supplied_id if supplied_id is not None else _id()
                original = current.get(rid)
                existing = original is not None
                if original is None:
                    original = DecisionRange(
                        rid, "intro", "keep", 0, 1, "proposed", False, "manual", "manual", "", (), ()
                    )
                allowed = {
                    "kind",
                    "action",
                    "start_ms",
                    "end_ms",
                    "review_status",
                    "locked",
                    "reason",
                    "reason_code",
                    "evidence_ids",
                    "audio_stream_ids",
                    "terminal",
                }
                if set(edit) - allowed:
                    raise ValueError("unknown review field")
                for key in ("evidence_ids", "audio_stream_ids"):
                    if key in edit:
                        edit[key] = tuple(edit[key])
                updated = replace(original, **edit)
                _validate_range(updated, version.duration_ms)
                if (
                    updated.origin in SUGGESTION_ORIGINS
                    and updated.action == "skip"
                    and updated.review_status == "approved"
                    and (original.review_status != "approved" or updated != original)
                    and acknowledged is not True
                    and legacy_acknowledged is not True
                ):
                    # Approving a range, or changing one already approved, is the owner's deliberate act for that
                    # range: nothing Reprise confirmed backs it, so a bulk or older client cannot do it by accident.
                    raise ReviewRefused(NEEDS_ACKNOWLEDGMENT)
                if existing and original.action == "keep" and updated.action != "keep":
                    raise ValueError("a protected keep cannot be implicitly removed")
                current[rid] = updated
                if not existing or updated != original:
                    c.execute(
                        "INSERT INTO reprise_manual_overrides VALUES (?,?,?,?,0) ON CONFLICT(version_id,range_id) DO UPDATE SET data=excluded.data,actor=excluded.actor,quarantined=0",
                        (media_version_id, rid, _json(asdict(updated)), str(actor)),
                    )
            if edits:
                c.execute("DELETE FROM reprise_ranges WHERE version_id=?", (media_version_id,))
                for r in current.values():
                    c.execute("INSERT INTO reprise_ranges VALUES (?,?,?)", (media_version_id, r.id, _json(asdict(r))))
                c.execute(
                    "UPDATE reprise_decision_results SET revision=revision+1 WHERE version_id=?", (media_version_id,)
                )
            return self._result(c, media_version_id)

    def _new_client(self, c, name, scopes):
        if not isinstance(name, str) or not name.strip():
            raise ValueError("client name required")
        scopes = tuple(sorted(set(DEFAULT_SCOPES if scopes is None else scopes)))
        if not scopes or not set(scopes) <= ALLOWED_SCOPES:
            raise ValueError("invalid client scopes")
        client_id, secret = _id(), secrets.token_urlsafe(32)
        c.execute(
            "INSERT INTO reprise_api_clients VALUES (?,?,?,?,0)",
            (client_id, name.strip(), _json(scopes), hashlib.sha256(secret.encode()).hexdigest()),
        )
        return {"id": client_id, "name": name.strip(), "scopes": list(scopes), "secret": secret}

    def create_client(self, name, scopes=None):
        with self.transaction() as c:
            return self._new_client(c, name, scopes)

    def list_clients(self):
        with self.marker_store._reading() as c:
            rows = c.execute("SELECT id,name,scopes,revoked FROM reprise_api_clients ORDER BY rowid").fetchall()
        return [
            {"id": r["id"], "name": r["name"], "scopes": json.loads(r["scopes"]), "revoked": bool(r["revoked"])}
            for r in rows
        ]

    def revoke_client(self, client_id):
        with self.transaction() as c:
            if not c.execute(
                "UPDATE reprise_api_clients SET revoked=1 WHERE id=? AND revoked=0", (client_id,)
            ).rowcount:
                raise KeyError(client_id)

    def rotate_client(self, client_id):
        with self.transaction() as c:
            row = c.execute(
                "SELECT name,scopes FROM reprise_api_clients WHERE id=? AND revoked=0", (client_id,)
            ).fetchone()
            if not row:
                raise KeyError(client_id)
            c.execute("UPDATE reprise_api_clients SET revoked=1 WHERE id=?", (client_id,))
            return self._new_client(c, row["name"], json.loads(row["scopes"]))

    def authenticate_client(self, secret):
        if not isinstance(secret, str) or not secret:
            return None
        digest = hashlib.sha256(secret.encode()).hexdigest()
        with self.marker_store._reading() as c:
            row = c.execute(
                "SELECT id,name,scopes FROM reprise_api_clients WHERE token_hash=? AND revoked=0", (digest,)
            ).fetchone()
        return ClientPrincipal(row["id"], row["name"], tuple(json.loads(row["scopes"]))) if row else None
