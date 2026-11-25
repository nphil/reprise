"""Sending the sure matches to Plex without being asked.

Reprise sends an episode's intro or credits to Plex by itself only when everything below holds. Anything else waits
for your approval, exactly as before.

* The analysis is complete and current, and the episode is a TV episode (not a special, never a film).
* The range is a skip the detector confirmed by picture *and* sound. A Plex or online suggestion never qualifies, and
  neither does a gap that was only filled in between two confirmed pieces.
* It is one unbroken run: the confirmed pieces of a kind must touch exactly. Nothing you approved, changed, locked or
  keep touches it.
* It passes every check a manual send passes: it is approved, prepared and applied through the same publisher, and a
  dry run of Plex's own exact-projection rule must give back exactly what was found.
* Nothing was ever sent, undone or left unfinished for that episode before: one automatic send per episode.

Only the confirmed kind is replaced: sure credits leave Plex's intro exactly as it is, and the reverse. One episode is
sent at a time. Every automatic change is journalled and can be undone from the report. Before the first automatic
write of each day Plex's database is backed up; no backup, no write.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import replace

from loguru import logger

from ..markers.publishers import plex_db
from ..shutdown import is_shutting_down
from . import plexbackup
from .models import ORIGIN_OWNER, REASON_OWNER_SPAN, DecisionRange, NothingToPublish, ReviewRefused, is_owner_span
from .policy import eligible_ranges
from .publishing import AUTO_ACTOR, owner_sentence
from .review import plex_projection
from .store import RevisionConflict
from .verification_profile import POLICY_VERSION

KINDS = ("intro", "credits")
LOOKS_PER_PASS = 40  # most episodes looked at in one pass
SENDS_PER_PASS = 3  # most episodes sent in one pass
RECHECK_S = 30 * 60  # how long before an analysis that was not ready is looked at again
FAILED_RETRY_S = 6 * 3600  # how long before a send that failed is tried again
FAILED_TRIES = 3  # after this many failures an episode is left for the owner
REFUSED_PAUSE_S = 30 * 60  # no automatic sending for this long after Plex refused a change
REPEATED_REFUSALS = 3  # this many refusals in a row mean something is wrong
REPEATED_REFUSAL_PAUSE_S = 6 * 3600
UNCERTAIN_PAUSE_S = 6 * 3600
PLEX_RETRY_S = 5 * 60  # how long to wait when Plex could not be reached
BACKUP_RETRY_S = 30 * 60  # how long before a failed backup is tried again

REFUSED_KEY = "plex_refused"
BACKUP_KEY = "backup_failed"
UNCERTAIN_KEY = "plex_uncertain"  # followed by the episode: each such change is reported once and never "fixed"


def _attempts_ddl(table):
    return (
        f"CREATE TABLE IF NOT EXISTS {table} ("
        "version_id TEXT NOT NULL, revision INTEGER NOT NULL, outcome TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', "
        "range_ids TEXT NOT NULL DEFAULT '[]', change_id TEXT, at REAL NOT NULL, retry_after REAL, "
        "tries INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (version_id, revision))"
    )


_ATTEMPTS_TABLE = "reprise_autopilot_attempts"
_LOCAL_ATTEMPTS_TABLE = "reprise_autopilot_local_attempts"


def _is_sure(item, decided):
    """A skip the detector confirmed by picture and sound that nobody has touched. A hole filled between two
    confirmed pieces ("bridged") was not itself compared, so it is not sure."""
    return (
        item.origin == "detector"
        and item.action == "skip"
        and item.review_status == "proposed"
        and not item.locked
        and item.id not in decided
        and not item.reason_code.endswith("bridged")
    )


def _meets(start, end, first, last):
    return start < last and first < end


def sure_runs(result, decided, keeps):
    """What Reprise may send by itself from one result, as ``({kind: pieces}, notes)``.

    ``decided`` is the set of range ids that are the owner's and ``keeps`` the ``(start, end)`` of every scene the
    owner keeps. A kind is in ``pieces`` (ordered) only when its confirmed pieces make one unbroken run that none of
    the owner's choices touches; ``notes`` say, in plain words, why a kind or the whole result is not."""
    if any(is_owner_span(r) for r in result.ranges):
        # An exact skip the owner set is theirs alone: it is never sent, replaced or worked around automatically.
        return {}, ["You set a skip yourself, so sending it is up to you."]
    if any(r.action == "skip" and r.review_status == "approved" for r in result.ranges):
        return {}, ["You already approved part of it, so sending it is up to you."]
    runs, notes = {}, []
    for kind in KINDS:
        sure = sorted(
            (r for r in result.ranges if r.kind == kind and _is_sure(r, decided)), key=lambda r: (r.start_ms, r.id)
        )
        if not sure:
            continue
        if any(a.end_ms != b.start_ms for a, b in zip(sure, sure[1:], strict=False)):
            notes.append(f"The confirmed {kind} has gaps in it, so it needs your approval.")
            continue
        first, last = sure[0].start_ms, sure[-1].end_ms
        if any(
            (r.locked or r.id in decided) and _meets(r.start_ms, r.end_ms, first, last) for r in result.ranges
        ) or any(_meets(start, end, first, last) for start, end in keeps):
            notes.append(f"You changed, locked or keep something in the {kind}, so it needs your approval.")
            continue
        runs[kind] = sure
    return runs, notes


