"""A stand-in for the approvals and picture endpoints, shared by the To approve e2e tests.

Only ``/api/reprise/approvals*`` and the picture endpoints under ``/api/reprise/media/<id>/`` are answered here; the
shell, the scripts and the styles are the app's real ones. The state moves like the real backend: a send moves the sure
ones on (to Plex, or to "approved, not sent"), an undo puts them back, a decision settles one suggestion and bumps its
revision (a stale one gets the same 409 the real route gives).
"""

from __future__ import annotations

import functools
import io
import itertools
import json
import re
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import Page, Route

from ._mocks import _fulfill_json

DURATION = 1_320_000
TOLERANCE_MS = 1500

EPISODES = {  # media id -> (show, season, episode number, title)
    "v201": ("Community", 2, 1, "Anthropology 101"),
    "v202": ("Community", 2, 2, "Accounting for Lawyers"),
    "v203": ("Community", 2, 3, "Basic Genealogy"),
    "v204": ("Community", 2, 4, "Epidemiology"),
    "v301": ("Parks and Recreation", 1, 1, "Pilot"),
    "v302": ("Parks and Recreation", 1, 2, "Canvassing"),
}

SENDING = {
    "on": {"available": True, "enabled": True, "message": None},
    "off": {
        "available": True,
        "enabled": False,
        "message": "Sending to Plex is turned off. You can still approve; turn it on in Settings, Sharing, to send.",
    },
    "missing": {"available": False, "enabled": False, "message": "Sending to Plex is not available on this install."},
}


def automatic_entry(mid: str, n: int, *, kinds=("credits",), sent_at: int = 1_700_000_000, span_ids=None) -> dict:
    """One row of ``GET /approvals/automatic`` for episode ``mid``."""
    show, season, number, _title = EPISODES[mid]
    return {
        "change_id": f"auto-{n}",
        "media_version_id": mid,
        "show": show,
        "season": season,
        "episode": number,
        "label": f"S{season:02d}E{number:02d}",
        "kinds": list(kinds),
        "sent_at": sent_at,
        "span_ids": [f"owner-{mid}-{k}" for k in kinds] if span_ids is None else span_ids,
        "origin": "local_model",
    }


def span(mid, kind, bucket, start, end, how_sure="high", source="Reprise", reason="", plex=None) -> dict:
    """One skip Reprise found: ``bucket`` is sure / needs (to look at) / ready (approved, not sent) / sent / kept."""
    return {
        "mid": mid,
        "kind": kind,
        "bucket": bucket,
        "start_ms": start,
        "end_ms": end,
        "first": (start, end),
        "how_sure": how_sure,
        "source": source,
        "reason": reason,
        "plex": plex,
    }


def default_spans() -> list[dict]:
    seen = "The same picture and music repeat in 3 other episodes."
    return [
        span("v201", "intro", "sure", 0, 32_000, reason=seen),
        span("v201", "credits", "sure", 1_250_000, DURATION, reason=seen),
        span("v202", "intro", "sure", 0, 33_500, reason=seen),
        span(
            "v203", "intro", "needs", 2_000, 34_000, "medium", "chapters", "The chapter list marks this as the intro."
        ),
        span(
            "v203",
            "credits",
            "needs",
            1_240_000,
            DURATION,
            "low",
            "online database",
            "A shared timing database lists these credits.",
            plex=(1_250_000, DURATION),
        ),
        span("v204", "intro", "ready", 0, 32_000, reason=seen),
        span("v301", "intro", "sure", 0, 25_000, reason=seen),
        span("v302", "intro", "sure", 0, 25_500, reason=seen),
    ]


