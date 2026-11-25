"""Which pictures an episode shares with its neighbours: the arithmetic behind the local vision check.

An intro, a closing-credits sequence and a studio logo play the same pictures in every episode of a season; the story
never does. So two episodes of one season show where a title sequence is by their pictures alone, to a fraction of a
second, without anyone saying what the pictures mean. This module compares small grey thumbnails of those pictures. It
reads no file and asks no model: callers hand in the thumbnails.
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from dataclasses import dataclass
from typing import NamedTuple

import numpy as np

THUMB_W = 32
THUMB_H = 18
FPS = 8  # pictures a second in a dense read
SAME = 0.92  # two thumbnails of one picture correlate far above this; thumbnails of different pictures stay below 0.7
FLAT_STD = 6.0  # a thumbnail this plain (a black card, a fade) carries no picture to compare
FLAT_MEAN_GAP = 12.0  # two plain pictures are alike when about as bright as each other
BRIDGE_FRAMES = FPS * 3 // 2  # plain pictures (1.5 s) that may join a run of shared pictures
GAP_FRAMES = 2  # pictures that match nothing, tolerated inside a run
SPLICE_FRAMES = FPS * 3  # a stretch (3 s) that matches nothing (a dissolve, a card of other names) may be crossed
SPLICE_LAG_MS = 500  # but only if the pictures beyond it play the other episode's footage this close to where they did
CUT_SIMILARITY = 0.6  # two consecutive pictures less alike than this are separated by a hard cut
CUT_BRIGHTNESS = 30.0  # a plain picture beside one this much brighter or darker is a hard cut too


@dataclass(frozen=True, eq=False)
class Frames:
    """Pictures of one stretch of a file, ready to compare."""

    t: np.ndarray  # presentation time of each picture (ms)
    unit: np.ndarray  # zero-mean, unit-length thumbnails, one row each
    mean: np.ndarray  # brightness
    flat: np.ndarray  # True for a plain picture

    def __len__(self) -> int:
        return len(self.t)


class Alike(NamedTuple):
    same: np.ndarray  # per picture: a picture of the other set shows the same thing
    blank: np.ndarray  # per plain picture: the other set has a plain picture about as bright
    best: np.ndarray  # per picture: index of the most similar picture of the other set (-1 when it has none)
    lag: np.ndarray  # per picture: how much later (ms) that picture plays in the other set (NaN when it has none)


def frames(times: Sequence[float], gray: np.ndarray) -> Frames:
    """Pictures from their times and grey thumbnails (one row of ``THUMB_W * THUMB_H`` values each)."""
    gray = np.asarray(gray, dtype=np.float32).reshape(-1, THUMB_W * THUMB_H)
    mean = gray.mean(axis=1) if len(gray) else np.zeros(0, np.float32)
    flat = gray.std(axis=1) < FLAT_STD if len(gray) else np.zeros(0, bool)
    centred = gray - mean[:, None]
    norm = np.linalg.norm(centred, axis=1)
    return Frames(
        np.asarray(times, dtype=np.float64)[: len(gray)], centred / np.maximum(norm, 1e-6)[:, None], mean, flat
    )


def frames_from_raw(times: Sequence[float], raw: bytes) -> Frames:
    """Pictures from a run of raw grey thumbnails (``THUMB_W * THUMB_H`` bytes each, in time order)."""
    size = THUMB_W * THUMB_H
    count = min(len(times), len(raw) // size)
    return frames(times[:count], np.frombuffer(raw, dtype=np.uint8, count=count * size).reshape(count, size))


def frames_from_jpegs(items: Sequence[tuple[int, bytes]]) -> Frames:
    """Pictures from ``(time ms, JPEG)`` pairs: each JPEG is reduced to a thumbnail by averaging blocks of pixels."""
    from PIL import Image

    ordered = sorted(items, key=lambda item: item[0])
    gray = np.zeros((len(ordered), THUMB_W * THUMB_H), np.float32)
    for row, (_, jpeg) in enumerate(ordered):
        with Image.open(io.BytesIO(jpeg)) as image:
            image.draft("L", (image.width // 2, image.height // 2))
            thumb = image.convert("L").resize((THUMB_W, THUMB_H), Image.Resampling.BOX)
        gray[row] = np.asarray(thumb, dtype=np.float32).ravel()
    return frames([t for t, _ in ordered], gray)


def alike(a: Frames, b: Frames) -> Alike:
    """For each picture of ``a``, whether ``b`` shows the same picture anywhere (alignment is not needed: a title
    sequence is the same footage whatever time it starts at, and a different scene never correlates this closely)."""
    count = len(a)
    if count == 0 or len(b) == 0:
        return Alike(np.zeros(count, bool), np.zeros(count, bool), np.full(count, -1), np.full(count, np.nan))
    similarity = np.where(b.flat[None, :], -1.0, a.unit @ b.unit.T)
    best = similarity.argmax(axis=1)
    same = ~a.flat & (similarity.max(axis=1) >= SAME)
    if b.flat.any():
        blank = a.flat & (np.abs(a.mean[:, None] - b.mean[None, b.flat]) < FLAT_MEAN_GAP).any(axis=1)
    else:
        blank = np.zeros(count, bool)
    matched = similarity.max(axis=1) > -1.0
    return Alike(same, blank, np.where(matched, best, -1), np.where(matched, b.t[best] - a.t, np.nan))


def _across(found: Alike, shared: int, step: int, drift: float) -> int | None:
    """The first shared picture within ``SPLICE_FRAMES`` beyond ``shared`` (going ``step`` at a time) that plays about
    ``drift`` ms later in the other episode, as ``shared`` does."""
    for k in range(1, SPLICE_FRAMES + 2):
        j = shared + step * k
        if not 0 <= j < len(found.same):
            return None
        if found.same[j] and abs(found.lag[j] - drift) <= SPLICE_LAG_MS:
            return j
    return None


def walk(found: Alike, start: int, step: int, splice: bool = False) -> int:
    """The outermost picture reached from ``start`` (a shared one) going ``step`` pictures at a time (+1 later, -1
    earlier): shared pictures continue the run, so do a couple of unmatched ones between them and plain pictures
    beside the run (a black frame it fades through). With ``splice`` a longer stretch of unmatched pictures is crossed
    too (a dissolve, a flash, a card with another name on it) when the pictures beyond it show the other episode's
    footage at the same place as the ones before it: story never sits in such a gap."""
    count = len(found.same)
    last, misses, plain = start, 0, 0
    shared = start
    i = start + step
    while 0 <= i < count:
        if found.same[i]:
            last = shared = i
            misses = plain = 0
        elif found.blank[i] and plain < BRIDGE_FRAMES:
            plain += 1
            if misses == 0:
                last = i
        else:
            misses += 1
            if misses > GAP_FRAMES:
                across = _across(found, shared, step, float(found.lag[shared])) if splice else None
                if across is None:
                    break
                last = shared = across
                misses = plain = 0
                i = across
        i += step
    return last


def clusters(times: Sequence[float], gap_ms: float) -> list[list[int]]:
    """Indexes of ``times`` (ascending) grouped so that neighbours in a group are at most ``gap_ms`` apart."""
    groups: list[list[int]] = []
    for i, t in enumerate(times):
        if groups and t - times[groups[-1][-1]] <= gap_ms:
            groups[-1].append(i)
        else:
            groups.append([i])
    return groups


def hard_cut(pictures: Frames, i: int) -> bool:
    """Whether a hard cut (not a fade or a dissolve) separates picture ``i - 1`` from picture ``i``."""
    if i <= 0 or i >= len(pictures):
        return False
    before, after = i - 1, i
    if pictures.flat[before] and pictures.flat[after]:
        return False
    if pictures.flat[before] != pictures.flat[after]:
        return bool(abs(pictures.mean[before] - pictures.mean[after]) > CUT_BRIGHTNESS)
    return bool(float(pictures.unit[before] @ pictures.unit[after]) < CUT_SIMILARITY)


def agree(values: Sequence[float], outermost: str, within_ms: float, need: int) -> float:
    """The edge other episodes agree on: the outermost value (``"min"`` for a start, ``"max"`` for an end) that at
    least ``need`` of ``values`` lie within ``within_ms`` of. When no value has that support, the narrowest one: a span
    that is too short only leaves a little unskipped, one that is too long can skip the story."""
    ordered = sorted(values) if outermost == "min" else sorted(values, reverse=True)
    for value in ordered:
        if sum(abs(other - value) <= within_ms for other in values) >= need:
            return value
    return max(values) if outermost == "min" else min(values)