def label_of(meta):
    """An episode as a person names it ("Show S01E02"), from its stored metadata."""
    show, season, episode = meta.get("show_title"), meta.get("season"), meta.get("episode")
    if show and isinstance(season, int) and isinstance(episode, int):
        return f"{show} S{season:02d}E{episode:02d}"
    return str(meta.get("title") or show or "an episode")


def _transient(exc):
    """True for a failure that says nothing about the episode itself: Plex or its database was busy or unreachable."""
    return isinstance(exc, OSError | sqlite3.OperationalError)


class AutoSender:
    """Looks at the episodes whose analysis changed and sends the sure ones, one at a time."""

    TABLE = _ATTEMPTS_TABLE  # where this sender writes down what it decided (a fixed name, never request data)

    def __init__(self, service, concerns, state, *, config_dir, enabled=lambda: True, clock=time.time, backup=None):
        self.service, self.concerns, self.state = service, concerns, state
        self.config_dir = config_dir
        self._enabled = enabled
        self._clock = clock
        self._backup = backup or plexbackup.ensure_daily_backup
        self._backup_retry_at, self._backup_error = 0.0, ""
        with service.store.transaction() as conn:
            conn.execute(_attempts_ddl(self.TABLE))

    # ── whether it may send at all ────────────────────────────────────────────────────────────────────────────────

    def blocked(self, now=None):
        """A plain sentence that says why nothing is being sent automatically right now, or None while it can."""
        now = self._clock() if now is None else now
        publisher = self.service.publisher
        if not self._enabled():
            return "Automatic sending is switched off."
        if publisher is None or not publisher.writes_enabled:
            return "Sending to Plex is switched off, so nothing is sent."
        if publisher.unfinished_writes():
            return "A change to Plex was cut off and has to be checked first."
        if now < self._backup_retry_at:
            return "Waiting: Reprise could not back up Plex's database."
        pause = self.state.get("send_pause")
        if pause and now < pause["until"]:
            return pause["why"]
        return None

    def _pause(self, now, seconds, why):
        self.state.put("send_pause", {"until": now + seconds, "why": why})

    # ── the pass ──────────────────────────────────────────────────────────────────────────────────────────────────

    def sweep(self, now=None):
        """Look at the episodes whose analysis changed and send the sure ones. Returns how many were sent."""
        now = self._clock() if now is None else now
        self._finish_interrupted(now)
        if self.blocked(now):
            return 0
        sent = 0
        for version_id, revision in self._pending(now, LOOKS_PER_PASS):
            if is_shutting_down() or self.blocked(now):
                break
            try:
                outcome = self._consider(version_id, revision, now)
            except Exception:
                logger.opt(exception=True).warning(
                    "Reprise could not decide whether to send an episode to Plex by itself. It will look again later"
                )
                self._retry(version_id, revision, "Something unexpected stopped the check.", now)
                continue
            if outcome == "sent":
                sent += 1
                if sent >= SENDS_PER_PASS:
                    break
        return sent

    def _pending(self, now, limit):
        """``[(episode, revision)]`` of the complete results nobody has looked at yet, or whose look is due again."""
        with self.service.store.marker_store._reading() as conn:
            rows = conn.execute(
                "SELECT r.version_id AS version_id, r.revision AS revision FROM reprise_decision_results r "
                f"LEFT JOIN {self.TABLE} a ON a.version_id=r.version_id AND a.revision=r.revision "
                "WHERE r.status='complete' AND r.policy_version=? "
                "AND (a.version_id IS NULL OR (a.outcome IN ('retry','failed') AND a.retry_after<=?)) "
                "AND EXISTS (SELECT 1 FROM reprise_bindings b WHERE b.version_id=r.version_id AND b.active=1) "
                "ORDER BY r.rowid LIMIT ?",
                (POLICY_VERSION, now, limit),
            ).fetchall()
        return [(row["version_id"], row["revision"]) for row in rows]

    # ── one episode ───────────────────────────────────────────────────────────────────────────────────────────────

    def _consider(self, version_id, revision, now):
        """Decide about one episode and send it when it is sure. Returns ``sent``, ``already`` (Plex holds exactly
        what was found), ``declined``, ``retry``, ``failed`` or ``waiting`` (nothing could be decided now; nothing was
        recorded)."""
        vetted = self._vet(version_id, revision, now)
        if isinstance(vetted, str):
            return vetted
        result, bindings, meta = vetted
        return self._decide(version_id, revision, result, bindings, meta, now)

    def _vet(self, version_id, revision, now):
        """``(result, bindings, meta)`` of an episode that may be looked at, or the outcome (already recorded) of why
        it may not: a film, a special, an analysis that is not current, an episode already sent, undone or unfinished."""
        service, store, publisher = self.service, self.service.store, self.service.publisher
        result = store.get_result(version_id)
        if result is None or result.result_revision != revision:
            return self._retry(version_id, revision, "It changed while Reprise was looking at it.", now, wait=60)
        version, bindings = store.get_media(version_id), service._bindings(version_id)
        if not version or not bindings:
            return self._retry(version_id, revision, "It is not in the library right now.", now)
        meta = bindings[0][2]
        if meta.get("kind") == "movie":
            return self._decline(version_id, revision, "Films are never sent to Plex automatically.", now)
        if meta.get("season") == 0:
            return self._decline(version_id, revision, "Specials are never sent to Plex automatically.", now)
        if (
            result.analysis_status != "complete"
            or result.freshness != "current"
            or not service._references_current(result)
            or not all(service._valid_binding(version, *b) for b in bindings)
        ):
            return self._retry(version_id, revision, "Its analysis is not current yet.", now)
        held = publisher.automatic_hold(version_id)
        if held:
            return self._decline(version_id, revision, held, now)
        return result, bindings, meta

    def _decide(self, version_id, revision, result, bindings, meta, now):
        """The sure matches of one vetted episode, sent when Plex can take them exactly as they were found."""
        store = self.service.store
        runs, notes = sure_runs(result, store.owner_decided_ids(version_id), store.owner_keep_ranges(version_id))
        if not runs:
            return self._decline(
                version_id,
                revision,
                " ".join(notes) or "Reprise isn't sure about any of it.",
                now,
            )
        return self._dispatch(version_id, revision, result, bindings, meta, runs, now)

    def _dispatch(self, version_id, revision, result, bindings, meta, runs, now):
        """Send ``runs`` (``{kind: pieces}``) when Plex can take them exactly and today's backup exists."""
        kinds, why = self._projectable(result, bindings, runs)
        if not kinds:
            return self._decline(version_id, revision, why, now)
        cfg = self.service.registry.get_config(bindings[0][1])
        database = plex_db.PlexMarkerPublisher.db_path_for(cfg) if cfg else None
        if not database:
            return self._decline(version_id, revision, "Reprise can't see Plex's database folder.", now)
        if not self._backed_up(database, now):
            return "waiting"
        pieces = [piece for kind in kinds for piece in runs[kind]]
        return self._send(version_id, revision, bindings[0][0], label_of(meta), kinds, pieces, now)

    def _projectable(self, result, bindings, runs):
        """``(kinds, why)``: the kinds Plex could take exactly as they were found, and the reason when none can."""
        good, why = [], []
        for kind in KINDS:
            if kind not in runs:
                continue
            try:
                self._shape(result, bindings, runs, [kind])
                good.append(kind)
            except ValueError as exc:
                why.append(f"The {kind} can't be sent. {owner_sentence(exc)}")
        if len(good) > 1:
            try:
                self._shape(result, bindings, runs, good)
            except ValueError:
                good = good[:1]
        return good, " ".join(why) or "Plex can't take it exactly."

    @staticmethod
    def _shape(result, bindings, runs, kinds):
        """A dry run of the publisher's own rule: with only these runs approved, every audio track must project to
        exactly what was found. Raises ``ValueError`` when it does not. Nothing is stored."""
        approved = {piece.id for kind in kinds for piece in runs[kind]}
        trial = replace(
            result,
            ranges=tuple(replace(r, review_status="approved") if r.id in approved else r for r in result.ranges),
        )
        shapes = set()
        for identity, _server, _meta in bindings:
            projected = plex_projection(trial, identity, eligible_ranges(trial, identity.audio_stream_id))
            shapes.add(tuple((p.kind, p.start_ms, p.end_ms) for p in projected))
        if len(shapes) != 1:
            raise ValueError("The audio tracks disagree about where it is")
        found = {kind: (runs[kind][0].start_ms, runs[kind][-1].end_ms) for kind in kinds}
        if {kind: (start, end) for kind, start, end in next(iter(shapes))} != found:
            raise ValueError("Plex would store it a little differently from what Reprise found")

    # ── the backup ────────────────────────────────────────────────────────────────────────────────────────────────

    def _backed_up(self, database, now):
        """True when today's backup of Plex's database exists (made now if need be); when it cannot be made, nothing
        is written, the owner is told once, and the next try is later."""
        if now < self._backup_retry_at:
            return False
        try:
            self._backup(database, plexbackup.backup_dir(self.config_dir), now=now)
        except Exception as exc:
            reason = str(exc) if isinstance(exc, plexbackup.BackupFailed) else "Plex's database could not be backed up."
            if not isinstance(exc, plexbackup.BackupFailed):
                logger.opt(exception=True).warning("The backup of Plex's database failed unexpectedly")
            self._backup_error = reason
            self._backup_retry_at = now + BACKUP_RETRY_S
            self.concerns.raise_problem(
                BACKUP_KEY,
                "Plex backup failed",
                f"Reprise could not back up Plex's database, so it sent nothing to Plex. {reason} It will try again soon.",
            )
            return False
        self._backup_retry_at = 0.0
        self.concerns.resolve(BACKUP_KEY, "Plex's database was backed up again. Automatic sending works again.")
        return True

    # ── the write ─────────────────────────────────────────────────────────────────────────────────────────────────

    def _send(self, version_id, revision, identity, label, kinds, pieces, now):
        # Written first, so a restart in the middle of the steps below can find what was approved and take it back.
        earlier = self._attempt(version_id, revision)
        tries = earlier["tries"] if earlier else 0
        self._record(version_id, revision, "working", now=now, tries=tries, range_ids=[p.id for p in pieces])
        try:
            ids, new_revision = self._approve(version_id, revision, pieces, now)
        except ReviewRefused as exc:
            return self._decline(version_id, revision, f"It can't be sent. {owner_sentence(exc)}", now)
        except (RevisionConflict, ValueError):
            return self._retry(version_id, revision, "It changed while Reprise was about to send it.", now, wait=60)
        return self._publish(version_id, revision, new_revision, identity, label, kinds, ids, now)

    def _approve(self, version_id, revision, pieces, now):
        """Approve the confirmed pieces; ``(ids, the episode's revision afterwards)``."""
        ids = [piece.id for piece in pieces]
        return ids, self.service.store.auto_approve(version_id, revision, ids).result_revision

    def _publish(self, version_id, revision, new_revision, identity, label, kinds, ids, now):
        """Prepare and apply what was approved, the journal naming Reprise as the actor; put it all back when it does
        not reach Plex."""
        publisher = self.service.publisher
        try:
            change = publisher.prepare(version_id, new_revision, identity, AUTO_ACTOR)
        except NothingToPublish:
            # Plex holds exactly what was approved: there is nothing to send and nothing went wrong.
            back = self._take_back(version_id, new_revision, ids)
            return self._already(version_id, (revision, back), now)
        except Exception as exc:
            # Nothing was written to Plex: take the approval back and leave the episode as it was.
            back = self._take_back(version_id, new_revision, ids)
            if _transient(exc):
                return self._wait_for_plex(version_id, revision, now)
            return self._fail(version_id, (revision, back), owner_sentence(exc), now)
        try:
            publisher.apply(change["id"], AUTO_ACTOR)
        except Exception as exc:
            return self._after_failed_apply(version_id, revision, new_revision, ids, change["id"], label, exc, now)
        self._sent(version_id, revision, new_revision, change["id"], ids, now)
        logger.info("Reprise sent the {} of {} to Plex automatically", " and ".join(kinds), label)
        return "sent"

    def _after_failed_apply(self, version_id, revision, new_revision, ids, change_id, label, exc, now):
        """The write raised. Plex is only ever read from here: ask the journal where the change stands."""
        publisher = self.service.publisher
        try:
            settled = publisher.recover(change_id, AUTO_ACTOR)["state"]
        except Exception:
            logger.opt(exception=True).warning("Reprise could not tell whether an automatic change reached Plex")
            settled = "uncertain"
        if settled == "applied":
            self._sent(version_id, revision, new_revision, change_id, ids, now)
            return "sent"
        if settled in ("prepared", "not_applied"):
            if settled == "prepared":
                try:
                    publisher.abandon(change_id, AUTO_ACTOR)
                except Exception:
                    logger.opt(exception=True).warning("Reprise could not close an automatic change that never started")
            back = self._take_back(version_id, new_revision, ids)
            if _transient(exc):
                return self._wait_for_plex(version_id, revision, now)
            self._refused(label, now)
            return self._fail(version_id, (revision, back), owner_sentence(exc), now)
        # Plex may or may not hold the change. Nothing is repeated or undone from here; the owner decides.
        self._uncertain(version_id, revision, change_id, ids, now)
        return "uncertain"

    def _refused(self, label, now):
        """Plex turned a change down: pause for a while (longer when it keeps happening) and tell the owner once."""
        refusals = self.state.get("send_refusals", 0) + 1
        self.state.put("send_refusals", refusals)
        repeated = refusals >= REPEATED_REFUSALS
        wait = REPEATED_REFUSAL_PAUSE_S if repeated else REFUSED_PAUSE_S
        self._pause(now, wait, "Paused for a while because Plex refused an automatic change.")
        self.concerns.raise_problem(
            REFUSED_KEY,
            "Plex refused an automatic change",
            f"Plex refused a change Reprise tried to send automatically for {label}. Nothing in Plex was changed. "
            f"Reprise paused automatic sending for {'6 hours' if repeated else '30 minutes'}.",
        )

    def _uncertain(self, version_id, revision, change_id, ids, now):
        """Nobody can tell whether Plex took a change: it is not repeated or undone, sending pauses for a while and the
        owner is told once (a later change that Plex accepts does not make this one certain, so it never clears)."""
        self._record(
            version_id,
            revision,
            "uncertain",
            "Reprise can't tell whether Plex took it.",
            now=now,
            range_ids=ids,
            change_id=change_id,
        )
        self._pause(now, UNCERTAIN_PAUSE_S, "Paused for a while because Reprise can't tell whether Plex took a change.")
        bindings = self.service._bindings(version_id)
        label = label_of(bindings[0][2]) if bindings else "an episode"
        self.concerns.raise_problem(
            f"{UNCERTAIN_KEY}:{version_id}",
            "Check an automatic change in Plex",
            f"Reprise can't tell whether Plex took the automatic change for {label}. It paused automatic sending for "
            "6 hours and won't send it again. Check the episode in Reprise.",
        )

    def _wait_for_plex(self, version_id, revision, now):
        """Plex or its database could not be reached: nothing is recorded, so the episode comes up again soon."""
        self._pause(now, PLEX_RETRY_S, "Waiting a few minutes because Plex couldn't be reached.")
        with self.service.store.transaction() as conn:
            conn.execute(
                f"DELETE FROM {self.TABLE} WHERE version_id=? AND revision=? AND outcome='working'",
                (version_id, revision),
            )
        return "waiting"

    def _take_back(self, version_id, revision, ids):
        """Undo the automatic approval of ranges that were not sent after all (never an owner's own decision). Returns
        the revision the episode is at afterwards (taking an approval back moves it on)."""
        try:
            self.service.store.auto_unapprove(version_id, revision, ids)
        except RevisionConflict:
            logger.info("Reprise left an automatic approval alone: the episode was changed by hand since")
        except Exception:
            logger.opt(exception=True).warning("Reprise could not take back an automatic approval")
        result = self.service.store.get_result(version_id)
        return result.result_revision if result else revision

    def _already(self, version_id, revisions, now):
        """Plex holds exactly what was found, so there is nothing to send and nothing went wrong: done for these
        revisions, with no alert and no retry."""
        for revision in dict.fromkeys(revisions):
            self._record(version_id, revision, "already", "Plex already has exactly this skip.", now=now)
        return "already"

    def _sent(self, version_id, revision, new_revision, change_id, ids, now):
        for number in (revision, new_revision):
            self._record(version_id, number, "sent", now=now, range_ids=ids, change_id=change_id)
        self.state.put("send_refusals", 0)
        self.concerns.resolve(REFUSED_KEY, "Plex accepted an automatic change again. Nothing more to do.")

    # ── after a restart ───────────────────────────────────────────────────────────────────────────────────────────

    def _finish_interrupted(self, now):
        with self.service.store.marker_store._reading() as conn:
            rows = conn.execute(
                f"SELECT version_id,revision,range_ids FROM {self.TABLE} WHERE outcome='working'"
            ).fetchall()
        for row in rows:
            try:
                self._finish_one(row["version_id"], row["revision"], json.loads(row["range_ids"]), now)
            except Exception:
                logger.opt(exception=True).warning("Reprise could not finish an automatic send that was cut off")

    def _finish_one(self, version_id, revision, ids, now):
        """An automatic send was cut off by a restart. Plex is only read: a change that never started is closed, one
        that was being written is classified, and an approval that went nowhere is taken back."""
        publisher = self.service.publisher
        mine = [
            c for c in publisher.list_changes(version_id) if c["actor"] == AUTO_ACTOR and c["revision"] == revision + 1
        ]
        change = mine[-1] if mine else None
        state = change["state"] if change else None
        if state == "applying":
            state = publisher.recover(change["id"], AUTO_ACTOR)["state"]
        if state == "prepared":
            publisher.abandon(change["id"], AUTO_ACTOR)
            state = "not_applied"
        if state == "applied":
            self._sent(version_id, revision, revision + 1, change["id"], ids, now)
        elif state in (None, "not_applied"):
            back = self._take_back(version_id, revision + 1, ids)
            self._fail(version_id, (revision, back), "It was cut off before it reached Plex.", now)
        else:
            self._uncertain(version_id, revision, change["id"], ids, now)

    # ── what was decided ──────────────────────────────────────────────────────────────────────────────────────────

    def _attempt(self, version_id, revision):
        with self.service.store.marker_store._reading() as conn:
            return conn.execute(
                f"SELECT outcome,tries FROM {self.TABLE} WHERE version_id=? AND revision=?",
                (version_id, revision),
            ).fetchone()

    def _record(
        self, version_id, revision, outcome, detail="", *, now, wait=None, tries=0, range_ids=(), change_id=None
    ):
        with self.service.store.transaction() as conn:
            conn.execute(
                f"INSERT OR REPLACE INTO {self.TABLE} VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    version_id,
                    revision,
                    outcome,
                    detail,
                    json.dumps(list(range_ids)),
                    change_id,
                    now,
                    None if wait is None else now + wait,
                    tries,
                ),
            )

    def _decline(self, version_id, revision, detail, now):
        """Final for this revision: it is left for the owner to approve."""
        self._record(version_id, revision, "declined", detail, now=now)
        return "declined"

    def _retry(self, version_id, revision, detail, now, wait=RECHECK_S):
        self._record(version_id, revision, "retry", detail, now=now, wait=wait)
        return "retry"

    def _fail(self, version_id, revisions, detail, now):
        """A send that did not work: tried again later, and left for the owner after a few failures. ``revisions`` are
        the numbers the episode went through (taking an approval back moves its revision on): all carry the record."""
        row = self._attempt(version_id, revisions[0])
        tries = (row["tries"] if row else 0) + 1
        final = tries >= FAILED_TRIES
        for revision in dict.fromkeys(revisions):
            if final:
                self._record(version_id, revision, "declined", detail, now=now, tries=tries)
            else:
                self._record(version_id, revision, "failed", detail, now=now, wait=FAILED_RETRY_S, tries=tries)
        return "declined" if final else "failed"


