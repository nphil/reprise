"""The "To approve" queue: what Reprise is waiting on the owner for, by show and season, and the one-click send.

Everything here is a view over what the store already holds plus a few actions (approve and send the sure ones, send
what the owner approved, decide one suggestion, undo any of them). A decision is saved with the same review and
publication calls the Library uses, so each keeps the checks it has there. Three groups of work wait for the owner,
and only the first can ever be approved in bulk:

* **sure**: the local vision model's High-confidence suggestions, exactly what ``RepriseService.approve_confident``
  approves. A Medium or Low one is never in this group.
* **needs you**: every other skip Reprise suggested that would change Plex (a Medium or Low guess of the local model,
  the file's chapter marks, an online database, a skip found by comparing episodes that was not sent by itself). Plex's
  own marker is not one of them: confirming it would change nothing. Nor is a suggestion Plex already serves.
* **ready**: skips the owner approved that Plex does not have yet. A skip Plex already serves exactly is not waiting,
  whether Reprise sent it, an older version of Reprise did, or Plex found it itself.

What Plex serves is read in a few batched calls (one per 40 episodes) each time the queue is built, so a show that is
finished in Plex is never listed as work. When Plex cannot be read nothing is taken out: the episodes stay listed.

Films are not here: the comparisons, the local model's grading and the sender all work on episodes.
"""

from __future__ import annotations

import json
import sqlite3
import statistics
from collections import defaultdict

from loguru import logger

from ..markers.publishers import plex_db
from . import plexbackup
from .models import ORIGIN_PLEX, SUGGESTION_ORIGINS, NothingToPublish, ReviewRefused, is_owner_span
from .publishing import AUTO_ACTOR
from .report import PLEX_BATCH, TOLERANCE_MS, _hidden_kinds, _pick_show, read_plex_markers

OWNER = "owner"
KINDS = ("intro", "credits")
KIND_WORD = {"intro": "Intro", "credits": "Credits"}
SURE, NEEDS = "sure", "needs"
BIG_MOVE_MS = 30000  # a skip that moves this far from Plex's is worth a look before it is sent
NOTHING_TO_SEND = "Plex already has these skips. Nothing to send."


# ── words ────────────────────────────────────────────────────────────────────────────────────────────────────────


def _secs(ms):
    """``12 s``, ``1 min 5 s``: whole seconds, for a plain sentence."""
    total = round(abs(ms) / 1000)
    if total < 60:
        return f"{total} s"
    minutes, rest = divmod(total, 60)
    return f"{minutes} min {rest} s" if rest else f"{minutes} min"


def _count(number, noun):
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


def _sentence(exc):
    return str(exc).strip().rstrip(".") or type(exc).__name__


def _transient(exc):
    """True for a failure that says nothing about the episode: Plex or its database was busy or unreachable."""
    return isinstance(exc, OSError | sqlite3.OperationalError)


def season_label(season):
    return "Specials" if season == 0 else f"Season {season}" if season is not None else "Episodes"


def episode_label(meta):
    season, episode = meta.get("season"), meta.get("episode")
    if isinstance(season, int) and isinstance(episode, int):
        return f"S{season:02d}E{episode:02d}"
    return str(meta.get("title") or "an episode")


# ── what is waiting ──────────────────────────────────────────────────────────────────────────────────────────────


def _current(result):
    return bool(result) and result.analysis_status == "complete" and result.freshness == "current"


def _grade(r, grades):
    """The local model's own rating of a suggestion ("high", "medium", "low"), None for any other source."""
    return next((grades[e] for e in r.evidence_ids if grades.get(e)), None)


def _rank(r, sure_ids, grade, medium):
    """How much a candidate deserves to be the one shown for its kind: lower first."""
    if r.id in sure_ids:
        return 0
    if r.origin not in SUGGESTION_ORIGINS:
        return 1  # Reprise confirmed it by comparing episodes
    if grade == medium:
        return 2
    return 3 if grade is None else 4  # chapter marks and online databases, then a Low guess


