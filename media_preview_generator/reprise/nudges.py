"""Phone notifications with buttons: what is waiting for the owner's approval, and what Reprise sent to Plex by itself.

Two kinds of message go out through the same ntfy topic as the problem alerts (``Alerts.notify``):

* **waiting**: one message per show-season that has suggestions waiting ("Community S2: 3 sure ones, 2 to look at"),
  with an *Approve & send* button for exactly the sure ones listed and an *Open* button for the To approve page;
* **automatic**: one batched message when Reprise sent something to Plex by itself, with an *Undo* button.

The buttons work from the phone without signing in, so each carries a **signed, single-use address**. The token is
``v1.<payload>.<signature>``: the payload is ``{a: action, p: params, n: nonce, e: expiry}`` as URL-safe base64 JSON and
the signature is HMAC-SHA256 over ``v1.<payload>`` keyed by the app's webhook secret (so changing that secret ends every
outstanding button). The list of episodes a button covers can be long, so it is kept in the app database next to the
nonce and the token only carries its digest (``p: {"d": ...}``); a click is checked against both. A click spends the
nonce in one atomic update, so it works once: a second click is refused and does nothing. Nothing secret is ever in an
address: the token proves only "this exact action was offered, recently, once".

Sending is rate limited to ``MAX_PER_HOUR`` per rolling hour (kept in the database, so a restart does not reset it),
silent during quiet hours, and never repeats a season unless something new is waiting there. What cannot go out now is
kept and goes out on a later pass, most useful first: automatic notices, then seasons with sure ones, then the rest.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
import unicodedata

from loguru import logger

from . import approvals
from .models import ReviewRefused

PUBLIC_URL = "https://reprise.nateshome.net"  # where the phone reaches Reprise; ``REPRISE_PUBLIC_URL`` overrides it
ACT_PATH = "/api/reprise/notify/act/"
APPROVE_PAGE = "/reprise/approve"

TOKEN_VERSION = "v1"
TOKEN_TTL_S = 6 * 3600  # how long an Approve or Undo button works
MAX_PER_HOUR = 3
RUN_EVERY_S = 300  # looking at the queue reads the whole catalogue, so not on every minute's pass
SHOWN_EPISODES = 8
KEEP_LOG = 200
KEEP_AUTO_S = 30 * 86400

SETTING_ON = "reprise_notify"
SETTING_QUIET_FROM = "reprise_notify_quiet_from"
SETTING_QUIET_UNTIL = "reprise_notify_quiet_until"
DEFAULT_QUIET_FROM = "22:00"
DEFAULT_QUIET_UNTIL = "08:00"
SETTING_NAMES = {"enabled": SETTING_ON, "quiet_from": SETTING_QUIET_FROM, "quiet_until": SETTING_QUIET_UNTIL}
_CLOCK_TIME = re.compile(r"([01]\d|2[0-3]):([0-5]\d)")

ACTION_APPROVE = "approve"
ACTION_UNDO = "undo"

_STOPPED = "Phone alerts are not available right now."
_SOMETHING_WRONG = "Reprise could not do that. Open the To approve page to see where things stand."
_KIND_WORDS = {"intro": ("intro", "intros"), "credits": ("ending", "endings")}


# ── signed tokens ────────────────────────────────────────────────────────────────────────────────────────────────


class TokenError(Exception):
    """A button address that cannot be used. ``status`` is the HTTP answer, ``message`` plain words for the owner."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


def _b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _signature(secret, signed):
    return _b64(hmac.new(secret.encode("utf-8"), signed.encode("ascii"), hashlib.sha256).digest())


def sign(secret, action, params, *, now, ttl=TOKEN_TTL_S):
    """A token for ``action`` with ``params``, good for ``ttl`` seconds from ``now``. Returns ``(token, payload)``."""
    payload = {"a": action, "p": params, "n": secrets.token_hex(12), "e": int(now) + int(ttl)}
    body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signed = f"{TOKEN_VERSION}.{body}"
    return f"{signed}.{_signature(secret, signed)}", payload