# ── the local vision model's high-confidence skips ───────────────────────────────────────────────────────────────

# Whether the intro switch ("Send high-confidence intros to Plex automatically") is offered at all. It stays False until
# the local model's intros have been graded and none was unsafe; while False no intro is ever sent from the model.
INTRO_AUTOSEND_SAFE = False

LOCAL_FAILED_KEY = "local_send_failed"  # followed by the episode


def local_runs(result, kinds, decided):
    """What Reprise may send by itself from the local vision model's High-confidence suggestions in one result, as
    ``({kind: [pick]}, notes)``; ``decided`` is the set of range ids that are the owner's.

    Only the proposed skips the model alone suggested and rated High (``service._confident_local``) of the offered
    ``kinds``, one per kind. Nothing at all when the owner already approved anything (a send would carry it along).
    A kind is left alone when the owner set, took back or turned down a skip for it, or when anything the analysis or
    the owner keeps or locked overlaps the pick; ``notes`` say why, in plain words."""
    from .service import _confident_local

    if any(r.action == "skip" and r.review_status == "approved" for r in result.ranges):
        return {}, ["You already approved something in it, so sending it is up to you."]
    confident = _confident_local(result.ranges, result.evidence)
    picks, notes = {}, []
    for kind in kinds:
        found = [r for r in confident if r.kind == kind]
        if not found:
            continue
        if any(is_owner_span(r) and r.kind == kind for r in result.ranges):
            notes.append(f"You set or took back the {kind} skip yourself, so it is up to you.")
        elif any(r.action == "skip" and r.kind == kind and r.review_status == "rejected" for r in result.ranges):
            notes.append(f"You turned down the {kind}, so it is left alone.")
        elif len(found) != 1:
            notes.append(f"The AI model gave more than one {kind}, so it needs your approval.")
        elif any(
            (r.action == "keep" or r.locked or r.id in decided) and _meets(r.start_ms, r.end_ms, *_span(found[0]))
            for r in result.ranges
        ):
            notes.append(f"Something is kept, locked or decided inside the {kind}, so it needs your approval.")
        else:
            picks[kind] = found
    return picks, notes