def _decisions(version_id, meta, result, duration_ms):
    """The decisions one analysis leaves for the owner, one per kind: ``[item]``. See the module docstring."""
    if not _current(result):
        return []
    from .localvision import MEDIUM
    from .service import _confident_local

    sure_ids = {r.id for r in _confident_local(result.ranges, result.evidence)}
    owned = {r.kind for r in result.ranges if is_owner_span(r) and r.review_status == "approved"}
    grades = {e.id: (e.metrics.get("local_model") or {}).get("confidence") for e in result.evidence}
    why = {e.id: e.reason for e in result.evidence if e.reason}
    by_kind = defaultdict(list)
    for r in result.ranges:
        if (
            r.action == "skip"
            and r.review_status == "proposed"
            and r.kind in KINDS
            and r.kind not in owned
            and r.origin != ORIGIN_PLEX
        ):
            by_kind[r.kind].append(r)
    items = []
    for kind in KINDS:
        candidates = by_kind.get(kind)
        if not candidates:
            continue
        best = min(
            candidates, key=lambda r: (_rank(r, sure_ids, _grade(r, grades), MEDIUM), r.start_ms - r.end_ms, r.id)
        )
        grade = _grade(best, grades)
        items.append(
            {
                "media_version_id": version_id,
                "episode": meta.get("episode"),
                "title": meta.get("title", ""),
                "label": episode_label(meta),
                "kind": kind,
                "bucket": SURE if best.id in sure_ids else NEEDS,
                "start_ms": best.start_ms,
                "end_ms": best.end_ms,
                "duration_ms": duration_ms,
                "how_sure": "confirmed" if best.origin not in SUGGESTION_ORIGINS else grade,
                "source": " and ".join(best.suggested_by) or "Reprise",
                "reason": (best.reason or next((why[e] for e in best.evidence_ids if e in why), ""))[:240],
                "range_ids": [r.id for r in candidates],
                "revision": result.result_revision,
            }
        )
    return items


def _joined(pieces):
    """The skips these approved pieces make, as Plex holds them: pieces of one kind that touch exactly are one skip."""
    skips = []
    for piece in pieces:
        if piece.kind not in KINDS:
            continue
        prior = next((s for s in reversed(skips) if s["kind"] == piece.kind), None)
        if prior and prior["end_ms"] == piece.start_ms:
            prior["end_ms"] = piece.end_ms
        else:
            skips.append({"kind": piece.kind, "start_ms": piece.start_ms, "end_ms": piece.end_ms})
    return skips


def _ready(service, version_id, meta, result, duration_ms, settled):
    """What the owner approved for this episode that Plex does not have yet, or None. An episode already sent (or sent
    and then undone on purpose) at this very revision has nothing left to send."""
    if not _current(result):
        return None
    state = settled.get(version_id)
    if state and state[1] == result.result_revision:
        return None
    bindings = service._bindings(version_id)
    if not bindings:
        return None
    pieces = service.eligible_ranges(result, bindings[0][0])
    if not pieces:
        return None
    return {
        "media_version_id": version_id,
        "episode": meta.get("episode"),
        "title": meta.get("title", ""),
        "label": episode_label(meta),
        "duration_ms": duration_ms,
        "revision": result.result_revision,
        "skips": _joined(pieces),
    }


def _tv_rows(service):
    """``{version_id: catalogue row}`` for every episode, in library order."""
    rows = {}
    for row in service.store.catalogue():
        if row["metadata"].get("show_title") and row["version_id"] not in rows:
            rows[row["version_id"]] = row
    return rows


class _Groups(defaultdict):
    """What ``_scan`` found, by ``(show, season)``. ``plex`` is what Plex answered while it ran, ``(markers, hidden,
    errors)`` as ``_plex_view`` returns them (None when Plex was not asked)."""

    plex = None


def _empty():
    return {SURE: [], NEEDS: [], "ready": [], "already": []}


def _held(markers, hidden, skip):
    """Whether Plex serves exactly this skip right now. An episode Plex did not answer for, or a kind its library
    hides, says nothing."""
    if markers is None or skip["kind"] in hidden:
        return False
    return any(
        m["kind"] == skip["kind"] and (m["start_ms"], m["end_ms"]) == (skip["start_ms"], skip["end_ms"])
        for m in markers
    )


