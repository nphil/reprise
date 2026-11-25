"""An optional, advisory look at an episode by a local vision model (llama-swap, OpenAI chat format).

The edges come from what can be measured, not from the model. The opening titles and the closing credits of a season play
the same pictures in every episode, so where the pictures an episode shares with its nearest neighbours begin and end is
found by comparing small thumbnails (``visionmatch``), to a fraction of a second, and snapped to the hard cut of the
picture there. The model only classifies single frames, and only where a comparison cannot decide: whether what follows
shared credits is a next-episode preview (kept in) or the story (left out), and, for a file with no neighbour to compare
with, which stretches between candidate cuts are titles or credits.

Every verdict carries a confidence (``HIGH``, ``MEDIUM``, ``LOW``) and the plain reasons for it, and stays a suggestion
the owner approves (source "Local model"): never applied, never sent, and it never changes what the comparison found.
A rule that cannot be sure fails safe: no span, or a narrower one, never one that takes in the story.

Every failure of the endpoint or of a file (down, busy, another model loaded, an odd answer, an unreadable file) ends the
check quietly: no advice, nothing else changes. Files are read one ffmpeg at a time at low priority.
"""

from __future__ import annotations

import base64
import json
import logging
import math
import re
import shutil
import subprocess
import threading
import time
import urllib.request
from collections import OrderedDict
from collections.abc import Callable, Hashable, Mapping, Sequence
from dataclasses import dataclass
from typing import NamedTuple, Protocol, TypeVar

import numpy as np

from . import visionmatch as vm

logger = logging.getLogger(__name__)

SOURCE_NAME = "Local model"
SOURCE_REASON = "a local vision model that looked at the episode's pictures"
SETTING = "reprise_local_vision"  # the owner's switch, address and model; off unless turned on
DEFAULT_URL = "http://192.168.1.69:9292/v1"
DEFAULT_MODEL = "qwen3-vl-4b"
SCENES = ("credits", "story", "preview", "black_logo")
HIGH, MEDIUM, LOW = "high", "medium", "low"
FRAME_WIDTH = 320
HEAD_MS = 300000  # where an intro can be: the first five minutes
TAIL_MS = 420000  # where credits can be: the last seven minutes
NEIGHBOURS = 3  # other episodes compared at most: two first, a third only when those two disagree
AGREE_MS = 1000  # references agree on an edge within this, and two of them make it "supported"
HINT_MS = 1500  # another source's time confirms an edge within this
CHAPTER_CONFLICT_MS = 5000  # chapter marks that put an edge this far off make the suggestion doubtful
SEED_GAP_MS = 12000  # shared keyframes this close belong to one stretch
MIN_SPAN_MS = 3000  # a shorter shared stretch is a logo or a coincidence, not an intro or credits
MIN_KEYFRAME_CREDITS_MS = 20000  # credits found from keyframes must run this long; shorter ones come from the end scan
END_SCAN_MS = 14000  # the end of the file read picture by picture to find where a short closing logo begins
REF_END_SCAN_MS = 26000  # and the same stretch of the other episode, which may end a little differently
WINDOW_MS = 6000  # how far one dense read reaches beyond a picture
MAX_EXTEND_MS = 60000  # a shared run is followed this far from where it was found, no more
PREVIEW_MAX_MS = 60000  # what follows the credits is a preview only when it is this short
TRAILING_FRAMES = 8  # frames the model sees of what follows shared credits
LABEL_FRAMES = 32  # frames the model sees of a stretch when there is no other episode to compare with
BRIDGE = 1  # a frame of another kind inside a run of credits is noise
MIN_LABELLED_RUN = 2  # credits frames in a row it takes to call a stretch titles or credits
CUT_SEARCH_MS = 40000  # the dense read that looks for cuts between two sampled frames reaches this far at most
CHECK_BUDGET_S = 1200  # a check that has run this long stops
TIMEOUT_S = 20
RETRIES = 1