def verify(secret, token, *, now):
    """The payload of a genuine, unexpired token. Raises ``TokenError``: 400 for something that is no token at all,
    403 for one the secret did not sign (or that was altered), 410 for one that ran out."""
    parts = token.split(".") if isinstance(token, str) and token.isascii() and len(token) <= 4096 else []
    if len(parts) != 3 or parts[0] != TOKEN_VERSION or not all(parts):
        raise TokenError(400, "That link is not one Reprise made.")
    signed = f"{parts[0]}.{parts[1]}"
    if not hmac.compare_digest(_signature(secret, signed).encode(), parts[2].encode()):
        raise TokenError(403, "That link is not valid any more. Open Reprise to do it from there.")
    try:
        payload = json.loads(_unb64(parts[1]))
        action, params, nonce, expires = payload["a"], payload["p"], payload["n"], payload["e"]
        if not (isinstance(action, str) and isinstance(params, dict) and isinstance(nonce, str) and nonce):
            raise ValueError
        if type(expires) is not int:
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise TokenError(400, "That link is not one Reprise made.") from None
    if now >= expires:
        raise TokenError(410, "That button has expired. Open Reprise to do it from there.")
    return payload


def _digest(action, params):
    return hashlib.sha256(json.dumps([action, params], separators=(",", ":"), sort_keys=True).encode()).hexdigest()[:32]


# ── words ────────────────────────────────────────────────────────────────────────────────────────────────────────


def _flag(raw):
    return raw if isinstance(raw, bool) else str(raw).strip().lower() in ("true", "1", "yes")


def _iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _ascii(text):
    """Text a header can carry: accents dropped (``Pokémon`` becomes ``Pokemon``), anything else left out."""
    return unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode().strip()


def _plural(number, one, many):
    return f"{number} {one if number == 1 else many}"


def _minutes(clock_time):
    match = _CLOCK_TIME.fullmatch(clock_time)
    return int(match[1]) * 60 + int(match[2])


def _season_short(season):
    return "Specials" if season == 0 else f"S{season}" if season is not None else "episodes"


def _episodes_text(numbers):
    """``E1, E3, E4 and 2 more`` for the episode numbers of one season."""
    numbers = sorted({n for n in numbers if n is not None})
    shown = ", ".join(f"E{n}" for n in numbers[:SHOWN_EPISODES])
    return f"{shown} and {len(numbers) - SHOWN_EPISODES} more" if len(numbers) > SHOWN_EPISODES else shown


def _action_label(text):
    """A button label as ntfy's ``Actions`` header wants it: plain, and quoted if it holds a comma or semicolon."""
    text = _ascii(text).replace('"', "").replace("'", "")
    return f'"{text}"' if "," in text or ";" in text else text


def public_url():
    """The address the phone uses for Reprise, without a trailing slash."""
    return (os.environ.get("REPRISE_PUBLIC_URL") or PUBLIC_URL).rstrip("/")