def _prune(service, groups, rows):
    """Take out of ``groups`` what Plex already settles, and return Plex's answer for the rest. A suggestion Plex serves
    to within a second is no decision (confirming it would change nothing); an approved skip Plex serves exactly is not
    waiting to be sent, so its episode moves to ``already``. What Plex cannot answer for stays where it is."""
    versions = {
        item["media_version_id"]: rows[item["media_version_id"]]["metadata"]
        for group in groups.values()
        for bucket in (SURE, NEEDS, "ready")
        for item in group[bucket]
    }
    view = _plex_view(service, versions.items()) if versions else ({}, {}, set())
    markers, hidden, _ = view
    for group in groups.values():
        for bucket in (SURE, NEEDS):
            group[bucket] = [
                item
                for item in group[bucket]
                if _compare(
                    item["kind"],
                    item["start_ms"],
                    item["end_ms"],
                    markers.get(item["media_version_id"]),
                    hidden.get(item["media_version_id"], ()),
                )["change"]
                != "same"
            ]
        waiting = []
        for item in group["ready"]:
            version_id = item["media_version_id"]
            left = [s for s in item["skips"] if not _held(markers.get(version_id), hidden.get(version_id, ()), s)]
            if left:
                waiting.append({**item, "skips": left})
            else:
                group["already"].append(item)
        group["ready"] = waiting
    return view


def _scan(service, show=None, season=None, *, plex=True):
    """Everything waiting, grouped: ``{(show, season): {"sure": [...], "needs": [...], "ready": [...], "already": [...]}}``.
    Only the episodes that hold a proposed or an approved skip are read, then Plex is asked once (in batches) what it
    serves for them and what it already has is taken out (see ``_prune``); ``plex=False`` leaves that out."""
    store, publisher = service.store, service.publisher
    rows = _tv_rows(service)

    def wanted(version_id):
        meta = (rows.get(version_id) or {}).get("metadata")
        if not meta:
            return False
        if show is not None and str(meta["show_title"]) != show:
            return False
        return season is None or meta.get("season") == season

    groups = _Groups(_empty)
    proposed = {v for v in store.versions_with_skips("proposed") if wanted(v)}
    approved = {v for v in store.versions_with_skips("approved") if wanted(v)}
    settled = publisher.settled_states(sorted(approved)) if publisher is not None and approved else {}
    for version_id in sorted(proposed | approved):
        meta = rows[version_id]["metadata"]
        version = store.get_media(version_id)
        result = store.get_result(version_id)
        if version is None or result is None:
            continue
        key = (str(meta["show_title"]), meta.get("season"))
        if version_id in proposed:
            for item in _decisions(version_id, meta, result, version.duration_ms):
                groups[key][item["bucket"]].append(item)
        if version_id in approved:
            ready = _ready(service, version_id, meta, result, version.duration_ms, settled)
            if ready:
                groups[key]["ready"].append(ready)
    for group in groups.values():
        group[SURE].sort(key=lambda i: (i["episode"] or 0, i["kind"]))
        group[NEEDS].sort(key=lambda i: (i["episode"] or 0, i["kind"]))
        group["ready"].sort(key=lambda i: i["episode"] or 0)
    if plex:
        groups.plex = _prune(service, groups, rows)
    return groups


def sending(service):
    """Whether Reprise can send to Plex right now, with the sentence that says why not."""
    publisher = service.publisher
    if publisher is None:
        return {"available": False, "enabled": False, "message": "Sending to Plex is not available on this install."}
    if not publisher.writes_enabled:
        return {
            "available": True,
            "enabled": False,
            "message": "Sending to Plex is turned off. You can still approve; turn it on in Settings, Sharing, to send.",
        }
    return {"available": True, "enabled": True, "message": None}


def queue(service):
    """The whole queue for the "To approve" page: counts only, from the store and one batched read of what Plex serves
    (so a show that is already finished in Plex is not listed), seasons sorted by what needs the owner most (most
    decisions first, then most sure ones, then most waiting to be sent)."""
    seasons = []
    for (show, number), group in _scan(service).items():
        episodes = {i["media_version_id"] for bucket in (SURE, NEEDS, "ready") for i in group[bucket]}
        seasons.append(
            {
                "show": show,
                "season": number,
                "label": season_label(number),
                "episodes": len(episodes),
                SURE: len(group[SURE]),
                NEEDS: len(group[NEEDS]),
                "ready": len(group["ready"]),
                "kinds": {k: sum(1 for i in group[SURE] if i["kind"] == k) for k in KINDS},
            }
        )
    seasons = [s for s in seasons if s[SURE] or s[NEEDS] or s["ready"]]
    seasons.sort(key=lambda s: (-s[NEEDS], -s[SURE], -s["ready"], s["show"].lower(), s["season"] or 0))
    shows = {}
    for s in seasons:
        shows.setdefault(s["show"], {"show": s["show"], SURE: 0, NEEDS: 0, "ready": 0, "seasons": []})
        entry = shows[s["show"]]
        entry["seasons"].append(s)
        for bucket in (SURE, NEEDS, "ready"):
            entry[bucket] += s[bucket]
    return {
        "shows": list(shows.values()),
        "totals": {
            SURE: sum(s[SURE] for s in seasons),
            NEEDS: sum(s[NEEDS] for s in seasons),
            "ready": sum(s["ready"] for s in seasons),
            "seasons": len(seasons),
        },
        "sending": sending(service),
    }