PROMPT = (
    "This is one frame of a TV episode. Say what it shows. scene is exactly one of: "
    '"credits" (opening titles or end credits: staff or cast names, a title card or series logo, rolling or stacked '
    'text over artwork, animation or footage); "preview" (a next-episode preview or "next time" card, or story '
    'footage under a next-episode caption); "black_logo" (an almost black screen, or a studio or network logo); '
    '"story" (an ordinary scene of the story with no credit text). overlay_text is true when any text other than '
    "subtitles is laid over the picture. Answer with only the JSON object."
)
SCHEMA = {
    "type": "object",
    "properties": {"scene": {"type": "string", "enum": list(SCENES)}, "overlay_text": {"type": "boolean"}},
    "required": ["scene", "overlay_text"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class Config:
    enabled: bool = False
    base_url: str = DEFAULT_URL
    model: str = DEFAULT_MODEL


@dataclass(frozen=True)
class Verdict:
    kind: str
    start_ms: int
    end_ms: int
    frames: tuple[
        tuple[int, str, bool], ...
    ]  # (time ms, scene, overlay text), every frame the model saw, in time order
    model: str
    confidence: str = LOW  # HIGH, MEDIUM or LOW: how far to trust it; only HIGH may be approved in bulk
    reasons: tuple[str, ...] = ()  # plain sentences saying why
    method: str = "pictures"  # "pictures" (compared with other episodes) or "labels" (the model's reading of frames)


class Media(NamedTuple):
    path: str
    duration_ms: int
    stamp: Hashable = None  # changes when the file does (size and modification time), so cached reads never go stale


class Hint(NamedTuple):
    """Another source's time for an intro or credits: Plex's marker, an online database's, the comparison's own."""

    start_ms: int
    end_ms: int
    name: str


def config() -> Config:
    from ..web.settings_manager import peek_settings_manager

    manager = peek_settings_manager()
    saved = manager.get(SETTING, None) if manager is not None else None
    if not isinstance(saved, dict):
        return Config()
    return Config(
        saved.get("enabled") is True,
        str(saved.get("base_url") or DEFAULT_URL).rstrip("/"),
        str(saved.get("model") or DEFAULT_MODEL),
    )


def set_config(enabled: bool, base_url: str, model: str) -> Config:
    """Save the switch, address and model. The address must be an http(s) URL, the model a plain name."""
    from ..web.settings_manager import get_settings_manager

    url = base_url.strip().rstrip("/")
    if not (url.startswith(("http://", "https://")) and len(url) < 200 and url.count(" ") == 0):
        raise ValueError("Invalid setting")
    name = model.strip()
    if not name or len(name) > 100 or any(ch in name for ch in " \n\t\"'"):
        raise ValueError("Invalid setting")
    get_settings_manager().set(SETTING, {"enabled": bool(enabled), "base_url": url, "model": name})
    return config()


def _open(request, timeout):
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def model_ready(cfg: Config, timeout: float = 5) -> bool:
    """Whether llama-swap already has this model loaded and ready. Asking never loads anything."""
    root = cfg.base_url[:-3] if cfg.base_url.endswith("/v1") else cfg.base_url
    try:
        running = _open(urllib.request.Request(root + "/running"), timeout).get("running", [])
    except (OSError, ValueError, AttributeError):
        return False
    return any(isinstance(m, dict) and m.get("model") == cfg.model and m.get("state") == "ready" for m in running)


def judge(cfg: Config, jpeg: bytes) -> tuple[str, bool] | None:
    """The model's ``(scene, overlay_text)`` for one frame; None when it could not be asked or answered oddly."""
    body = json.dumps(
        {
            "model": cfg.model,
            "temperature": 0,
            "max_tokens": 40,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": PROMPT},
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()},
                        },
                    ],
                }
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "frame", "strict": True, "schema": SCHEMA},
            },
        }
    ).encode()
    for _ in range(1 + RETRIES):
        try:
            request = urllib.request.Request(
                cfg.base_url + "/chat/completions", body, {"Content-Type": "application/json"}
            )
            answer = json.loads(_open(request, TIMEOUT_S)["choices"][0]["message"]["content"])
            if answer["scene"] in SCENES and isinstance(answer["overlay_text"], bool):
                return answer["scene"], answer["overlay_text"]
        except (OSError, ValueError, KeyError, IndexError, TypeError):
            continue
    return None


# --- reading pictures from files ---------------------------------------------------------------------------------------


class Source(Protocol):
    """Where an episode's pictures come from (ffmpeg in production, scripted pictures in tests)."""

    def keyframes(self, path: str, start_ms: int, end_ms: int) -> list[tuple[int, bytes]]:
        """``(time ms, JPEG)`` of each keyframe in the stretch (a keyframe is where an encoder puts a whole picture)."""
        ...

    def gray(self, path: str, start_ms: int, end_ms: int) -> bytes:
        """Every picture of the stretch, ``vm.FPS`` a second, as raw grey ``vm.THUMB_W`` x ``vm.THUMB_H`` thumbnails."""
        ...

    def still(self, path: str, at_ms: int) -> bytes:
        """The picture at this time as a JPEG."""
        ...


