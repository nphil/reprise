"""Review-only recurrence of an episode's picture and sound in other episodes, whatever their file formats.

Two files are compared in the canonical picture and sound (see ``canonical``), so the codec, bit depth, chroma
layout, resolution, frame rate and audio layout of two files of the same footage do not matter. Unknown metadata is
not guessed. A qualified match is not certification of rendered appearance or of arbitrarily small semantic changes.

The picture and the sound of two files are never assumed to share one offset: the picture votes propose a clock, each
sound track is searched for its own offset within a bounded window around it, and each is then verified strictly at
its own.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
from collections import Counter, defaultdict, deque
from collections.abc import Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import replace
from fractions import Fraction
from itertools import zip_longest
from typing import NamedTuple

import numpy as np
from loguru import logger

from ..gpu.ffmpeg_capabilities import ffmpeg_binary
from . import acceleration, canonical, container_limits, gpu_pairs, scan_cache
from . import media as decoder
from . import recurrence as verify
from .admission import EXTRA_REFERENCES, MAX_REFERENCES, admit_references
from .models import (
    ORIGIN_ONLINE,
    ORIGIN_PLEX,
    REASON_ONLINE_SUGGESTION,
    REASON_PLEX_SUGGESTION,
    AnalysisDraft,
    CandidateWindow,
    DecisionRange,
    Evidence,
    MediaVersion,
    OnlineWindow,
)
from .uniqueness import COMPARISON_KEY, is_one_of_a_kind, sentence
from .verification_profile import PROFILE_FINGERPRINT, PROFILE_ID, PROFILE_SPEC

# A verified run: target start and end, the reference, its picture offset, the unspecified colour fields, the native
# anchors, the first track's sound offset (both offsets are reference time minus speed times target time, in ms), the
# channels compared per track, the speed (reference time per target time) and the level each track's reference sound
# was brought to the target's by.
_VerifiedRun = tuple[int, int, MediaVersion, Fraction, list[str], dict, Fraction, dict, Fraction, dict]
_LedgerKey = tuple[str, str, str | None, str | None, str | None]
# A hole filled inside a Plex marker: its start and end, the marker, and the confirmed runs on either side of it.
_Bridge = tuple[int, int, CandidateWindow, _VerifiedRun, _VerifiedRun]

MAX_CANDIDATE_MS = PROFILE_SPEC["candidate"]["max_ms"]
REFERENCE_RADIUS_MS = PROFILE_SPEC["candidate"]["reference_radius_ms"]
CONTEXT_MS = PROFILE_SPEC["candidate"]["context_ms"]
SOUND_SEARCH_MS = PROFILE_SPEC["sound_offset"]["search_ms"]
SOUND_PROBE_MS = PROFILE_SPEC["sound_offset"]["probe_ms"]
SOUND_BLOCK_MS = PROFILE_SPEC["sound_offset"]["block_ms"]
SOUND_CORRELATION = PROFILE_SPEC["sound_offset"]["correlation"]
SOUND_GAIN_LIMIT = PROFILE_SPEC["sound_offset"]["gain_limit"]
SOUND_RATE = canonical.SOUND_RATE
HINT_TICK_MS = Fraction(PROFILE_SPEC["timing"]["hint_tick_ms"])
WARNING = (
    "Recurrence of the canonical picture and sound within the profile's limits; "
    "not certified rendered equivalence. Unspecified interpretation and sufficiently small semantic differences are not established."
)


def _workers():
    """How many references are searched at once (REPRISE_ANALYSIS_WORKERS, 1 to 8): the searches are independent and
    their results are merged in order, so this changes how long an analysis takes and how much of the host it takes."""
    try:
        return max(1, min(8, int(os.environ.get("REPRISE_ANALYSIS_WORKERS", "3"))))
    except ValueError:
        return 3


SEARCH_WORKERS = _workers()
# What one search holds, in processes and threads, at its busiest: the picture check reads two windows at once, each an
# ffmpeg, an ffprobe and the threads that watch them (measured on the NAS: 61 for two searches with an NVDEC decode
# each). APP_PIDS stays free for the rest of the app. A container with a process limit (Docker --pids-limit, 128 there)
# fails a decode whose thread cannot start, so no more searches run at once than the limit has room for.
SEARCH_PIDS = 32
APP_PIDS = 16


def _search_workers():
    """How many references are searched at once right now: SEARCH_WORKERS, or fewer when the container's process limit
    has no room for that many (never fewer than one)."""
    room = container_limits.process_headroom()
    if room is None:
        return SEARCH_WORKERS
    return max(1, min(SEARCH_WORKERS, (room - APP_PIDS) // SEARCH_PIDS))


# The progress bar: the share of a window's bar the target's scan takes (measured: about a tenth of the window's time),
# the share of a search that is its reference scan (the rest is the sound and picture checks of its proposals), and how
# often a scan reports (frames).
TARGET_SCAN_SHARE = 0.1
SCAN_SHARE = 0.8
PROGRESS_EVERY_FRAMES = 24

# The notes below are stored with the result and shown to the owner when nothing could be proposed.
# Plain sentences, one idea each; technical detail never goes in the first line.
FALLBACK_WINDOW_MS = 90000  # how much of the file's start / end is checked when Plex has no marker for it
OPENING_WINDOW = "opening-window"  # candidate source for the start of the file when Plex has no intro marker
ENDING_WINDOW = "ending-window"  # candidate source for the end of the file when Plex has no credits marker
NO_REFERENCES = "There are no other episodes of this season to compare with, so nothing could be confirmed."
NO_USABLE_REFERENCES = "None of the other episodes of this season could be used for the comparison."
NO_OTHER_EPISODES = "This is the only episode of its season in the library, so there is nothing to compare it with."
OTHER_EPISODES_NOT_READY = "Other episodes of this season are in the library but none are ready to compare yet. Refresh the library and try again."
MOVIE_NOTE = (
    "A film has no other episodes to compare with, so its credits can only be suggested (by Plex and the online "
    "databases). Nothing is confirmed, so a suggestion waits for your approval."
)
FILM_NOT_CONFIRMED = "There is nothing in a film to confirm it against."
COPY_PICTURE_DIFFERS = (
    "This copy of the episode has a different picture format from every other episode of its season, "
    "so it can't be compared with them and nothing could be confirmed."
)
COPY_AUDIO_DIFFERS = (
    "This copy of the episode has audio tracks that no other episode of its season has, "
    "so it can't be compared with them and nothing could be confirmed."
)
# Why a nearer episode was left out of the comparison (the next nearest takes its place), for one and for several.
_LEFT_OUT = {
    "picture": ("has a different picture format", "have a different picture format"),
    "audio": ("has different audio tracks", "have different audio tracks"),
    "unreadable": ("could not be read", "could not be read"),
}
COMPARISON_UNAVAILABLE = "A comparison with another episode could not be completed."
ONLINE_NOT_COMPARED = (
    "The online times for the {kind} could not be compared with another episode, so they stay a suggestion."
)
ONLINE_HINT_FAILED = "Searching where the online times point in another episode for the {kind} could not be completed."
UNREADABLE_MEDIA = "Analysis stopped because this file could not be read."
UNCOMPARABLE_MEDIA = "Analysis stopped because this file can't be compared exactly."
COMPARISON_FAILED = "This episode can't be compared exactly with the others, so nothing could be confirmed."
COMPARISON_UNREADABLE = "This episode could not be read for comparison, so nothing could be confirmed."
_FALLBACK_NOTES = {
    OPENING_WINDOW: f"Plex has no intro marker for this episode, so only its first {FALLBACK_WINDOW_MS // 1000} seconds were checked.",
    ENDING_WINDOW: f"Plex has no credits marker for this episode, so only its last {FALLBACK_WINDOW_MS // 1000} seconds were checked.",
}


def _no_repeat_found(count):
    others = "1 other episode" if count == 1 else f"{count} other episodes"
    return f"Compared with {others} of this season, but no repeating intro or credits could be confirmed."


def _one_of_a_kind_note(kind, facts):
    """The note for an intro or credits that other episodes do not share (see ``uniqueness``)."""
    return f"{sentence(kind, facts)}."


def _left_out_notes(left_out):
    """One note per cause for the episodes the comparison passed over, as ``{cause: how many}``."""
    notes = []
    for cause, count in left_out.items():
        if count:
            one, many = _LEFT_OUT[cause]
            notes.append(
                f"1 other episode of this season {one}, so it was left out of the comparison."
                if count == 1
                else f"{count} other episodes of this season {many}, so they were left out of the comparison."
            )
    return notes


PLEX_SOURCE = "plex"  # CandidateWindow.source of a marker Plex itself detected for this exact file
PLEX_EVIDENCE_TEXT = "Plex has a marker here, but comparing episodes did not confirm it."
ONLINE_SOURCE = "online"  # CandidateWindow.source of the times online databases give for this exact episode
SUGGESTED_SOURCES = (PLEX_SOURCE, ONLINE_SOURCE)  # windows that are suggestions when comparing cannot confirm them
PLEX_END_SLACK_MS = 1000  # Plex rounds: a marker may end up to a second after its file does, and then ends with it
ONLINE_AGREE_MS = PROFILE_SPEC["online"]["agree_ms"]  # an online time and Plex's marker agree within this at both ends
NOT_CONFIRMED = "Reprise could not confirm it by comparing episodes."
TOO_LONG_TO_COMPARE = f"It is longer than the {MAX_CANDIDATE_MS // 60000} minutes Reprise can compare."
PLEX_CONFIRMED_SHARE = Fraction(PROFILE_SPEC["candidate"]["plex_confirmed_share"])
BRIDGE_MAX_GAP_MS = PROFILE_SPEC["bridge"]["max_gap_ms"]
BRIDGED = "bridged"  # the reason code of a hole filled between two confirmed pieces of a suggested window


def _confirmed_only(covered_ms):
    seconds = max(1, round(covered_ms / 1000))
    span = "1 second" if seconds == 1 else f"{seconds} seconds"
    return f"Comparing episodes confirmed only {span} of it, less than half, so that part is not proposed on its own."


def _seconds(ms):
    return f"{ms / 1000:.1f} s"


def _bridged_text(whose, length_ms):
    return f"A short gap ({_seconds(length_ms)}) between two parts of {whose} that Reprise confirmed, filled in."


def _bridges_note(whose, lengths):
    """The reason listing the holes filled inside one suggested window, in plain words."""
    if len(lengths) == 1:
        return f"Filled 1 short gap ({_seconds(lengths[0])}) inside {whose}."
    return f"Filled {len(lengths)} short gaps (longest {_seconds(max(lengths))}) inside {whose}."


def _bridges(window, runs, owner_keeps):
    """The holes inside a Plex marker that may be filled: between two neighbouring confirmed runs, at most
    ``BRIDGE_MAX_GAP_MS`` long, entirely inside the marker and clear of every range the owner keeps. The marker's own
    ends are never filled, only what lies between two confirmed runs; every other hole stays a protected keep."""
    holes = []
    for before, after in zip(runs, runs[1:], strict=False):
        lo, hi = before[1], after[0]
        if (
            0 < hi - lo <= BRIDGE_MAX_GAP_MS
            and window.start_ms <= lo
            and hi <= window.end_ms
            and not any(start < hi and end > lo for start, end in owner_keeps)
        ):
            holes.append((lo, hi, window, before, after))
    return holes


def _clock(ms):
    hours, rest = divmod(ms // 1000, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


def _names_text(names):
    """Names in plain words: "Plex", "Plex and IntroDB", "Plex, IntroDB and SkipDB"."""
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def _whose(window, names):
    """How a note names a suggested window: "Plex's intro" for a marker of Plex's, "the intro IntroDB suggested" for
    online times."""
    return (
        f"Plex's {window.kind}" if window.source == PLEX_SOURCE else f"the {window.kind} {_names_text(names)} suggested"
    )


def _suggestion_note(window, names, why):
    span = f"from {_clock(window.start_ms)} to {_clock(window.end_ms)}"
    return (
        f"{_names_text(names)} suggested the {window.kind} ({span}). {why} It stays a suggestion until you approve it."
    )


def _signature_tracks(media):
    """The audio tracks the library registered the file with, as far as a pass that decodes nothing needs them."""
    return [(str(track["plex_id"]),) for track in json.loads(media.stream_signature)["audio_streams"]]


def _fit_windows(windows, duration):
    """Plex's markers as windows of this file, and a note for each left out: one ending up to a second after the
    file ends with it, and one that does not fit the file at all is ignored."""
    fitted, ignored = set(), []
    for window in windows:
        if window.source != PLEX_SOURCE:
            fitted.add(window)
            continue
        end = min(window.end_ms, duration) if window.end_ms - duration <= PLEX_END_SLACK_MS else window.end_ms
        if 0 <= window.start_ms < end <= duration:
            fitted.add(replace(window, end_ms=end))
        else:
            ignored.append(f"Plex's {window.kind} marker does not fit this file, so it was ignored.")
    return sorted(fitted, key=lambda w: (w.start_ms, w.end_ms, w.kind, w.source)), ignored


def _suggestion(media, window, names, tracks, movie=False):
    """A suggested window as a proposal: evidence that nothing confirmed it, and a skip only the owner can make
    eligible. Plex's marker (origin plex, also when an online database agrees with it) or online times (origin online);
    ``names`` are who suggested it."""
    plex = window.source == PLEX_SOURCE
    who = _names_text(names)
    eid_parts = (media.id, PROFILE_ID, PROFILE_FINGERPRINT, window.kind, window.start_ms, window.end_ms)
    eid = _id(*eid_parts, "plex") if names == ("Plex",) else _id(*eid_parts, window.source, names)
    if names == ("Plex",):
        claim = "Plex's own marker; not confirmed by comparing episodes"
        text = (
            "Plex has a marker here, but a film has no other episode to confirm it against."
            if movie
            else PLEX_EVIDENCE_TEXT
        )
    else:
        claim = f"{who}'s times; not confirmed by comparing episodes"
        text = (
            f"{who} put the {window.kind} here, but a film has no other episode to confirm it against."
            if movie
            else f"{who} put the {window.kind} here, but comparing episodes did not confirm it."
        )
    metrics = {
        "verification_profile": PROFILE_ID,
        "profile_fingerprint": PROFILE_FINGERPRINT,
        "candidate_source": window.source,
        "suggested_by": list(names),
        "checked_audio_stream_ids": [],
        "checked_channels": {},
        "reference_offset_ms": None,
        "reference_offset_rational_ms": None,
        "sound_offset_ms": None,
        "sound_offset_rational_ms": None,
        "reference_speed": None,
        "reference_speed_rational": None,
        "sound_levels": {},
        "unspecified_interpretation_fields": [],
        "claim": claim,
        "admission_provenance": None,
        "native_anchors": None,
        "bridged": False,
        "bridge_start_ms": None,
        "bridge_end_ms": None,
        "bridge_length_ms": None,
    }
    evidence = Evidence(
        eid,
        window.start_ms,
        window.end_ms,
        "audiovisual",
        "unknown",
        "plex_marker_unconfirmed" if plex else "online_times_unconfirmed",
        text,
        None,
        None,
        None,
        metrics,
    )
    suggestion = DecisionRange(
        _id("range", eid),
        window.kind,
        "skip",
        window.start_ms,
        window.end_ms,
        "proposed",
        False,
        ORIGIN_PLEX if plex else ORIGIN_ONLINE,
        REASON_PLEX_SUGGESTION if plex else REASON_ONLINE_SUGGESTION,
        f"Suggested by {who} — not confirmed by Reprise.",
        (eid,),
        tuple(t[0] for t in tracks),
        False,
        names,
    )
    return evidence, suggestion


def _carve(windows, around):
    """The windows without ``around``'s stretch where they are the opening or ending window that stands in for a
    missing marker: the online times are where to look there, and a keep over them would veto a suggestion."""
    kept = []
    for w in windows:
        if (
            w.source in SUGGESTED_SOURCES
            or w.kind != around.kind
            or w.end_ms <= around.start_ms
            or w.start_ms >= around.end_ms
        ):
            kept.append(w)
            continue
        # A remainder shorter than a run can be is not worth a window (and a keep over it would only be noise).
        if around.start_ms - w.start_ms >= verify.MIN_RUN_MS:
            kept.append(replace(w, end_ms=around.start_ms))
        if w.end_ms - around.end_ms >= verify.MIN_RUN_MS:
            kept.append(replace(w, start_ms=around.end_ms))
    return kept


def _with_online(windows, found):
    """The windows with this episode's online times added.

    A time that agrees with a Plex marker of the same kind (within ``ONLINE_AGREE_MS`` at both ends) is the same
    suggestion: the marker's window, naming both. One that overlaps a Plex marker without agreeing is not added (the
    marker is what Plex serves; it is noted). Any other becomes a window of its own, and the opening or ending window
    that stands in for a missing marker gives way to it.

    Returns ``(windows, suggested_by, notes)``: every suggested window's names, in display order.
    """
    kept, suggested_by, notes = list(windows), {}, []
    for online in found:
        names = online.sources
        if online.dissent:
            notes.append(
                f"{_names_text(online.dissent)} gave other times for the {online.kind} than {_names_text(names)}; "
                f"{_names_text(names)}'s were used."
            )
        marks = [w for w in windows if w.source == PLEX_SOURCE and w.kind == online.kind]
        same = next(
            (
                w
                for w in marks
                if abs(w.start_ms - online.start_ms) <= ONLINE_AGREE_MS
                and abs(w.end_ms - online.end_ms) <= ONLINE_AGREE_MS
            ),
            None,
        )
        if same is not None:
            suggested_by[same] = ("Plex", *names)
        elif any(w.start_ms < online.end_ms and online.start_ms < w.end_ms for w in marks):
            notes.append(
                f"{_names_text(names)} put the {online.kind} from {_clock(online.start_ms)} to {_clock(online.end_ms)}, "
                "which differs from Plex's marker; only Plex's was used."
            )
        else:
            window = CandidateWindow(online.kind, online.start_ms, online.end_ms, ONLINE_SOURCE)
            suggested_by[window] = names
            kept = [*_carve(kept, window), window]
    for w in kept:
        if w.source == PLEX_SOURCE:
            suggested_by.setdefault(w, ("Plex",))
    return sorted(kept, key=lambda w: (w.start_ms, w.end_ms, w.kind, w.source)), suggested_by, notes


def _confirmed_ms(runs, lo, hi):
    """How much of ``lo``..``hi`` the verified runs cover, each stretch counted once."""
    covered, edge = 0, lo
    for a, b in sorted((max(lo, run[0]), min(hi, run[1])) for run in runs if run[0] < hi and run[1] > lo):
        covered += max(0, b - max(a, edge))
        edge = max(edge, b)
    return covered


def _merged(spans):
    """Overlapping or touching ``(start, end)`` spans as one, in order."""
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _select_runs(verified):
    """The verified runs that stand, in target order: none starts inside the one before it (of two that start
    together the longer stands) and none takes its reference's own time backwards."""
    selected = []
    last_target = -1
    last_references: dict[str, Fraction] = {}
    for entry in sorted(verified, key=lambda row: (row[0], -(row[1] - row[0]))):
        lo, hi, ref, offset, *_, speed, _ = entry
        if lo < last_target or speed * lo + offset < last_references.get(ref.id, Fraction(-1)):
            continue
        selected.append(entry)
        last_target = hi
        last_references[ref.id] = speed * hi + offset
    return selected