# ── what would change in Plex ────────────────────────────────────────────────────────────────────────────────────


def _closest(markers, kind, start_ms, end_ms):
    """The marker Plex serves for this kind that lines up best with the span, or None."""
    same = [m for m in markers if m["kind"] == kind]
    if not same:
        return None
    return max(
        same, key=lambda m: (min(m["end_ms"], end_ms) - max(m["start_ms"], start_ms), -abs(m["start_ms"] - start_ms))
    )


def _compare(kind, start_ms, end_ms, markers, hidden):
    """How one skip differs from what Plex has: ``{"plex": marker or None, "change": ...}`` where change is one of
    new (Plex has none), same (within a second), moved or unknown (Plex could not be read for it)."""
    if markers is None or kind in hidden:
        return {"plex": None, "change": "unknown"}
    marker = _closest(markers, kind, start_ms, end_ms)
    if marker is None:
        return {"plex": None, "change": "new"}
    start, end = start_ms - marker["start_ms"], end_ms - marker["end_ms"]
    plex = {"start_ms": marker["start_ms"], "end_ms": marker["end_ms"]}
    if abs(start) <= TOLERANCE_MS and abs(end) <= TOLERANCE_MS:
        return {"plex": plex, "change": "same", "start_delta_ms": start, "end_delta_ms": end}
    return {"plex": plex, "change": "moved", "start_delta_ms": start, "end_delta_ms": end}


def _changes(spans):
    """The plain account of what sending these skips does to Plex: ``{"lines": [...], "counts": {...}}``."""
    counts = {"new": 0, "same": 0, "moved": 0, "unknown": 0}
    lines = []
    for kind in KINDS:
        rows = [s for s in spans if s["kind"] == kind]
        if not rows:
            continue
        word = KIND_WORD[kind]
        for row in rows:
            counts[row["change"]] += 1
        new = [r for r in rows if r["change"] == "new"]
        same = [r for r in rows if r["change"] == "same"]
        moved = [r for r in rows if r["change"] == "moved"]
        unknown = [r for r in rows if r["change"] == "unknown"]
        if new:
            lines.append(f"{word} skip is added on {_count(len(new), 'episode')} where Plex has none.")
        for edge, noun in (("start", "starts"), ("end", "ends")):
            shifts = [r[f"{edge}_delta_ms"] for r in moved if abs(r[f"{edge}_delta_ms"]) > TOLERANCE_MS]
            for sign, direction in ((1, "later"), (-1, "earlier")):
                these = [abs(d) for d in shifts if d * sign > 0]
                if these:
                    lines.append(
                        f"{word} skip {noun} about {_secs(statistics.median(these))} {direction} "
                        f"on {_count(len(these), 'episode')}."
                    )
        if same:
            lines.append(f"{word} skip already matches Plex on {_count(len(same), 'episode')}.")
        if unknown:
            lines.append(
                f"Plex could not be read for {_count(len(unknown), 'episode')}, so what changes there is unknown."
            )
        big = [r for r in moved if max(abs(r["start_delta_ms"]), abs(r["end_delta_ms"])) > BIG_MOVE_MS]
        if big:
            lines.append(
                f"{_count(len(big), 'episode')} move by more than {_secs(BIG_MOVE_MS)}: check one before you send."
            )
    return {"lines": lines, "counts": counts}


