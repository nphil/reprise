"""The Plex-versus-Reprise report: one read that says, episode by episode, where the skip buttons Plex shows differ
from what Reprise found.

Reprise does the comparing so a person (or an agent) reads the answer instead of working it out from raw ranges.
Nothing here decodes media, opens Plex's database or changes anything: it reads Reprise's own saved results and the
markers Plex serves over its API (``includeMarkers``), a few dozen episodes to a request. Reprise's side is only
compared when its analysis is complete and current; a changed file or a failed run says so instead of being compared.
"""

from __future__ import annotations

import re
import time
from dataclasses import replace
from datetime import UTC, datetime

from loguru import logger

from .models import SUGGESTION_ORIGINS, ReportRefused
from .service import _stale
from .uniqueness import COMPARISON_KEY, is_one_of_a_kind, sentence

# Plex stores markers to the millisecond and Reprise works on its own clock, so differences under a second are
# rounding, not disagreement.
TOLERANCE_MS = 1000
SCHEMA_VERSION = 1
# The plain text is for one cheap read by an agent: it never grows past this many bytes, whatever the season holds.
TEXT_LIMIT = 4000
# Episodes asked of Plex per request (a comma-separated list of rating keys; the address stays short).
PLEX_BATCH = 40
PLEX_KINDS = ("intro", "credits")

# code -> (severity, short token for the plain text, how the summary counts it). ``problem`` is Plex skipping story or
# a failed run, ``attention`` is something to look at or finish, ``info`` is worth knowing and needs nothing,
# ``good`` is done. Order is the order they are shown.
FLAGS = {
    "plex_cuts_into_keep": ("problem", "CUTS_KEEP", "where Plex skips into a scene you protected"),
    "plex_starts_early": ("problem", "EARLY", "where Plex's skip starts too early"),
    "plex_ends_late": ("problem", "LATE", "where Plex's skip ends too late"),
    "plex_missing": ("attention", "NO_PLEX", "where Plex has no skip Reprise found"),
    "plex_covers_unconfirmed": (
        "info",
        "COVERS_UNCONFIRMED",
        "where Plex's skip covers something Reprise could not confirm",
    ),
    "unconfirmed": ("attention", "UNCONFIRMED", "with only unconfirmed suggestions"),
    "one_of_a_kind": ("info", "UNIQUE", "where the intro or credits is one of a kind, so is not skipped"),
    "failed": ("problem", "FAILED", "where the analysis failed"),
    "stale": ("attention", "STALE", "with an out-of-date analysis"),
    "not_analyzed": ("attention", "NOT_ANALYZED", "not analyzed yet"),
    "published": ("good", "PUBLISHED", "sent to Plex"),
}

_PLAIN_REASONS = (
    (r"^unapproved_or_unverified$", "Nothing is approved yet, so nothing will be skipped."),
    (r"^Media analysis failed$", "Reprise couldn't read this file well enough to compare it."),
    (r"^Media is unsupported$", "Reprise can't analyze this kind of file yet."),
    (r"^Analysis cancelled$", "The analysis was cancelled before it finished."),
    (r"^Analysis failed or media changed$", "The analysis failed, or the file changed while it was running."),
    (r"^Analysis did not complete$", "The analysis did not finish."),
    (r"^Binding changed; reanalysis required$", "This file changed. Analyze it again."),
    (
        r"^Plex mapping or local media has changed or is unavailable$",
        "Reprise can't find this file where Plex says it is.",
    ),
    (r"^Reference media is stale or unavailable$", "An episode used for comparison has changed. Analyze again."),
    (
        r"^Reference unsupported",
        "Another episode couldn't be used for comparison because it uses a different video or audio format.",
    ),
)
_CODE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$")

_FAILED_TEXT = {
    "failed": "Analysis failed",
    "cancelled": "Analysis was cancelled",
    "unsupported": "Reprise can't analyze this file",
}
_NOT_COMPARED = {
    "missing": "Not analyzed yet.",
    "running": "Being analyzed now.",
    "failed": "The analysis failed, so there is nothing to compare.",
    "cancelled": "The analysis was cancelled, so there is nothing to compare.",
    "unsupported": "Reprise can't analyze this file, so there is nothing to compare.",
}