class FfmpegSource:
    """Pictures read by ffmpeg, one process at a time, at low priority, decoding only the pictures that are needed."""

    def __init__(self, ffmpeg: str) -> None:
        self.ffmpeg = ffmpeg
        self.nice = shutil.which("nice")

    def _run(self, args: list[str], timeout: float, verbosity: str = "error") -> subprocess.CompletedProcess[bytes]:
        prefix = [self.nice, "-n", "10"] if self.nice else []
        return subprocess.run(
            [*prefix, self.ffmpeg, "-nostdin", "-v", verbosity, "-threads", "1", *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            check=False,
        )

    def keyframes(self, path: str, start_ms: int, end_ms: int) -> list[tuple[int, bytes]]:
        done = self._run(
            [
                "-skip_frame",
                "nokey",
                "-ss",
                f"{start_ms / 1000:.3f}",
                "-t",
                f"{(end_ms - start_ms) / 1000:.3f}",
                "-copyts",
                "-i",
                path,
                "-an",
                "-sn",
                "-dn",
                "-vf",
                f"scale={FRAME_WIDTH}:-2,showinfo,setpts=N/24/TB",
                "-fps_mode",
                "passthrough",
                "-f",
                "image2pipe",
                "-c:v",
                "mjpeg",
                "-q:v",
                "5",
                "-",
            ],
            300,
            "info",
        )
        times = [round(float(m.group(1)) * 1000) for m in re.finditer(rb"pts_time:\s*(-?[0-9.]+)", done.stderr)]
        marks = [m.start() for m in re.finditer(rb"\xff\xd8\xff", done.stdout)]
        pictures = [done.stdout[a:b] for a, b in zip(marks, [*marks[1:], len(done.stdout)], strict=True)]
        return [(t, jpeg) for t, jpeg in zip(times, pictures, strict=False) if start_ms <= t <= end_ms]

    def gray(self, path: str, start_ms: int, end_ms: int) -> bytes:
        done = self._run(
            [
                "-skip_frame",
                "noref",
                "-ss",
                f"{start_ms / 1000:.3f}",
                "-t",
                f"{(end_ms - start_ms) / 1000:.3f}",
                "-i",
                path,
                "-an",
                "-sn",
                "-dn",
                "-vf",
                f"fps={vm.FPS},scale={vm.THUMB_W}:{vm.THUMB_H}:flags=area,format=gray",
                "-f",
                "rawvideo",
                "-",
            ],
            120,
        )
        return done.stdout

    def still(self, path: str, at_ms: int) -> bytes:
        done = self._run(
            [
                "-ss",
                f"{max(0, at_ms) / 1000:.3f}",
                "-i",
                path,
                "-frames:v",
                "1",
                "-vf",
                f"scale={FRAME_WIDTH}:-2",
                "-f",
                "image2pipe",
                "-c:v",
                "mjpeg",
                "-",
            ],
            60,
        )
        if not done.stdout:
            raise OSError("no frame")
        return done.stdout


T = TypeVar("T")


class ReadCache:
    """The thumbnails read from files, kept for the next episode compared with the same neighbours (small, bounded)."""

    def __init__(self, size: int = 160) -> None:
        self._items: OrderedDict[Hashable, object] = OrderedDict()
        self._size = size
        self._lock = threading.Lock()

    def get(self, key: Hashable, make: Callable[[], T]) -> T:
        with self._lock:
            if key in self._items:
                self._items.move_to_end(key)
                return self._items[key]  # type: ignore[return-value]
        value = make()
        with self._lock:
            self._items[key] = value
            self._items.move_to_end(key)
            while len(self._items) > self._size:
                self._items.popitem(last=False)
        return value


_CACHE = ReadCache()


class _Keys(NamedTuple):
    frames: vm.Frames
    jpegs: tuple[bytes, ...]  # parallel to the frames; kept for the episode itself only (the model looks at them)


def _bounds(duration_ms: int, region: str) -> tuple[int, int]:
    return (0, min(duration_ms, HEAD_MS)) if region == "head" else (max(0, duration_ms - TAIL_MS), duration_ms + 1000)


class _Reads:
    def __init__(self, source: Source, cache: ReadCache) -> None:
        self.source, self.cache = source, cache

    def keys(self, media: Media, region: str, jpegs: bool = False) -> _Keys:
        def make() -> _Keys:
            items = sorted(self.source.keyframes(media.path, *_bounds(media.duration_ms, region)), key=lambda x: x[0])
            return _Keys(vm.frames_from_jpegs(items), tuple(j for _, j in items) if jpegs else ())

        if jpegs:
            return make()
        return self.cache.get(("keys", media.path, media.stamp, media.duration_ms, region), make)

    def dense(self, media: Media, start_ms: float, end_ms: float) -> vm.Frames:
        start, end = max(0, int(start_ms)), min(media.duration_ms, int(end_ms))
        if end <= start:
            return vm.frames([], np.zeros((0, vm.THUMB_W * vm.THUMB_H)))

        def make() -> vm.Frames:
            raw = self.source.gray(media.path, start, end)
            size = vm.THUMB_W * vm.THUMB_H
            return vm.frames_from_raw([start + k * 1000 / vm.FPS for k in range(len(raw) // size)], raw)

        return self.cache.get(("dense", media.path, media.stamp, start, end), make)


# --- the check -----------------------------------------------------------------------------------------------------


class _Stop(Exception):
    """The check was cancelled or ran out of time."""


class _NoAnswer(Exception):
    """The model could not be asked or gave an odd answer."""


class _Unreadable(Exception):
    """A neighbouring episode's pictures could not be read."""


class _Pair(NamedTuple):
    """Where the episode shares pictures with one neighbour."""

    start: float
    end: float
    start_hard: bool  # a hard cut (not a fade) or the file's edge bounds the shared run there
    end_hard: bool
    seeds: int


class _Found(NamedTuple):
    start: float
    end: float
    start_support: int  # neighbours that put the start there (within AGREE_MS)
    end_support: int
    start_hard: bool
    end_hard: bool
    compared: int  # neighbours read and compared
    shared: int  # of them, those that show the same pictures


class _Ctx:
    def __init__(
        self,
        cfg: Config,
        source: Source,
        episode: Media,
        references: Sequence[Media],
        cache: ReadCache,
        cancel: Callable[[], bool] | None,
    ) -> None:
        self.cfg, self.reads, self.episode, self.refs = cfg, _Reads(source, cache), episode, list(references)
        self.source, self.cancel = source, cancel
        self.deadline = time.monotonic() + CHECK_BUDGET_S
        self.model_down = False
        self._own: dict[str, _Keys] = {}

    def live(self) -> None:
        if (self.cancel is not None and self.cancel()) or time.monotonic() > self.deadline:
            raise _Stop

    def own(self, kind: str) -> _Keys:
        region = "head" if kind == "intro" else "tail"
        if region not in self._own:
            self.live()
            self._own[region] = self.reads.keys(self.episode, region, jpegs=True)
        return self._own[region]

    def scene(self, jpeg: bytes) -> tuple[str, bool]:
        if self.model_down:
            raise _NoAnswer
        self.live()
        answer = judge(self.cfg, jpeg)
        if answer is None:
            self.model_down = True
            raise _NoAnswer
        return answer


def _extend(ctx: _Ctx, ref: Media, t_e: float, t_r: float, step: int) -> tuple[float, bool]:
    """The edge of the run of shared pictures beyond a shared one (the episode's time ``t_e`` is the neighbour's
    ``t_r``), going later (``step`` 1) or earlier (-1), and whether a hard cut or the file's edge is there."""
    e, edge, hard = ctx.episode, t_e, False
    cur_e, cur_r = t_e, t_r
    while True:
        ctx.live()
        if step < 0:
            a, b = max(0.0, cur_e - WINDOW_MS), cur_e + 600
            ra, rb = max(0.0, cur_r - WINDOW_MS - 2000), cur_r + 2600
        else:
            a, b = max(0.0, cur_e - 600), min(float(e.duration_ms), cur_e + WINDOW_MS)
            ra, rb = max(0.0, cur_r - 2600), min(float(ref.duration_ms), cur_r + WINDOW_MS + 2000)
        mine, theirs = ctx.reads.dense(e, a, b), ctx.reads.dense(ref, ra, rb)
        if not len(mine):
            break
        found = vm.alike(mine, theirs)
        shared = np.nonzero(found.same)[0]
        if not len(shared):
            break
        start = int(shared[int(np.argmin(np.abs(mine.t[shared] - cur_e)))])
        last = vm.walk(found, start, step)
        edge = float(mine.t[last]) + (-500.0 if step < 0 else 500.0) / vm.FPS
        outside = last + step
        if 0 <= outside < len(mine):
            hard = vm.hard_cut(mine, last if step < 0 else outside)
        at_window_end = last == 0 if step < 0 else last == len(mine) - 1
        if not at_window_end or abs(edge - t_e) >= MAX_EXTEND_MS or (a <= 0 if step < 0 else b >= e.duration_ms):
            break
        cur_e = float(mine.t[last])
        if found.same[last]:
            cur_r = float(theirs.t[found.best[last]])
    if step < 0 and edge < 1000 / vm.FPS:
        edge, hard = 0.0, True
    if step > 0 and edge > e.duration_ms - 1000 / vm.FPS:
        edge, hard = float(e.duration_ms), True
    return edge, hard


class _Suffix(NamedTuple):
    start: float
    shared: int
    reached: bool  # the run still went on at the start of what was read
    hard: bool
    t_e: float
    t_r: float | None


def _suffix(ctx: _Ctx, ref: Media) -> _Suffix | None:
    """The run of shared pictures that reaches the end of the episode, read picture by picture: closing logos are too
    short for keyframes to find."""
    e = ctx.episode
    mine = ctx.reads.dense(e, e.duration_ms - END_SCAN_MS, e.duration_ms)
    theirs = ctx.reads.dense(ref, ref.duration_ms - REF_END_SCAN_MS, ref.duration_ms)
    if not len(mine) or not len(theirs):
        return None
    found = vm.alike(mine, theirs)
    count = len(mine)
    late = [k for k in range(count - 1, max(-1, count - 1 - 3 * vm.FPS), -1) if found.same[k]]  # one in the last 3 s
    if not late:
        return None
    first = vm.walk(found, late[0], -1)
    start = float(mine.t[first]) - 500.0 / vm.FPS if first > 0 else float(mine.t[0])
    matched = float(theirs.t[found.best[first]]) if found.same[first] else None
    return _Suffix(
        start,
        int(found.same[first:].sum()),
        first == 0,
        first > 0 and vm.hard_cut(mine, first),
        float(mine.t[first]),
        matched,
    )


def _intro_pair(ctx: _Ctx, ref: Media) -> _Pair | None:
    own, other = ctx.own("intro"), ctx.reads.keys(ref, "head")
    found = vm.alike(own.frames, other.frames)
    seeds = np.nonzero(found.same)[0]
    if not len(seeds):
        return None
    groups = vm.clusters([own.frames.t[i] for i in seeds], SEED_GAP_MS)
    best = max(groups, key=lambda g: (own.frames.t[seeds[g[-1]]] - own.frames.t[seeds[g[0]]], len(g)))
    first, last = int(seeds[best[0]]), int(seeds[best[-1]])
    start, start_hard = _extend(ctx, ref, own.frames.t[first], other.frames.t[found.best[first]], -1)
    end, end_hard = _extend(ctx, ref, own.frames.t[last], other.frames.t[found.best[last]], 1)
    return _Pair(start, end, start_hard, end_hard, len(best)) if end - start >= MIN_SPAN_MS else None


def _credits_pair(ctx: _Ctx, ref: Media) -> _Pair | None:
    e = ctx.episode
    own, other = ctx.own("credits"), ctx.reads.keys(ref, "tail")
    found = vm.alike(own.frames, other.frames)
    seeds = np.nonzero(found.same)[0]
    if len(seeds):
        group = vm.clusters([own.frames.t[i] for i in seeds], SEED_GAP_MS)[-1]
        first, last = int(seeds[group[0]]), int(seeds[group[-1]])
        start, start_hard = _extend(ctx, ref, own.frames.t[first], other.frames.t[found.best[first]], -1)
        end, end_hard = _extend(ctx, ref, own.frames.t[last], other.frames.t[found.best[last]], 1)
        if end - start >= MIN_KEYFRAME_CREDITS_MS:
            return _Pair(start, end, start_hard, end_hard, len(group))
    tail = _suffix(ctx, ref)
    if tail is None or tail.shared < 3 or e.duration_ms - tail.start < MIN_SPAN_MS:
        return None
    start, hard = tail.start, tail.hard
    if tail.reached and tail.t_r is not None:
        start, hard = _extend(ctx, ref, tail.t_e, tail.t_r, -1)
    return _Pair(start, float(e.duration_ms), hard, True, tail.shared)


def _agreed(pairs: list[_Pair | None]) -> bool:
    shared = [p for p in pairs if p]
    return len(shared) >= 2 and all(
        abs(a.start - b.start) <= AGREE_MS and abs(a.end - b.end) <= AGREE_MS for a in shared for b in shared
    )


def _compare(ctx: _Ctx, kind: str) -> _Found | None | str:
    """What the episode shares with its neighbours: a ``_Found``; None when they were compared and share nothing; the
    string ``"unread"`` when no neighbour could be compared at all."""
    pair = _intro_pair if kind == "intro" else _credits_pair
    pairs: list[_Pair | None] = []
    for index, ref in enumerate(ctx.refs[:NEIGHBOURS]):
        if index == 2 and (all(p is None for p in pairs) or _agreed(pairs)):
            break  # a third neighbour only settles a disagreement
        try:
            pairs.append(pair(ctx, ref))
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            logger.debug("A neighbouring episode could not be read: %s", type(exc).__name__)
    if not pairs:
        return "unread"
    shared = [p for p in pairs if p]
    if not shared:
        return None
    # Pictures that two episodes show alike are never this episode's own story, but one neighbour's longer run may be
    # recap or preview footage that this episode plays as story. So an edge needs a second neighbour within AGREE_MS;
    # without one the narrower stretch is taken (a span too short leaves a little unskipped, one too long skips story).
    need = min(2 if len(pairs) >= 2 else 1, len(shared))
    start, end = (
        vm.agree([p.start for p in shared], "min", AGREE_MS, need),
        vm.agree([p.end for p in shared], "max", AGREE_MS, need),
    )
    at_start = [p for p in shared if abs(p.start - start) <= AGREE_MS]
    at_end = [p for p in shared if abs(p.end - end) <= AGREE_MS]
    return _Found(
        start,
        end,
        len(at_start),
        len(at_end),
        all(p.start_hard for p in at_start),
        all(p.end_hard for p in at_end),
        len(pairs),
        len(shared),
    )


def _trailing(ctx: _Ctx, end: float) -> tuple[float, list[tuple[int, str, bool]], str]:
    """Whether what follows credits found by pictures is part of them: a next-episode preview runs on to the file's end
    and is kept in; story (a tag) is not. The model looks at the keyframes after the shared run. Returns the credits'
    end, the frames asked about and ``"kept"``, ``"left"`` (story, a long stretch or nothing to look at) or
    ``"unsure"`` (the model could not be asked): only a stretch the model calls a preview is taken in."""
    duration = ctx.episode.duration_ms
    if duration - end <= 1500:
        return float(duration), [], "none"
    own = ctx.own("credits")
    after = [i for i, t in enumerate(own.frames.t) if end + 500 < t < duration]
    if duration - end > PREVIEW_MAX_MS or not after:
        return end, [], "left"
    chosen = [
        after[round(k * (len(after) - 1) / max(1, TRAILING_FRAMES - 1))]
        for k in range(min(TRAILING_FRAMES, len(after)))
    ]
    asked: list[tuple[int, str, bool]] = []
    try:
        for i in dict.fromkeys(chosen):
            scene, overlay = ("black_logo", False) if own.frames.flat[i] else ctx.scene(own.jpegs[i])
            asked.append((int(own.frames.t[i]), scene, overlay))
    except _NoAnswer:
        return end, asked, "unsure"
    # Story footage with text laid over it is, after the credits, a preview's caption: read it so.
    scenes = ["preview" if s == "story" and o else s for _, s, o in asked]
    first_preview = scenes.index("preview") if "preview" in scenes else len(scenes)
    if "story" in scenes[:first_preview]:
        return end, asked, "left"
    return float(duration), asked, "kept"


def _clock(ms: float) -> str:
    seconds = round(ms / 1000)
    return (
        f"{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"
        if seconds >= 3600
        else f"{seconds // 60}:{seconds % 60:02d}"
    )


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _agrees(hints: Sequence[Hint], start: float, end: float) -> list[str]:
    return [h.name for h in hints if abs(h.start_ms - start) <= HINT_MS and abs(h.end_ms - end) <= HINT_MS]


def _chapter_note(chapter: tuple[int, int] | None, start: float, end: float) -> tuple[str, bool]:
    """A sentence about the file's chapter marks and whether they contradict the span."""
    if chapter is None:
        return "", False
    if abs(chapter[0] - start) <= HINT_MS and abs(chapter[1] - end) <= HINT_MS:
        return "The file's chapter marks put it in the same place.", False
    if abs(chapter[0] - start) > CHAPTER_CONFLICT_MS or abs(chapter[1] - end) > CHAPTER_CONFLICT_MS:
        return f"The file's chapter marks put it elsewhere ({_clock(chapter[0])} to {_clock(chapter[1])}).", True
    return "", False


def _pictures_verdict(
    ctx: _Ctx, kind: str, found: _Found, hints: Sequence[Hint], chapter: tuple[int, int] | None
) -> Verdict:
    end, frames, outcome = _trailing(ctx, found.end) if kind == "credits" else (found.end, [], "none")
    duration = ctx.episode.duration_ms
    end_hard = found.end_hard or outcome == "kept"
    others = _plural(found.compared, "other episode", "other episodes")
    reasons = [f"The same pictures play at this spot in {found.shared} of the {others} it was compared with."]
    supported = found.start_support >= 2 and found.end_support >= 2
    if supported:
        reasons.append("At least two of them agree on both ends to within a second.")
    elif found.compared >= 2:
        reasons.append(
            "The other episodes do not agree on both ends to within a second, so the narrower times were taken."
        )
    else:
        reasons.append("Only one other episode could be compared, so nothing confirms it.")
    sharp = found.start_hard and end_hard
    reasons.append(
        "Both ends fall on a hard cut."
        if sharp
        else "An end falls on a fade rather than a hard cut, so it may be off by a moment."
    )
    clean = True
    if outcome == "kept":
        reasons.append(
            f"The model read the last {round((duration - found.end) / 1000)} seconds as a next-episode preview, so the suggestion runs to the end of the file."
        )
    elif outcome == "left":
        reasons.append(
            "What follows the credits is left out: the model read it as story, or it is too long for a preview."
        )
    elif outcome == "unsure":
        reasons.append("The model could not be asked about what follows the credits, so that stretch is left out.")
        clean = False
    note, conflict = _chapter_note(chapter, found.start, end)
    if note:
        reasons.append(note)
    agree = _agrees(hints, found.start, end)
    if agree:
        reasons.append(f"{' and '.join(dict.fromkeys(agree))} put it in the same place.")
    high = found.compared >= 2 and supported and sharp and clean and not conflict
    level = HIGH if high else MEDIUM
    return Verdict(
        kind,
        round(found.start),
        round(end),
        tuple(frames),
        ctx.cfg.model,
        level,
        (f"Confidence: {level}.", *reasons),
        "pictures",
    )


# --- no other episode to compare with: the model labels the stretches between candidate cuts --------------------------


def _cuts(ctx: _Ctx, a: float, b: float, marks: Sequence[float], later: bool) -> list[float]:
    """Candidate cut times in ``a..b`` of the episode, earliest first: hard cuts of the picture (and black-frame edges),
    and other sources' marks (chapters, Plex, online times). A long stretch is searched from the side the edge is on
    (``later``: the end of an intro, whose search starts at ``a``; otherwise the start of credits, whose ends at ``b``)."""
    cuts = {float(m) for m in marks if a < m < b}
    lo, hi = (a, min(b, a + CUT_SEARCH_MS)) if later else (max(a, b - CUT_SEARCH_MS), b)
    window = ctx.reads.dense(ctx.episode, lo, hi)
    cuts.update(float(window.t[i]) for i in range(1, len(window)) if a < window.t[i] < b and vm.hard_cut(window, i))
    return sorted(cuts)


def _bisect(
    ctx: _Ctx, inside: float, outside: float, marks: Sequence[float], asked: list[tuple[int, str, bool]]
) -> float:
    """The cut between a frame the model called credits (``inside``) and one it did not (``outside``): the candidate
    cuts between them are halved, the model looking at a frame just inside each; with no cut to choose from, the
    inside time (the safe side) is kept."""
    low, high = sorted((inside, outside))
    later = outside > inside
    cuts = _cuts(ctx, low, high, marks, later)
    while cuts:
        middle = cuts[len(cuts) // 2]
        probe = middle - 300 if later else middle + 300
        scene, overlay = ctx.scene(ctx.source.still(ctx.episode.path, round(probe)))
        asked.append((round(probe), scene, overlay))
        if scene == "credits":
            cuts, inside = (cuts[len(cuts) // 2 + 1 :], middle) if later else (cuts[: len(cuts) // 2], middle)
        else:
            cuts = cuts[: len(cuts) // 2] if later else cuts[len(cuts) // 2 + 1 :]
    return inside


def _labelled(ctx: _Ctx, kind: str, hints: Sequence[Hint], chapter: tuple[int, int] | None) -> Verdict | None:
    """The span the model's reading of frames gives, for a file with no other episode to compare with: a frame every
    so often is labelled, the first (intro) or last (credits) run of credits frames is the stretch, and each end is
    placed on a candidate cut by halving. Never more than a medium confidence."""
    own = ctx.own(kind)
    frames, count = own.frames, len(own.frames)
    if count < 4:
        return None
    marks = [m for h in hints for m in (h.start_ms, h.end_ms)] + ([chapter[0], chapter[1]] if chapter else [])
    picks = list(range(0, count, max(1, math.ceil(count / LABEL_FRAMES))))
    asked: list[tuple[int, str, bool]] = []
    scenes: list[str] = []
    for i in picks:
        scene, overlay = ("black_logo", False) if frames.flat[i] else ctx.scene(own.jpegs[i])
        asked.append((int(frames.t[i]), scene, overlay))
        scenes.append("preview" if kind == "credits" and scene == "story" and overlay else scene)
    runs: list[tuple[int, int]] = []
    for i, scene in enumerate(scenes):
        if scene != "credits":
            continue
        if runs and i - runs[-1][1] - 1 <= BRIDGE:
            runs[-1] = (runs[-1][0], i)
        else:
            runs.append((i, i))
    runs = [r for r in runs if r[1] - r[0] + 1 >= MIN_LABELLED_RUN]
    if not runs:
        return None
    first, last = runs[0] if kind == "intro" else runs[-1]
    duration = ctx.episode.duration_ms
    at = [float(frames.t[i]) for i in picks]
    start = at[first] if first == 0 else _bisect(ctx, at[first], at[first - 1], marks, asked)
    after = scenes[last + 1 :]
    if kind == "credits" and ("story" not in after or duration - at[last] <= PREVIEW_MAX_MS):
        end = float(duration)
    elif last == len(picks) - 1:
        end = at[last]
    else:
        end = _bisect(ctx, at[last], at[last + 1], marks, asked)
    if end - start < MIN_SPAN_MS:
        return None
    reasons = [
        f"The model read {len(picks)} frames and took the {'first' if kind == 'intro' else 'last'} stretch of titles or credits; there was no other episode to compare with, so nothing confirms it.",
    ]
    note, conflict = _chapter_note(chapter, start, end)
    agree = _agrees(hints, start, end)
    if note:
        reasons.append(note)
    if agree:
        reasons.append(f"{' and '.join(dict.fromkeys(agree))} put it in the same place.")
    corroborated = (note.endswith("same place.") or bool(agree)) and not conflict
    level = MEDIUM if corroborated else LOW
    return Verdict(
        kind,
        round(start),
        round(end),
        tuple(sorted(asked)),
        ctx.cfg.model,
        level,
        (f"Confidence: {level}.", *reasons),
        "labels",
    )


def check(
    cfg: Config,
    source: Source,
    episode: Media,
    anchors: Mapping[str, Sequence[tuple[int, int]]],
    *,
    references: Sequence[Media] = (),
    chapters: Mapping[str, tuple[int, int]] | None = None,
    hints: Mapping[str, Sequence[Hint]] | None = None,
    cancel: Callable[[], bool] | None = None,
    cache: ReadCache | None = None,
) -> dict[str, Verdict]:
    """A verdict for each kind in ``anchors`` (``{kind: [(start_ms, end_ms), ...]}``, the kinds to look at) the pictures
    or the model's reading support; ``{}`` when the model is not loaded (the check then simply did not happen).

    ``references`` are the nearest other episodes of the season, nearest first; ``chapters`` is ``{kind: (start, end)}``
    from the file's own chapter marks and ``hints`` other sources' times, which only corroborate or doubt a verdict."""
    if not model_ready(cfg):
        return {}
    ctx = _Ctx(cfg, source, episode, references, cache or _CACHE, cancel)
    verdicts: dict[str, Verdict] = {}
    for kind in anchors:
        kind_hints = (hints or {}).get(kind, ())
        chapter = (chapters or {}).get(kind)
        try:
            found = _compare(ctx, kind) if ctx.refs else "unread"
            if found == "unread":
                verdict = _labelled(ctx, kind, kind_hints, chapter)
            elif isinstance(found, _Found):
                verdict = _pictures_verdict(ctx, kind, found, kind_hints, chapter)
            else:
                verdict = None
        except (_Stop, _NoAnswer):
            break
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            logger.debug("Local vision check of the %s ended: %s", kind, type(exc).__name__)
            continue
        if verdict is not None:
            verdicts[kind] = verdict
    return verdicts