def _plex_view(service, versions):
    """``(markers, hidden, errors)`` for these ``(version_id, meta)`` pairs: the markers Plex serves now per version
    (absent when Plex did not return that episode), the marker kinds each library hides, and why Plex could not be read."""
    entries = []
    for version_id, meta in versions:
        bindings = service._bindings(version_id)
        if bindings:
            identity, server_id, _ = bindings[0]
            entries.append((version_id, server_id, identity.rating_key, str(meta.get("library_id", ""))))
    markers, hidden, errors = {}, {}, set()
    for server_id in dict.fromkeys(e[1] for e in entries):
        group = [e for e in entries if e[1] == server_id]
        server = service.registry.get(server_id)
        if server is None:
            errors.add("Plex is not connected")
            continue
        found, error = {}, None
        keys = [e[2] for e in group]
        for start in range(0, len(keys), PLEX_BATCH):  # a Plex that does not answer is asked once, not once per batch
            part, error = read_plex_markers(server, keys[start : start + PLEX_BATCH])
            found.update(part)
            if error:
                break
        if error:
            errors.add(error)
        kinds = {library: _hidden_kinds(server, library) for library in {e[3] for e in group}}
        for version_id, _, rating_key, library in group:
            hidden[version_id] = kinds[library]
            if rating_key in found:
                markers[version_id] = found[rating_key]
    return markers, hidden, errors


def season_plan(service, show, season):
    """One season in detail: the sure ones, the ones that need the owner and the ones ready to send, each set against
    what Plex has now, and the plain account of what sending would change. Reads Plex (and only reads)."""
    rows = _tv_rows(service)
    titles = sorted({str(r["metadata"]["show_title"]) for r in rows.values()})
    title = _pick_show(titles, show)
    groups = _scan(service, title, season)
    group = groups.get((title, season)) or _empty()
    versions = {}
    for bucket in (SURE, NEEDS, "ready"):
        for item in group[bucket]:
            versions[item["media_version_id"]] = rows[item["media_version_id"]]["metadata"]
    markers, hidden, errors = groups.plex
    for bucket in (SURE, NEEDS):
        for item in group[bucket]:
            version_id = item["media_version_id"]
            item.update(
                _compare(
                    item["kind"], item["start_ms"], item["end_ms"], markers.get(version_id), hidden.get(version_id, ())
                )
            )
    ready_spans = []
    for item in group["ready"]:
        version_id = item["media_version_id"]
        for skip in item["skips"]:
            skip.update(
                _compare(
                    skip["kind"], skip["start_ms"], skip["end_ms"], markers.get(version_id), hidden.get(version_id, ())
                )
            )
            ready_spans.append(skip)
    unread = sum(1 for v in versions if v not in markers)
    plex = {
        "status": "unavailable" if versions and unread == len(versions) else "partly" if unread else "read",
        "message": "; ".join(sorted(errors)) or ("Some episodes are not in Plex any more." if unread else None),
    }
    return {
        "show": title,
        "season": season,
        "label": season_label(season),
        SURE: group[SURE],
        NEEDS: group[NEEDS],
        "ready": group["ready"],
        "plex": plex,
        "changes": {SURE: _changes(group[SURE]), "ready": _changes(ready_spans)},
        "sending": sending(service),
    }


# ── deciding one ─────────────────────────────────────────────────────────────────────────────────────────────────


def _hold_edits(ranges):
    """One edit per range that leaves it exactly as it is. A review that carries any edit takes back every approval it
    does not repeat, so a decision has to repeat all of them."""
    return {
        r.id: {
            "range_id": r.id,
            "action": r.action,
            "kind": r.kind,
            "start_ms": r.start_ms,
            "end_ms": r.end_ms,
            "review_status": r.review_status,
            "locked": r.locked,
            "reason": r.reason,
        }
        for r in ranges
    }


