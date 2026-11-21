"""What Reprise keeps so a stopped analysis carries on after a restart, and the limits that keep that useful.

A job that analyzes episodes saves its place in its own settings under :data:`QUEUE_KEY` (a plain dict, kept in the
jobs database like the rest of the job), so a restart picks the job up where it stopped instead of failing it:

* ``ids`` - the episodes of a whole-library analysis, found once and kept (an owner's job already lists its episodes);
* ``next`` - the position of the first episode not finished yet; episodes are finished strictly in order;
* ``attempts`` - per episode, how many times its analysis started without finishing;
* ``running`` - the episode being analyzed right now; still set after a restart, it was cut off;
* ``counts`` - how the finished episodes ended: ``results``, ``file`` (left out), ``refused`` and ``set_aside``;
* ``resumes`` - every automatic resume with the number of episodes finished by then, so a restart loop shows;
* ``recovery`` / ``paused`` / ``blocked`` / ``waiting_since`` - what the owner is told: waiting, resumed or paused.

Everything here is a pure function of that dict and a clock the caller passes in, so the limits that stop a useless
loop are tested without waiting. ``RepriseService`` saves the dict and starts the work.
"""

from __future__ import annotations

import copy
import threading
from datetime import UTC, datetime, timedelta
from typing import Any

QUEUE_KEY = "reprise_queue"

# An episode whose analysis started this many times without finishing is set aside; the job moves on.
MAX_ATTEMPTS = 2
# This many resumes inside the window, with no episode finished since the first, and the job pauses for its owner.
LOOP_RESUMES = 3
LOOP_WINDOW = timedelta(minutes=15)
# A job that cannot resume because its media or Plex stays unavailable this long pauses for its owner.
WAIT_LIMIT = timedelta(minutes=30)
# How long after startup the first resume waits, and how often the waiting jobs are looked at again.
DEFAULT_DELAY_SECONDS = 120
MAX_DELAY_SECONDS = 3600
POLL_SECONDS = 30
# How many of a job's next episodes are looked at to tell whether its media can be reached.
CHECK_EPISODES = 25
_RESUMES_KEPT = 6

# One lock for every change to a job's queue, taken before the job manager's own lock and never the other way round.
LOCK = threading.RLock()

# The wording an interrupted run leaves on its episode (also what the startup cleanup of stopped runs writes).
INTERRUPTED_REASON = "Analysis was interrupted before it finished"
SET_ASIDE_REASON = f"Skipped after {MAX_ATTEMPTS} interrupted attempts — analyze it again to retry"
PREVIEW_INTERRUPTED = "This preview was interrupted by a restart. Create the preview again."

RESUMED_MESSAGE = "Resumed after a restart"
WAITING_MESSAGES = {
    None: "Restarted. Analysis resumes in a moment.",
    "folders": "Restarted. Waiting for your media folders to be available before resuming.",
    "plex": "Restarted. Waiting for Plex to be reachable before resuming.",
}
PAUSED_MESSAGES = {
    "kept_stopping": "Analysis paused: it kept stopping. Resume when ready.",
    "media_unavailable": "Analysis paused: your media folders or Plex were not available after the restart. "
    "Resume when ready.",
    "switched_off": "Analysis was stopped by a restart. Resume when ready.",
    "resume_failed": "Analysis paused: it could not resume by itself. Resume when ready.",
}

# How a finished episode ended, as ``counts`` keys. Set-aside episodes are not "finished": they got no analysis.
RESULT, FILE_LEFT_OUT, REFUSED, SET_ASIDE = "results", "file", "refused", "set_aside"


def queue_of(config: dict[str, Any] | None) -> dict[str, Any]:
    """The job's saved queue, as a copy the caller may change freely (empty for a job that has none yet)."""
    saved = (config or {}).get(QUEUE_KEY)
    return copy.deepcopy(saved) if isinstance(saved, dict) else {}


def position(queue: dict[str, Any]) -> int:
    """Where the job stands: how many of its episodes are finished (they are finished in order)."""
    try:
        return max(0, int(queue.get("next", 0)))
    except (TypeError, ValueError):
        return 0