def plain_reason(text):
    """One saved reason in plain words (the raw text stays in the API's ``reasons``)."""
    text = str(text or "")
    for pattern, plain in _PLAIN_REASONS:
        if re.match(pattern, text, re.IGNORECASE):
            return plain
    return "Reprise needs attention here." if _CODE.match(text) else text


def seconds_text(ms):
    """``52 s``, ``4.5 s``: whole seconds from ten up, one decimal below."""
    value = ms / 1000
    return f"{value:.0f} s" if value >= 9.95 else f"{value:.1f} s".replace(".0 s", " s")


def clock(ms):
    """``m:ss`` (``h:mm:ss`` from an hour), the unit a person reads off a player."""
    total = max(0, int(ms) // 1000)
    hours, minutes, secs = total // 3600, total // 60 % 60, total % 60
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _confirmed(ranges):
    """Reprise's confirmed skips, with pieces of one kind that touch exactly joined into one run (as they are sent to
    Plex), so a marker is compared with the whole confirmed intro or credits, not its first piece."""
    pieces = sorted(
        (
            r
            for r in ranges
            if r.action == "skip" and r.origin not in SUGGESTION_ORIGINS and r.review_status != "rejected"
        ),
        key=lambda r: (r.kind, r.start_ms, r.end_ms),
    )
    joined = []
    for r in pieces:
        if joined and joined[-1].kind == r.kind and joined[-1].end_ms == r.start_ms:
            joined[-1] = replace(joined[-1], end_ms=r.end_ms, terminal=r.terminal)
        else:
            joined.append(r)
    return joined


def _suggested(ranges):
    return [
        r for r in ranges if r.action == "skip" and r.origin in SUGGESTION_ORIGINS and r.review_status != "rejected"
    ]


def _keeps(ranges):
    return [r for r in ranges if r.action == "keep" and r.review_status != "rejected"]


def _protected(r):
    """Whether a keep is the owner's own decision (added, locked or reviewed by them). Only those are a scene to
    protect: a keep the analysis made (reason code ``native_signal_unknown``) says only that comparing episodes could
    not confirm that stretch. The analysis never records that content differs, so no keep is a proven-unique scene."""
    return r.origin == "manual" or r.locked or r.review_status != "proposed"


# What an analysis from before the per-kind facts said when no episode shared anything (analysis.py ``_no_repeat_found``).
_NOTHING_REPEATED = re.compile(
    r"^Compared with (\d+) other episodes? of this season, but no repeating intro or credits"
)


def _comparisons(result):
    """``{kind: facts}``: what the analysis recorded about each kind's comparison with other episodes. A result from
    before the facts were kept has only the note saying nothing repeated, which holds for both kinds."""
    if result is None:
        return {}
    found = {}
    for e in result.evidence:
        facts = e.metrics.get(COMPARISON_KEY)
        if isinstance(facts, dict) and "kind" in facts:
            found.setdefault(facts["kind"], facts)
    if found:
        return found
    for reason in result.reasons:
        match = _NOTHING_REPEATED.match(reason)
        if match:
            return {
                kind: {
                    "kind": kind,
                    "episodes_searched": int(match.group(1)),
                    "sound_matched": 0,
                    "picture_matched": 0,
                    "confirmed_ms": 0,
                }
                for kind in PLEX_KINDS
            }
    return {}


def _overlap(start, end, spans):
    """Milliseconds of ``[start, end)`` that fall inside any of ``spans``, counted once where spans overlap."""
    total, edge = 0, start
    for left, right in sorted((s, e) for s, e in spans if e > start and s < end):
        left = max(left, edge)
        right = min(right, end)
        if right > left:
            total += right - left
            edge = right
    return total


def _nearest(marker, confirmed):
    """The confirmed Reprise range of the marker's kind that it is most likely about: the most shared time, else the
    closest start."""
    same = [r for r in confirmed if r.kind == marker["kind"]]
    if not same:
        return None
    return max(
        same,
        key=lambda r: (
            _overlap(marker["start_ms"], marker["end_ms"], [(r.start_ms, r.end_ms)]),
            -abs(r.start_ms - marker["start_ms"]),
        ),
    )


def classify(
    *, status, freshness, stale, plex, ranges, duration_ms, published, unknown_kinds=frozenset(), comparisons=None
):
    """Every flag one episode earns, worst first. A flag is ``{code, severity, kind, seconds, text}``.

    ``plex`` is the markers Plex serves (``{kind, start_ms, end_ms}`` each) or None when Plex could not be read;
    ``unknown_kinds`` are the kinds Plex hides in this library, whose absence says nothing. Reprise's ranges are only
    compared with Plex's when the analysis is complete and current. ``comparisons`` is what the analysis found about
    each kind (see ``_comparisons``): a kind shared by none of the episodes compared is flagged ``one_of_a_kind``."""
    comparisons = comparisons or {}
    found = {}

    def add(code, text, kind=None, seconds_ms=None):
        key = (code, kind)
        if key in found and (seconds_ms or 0) <= (found[key]["_ms"] or 0):
            return
        found[key] = {
            "code": code,
            "severity": FLAGS[code][0],
            "kind": kind,
            "seconds": None if seconds_ms is None else round(seconds_ms / 1000, 1),
            "text": text,
            "_ms": seconds_ms,
        }

    if status == "missing":
        add("not_analyzed", "Not analyzed yet")
    elif status in _FAILED_TEXT:
        add("failed", _FAILED_TEXT[status])
    if freshness == "stale" and status != "missing":
        add("stale", (stale or {}).get("label") or "File changed")
    if status == "complete" and freshness == "current":
        confirmed, suggested, keeps = _confirmed(ranges), _suggested(ranges), _keeps(ranges)
        alone = set()  # the kinds shared by none of the episodes compared: not skipped, and nothing left to confirm
        for kind in PLEX_KINDS:
            facts = comparisons.get(kind)
            if (
                facts is not None
                and not any(r.kind == kind for r in confirmed)
                and any(r.kind == kind for r in (*suggested, *keeps))
                and is_one_of_a_kind(facts, sum(r.end_ms - r.start_ms for r in suggested if r.kind == kind))
            ):
                alone.add(kind)
                add("one_of_a_kind", sentence(kind, facts), kind)
        pending = [r for r in suggested if r.kind not in alone]
        if pending and not confirmed:
            names = " and ".join(sorted({r.kind for r in pending}))
            add("unconfirmed", f"Reprise could not confirm the {names}")
        if plex is not None:
            owned = [(r.start_ms, r.end_ms) for r in keeps if _protected(r)]
            unsure = [(r.start_ms, r.end_ms) for r in keeps if not _protected(r)]
            for marker in plex:
                kind = marker["kind"]
                end = min(marker["end_ms"], duration_ms) if duration_ms else marker["end_ms"]
                lost = _overlap(marker["start_ms"], end, owned)
                if lost > TOLERANCE_MS:
                    add(
                        "plex_cuts_into_keep",
                        f"Plex's {kind} skip cuts {seconds_text(lost)} out of a scene you protected",
                        kind,
                        lost,
                    )
                uncertain = _overlap(marker["start_ms"], end, unsure)
                if uncertain > TOLERANCE_MS:
                    add(
                        "plex_covers_unconfirmed",
                        f"Plex's {kind} skip covers {seconds_text(uncertain)} Reprise could not confirm",
                        kind,
                        uncertain,
                    )
                ref = _nearest(marker, confirmed)
                if ref is None:
                    continue
                early = ref.start_ms - marker["start_ms"]
                if early > TOLERANCE_MS:
                    add("plex_starts_early", f"Skips {seconds_text(early)} of story before the {kind}", kind, early)
                late = end - ref.end_ms
                runs_to_end = ref.terminal or (duration_ms and ref.end_ms >= duration_ms - TOLERANCE_MS)
                if late > TOLERANCE_MS and not runs_to_end:
                    add("plex_ends_late", f"Skips {seconds_text(late)} of story after the {kind}", kind, late)
            served = {m["kind"] for m in plex}
            for kind in PLEX_KINDS:
                if kind not in served and kind not in unknown_kinds and any(r.kind == kind for r in confirmed):
                    add("plex_missing", f"Plex has no {kind} skip, Reprise found one", kind)
    if published:
        add("published", "Sent to Plex")
    order = list(FLAGS)
    ordered = sorted(found.values(), key=lambda f: (order.index(f["code"]), f["kind"] or ""))
    for flag in ordered:
        del flag["_ms"]
    return ordered


def _range_json(r):
    return {
        "id": r.id,
        "kind": r.kind,
        "start_ms": r.start_ms,
        "end_ms": r.end_ms,
        "origin": r.origin,
        "review_status": r.review_status,
        "locked": r.locked,
        "suggested_by": list(r.suggested_by or (["Plex"] if r.origin == "plex" else [])),
    }


def _verdict(status, flags):
    codes = {f["code"] for f in flags}
    if codes & {"plex_cuts_into_keep", "plex_starts_early", "plex_ends_late", "plex_missing"}:
        return "differs"
    if codes & {"failed", "stale", "not_analyzed", "unconfirmed"}:
        return "needs_attention"
    return "pending" if status == "running" else "ok"


def _sentence(status, flags, plex, confirmed):
    problems = [f["text"] for f in flags if f["severity"] != "good"]
    if problems:
        return ". ".join(problems) + "."
    if status != "complete":
        return _NOT_COMPARED.get(status, "Not compared.")
    if plex is None:
        return "Plex could not be read, so its skips were not compared."
    if not confirmed and not plex:
        return "Reprise found nothing to skip, and Plex has no skip either."
    return "Plex and Reprise agree."


def _by_time(ranges):
    return sorted(ranges, key=lambda r: (r.start_ms, r.end_ms))


def episode_row(item, result, current, why, plex, unknown_kinds, published_revision):
    """One report row. ``item``: catalogue facts; ``result``: the saved ``AnalysisResult`` or None; ``current``: the
    service's lookup answer; ``why``: the stale reason code; ``plex``: markers, None when unread."""
    status = current["analysis_status"]
    freshness = current["freshness"]
    stale = _stale(why) if why else None
    ranges = result.ranges if result else ()
    published = published_revision is not None
    flags = classify(
        status=status,
        freshness=freshness,
        stale=stale,
        plex=plex,
        ranges=ranges,
        duration_ms=item["duration_ms"],
        published=published,
        unknown_kinds=unknown_kinds,
        comparisons=_comparisons(result),
    )
    confirmed = _confirmed(ranges)
    season, episode = item["season"], item["episode"]
    reasons = [r for r in (result.reasons if result else current["reasons"]) if r != "unapproved_or_unverified"]
    return {
        "id": item["id"],
        "server_id": item["server_id"],
        "show_title": item["show_title"],
        "season": season,
        "episode": episode,
        "title": item["title"],
        "label": f"S{season}E{int(episode):02d}" if isinstance(season, int) and isinstance(episode, int) else "",
        "duration_ms": item["duration_ms"],
        "verdict": _verdict(status, flags),
        "summary": _sentence(status, flags, plex, confirmed),
        "flags": flags,
        "analysis": {
            "status": status,
            "freshness": freshness,
            "stale": stale,
            "result_revision": current["result_revision"],
            "reasons": [plain_reason(r) for r in reasons[:3]],
        },
        "plex": {
            "status": "unavailable" if plex is None else "read",
            "markers": [{k: m[k] for k in ("kind", "start_ms", "end_ms")} for m in plex or ()],
            "unknown_kinds": sorted(unknown_kinds),
        },
        "reprise": {
            "skips": [_range_json(r) for r in _by_time(confirmed)],
            "suggestions": [_range_json(r) for r in _by_time(_suggested(ranges))],
            "keeps": [{**_range_json(r), "protected": _protected(r)} for r in _by_time(_keeps(ranges))],
            "to_review": sum(1 for r in confirmed if r.review_status == "proposed"),
            "approved": sum(1 for r in confirmed if r.review_status == "approved"),
        },
        "published": published,
        "published_revision": published_revision,
    }


def _plural(n, one, many=None):
    return f"{n} {one if n == 1 else many or one + 's'}"


def summarize(rows, plex_status):
    """Counts per flag and the one line that says what the season comes to."""
    counts = {code: sum(1 for row in rows if any(f["code"] == code for f in row["flags"])) for code in FLAGS}
    phrases = {code: f"{_plural(n, 'episode')} {FLAGS[code][2]}" for code, n in counts.items() if n}
    shown = [phrases[code] for code in FLAGS if code in phrases and FLAGS[code][0] != "good"]
    analyzed = sum(
        1 for row in rows if row["analysis"]["status"] == "complete" and row["analysis"]["freshness"] == "current"
    )
    if plex_status == "unavailable":
        shown.insert(0, "Plex could not be read, so its skips were not compared")
    headline = (
        "; ".join(shown)
        if shown
        else f"No differences: Plex and Reprise agree on all {_plural(analyzed, 'analyzed episode')}."
    )
    return {
        "episodes": len(rows),
        "analyzed": analyzed,
        "counts": counts,
        "phrases": phrases,
        "headline": headline,
        "differs": sum(1 for row in rows if row["verdict"] == "differs"),
        "needs_attention": sum(1 for row in rows if row["verdict"] == "needs_attention"),
    }


# ── gathering ────────────────────────────────────────────────────────────────────────────────────────────────────


def _pick_show(titles, wanted):
    """The one show title ``wanted`` names: an exact match (any case) wins, else the only title that contains it."""
    wanted = " ".join(str(wanted or "").split())
    if not wanted:
        raise ReportRefused("Say which show to report on.")
    low = wanted.lower()
    exact = [t for t in titles if t.lower() == low]
    if exact:
        return exact[0]
    partial = [t for t in titles if low in t.lower()]
    if len(partial) == 1:
        return partial[0]
    if partial:
        raise ReportRefused(
            f"More than one show matches '{wanted}': {', '.join(sorted(partial)[:6])}. Use the full title."
        )
    raise ReportRefused(f"Reprise has not read a show called '{wanted}'. Refresh the library if it is new.")


def _marker_json(node):
    try:
        kind = node.get("type")
        if kind not in PLEX_KINDS:
            return None
        return {
            "kind": kind,
            "start_ms": int(node.get("startTimeOffset") or 0),
            "end_ms": int(node.get("endTimeOffset") or 0),
        }
    except (TypeError, ValueError):
        return None


def read_plex_markers(server, rating_keys):
    """``({rating_key: [marker, ...]}, error)``: the intro and credits markers Plex serves now, a batch at a time
    (``includeMarkers``). A key Plex does not return, or a batch that failed, is simply absent; ``error`` says why."""
    found, error = {}, None
    keys = list(dict.fromkeys(rating_keys))
    for start in range(0, len(keys), PLEX_BATCH):
        chunk = keys[start : start + PLEX_BATCH]
        try:
            container = server._plex_http(
                "get", "/library/metadata/" + ",".join(chunk), params={"includeMarkers": "1"}
            ).json()["MediaContainer"]
        except Exception as exc:
            logger.debug("Plex markers could not be read for the report: {}", exc)
            error = "Plex could not be reached"
            continue
        for item in container.get("Metadata", []) or []:
            found[str(item.get("ratingKey"))] = [m for m in map(_marker_json, item.get("Marker", []) or []) if m]
    return found, error


def _hidden_kinds(server, library_id):
    """The marker kinds a Plex library hides (its setting is off), or both when that cannot be read: Plex then
    serves none of them, so an absent marker says nothing about the episode."""
    reader = getattr(server, "hidden_marker_types", None)
    if reader is None:
        return frozenset()
    try:
        hidden = reader(library_id)
    except Exception:
        hidden = None
    return frozenset(PLEX_KINDS) if hidden is None else frozenset(hidden)


def build_report(service, show, season=None):
    """The whole report for one show (or one season of it). Read-only; see the module docstring."""
    catalogue = service.store.catalogue()
    titles = sorted({str(r["metadata"].get("show_title")) for r in catalogue if r["metadata"].get("show_title")})
    title = _pick_show(titles, show)
    seen, picked = set(), []
    for row in catalogue:
        meta = row["metadata"]
        if row["version_id"] in seen or str(meta.get("show_title")) != title:
            continue
        seen.add(row["version_id"])
        if season is not None and str(meta.get("season")) != str(season):
            continue
        picked.append(row)
    if not picked:
        have = sorted(
            {
                r["metadata"].get("season")
                for r in catalogue
                if str(r["metadata"].get("show_title")) == title and r["metadata"].get("season") is not None
            }
        )
        raise ReportRefused(
            f"Reprise has no season {season} of {title}." + (f" It has: {', '.join(map(str, have))}." if have else "")
        )
    picked.sort(
        key=lambda r: (
            r["metadata"].get("season") is None,
            r["metadata"].get("season") or 0,
            r["metadata"].get("episode") or 0,
            r["version_id"],
        )
    )

    entries = []
    for row in picked:
        version_id, meta = row["version_id"], row["metadata"]
        bindings = service._bindings(version_id)
        if not bindings:
            continue
        identity = bindings[0][0]
        current, why = service._lookup(identity)
        entries.append(
            {
                "item": {
                    "id": version_id,
                    "server_id": row["server_id"],
                    "show_title": title,
                    "season": meta.get("season"),
                    "episode": meta.get("episode"),
                    "title": meta.get("title", ""),
                    "duration_ms": identity.duration_ms,
                },
                "rating_key": identity.rating_key,
                "library_id": str(meta.get("library_id", "")),
                "result": service.store.get_result(version_id) if current["analysis_status"] != "missing" else None,
                "current": current,
                "why": why,
            }
        )

    markers, errors, hidden = {}, set(), {}
    for server_id in dict.fromkeys(e["item"]["server_id"] for e in entries):
        group = [e for e in entries if e["item"]["server_id"] == server_id]
        server = service.registry.get(server_id)
        if server is None:
            errors.add("Plex is not connected")
            continue
        found, error = read_plex_markers(server, [e["rating_key"] for e in group])
        if error:
            errors.add(error)
        markers[server_id] = found
        for library in {e["library_id"] for e in group}:
            hidden[(server_id, library)] = _hidden_kinds(server, library)

    published = {}
    if service.publisher is not None:
        published = service.publisher.published_versions([e["item"]["id"] for e in entries])

    rows = []
    for e in entries:
        server_id = e["item"]["server_id"]
        plex = markers.get(server_id, {}).get(e["rating_key"])
        rows.append(
            episode_row(
                e["item"],
                e["result"],
                e["current"],
                e["why"],
                plex,
                hidden.get((server_id, e["library_id"]), frozenset()),
                published.get(e["item"]["id"]),
            )
        )
    unread = sum(1 for row in rows if row["plex"]["status"] == "unavailable")
    plex_status = "unavailable" if rows and unread == len(rows) else "partly" if unread else "read"
    return {
        "schema_version": SCHEMA_VERSION,
        "show": title,
        "season": season if season is None else int(season),
        "generated_at": datetime.fromtimestamp(time.time(), UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tolerance_ms": TOLERANCE_MS,
        "plex": {
            "status": plex_status,
            "message": "; ".join(sorted(errors)) or ("Some episodes are not in Plex any more." if unread else None),
        },
        "summary": summarize(rows, plex_status),
        "episodes": rows,
    }


# ── plain text ───────────────────────────────────────────────────────────────────────────────────────────────────


def _spans(markers):
    """``i3:37-4:06 c21:16-21:40`` (``-`` when there are none)."""
    ordered = sorted(
        markers, key=lambda m: (PLEX_KINDS.index(m["kind"]) if m["kind"] in PLEX_KINDS else 9, m["start_ms"])
    )
    return " ".join(f"{m['kind'][0]}{clock(m['start_ms'])}-{clock(m['end_ms'])}" for m in ordered) or "-"


def _union(spans):
    """Overlapping or touching ``(start, end)`` spans as one, in order."""
    merged = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def _flag_token(flag):
    token = FLAGS[flag["code"]][1]
    detail = " ".join(
        x
        for x in (
            flag["kind"][0] if flag["kind"] else "",
            f"{flag['seconds']:g}s" if flag["seconds"] is not None else "",
        )
        if x
    )
    return f"{token}({detail})" if detail else token


def _keep_spans(row, protected):
    """``0:30-0:45 1:00-1:10``: the kept ranges the owner protected (or, not ``protected``, those Reprise could not
    confirm), touching ones as one."""
    return " ".join(
        f"{clock(s)}-{clock(e)}"
        for s, e in _union((r["start_ms"], r["end_ms"]) for r in row["reprise"]["keeps"] if r["protected"] is protected)
    )


def _row_line(row):
    plex = "?" if row["plex"]["status"] == "unavailable" else _spans(row["plex"]["markers"])
    mine = _spans(row["reprise"]["skips"] or row["reprise"]["suggestions"])
    keeps = _keep_spans(row, True)
    unsure = _keep_spans(row, False)
    flags = " ".join(_flag_token(f) for f in row["flags"]) or "OK"
    state = "" if row["analysis"]["status"] in ("complete", "missing") else f"[{row['analysis']['status']}] "
    parts = [f"{row['label'] or '?'} {state}P {plex}", f"R {mine}"]
    if keeps:
        parts.append(f"K {keeps}")
    if unsure:
        parts.append(f"U {unsure}")
    return " | ".join(parts) + " | " + flags


def render_text(report):
    """A short plain-text table of the report: under ``TEXT_LIMIT`` bytes however big the season is. One season lists
    every episode; a whole show lists one line per season, because a table of every episode would not fit."""
    scope = report["show"] + (f" S{report['season']}" if report["season"] is not None else "")
    summary = report["summary"]
    lines = [
        f"{scope}: Plex vs Reprise, {_plural(summary['episodes'], 'episode')} ({report['generated_at']})",
        summary["headline"],
    ]
    if report["season"] is None:
        seasons = sorted({row["season"] for row in report["episodes"]}, key=lambda s: (s is None, s or 0))
        for number in seasons:
            rows = [row for row in report["episodes"] if row["season"] == number]
            line = summarize(rows, report["plex"]["status"])
            bad = [f"{n} {FLAGS[c][1]}" for c, n in line["counts"].items() if n and FLAGS[c][0] != "good"]
            lines.append(f"S{number}: {_plural(len(rows), 'episode')}; " + (", ".join(bad) if bad else "OK"))
        lines.append("Add season=N for one line per episode.")
    else:
        lines.append(
            "m:ss. P Plex, R Reprise (i intro, c credits), K scene you protected, U range Reprise could not confirm. "
            "Under 1 s is not flagged."
        )
        if report["plex"]["status"] != "read":
            lines.append("Plex: " + (report["plex"]["message"] or "not all episodes could be read") + ".")
        budget = TEXT_LIMIT - sum(len(x.encode()) + 1 for x in lines) - 80
        shown = 0
        for row in report["episodes"]:
            line = _row_line(row)
            if len(line.encode()) + 1 > budget:
                break
            lines.append(line)
            budget -= len(line.encode()) + 1
            shown += 1
        if shown < len(report["episodes"]):
            lines.append(f"... {len(report['episodes']) - shown} more episodes not shown (text limit).")
    return ("\n".join(lines) + "\n").encode()[:TEXT_LIMIT].decode(errors="ignore")