def decide(
    service,
    media_version_id,
    expected_revision,
    kind,
    decision,
    *,
    start_ms=None,
    end_ms=None,
    span_id=None,
    range_ids=(),
):
    """Settle one suggestion. ``skip`` sets the owner's own exact skip at ``start_ms``-``end_ms`` (what the owner saw,
    or moved); ``keep`` rejects the suggestions of this kind; ``restore`` takes either back (the ``undo`` that every
    answer carries). Returns the new revision and the ``undo`` that reverses exactly this decision."""
    if kind not in KINDS:
        raise ReviewRefused("Only an intro or a credits skip can be decided here.")
    stored = service.store.get_result(media_version_id)
    if stored is None:
        raise KeyError(media_version_id)
    edits = _hold_edits(stored.ranges)
    by_id = {r.id: r for r in stored.ranges}
    undo = {"decision": "restore", "kind": kind, "span_id": None, "range_ids": []}
    extra = []
    if decision == "skip":
        if type(start_ms) is not int or type(end_ms) is not int or not 0 <= start_ms < end_ms:
            raise ReviewRefused("Say where the skip starts and ends.")
        extra.append(
            {
                "range_id": None,
                "kind": kind,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "review_status": "approved",
                "acknowledge_owner_span": True,
            }
        )
    elif decision == "keep":
        pending = [
            r.id
            for r in stored.ranges
            if r.action == "skip" and r.kind == kind and r.review_status == "proposed" and r.origin != ORIGIN_PLEX
        ]
        if not pending:
            raise ReviewRefused("There is nothing left to decide for this one.")
        for rid in pending:
            edits[rid]["review_status"] = "rejected"
        undo["range_ids"] = pending
    elif decision == "restore":
        spans = [by_id[span_id]] if span_id in by_id else []
        if span_id is not None and not spans:
            raise ReviewRefused("That decision no longer exists.")
        for span in spans:
            if not is_owner_span(span) or span.kind != kind:
                raise ReviewRefused("That is not a skip you set.")
            edits[span.id]["review_status"] = "rejected"
        for rid in range_ids:
            if rid not in by_id or by_id[rid].kind != kind or by_id[rid].action != "skip":
                raise ReviewRefused("That suggestion no longer exists.")
            edits[rid]["review_status"] = "proposed"
    else:
        raise ReviewRefused("A decision is skip, keep or restore.")
    saved = service.review(media_version_id, expected_revision, [*edits.values(), *extra], OWNER)
    answer = {
        "media_version_id": media_version_id,
        "kind": kind,
        "decision": decision,
        "revision": saved.result_revision,
    }
    if decision == "skip":
        made = [r for r in saved.ranges if is_owner_span(r) and r.kind == kind and r.review_status == "approved"]
        undo["span_id"] = made[0].id if made else None
        answer.update(start_ms=start_ms, end_ms=end_ms, span_id=undo["span_id"])
    answer["undo"] = undo
    return answer


# ── sending ──────────────────────────────────────────────────────────────────────────────────────────────────────


def _owner_spans(result):
    return [r for r in result.ranges if is_owner_span(r) and r.review_status == "approved"]


def _backup(service, versions, now):
    """Make sure today's backup of Plex's database exists before anything is written. Raises ``ReviewRefused`` (with
    the reason, in plain words) when it cannot be made: no backup, no write."""
    config_dir = getattr(service.job_manager, "config_dir", None)
    databases, files = [], []
    for version_id in versions:
        bindings = service._bindings(version_id)
        cfg = service.registry.get_config(bindings[0][1]) if bindings else None
        database = plex_db.PlexMarkerPublisher.db_path_for(cfg) if cfg else None
        if not database or not config_dir:
            raise ReviewRefused("Nothing was sent. Reprise can't see Plex's database folder: check Settings, Sharing.")
        if database not in databases:
            databases.append(database)
    created = False
    for database in databases:
        try:
            path, made = plexbackup.ensure_daily_backup(database, plexbackup.backup_dir(config_dir), now=now)
        except plexbackup.BackupFailed as exc:
            raise ReviewRefused(f"Nothing was sent. Reprise could not back up Plex's database first. {exc}") from exc
        except Exception as exc:
            logger.opt(exception=True).warning("The backup of Plex's database failed unexpectedly")
            raise ReviewRefused("Nothing was sent. Plex's database could not be backed up first.") from exc
        created = created or made
        files.append(path)
    return {"created": created, "files": [f.rsplit("/", 1)[-1] for f in files]}


def _send_one(service, version_id):
    """Prepare and apply one episode through the publisher, as a manual send does. ``(state, detail)``: ``sent`` with
    the change, ``already`` (Plex holds exactly what was approved: nothing to send), ``failed`` with a reason (nothing
    reached Plex), ``unreachable`` (Plex could not be reached) or ``uncertain`` (nobody can tell whether Plex took it)."""
    publisher, store = service.publisher, service.store
    result = store.get_result(version_id)
    bindings = service._bindings(version_id)
    if result is None or not bindings:
        return "failed", "It is no longer in the library."
    try:
        change = publisher.prepare(version_id, result.result_revision, bindings[0][0], OWNER)
    except NothingToPublish:
        return "already", NOTHING_TO_SEND
    except Exception as exc:
        if _transient(exc):
            return "unreachable", "Plex could not be reached."
        return "failed", f"Plex's data did not allow it: {_sentence(exc)}."
    try:
        return "sent", publisher.apply(change["id"], OWNER)
    except Exception as exc:
        try:
            settled = publisher.recover(change["id"], OWNER)
        except Exception:
            logger.opt(exception=True).warning("Reprise could not tell whether a change reached Plex")
            return "uncertain", "Reprise can't tell whether Plex took it. Check it in the Library."
        if settled["state"] == "applied":
            return "sent", settled
        if settled["state"] in ("prepared", "not_applied"):
            if settled["state"] == "prepared":
                try:
                    publisher.abandon(change["id"], OWNER)
                except Exception:
                    logger.opt(exception=True).warning("Reprise could not close a change that never started")
            if _transient(exc):
                return "unreachable", "Plex could not be reached."
            return "failed", f"Plex refused it: {_sentence(exc)}."
        return "uncertain", "Reprise can't tell whether Plex took it. Check it in the Library."