def todo(config: dict[str, Any] | None, limit: int | None = None) -> list[str]:
    """The episodes the job still has to do, in order (all of them when ``limit`` is None)."""
    config = config or {}
    queue = queue_of(config)
    ids = queue.get("ids") or config.get("media_version_ids") or []
    return [str(media_id) for media_id in ids[position(queue) :][:limit]]


def analyzing(config: dict[str, Any] | None) -> bool:
    """Whether the job is past discovery: it knows its episodes. Before that, a resume re-reads the library."""
    config = config or {}
    return bool(queue_of(config).get("ids") or config.get("media_version_ids"))


def finished(queue: dict[str, Any]) -> int:
    """How many episodes the job has finished, however they ended (set-aside ones are not counted: nothing ran)."""
    counts = queue.get("counts") or {}
    return sum(int(counts.get(key, 0)) for key in (RESULT, FILE_LEFT_OUT, REFUSED))


def counts_of(queue: dict[str, Any]) -> dict[str, int]:
    """How the finished episodes ended, with every kind present."""
    counts = queue.get("counts") or {}
    return {key: int(counts.get(key, 0)) for key in (RESULT, FILE_LEFT_OUT, REFUSED, SET_ASIDE)}


def attempts_of(queue: dict[str, Any], media_id: str) -> int:
    """How many times this episode's analysis started without finishing."""
    return int((queue.get("attempts") or {}).get(media_id, 0))


def exhausted(queue: dict[str, Any], media_id: str) -> bool:
    """Whether this episode has been tried the most the job allows: it is set aside, not tried again."""
    return attempts_of(queue, media_id) >= MAX_ATTEMPTS


def begin(queue: dict[str, Any], media_id: str) -> dict[str, Any]:
    """This episode's analysis starts: count the attempt now, so a kill in the middle still counts."""
    attempts = dict(queue.get("attempts") or {})
    attempts[media_id] = attempts_of(queue, media_id) + 1
    return {**queue, "attempts": attempts, "running": media_id}


def settle(queue: dict[str, Any], index: int, media_id: str, outcome: str | None) -> dict[str, Any]:
    """Episode ``index`` is done with: it has a result, was left out or refused, or was set aside.

    ``outcome`` is one of the ``counts`` keys, or None for an episode this job had already finished before a restart.
    """
    attempts = {key: value for key, value in (queue.get("attempts") or {}).items() if key != media_id}
    counts = dict(queue.get("counts") or {})
    if outcome:
        counts[outcome] = int(counts.get(outcome, 0)) + 1
    running = queue.get("running")
    return {
        **queue,
        "next": max(position(queue), index + 1),
        "attempts": attempts,
        "running": None if running == media_id else running,
        "counts": counts,
    }


def give_back(queue: dict[str, Any], media_id: str) -> dict[str, Any]:
    """A stop that came with warning cut this episode off: the attempt does not count, the episode stays to do.

    Only the episode marked running is given back, so asking twice (the run noticing the stop, and the shutdown
    checkpoint) changes nothing the second time.
    """
    if queue.get("running") != media_id:
        return queue
    attempts = dict(queue.get("attempts") or {})
    left = attempts_of(queue, media_id) - 1
    if left > 0:
        attempts[media_id] = left
    else:
        attempts.pop(media_id, None)
    return {**queue, "attempts": attempts, "running": None}


def note_restart(queue: dict[str, Any], now: datetime, *, wait: bool = True) -> dict[str, Any]:
    """The process stopped with this job unfinished. An episode still marked running was cut off without warning
    and keeps its counted attempt. ``wait`` leaves the job waiting for the resume pass (not for one the owner holds).
    """
    out = {**queue, "running": None}
    out.pop("blocked", None)
    if wait:
        out.pop("paused", None)
        out.update(recovery="waiting", waiting_since=now.isoformat())
    return out


