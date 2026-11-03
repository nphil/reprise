"""Review-only codec-tolerant recurrence under matching native declarations.

Unknown metadata is not guessed. Qualified native-code-signal matches are not
certification of rendered appearance or of arbitrarily small semantic changes.

The picture and the sound of two files are never assumed to share one offset: the
picture votes propose an offset, the sound is searched for its own within a bounded
window around it, and each is then verified strictly at its own offset.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from dataclasses import replace
from fractions import Fraction

from ..gpu.ffmpeg_capabilities import ffmpeg_binary
from . import media as decoder
from . import recurrence as verify
from .admission import admit_references
from .models import (
    ORIGIN_PLEX,
    REASON_PLEX_SUGGESTION,
    AnalysisDraft,
    CandidateWindow,
    DecisionRange,
    Evidence,
    MediaVersion,
)
from .verification_profile import PROFILE_FINGERPRINT, PROFILE_ID, PROFILE_SPEC

# A verified run: target start and end, the reference, its picture offset, the unspecified colour fields, the native
# anchors, the sound offset (both offsets are reference time minus target time, in ms) and the channels compared.
_VerifiedRun = tuple[int, int, MediaVersion, Fraction, list[str], dict, Fraction, dict]
_LedgerKey = tuple[str, str, str | None, str | None, str | None]

MAX_CANDIDATE_MS = PROFILE_SPEC["candidate"]["max_ms"]
REFERENCE_RADIUS_MS = PROFILE_SPEC["candidate"]["reference_radius_ms"]
CONTEXT_MS = PROFILE_SPEC["candidate"]["context_ms"]
SOUND_SEARCH_MS = PROFILE_SPEC["sound_offset"]["search_ms"]
SOUND_PROBE_MS = PROFILE_SPEC["sound_offset"]["probe_ms"]
SOUND_BLOCK_MS = PROFILE_SPEC["sound_offset"]["block_ms"]
SOUND_CORRELATION = PROFILE_SPEC["sound_offset"]["correlation"]
LFE_CHANNELS = frozenset(PROFILE_SPEC["audio"]["lfe_channels"])
WARNING = (
    "Codec-tolerant native-code-signal recurrence under matching declarations; "
    "not certified rendered equivalence. Unspecified interpretation and sufficiently small semantic differences are not established."
)

# The notes below are stored with the result and shown to the owner when nothing could be proposed.
# Plain sentences, one idea each; technical detail never goes in the first line.
FALLBACK_WINDOW_MS = 90000  # how much of the file's start / end is checked when Plex has no marker for it
OPENING_WINDOW = "opening-window"  # candidate source for the start of the file when Plex has no intro marker
ENDING_WINDOW = "ending-window"  # candidate source for the end of the file when Plex has no credits marker
NO_REFERENCES = "There are no other episodes of this season to compare with, so nothing could be confirmed."
NO_USABLE_REFERENCES = "None of the other episodes of this season could be used for the comparison."
NO_OTHER_EPISODES = "This is the only episode of its season in the library, so there is nothing to compare it with."
OTHER_EPISODES_NOT_READY = "Other episodes of this season are in the library but none are ready to compare yet. Refresh the library and try again."
REFERENCE_PICTURE_DIFFERS = (
    "Another episode of this season has a different picture format, so it can't be compared exactly."
)
REFERENCE_AUDIO_DIFFERS = (
    "Another episode of this season has a different audio format, so it can't be compared exactly."
)
REFERENCE_UNREADABLE = "Another episode of this season could not be read."
COMPARISON_UNAVAILABLE = "A comparison with another episode could not be completed."
COMPARISON_MISALIGNED = (
    "A comparison with another episode could not be completed because the sound doesn't line up exactly."
)
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


PLEX_SOURCE = "plex"  # CandidateWindow.source of a marker Plex itself detected for this exact file
PLEX_END_SLACK_MS = 1000  # Plex rounds: a marker may end up to a second after its file does, and then ends with it
PLEX_SUGGESTION_TEXT = "Suggested by Plex — not confirmed by Reprise."
PLEX_EVIDENCE_TEXT = "Plex has a marker here, but comparing episodes did not confirm it."
NOT_CONFIRMED = "Reprise could not confirm it by comparing episodes."
TOO_LONG_TO_COMPARE = f"It is longer than the {MAX_CANDIDATE_MS // 60000} minutes Reprise can compare."
PLEX_CONFIRMED_SHARE = Fraction(PROFILE_SPEC["candidate"]["plex_confirmed_share"])


def _confirmed_only(covered_ms):
    seconds = max(1, round(covered_ms / 1000))
    span = "1 second" if seconds == 1 else f"{seconds} seconds"
    return f"Comparing episodes confirmed only {span} of it, less than half, so that part is not proposed on its own."


def _clock(ms):
    hours, rest = divmod(ms // 1000, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


def _plex_note(window, why):
    span = f"from {_clock(window.start_ms)} to {_clock(window.end_ms)}"
    return f"Plex suggested the {window.kind} ({span}). {why} It stays a suggestion until you approve it."


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


def _plex_suggestion(media, window, tracks):
    """Plex's marker as a proposal: evidence that nothing confirmed it, and a skip only the owner can make eligible."""
    eid = _id(media.id, PROFILE_ID, PROFILE_FINGERPRINT, window.kind, window.start_ms, window.end_ms, "plex")
    metrics = {
        "verification_profile": PROFILE_ID,
        "profile_fingerprint": PROFILE_FINGERPRINT,
        "candidate_source": window.source,
        "checked_audio_stream_ids": [],
        "checked_channels": {},
        "reference_offset_ms": None,
        "reference_offset_rational_ms": None,
        "sound_offset_ms": None,
        "sound_offset_rational_ms": None,
        "unspecified_interpretation_fields": [],
        "claim": "Plex's own marker; not confirmed by comparing episodes",
        "admission_provenance": None,
        "native_anchors": None,
    }
    evidence = Evidence(
        eid,
        window.start_ms,
        window.end_ms,
        "audiovisual",
        "unknown",
        "plex_marker_unconfirmed",
        PLEX_EVIDENCE_TEXT,
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
        ORIGIN_PLEX,
        REASON_PLEX_SUGGESTION,
        PLEX_SUGGESTION_TEXT,
        (eid,),
        tuple(t[0] for t in tracks),
        False,
    )
    return evidence, suggestion


