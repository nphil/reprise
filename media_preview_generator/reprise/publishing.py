"""Reviewed Plex publication with a durable, deliberately non-atomic app journal.

The app and Plex are separate databases. An intent is committed in markers.db
before Plex can commit. Interrupted intents are classified by exact snapshots;
recovery never repeats a write. Only the tested local Plex representation is
supported. The old publisher's broad replacement and version-winner logic are
not authorization and are not used here.
"""

from __future__ import annotations

import copy
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from urllib.parse import urlsplit

from ..markers.models import Marker, MarkerType
from ..markers.publishers import plex_db
from .admission import stable_metadata
from .identity import is_current
from .models import PlaybackIdentity
from .review import plex_projection
from .store import RevisionConflict


class PublicationConflict(RevisionConflict, ValueError):
    """The reviewed state or the external database no longer matches."""


# Who the journal names for a change Reprise made by itself (its automatic sending); the owner's carry their own name.
AUTO_ACTOR = "reprise-auto"


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _rows(conn, sql, args=()):
    cursor = conn.execute(sql, args)
    names = [x[0] for x in cursor.description]
    return [dict(zip(names, row, strict=False)) for row in cursor.fetchall()]


def _selected(conn, table, column, values):
    # All identifiers are fixed call-site constants, never request data.
    if not values:
        return []
    marks = ",".join("?" for _ in values)
    return _rows(conn, f'SELECT * FROM "{table}" WHERE "{column}" IN ({marks}) ORDER BY id', tuple(values))


def _live(row):
    return row.get("deleted_at") is None


def _projection(ranges):
    return [(r.kind, r.start_ms, r.end_ms) for r in ranges]


def _plex_skips(snapshot):
    """``{(kind, start_ms, end_ms)}``: each intro or credits Plex serves right now that is the only one of its kind on
    every item of the snapshot, with the same times on all of them. A kind with several markers is left out, and so is
    everything when a row cannot be read (the plan refuses such a row)."""
    tags = {t["id"] for t in snapshot["tags"] if t["tag_type"] == 12}
    items = {m["id"] for m in snapshot["metadata_items"] if _live(m)}
    held = {}  # (item, kind) -> the served (start_ms, end_ms) of each row
    for row in snapshot["taggings"]:
        if row["tag_id"] not in tags or row["metadata_item_id"] not in items:
            continue
        try:
            fields, _ = plex_db.decode_extra_data(row["extra_data"])
            served = plex_db._served_times(
                MarkerType(row["text"]), row["time_offset"], row["end_time_offset"], fields.get("pv:final") == "1"
            )
        except (plex_db.PublishError, ValueError, TypeError):
            return frozenset()
        held.setdefault((row["metadata_item_id"], row["text"]), []).append(served)
    same = set()
    for kind in {kind for _, kind in held}:
        per_item = [held.get((item, kind), []) for item in items]
        if len(per_item[0]) == 1 and all(markers == per_item[0] for markers in per_item):
            same.add((kind, *per_item[0][0]))
    return frozenset(same)