@functools.lru_cache(maxsize=256)
def _jpeg(width: int, height: int, tiles: int, tint: int) -> bytes:
    from PIL import Image

    image = Image.new("RGB", (width * tiles, height))
    pixels = image.load()
    for x in range(width * tiles):
        shade = (tint + x // width) * 37
        for y in range(height):
            pixels[x, y] = ((shade + x) % 200 + 30, (shade * 2 + y * 2) % 180 + 40, (shade * 3 + x + y) % 160 + 60)
    out = io.BytesIO()
    image.save(out, "JPEG", quality=60)
    return out.getvalue()


class ApprovalsApi:
    """The approvals API as the To approve page sees it. ``calls`` lists every request, ``posts(path)`` the bodies sent
    to one endpoint; ``fail[(method, path)] = (status, body)`` makes the next such request fail once."""

    def __init__(
        self,
        page: Page,
        *,
        sending: str = "on",
        spans: list[dict] | None = None,
        cuts=None,
        automatic: list[dict] | None = None,
    ) -> None:
        self.sending = sending
        self.automatic = list(automatic or [])  # what Reprise sent by itself: ``GET /approvals/automatic``
        self.notice_marks: dict[str, str] = {}  # change id -> "read" or "dismissed", as the server keeps them
        self.spans = spans if spans is not None else default_spans()
        self.cuts = [31_000, 32_000, 33_000] if cuts is None else list(cuts)
        self.revisions = dict.fromkeys(EPISODES, 1)
        self.calls: list[tuple[str, str, dict, dict | None]] = []
        self.fail: dict[tuple[str, str], tuple[int, dict]] = {}
        self.send_fails: dict[str, str] = {}  # media id -> why Plex would not take it
        self.undo_fails: dict[str, str] = {}
        self.hold: set[str] = set()  # paths whose answer waits for release()
        self.held: list[Route] = []
        self.plex = {"status": "read", "message": None}
        self._ids = itertools.count(1)
        page.route(
            re.compile(r".*/api/reprise/(approvals|media/[^/?]+/(frame|strip|cuts|stream))(/|\?|$)"), self._handle
        )

    # -- what the tests look at ---------------------------------------------------------------------------------

    def posts(self, path: str) -> list[dict]:
        return [body for method, p, _query, body in self.calls if method == "POST" and p == path]

    def release(self) -> None:
        held, self.held = self.held, []
        self.hold.clear()
        for route in held:
            self._handle(route)

    # -- routing ------------------------------------------------------------------------------------------------

    def _handle(self, route: Route) -> None:
        request = route.request
        url = urlparse(request.url)
        path = url.path.split("/api/reprise", 1)[1]
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        if path.startswith("/media/"):
            media, _, what = path[len("/media/") :].partition("/")
            status, kind, body = self.picture(media, what, query)
            return route.fulfill(status=status, content_type=kind, body=body)
        if path in self.hold:
            self.held.append(route)
            return None
        body = request.post_data_json if request.method == "POST" else None
        self.calls.append((request.method, path, query, body))  # before answering, so a test sees it at once
        forced = self.fail.pop((request.method, path), None)
        if forced:
            return _fulfill_json(route, forced[1], forced[0])
        if path == "/approvals":
            return _fulfill_json(route, self.queue())
        if path == "/approvals/season":
            return _fulfill_json(route, self.season(query["show"], int(query["season"])))
        if path == "/approvals/automatic":
            listed = [a for a in self.automatic if self.notice_marks.get(a["change_id"]) != "dismissed"]
            return _fulfill_json(route, {"sent": [{**a, "read": a["change_id"] in self.notice_marks} for a in listed]})
        if path == "/approvals/automatic/notices":
            mark = "read" if body["action"] == "read" else "dismissed"
            ids = [a["change_id"] for a in self.automatic] if body.get("all") else body["change_ids"]
            for change_id in ids:
                if mark == "dismissed" or self.notice_marks.get(change_id) != "dismissed":
                    self.notice_marks[change_id] = mark
            return _fulfill_json(route, {"marked": len(ids)})
        if path == "/approvals/send":
            status, answer = self.send(body)
        elif path == "/approvals/undo":
            status, answer = 200, self.undo(body)
        elif path == "/approvals/decide":
            status, answer = self.decide(body)
        else:
            status, answer = 404, {"error": "Not found"}
        return _fulfill_json(route, answer, status)

    def picture(self, media: str, what: str, query: dict) -> tuple[int, str, bytes]:
        """The pictures: tiny but real JPEGs that differ by time. A subclass can serve real frames instead."""
        if what == "frame":
            return 200, "image/jpeg", _jpeg(160, 90, 1, int(query.get("t_ms", 0)) // 1000)
        if what == "strip":
            count = int(query.get("count", 8))
            return 200, "image/jpeg", _jpeg(80, 45, count, int(query.get("start_ms", 0)) // 1000)
        if what == "cuts":
            low, high = int(query.get("start_ms", 0)), int(query.get("end_ms", DURATION))
            return 200, "application/json", json.dumps({"cuts": [t for t in self.cuts if low <= t <= high]}).encode()
        return 404, "application/json", b'{"error": "Not found"}'

    # -- the queue ----------------------------------------------------------------------------------------------

    @staticmethod
    def _label(mid: str) -> str:
        _show, season, number, _title = EPISODES[mid]
        return f"S{season:02d}E{number:02d}"

    def _groups(self) -> dict[tuple[str, int], dict[str, list[dict]]]:
        groups: dict[tuple[str, int], dict[str, list[dict]]] = {}
        for s in self.spans:
            if s["bucket"] in ("sure", "needs", "ready"):
                show, season, *_ = EPISODES[s["mid"]]
                groups.setdefault((show, season), {"sure": [], "needs": [], "ready": []})[s["bucket"]].append(s)
        for group in groups.values():
            for rows in group.values():
                rows.sort(key=lambda s: (EPISODES[s["mid"]][2], s["kind"]))
        return groups

    def queue(self) -> dict:
        seasons = []
        for (show, season), group in self._groups().items():
            episodes = {s["mid"] for rows in group.values() for s in rows}
            seasons.append(
                {
                    "show": show,
                    "season": season,
                    "label": f"Season {season}",
                    "episodes": len(episodes),
                    "sure": len(group["sure"]),
                    "needs": len(group["needs"]),
                    "ready": len({s["mid"] for s in group["ready"]}),
                    "kinds": {k: sum(1 for s in group["sure"] if s["kind"] == k) for k in ("intro", "credits")},
                }
            )
        seasons.sort(key=lambda s: (-s["needs"], -s["sure"], -s["ready"], s["show"].lower(), s["season"]))
        shows: dict[str, dict] = {}
        for s in seasons:
            entry = shows.setdefault(s["show"], {"show": s["show"], "sure": 0, "needs": 0, "ready": 0, "seasons": []})
            entry["seasons"].append(s)
            for bucket in ("sure", "needs", "ready"):
                entry[bucket] += s[bucket]
        return {
            "shows": list(shows.values()),
            "totals": {
                "sure": sum(s["sure"] for s in seasons),
                "needs": sum(s["needs"] for s in seasons),
                "ready": sum(s["ready"] for s in seasons),
                "seasons": len(seasons),
            },
            "sending": dict(SENDING[self.sending]),
        }

    # -- one season ---------------------------------------------------------------------------------------------

    @staticmethod
    def _compare(s: dict) -> dict:
        if s["plex"] is None:
            return {"plex": None, "change": "new"}
        start, end = s["start_ms"] - s["plex"][0], s["end_ms"] - s["plex"][1]
        same = abs(start) <= TOLERANCE_MS and abs(end) <= TOLERANCE_MS
        return {
            "plex": {"start_ms": s["plex"][0], "end_ms": s["plex"][1]},
            "change": "same" if same else "moved",
            "start_delta_ms": start,
            "end_delta_ms": end,
        }

    def _item(self, s: dict) -> dict:
        mid = s["mid"]
        _show, _season, number, title = EPISODES[mid]
        return {
            "media_version_id": mid,
            "episode": number,
            "title": title,
            "label": self._label(mid),
            "kind": s["kind"],
            "bucket": s["bucket"],
            "start_ms": s["start_ms"],
            "end_ms": s["end_ms"],
            "duration_ms": DURATION,
            "how_sure": s["how_sure"],
            "source": s["source"],
            "reason": s["reason"],
            "range_ids": [f"r-{mid}-{s['kind']}"],
            "revision": self.revisions[mid],
            **self._compare(s),
        }

    def _changes(self, rows: list[dict]) -> dict:
        lines = []
        for kind, word in (("intro", "Intro"), ("credits", "Credits")):
            these = [self._compare(s)["change"] for s in rows if s["kind"] == kind]
            if these.count("new"):
                lines.append(f"{word} skip is added on {_count(these.count('new'))} where Plex has none.")
            if these.count("moved"):
                lines.append(f"{word} skip moves on {_count(these.count('moved'))}.")
            if these.count("same"):
                lines.append(f"{word} skip already matches Plex on {_count(these.count('same'))}.")
        counts = {c: sum(1 for s in rows if self._compare(s)["change"] == c) for c in ("new", "same", "moved")}
        return {"lines": lines, "counts": {**counts, "unknown": 0}}

    def season(self, show: str, season: int) -> dict:
        group = self._groups().get((show, season), {"sure": [], "needs": [], "ready": []})
        ready = []
        for mid in sorted({s["mid"] for s in group["ready"]}, key=lambda m: EPISODES[m][2]):
            _show, _season, number, title = EPISODES[mid]
            ready.append(
                {
                    "media_version_id": mid,
                    "episode": number,
                    "title": title,
                    "label": self._label(mid),
                    "duration_ms": DURATION,
                    "revision": self.revisions[mid],
                    "skips": [
                        {"kind": s["kind"], "start_ms": s["start_ms"], "end_ms": s["end_ms"], **self._compare(s)}
                        for s in group["ready"]
                        if s["mid"] == mid
                    ],
                }
            )
        return {
            "show": show,
            "season": season,
            "label": f"Season {season}",
            "sure": [self._item(s) for s in group["sure"]],
            "needs": [self._item(s) for s in group["needs"]],
            "ready": ready,
            "plex": dict(self.plex),
            "changes": {"sure": self._changes(group["sure"]), "ready": self._changes(group["ready"])},
            "sending": dict(SENDING[self.sending]),
        }

    # -- sending, undoing, deciding -----------------------------------------------------------------------------

    def send(self, body: dict) -> tuple[int, dict]:
        show, season, scope = body["show"], body["season"], body["scope"]
        to_plex = body.get("to_plex", True)
        if to_plex and self.sending != "on":
            return 400, {"error": SENDING[self.sending]["message"]}
        if scope not in ("sure", "approved") or (scope == "approved" and not to_plex):
            return 400, {"error": "Say whether to approve the sure ones or to send the ones you approved."}
        wanted = "sure" if scope == "sure" else "ready"
        todo = [s for s in self.spans if EPISODES[s["mid"]][:2] == (show, season) and s["bucket"] == wanted]
        if not todo:
            return 400, {"error": "There is nothing sure left to approve in this season."}
        approved, sent, failed = [], [], []
        for mid in sorted({s["mid"] for s in todo}, key=lambda m: EPISODES[m][2]):
            mine = [s for s in todo if s["mid"] == mid]
            entry = {
                "media_version_id": mid,
                "episode": EPISODES[mid][2],
                "label": self._label(mid),
                "kinds": sorted({s["kind"] for s in mine}),
            }
            owner_ids = [f"owner-{mid}-{s['kind']}" for s in mine] if scope == "sure" else []
            if scope == "sure":
                approved.append({**entry, "span_ids": owner_ids})
            if to_plex and mid in self.send_fails:
                failed.append(
                    {k: entry[k] for k in ("media_version_id", "episode", "label")} | {"reason": self.send_fails[mid]}
                )
                bucket = "ready"
            elif to_plex:
                sent.append({**entry, "change_id": f"c{next(self._ids)}", "span_ids": owner_ids})
                bucket = "sent"
            else:
                bucket = "ready"
            for s in mine:
                s["bucket"] = bucket
            self.revisions[mid] += 1
        backup = {"created": True, "files": ["com.plexapp.plugins.library.db"]} if to_plex else None
        return 200, {
            "show": show,
            "season": season,
            "scope": scope,
            "approved": approved,
            "sent": sent,
            "failed": failed,
            "stopped": None,
            "backup": backup,
        }

    def undo(self, body: dict) -> dict:
        restored, failed = [], []
        for change in reversed(body["changes"]):
            mid, label = change["media_version_id"], change.get("label") or "an episode"
            self.automatic = [a for a in self.automatic if a["change_id"] != change.get("change_id")]
            if mid in self.undo_fails:
                failed.append({"media_version_id": mid, "label": label, "reason": self.undo_fails[mid]})
                continue
            withdrawn = bool(change.get("span_ids"))
            for s in self.spans:
                if s["mid"] == mid and s["bucket"] in ("sent", "ready"):
                    s["bucket"] = "sure" if withdrawn else "ready"
            self.revisions[mid] += 1
            restored.append({"media_version_id": mid, "label": label, "withdrawn": withdrawn})
        restored.reverse()
        failed.reverse()
        return {"restored": restored, "failed": failed}

    def decide(self, body: dict) -> tuple[int, dict]:
        mid, kind, decision = body["media_version_id"], body["kind"], body["decision"]
        if body["expected_revision"] != self.revisions[mid]:
            return 409, {"error": "Conflict; refresh and try again"}
        target = next(s for s in self.spans if s["mid"] == mid and s["kind"] == kind)
        owner_id = f"owner-{mid}-{kind}"
        if decision == "skip":
            target["bucket"], target["start_ms"], target["end_ms"] = "ready", body["start_ms"], body["end_ms"]
            undo = {"decision": "restore", "kind": kind, "span_id": owner_id, "range_ids": []}
        elif decision == "keep":
            target["bucket"] = "kept"
            undo = {"decision": "restore", "kind": kind, "span_id": None, "range_ids": [f"r-{mid}-{kind}"]}
        else:  # restore: the suggestion waits again at the time it was found
            target["bucket"] = "needs"
            target["start_ms"], target["end_ms"] = target["first"]
            undo = {"decision": "skip"}
        self.revisions[mid] += 1
        answer = {
            "media_version_id": mid,
            "kind": kind,
            "decision": decision,
            "revision": self.revisions[mid],
            "undo": undo,
        }
        if decision == "skip":
            answer.update(start_ms=body["start_ms"], end_ms=body["end_ms"], span_id=owner_id)
        return 200, answer


def _count(n: int) -> str:
    return f"{n} episode" + ("" if n == 1 else "s")