def _coverage(window, runs, owner_keeps):
    """What a suggested window's confirmed share is made of: how much of it the runs that stand cover, and the holes
    between them that would be filled (see ``_bridges``)."""
    holes = _bridges(window, runs, owner_keeps)
    covered = sum(max(0, min(b, window.end_ms) - max(a, window.start_ms)) for a, b, *_ in runs)
    return covered, holes


def _share(window, covered, holes):
    """The share of a suggested window that counts as confirmed: what the runs cover plus the holes filled."""
    return Fraction(covered + sum(hi - lo for lo, hi, *_ in holes), window.end_ms - window.start_ms)


def _window_confirmed(window, verified, owner_keeps):
    """Whether the runs verified so far would confirm a suggested window as it stands: at least
    ``PLEX_CONFIRMED_SHARE`` of it, short holes filled."""
    return _share(window, *_coverage(window, _select_runs(verified), owner_keeps)) >= PLEX_CONFIRMED_SHARE


def _hint_range(window, target_online, reference_online, duration):
    """Where else in a reference the window's content may be, from the online databases' positions: the window shifted
    by how far the reference's intro (or credits) sits from the target's, or around the reference's own online position
    when the target has none. None when the reference has no online position for the window's kind."""
    there = next((o for o in reference_online if o.kind == window.kind), None)
    if there is None:
        return None
    here = next((o for o in target_online if o.kind == window.kind), None)
    if here is not None:
        shift = there.start_ms - here.start_ms
        lo, hi = window.start_ms - CONTEXT_MS + shift, window.end_ms + CONTEXT_MS + shift
    else:
        lo, hi = there.start_ms, there.end_ms
    lo, hi = max(0, lo - REFERENCE_RADIUS_MS), min(duration, hi + REFERENCE_RADIUS_MS)
    return (lo, hi) if lo < hi else None