class ReprisePublisher:
    def __init__(self, store, registry, service, *, writes_enabled=False):
        self.store, self.registry, self.service = store, registry, service
        self.writes_enabled = writes_enabled is True
        with self._journal() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS reprise_publication_changes (
                id TEXT PRIMARY KEY, media_version_id TEXT NOT NULL,
                state TEXT NOT NULL, actor TEXT NOT NULL, updated_actor TEXT NOT NULL,
                created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
                payload TEXT NOT NULL)""")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS reprise_publication_media ON reprise_publication_changes(media_version_id,created_at)"
            )

    @contextmanager
    def _journal(self):
        # MarkerStore normally uses NORMAL for analysis throughput. A publish
        # intent must be fsynced before the independent Plex database commits.
        marker = self.store.marker_store
        with marker._lock:
            previous = marker._conn.execute("PRAGMA synchronous").fetchone()[0]
            marker._conn.execute("PRAGMA synchronous=FULL")
            try:
                with self.store.transaction() as conn:
                    yield conn
            finally:
                marker._conn.execute(f"PRAGMA synchronous={int(previous)}")

    def _enabled(self):
        if not self.writes_enabled:
            raise ValueError("Reprise Plex writes are disabled")

    @staticmethod
    def _actor(actor):
        # HTTP adapters supply the authenticated owner; never accept an actor
        # from the request body. This check is not an authentication mechanism.
        if not isinstance(actor, str) or not actor.strip():
            raise ValueError("A trusted service actor is required")

    def _database_for(self, server_id, identity):
        cfg = self.registry.get_config(server_id)
        server = self.registry.get(server_id)
        if not cfg or not cfg.enabled or getattr(cfg.type, "value", cfg.type) != "plex" or not server:
            raise ValueError("The bound Plex server is not enabled")
        folder = str((cfg.output or {}).get("plex_config_folder") or "").strip()
        path = plex_db.PlexMarkerPublisher.db_path_for(cfg)
        if not folder or not path:
            raise ValueError("Publication requires the local Plex database")
        if plex_db.plex_machine_identifier(folder) != identity.plex_machine_identifier:
            raise PublicationConflict("The local database belongs to a different Plex server")
        live = server._plex_http("get", "/identity").json()["MediaContainer"]
        if str(live.get("machineIdentifier", "")) != identity.plex_machine_identifier:
            raise PublicationConflict("The connected Plex server identity changed")
        if not plex_db.is_tested_pms_version(live.get("version")):
            raise ValueError("This Plex version has not been verified for database publication")
        items = (
            server._plex_http("get", f"/library/metadata/{identity.rating_key}")
            .json()["MediaContainer"]
            .get("Metadata", [])
        )
        items = [item for item in items if str(item.get("ratingKey")) == identity.rating_key]
        if len(items) != 1:
            raise PublicationConflict("Plex no longer resolves the exact metadata item")
        media = [m for m in items[0].get("Media", []) if str(m.get("id")) == identity.media_id]
        if len(media) != 1 or len(media[0].get("Part", [])) != 1:
            raise PublicationConflict("Plex no longer resolves the exact single-part media version")
        part = media[0]["Part"][0]
        audio = [
            s
            for s in part.get("Stream", [])
            if str(s.get("streamType")) == "2" and str(s.get("id")) == identity.audio_stream_id
        ]
        if (
            str(part.get("id")) != identity.part_id
            or urlsplit(str(part.get("key", ""))).path != identity.part_key
            or int(part.get("size") or 0) != identity.part_size_bytes
            or int(media[0].get("duration") or items[0].get("duration") or 0) != identity.duration_ms
            or len(audio) != 1
        ):
            raise PublicationConflict("Plex playback part, duration, size, or selected audio changed")
        db = plex_db.LocalPlexDb(lambda: path)
        report = db.file_checks(deadline=time.monotonic() + 10)
        if not report.ready:
            raise ValueError("The local Plex database did not pass its safety checks")
        return db

    def validate_target(self, media_version_id, identity):
        """Read-only prerequisite check for the owner's publication switch."""
        if not isinstance(identity, PlaybackIdentity):
            raise ValueError("An exact playback identity is required")
        version = self.store.resolve_binding(identity)
        bindings = [b for b in self.store.get_bindings(media_version_id) if b[0] == identity]
        if not version or version.id != media_version_id or not is_current(version) or len(bindings) != 1:
            raise PublicationConflict("The publication target is not current and uniquely bound")
        if not self.service._valid_binding(version, *bindings[0]):
            raise PublicationConflict("The selected library or path mapping is no longer authorized")
        server_id = bindings[0][1]
        db = self._database_for(server_id, identity)
        with db._database(read_only=True, deadline=time.monotonic() + 10) as conn:
            conn.execute("BEGIN")
            snapshot = self._snapshot(conn, identity)
            if not any(str(p["id"]) == identity.part_id and _live(p) for p in snapshot["media_parts"]):
                raise PublicationConflict("Plex no longer has the selected part")
        return {"ready": True, "server_id": server_id, "machine_identifier": identity.plex_machine_identifier}

    def _snapshot(self, conn, identity):
        try:
            plex_db.LocalPlexDb._check_schema(conn)
        except plex_db.PublishError as exc:
            raise ValueError("The Plex database schema is unsupported") from exc
        # Include the entire shared-file component, not just siblings selected
        # by the legacy writer's same-version-files heuristic.
        keys = {int(identity.rating_key)}
        while True:
            metadata = _selected(conn, "metadata_items", "id", sorted(keys))
            media = _selected(conn, "media_items", "metadata_item_id", sorted(keys))
            parts = _selected(conn, "media_parts", "media_item_id", [m["id"] for m in media])
            files = sorted({p["file"] for p in parts if _live(p) and p.get("file")})
            siblings = _selected(conn, "media_parts", "file", files)
            sibling_media = _selected(conn, "media_items", "id", sorted({p["media_item_id"] for p in siblings}))
            expanded = keys | {m["metadata_item_id"] for m in sibling_media}
            if expanded == keys:
                break
            if len(expanded) > 100:
                raise ValueError("Shared-file metadata is too ambiguous to publish")
            keys = expanded
        streams = _selected(conn, "media_streams", "media_item_id", [m["id"] for m in media])
        taggings = _selected(conn, "taggings", "metadata_item_id", sorted(keys))
        tags = _rows(
            conn,
            "SELECT * FROM tags WHERE tag_type=12 OR id IN (SELECT tag_id FROM taggings WHERE metadata_item_id IN ("
            + ",".join("?" for _ in keys)
            + ")) ORDER BY id",
            tuple(sorted(keys)),
        )
        schema = _rows(
            conn,
            "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE tbl_name IN ('tags','taggings','metadata_items','media_items','media_parts','media_streams') ORDER BY type,name",
        )
        sequence = _rows(conn, "SELECT name,seq FROM sqlite_sequence WHERE name='taggings' ORDER BY name")
        return {
            "metadata_items": metadata,
            "media_items": media,
            "media_parts": parts,
            "media_streams": streams,
            "taggings": taggings,
            "tags": tags,
            "schema": schema,
            "tagging_sequence": sequence,
        }

    def _canonical(self, snapshot, identity, media_version_id, expected_revision):
        media = [m for m in snapshot["media_items"] if _live(m)]
        metadata = {m["id"]: m for m in snapshot["metadata_items"]}
        if not media or any(not _live(m) for m in metadata.values()):
            raise PublicationConflict("Plex metadata is missing or deleted")
        if {m["metadata_item_id"] for m in media} != set(metadata):
            raise PublicationConflict("Plex metadata is missing or deleted")
        plex_now = _plex_skips(snapshot)
        with self.store.marker_store._reading(snapshot=True) as conn:
            bindings = _rows(
                conn,
                "SELECT * FROM reprise_bindings WHERE machine=? AND active=1 ORDER BY rating_key,media_id,part_id,audio_id,version_id",
                (identity.plex_machine_identifier,),
            )
        canonical, wanted = [], None
        selected_found = False
        for item in media:
            parts = [p for p in snapshot["media_parts"] if p["media_item_id"] == item["id"] and _live(p)]
            if len(parts) != 1:
                raise ValueError("Stacked or missing playable parts are unsupported")
            part = parts[0]
            matches = [
                b
                for b in bindings
                if (b["rating_key"], b["media_id"], b["part_id"])
                == (str(item["metadata_item_id"]), str(item["id"]), str(part["id"]))
            ]
            version_ids = {b["version_id"] for b in matches}
            if len(version_ids) != 1:
                raise ValueError("Every playable version and shared-file sibling needs an unambiguous reviewed binding")
            version_id = version_ids.pop()
            version = self.store.get_media(version_id)
            result = self.store.get_result(version_id)
            if not version or not is_current(version) or not result or result.freshness != "current":
                raise PublicationConflict("A playable media version or its evidence is no longer current")
            if version_id == media_version_id and result.result_revision != expected_revision:
                raise PublicationConflict("The reviewed revision changed")
            audio = [
                s for s in snapshot["media_streams"] if s["media_part_id"] == part["id"] and s["stream_type_id"] == 2
            ]
            track_ids = {str(s["id"]) for s in audio}
            signature = json.loads(version.stream_signature)
            tracks = signature.get("audio_streams", [])
            if (
                not track_ids
                or {b["audio_id"] for b in matches} != track_ids
                or {str(t["plex_id"]) for t in tracks} != track_ids
                or len(matches) != len(track_ids)
            ):
                raise ValueError("Every playable audio track must be bound and reviewed")
            for stream in audio:
                track = next(t for t in tracks if str(t["plex_id"]) == str(stream["id"]))
                if int(track["index"]) != stream["index"]:
                    raise PublicationConflict("The selected audio stream generation changed")
            if part.get("size") != version.size_bytes or item.get("duration") != version.duration_ms:
                raise PublicationConflict("Plex media size or duration changed")
            if part.get("duration") not in (None, version.duration_ms):
                raise PublicationConflict("Plex part duration changed")
            from ..servers.ownership import apply_path_mappings

            for server_id in {b["server_id"] for b in matches}:
                cfg = self.registry.get_config(server_id)
                if not cfg:
                    raise PublicationConflict("The bound server configuration disappeared")
                candidates = apply_path_mappings(part["file"], cfg.path_mappings)
                if version.canonical_path not in {os.path.realpath(p) for p in candidates}:
                    raise PublicationConflict("Plex now points to a different media file")
            for binding in matches:
                bound = PlaybackIdentity(**json.loads(binding["identity"]))
                if self.store.resolve_binding(bound) != version:
                    raise PublicationConflict("The exact playback binding changed")
                if (bound.part_size_bytes, bound.duration_ms) != (version.size_bytes, version.duration_ms):
                    raise PublicationConflict("Playback identity differs from the reviewed file")
                if (
                    not bound.part_key.startswith(f"/library/parts/{part['id']}/")
                    or "?" in bound.part_key
                    or "#" in bound.part_key
                ):
                    raise ValueError("The part key is not an exact token-free playback path")
                projected = plex_projection(result, bound, self.service.eligible_ranges(result, bound), plex_now)
                projection = _projection(projected)
                if wanted is None:
                    wanted = projection
                elif projection != wanted:
                    raise ValueError("Playable versions, audio tracks, or shared-file siblings disagree")
                if bound == identity and version_id == media_version_id:
                    selected_found = True
            canonical.append({"version": asdict(version), "result": asdict(result), "bindings": matches})
        if not selected_found:
            raise PublicationConflict("The selected exact playback identity is not current")
        # Stable JSON shape avoids tuple/list differences after durable reload.
        return json.loads(_json(canonical)), wanted

    @staticmethod
    def _plan(conn, before, projection):
        after = copy.deepcopy(before)
        tags = [t for t in before["tags"] if t["tag_type"] == 12 and t["tag"] == ""]
        if len(tags) != 1:
            raise ValueError("Plex must have exactly one existing marker tag")
        tag_id = tags[0]["id"]
        marker_tags = {t["id"] for t in before["tags"] if t["tag_type"] == 12}
        old = [r for r in before["taggings"] if r["tag_id"] in marker_tags]
        if any(r["tag_id"] != tag_id or r["text"] not in ("intro", "credits") for r in old):
            raise ValueError("Existing Plex marker representation is unsupported")
        for row in old:
            try:
                fields, _ = plex_db.decode_extra_data(row["extra_data"])
            except plex_db.PublishError as exc:
                raise ValueError("Existing Plex marker encoding is unsupported") from exc
            version = plex_db.INTRO_JSON_VERSION if row["text"] == "intro" else plex_db.CREDITS_JSON_VERSION
            if (
                str(fields.get("pv:version")) != str(version)
                or set(fields) - {"pv:version", "pv:final", "url"}
                or ("pv:final" in fields and (row["text"] != "credits" or str(fields["pv:final"]) != "1"))
                or type(row["time_offset"]) is not int
                or type(row["end_time_offset"]) is not int
            ):
                raise ValueError("Existing Plex marker encoding is unsupported")
            start, end = plex_db._served_times(
                MarkerType(row["text"]), row["time_offset"], row["end_time_offset"], fields.get("pv:final") == "1"
            )
            if not 0 <= start < end:
                raise ValueError("Existing Plex marker bounds are unsupported")
        # Only approved kinds are replaced. Rows and part payloads of any other kind stay exactly as Plex has them
        # (they were still validated above), so fixing credits never touches Plex's intro, and the reverse.
        approved_kinds = {kind for kind, _, _ in projection}
        columns = [r[1] for r in conn.execute("PRAGMA table_info(taggings)")]
        known = {
            "id",
            "metadata_item_id",
            "tag_id",
            "index",
            "text",
            "time_offset",
            "end_time_offset",
            "thumb_url",
            "created_at",
            "extra_data",
        }
        if set(columns) != known:
            raise ValueError("Plex tagging columns differ from the verified representation")
        # `after` is a deep copy: rewrite its own rows, never `before`'s.
        after["taggings"] = [
            r for r in after["taggings"] if r["tag_id"] not in marker_tags or r["text"] not in approved_kinds
        ]
        sequence = before["tagging_sequence"]
        last_id = sequence[0]["seq"] if sequence else 0
        next_id = max(last_id, conn.execute("SELECT COALESCE(MAX(id),0) FROM taggings").fetchone()[0]) + 1
        now = int(time.time())
        markers = [Marker(MarkerType(kind), start, end, ("reprise-reviewed",)) for kind, start, end in projection]
        durations = {m["id"]: m["duration"] for m in before["media_items"] if _live(m)}
        metadata_ids = sorted({m["metadata_item_id"] for m in before["media_items"] if _live(m)})
        for key in metadata_ids:
            duration = next(m["duration"] for m in before["media_items"] if m["metadata_item_id"] == key and _live(m))
            new_rows = []
            for marker in markers:
                start, end, extra = plex_db._stored_row(marker, duration)
                new_rows.append(
                    dict(
                        id=next_id,
                        metadata_item_id=key,
                        tag_id=tag_id,
                        index=0,
                        text=marker.type.value,
                        time_offset=start,
                        end_time_offset=end,
                        thumb_url="",
                        created_at=now,
                        extra_data=extra,
                    )
                )
                next_id += 1
            kept = [r for r in after["taggings"] if r["metadata_item_id"] == key and r["tag_id"] in marker_tags]
            if kept:
                # Same numbering as the legacy writer: one contiguous run per item, ordered by type then time.
                # A kept row changes only its `index` (when that run needs it); id, times and extra_data never.
                for index, row in enumerate(
                    sorted(kept + new_rows, key=lambda r: (r["text"], r["time_offset"], r["end_time_offset"]))
                ):
                    row["index"] = index
            else:
                for index, row in enumerate(new_rows):
                    row["index"] = index
            after["taggings"].extend(new_rows)
        after["taggings"].sort(key=lambda r: r["id"])
        for part in after["media_parts"]:
            if not _live(part) or part["media_item_id"] not in durations:
                continue
            try:
                fields, url_form = plex_db.decode_extra_data(part["extra_data"])
            except plex_db.PublishError as exc:
                raise ValueError("Existing Plex part encoding is unsupported") from exc
            changed = False
            for kind in ("intro", "credits"):
                key = "pv:" + ("intros" if kind == "intro" else "credits")
                if key in fields:
                    try:
                        plex_db._check_marker_array(key, fields[key])
                    except plex_db.PublishError as exc:
                        raise ValueError("Existing Plex part markers are unsupported") from exc
                mtype = MarkerType(kind)
                selected = [m for m in markers if m.type == mtype]
                if not selected:
                    continue
                fields[key] = plex_db._part_payload(mtype, selected, durations[part["media_item_id"]])
                changed = True
            if changed:
                part["extra_data"] = plex_db.encode_extra_data(fields, url_form=url_form)
        after["tagging_sequence"] = [{"name": "taggings", "seq": next_id - 1}]
        return after

    def prepare(self, media_version_id, expected_revision, identity, actor):
        self._actor(actor)
        if not isinstance(identity, PlaybackIdentity) or type(expected_revision) is not int:
            raise ValueError("An exact playback identity and revision are required")
        bindings = [b for b in self.store.get_bindings(media_version_id) if b[0] == identity]
        if len(bindings) != 1:
            raise PublicationConflict("The selected playback identity is not uniquely bound")
        version = self.store.get_media(media_version_id)
        if not version or not self.service._valid_binding(version, *bindings[0]):
            raise PublicationConflict("The selected library or path mapping is no longer authorized")
        server_id = bindings[0][1]
        db = self._database_for(server_id, identity)
        with self._journal() as app:
            with db._database(read_only=True, deadline=time.monotonic() + 10) as conn:
                conn.execute("BEGIN")
                before = self._snapshot(conn, identity)
                canonical, projection = self._canonical(before, identity, media_version_id, expected_revision)
                after = self._plan(conn, before, projection)
                db_identity = list(db.trust_key()[1])
            payload = dict(
                server_id=server_id,
                identity=asdict(identity),
                revision=expected_revision,
                canonical=canonical,
                projection=projection,
                before=before,
                after=after,
                db_identity=db_identity,
            )
            change_id, now = uuid.uuid4().hex, int(time.time())
            app.execute(
                "INSERT INTO reprise_publication_changes VALUES (?,?,?,?,?,?,?,?)",
                (change_id, media_version_id, "prepared", actor, actor, now, now, _json(payload)),
            )
        return self._public(self._load(change_id))

    def _load(self, change_id):
        with self.store.marker_store._reading() as conn:
            row = conn.execute("SELECT * FROM reprise_publication_changes WHERE id=?", (change_id,)).fetchone()
        if row is None:
            raise KeyError(change_id)
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        return result

    @staticmethod
    def _public(row):
        answer = {
            "id": row["id"],
            "change_id": row["id"],
            "media_version_id": row["media_version_id"],
            "state": row["state"],
            "status": row["state"],
            "revision": row["payload"]["revision"],
            "actor": row["actor"],
            "updated_actor": row["updated_actor"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        for side, other in (("before", "after"), ("after", "before")):
            snapshot = row["payload"][side]
            # A marker row the change leaves exactly as Plex has it (same row, times and data) is flagged, so a kind
            # that was not approved reads as unchanged.
            kept = {
                (r["id"], r["time_offset"], r["end_time_offset"], r["extra_data"])
                for r in row["payload"][other]["taggings"]
            }
            tags = {t["id"] for t in snapshot["tags"] if t["tag_type"] == 12}
            markers = []
            for marker in snapshot["taggings"]:
                if marker["tag_id"] not in tags or marker["text"] not in ("intro", "credits"):
                    continue
                start, end = plex_db._served_times(
                    MarkerType(marker["text"]),
                    marker["time_offset"],
                    marker["end_time_offset"],
                    plex_db._row_is_final(marker["extra_data"]),
                )
                markers.append(
                    {
                        "type": marker["text"],
                        "start_ms": start,
                        "end_ms": end,
                        "rating_key": str(marker["metadata_item_id"]),
                        "unchanged": (
                            marker["id"],
                            marker["time_offset"],
                            marker["end_time_offset"],
                            marker["extra_data"],
                        )
                        in kept,
                    }
                )
            answer[side + "_markers"] = markers
        answer["identity"] = row["payload"]["identity"]
        return answer

    def list_changes(self, media_version_id):
        with self.store.marker_store._reading() as conn:
            ids = [
                r["id"]
                for r in conn.execute(
                    "SELECT id FROM reprise_publication_changes WHERE media_version_id=? ORDER BY created_at DESC,id DESC",
                    (media_version_id,),
                )
            ]
        return [self._public(self._load(i)) for i in ids]

    def _latest_settled(self, media_version_ids):
        """``{media_version_id: (state, revision, actor)}`` for each version's latest applied-or-undone change in the
        journal. One read of the journal: the Plex report asks for a whole season at once."""
        ids = list(dict.fromkeys(media_version_ids))
        latest = {}
        with self.store.marker_store._reading() as conn:
            for start in range(0, len(ids), 400):
                chunk = ids[start : start + 400]
                rows = conn.execute(
                    "SELECT media_version_id,state,actor,json_extract(payload,'$.revision') AS revision "
                    "FROM reprise_publication_changes WHERE state IN ('applied','undone') "
                    f"AND media_version_id IN ({','.join('?' for _ in chunk)}) ORDER BY created_at,rowid",
                    chunk,
                ).fetchall()
                for row in rows:
                    latest[row["media_version_id"]] = (row["state"], row["revision"], row["actor"])
        return latest

    def published_versions(self, media_version_ids):
        """``{media_version_id: revision}`` for each version whose latest settled change in the journal is an applied
        one (a later undo clears it)."""
        latest = self._latest_settled(media_version_ids)
        return {version: revision for version, (state, revision, _) in latest.items() if state == "applied"}

    def automatically_published(self, media_version_ids):
        """The versions among these whose latest settled change is an applied one Reprise made by itself."""
        latest = self._latest_settled(media_version_ids)
        return {version for version, (state, _, actor) in latest.items() if state == "applied" and actor == AUTO_ACTOR}

    def automatic_hold(self, media_version_id):
        """Why Reprise must not send this episode by itself, as a plain sentence; None when nothing stands in the way.
        A change already sent (by anyone), undone or left unfinished is a decision the owner takes from there; one that
        never reached Plex (``not_applied``) counts for nothing."""
        with self.store.marker_store._reading() as conn:
            rows = conn.execute(
                "SELECT state,actor FROM reprise_publication_changes WHERE media_version_id=?", (media_version_id,)
            ).fetchall()
        for row in rows:
            if row["state"] == "applied":
                return "It was already sent to Plex" + (" automatically." if row["actor"] == AUTO_ACTOR else " by you.")
            if row["state"] == "undone":
                return "A change sent to Plex for it was undone, so what happens next is yours to decide."
            if row["state"] != "not_applied":
                return "An earlier change to Plex for it is not finished."
        return None

    def unfinished_writes(self):
        """How many changes are being written to Plex right now or were cut off mid-write (``applying``/``undoing``).
        An ``uncertain`` one is not counted: it is final, and nothing resolves it from here."""
        with self.store.marker_store._reading() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM reprise_publication_changes WHERE state IN ('applying','undoing')"
            ).fetchone()[0]

    def abandon(self, change_id, actor):
        """Give up a prepared change that was never applied: Plex was not touched and the change is no longer offered."""
        self._actor(actor)
        self._intent(change_id, "prepared", "not_applied", actor)

    def _set_state(self, change_id, state, actor):
        with self._journal() as conn:
            conn.execute(
                "UPDATE reprise_publication_changes SET state=?,updated_actor=?,updated_at=? WHERE id=?",
                (state, actor, int(time.time()), change_id),
            )

    def _intent(self, change_id, source, target, actor):
        with self._journal() as conn:
            row = self._load(change_id)
            if row["state"] != source:
                raise PublicationConflict(
                    "Publication is not in the required state; recover interrupted intents instead of replaying"
                )
            conn.execute(
                "UPDATE reprise_publication_changes SET state=?,updated_actor=?,updated_at=? WHERE id=?",
                (target, actor, int(time.time()), change_id),
            )
        return row

    @staticmethod
    def _restore_rows(conn, source, target):
        # Only rows this publication owns are deleted/inserted. Metadata, tags,
        # streams, unrelated taggings, and part generation fields are untouched.
        old = {r["id"]: r for r in source["taggings"]}
        new = {r["id"]: r for r in target["taggings"]}
        for key, row in old.items():
            if row != new.get(key):
                conn.execute("DELETE FROM taggings WHERE id=?", (key,))
        for key, row in new.items():
            if row != old.get(key):
                columns = list(row)
                conn.execute(
                    "INSERT INTO taggings ("
                    + ",".join('"' + c + '"' for c in columns)
                    + ") VALUES ("
                    + ",".join("?" for _ in columns)
                    + ")",
                    tuple(row[c] for c in columns),
                )
        prior_parts = {p["id"]: p for p in source["media_parts"]}
        for part in target["media_parts"]:
            if prior_parts[part["id"]]["extra_data"] != part["extra_data"]:
                conn.execute("UPDATE media_parts SET extra_data=? WHERE id=?", (part["extra_data"], part["id"]))
        # AUTOINCREMENT is an owned side effect too. Exact precondition checks
        # include this row, so an unrelated later insert makes undo refuse
        # rather than rewinding another writer's sequence.
        if target["tagging_sequence"]:
            conn.execute(
                "UPDATE sqlite_sequence SET seq=? WHERE name='taggings'", (target["tagging_sequence"][0]["seq"],)
            )
        else:
            conn.execute("DELETE FROM sqlite_sequence WHERE name='taggings'")

    def _generation_current(self, payload):
        for entry in payload["canonical"]:
            version = self.store.get_media(entry["version"]["id"])
            if not version or asdict(version) != entry["version"] or not is_current(version):
                raise PublicationConflict("The media file generation changed")
            for binding in entry["bindings"]:
                identity = PlaybackIdentity(**json.loads(binding["identity"]))
                if self.store.resolve_binding(identity) != version:
                    raise PublicationConflict("The playback binding generation changed")
                current = [b for b in self.store.get_bindings(version.id) if b[0] == identity]
                if (
                    len(current) != 1
                    or current[0][1] != binding["server_id"]
                    or stable_metadata(current[0][2]) != stable_metadata(json.loads(binding["metadata"]))
                    or not self.service._valid_binding(version, *current[0])
                ):
                    raise PublicationConflict("The bound library, path mapping, or authorization changed")

    def _execute(self, change_id, actor, *, undo):
        self._enabled()
        self._actor(actor)
        row = self._load(change_id)
        payload = row["payload"]
        identity = PlaybackIdentity(**payload["identity"])
        self._generation_current(payload)
        db = self._database_for(payload["server_id"], identity)
        if list(db.trust_key()[1]) != payload["db_identity"]:
            raise PublicationConflict("The Plex database file was replaced")
        source_state, intent_state = ("applied", "undoing") if undo else ("prepared", "applying")
        self._intent(change_id, source_state, intent_state, actor)
        before, after = (payload["after"], payload["before"]) if undo else (payload["before"], payload["after"])
        # Hold the canonical store writer lock from revalidation through Plex
        # commit. Its earlier intent is already durable on another transaction.
        with self._journal():
            if self._load(change_id)["state"] != intent_state:
                raise PublicationConflict("The publication intent was already recovered")
            self._generation_current(payload)
            with db._database(read_only=False, deadline=time.monotonic() + 10) as conn:
                conn.execute("PRAGMA synchronous=FULL")
                conn.execute("BEGIN IMMEDIATE")
                try:
                    current = self._snapshot(conn, identity)
                    if current != before:
                        raise PublicationConflict("Plex rows or part generation changed; publication refused")
                    if not undo:
                        canonical, projection = self._canonical(
                            current, identity, row["media_version_id"], payload["revision"]
                        )
                        if canonical != payload["canonical"] or json.loads(_json(projection)) != payload["projection"]:
                            raise PublicationConflict("The approved revision, evidence, or track bindings changed")
                    self._enabled()
                    self._restore_rows(conn, before, after)
                    if self._snapshot(conn, identity) != after:
                        raise PublicationConflict("Plex did not retain the exact planned state")
                    self._generation_current(payload)
                    self._enabled()
                    conn.execute("COMMIT")
                except BaseException:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    raise
        # If this journal update fails, the earlier intent survives. Never
        # pretend the app/Plex commits were atomic or blindly run them again.
        self._set_state(change_id, "undone" if undo else "applied", actor)
        return self._public(self._load(change_id))

    def apply(self, change_id, actor):
        return self._execute(change_id, actor, undo=False)

    def undo(self, change_id, actor):
        return self._execute(change_id, actor, undo=True)

    def recover(self, change_id, actor="recovery"):
        """Classify an interrupted intent without ever writing to Plex."""
        self._actor(actor)
        row = self._load(change_id)
        if row["state"] not in ("applying", "undoing"):
            return self._public(row)
        payload = row["payload"]
        identity = PlaybackIdentity(**payload["identity"])
        db = self._database_for(payload["server_id"], identity)
        with self._journal() as app:
            row = self._load(change_id)
            if row["state"] not in ("applying", "undoing"):
                return self._public(row)
            state = "uncertain"
            try:
                self._generation_current(payload)
                if list(db.trust_key()[1]) != payload["db_identity"]:
                    raise PublicationConflict("Plex database generation changed")
                with db._database(read_only=True, deadline=time.monotonic() + 10) as conn:
                    conn.execute("BEGIN")
                    current = self._snapshot(conn, identity)
                if current == payload["before"]:
                    state = "undone" if row["state"] == "undoing" else "not_applied"
                elif current == payload["after"]:
                    state = "applied"
            except (ValueError, OSError, sqlite3.Error):
                state = "uncertain"
            app.execute(
                "UPDATE reprise_publication_changes SET state=?,updated_actor=?,updated_at=? WHERE id=?",
                (state, actor, int(time.time()), change_id),
            )
        return self._public(self._load(change_id))