def _span(item):
    return item.start_ms, item.end_ms


class LocalModelSender(AutoSender):
    """Sends the local vision model's High-confidence endings (and, once offered, intros) to Plex by itself.

    Each pick is first made the owner's own exact skip, through the same review the owner's "approve the confident
    ones" uses but with Reprise (``AUTO_ACTOR``) as the actor, and then published like any other automatic change, so
    the journal, the hold on a second automatic send and the report's Undo all recognise it. A pass that goes wrong
    after that takes the skip away again and tells the owner. It shares the pause, backup and refusal rules of the
    sender of sure matches, and keeps its own record of what it decided (it looks at results that sender already
    declined)."""

    TABLE = _LOCAL_ATTEMPTS_TABLE

    def __init__(self, service, concerns, state, *, config_dir, kinds, clock=time.time, backup=None):
        self._kinds = kinds
        self._alerted = False
        super().__init__(
            service,
            concerns,
            state,
            config_dir=config_dir,
            enabled=lambda: bool(self._offered()),
            clock=clock,
            backup=backup,
        )

    def _offered(self):
        """The kinds that may be sent now: what the owner switched on, and never an intro while those are not safe."""
        return tuple(kind for kind in self._kinds() if kind == "credits" or INTRO_AUTOSEND_SAFE)

    def _decide(self, version_id, revision, result, bindings, meta, now):
        picks, notes = local_runs(result, self._offered(), self.service.store.owner_decided_ids(version_id))
        if not picks:
            return self._decline(
                version_id,
                revision,
                " ".join(notes) or "The AI model isn't sure about any skip for it.",
                now,
            )
        tracks = tuple(sorted({identity.audio_stream_id for identity, _server, _meta in bindings}))
        runs = {
            kind: [
                DecisionRange(
                    f"auto-{kind}",
                    kind,
                    "skip",
                    pick[0].start_ms,
                    pick[0].end_ms,
                    "approved",
                    True,
                    ORIGIN_OWNER,
                    REASON_OWNER_SPAN,
                    "Set by you",
                    (),
                    tracks,
                )
            ]
            for kind, pick in picks.items()
        }
        return self._dispatch(version_id, revision, result, bindings, meta, runs, now)

    @staticmethod
    def _shape(result, bindings, runs, kinds):
        """The dry run with the exact skips these runs would become added to the result, as the publisher will see them."""
        spans = tuple(span for kind in kinds for span in runs[kind])
        AutoSender._shape(replace(result, ranges=(*result.ranges, *spans)), bindings, runs, kinds)

    def _approve(self, version_id, revision, pieces, now):
        from .approvals import _hold_edits

        store = self.service.store
        result = store.get_result(version_id)
        if result is None or result.result_revision != revision:
            raise RevisionConflict("result changed")
        edits = list(_hold_edits(result.ranges).values())
        edits += [
            {
                "range_id": None,
                "kind": piece.kind,
                "start_ms": piece.start_ms,
                "end_ms": piece.end_ms,
                "review_status": "approved",
                "acknowledge_owner_span": True,
            }
            for piece in pieces
        ]
        saved = self.service.review(version_id, revision, edits, AUTO_ACTOR)
        kinds = {piece.kind for piece in pieces}
        ids = [r.id for r in saved.ranges if is_owner_span(r) and r.review_status == "approved" and r.kind in kinds]
        earlier = self._attempt(version_id, revision)
        self._record(version_id, revision, "working", now=now, tries=earlier["tries"] if earlier else 0, range_ids=ids)
        return ids, saved.result_revision

    def _take_back(self, version_id, revision, ids):
        """Take away the exact skips this sender made, as if they had never been set. Returns the revision the episode is
        at afterwards."""
        try:
            self.service.store.discard_owner_spans(version_id, revision, AUTO_ACTOR)
        except RevisionConflict:
            logger.info("Reprise left an automatic skip alone: the episode was changed by hand since")
        except Exception:
            logger.opt(exception=True).warning("Reprise could not take back an automatic skip")
        result = self.service.store.get_result(version_id)
        return result.result_revision if result else revision

    def _refused(self, label, now):
        self._alerted = True  # Plex's refusal is already told to the owner in its own words
        super()._refused(label, now)

    def _fail(self, version_id, revisions, detail, now):
        outcome = super()._fail(version_id, revisions, detail, now)
        told, self._alerted = self._alerted, False
        if not told:
            bindings = self.service._bindings(version_id)
            label = label_of(bindings[0][2]) if bindings else "an episode"
            later = (
                "It won't try again. The skip waits for you in To approve."
                if outcome == "declined"
                else "It will try again later."
            )
            self.concerns.raise_problem(
                f"{LOCAL_FAILED_KEY}:{version_id}",
                "An automatic send didn't work",
                f"Reprise tried to send a skip for {label} on its own, but it didn't work. {detail} "
                f"It put everything back the way it was, so nothing changed in Plex. {later}",
            )
        return outcome

    def _sent(self, version_id, revision, new_revision, change_id, ids, now):
        super()._sent(version_id, revision, new_revision, change_id, ids, now)
        bindings = self.service._bindings(version_id)
        label = label_of(bindings[0][2]) if bindings else "an episode"
        self.concerns.resolve(f"{LOCAL_FAILED_KEY}:{version_id}", f"The skip for {label} was sent to Plex after all.")

    def _already(self, version_id, revisions, now):
        outcome = super()._already(version_id, revisions, now)
        bindings = self.service._bindings(version_id)
        label = label_of(bindings[0][2]) if bindings else "an episode"
        # A failure told about this episode earlier is over: Plex has the skip.
        self.concerns.resolve(f"{LOCAL_FAILED_KEY}:{version_id}", f"Plex already has the skip for {label}.")
        return outcome