class _PairUnsupported(decoder.DecodeError):
    """Two cleanly decoded files turn out not to be comparable with each other: their frames carry another kind of
    picture, or frame it another way, than their stream headers said."""


class _ReferenceFailed(decoder.DecodeError):
    """A reference could not be read. That is the reference's failure, never the target's: the reference is left out
    and the analysis goes on."""


@contextlib.contextmanager
def _of_reference():
    """Whatever a read fails with inside this block is a ``_ReferenceFailed``."""
    try:
        yield
    except (decoder.Cancelled, _ReferenceFailed):
        raise
    except decoder.DecodeError as exc:
        raise _ReferenceFailed(str(exc)) from exc


class _Reference:
    """A reference's reader (frames or sound chunks): entering, reading and leaving it fail as ``_ReferenceFailed``.
    Inside the ``with`` it is what the reader's own ``with`` gives: its items and its attributes."""

    def __init__(self, reader):
        self.reader = self.stream = reader

    def __enter__(self):
        with _of_reference():
            self.stream = self.reader.__enter__()
        return self

    def __exit__(self, *failure):
        with _of_reference():
            return self.reader.__exit__(*failure)

    def __iter__(self):
        return self

    def __next__(self):
        with _of_reference():
            return next(self.stream)

    def __getattr__(self, name):
        return getattr(self.stream, name)


def _as_reference(stream):
    """Iterate a stream derived from a reference's reader: what it fails with is a ``_ReferenceFailed`` too."""
    while True:
        with _of_reference():
            try:
                item = next(stream)
            except StopIteration:
                return
        yield item


