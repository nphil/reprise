"""Reprise data in the existing MarkerStore database and transaction lifecycle."""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from dataclasses import asdict, replace

from .identity import is_current
from .models import AnalysisResult, ClientPrincipal, DecisionRange, Evidence, MediaVersion, PlaybackIdentity

POLICY_VERSION = "1"
DEFAULT_SCOPES = ("results:read", "jobs:read")
ALLOWED_SCOPES = frozenset((*DEFAULT_SCOPES, "analysis:request"))


class RevisionConflict(Exception):
    pass


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _id():
    return uuid.uuid4().hex


def _range(data):
    data = dict(data)
    data["evidence_ids"] = tuple(data["evidence_ids"])
    data["audio_stream_ids"] = tuple(data["audio_stream_ids"])
    return DecisionRange(**data)


def _validate_range(r, duration):
    if (
        r.kind not in ("intro", "credits", "recap", "preview")
        or r.action not in ("skip", "keep")
        or r.review_status not in ("proposed", "approved", "rejected")
        or r.origin not in ("manual", "detector")
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
            ):
                c.execute(sql)

    def transaction(self):
        return self.marker_store._tx()

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
            if old and old["data"] != _json(asdict(version)):
                raise ValueError("media version IDs are immutable")
            c.execute("INSERT OR IGNORE INTO reprise_media_versions VALUES (?,?)", (version.id, _json(asdict(version))))
            prior = c.execute(
                "SELECT metadata,active,server_id FROM reprise_bindings WHERE machine=? AND rating_key=? AND media_id=? AND part_id=? AND audio_id=? AND version_id=?",
                (*binding, identity.audio_stream_id, version.id),
            ).fetchone()
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
                    removed += 1
            return removed

    def get_media(self, media_version_id):
        with self.marker_store._reading() as c:
            row = c.execute("SELECT data FROM reprise_media_versions WHERE id=?", (media_version_id,)).fetchone()
        return MediaVersion(**json.loads(row["data"])) if row else None

    def get_bindings(self, media_version_id):
        with self.marker_store._reading() as c:
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
            and v
            and is_current(MediaVersion(**json.loads(v["data"])))
            and c.execute(
                "SELECT 1 FROM reprise_bindings WHERE version_id=? AND active=1", (media_version_id,)
            ).fetchone()
            else "stale"
        )
        if freshness == "current":
            for reference_id in {e.reference_version_id for e in evidence if e.reference_version_id}:
                ref = c.execute(
                    "SELECT data FROM reprise_media_versions WHERE id=? AND EXISTS (SELECT 1 FROM reprise_bindings WHERE version_id=? AND active=1)",
                    (reference_id, reference_id),
                ).fetchone()
                if not ref or not is_current(MediaVersion(**json.loads(ref["data"]))):
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

    def finish_analysis(self, analysis_id, evidence, ranges, expected_media_version):
        evidence, ranges = tuple(evidence), tuple(ranges)
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
                if (
                    type(e.start_ms) is not int
                    or type(e.end_ms) is not int
                    or not 0 <= e.start_ms < e.end_ms <= duration
                ):
                    raise ValueError("invalid evidence range")
            for r in ranges:
                _validate_range(r, duration)
                if r.review_status != "proposed" or r.origin != "detector" or r.locked:
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
                    if r.origin == "detector"
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
                and previous.evidence_signature == run["signature"]
                and semantic_evidence(previous.evidence) == semantic_evidence(evidence)
                and semantic_ranges(baseline) == semantic_ranges(ranges)
            )
            if same:
                c.execute(
                    "UPDATE reprise_decision_results SET status=?,analysis_id=?,reasons=? WHERE version_id=?",
                    ("complete", analysis_id, "[]", expected_media_version),
                )
            else:
                override_ids = {
                    row["range_id"]
                    for row in c.execute(
                        "SELECT range_id FROM reprise_manual_overrides WHERE version_id=? AND quarantined=0",
                        (expected_media_version,),
                    )
                }
                preserved = [
                    r
                    for r in (previous.ranges if previous else ())
                    if r.origin == "manual"
                    or r.locked
                    or r.id in override_ids
                    or (r.action == "keep" and r.review_status != "proposed")
                ]
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
                    (analysis_id, revision, run["signature"], POLICY_VERSION, "complete", "[]", expected_media_version),
                )
            c.execute("UPDATE reprise_analysis_runs SET status=? WHERE id=?", ("complete", analysis_id))
            return self._result(c, expected_media_version)

    def fail_analysis(self, analysis_id, status, reason):
        if status not in ("failed", "cancelled", "unsupported"):
            raise ValueError("invalid failure status")
        with self.transaction() as c:
            run = c.execute("SELECT * FROM reprise_analysis_runs WHERE id=?", (analysis_id,)).fetchone()
            if not run or run["status"] != "running":
                raise RevisionConflict("analysis is no longer running")
            c.execute("UPDATE reprise_analysis_runs SET status=?,reason=? WHERE id=?", (status, reason, analysis_id))
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
                    (status, analysis_id, _json([reason]), run["version_id"]),
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