def holds_start(config: dict[str, Any] | None) -> bool:
    """Whether the job waits for the resume pass, which alone may start it (nothing else may start it earlier)."""
    return queue_of(config).get("recovery") == "waiting"


def block(queue: dict[str, Any], blocked: str | None) -> dict[str, Any]:
    """Say what the waiting job waits for (``folders`` or ``plex``), or that it waits for nothing but the delay."""
    out = {**queue}
    if blocked:
        out["blocked"] = blocked
    else:
        out.pop("blocked", None)
    return out


def waited_too_long(queue: dict[str, Any], now: datetime) -> bool:
    """Whether the job has been waiting for unavailable media for longer than :data:`WAIT_LIMIT`."""
    since = _parsed(queue.get("waiting_since"))
    return since is not None and now - since >= WAIT_LIMIT


def verdict(queue: dict[str, Any], config: dict[str, Any] | None, now: datetime) -> str | None:
    """None when an automatic resume is fine, else the reason it must wait for its owner (a ``PAUSED_MESSAGES`` key).

    Analysis that keeps dying without finishing an episode is a loop: after :data:`LOOP_RESUMES` resumes inside
    :data:`LOOP_WINDOW` with nothing finished since, it stops. Before the episodes are known (the library is still
    being read) a job is only run once more by itself.
    """
    resumes = queue.get("resumes") or []
    if not analyzing(config):
        return "kept_stopping" if resumes else None
    done = finished(queue)
    stuck = [
        entry
        for entry in resumes
        if entry.get("completed") == done
        and (at := _parsed(entry.get("at"))) is not None
        and timedelta(0) <= now - at <= LOOP_WINDOW
    ]
    return "kept_stopping" if len(stuck) >= LOOP_RESUMES else None


def record_resume(queue: dict[str, Any], now: datetime) -> dict[str, Any]:
    """The job starts again by itself: remember it, so a loop of resumes that achieve nothing is noticed."""
    resumes = [*(queue.get("resumes") or []), {"at": now.isoformat(), "completed": finished(queue)}]
    out = {**queue, "resumes": resumes[-_RESUMES_KEPT:], "recovery": "resumed"}
    for key in ("paused", "blocked", "waiting_since"):
        out.pop(key, None)
    return out


def pause(queue: dict[str, Any], reason: str) -> dict[str, Any]:
    """The job waits for its owner, for ``reason`` (a ``PAUSED_MESSAGES`` key)."""
    out = {**queue, "recovery": "paused", "paused": reason}
    for key in ("blocked", "waiting_since"):
        out.pop(key, None)
    return out


def owner_resumed(queue: dict[str, Any]) -> dict[str, Any]:
    """The owner pressed Resume on a job that was waiting or paused: go on, with a clean slate for the loop guard."""
    if queue.get("recovery") not in ("waiting", "paused"):
        return queue
    out = {**queue, "recovery": "resumed", "resumes": []}
    for key in ("paused", "blocked", "waiting_since"):
        out.pop(key, None)
    return out


def release(queue: dict[str, Any]) -> dict[str, Any]:
    """The owner took a waiting job into their own hands (paused it): the resume pass leaves it alone."""
    out = {**queue}
    for key in ("recovery", "blocked", "waiting_since"):
        out.pop(key, None)
    return out


def describe(queue: dict[str, Any]) -> dict[str, Any] | None:
    """What the owner is told about this job's recovery (None when it never had to recover)."""
    state = queue.get("recovery")
    if state == "waiting":
        return {"state": state, "message": WAITING_MESSAGES.get(queue.get("blocked"), WAITING_MESSAGES[None])}
    if state == "paused":
        paused = queue.get("paused")
        reason = paused if isinstance(paused, str) and paused in PAUSED_MESSAGES else "kept_stopping"
        return {"state": state, "reason": reason, "message": PAUSED_MESSAGES[reason]}
    if state == "resumed":
        return {"state": state, "message": RESUMED_MESSAGE}
    return None


def _parsed(text: object) -> datetime | None:
    try:
        value = datetime.fromisoformat(str(text))
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)
