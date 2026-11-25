#!/usr/bin/env python3
"""How well does the local vision check find intros and credits, measured against times a person already settled?

Read only. Run inside the Reprise container, where ffmpeg and the media are, with ``--module-dir`` pointing at a copy of
``localvision.py`` and ``visionmatch.py`` when the installed image has an older one. The truth is every approved owner
span (origin "owner") in the Reprise store (opened read-only), or a JSON list given with --truth
(``[{"path", "kind", "start_ms", "end_ms"}]``, plus ``{"path", "kind", "none": true}`` for an episode known to have no
such span). Each episode is checked as a new show would be (its nearest other episodes of the season are the
neighbours; no chapter marks or other sources' times), one episode at a time through the model that is already loaded.
Nothing is written anywhere.

Prints, per kind: how many spans were proposed or missed, how many starts and ends fall within 1 s of the truth, the
median and largest error, the "dangerous" ones (seconds the span covers beyond the true span on either side: 0.5, 2 and
5 s), the confidence levels and how often "high" was right, and what each episode cost (seconds, model questions).
``--split heldout`` keeps the episodes a fixed rule (a hash of the path) sets aside, ``dev`` the rest.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import sqlite3
import statistics
import sys
import time
import types
import urllib.request
from pathlib import Path


def load_module(directory: Path):
    """``localvision`` imported as part of a package, so its relative import of ``visionmatch`` works from any folder."""
    package = types.ModuleType("reprise_vision")
    package.__path__ = [str(directory)]  # type: ignore[attr-defined]
    sys.modules["reprise_vision"] = package
    return importlib.import_module("reprise_vision.localvision")


def held_out(path: str) -> bool:
    return int(hashlib.sha1(path.encode()).hexdigest()[:2], 16) % 5 in (0, 1)


def store_rows(db: str, show: str):
    connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    episodes = []
    for media, meta in connection.execute(
        "SELECT v.data, b.metadata FROM reprise_bindings b JOIN reprise_media_versions v ON v.id=b.version_id WHERE b.active=1"
    ):
        media, meta = json.loads(media), json.loads(meta)
        if meta.get("show_title") == show:
            episodes.append(
                {
                    "path": media["canonical_path"],
                    "duration_ms": media["duration_ms"],
                    "season": meta.get("season"),
                    "episode": meta.get("episode") or 0,
                }
            )
    truths = []
    by_path = {e["path"]: e for e in episodes}
    for media, span in connection.execute(
        "SELECT v.data, r.data FROM reprise_ranges r JOIN reprise_media_versions v ON v.id=r.version_id "
        'WHERE r.data LIKE \'%"origin":"owner"%\''
    ):
        media, span = json.loads(media), json.loads(span)
        if span["review_status"] == "approved" and media["canonical_path"] in by_path:
            truths.append(
                {
                    "path": media["canonical_path"],
                    "kind": span["kind"],
                    "start_ms": span["start_ms"],
                    "end_ms": span["end_ms"],
                }
            )
    return episodes, truths


def models_loaded(cfg) -> list[str]:
    try:
        with urllib.request.urlopen(cfg.base_url.rsplit("/v1", 1)[0] + "/running", timeout=5) as response:
            return sorted(m["model"] for m in json.load(response).get("running", []))
    except (OSError, ValueError):
        return []


def score(row: dict) -> dict:
    """The errors of one proposed span against its truth: seconds off at each end, and the seconds it covers beyond."""
    truth, got = row["truth"], row["got"]
    row["start_error_s"], row["end_error_s"] = (got[0] - truth[0]) / 1000, (got[1] - truth[1]) / 1000
    row["beyond_s"] = (max(0, truth[0] - got[0]) + max(0, got[1] - truth[1])) / 1000
    return row


def summarize(rows: list[dict], kind: str) -> str:
    mine = [r for r in rows if r["kind"] == kind]
    spans = [r for r in mine if r["truth"] is not None]
    none = [r for r in mine if r["truth"] is None]
    found = [score(r) for r in spans if r["got"] is not None]
    pct = lambda n, d: f"{100 * n / d:.0f}%" if d else "n/a"  # noqa: E731
    starts = [abs(r["start_error_s"]) for r in found]
    ends = [abs(r["end_error_s"]) for r in found]
    high = [r for r in mine if r["confidence"] == "high" and r["got"] is not None]
    wrong_high = [r for r in high if r["truth"] is None or r["beyond_s"] > 2]
    lines = [
        f"{kind}: {len(spans)} with a span, {len(found)} proposed, {len(spans) - len(found)} missed; "
        f"{len(none)} with none, {sum(1 for r in none if r['got'] is not None)} given a span anyway",
        f"  within 1 s: start {pct(sum(1 for s in starts if s <= 1), len(found))}, end {pct(sum(1 for e in ends if e <= 1), len(found))}; "
        f"median error start {statistics.median(starts) if starts else float('nan'):.1f} s end {statistics.median(ends) if ends else float('nan'):.1f} s; "
        f"largest start {max(starts, default=float('nan')):.1f} s end {max(ends, default=float('nan')):.1f} s",
        "  beyond the true span: "
        + ", ".join(f"over {limit} s: {sum(1 for r in found if r['beyond_s'] > limit)}" for limit in (0.5, 2, 5)),
        "  confidence: "
        + ", ".join(
            f"{level} {sum(1 for r in mine if r['confidence'] == level)}" for level in ("high", "medium", "low")
        )
        + f"; high and wrong (over 2 s beyond, or a span where there is none): {len(wrong_high)} of {len(high)}",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="/config/markers.db")
    parser.add_argument("--truth", help="a JSON file of truths instead of the owner spans in the store")
    parser.add_argument("--show", default="The Apothecary Diaries")
    parser.add_argument("--kinds", default="intro,credits")
    parser.add_argument("--split", choices=("all", "heldout", "dev"), default="all")
    parser.add_argument("--limit", type=int, default=0, help="episodes at most, spread over the seasons (0: all)")
    parser.add_argument(
        "--module-dir", default=str(Path(__file__).resolve().parent.parent / "media_preview_generator/reprise")
    )
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--json", action="store_true", help="print every row as JSON as well")
    args = parser.parse_args()
    lv = load_module(Path(args.module_dir))
    cfg = lv.Config(True, (args.base_url or lv.DEFAULT_URL).rstrip("/"), args.model or lv.DEFAULT_MODEL)
    if not lv.model_ready(cfg):
        print(f"{cfg.model} is not loaded at {cfg.base_url}: nothing run", file=sys.stderr)
        return 2
    kinds = args.kinds.split(",")
    episodes, truths = store_rows(args.db, args.show)
    if args.truth:
        truths = json.loads(Path(args.truth).read_text())
    wanted = {"heldout": held_out, "dev": lambda p: not held_out(p), "all": lambda p: True}[args.split]
    truth_of = {(t["path"], t["kind"]): None if t.get("none") else (t["start_ms"], t["end_ms"]) for t in truths}
    chosen = sorted(
        (e for e in episodes if wanted(e["path"]) and any((e["path"], k) in truth_of for k in kinds)),
        key=lambda e: (e["season"] or 0, e["episode"]),
    )
    if args.limit:
        chosen = chosen[:: max(1, len(chosen) // args.limit)][: args.limit]
    asked = {"n": 0}
    real_judge = lv.judge

    def counted(cfg, jpeg):
        asked["n"] += 1
        return real_judge(cfg, jpeg)

    lv.judge = counted
    source, before, rows, costs = lv.FfmpegSource(args.ffmpeg), models_loaded(cfg), [], []
    for ep in chosen:
        near = sorted(
            (o for o in episodes if o["season"] == ep["season"] and o["path"] != ep["path"]),
            key=lambda o: (abs(o["episode"] - ep["episode"]), o["episode"]),
        )
        anchors = {
            k: [(0, 120000) if k == "intro" else (max(0, ep["duration_ms"] - 300000), ep["duration_ms"])]
            for k in kinds
            if (ep["path"], k) in truth_of
        }
        began, questions = time.monotonic(), asked["n"]
        verdicts = lv.check(
            cfg,
            source,
            lv.Media(ep["path"], ep["duration_ms"], None),
            anchors,
            references=[lv.Media(o["path"], o["duration_ms"], None) for o in near[: lv.NEIGHBOURS]],
        )
        costs.append((time.monotonic() - began, asked["n"] - questions))
        for kind in anchors:
            v = verdicts.get(kind)
            rows.append(
                {
                    "episode": f"S{ep['season']:02d}E{ep['episode']:02d}",
                    "kind": kind,
                    "truth": truth_of[(ep["path"], kind)],
                    "got": None if v is None else (v.start_ms, v.end_ms),
                    "confidence": None if v is None else v.confidence,
                    "reasons": [] if v is None else list(v.reasons),
                }
            )
        print(
            ep["path"].rsplit("/", 1)[-1][:50],
            {
                k: (
                    None
                    if verdicts.get(k) is None
                    else (verdicts[k].start_ms, verdicts[k].end_ms, verdicts[k].confidence)
                )
                for k in anchors
            },
            f"{costs[-1][0]:.0f} s, {costs[-1][1]} model questions",
            flush=True,
        )
    if args.json:
        print(json.dumps(rows, indent=1))
    for kind in kinds:
        print(summarize(rows, kind))
    seconds = [c[0] for c in costs]
    print(
        f"{len(costs)} episodes: median {statistics.median(seconds) if seconds else 0:.0f} s, longest {max(seconds, default=0):.0f} s, "
        f"{sum(c[1] for c in costs) / max(1, len(costs)):.1f} model questions each; "
        f"models loaded before {before}, after {models_loaded(cfg)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
