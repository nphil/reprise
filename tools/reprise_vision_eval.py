#!/usr/bin/env python3
"""How well does the local vision check find intros and credits the owner has already set exactly?

Read only. The truth is every approved owner span (origin "owner") in the Reprise store (opened read-only), or a JSON
list given with --truth (``[{"path", "duration_ms", "kind", "start_ms", "end_ms"}]``). For each episode the check is
run as the analysis would run it, from a rough suggestion standing in for the first guess: the true span moved by
--shift seconds (earlier at the start, later at the end alternately by episode). Nothing is written anywhere.

Prints, per kind: the boundary error in seconds, and the dangerous mistakes: story frames called credits or titles, and
seconds of story the proposed span would skip over. Run inside the Reprise container, where ffmpeg and the media are.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys

try:
    from media_preview_generator.reprise import localvision
except ImportError:  # run from a copy of localvision.py next to this file (no package installed)
    import localvision


def truths(db: str, show: str, kinds: set[str]) -> list[dict]:
    connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = connection.execute(
        "SELECT v.data, r.data FROM reprise_ranges r JOIN reprise_media_versions v ON v.id=r.version_id "
        'WHERE r.data LIKE \'%"origin":"owner"%\''
    ).fetchall()
    found = []
    for media, span in rows:
        media, span = json.loads(media), json.loads(span)
        if (
            span["origin"] == "owner"
            and span["review_status"] == "approved"
            and span["kind"] in kinds
            and show in media["canonical_path"]
        ):
            found.append(
                {
                    "path": media["canonical_path"],
                    "duration_ms": media["duration_ms"],
                    "kind": span["kind"],
                    "start_ms": span["start_ms"],
                    "end_ms": span["end_ms"],
                }
            )
    return sorted(found, key=lambda t: (t["kind"], t["path"]))


def evaluate(cfg, ffmpeg: str, item: dict, shift_ms: int, index: int) -> dict:
    start, end, duration, kind = item["start_ms"], item["end_ms"], item["duration_ms"], item["kind"]
    sign = 1 if index % 2 else -1
    anchor = (max(0, start + sign * shift_ms), min(duration, max(start + 1000, end - sign * shift_ms)))
    verdicts = localvision.check(cfg, lambda ms: localvision.grab(ffmpeg, item["path"], ms), duration, {kind: [anchor]})
    verdict = verdicts.get(kind)
    result = {"path": item["path"].rsplit("/", 1)[-1][:60], "kind": kind, "truth": (start, end)}
    if verdict is None:
        return {**result, "proposed": None, "frames": 0, "story_as_credits": 0, "story_skipped_s": 0.0}
    # Truth for a frame: inside the true span it is credits or titles (previews included for credits), outside it is story.
    story_as_credits = sum(
        1 for t, scene, _ in verdict.frames if scene == "credits" and not start - 1000 <= t <= end + 1000
    )
    # Seconds the proposed span would skip that the owner's span does not (on either side): the dangerous direction.
    skipped = max(0, start - verdict.start_ms) + max(0, verdict.end_ms - end)
    return {
        **result,
        "proposed": (verdict.start_ms, verdict.end_ms),
        "start_error_s": (verdict.start_ms - start) / 1000,
        "end_error_s": (verdict.end_ms - end) / 1000,
        "frames": len(verdict.frames),
        "story_as_credits": story_as_credits,
        "story_skipped_s": skipped / 1000,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="/config/markers.db")
    parser.add_argument("--truth", help="a JSON file of truths instead of the store")
    parser.add_argument("--show", default="The Apothecary Diaries")
    parser.add_argument("--kinds", default="credits,intro")
    parser.add_argument("--limit", type=int, default=10, help="episodes per kind")
    parser.add_argument("--shift", type=float, default=15.0, help="seconds the first guess is off the truth")
    parser.add_argument("--base-url", default=localvision.DEFAULT_URL)
    parser.add_argument("--model", default=localvision.DEFAULT_MODEL)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    cfg = localvision.Config(True, args.base_url.rstrip("/"), args.model)
    if not localvision.model_ready(cfg):
        print(f"{cfg.model} is not loaded at {cfg.base_url}: nothing run", file=sys.stderr)
        return 2
    kinds = set(args.kinds.split(","))
    items = json.load(open(args.truth)) if args.truth else truths(args.db, args.show, kinds)
    chosen = []
    for kind in sorted(kinds):
        mine = [t for t in items if t["kind"] == kind]
        chosen += mine[:: max(1, len(mine) // args.limit)][: args.limit]  # spread over the seasons
    results = [evaluate(cfg, args.ffmpeg, item, round(args.shift * 1000), n) for n, item in enumerate(chosen)]
    if args.json:
        print(json.dumps(results, indent=1))
    for r in results:
        print(
            f"{r['kind']:7} {r['path']:60} truth {r['truth'][0] / 1000:7.1f}-{r['truth'][1] / 1000:7.1f}  "
            + (
                "no span proposed"
                if r["proposed"] is None
                else f"start {r['start_error_s']:+6.1f}s end {r['end_error_s']:+6.1f}s frames {r['frames']:3} "
                f"story-as-credits {r['story_as_credits']} story-skipped {r['story_skipped_s']:.1f}s"
            )
        )
    for kind in sorted(kinds):
        mine = [r for r in results if r["kind"] == kind]
        found = [r for r in mine if r["proposed"]]
        if not mine:
            continue
        starts = [abs(r["start_error_s"]) for r in found]
        ends = [abs(r["end_error_s"]) for r in found]
        print(
            f"{kind}: {len(found)}/{len(mine)} proposed; start error median "
            f"{statistics.median(starts) if starts else float('nan'):.1f}s max {max(starts, default=float('nan')):.1f}s; end error median "
            f"{statistics.median(ends) if ends else float('nan'):.1f}s max {max(ends, default=float('nan')):.1f}s; "
            f"within 2 s: {sum(1 for r in found if abs(r['start_error_s']) <= 2 and abs(r['end_error_s']) <= 2)}; "
            f"dangerous (story called credits, frames) {sum(r['story_as_credits'] for r in found)}; "
            f"episodes that would skip story over 2 s: {sum(1 for r in found if r['story_skipped_s'] > 2)}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
