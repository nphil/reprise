"""The video file's own chapter marks as suggested intro and credits times.

Many releases carry chapters titled "Opening", "Ending", "Preview". They are read with ffprobe and offered like an
online database's times: a suggestion the owner approves, never a skip by themselves (the comparison of episodes may
confirm the same stretch, and then it is an ordinary finding). A show whose episodes mostly have the chapter, with
lengths that cluster, flags the episode whose chapter is a different length so the owner looks at it.
"""

from __future__ import annotations

import json
import re
import statistics
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import replace

from .models import OnlineWindow
from .verification_profile import PROFILE_SPEC

SOURCE_NAME = "Chapter marks"  # shown as "Chapter marks suggestion"
SOURCE_REASON = "the file's own chapter marks"  # the same, in the sentence "Suggested by ..."
AGREE_MS = PROFILE_SPEC["online"]["agree_ms"]
END_SLACK_MS = 1000  # a last chapter may end up to a second off the file's end
AGREEMENT_SHARE = 0.8  # of a show's episodes with the chapter, for its length to count as the show's
AGREEMENT_MIN_FILES = 3
CLUSTER_MS = 2000  # a chapter this close to the show's usual length is not an outlier
PROBE_TIMEOUT = 30

_INTRO = {"opening", "op", "intro", "introduction", "openingcredits", "titlesequence", "maintitles"}
_CREDITS = {"ending", "ed", "credits", "endcredits", "endingcredits", "outro", "closing", "closingcredits"}
_AFTER_CREDITS = re.compile(r"^(preview|nextep|next|comingup|comingnext|comingsoon|trailer|teaser|sneakpeek)")


def _key(title: str) -> str:
    """A chapter title reduced to its letters, lower case, without a trailing number ("Ending 2" is "ending")."""
    return re.sub(r"[^a-z0-9]", "", title.lower()).rstrip("0123456789")


def read(ffprobe: str, path: str) -> list[tuple[str, int, int]]:
    """The file's chapters as ``(title, start_ms, end_ms)``; empty when it has none or cannot be read."""
    try:
        raw = subprocess.run(
            [ffprobe, "-v", "error", "-show_chapters", "-of", "json", path],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=PROBE_TIMEOUT,
            check=True,
        ).stdout
        items = json.loads(raw).get("chapters", [])
        return [
            (
                str((c.get("tags") or {}).get("title", "")),
                round(float(c["start_time"]) * 1000),
                round(float(c["end_time"]) * 1000),
            )
            for c in items
        ]
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return []


def spans(chapters: Sequence[tuple[str, int, int]], duration_ms: int) -> dict[str, tuple[int, int, int]]:
    """``{kind: (start_ms, end_ms, chapter_ms)}`` the chapters propose for a file of this length.

    The opening is its chapter; the first one of that name. The ending starts at the last chapter titled like credits
    and runs to the end of the file when nothing follows it but previews (or nothing at all), otherwise to its own
    end. ``chapter_ms`` is the chapter's own length, which is what a show's episodes are compared by.
    """
    found = {}
    last = len(chapters) - 1
    for i, (title, start, end) in enumerate(chapters):
        name = _key(title)
        if name in _INTRO and "intro" not in found:
            kind, stop = "intro", end
        elif name in _CREDITS:
            kind = "credits"
            tail_is_preview = all(_AFTER_CREDITS.match(_key(t)) for t, _, _ in chapters[i + 1 :])
            stop = duration_ms if i == last or tail_is_preview else end
        else:
            continue
        if kind == "credits" and stop > duration_ms - END_SLACK_MS and stop != duration_ms:
            stop = duration_ms
        if 0 <= start < stop <= duration_ms:
            found[kind] = (start, stop, end - start)
    return found


def windows(
    chapters: Mapping[str, Sequence[tuple[str, int, int]]],
    durations: Mapping[str, int],
    target: str,
    *,
    kinds: Sequence[str] = ("intro", "credits"),
) -> dict[str, tuple[OnlineWindow, ...]]:
    """The chapter windows of each file in ``chapters`` (``{media version id: chapters}``) as online-style
    suggestions, naming this source. The ``target`` episode's window carries a note when the show's episodes mostly
    have the chapter and its length is not the length they cluster on."""
    per_file = {mid: spans(c, durations[mid]) for mid, c in chapters.items()}
    result = {}
    for mid, found in per_file.items():
        out = []
        for kind in kinds:
            if kind not in found:
                continue
            start, end, length = found[kind]
            note = (
                _outlier_note(
                    kind, length, [f[kind][2] for m, f in per_file.items() if m != mid and kind in f], len(per_file) - 1
                )
                if mid == target
                else ""
            )
            out.append(OnlineWindow(kind, start, end, (SOURCE_NAME,), note=note))
        if out:
            result[mid] = tuple(out)
    return result


def _outlier_note(kind: str, length: int, others: list[int], other_files: int) -> str:
    """A sentence when most episodes have this chapter at one usual length and this one's differs, else empty."""
    if other_files + 1 < AGREEMENT_MIN_FILES or (len(others) + 1) / (other_files + 1) < AGREEMENT_SHARE:
        return ""
    usual = statistics.median(others)
    if abs(length - usual) <= CLUSTER_MS or sum(abs(x - usual) <= CLUSTER_MS for x in others) < AGREEMENT_SHARE * len(
        others
    ):
        return ""
    return (
        f"This episode's {kind} chapter is {length / 1000:.1f} s long, but in {len(others)} other episodes it is "
        f"about {usual / 1000:.1f} s, so check it."
    )


def merge(found: dict[str, tuple[OnlineWindow, ...]], chapter_windows: Mapping[str, Sequence[OnlineWindow]]) -> dict:
    """``found`` (what the online databases gave) with the chapter windows added. Where a database gave the same
    kind, the chapter marks join its names when they agree within ``AGREE_MS`` at both ends and are left out when they
    do not: the database's times are the suggestion."""
    merged = {mid: list(ws) for mid, ws in found.items()}
    for mid, extra in chapter_windows.items():
        mine = merged.setdefault(mid, [])
        for window in extra:
            same = next((i for i, w in enumerate(mine) if w.kind == window.kind), None)
            if same is None:
                mine.append(window)
            elif (
                abs(mine[same].start_ms - window.start_ms) <= AGREE_MS
                and abs(mine[same].end_ms - window.end_ms) <= AGREE_MS
            ):
                mine[same] = replace(mine[same], sources=(*mine[same].sources, SOURCE_NAME))
    return {mid: tuple(ws) for mid, ws in merged.items() if ws}