def _episode(rows, version_id):
    meta = rows[version_id]["metadata"]
    return {"media_version_id": version_id, "episode": meta.get("episode"), "label": episode_label(meta)}


def send(service, show, season, scope, *, to_plex=True, now=None):
    """Approve and send one season. ``scope`` ``sure`` first approves the season's sure ones (and only those) as the
    owner's own exact skips; ``approved`` takes what the owner already approved. With ``to_plex`` they are then sent:
    Plex's database is backed up first (no backup, no write) and every change goes through the publisher's own checks,
    one episode at a time. Without it (sending is off) the sure ones are only approved. Returns what was approved,
    sent and left, with the ids ``undo`` needs. ``already`` lists the episodes where Plex holds exactly what was
    approved (nothing to send, not a failure); when that is all there was, ``message`` says so in plain words."""
    state = sending(service)
    if to_plex and not state["enabled"]:
        raise ReviewRefused(state["message"])
    if scope not in ("sure", "approved") or (scope == "approved" and not to_plex):
        raise ReviewRefused("Say whether to approve the sure ones or to send the ones you approved.")
    rows = _tv_rows(service)
    title = _pick_show(sorted({str(r["metadata"]["show_title"]) for r in rows.values()}), show)
    group = _scan(service, title, season).get((title, season)) or _empty()
    todo = sorted(
        {i["media_version_id"] for i in group[SURE if scope == "sure" else "ready"]},
        key=lambda v: (rows[v]["metadata"].get("episode") or 0, v),
    )
    already = []
    if scope == "approved":  # approved earlier, and Plex serves exactly that now: the scan took them out of the queue
        already = [
            {**_episode(rows, i["media_version_id"]), "kinds": sorted({s["kind"] for s in i["skips"]})}
            for i in group["already"]
        ]
    if not todo and not already:
        raise ReviewRefused(
            "There is nothing sure left to approve in this season."
            if scope == "sure"
            else "Nothing in this season is waiting to be sent."
        )
    backup = _backup(service, todo, now) if to_plex and todo else None
    approved, failed, spans_of = [], [], {}
    if scope == "sure":
        outcome = service.approve_confident(title, season)
        failed = [{**_episode(rows, s["id"]), "reason": s["reason"]} for s in outcome["skipped"]]
        kinds_of = {}
        for entry in outcome["approved"]:
            kinds_of.setdefault(entry["id"], set()).add(entry["kind"])
        for version_id, kinds in kinds_of.items():
            after = service.store.get_result(version_id)
            spans_of[version_id] = [r.id for r in _owner_spans(after) if r.kind in kinds] if after else []
            approved.append({**_episode(rows, version_id), "kinds": sorted(kinds), "span_ids": spans_of[version_id]})
        todo = [version_id for version_id in todo if version_id in kinds_of]
    else:
        kinds_of = {i["media_version_id"]: {s["kind"] for s in i["skips"]} for i in group["ready"]}
    sent, stopped = [], None
    for number, version_id in enumerate(todo if to_plex else ()):
        status, detail = _send_one(service, version_id)
        if status == "sent":
            sent.append(
                {
                    **_episode(rows, version_id),
                    "kinds": sorted(kinds_of.get(version_id, ())),
                    "change_id": detail["id"],
                    "span_ids": spans_of.get(version_id, []),
                }
            )
            continue
        if status == "already":
            already.append({**_episode(rows, version_id), "kinds": sorted(kinds_of.get(version_id, ()))})
            continue
        failed.append({**_episode(rows, version_id), "reason": detail})
        if status in ("unreachable", "uncertain"):
            stopped = detail
            failed += [
                {**_episode(rows, v), "reason": "Not tried: sending stopped before it."} for v in todo[number + 1 :]
            ]
            break
    return {
        "show": title,
        "season": season,
        "scope": scope,
        "approved": approved,
        "sent": sent,
        "failed": failed,
        "already": already,
        "stopped": stopped,
        "backup": backup,
        "message": NOTHING_TO_SEND if already and not (approved or sent or failed) else None,
    }