class _AlignmentUnsupported(decoder.DecodeError):
    """Cleanly decoded native grids cannot support this proposed offset."""


def _id(*parts):
    return hashlib.sha256(json.dumps(parts, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:32]


def _ratio(value):
    return Fraction(str(value).replace(":", "/"))


def _representation(item, stream, cancel, pause, *, ffmpeg_path=None):
    """Stream declarations supplement, never replace, the same-decode frame audit."""
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
    if video.get("side_data_list"):
        raise decoder.DecodeError("Video interpretation side data is unsupported")
    if video.get("color_transfer") in ("smpte2084", "arib-std-b67"):
        raise decoder.DecodeError("HDR interpretation is unsupported")
    try:
        result = (
            video["codec_name"],
            video.get("profile", "unknown"),
            video["width"],
            video["height"],
            video["pix_fmt"],
            str(_ratio(video["sample_aspect_ratio"])),
            str(_ratio(video["r_frame_rate"])),
            *(verify.declaration(video.get(k), k) for k in verify.COLOR_FIELDS),
        )
        if _ratio(video["sample_aspect_ratio"]) <= 0 or _ratio(video["r_frame_rate"]) <= 0:
            raise ValueError("Nonpositive geometry or cadence")
    except (KeyError, ValueError, ZeroDivisionError) as exc:
        raise decoder.DecodeError("Missing native representation declarations") from exc
    decoder.check_current(item)
    return result


def _scan(item, stream, start, end, cancel, pause, threads, *, ffmpeg_path=None):
    """Keep only hints; accept them after complete native decoder exit."""
    hints, declarations, unspecified = [], None, []
    with decoder.native_frames(item, stream, start, end, cancel, pause, threads, ffmpeg_path=ffmpeg_path) as frames:
        for frame in frames:
            if cancel():
                raise decoder.Cancelled("Analysis cancelled")
            if frame.interpretation_flags:
                raise decoder.DecodeError("Unsupported native interpretation")
            signature = verify.frame_declarations(frame)
            if declarations is None:
                declarations = signature
                unspecified = [
                    k for k in verify.COLOR_FIELDS if verify.declaration(getattr(frame, k), k) == "unspecified"
                ]
            elif signature != declarations:
                raise decoder.DecodeError("Native declarations changed within comparison window")
            if len(hints) >= PROFILE_SPEC["candidate"]["maximum_scan_frames"]:
                raise decoder.DecodeError("Native comparison frame budget exceeded")
            if frame.pts_ms < end:
                hints.append((frame.pts_ms, verify.descriptor(frame), verify.spatial_information(frame)))
    decoder.check_current(item)
    return hints, declarations, unspecified


def _video_support(
    target, reference, video, ref_video, start, end, offset, cancel, pause, threads, *, ffmpeg_path=None
):
    """Stage locally; either decoder's final failure invalidates all support."""
    points = []
    with decoder.native_frames(
        target, video, math.floor(start), math.ceil(end), cancel, pause, threads, ffmpeg_path=ffmpeg_path
    ) as left:
        with decoder.native_frames(
            reference,
            ref_video,
            math.floor(start + offset),
            math.ceil(end + offset),
            cancel,
            pause,
            threads,
            ffmpeg_path=ffmpeg_path,
        ) as right:
            # Container timestamps are whole ticks (a Matroska file's are milliseconds), so two files' frames of the
            # same instant may sit one tick apart, and the picture offset, itself a difference of two of them, is
            # rounded the same way: real 24000/1001 fps pairs land 0 or 1 ms off. Pairing therefore tolerates one
            # tick of the coarser stream when that is wider than the profile's floor; frames are never that close.
            tolerance = max(
                Fraction(PROFILE_SPEC["video"]["pair_tolerance_ms"]), left.time_base * 1000, right.time_base * 1000
            )
            a, b = next(left, None), next(right, None)
            reference_index = 0
            declarations = None
            while a is not None:
                signature = verify.frame_declarations(a)
                if declarations is not None and signature != declarations:
                    raise decoder.DecodeError("Native declarations changed during verification")
                declarations = signature
                wanted = a.pts_ms + offset
                while b is not None and b.pts_ms < wanted - tolerance:
                    b = next(right, None)
                    reference_index += 1
                matched = b is not None and abs(b.pts_ms - wanted) <= tolerance
                passed = matched and verify.video_matches(a, b)
                points.append((a.pts_ms, b.pts_ms if matched else None, passed, reference_index if matched else None))
                a = next(left, None)
            while b is not None:
                b = next(right, None)
    decoder.check_current(target)
    decoder.check_current(reference)
    safe, bad = [], []
    for a, b in zip(points, points[1:], strict=False):
        lo, hi = max(start, a[0]), min(end, b[0])
        if lo >= hi:
            continue
        continuous = (
            0 < b[0] - a[0] <= verify.MAX_FRAME_GAP_MS
            and a[1] is not None
            and b[1] is not None
            and b[3] == a[3] + 1
            and abs((b[1] - a[1]) - (b[0] - a[0])) <= tolerance
        )
        if a[2] and b[2] and continuous:
            # The pair's support is the target frame's span; the reference frame's span agrees with it within the
            # pairing tolerance (continuity above), and clipping to their intersection would leave tick-rounding
            # holes of a millisecond between consecutive pairs that split a run into pieces too short to anchor.
            safe.append((lo, hi))
        else:
            bad.append((lo - verify.CHANGE_GUARD_MS, hi + verify.CHANGE_GUARD_MS))
    return verify.subtract(safe, bad)


def _checked_channels(channel_ids):
    """The native channels the strict identity compares: every one except the low-frequency effects channel when
    there are others. The LFE carries no speech, is band-limited and is optional in reproduction (ITU-R BS.775), and
    two encodes of one mix can disagree in it alone (real 5.1 AC-3 rips do), so a difference only there never vetoes."""
    main = [i for i, name in enumerate(channel_ids) if name not in LFE_CHANNELS]
    return main if main else list(range(len(channel_ids)))


def _audio_support(
    target, reference, tracks, ref_tracks, start, end, offset, cancel, pause, threads, *, ffmpeg_path=None
):
    """The target times where every mapped track's main channels are the reference's at ``offset``, and which
    channels of each track that rests on."""
    safe = [(start, end)]
    checked = {}
    if not tracks:
        raise decoder.DecodeError("No mapped native audio tracks")
    for track, ref_track in zip(tracks, ref_tracks, strict=True):
        rate = track[2]
        if rate <= 0 or rate % 100:
            raise decoder.DecodeError("Native audio window does not fit sample grid")
        chunk_samples = rate * PROFILE_SPEC["audio"]["chunk_seconds"]
        verifier = None
        expected = None
        audio_format = None
        with decoder.native_audio_chunks(
            target, track[1], start, end, cancel, pause, threads, chunk_samples=chunk_samples, ffmpeg_path=ffmpeg_path
        ) as left:
            with decoder.native_audio_chunks(
                reference,
                ref_track[1],
                start + offset,
                end + offset,
                cancel,
                pause,
                threads,
                chunk_samples=chunk_samples,
                ffmpeg_path=ffmpeg_path,
            ) as right:
                for a, b in zip(left, right, strict=True):
                    if cancel():
                        raise decoder.Cancelled("Analysis cancelled")
                    current = (a.sample_rate, a.channel_layout, a.channel_ids)
                    if (
                        current != (b.sample_rate, b.channel_layout, b.channel_ids)
                        or a.sample_rate != rate
                        or (audio_format is not None and current != audio_format)
                    ):
                        raise decoder.DecodeError("Native audio rate or channel layout differs")
                    audio_format = current
                    if (
                        a.support_start_ms + offset != b.support_start_ms
                        or a.support_end_ms + offset != b.support_end_ms
                    ):
                        raise _AlignmentUnsupported("Native audio sample grids do not align")
                    if expected is not None and a.support_start_ms != expected:
                        raise decoder.DecodeError("Native audio chunks are not contiguous")
                    if a.support_end_ms - a.support_start_ms != Fraction(len(a.data) * 1000, rate):
                        raise decoder.DecodeError("Native audio sample support mismatch")
                    if verifier is None:
                        verifier = verify.AudioVerifier(rate, a.support_start_ms)
                        columns = _checked_channels(a.channel_ids)
                        checked[track[0]] = [a.channel_ids[i] for i in columns]
                    verifier.feed(a.data[:, columns], b.data[:, columns])
                    expected = a.support_end_ms
        # Final declaration scope exists only AFTER both fully exhausted contexts
        # exit successfully. Never publish provisional chunks or early matches.
        if left.declarations is None or right.declarations is None or left.declarations != right.declarations:
            raise decoder.DecodeError("Native audio declarations differ")
        decoder.check_current(target)
        decoder.check_current(reference)
        safe = verify.intersect(safe, verifier.finish() if verifier is not None else [])
    return safe, checked


def _sound_offset(
    target, reference, track, ref_track, hints, rh, lo, hi, offset, cancel, pause, threads, *, ffmpeg_path=None
):
    """The sound offset to try for one picture proposal, or None when no sound within the search window lines up
    well enough: a whole-sample lag chosen by cross-correlating a bounded probe of the first track (see
    ``recurrence.sound_lag``). Only the strict sound identity at that offset is support."""
    search, half = Fraction(SOUND_SEARCH_MS), Fraction(SOUND_PROBE_MS, 2)
    # The probe keeps away from the reference's ends so the whole search window exists there, when that leaves enough.
    start, end = max(lo, search - offset), min(hi, reference.duration_ms - offset - search)
    if end - start < verify.MIN_RUN_MS:
        start, end = lo, hi
    agreeing = [t for t, _ in verify.agreeing_frames(hints, rh, start, end, offset)]
    centre = agreeing[len(agreeing) // 2] if agreeing else (start + end) / 2
    probe_start = max(start, min(centre - half, end - 2 * half))
    probe_end = min(end, probe_start + 2 * half)
    rate = track[2]
    left = decoder.native_audio_pcm(
        target, track[1], probe_start, probe_end, cancel, pause, threads, ffmpeg_path=ffmpeg_path
    )
    right = decoder.native_audio_pcm(
        reference,
        ref_track[1],
        max(Fraction(0), probe_start + offset - search),
        min(Fraction(reference.duration_ms), probe_end + offset + search),
        cancel,
        pause,
        threads,
        ffmpeg_path=ffmpeg_path,
    )
    if (left.sample_rate, left.channel_layout, left.channel_ids) != (
        right.sample_rate,
        right.channel_layout,
        right.channel_ids,
    ) or left.sample_rate != rate:
        raise decoder.DecodeError("Native audio rate or channel layout differs")
    if len(right.data) < len(left.data):
        return None
    base = right.support_start_ms - left.support_start_ms  # the sound offset at lag zero, on the sample grid
    found = verify.sound_lag(left.data, right.data, round((offset - base) * rate / 1000), rate * SOUND_BLOCK_MS // 1000)
    if found is None or found[1] < SOUND_CORRELATION:
        return None
    return base + Fraction(found[0] * 1000, rate)


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
    comparison_failure=None,
) -> AnalysisDraft:
    """One analysis pass. ``comparison_failure`` is what an earlier pass failed with (a headline, then details): it
    turns comparing off, so only Plex's markers are offered and that failure is why nothing was confirmed."""
    cancel = cancel_check or (lambda: False)
    pause = pause_check or (lambda: False)
    threads = 1
    ffmpeg_path = ffmpeg_path or ffmpeg_binary()
    reasons, verified = [], []
    windows, ignored = _fit_windows(
        sorted(set(candidates), key=lambda w: (w.start_ms, w.end_ms, w.kind, w.source)), media.duration_ms
    )
    try:
        if cancel():
            raise decoder.Cancelled("Analysis cancelled")
        if comparison_failure is None:
            video, tracks, _ = decoder.probe(media, cancel, pause, ffmpeg_path=ffmpeg_path)
            representation = _representation(media, video, cancel, pause, ffmpeg_path=ffmpeg_path)
        else:
            video = representation = None
            tracks = _signature_tracks(media)
        refs = []
        provenance = {}
        admitted = admit_references(media, references, store)
        for ref, snapshot in admitted:
            provenance[ref.id] = snapshot
            try:
                rv, rt, _ = decoder.probe(ref, cancel, pause, ffmpeg_path=ffmpeg_path)
                if representation != _representation(ref, rv, cancel, pause, ffmpeg_path=ffmpeg_path):
                    reasons.append(REFERENCE_PICTURE_DIFFERS)
                elif len(rt) != len(tracks) or any(a[2:] != b[2:] for a, b in zip(tracks, rt, strict=True)):
                    reasons.append(REFERENCE_AUDIO_DIFFERS)
                else:
                    refs.append((ref, rv, rt))
            except decoder.Cancelled:
                raise
            except (decoder.DecodeError, OSError, ValueError, KeyError, TypeError):
                reasons.append(REFERENCE_UNREADABLE)
        if comparison_failure is not None:
            reasons.append(comparison_failure[0])
        elif not admitted:
            reasons.append(NO_REFERENCES)
        elif not refs:
            reasons.append(NO_USABLE_REFERENCES)
        for number, window in enumerate(windows):
            # A Plex marker too long to compare is not an error: it cannot be confirmed, so it is only a suggestion.
            too_long = window.end_ms - window.start_ms > MAX_CANDIDATE_MS
            if (
                window.kind not in ("intro", "credits", "recap", "preview")
                or not (0 <= window.start_ms < window.end_ms <= media.duration_ms)
                or (too_long and window.source != PLEX_SOURCE)
            ):
                raise decoder.DecodeError("Invalid or oversized candidate window")
            if not refs or too_long:
                continue
            if progress_callback:
                progress_callback(int(100 * number / max(1, len(windows))), "Verifying native audiovisual recurrence")
            ts, te = max(0, window.start_ms - CONTEXT_MS), min(media.duration_ms, window.end_ms + CONTEXT_MS)
            try:
                hints, declarations, unspecified = _scan(
                    media, video, ts, te, cancel, pause, threads, ffmpeg_path=ffmpeg_path
                )
            except decoder.Cancelled:
                raise
            except decoder.DecodeError:
                raise
            for position, (ref, rv, rt) in enumerate(refs):
                if progress_callback:
                    done = (number + position / len(refs)) / len(windows)
                    progress_callback(
                        int(100 * done),
                        f"Checking the {window.kind}: comparing with episode {position + 1} of {len(refs)}",
                    )
                rs, re = max(0, ts - REFERENCE_RADIUS_MS), min(ref.duration_ms, te + REFERENCE_RADIUS_MS)
                if rs >= re:
                    continue
                try:
                    rh, r_decl, _ = _scan(ref, rv, rs, re, cancel, pause, threads, ffmpeg_path=ffmpeg_path)
                    if declarations != r_decl:
                        raise decoder.DecodeError("Native frame declarations differ")
                    if not hints or not rh:
                        continue
                    proposals = verify.perceptual_shifts(hints, rh)
                    for offset in proposals:
                        lo = max(Fraction(ts), -offset, hints[0][0], rh[0][0] - offset)
                        hi = min(Fraction(te), ref.duration_ms - offset, hints[-1][0], rh[-1][0] - offset)
                        if hi - lo < verify.MIN_RUN_MS:
                            continue
                        try:
                            sound = _sound_offset(
                                media,
                                ref,
                                tracks[0],
                                rt[0],
                                hints,
                                rh,
                                lo,
                                hi,
                                offset,
                                cancel,
                                pause,
                                threads,
                                ffmpeg_path=ffmpeg_path,
                            )
                            if sound is None:
                                continue
                            # The sound is checked over what the reference holds at the sound offset; the picture at
                            # the picture offset, and only where the sound already matched, since it is the slow half
                            # (minutes per offset) and a proposal whose sound does not match can never be supported.
                            slo, shi = max(lo, -sound), min(hi, ref.duration_ms - sound)
                            audio, checked = (
                                _audio_support(
                                    media,
                                    ref,
                                    tracks,
                                    rt,
                                    slo,
                                    shi,
                                    sound,
                                    cancel,
                                    pause,
                                    threads,
                                    ffmpeg_path=ffmpeg_path,
                                )
                                if shi - slo >= verify.MIN_RUN_MS
                                else ([], {})
                            )
                            picture = []
                            if audio:
                                plo, phi = _picture_span(hints, lo, hi, audio)
                                picture = _video_support(
                                    media,
                                    ref,
                                    video,
                                    rv,
                                    plo,
                                    phi,
                                    offset,
                                    cancel,
                                    pause,
                                    threads,
                                    ffmpeg_path=ffmpeg_path,
                                )
                        except decoder.Cancelled:
                            raise
                        except _AlignmentUnsupported:
                            reasons.append(COMPARISON_MISALIGNED)
                            continue
                        matches = verify.intersect(audio, picture)
                        for a, b in matches:
                            anchored = verify.anchor(hints, rh, max(a, window.start_ms), min(b, window.end_ms), offset)
                            if anchored is not None:
                                a, b, anchors = anchored
                                verified.append((a, b, ref, offset, unspecified, anchors, sound, checked))
                    decoder.check_current(ref)
                except decoder.Cancelled:
                    raise
                except decoder.DecodeError:
                    raise
                except (OSError, ValueError, KeyError, TypeError):
                    reasons.append(COMPARISON_UNAVAILABLE)
        decoder.check_current(media)
        selected = []
        last_target = -1
        last_references: dict[str, Fraction] = {}
        for entry in sorted(verified, key=lambda row: (row[0], -(row[1] - row[0]))):
            lo, hi, ref, offset, *_ = entry
            decoder.check_current(ref)
            if lo < last_target or lo + offset < last_references.get(ref.id, Fraction(-1)):
                continue
            selected.append(entry)
            last_target = hi
            last_references[ref.id] = hi + offset
        verified = selected
        # A Plex marker is the comparison's to propose only when at least half of it was confirmed; otherwise it is
        # not a keep but a suggestion that skips nothing until the owner approves it, and the smaller confirmed
        # fragments inside it are dropped (a one-second skip in the middle of an intro helps nobody) and noted.
        suggested, fragments = [], {}
        for w in windows:
            if w.source != PLEX_SOURCE:
                continue
            covered = sum(max(0, min(b, w.end_ms) - max(a, w.start_ms)) for a, b, *_ in verified)
            if Fraction(covered, w.end_ms - w.start_ms) < PLEX_CONFIRMED_SHARE:
                suggested.append(w)
                if covered:
                    fragments[w] = covered
        windows = [w for w in windows if w not in suggested]
        verified = [run for run in verified if not any(run[0] < w.end_ms and run[1] > w.start_ms for w in suggested)]
        # Unknown remains a keep; evidence never bridges separately anchored holes.
        ranges, evidence = [], []
        boundaries = sorted(
            {x for w in windows for x in (w.start_ms, w.end_ms)} | {x for a, b, *_ in verified for x in (a, b)}
        )
        pieces: list[tuple[int, int, CandidateWindow, _VerifiedRun | None, _LedgerKey]] = []
        for lo, hi in zip(boundaries, boundaries[1:], strict=False):
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
                pieces[-1] = (pieces[-1][0], hi, selected_window, support, key)
            else:
                pieces.append((lo, hi, selected_window, support, key))
        for lo, hi, window, support, _ in pieces:
            verdict = "match" if support else "unknown"
            evidence_reference = support[2] if support else None
            offset = support[3] if support else None
            sound = support[6] if support else None
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
                str(offset),
                str(sound),
            )
            metrics = {
                "verification_profile": PROFILE_ID,
                "profile_fingerprint": PROFILE_FINGERPRINT,
                "candidate_source": window.source,
                "checked_audio_stream_ids": [t[0] for t in tracks] if support else [],
                "checked_channels": support[7] if support else {},
                "reference_offset_ms": float(offset) if offset is not None else None,
                "reference_offset_rational_ms": str(offset) if offset is not None else None,
                "sound_offset_ms": float(sound) if sound is not None else None,
                "sound_offset_rational_ms": str(sound) if sound is not None else None,
                "unspecified_interpretation_fields": support[4] if support else [],
                "claim": "native-code-signal recurrence, not rendered equivalence",
                "admission_provenance": provenance[support[2].id] if support else None,
                "native_anchors": support[5] if support else None,
            }
            evidence.append(
                Evidence(
                    eid,
                    lo,
                    hi,
                    "audiovisual",
                    verdict,
                    "native_signal_" + verdict,
                    text,
                    evidence_reference.id if evidence_reference else None,
                    math.ceil(lo + offset) if offset is not None else None,
                    math.floor(hi + offset) if offset is not None else None,
                    metrics,
                )
            )
            ranges.append(
                DecisionRange(
                    _id("range", eid),
                    window.kind,
                    "skip" if support else "keep",
                    lo,
                    hi,
                    "proposed",
                    False,
                    "detector",
                    "native_signal_" + verdict,
                    text,
                    (eid,),
                    tuple(t[0] for t in tracks) if support else (),
                    False,
                )
            )
        for window in suggested:
            plex_evidence, plex_range = _plex_suggestion(media, window, tracks)
            evidence.append(plex_evidence)
            ranges.append(plex_range)
        if refs:
            if not verified and not fragments:
                reasons.append(_no_repeat_found(len(refs)))
            for window in windows:
                note = _FALLBACK_NOTES.get(window.source)
                if note and not any(a < window.end_ms and b > window.start_ms for a, b, *_ in verified):
                    reasons.append(note)
        for window in suggested:
            if window.end_ms - window.start_ms > MAX_CANDIDATE_MS:
                why = TOO_LONG_TO_COMPARE
            elif window in fragments:
                why = _confirmed_only(fragments[window])
            else:
                why = NOT_CONFIRMED
            reasons.append(_plex_note(window, why))
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
) -> AnalysisDraft:
    """Compare the episode with the references and propose what repeats; see ``_analyze``.

    When comparing fails outright (a format that cannot be compared exactly, an unreadable stream), Plex's own markers
    do not depend on it: they are still offered as suggestions, with the failure as the reason nothing was confirmed.
    With no Plex marker, or if the file itself has changed, the analysis fails as it always did.
    """
    options = {
        "store": store,
        "cancel_check": cancel_check,
        "pause_check": pause_check,
        "progress_callback": progress_callback,
        "ffmpeg_threads": ffmpeg_threads,
        "ffmpeg_path": ffmpeg_path,
    }
    draft = _analyze(media, candidates, references, **options)
    if draft.status != "failed" or not any(window.source == PLEX_SOURCE for window in candidates):
        return draft
    headline = COMPARISON_FAILED if draft.reasons[:1] == (UNCOMPARABLE_MEDIA,) else COMPARISON_UNREADABLE
    retry = _analyze(media, candidates, [], comparison_failure=(headline, *draft.reasons[1:]), **options)
    return retry if retry.status in ("complete", "cancelled") else draft
