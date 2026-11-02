"""Review-only codec-tolerant recurrence under matching native declarations.

Unknown metadata is not guessed. Qualified native-code-signal matches are not
certification of rendered appearance or of arbitrarily small semantic changes.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from fractions import Fraction

from ..gpu.ffmpeg_capabilities import ffmpeg_binary
from . import media as decoder
from . import recurrence as verify
from .admission import admit_references
from .models import AnalysisDraft, CandidateWindow, DecisionRange, Evidence, MediaVersion
from .verification_profile import PROFILE_FINGERPRINT, PROFILE_ID, PROFILE_SPEC

_VerifiedRun = tuple[int, int, MediaVersion, Fraction, list[str], dict]
_LedgerKey = tuple[str, str, str | None, str | None]

MAX_CANDIDATE_MS = PROFILE_SPEC["candidate"]["max_ms"]
REFERENCE_RADIUS_MS = PROFILE_SPEC["candidate"]["reference_radius_ms"]
CONTEXT_MS = PROFILE_SPEC["candidate"]["context_ms"]
WARNING = (
    "Codec-tolerant native-code-signal recurrence under matching declarations; "
    "not certified rendered equivalence. Unspecified interpretation and sufficiently small semantic differences are not established."
)


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
    tolerance = Fraction(PROFILE_SPEC["video"]["pair_tolerance_ms"])
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
            safe.append((max(lo, a[1] - offset), min(hi, b[1] - offset)))
        else:
            bad.append((lo - verify.CHANGE_GUARD_MS, hi + verify.CHANGE_GUARD_MS))
    return verify.subtract(safe, bad)


def _audio_support(
    target, reference, tracks, ref_tracks, start, end, offset, cancel, pause, threads, *, ffmpeg_path=None
):
    safe = [(start, end)]
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
                    verifier.feed(a.data, b.data)
                    expected = a.support_end_ms
        # Final declaration scope exists only AFTER both fully exhausted contexts
        # exit successfully. Never publish provisional chunks or early matches.
        if left.declarations is None or right.declarations is None or left.declarations != right.declarations:
            raise decoder.DecodeError("Native audio declarations differ")
        decoder.check_current(target)
        decoder.check_current(reference)
        safe = verify.intersect(safe, verifier.finish() if verifier is not None else [])
    return safe


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
    cancel = cancel_check or (lambda: False)
    pause = pause_check or (lambda: False)
    threads = 1
    ffmpeg_path = ffmpeg_path or ffmpeg_binary()
    reasons, verified = [], []
    windows = sorted(set(candidates), key=lambda w: (w.start_ms, w.end_ms, w.kind, w.source))
    try:
        if cancel():
            raise decoder.Cancelled("Analysis cancelled")
        video, tracks, _ = decoder.probe(media, cancel, pause, ffmpeg_path=ffmpeg_path)
        representation = _representation(media, video, cancel, pause, ffmpeg_path=ffmpeg_path)
        refs = []
        provenance = {}
        for ref, snapshot in admit_references(media, references, store):
            provenance[ref.id] = snapshot
            try:
                rv, rt, _ = decoder.probe(ref, cancel, pause, ffmpeg_path=ffmpeg_path)
                if representation != _representation(ref, rv, cancel, pause, ffmpeg_path=ffmpeg_path):
                    reasons.append("Reference unsupported: native representation declarations differ")
                elif len(rt) != len(tracks) or any(a[2:] != b[2:] for a, b in zip(tracks, rt, strict=True)):
                    reasons.append("Reference unsupported: native audio format differs")
                else:
                    refs.append((ref, rv, rt))
            except decoder.Cancelled:
                raise
            except (decoder.DecodeError, OSError, ValueError, KeyError, TypeError) as exc:
                reasons.append("Reference unavailable: " + str(exc))
        for number, window in enumerate(windows):
            if (
                window.kind not in ("intro", "credits", "recap", "preview")
                or not (0 <= window.start_ms < window.end_ms <= media.duration_ms)
                or window.end_ms - window.start_ms > MAX_CANDIDATE_MS
            ):
                raise decoder.DecodeError("Invalid or oversized candidate window")
            if not refs:
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
            for ref, rv, rt in refs:
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
                            audio = _audio_support(
                                media, ref, tracks, rt, lo, hi, offset, cancel, pause, threads, ffmpeg_path=ffmpeg_path
                            )
                            picture = _video_support(
                                media, ref, video, rv, lo, hi, offset, cancel, pause, threads, ffmpeg_path=ffmpeg_path
                            )
                        except decoder.Cancelled:
                            raise
                        except _AlignmentUnsupported as exc:
                            reasons.append("Native offset comparison unavailable: " + str(exc))
                            continue
                        matches = verify.intersect(audio, picture)
                        for a, b in matches:
                            anchored = verify.anchor(hints, rh, max(a, window.start_ms), min(b, window.end_ms), offset)
                            if anchored is not None:
                                a, b, anchors = anchored
                                verified.append((a, b, ref, offset, unspecified, anchors))
                    decoder.check_current(ref)
                except decoder.Cancelled:
                    raise
                except decoder.DecodeError:
                    raise
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    reasons.append("Native comparison unavailable: " + str(exc))
        decoder.check_current(media)
        selected = []
        last_target = -1
        last_references = {}
        for entry in sorted(verified, key=lambda row: (row[0], -(row[1] - row[0]))):
            lo, hi, ref, offset, _, _ = entry
            decoder.check_current(ref)
            if lo < last_target or lo + offset < last_references.get(ref.id, Fraction(-1)):
                continue
            selected.append(entry)
            last_target = hi
            last_references[ref.id] = hi + offset
        verified = selected
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
            )
            if pieces and pieces[-1][1] == lo and pieces[-1][4] == key:
                pieces[-1] = (pieces[-1][0], hi, selected_window, support, key)
            else:
                pieces.append((lo, hi, selected_window, support, key))
        for lo, hi, window, support, _ in pieces:
            verdict = "match" if support else "unknown"
            evidence_reference = support[2] if support else None
            offset = support[3] if support else None
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
            )
            metrics = {
                "verification_profile": PROFILE_ID,
                "profile_fingerprint": PROFILE_FINGERPRINT,
                "candidate_source": window.source,
                "checked_audio_stream_ids": [t[0] for t in tracks] if support else [],
                "reference_offset_ms": float(offset) if offset is not None else None,
                "reference_offset_rational_ms": str(offset) if offset is not None else None,
                "unspecified_interpretation_fields": support[4] if support else [],
                "claim": "native-code-signal recurrence, not rendered equivalence",
                "admission_provenance": provenance[evidence_reference.id] if support else None,
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
        if not verified:
            reasons.append("No fully verified native-signal recurrence; all candidate content preserved")
        if progress_callback:
            progress_callback(100, "Analysis complete")
        return AnalysisDraft(tuple(evidence), tuple(ranges), "complete", tuple(dict.fromkeys(reasons)))
    except decoder.Cancelled as exc:
        return AnalysisDraft((), (), "cancelled", (str(exc),))
    except (decoder.DecodeError, OSError, ValueError, KeyError, TypeError) as exc:
        return AnalysisDraft((), (), "failed", (str(exc),))