def _id(*parts):
    return hashlib.sha256(json.dumps(parts, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:32]


def _ratio(value):
    return Fraction(str(value).replace(":", "/"))


class _Format(NamedTuple):
    """What a file's video stream says of its picture before any frame is decoded."""

    size: tuple[int, int, Fraction]  # width, height and sample aspect ratio
    colour: tuple[str, str]  # the colour class: pictures compare only within one
    rate: Fraction  # frames per second


def _representation(item, stream, cancel, pause, *, ffmpeg_path=None):
    """The stream's picture format. Stream declarations supplement, never replace, the same-decode frame audit."""
    info = json.loads(
        decoder.run(
            [
                decoder.paired_ffprobe(ffmpeg_path or ffmpeg_binary()),
                "-v",
                "error",
                "-show_streams",
                "-of",
                "json",
                item.canonical_path,
            ],
            cancel,
            pause,
        )
    )
    video = next(s for s in info["streams"] if s["index"] == stream)
    if video.get("field_order") not in (None, "unknown", "progressive"):
        raise decoder.DecodeError("Interlaced video is unsupported by native-code-signal profile")
    if any(video.get(k, 0) for k in ("crop_top", "crop_bottom", "crop_left", "crop_right")):
        raise decoder.DecodeError("Cropped video interpretation is unsupported")
    if float(video.get("tags", {}).get("rotate", 0)) != 0:
        raise decoder.DecodeError("Rotated video interpretation is unsupported")
    if any(side.get("side_data_type") not in decoder.STREAM_SIDE_DATA for side in video.get("side_data_list", [])):
        raise decoder.DecodeError("Video interpretation side data is unsupported")
    try:
        size = (int(video["width"]), int(video["height"]), _ratio(video["sample_aspect_ratio"]))
        rate = _ratio(video["r_frame_rate"])
        if min(size) <= 0 or rate <= 0:
            raise ValueError("Nonpositive geometry or cadence")
        for key in verify.COLOR_FIELDS:
            canonical.declaration(video.get(key), key)
        colour = canonical.colour_class(video.get("color_transfer"), video.get("color_space"), size[0], size[1])
    except (KeyError, ValueError, ZeroDivisionError) as exc:
        raise decoder.DecodeError("Missing native representation declarations") from exc
    decoder.check_current(item)
    return _Format(size, colour, rate)


def _track_map(tracks, reference_tracks):
    """The reference's track for each of the target's, or None when the reference has none for one of them.

    A track's partner is the reference track at the same place among those of its language (an untagged track's
    language is the unknown one); a track whose language the reference lacks pairs with the track at its own position.
    A wrong pairing can only cost a confirmation, never grant one: every pair is still compared sample for sample.
    A reference may hold more tracks than the target; those are not compared."""
    by_language = defaultdict(list)
    for track in reference_tracks:
        by_language[track[4]].append(track)
    places: Counter = Counter()
    mapped = []
    for position, track in enumerate(tracks):
        same = by_language[track[4]]
        if places[track[4]] < len(same):
            mapped.append(same[places[track[4]]])
        elif position < len(reference_tracks):
            mapped.append(reference_tracks[position])
        else:
            return None
        places[track[4]] += 1
    return mapped


def _scan(item, stream, start, end, cancel, pause, threads, *, ffmpeg_path=None, on_progress=None):
    """Keep only hints; accept them after complete native decoder exit.

    A window the scan cache holds (a file's frames are scanned once, whichever episode asks) is answered from it, still
    followed by the generation check. ``on_progress`` is told the fraction of the window scanned."""
    kept = scan_cache.cache
    if kept is not None:
        found = kept.lookup(item, stream, start, end)
        if found is not None:
            decoder.check_current(item)
            if on_progress:
                on_progress(1.0)
            return found
    hints, declarations, unspecified, region = [], None, [], None
    with decoder.native_frames(
        item, stream, start, end, cancel, pause, threads, ffmpeg_path=ffmpeg_path, reduce=True
    ) as frames:
        for frame in frames:
            if cancel():
                raise decoder.Cancelled("Analysis cancelled")
            if frame.interpretation_flags:
                raise decoder.DecodeError("Unsupported native interpretation")
            signature = verify.frame_declarations(frame)
            if declarations is None:
                declarations = signature
                unspecified = [
                    k for k in verify.COLOR_FIELDS if canonical.declaration(getattr(frame, k), k) == "unspecified"
                ]
                region = canonical.whole(frame)
            elif signature != declarations:
                raise decoder.DecodeError("Native declarations changed within comparison window")
            if len(hints) >= PROFILE_SPEC["candidate"]["maximum_scan_frames"]:
                raise decoder.DecodeError("Native comparison frame budget exceeded")
            if frame.pts_ms < end:
                picture = canonical.picture(frame, region)
                hints.append((frame.pts_ms, verify.descriptor(picture), verify.spatial_information(picture)))
                if on_progress and len(hints) % PROGRESS_EVERY_FRAMES == 0:
                    on_progress(min(1.0, float((frame.pts_ms - start) / (end - start))))
    decoder.check_current(item)
    if kept is not None:
        kept.store(item, stream, start, end, hints, declarations, unspecified)
    return hints, declarations, unspecified


def _framing(declarations, reference_declarations):
    """How two scanned files frame the same picture (see ``canonical.regions``), from what their decoded frames
    declare; ``_PairUnsupported`` when the frames do not carry the same kind of picture in the same framing."""

    def colour(declared):
        fields = dict(zip(verify.COLOR_FIELDS, declared[5:], strict=False))
        return canonical.colour_class(fields["color_transfer"], fields["color_space"], declared[0], declared[1])

    framing = canonical.regions(
        (declarations[0], declarations[1], declarations[4]),
        (reference_declarations[0], reference_declarations[1], reference_declarations[4]),
    )
    if framing is None or colour(declarations) != colour(reference_declarations):
        raise _PairUnsupported("The two files' frames do not show the same kind of picture")
    return framing


def _video_support(
    target, reference, video, ref_video, start, end, clock, hold, framing, cancel, pause, threads, *, ffmpeg_path=None
):
    """The target times whose picture is the reference's on ``clock``. Stage locally; either decoder's final failure
    invalidates all support.

    Every target frame is compared with the reference frames within reach of where the clock puts it: the one frame
    there when the two files run frame for frame, the frames shown meanwhile when their cadences differ (``hold``, see
    ``recurrence.hold``). A frame's verdict is its best pairing, on both sides: a reference frame between the
    target's that no target frame agrees with is footage the target does not have, and vetoes like a failed target
    frame. ``recurrence.agreeing`` then decides which frames count (isolated near misses do, anything else does not).
    Two frames are compared as canonical pictures of the regions ``framing`` gives them (``canonical.registered``).
    """
    times: list[Fraction] = []  # the target frames
    ratios: list[float | None] = []  # and how far each is from its best reference frame (None: it has none)
    # The reference frames: [pts, the frame and its canonical pictures by region while in reach, best ratio].
    others: list[list] = []

    def settle(index, entry, excess):
        """A pair's ratio counts for its target frame (``index``) and, when it is the best so far, for its reference
        frame (``entry``)."""
        ratios[index] = excess if ratios[index] is None else min(ratios[index], excess)
        entry[3] = excess if entry[3] is None else min(entry[3], excess)

    # A GPU decode delivers the frames already reduced to their canonical cells; pairs of those are measured in batches
    # on the GPU (``gpu_pairs``) and the CPU measures any pair it cannot. A measured framing moves the regions per
    # pair, which needs the whole frames, so nothing is reduced then.
    reduced = not framing.measured
    pairs = gpu_pairs.GpuPairs(framing, settle) if reduced else None
    with decoder.native_frames(
        target,
        video,
        math.floor(start),
        math.ceil(end),
        cancel,
        pause,
        threads,
        ffmpeg_path=ffmpeg_path,
        reduce=framing.target if reduced else None,
    ) as left:
        with _Reference(
            decoder.native_frames(
                reference,
                ref_video,
                max(0, math.floor(clock.at(start) - hold)),
                math.ceil(clock.at(end) + hold),
                cancel,
                pause,
                threads,
                ffmpeg_path=ffmpeg_path,
                reduce=framing.reference if reduced else None,
            )
        ) as right:
            # Container timestamps are whole ticks (a Matroska file's are milliseconds), so two files' frames of the
            # same instant may sit one tick apart, and the picture offset, itself a difference of two of them, is
            # rounded the same way: real 24000/1001 fps pairs land 0 or 1 ms off. Pairing therefore tolerates one
            # tick of the coarser stream when that is wider than the profile's floor; frames are never that close.
            tolerance = hold + max(
                Fraction(PROFILE_SPEC["video"]["pair_tolerance_ms"]), left.time_base * 1000, right.time_base * 1000
            )
            near: deque[list] = deque()  # the reference frames the next target frames may still pair with
            b = next(right, None)
            declarations = other_declarations = None
            for a in left:
                signature = verify.frame_declarations(a)
                if declarations is not None and signature != declarations:
                    raise decoder.DecodeError("Native declarations changed during verification")
                declarations = signature
                wanted = clock.at(a.pts_ms)
                while near and near[0][0] < wanted - tolerance:
                    gone = near.popleft()
                    gone[1] = gone[2] = None  # out of reach for good: its frame and pictures are no longer needed
                while b is not None and b.pts_ms <= wanted + tolerance:
                    signature = verify.frame_declarations(b)
                    if other_declarations is not None and signature != other_declarations:
                        raise _ReferenceFailed("Native declarations changed during verification")
                    other_declarations = signature
                    others.append([b.pts_ms, None, None, None])
                    if b.pts_ms >= wanted - tolerance:
                        others[-1][1:3] = b, {}
                        near.append(others[-1])
                    b = next(right, None)
                times.append(a.pts_ms)
                ratios.append(None)
                index = len(ratios) - 1
                pictures: dict = {}  # this frame's canonical picture, by the region it is compared in
                for entry in near:
                    if pairs is not None and pairs.offer(index, entry, a, entry[1]):
                        continue
                    region, other_region = canonical.registered(framing, a, entry[1])
                    if region not in pictures:
                        pictures[region] = canonical.picture(a, region)
                    if other_region not in entry[2]:
                        entry[2][other_region] = canonical.picture(entry[1], other_region)
                    settle(index, entry, verify.picture_excess(pictures[region], entry[2][other_region]))
            if pairs is not None:
                pairs.flush()
            while b is not None:
                b = next(right, None)
    decoder.check_current(target)
    with _of_reference():
        decoder.check_current(reference)
    safe, bad = [], []
    agree = verify.agreeing(times, ratios)
    for index in range(len(times) - 1):
        lo, hi = max(start, times[index]), min(end, times[index + 1])
        if lo >= hi:
            continue
        if agree[index] and agree[index + 1] and 0 < times[index + 1] - times[index] <= verify.MAX_FRAME_GAP_MS:
            # The pair's support is the target frame's span; the reference frame's span agrees with it within the
            # pairing tolerance, and clipping to their intersection would leave tick-rounding holes of a millisecond
            # between consecutive pairs that split a run into pieces too short to anchor.
            safe.append((lo, hi))
        else:
            bad.append((lo - verify.CHANGE_GUARD_MS, hi + verify.CHANGE_GUARD_MS))
    if times:
        # The reference frames between the target's first and last: one outside them has no target frame to pair
        # with because the window ends there, not because the target lacks it.
        between = [(clock.back(pts), ratio) for pts, *_, ratio in others if times[0] <= clock.back(pts) <= times[-1]]
        verdicts = verify.agreeing([t for t, _ in between], [ratio for _, ratio in between])
        for (moment, _), agreed in zip(between, verdicts, strict=True):
            if not agreed:
                bad.append((moment - hold - verify.CHANGE_GUARD_MS, moment + hold + verify.CHANGE_GUARD_MS))
    return verify.subtract(safe, bad)


def _canonical_sound(chunks, speed=Fraction(1), offset=Fraction(0), layout=None):
    """A track's canonical sound as its native chunks arrive (see ``canonical.resampler``): pieces
    ``(index of the first sample, samples)``, sample n being the track at ``speed * n / SOUND_RATE`` seconds plus
    ``offset`` ms. The native chunks must follow each other without a gap. ``layout`` (a list) is given the track's
    channel names."""
    resampler = expected = current = None
    for chunk in chunks:
        rate = chunk.sample_rate
        if expected is not None and chunk.support_start_ms != expected:
            raise decoder.DecodeError("Native audio chunks are not contiguous")
        if chunk.support_end_ms - chunk.support_start_ms != Fraction(len(chunk.data) * 1000, rate):
            raise decoder.DecodeError("Native audio sample support mismatch")
        if resampler is None:
            resampler = canonical.resampler(rate, chunk.support_start_ms, speed, offset)
            current = (rate, chunk.channel_ids)
            if layout is not None:
                layout.extend(chunk.channel_ids)
        elif (rate, chunk.channel_ids) != current:
            raise decoder.DecodeError("Native audio rate or channel layout changed")
        expected = chunk.support_end_ms
        samples = resampler.feed(canonical.mono(chunk.data, chunk.channel_ids))
        if len(samples):
            yield resampler.next - len(samples), samples


def _audio_support(
    target, reference, tracks, ref_tracks, start, end, sounds, cancel, pause, threads, *, ffmpeg_path=None
):
    """The target times where every track's canonical sound is its reference track's, with the channels each track's
    sound is mixed from and the level its reference was brought to. ``sounds`` gives each track's sound clock and
    level (see ``_sound_offset``). A sound that ends inside the window (the picture often outlasts it by a few
    frames) is compared up to its end, or the end of the other's if that is earlier: nothing past either end is
    support, and anything either holds that the other does not is simply not compared."""
    safe = [(start, end)]
    checked, levels = {}, {}
    if not tracks:
        raise decoder.DecodeError("No mapped native audio tracks")
    seconds = PROFILE_SPEC["audio"]["chunk_seconds"]
    for track, ref_track, (clock, level) in zip(tracks, ref_tracks, sounds, strict=True):
        verifier = None
        layout: list[str] = []
        with decoder.native_audio_chunks(
            target,
            track[1],
            start,
            end,
            cancel,
            pause,
            threads,
            chunk_samples=track[2] * seconds,
            ffmpeg_path=ffmpeg_path,
            to_end=True,
        ) as left:
            with _Reference(
                decoder.native_audio_chunks(
                    reference,
                    ref_track[1],
                    max(Fraction(0), clock.at(start)),
                    clock.at(end),
                    cancel,
                    pause,
                    threads,
                    chunk_samples=ref_track[2] * seconds,
                    ffmpeg_path=ffmpeg_path,
                    to_end=True,
                )
            ) as right:
                # Both sounds arrive on the target's canonical sample grid; what each holds that the other has not
                # reached yet waits here, and the samples both hold are compared as soon as both are in.
                held = [[0, np.zeros(0)], [0, np.zeros(0)]]
                for pieces in zip_longest(
                    _canonical_sound(left, layout=layout),
                    _as_reference(_canonical_sound(right, clock.rate, clock.offset)),
                ):
                    if cancel():
                        raise decoder.Cancelled("Analysis cancelled")
                    for side, piece in zip(held, pieces, strict=True):
                        if piece is not None:
                            side[:] = piece if not len(side[1]) else [side[0], np.concatenate((side[1], piece[1]))]
                    (ours_at, ours), (theirs_at, theirs) = held
                    first, last = max(ours_at, theirs_at), min(ours_at + len(ours), theirs_at + len(theirs))
                    if last <= first:
                        continue
                    if verifier is None:
                        verifier = verify.AudioVerifier(SOUND_RATE, Fraction(first * 1000, SOUND_RATE))
                    verifier.feed(
                        ours[first - ours_at : last - ours_at, None],
                        theirs[first - theirs_at : last - theirs_at, None] * level,
                    )
                    held = [[last, ours[last - ours_at :]], [last, theirs[last - theirs_at :]]]
        # A stream's read counts only AFTER both fully exhausted contexts exit successfully (the readers set their
        # declarations then). Never publish provisional chunks or early matches.
        if left.declarations is None:
            raise decoder.DecodeError("Native audio was not read to its end")
        if right.declarations is None:
            raise _ReferenceFailed("Native audio was not read to its end")
        decoder.check_current(target)
        with _of_reference():
            decoder.check_current(reference)
        checked[track[0]] = canonical.mixed(layout)
        levels[track[0]] = level
        safe = verify.intersect(safe, verifier.finish() if verifier is not None else [])
    return safe, checked, levels


def _sound_offset(
    target,
    reference,
    track,
    ref_track,
    hints,
    rh,
    lo,
    hi,
    clock,
    slack,
    near,
    cancel,
    pause,
    threads,
    *,
    ffmpeg_path=None,
):
    """Where one track's sound lines up for a picture proposal, as ``(the sound's clock, level)``, or None when no
    sound within ``SOUND_SEARCH_MS`` of the offset ``near`` (ms) lines up well enough. The window is wide because
    every release has its own sound/picture sync error: two files of one episode keep their sound up to about
    400 ms from where their pictures put it (see the profile's calibration).

    A bounded probe of the track's canonical sound is cross-correlated with the reference track's, read at the
    proposal's speed (see ``recurrence.sound_lag``): the lag, to a fraction of a sample, is the sound's offset, and
    the level is the factor that brings the reference's sound to the target's (two releases are rarely mastered at
    one loudness). Neither is support: only the strict sound identity on that clock, at that level, is."""
    search, half = Fraction(SOUND_SEARCH_MS), Fraction(SOUND_PROBE_MS, 2)
    # The probe keeps away from the reference's ends so the whole search window exists there, when that leaves enough.
    start, end = max(lo, clock.back(search)), min(hi, clock.back(reference.duration_ms - search))
    if end - start < verify.MIN_RUN_MS:
        start, end = lo, hi
    agreeing = [t for t, _ in verify.agreeing_frames(hints, rh, start, end, clock, slack)]
    centre = agreeing[len(agreeing) // 2] if agreeing else (start + end) / 2
    probe_start = max(start, min(centre - half, end - 2 * half))
    probe_end = min(end, probe_start + 2 * half)
    left = decoder.native_audio_pcm(
        target, track[1], probe_start, probe_end, cancel, pause, threads, ffmpeg_path=ffmpeg_path, to_end=True
    )
    with _of_reference():
        right = decoder.native_audio_pcm(
            reference,
            ref_track[1],
            max(Fraction(0), clock.at(probe_start) - search),
            min(Fraction(reference.duration_ms), clock.at(probe_end) + search),
            cancel,
            pause,
            threads,
            ffmpeg_path=ffmpeg_path,
            to_end=True,
        )
    ours_at, ours = canonical.sound(left.data, left.channel_ids, left.sample_rate, left.support_start_ms)
    theirs_at, theirs = canonical.sound(
        right.data, right.channel_ids, right.sample_rate, right.support_start_ms, clock.rate
    )
    if not len(ours) or len(theirs) < len(ours):
        return None
    # Sample n of ours is the target at n / SOUND_RATE seconds and sample n of theirs the reference at speed times
    # that, so a lag of one sample moves the offset by one sample of reference time.
    step = clock.rate * 1000 / SOUND_RATE
    base = (theirs_at - ours_at) * step  # the sound offset at lag zero
    found = verify.sound_lag(ours, theirs, round((near - base) / step), SOUND_RATE * SOUND_BLOCK_MS // 1000)
    if found is None or found[1] < SOUND_CORRELATION:
        return None
    lag, _, level = found
    if not 1 / SOUND_GAIN_LIMIT <= level <= SOUND_GAIN_LIMIT:
        return None
    return verify.Clock(clock.rate, base + Fraction(round(lag * 64), 64) * step), level


def _picture_span(hints, lo, hi, audio):
    """Where the picture needs checking: the sound support's hull plus the change guard, from the last scanned frame
    at or before it. Support only counts where both signals match, so this is exact: a pair outside it can neither
    support nor, with its guard, veto anything inside, and it saves decoding the rest of a long window."""
    start, end = audio[0][0] - verify.CHANGE_GUARD_MS, audio[-1][1] + verify.CHANGE_GUARD_MS
    aligned = max((t for t, _, _ in hints if t <= start), default=lo)
    return max(lo, aligned), min(hi, end)


def _analyze(
    media: MediaVersion,
    candidates: Sequence[CandidateWindow],
    references: Sequence[MediaVersion],
    *,
    store,
    cancel_check,
    pause_check,
    progress_callback,
    ffmpeg_threads,
    ffmpeg_path=None,
    owner_keeps: Sequence[tuple[int, int]] = (),
    online: Mapping[str, Sequence[OnlineWindow]] | None = None,
    movie: bool = False,
    comparison_failure=None,
) -> AnalysisDraft:
    """One analysis pass. ``comparison_failure`` is what an earlier pass failed with (a headline, then details): it
    turns comparing off, so only the suggested windows (Plex's markers, online times) are offered and that failure is
    why nothing was confirmed. ``owner_keeps`` are the (start, end) ranges the owner keeps: no gap is ever filled
    across one. ``online`` maps a media version id (this file's and its references') to where the online databases
    put its intro and credits, read on that file's clock: the target's become suggested windows, every file's say
    where else to look in a reference. ``movie`` marks a film: nothing is compared with it, and the notes say so."""
    cancel = cancel_check or (lambda: False)
    pause = pause_check or (lambda: False)
    threads = 1
    ffmpeg_path = ffmpeg_path or ffmpeg_binary()
    reasons: list[str] = []
    verified: list[_VerifiedRun] = []
    hint_notes: list[str] = []
    online = online or {}
    windows, ignored = _fit_windows(
        sorted(set(candidates), key=lambda w: (w.start_ms, w.end_ms, w.kind, w.source)), media.duration_ms
    )
    windows, suggested_by, online_notes = _with_online(windows, online.get(media.id, ()))
    try:
        if cancel():
            raise decoder.Cancelled("Analysis cancelled")
        if comparison_failure is None:
            video, tracks, _ = decoder.probe(media, cancel, pause, ffmpeg_path=ffmpeg_path)
            representation = _representation(media, video, cancel, pause, ffmpeg_path=ffmpeg_path)
        else:
            video = representation = None
            tracks = _signature_tracks(media)
        # The references: the nearest admitted episodes this file can be compared with, MAX_REFERENCES of them to begin
        # with (``spare`` below holds the ones a suggested window goes on to when these leave it less than half
        # confirmed). One that cannot be used is passed over for the next nearest, whether that shows when it is first
        # looked at or only when reading it fails: a reference's failure never costs the target its analysis.
        refs: list[tuple] = []  # (reference, its video stream, its track for each of the target's, its picture format)
        left_out = {"picture": 0, "audio": 0, "unreadable": 0}  # the nearer episodes passed over, and why
        compared: set[str] = set()  # the references a search of which ran to its end
        admitted = admit_references(media, references, store)
        provenance = {ref.id: snapshot for ref, snapshot in admitted}
        waiting = iter(admitted)

        def next_reference():
            """The next nearest admitted episode this file can be compared with, or None when none is left."""
            for ref, _ in waiting:
                try:
                    rv, rt, _ = decoder.probe(ref, cancel, pause, ffmpeg_path=ffmpeg_path)
                    fmt = _representation(ref, rv, cancel, pause, ffmpeg_path=ffmpeg_path)
                    mapped = _track_map(tracks, rt)
                    if fmt.colour != representation.colour or canonical.regions(representation.size, fmt.size) is None:
                        left_out["picture"] += 1
                    elif mapped is None:
                        left_out["audio"] += 1
                    else:
                        return ref, rv, mapped, fmt
                except decoder.Cancelled:
                    raise
                except (decoder.DecodeError, OSError, ValueError, KeyError, TypeError):
                    left_out["unreadable"] += 1
            return None

        while comparison_failure is None and len(refs) < MAX_REFERENCES:
            found = next_reference()
            if found is None:
                break
            refs.append(found)
        # The episodes after those, read as far as a window needs them: the next nearest, in the same order.
        spare: list[tuple] = []

        def more_references(start, count):
            """``count`` of the episodes after the nearest ones, from the ``start``-th on, as far as there are any."""
            while len(spare) < start + count and (entry := next_reference()) is not None:
                spare.append(entry)
            return spare[start : start + count]

        # The bar: each window takes an equal share, the target's scan the first part of it and the searches the rest.
        reported = [-1]

        def report(number, fraction, message):
            percent = min(99, int(100 * (number + fraction) / max(1, len(windows))))
            if progress_callback and (percent > reported[0] or percent == 0 and reported[0] < 0):
                reported[0] = percent
                progress_callback(percent, message)

        outcomes: dict[
            CandidateWindow, tuple[set[str], set[str]]
        ] = {}  # per window: references searched, sound matched
        for number, window in enumerate(windows):
            # A Plex marker too long to compare is not an error: it cannot be confirmed, so it is only a suggestion.
            too_long = window.end_ms - window.start_ms > MAX_CANDIDATE_MS
            if (
                window.kind not in ("intro", "credits", "recap", "preview")
                or not (0 <= window.start_ms < window.end_ms <= media.duration_ms)
                or (too_long and window.source not in SUGGESTED_SOURCES)
            ):
                raise decoder.DecodeError("Invalid or oversized candidate window")
            if not refs or too_long:
                continue
            # What this window's searches found, for the verdict on the kind (see ``uniqueness``): the references whose
            # search ran to its end, and those whose sound lines up somewhere in it.
            searched: set[str] = set()
            sound: set[str] = set()
            outcomes[window] = (searched, sound)
            report(number, 0.0, "Verifying native audiovisual recurrence")
            ts, te = max(0, window.start_ms - CONTEXT_MS), min(media.duration_ms, window.end_ms + CONTEXT_MS)
            try:
                hints, declarations, unspecified = _scan(
                    media,
                    video,
                    ts,
                    te,
                    cancel,
                    pause,
                    threads,
                    ffmpeg_path=ffmpeg_path,
                    on_progress=lambda fraction, number=number: report(
                        number, TARGET_SCAN_SHARE * fraction, "Verifying native audiovisual recurrence"
                    ),
                )
            except decoder.Cancelled:
                raise
            except decoder.DecodeError:
                if window.source != ONLINE_SOURCE:
                    raise
                # Online times are optional: a window that cannot be compared stays a suggestion, whatever the file.
                reasons.append(ONLINE_NOT_COMPARED.format(kind=window.kind))
                continue

            def neighbourhood(entry, ts=ts, te=te):
                """Where a reference is searched by default: around the window's own position in it, at every speed
                the reference may play the footage at (a 25 fps copy is a minute ahead by the end of an episode)."""
                rates = verify.speeds(representation.rate, entry[3].rate)
                return (
                    max(0, math.floor(min(rate * ts for rate in rates)) - REFERENCE_RADIUS_MS),
                    min(entry[0].duration_ms, math.ceil(max(rate * te for rate in rates)) + REFERENCE_RADIUS_MS),
                )

            # A search: a reference (as in ``refs``), the range of it to search, and whether an online position added it.
            searches = [(*entry, *neighbourhood(entry), False) for entry in refs]
            hinted_refs: set[str] = set()
            progress = [0.0] * len(searches)  # how far each search is, 0..1: the bar's share of this window's searches
            failed_from = [math.inf]  # the first search (in order) that failed the target: later ones are not needed

            def search(
                position,
                ref,
                rv,
                rt,
                fmt,
                rs,
                re,
                window=window,
                ts=ts,
                te=te,
                hints=hints,
                declarations=declarations,
                unspecified=unspecified,
                progress=progress,
                failed_from=failed_from,
                searches=searches,
                searched=searched,
                sound=sound,
            ):
                """One reference's search of one window: the runs it verified and the notes it leaves. It runs on a
                worker thread beside the other searches and only reads what they share; whoever calls it merges the
                result in search order, so the outcome does not depend on which finishes first."""
                rows: list[_VerifiedRun] = []
                notes: list[str] = []

                def stop():
                    return cancel() or position > failed_from[0]

                def scanned(fraction):
                    progress[position] = SCAN_SHARE * fraction

                try:
                    if rs >= re:
                        return rows, notes
                    with _of_reference():
                        rh, r_decl, _ = _scan(
                            ref, rv, rs, re, stop, pause, threads, ffmpeg_path=ffmpeg_path, on_progress=scanned
                        )
                    framing = _framing(declarations, r_decl)
                    if not hints or not rh:
                        return rows, notes
                    proposals = verify.perceptual_shifts(
                        hints,
                        rh,
                        [
                            (rate, verify.hold(rate, representation.rate, fmt.rate))
                            for rate in verify.speeds(representation.rate, fmt.rate)
                        ],
                    )
                    for count, (clock, hold) in enumerate(proposals):
                        progress[position] = SCAN_SHARE + (1 - SCAN_SHARE) * count / len(proposals)
                        slack = hold + HINT_TICK_MS
                        lo = max(Fraction(ts), clock.back(0), hints[0][0], clock.back(rh[0][0]))
                        hi = min(Fraction(te), clock.back(ref.duration_ms), hints[-1][0], clock.back(rh[-1][0]))
                        if hi - lo < verify.MIN_RUN_MS:
                            continue
                        # Every track's sound has its own clock: the first track's is looked for around the picture's
                        # offset, the others' around the first's. A track with none ends the proposal.
                        sounds: list[tuple[verify.Clock, float]] = []
                        for track, ref_track in zip(tracks, rt, strict=True):
                            found = _sound_offset(
                                media,
                                ref,
                                track,
                                ref_track,
                                hints,
                                rh,
                                lo,
                                hi,
                                clock,
                                slack,
                                sounds[0][0].offset if sounds else clock.offset,
                                stop,
                                pause,
                                threads,
                                ffmpeg_path=ffmpeg_path,
                            )
                            if found is None:
                                break
                            sounds.append(found)
                        if len(sounds) != len(tracks):
                            continue
                        # The sound is checked over what the reference holds on each track's clock; the picture on
                        # the picture's, and only where the sound already matched, since it is the slow half (minutes
                        # per proposal) and a proposal whose sound does not match can never be supported.
                        slo = max(lo, *(sound.back(0) for sound, _ in sounds))
                        shi = min(hi, *(sound.back(ref.duration_ms) for sound, _ in sounds))
                        audio, checked, levels = (
                            _audio_support(
                                media,
                                ref,
                                tracks,
                                rt,
                                slo,
                                shi,
                                sounds,
                                stop,
                                pause,
                                threads,
                                ffmpeg_path=ffmpeg_path,
                            )
                            if shi - slo >= verify.MIN_RUN_MS
                            else ([], {}, {})
                        )
                        picture = []
                        if audio:
                            sound.add(ref.id)
                            plo, phi = _picture_span(hints, lo, hi, audio)
                            picture = _video_support(
                                media,
                                ref,
                                video,
                                rv,
                                plo,
                                phi,
                                clock,
                                hold,
                                framing,
                                stop,
                                pause,
                                threads,
                                ffmpeg_path=ffmpeg_path,
                            )
                        for a, b in verify.intersect(audio, picture):
                            anchored = verify.anchor(
                                hints, rh, max(a, window.start_ms), min(b, window.end_ms), clock, slack
                            )
                            if anchored is not None:
                                a, b, anchors = anchored
                                rows.append(
                                    (
                                        a,
                                        b,
                                        ref,
                                        clock.offset,
                                        unspecified,
                                        anchors,
                                        sounds[0][0].offset,
                                        checked,
                                        clock.rate,
                                        levels,
                                    )
                                )
                    searched.add(ref.id)
                    with _of_reference():
                        decoder.check_current(ref)
                except decoder.Cancelled:
                    raise
                except (_ReferenceFailed, _PairUnsupported):
                    raise  # the reference's own failure: it is replaced and the other searches go on
                except decoder.DecodeError:
                    if not (window.source == ONLINE_SOURCE or searches[position][6]):
                        failed_from[0] = min(failed_from[0], position)
                    raise
                except (OSError, ValueError, KeyError, TypeError):
                    notes.append(COMPARISON_UNAVAILABLE)
                finally:
                    progress[position] = 1.0
                return rows, notes

            def run(positions, number=number, window=window, searches=searches, progress=progress):
                """Search these positions of ``searches`` (up to _search_workers() at a time), report the bar as they go,
                and merge their rows and notes in position order: the first search to fail the target, in that order,
                raises. Returns the searches whose reference could not be used, as ``(position, cause)``."""
                configured = min(SEARCH_WORKERS, len(positions))
                workers = min(_search_workers(), len(positions))
                if workers < configured:
                    logger.info(
                        "Reprise compares with {} other episode(s) at a time instead of {}: the container's process "
                        "limit leaves no room for more",
                        workers,
                        configured,
                    )
                dropped = []
                with ThreadPoolExecutor(max_workers=workers) as pool:

                    def pinned_search(position):
                        with acceleration.pinned(position):  # a slot per search: see acceleration.order
                            return search(position, *searches[position][:6])

                    futures = [pool.submit(pinned_search, position) for position in positions]
                    while True:
                        waiting_on = [p for p, f in zip(positions, futures, strict=True) if not f.done()]
                        report(
                            number,
                            TARGET_SCAN_SHARE + (1 - TARGET_SCAN_SHARE) * sum(progress) / len(searches),
                            f"Checking the {window.kind}: comparing with episode "
                            f"{(waiting_on[0] if waiting_on else positions[-1]) + 1} of {len(searches)}",
                        )
                        if not waiting_on:
                            break
                        # Only the unfinished ones: waiting on a set with a finished future returns at once, a busy loop.
                        wait([f for f in futures if not f.done()], timeout=0.5, return_when=FIRST_COMPLETED)
                    for position, future in zip(positions, futures, strict=True):
                        try:
                            rows, notes = future.result()
                        except decoder.Cancelled:
                            raise
                        except decoder.DecodeError as exc:
                            if window.source == ONLINE_SOURCE or searches[position][6]:
                                # An online window, and a search an online position added, are optional: they never
                                # make the analysis fail, they are only noted.
                                reasons.append(
                                    ONLINE_NOT_COMPARED.format(kind=window.kind)
                                    if window.source == ONLINE_SOURCE
                                    else ONLINE_HINT_FAILED.format(kind=window.kind)
                                )
                            elif isinstance(exc, _ReferenceFailed | _PairUnsupported):
                                # The other episode's failure, not this one's: it is left out, the analysis goes on.
                                logger.warning(
                                    "Reprise left {} out of the comparison: {}",
                                    searches[position][0].canonical_path,
                                    exc,
                                )
                                dropped.append(
                                    (position, "picture" if isinstance(exc, _PairUnsupported) else "unreadable")
                                )
                            else:
                                raise  # every other failure ends the analysis as before
                            continue
                        compared.add(searches[position][0].id)
                        verified.extend(rows)
                        reasons.extend(notes)
                return dropped

            # The default neighbourhoods first, together. A reference that could not be used is left out from here on
            # and the next nearest episode, when there is one, is searched in its place.
            positions = list(range(len(searches)))
            while positions:
                dropped = run(positions)
                positions = []
                for position, cause in dropped:
                    refs[:] = [entry for entry in refs if entry[0].id != searches[position][0].id]
                    left_out[cause] += 1
                    replacement = next_reference()
                    if replacement is not None:
                        refs.append(replacement)
                        searches.append((*replacement, *neighbourhood(replacement), False))
                        progress.append(0.0)
                        positions.append(len(searches) - 1)
            # Where the online databases put the same content in a reference is one more place to search. It is tried
            # only when the default searches are all in, while the stretch they expect here is not yet half confirmed,
            # and only for a reference that confirmed nothing of this window where the default neighbourhood was searched.
            hinted_from = len(searches)
            for entry in refs:
                extra = _hint_range(window, online.get(media.id, ()), online.get(entry[0].id, ()), entry[0].duration_ms)
                default = neighbourhood(entry)
                if extra is not None and not (default[0] <= extra[0] and extra[1] <= default[1]):
                    searches.append((*entry, *extra, True))
                    progress.append(0.0)
            for position in range(hinted_from, len(searches)):
                ref = searches[position][0]
                here = next((o for o in online.get(media.id, ()) if o.kind == window.kind), None)
                lo, hi = (
                    (max(window.start_ms, here.start_ms), min(window.end_ms, here.end_ms))
                    if here is not None
                    else (window.start_ms, window.end_ms)
                )
                if hi > lo and Fraction(_confirmed_ms(verified, lo, hi), hi - lo) >= PLEX_CONFIRMED_SHARE:
                    progress[position] = 1.0
                    continue
                if any(
                    entry[2].id == ref.id and entry[0] < window.end_ms and entry[1] > window.start_ms
                    for entry in verified
                ):
                    progress[position] = 1.0
                    continue
                hinted_refs.add(ref.id)
                run([position])
            if any(
                entry[2].id in hinted_refs and entry[0] < window.end_ms and entry[1] > window.start_ms
                for entry in verified
            ):
                hint_notes.append(
                    f"The online times for the {window.kind} showed where to look in another episode, and comparing "
                    "there confirmed it."
                )
            # Episodes further off. The nearest may simply not carry what this window holds (a special's own title
            # sequence, a variant of it that only two episodes share), and a window Plex or an online database suggests
            # is worth a wider look: while it is still less than half confirmed, the next episodes in order are
            # searched, MAX_REFERENCES at a time, up to EXTRA_REFERENCES more, each around the window's own position
            # only (the online databases are asked about the nearest alone). What they confirm is confirmed exactly as
            # for the nearest: nothing is guessed.
            used = 0  # how many of ``spare`` this window has searched
            while (
                window.source in SUGGESTED_SOURCES
                and used < EXTRA_REFERENCES
                and not _window_confirmed(window, verified, owner_keeps)
            ):
                batch = more_references(used, min(MAX_REFERENCES, EXTRA_REFERENCES - used))
                if not batch:
                    break
                logger.info(
                    "Reprise searches {} more episode(s) for the {}: the nearest leave it less than half confirmed",
                    len(batch),
                    window.kind,
                )
                first = len(searches)
                for entry in batch:
                    searches.append((*entry, *neighbourhood(entry), False))
                    progress.append(0.0)
                used += len(batch)
                for position, cause in run(list(range(first, len(searches)))):
                    # One that cannot be used is left out from here on, in every window, and the next takes its place.
                    spare[:] = [entry for entry in spare if entry[0].id != searches[position][0].id]
                    left_out[cause] += 1
                    used -= 1
        decoder.check_current(media)
        # The headline: why nothing could be compared, or which nearer episodes were passed over. It leads the notes.
        if comparison_failure is not None:
            headline = [comparison_failure[0]]
        elif not admitted:
            headline = [NO_REFERENCES]
        elif refs or compared:
            headline = _left_out_notes(left_out)
        elif left_out["picture"] == len(admitted):
            headline = [COPY_PICTURE_DIFFERS]
        elif left_out["audio"] == len(admitted):
            headline = [COPY_AUDIO_DIFFERS]
        else:
            headline = [NO_USABLE_REFERENCES, *_left_out_notes(left_out)]
        reasons[:0] = headline
        for entry in verified:
            decoder.check_current(entry[2])
        # What the searches found, one kind at a time (the windows of a kind share it), kept with the evidence so the
        # report can say an intro is one of a kind. Nothing below decides on it.
        facts: dict[str, dict] = {}
        for kind in sorted({w.kind for w in outcomes}):
            of_kind = [w for w in outcomes if w.kind == kind]
            spans = _merged([(w.start_ms, w.end_ms) for w in of_kind])
            inside = [run for run in verified if any(run[0] < hi and run[1] > lo for lo, hi in spans)]
            searched_here = set().union(*(outcomes[w][0] for w in of_kind))
            if searched_here:
                facts[kind] = {
                    "kind": kind,
                    "episodes_searched": len(searched_here),
                    "sound_matched": len(set().union(*(outcomes[w][1] for w in of_kind))),
                    "picture_matched": len({run[2].id for run in inside}),
                    "confirmed_ms": sum(_confirmed_ms(inside, lo, hi) for lo, hi in spans),
                }
        verified = _select_runs(verified)
        # A suggested window (Plex's marker, or an online database's times) is the comparison's to propose only when at
        # least half of it was confirmed; otherwise it is not a keep but a suggestion that skips nothing until the owner
        # approves it, and the smaller confirmed fragments inside it are dropped (a one-second skip in the middle of an
        # intro helps nobody) and noted.
        # A hole of at most BRIDGE_MAX_GAP_MS between two confirmed runs inside such a window is filled: two lossy
        # encodes fail some frames' limits and each failure vetoes its neighbourhood, so a real intro is confirmed
        # in fragments. Filled time counts as confirmed for the share.
        suggested, fragments, bridged = [], {}, []
        for w in windows:
            if w.source not in SUGGESTED_SOURCES:
                continue
            covered, holes = _coverage(w, verified, owner_keeps)
            if _share(w, covered, holes) < PLEX_CONFIRMED_SHARE:
                suggested.append(w)
                if covered:
                    fragments[w] = covered
            else:
                bridged.extend(holes)
        windows = [w for w in windows if w not in suggested]
        verified = [run for run in verified if not any(run[0] < w.end_ms and run[1] > w.start_ms for w in suggested)]
        # Unknown remains a keep; only a hole inside a suggested window (above) is ever filled.
        ranges, evidence = [], []
        boundaries = sorted(
            {x for w in windows for x in (w.start_ms, w.end_ms)}
            | {x for a, b, *_ in verified for x in (a, b)}
            | {x for a, b, *_ in bridged for x in (a, b)}
        )
        pieces: list[tuple[int, int, CandidateWindow, _VerifiedRun | None, _LedgerKey, _Bridge | None]] = []
        for lo, hi in zip(boundaries, boundaries[1:], strict=False):
            bridge = next((b for b in bridged if b[0] <= lo and hi <= b[1]), None)
            if bridge is not None:
                hole_key = (bridge[2].kind, bridge[2].source, BRIDGED, str(lo), str(hi))
                pieces.append((lo, hi, bridge[2], None, hole_key, bridge))
                continue
            selected_window = next((w for w in windows if w.start_ms <= lo and hi <= w.end_ms), None)
            if selected_window is None:
                continue
            support = next((entry for entry in verified if entry[0] <= lo and hi <= entry[1]), None)
            key = (
                selected_window.kind,
                selected_window.source,
                support[2].id if support else None,
                str(support[3]) if support else None,
                str(support[6]) if support else None,
            )
            if pieces and pieces[-1][1] == lo and pieces[-1][4] == key:
                pieces[-1] = (pieces[-1][0], hi, selected_window, support, key, None)
            else:
                pieces.append((lo, hi, selected_window, support, key, None))
        for lo, hi, window, support, _, bridge in pieces:
            # A filled hole carries the reference and channel details of the confirmed run before it, but no offset:
            # nothing was compared across the hole itself.
            basis = support or (bridge[3] if bridge else None)
            verdict = "match" if basis else "unknown"
            evidence_reference = basis[2] if basis else None
            piece_offset = support[3] if support else None
            piece_sound = support[6] if support else None
            piece_speed = support[8] if support else None
            if bridge:
                text = _bridged_text(_whose(window, suggested_by[window]), hi - lo)
            else:
                text = WARNING if support else "Content or timing could not be verified; preserve it."
            eid = _id(
                media.id,
                PROFILE_ID,
                PROFILE_FINGERPRINT,
                window.kind,
                lo,
                hi,
                verdict,
                evidence_reference.id if evidence_reference else None,
                str(piece_offset),
                str(piece_sound),
                str(piece_speed),
                bridge is not None,
            )
            metrics = {
                "verification_profile": PROFILE_ID,
                "profile_fingerprint": PROFILE_FINGERPRINT,
                "candidate_source": window.source,
                "checked_audio_stream_ids": [t[0] for t in tracks] if basis else [],
                "checked_channels": basis[7] if basis else {},
                "reference_offset_ms": float(piece_offset) if piece_offset is not None else None,
                "reference_offset_rational_ms": str(piece_offset) if piece_offset is not None else None,
                "sound_offset_ms": float(piece_sound) if piece_sound is not None else None,
                "sound_offset_rational_ms": str(piece_sound) if piece_sound is not None else None,
                "reference_speed": float(piece_speed) if piece_speed is not None else None,
                "reference_speed_rational": str(piece_speed) if piece_speed is not None else None,
                "sound_levels": basis[9] if basis else {},
                "unspecified_interpretation_fields": basis[4] if basis else [],
                "claim": "native-code-signal recurrence, not rendered equivalence",
                "admission_provenance": provenance[basis[2].id] if basis else None,
                "native_anchors": support[5] if support else None,
                "bridged": bridge is not None,
                "bridge_start_ms": lo if bridge else None,
                "bridge_end_ms": hi if bridge else None,
                "bridge_length_ms": hi - lo if bridge else None,
            }
            if window.kind in facts:
                metrics[COMPARISON_KEY] = facts[window.kind]
            evidence.append(
                Evidence(
                    eid,
                    lo,
                    hi,
                    "audiovisual",
                    verdict,
                    "native_signal_" + (BRIDGED if bridge else verdict),
                    text,
                    evidence_reference.id if evidence_reference else None,
                    math.ceil(piece_speed * lo + piece_offset)
                    if piece_speed is not None and piece_offset is not None
                    else None,
                    math.floor(piece_speed * hi + piece_offset)
                    if piece_speed is not None and piece_offset is not None
                    else None,
                    metrics,
                )
            )
            ranges.append(
                DecisionRange(
                    _id("range", eid),
                    window.kind,
                    "skip" if basis else "keep",
                    lo,
                    hi,
                    "proposed",
                    False,
                    "detector",
                    "native_signal_" + (BRIDGED if bridge else verdict),
                    text,
                    (eid,),
                    tuple(t[0] for t in tracks) if basis else (),
                    False,
                )
            )
        for window in suggested:
            suggestion_evidence, suggestion_range = _suggestion(media, window, suggested_by[window], tracks, movie)
            if window.kind in facts:
                suggestion_evidence = replace(
                    suggestion_evidence, metrics={**suggestion_evidence.metrics, COMPARISON_KEY: facts[window.kind]}
                )
            evidence.append(suggestion_evidence)
            ranges.append(suggestion_range)
        if refs or compared:
            if not verified and not fragments:
                reasons.append(_no_repeat_found(len(compared) or len(refs)))
            for window in windows:
                note = _FALLBACK_NOTES.get(window.source)
                if note and not any(a < window.end_ms and b > window.start_ms for a, b, *_ in verified):
                    reasons.append(note)
        for window in windows:
            lengths = [hi - lo for lo, hi, w, *_ in bridged if w == window]
            if lengths:
                reasons.append(_bridges_note(_whose(window, suggested_by[window]), lengths))
        for window in suggested:
            if movie:
                why = FILM_NOT_CONFIRMED
            elif window.end_ms - window.start_ms > MAX_CANDIDATE_MS:
                why = TOO_LONG_TO_COMPARE
            elif window in fragments:
                why = _confirmed_only(fragments[window])
            else:
                why = NOT_CONFIRMED
            reasons.append(_suggestion_note(window, suggested_by[window], why))
        for kind, found in facts.items():
            kind_ranges = [r for r in ranges if r.kind == kind]
            if not any(r.action == "skip" and r.origin == "detector" for r in kind_ranges) and is_one_of_a_kind(
                found, sum(w.end_ms - w.start_ms for w in suggested if w.kind == kind)
            ):
                reasons.append(_one_of_a_kind_note(kind, found))
        reasons.extend(hint_notes)
        reasons.extend(online_notes)
        if comparison_failure is not None:
            reasons.extend(comparison_failure[1:])
        reasons.extend(ignored)
        if progress_callback:
            progress_callback(100, "Analysis complete")
        evidence.sort(key=lambda e: (e.start_ms, e.end_ms, e.id))
        ranges.sort(key=lambda r: (r.start_ms, r.end_ms, r.id))
        return AnalysisDraft(tuple(evidence), tuple(ranges), "complete", tuple(dict.fromkeys(reasons)))
    except decoder.Cancelled as exc:
        return AnalysisDraft((), (), "cancelled", (str(exc),))
    except (decoder.DecodeError, OSError, ValueError, KeyError, TypeError) as exc:
        if isinstance(exc, decoder.DecodeError):
            return AnalysisDraft((), (), "failed", (UNCOMPARABLE_MEDIA, str(exc)))
        return AnalysisDraft((), (), "failed", (UNREADABLE_MEDIA,))


def analyze(
    media: MediaVersion,
    candidates: Sequence[CandidateWindow],
    references: Sequence[MediaVersion],
    *,
    store,
    cancel_check,
    pause_check,
    progress_callback,
    ffmpeg_threads,
    ffmpeg_path=None,
    owner_keeps: Sequence[tuple[int, int]] = (),
    online: Mapping[str, Sequence[OnlineWindow]] | None = None,
    movie: bool = False,
) -> AnalysisDraft:
    """Compare the episode with the references and propose what repeats; see ``_analyze``.

    When comparing fails outright (a format that cannot be compared exactly, an unreadable stream), the suggested
    windows (Plex's own markers, the times online databases give) do not depend on it: they are still offered as
    suggestions, with the failure as the reason nothing was confirmed. With none of them, or if the file itself has
    changed, the analysis fails as it always did.
    """
    options = {
        "store": store,
        "cancel_check": cancel_check,
        "pause_check": pause_check,
        "progress_callback": progress_callback,
        "ffmpeg_threads": ffmpeg_threads,
        "ffmpeg_path": ffmpeg_path,
        "owner_keeps": owner_keeps,
        "online": online,
        "movie": movie,
    }
    with acceleration.compare_lease():
        draft = _analyze(media, candidates, references, **options)
        if draft.status != "failed" or not (
            any(window.source == PLEX_SOURCE for window in candidates) or (online or {}).get(media.id)
        ):
            return draft
        headline = COMPARISON_FAILED if draft.reasons[:1] == (UNCOMPARABLE_MEDIA,) else COMPARISON_UNREADABLE
        retry = _analyze(media, candidates, [], comparison_failure=(headline, *draft.reasons[1:]), **options)
        return retry if retry.status in ("complete", "cancelled") else draft