class Notifier:
    """Decides what to tell the owner's phone and when, sends it through ``alerts``, and runs the buttons.

    ``secret`` returns the signing secret (the webhook secret); ``scan`` returns what is waiting, grouped as
    ``approvals._scan`` does (read from the store only, Plex is not asked)."""

    def __init__(self, service, alerts, *, clock=time.time, secret=None, scan=None, every=RUN_EVERY_S):
        self.service, self.alerts, self._clock = service, alerts, clock
        self._secret = secret or _webhook_secret
        self._scan = scan or approvals._scan
        self._every = every
        self._due = 0
        self._warned = False
        self._run_lock = threading.Lock()
        with service.store.transaction() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS reprise_notify_tokens (
                nonce TEXT PRIMARY KEY, action TEXT NOT NULL, params TEXT NOT NULL, expires INTEGER NOT NULL,
                used INTEGER NOT NULL DEFAULT 0)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS reprise_notify_state (
                key TEXT PRIMARY KEY, value TEXT NOT NULL, at INTEGER NOT NULL)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS reprise_notify_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT, at INTEGER NOT NULL, kind TEXT NOT NULL,
                key TEXT NOT NULL, title TEXT NOT NULL)""")

    # ── settings ─────────────────────────────────────────────────────────────────────────────────────────────────

    def _setting(self, key, default):
        return self.service._setting(key, default)

    def enabled(self):
        return _flag(self._setting(SETTING_ON, True))

    def _quiet_times(self):
        """``(from, until)`` as ``HH:MM``; a value that is not a time (a hand-edited file) reads as the default."""
        found = []
        for key, default in ((SETTING_QUIET_FROM, DEFAULT_QUIET_FROM), (SETTING_QUIET_UNTIL, DEFAULT_QUIET_UNTIL)):
            raw = self._setting(key, default)
            found.append(raw if isinstance(raw, str) and _CLOCK_TIME.fullmatch(raw) else default)
        return tuple(found)

    def settings(self):
        quiet_from, quiet_until = self._quiet_times()
        return {"enabled": self.enabled(), "quiet_from": quiet_from, "quiet_until": quiet_until}

    def update_settings(self, values):
        """Change some of ``enabled`` (a boolean), ``quiet_from`` and ``quiet_until`` (``HH:MM``). Anything else is
        refused in plain words."""
        if not isinstance(values, dict) or not values or set(values) - set(SETTING_NAMES):
            raise ValueError("Choose on or off, and the quiet hours as times like 22:00.")
        for name, value in values.items():
            if name == "enabled":
                valid = type(value) is bool
            else:
                valid = isinstance(value, str) and _CLOCK_TIME.fullmatch(value) is not None
            if not valid:
                raise ValueError("Choose on or off, and the quiet hours as times like 22:00.")
        store = getattr(self.service.registry, "settings", None)
        if store is None:
            raise ValueError("Reprise can't save settings right now.")
        for name, value in values.items():
            store.set(SETTING_NAMES[name], value)
        return self.settings()

    def quiet_now(self, now):
        """Whether ``now`` (epoch seconds) falls inside the quiet hours, in the app's local time. Equal times mean there
        are no quiet hours; a window may wrap midnight (22:00 to 08:00)."""
        start, end = (_minutes(t) for t in self._quiet_times())
        if start == end:
            return False
        local = time.localtime(now)
        minute = local.tm_hour * 60 + local.tm_min
        return start <= minute < end if start < end else minute >= start or minute < end

    def status(self):
        """What the Settings page shows: the settings, the last message sent and how many are being held back."""
        now = int(self._clock())
        row = self._query("SELECT at,title FROM reprise_notify_log ORDER BY id DESC LIMIT 1")
        waiting = int((self._get("pending") or {}).get("n", 0)) if self.enabled() else 0
        if not waiting:
            reason = None
        elif self.quiet_now(now):
            reason = "quiet_hours"
        else:
            reason = "limit"
        return {
            **self.settings(),
            "last": {"title": row[0]["title"], "at": _iso(row[0]["at"])} if row else None,
            "waiting": waiting,
            "waiting_reason": reason,
        }

    # ── the database ─────────────────────────────────────────────────────────────────────────────────────────────

    def _query(self, sql, *params):
        with self.service.store.marker_store._reading() as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def _get(self, key):
        rows = self._query("SELECT value FROM reprise_notify_state WHERE key=?", key)
        return json.loads(rows[0]["value"]) if rows else None

    def _put(self, key, value, now):
        with self.service.store.transaction() as conn:
            conn.execute(
                "INSERT INTO reprise_notify_state(key,value,at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, at=excluded.at",
                (key, json.dumps(value, separators=(",", ":"), sort_keys=True), now),
            )

    def _drop(self, *keys):
        with self.service.store.transaction() as conn:
            conn.executemany("DELETE FROM reprise_notify_state WHERE key=?", [(key,) for key in keys])

    def _sent_in_last_hour(self, now):
        return self._query("SELECT COUNT(*) AS n FROM reprise_notify_log WHERE at>?", now - 3600)[0]["n"]

    # ── buttons: issue and redeem ────────────────────────────────────────────────────────────────────────────────

    def issue(self, action, params, now):
        """A fresh token for ``action`` with ``params``. The parameters stay in the database, the token carries only
        their digest, so a long list of episodes does not make a long address."""
        token, payload = sign(self._secret(), action, {"d": _digest(action, params)}, now=now)
        with self.service.store.transaction() as conn:
            conn.execute("DELETE FROM reprise_notify_tokens WHERE expires<=?", (int(now),))
            conn.execute(
                "INSERT INTO reprise_notify_tokens(nonce,action,params,expires,used) VALUES (?,?,?,?,0)",
                (payload["n"], action, json.dumps(params, separators=(",", ":")), payload["e"]),
            )
        return token

    def redeem(self, token, now):
        """Check ``token`` and spend it: ``(action, params)``. Raises ``TokenError``; a token that was spent already is
        a 410, whoever holds it."""
        payload = verify(self._secret(), token, now=now)
        action, nonce = payload["a"], payload["n"]
        with self.service.store.transaction() as conn:
            conn.execute("DELETE FROM reprise_notify_tokens WHERE expires<=?", (int(now),))
            row = conn.execute("SELECT action,params FROM reprise_notify_tokens WHERE nonce=?", (nonce,)).fetchone()
            if row is None or row["action"] != action:
                raise TokenError(410, "That button is not available any more. Open Reprise to do it from there.")
            params = json.loads(row["params"])
            if not hmac.compare_digest(str(payload["p"].get("d", "")).encode(), _digest(action, params).encode()):
                raise TokenError(403, "That link is not valid any more. Open Reprise to do it from there.")
            spent = conn.execute("UPDATE reprise_notify_tokens SET used=1 WHERE nonce=? AND used=0", (nonce,))
            if spent.rowcount != 1:
                raise TokenError(410, "That button was already used. Nothing was done a second time.")
        return action, params

    def act(self, token):
        """Run the button ``token`` stands for. Returns ``(http status, {"ok": bool, "message": plain sentence, ...})``;
        never raises."""
        try:
            action, params = self.redeem(token, int(self._clock()))
            if action == ACTION_APPROVE:
                return self._approve(params)
            if action == ACTION_UNDO:
                return self._undo(params)
            return 400, {"ok": False, "message": "That link is not one Reprise made."}
        except TokenError as refused:
            return refused.status, {"ok": False, "message": refused.message}
        except ReviewRefused as refused:
            return 409, {"ok": False, "message": str(refused)}
        except Exception as exc:  # deliberately broad: a button press must answer, never crash
            logger.warning("A phone button failed: {}", type(exc).__name__)
            return 500, {"ok": False, "message": _SOMETHING_WRONG}

    def _approve(self, params):
        show, season, ids = params["show"], params["season"], params["ids"]
        to_plex = approvals.sending(self.service)["enabled"]
        result = approvals.send(self.service, show, season, "sure", to_plex=to_plex, only=ids)
        label = f"{show} {_season_short(season)}"
        approved = sum(len(entry["kinds"]) for entry in result["approved"])
        sent = sum(len(entry["kinds"]) for entry in result["sent"])
        failed = result["failed"]
        done = sent if to_plex else approved
        if not done:
            text = f"Nothing was sent for {label}." if to_plex else f"Nothing was approved for {label}."
        elif to_plex:
            text = f"Sent {_plural(sent, 'sure one', 'sure ones')} to Plex for {label}."
        else:
            text = (
                f"Approved {_plural(approved, 'sure one', 'sure ones')} for {label}. "
                "Sending to Plex is off, so nothing was sent."
            )
        if failed:
            text += f" {len(failed)} could not be done: {failed[0]['reason']}"
            self._drop(f"season:{show}|{season}")  # so the next pass tells the owner what is still waiting
        body = {"ok": bool(done), "message": text, "approved": approved, "sent": sent, "failed": len(failed)}
        return (200 if done else 409), body

    def _undo(self, params):
        result = approvals.undo(self.service, params["changes"])
        restored, failed = result["restored"], result["failed"]
        if restored:
            text = f"Put {_plural(len(restored), 'episode', 'episodes')} back as they were in Plex."
        else:
            text = "Nothing was put back."
        if failed:
            text += f" {_plural(len(failed), 'episode', 'episodes')} could not be put back: {failed[0]['reason']}"
        body = {"ok": bool(restored), "message": text, "restored": len(restored), "failed": len(failed)}
        return (200 if restored else 409), body

    # ── what to tell the phone ───────────────────────────────────────────────────────────────────────────────────

    def run(self, now=None):
        """One look at what is waiting; sends what the limits allow. Returns how many messages went out. Safe to call
        on every pass: it does nothing while off, without a token, or before its next turn; it never raises."""
        now = int(self._clock() if now is None else now)
        if not self._run_lock.acquire(blocking=False):
            return 0
        try:
            return self._run(now)
        except Exception as exc:  # deliberately broad: phone messages must never stop the automatic work
            logger.warning("Phone notifications did not run this time: {}", type(exc).__name__)
            return 0
        finally:
            self._run_lock.release()

    def _run(self, now):
        if not self.enabled():
            return 0
        if not self.alerts.has_token():
            if not self._warned:
                logger.warning("Phone notifications are on but no ntfy token file was found; none will be sent")
                self._warned = True
            return 0
        self._warned = False
        if now < self._due:
            return 0
        self._due = now + self._every
        candidates = sorted([*self._automatic(now), *self._seasons(now)], key=lambda c: c["rank"])
        sent = 0
        if not self.quiet_now(now):
            for candidate in candidates:
                if self._sent_in_last_hour(now) >= MAX_PER_HOUR or not self._deliver(candidate, now):
                    break
                sent += 1
        self._put("pending", {"n": len(candidates) - sent}, now)
        return sent

    def _seasons(self, now):
        """The seasons with something new waiting, each as one message to send, most useful first."""
        groups = {k: g for k, g in self._scan(self.service).items() if g[approvals.SURE] or g[approvals.NEEDS]}
        known = {f"season:{show}|{season}" for show, season in groups}
        stale = [r["key"] for r in self._query("SELECT key FROM reprise_notify_state WHERE key LIKE 'season:%'")]
        self._drop(*(key for key in stale if key not in known))  # a season with nothing waiting starts afresh
        found = []
        for (show, season), group in groups.items():
            sure, needs = group[approvals.SURE], group[approvals.NEEDS]
            ids = sorted({item["media_version_id"] for item in sure})
            key = f"season:{show}|{season}"
            current = {"c": len(sure), "n": len(needs), "ids": ids}
            before = self._get(key)
            if before is not None:
                grew = set(ids) - set(before["ids"]) or current["c"] > before["c"] or current["n"] > before["n"]
                if not grew:  # fewer than before (the owner dealt with some): nothing new to say
                    if before != current:
                        self._put(key, current, now)
                    continue
            found.append(self._season_message(show, season, sure, needs, ids, key, current))
        return found

    def _season_message(self, show, season, sure, needs, ids, key, current):
        count, looks = len(sure), len(needs)
        parts = [
            p
            for p in (_plural(count, "sure one", "sure ones") if count else "", f"{looks} to look at" if looks else "")
            if p
        ]
        suffix = f" {_season_short(season)}: {', '.join(parts)}"
        title = f"{(_ascii(show) or 'A show')[: max(8, 60 - len(suffix))]}{suffix}"
        lines = [f"{show} {_season_short(season)} has {' and '.join(parts)}."]
        if count:
            episodes = _episodes_text(i["episode"] for i in sure)
            lines.append(f"Sure ones: {episodes}. They are ready for you to approve.")
        if looks:
            lines.append(f"{_plural(looks, 'suggestion needs', 'suggestions need')} a look from you.")
        return {
            "rank": (1 if count else 2, -(count or looks), show.lower(), season or 0),
            "kind": "waiting",
            "key": key,
            "title": title,
            "message": "\n".join(lines),
            "tag": "clipboard",
            "button": (ACTION_APPROVE, {"show": show, "season": season, "ids": ids}, count) if count else None,
            "remember": [(key, current)],
        }

    def _automatic(self, now):
        """One message for what Reprise sent to Plex by itself since the last one (and no longer ago than a button
        stays good), or nothing."""
        recent = getattr(approvals, "recent_automatic", None)
        if recent is None:
            return []
        entries = recent(self.service, since=now - TOKEN_TTL_S)
        told = {r["key"] for r in self._query("SELECT key FROM reprise_notify_state WHERE key LIKE 'auto:%'")}
        new = [e for e in entries if f"auto:{e['change_id']}" not in told and e["sent_at"] >= now - TOKEN_TTL_S]
        if not new:
            return []
        new.reverse()  # the list is newest first; ``approvals.undo`` wants the oldest change first
        counts = {"intro": 0, "credits": 0}
        for entry in new:
            for kind in entry["kinds"]:
                counts[kind] = counts.get(kind, 0) + 1
        things = " and ".join(
            _plural(counts[kind], *_KIND_WORDS[kind]) for kind in ("intro", "credits") if counts.get(kind)
        )
        by_season = {}
        for entry in new:
            by_season.setdefault((entry["show"], entry["season"]), []).append(entry["episode"])
        where = "; ".join(
            f"{show} {_season_short(season)} {_episodes_text(eps)}" for (show, season), eps in by_season.items()
        )
        changes = [
            {
                "media_version_id": e["media_version_id"],
                "change_id": e["change_id"],
                "span_ids": e["span_ids"],
                "label": e["label"],
            }
            for e in new
        ]
        return [
            {
                "rank": (0,),
                "kind": "automatic",
                "key": "auto",
                "title": f"Reprise sent {things} to Plex",
                "message": f"Reprise sent {things} to Plex automatically: {where}. You can undo it for a few hours.",
                "tag": "robot",
                "button": (ACTION_UNDO, {"changes": changes}, 0),
                "remember": [(f"auto:{e['change_id']}", {"at": e["sent_at"]}) for e in new],
            }
        ]

    def _deliver(self, candidate, now):
        """Send one message with its buttons. True if the phone service took it."""
        base = public_url()
        page = f"{base}{APPROVE_PAGE}"
        actions = []
        if candidate["button"]:
            action, params, count = candidate["button"]
            if action == ACTION_UNDO:
                label = "Undo"
            else:
                label = f"Approve & send {count}" if approvals.sending(self.service)["enabled"] else f"Approve {count}"
            link = f"{base}{ACT_PATH}{self.issue(action, params, now)}"
            actions.append(f"http, {_action_label(label)}, {link}, method=POST, clear=true")
        actions.append(f"view, Open, {page}")
        failure = self.alerts.notify(
            candidate["title"], candidate["message"], tag=candidate["tag"], actions="; ".join(actions), click=page
        )
        if failure is not None:
            return False
        with self.service.store.transaction() as conn:
            conn.execute(
                "INSERT INTO reprise_notify_log(at,kind,key,title) VALUES (?,?,?,?)",
                (now, candidate["kind"], candidate["key"], candidate["title"]),
            )
            conn.execute(
                "DELETE FROM reprise_notify_log WHERE id NOT IN (SELECT id FROM reprise_notify_log ORDER BY id DESC LIMIT ?)",
                (KEEP_LOG,),
            )
            conn.execute("DELETE FROM reprise_notify_state WHERE key LIKE 'auto:%' AND at<?", (now - KEEP_AUTO_S,))
        for key, value in candidate["remember"]:
            self._put(key, value, now)
        return True


def _webhook_secret():
    from ..web.auth import get_webhook_secret

    return get_webhook_secret()