def undo(service, changes):
    """Take back what ``send`` did. ``changes`` is the ``sent`` entries it returned (Plex gets the markers it had
    before and the approval the click made is withdrawn so they wait again) or its ``approved`` entries when nothing
    was sent (only the approval is withdrawn). An episode that cannot be put back is reported and left alone."""
    publisher = service.publisher
    if publisher is None and any(change.get("change_id") for change in changes):
        raise ReviewRefused("Sending to Plex is not available on this install.")
    restored, failed = [], []
    for change in reversed(changes):  # newest first: each change's undo expects Plex as that change left it
        version_id, change_id = change["media_version_id"], change.get("change_id")
        label = change.get("label") or "an episode"
        if change_id:
            try:
                publisher.undo(change_id, OWNER)
            except Exception as exc:
                failed.append({"media_version_id": version_id, "label": label, "reason": _undo_reason(exc)})
                continue
        withdrawn = True
        for span_id in change.get("span_ids", []):
            stored = service.store.get_result(version_id)
            span = next((r for r in (stored.ranges if stored else ()) if r.id == span_id), None)
            if span is None or span.review_status != "approved":
                continue
            try:
                decide(service, version_id, stored.result_revision, span.kind, "restore", span_id=span_id)
            except Exception as exc:
                logger.warning("Reprise could not withdraw an approval after an undo: {}", exc)
                withdrawn = False
        restored.append({"media_version_id": version_id, "label": label, "withdrawn": withdrawn})
    restored.reverse()
    failed.reverse()
    return {"restored": restored, "failed": failed}


def _undo_reason(exc):
    if _transient(exc):
        return "Plex could not be reached."
    return f"It could not be put back: {_sentence(exc)}."


# ── what Reprise sent by itself ──────────────────────────────────────────────────────────────────────────────────


def recent_automatic(service, *, since=None, limit=50):
    """What Reprise sent to Plex by itself that is still in Plex, newest first: ``[entry]`` for each applied change the
    journal names Reprise as the actor of (a later undo takes it out of the list). ``since`` is the earliest ``sent_at``
    (seconds since 1970), ``limit`` the most entries. An entry is ``{"change_id", "media_version_id", "show", "season",
    "episode", "label", "kinds", "sent_at", "span_ids", "origin"}``. ``span_ids`` are the exact skips to withdraw when
    the change is undone (what ``undo`` takes along with ``change_id``): the ones Reprise set from the local model's
    suggestions (``origin`` ``local_model``), none for a skip the detector confirmed (``origin`` ``sure``)."""
    if service.publisher is None:
        return []
    sql = (
        "SELECT id,media_version_id,updated_at,json_extract(payload,'$.projection') AS projection,"
        "json_extract(payload,'$.canonical') AS canonical FROM reprise_publication_changes "
        "WHERE state='applied' AND actor=?"
    )
    args = [AUTO_ACTOR]
    if since is not None:
        sql += " AND updated_at>=?"
        args.append(since)
    with service.store.marker_store._reading() as conn:
        rows = conn.execute(sql + " ORDER BY updated_at DESC,rowid DESC LIMIT ?", (*args, limit)).fetchall()
    found = []
    for row in rows:
        version_id = row["media_version_id"]
        kinds = {kind for kind, _start, _end in json.loads(row["projection"] or "[]")}
        entry = next((e for e in json.loads(row["canonical"] or "[]") if e["version"]["id"] == version_id), None)
        spans = [
            r["id"]
            for r in (entry["result"]["ranges"] if entry else ())
            if r["origin"] == "owner" and r["action"] == "skip" and r["review_status"] == "approved"
            if r["kind"] in kinds
        ]
        bindings = service._bindings(version_id)
        meta = bindings[0][2] if bindings else {}
        found.append(
            {
                "change_id": row["id"],
                "media_version_id": version_id,
                "show": meta.get("show_title"),
                "season": meta.get("season"),
                "episode": meta.get("episode"),
                "label": episode_label(meta),
                "kinds": [kind for kind in KINDS if kind in kinds],
                "sent_at": row["updated_at"],
                "span_ids": spans,
                "origin": "local_model" if spans else "sure",
            }
        )
    return found
