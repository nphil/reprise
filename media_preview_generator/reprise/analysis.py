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

from . import media as decoder
from . import recurrence as verify
from .models import AnalysisDraft, CandidateWindow, DecisionRange, Evidence, MediaVersion

_VerifiedRun = tuple[int, int, MediaVersion, Fraction, list[str]]
_LedgerKey = tuple[str, str, str | None, str | None]

MAX_CANDIDATE_MS = 180_000
REFERENCE_RADIUS_MS = 30_000
MAX_REFERENCES = 6
CONTEXT_MS = 2000
WARNING = (
    "Codec-tolerant native-code-signal recurrence under matching declarations; "
    "not certified rendered equivalence. Unspecified interpretation and sufficiently small semantic differences are not established."
)


def _id(*parts):
    return hashlib.sha256(json.dumps(parts, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:32]


def _ratio(value):
    return Fraction(str(value).replace(":", "/"))


def _representation(item, stream, cancel, pause):
    """Stream declarations supplement, never replace, the same-decode frame audit."""
    info = json.loads(
        decoder.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", item.canonical_path], cancel, pause)
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
            *(verify.declaration(video.get(k)) for k in verify.COLOR_FIELDS),
        )
        if _ratio(video["sample_aspect_ratio"]) <= 0 or _ratio(video["r_frame_rate"]) <= 0:
            raise ValueError("Nonpositive geometry or cadence")
    except (KeyError, ValueError, ZeroDivisionError) as exc:
        raise decoder.DecodeError("Missing native representation declarations") from exc
    decoder.check_current(item)
    return result


def _scan(item, stream, start, end, cancel, pause, threads):
    """Proposal descriptors only, accepted after full decoder completion."""
    hints, exact = [], []
    declarations = None
    unspecified = []
    with decoder.native_frames(item, stream, start, end, cancel, pause, threads) as frames:
        for frame in frames:
            if cancel():
                raise decoder.Cancelled("Analysis cancelled")
            if frame.interpretation_flags:
                raise decoder.DecodeError("Unsupported native interpretation: " + ",".join(frame.interpretation_flags))
            signature = verify.frame_declarations(frame)
            if declarations is None:
                declarations = signature
                unspecified = [k for k in verify.COLOR_FIELDS if verify.declaration(getattr(frame, k)) == "unspecified"]
            elif signature != declarations:
                raise decoder.DecodeError("Native declarations changed within comparison window")
            if len(hints) >= 40000:
                raise decoder.DecodeError("Native comparison frame budget exceeded")
            if frame.pts_ms < end:
                hints.append((frame.pts_ms, verify.descriptor(frame)))
                digest = hashlib.sha256()
                for plane in frame.planes:
                    digest.update(plane.data)
                exact.append((frame.pts_ms, digest.digest()))
    decoder.check_current(item)
    return hints, exact, declarations, unspecified


def _audio_suggestions(target, reference, start, end, ref_start, ref_end, target_track, ref_track, cancel, pause):
    # Existing gap-tolerant Chromaprint matcher supplies hints only.
    try:
        import numpy as np

        from ..markers.audio.fingerprint import fingerprint_command
        from ..markers.audio.matcher import pair_runs

        def read_fp(item, lo, hi, track):
            command = fingerprint_command("ffmpeg", item.canonical_path, (hi - lo) / 1000, audio_stream=track[1])
            command[1:1] = ["-threads", "1"]
            command[command.index("-ss") + 1] = f"{lo / 1000:.6f}"
            return np.frombuffer(decoder.run(command, cancel, pause, 1024 * 1024), dtype="<u4")

        left, right = read_fp(target, start, end, target_track), read_fp(reference, ref_start, ref_end, ref_track)
        return [
            Fraction(round(ref_start + 1000 * r.b_start_s - start - 1000 * r.a_start_s)) for r in pair_runs(left, right)
        ]
    except decoder.Cancelled:
        raise
    except (decoder.DecodeError, ImportError, ValueError, OSError):
        return []


def _video_support(target, reference, video, ref_video, start, end, offset, cancel, pause, threads):
    """Stage results locally. Tail failure in EITHER decoder discards the window."""
    points = []
    with decoder.native_frames(target, video, math.floor(start), math.ceil(end), cancel, pause, threads) as left:
        with decoder.native_frames(
            reference, ref_video, math.floor(start + offset), math.ceil(end + offset), cancel, pause, threads
        ) as right:
            a, b = next(left, None), next(right, None)
            while a is not None:
                wanted = a.pts_ms + offset
                while b is not None and b.pts_ms < wanted - Fraction(1, 2):
                    b = next(right, None)
                matched = b is not None and abs(b.pts_ms - wanted) <= Fraction(1, 2)
                passed = matched and verify.video_matches(a, b)
                points.append((a.pts_ms, b.pts_ms if matched else None, passed))
                a = next(left, None)
            # Exhaust unmatched reference tail too; errors after last yield matter.
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
            and abs((b[1] - a[1]) - (b[0] - a[0])) <= Fraction(1, 2)
        )
        if a[2] and b[2] and continuous:
            safe.append((max(lo, a[1] - offset), min(hi, b[1] - offset)))
        else:
            bad.append((lo - verify.CHANGE_GUARD_MS, hi + verify.CHANGE_GUARD_MS))
    return verify.subtract(safe, bad)


def _audio_support(target, reference, tracks, ref_tracks, start, end, offset, cancel, pause, threads):
    safe = [(start, end)]
    for track, ref_track in zip(tracks, ref_tracks, strict=True):
        good, bad = [], []
        chunk = start
        while chunk < end:
            if cancel():
                raise decoder.Cancelled("Analysis cancelled")
            stop = min(end, chunk + 10000 + verify.AUDIO_WINDOW_MS)
            a = decoder.native_audio_pcm(target, track[1], chunk, stop, cancel, pause, threads)
            b = decoder.native_audio_pcm(reference, ref_track[1], chunk + offset, stop + offset, cancel, pause, threads)
            if (a.sample_rate, a.channel_layout, a.channel_ids) != (b.sample_rate, b.channel_layout, b.channel_ids):
                raise decoder.DecodeError("Native audio rate or channel layout differs")
            if a.support_start_ms + offset != b.support_start_ms or a.support_end_ms + offset != b.support_end_ms:
                raise decoder.DecodeError("Native audio sample grids do not align")
            rate = a.sample_rate
            width, hop = rate * verify.AUDIO_WINDOW_MS // 1000, rate * verify.AUDIO_HOP_MS // 1000
            if width * 1000 != rate * verify.AUDIO_WINDOW_MS or hop * 1000 != rate * verify.AUDIO_HOP_MS:
                raise decoder.DecodeError("Native audio window does not fit sample grid")
            for index in range(0, len(a.data) - width + 1, hop):
                lo = a.support_start_ms + Fraction(index * 1000, rate)
                hi = lo + verify.AUDIO_WINDOW_MS
                if verify.audio_matches(a.data[index : index + width], b.data[index : index + width], rate):
                    good.append((lo, hi))
                else:
                    # Failed overlapping windows veto their ENTIRE support, not
                    # merely their centers. Guard uncertainty around changes.
                    bad.append((lo - verify.CHANGE_GUARD_MS, hi + verify.CHANGE_GUARD_MS))
            chunk += 10000
        safe = verify.intersect(safe, verify.subtract(good, bad))
    return safe


def analyze(
    media: MediaVersion,
    candidates: Sequence[CandidateWindow],
    references: Sequence[MediaVersion],
    *,
    cancel_check,
    pause_check,
    progress_callback,
    ffmpeg_threads,
) -> AnalysisDraft:
    cancel = cancel_check or (lambda: False)
    pause = pause_check or (lambda: False)
    threads = max(1, min(int(ffmpeg_threads or 1), 4))
    reasons, verified = [], []
    windows = sorted(set(candidates), key=lambda w: (w.start_ms, w.end_ms, w.kind, w.source))
    try:
        if cancel():
            raise decoder.Cancelled("Analysis cancelled")
        video, tracks, _ = decoder.probe(media, cancel, pause)
        representation = _representation(media, video, cancel, pause)
        refs = []
        for ref in references[:MAX_REFERENCES]:
            if ref.id == media.id:
                continue
            try:
                rv, rt, _ = decoder.probe(ref, cancel, pause)
                if representation != _representation(ref, rv, cancel, pause):
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
                hints, hashes, declarations, unspecified = _scan(media, video, ts, te, cancel, pause, threads)
            except decoder.Cancelled:
                raise
            except decoder.DecodeError as exc:
                reasons.append("Native target window unsupported: " + str(exc))
                continue
            for ref, rv, rt in refs:
                rs, re = max(0, ts - REFERENCE_RADIUS_MS), min(ref.duration_ms, te + REFERENCE_RADIUS_MS)
                if rs >= re:
                    continue
                try:
                    rh, r_hashes, r_decl, _ = _scan(ref, rv, rs, re, cancel, pause, threads)
                    if declarations != r_decl:
                        raise decoder.DecodeError("Native frame declarations differ")
                    proposals = verify.perceptual_shifts(hints, rh)
                    proposals += [Fraction(x) for x in verify.shifts(hashes, r_hashes)]
                    if not proposals and te - ts >= 8000:
                        proposals += _audio_suggestions(media, ref, ts, te, rs, re, tracks[0], rt[0], cancel, pause)
                    for offset in dict.fromkeys(proposals):
                        lo, hi = max(Fraction(ts), -offset), min(Fraction(te), ref.duration_ms - offset)
                        if hi - lo < verify.MIN_RUN_MS:
                            continue
                        try:
                            audio = _audio_support(media, ref, tracks, rt, lo, hi, offset, cancel, pause, threads)
                            if not audio:
                                continue
                            picture = _video_support(media, ref, video, rv, lo, hi, offset, cancel, pause, threads)
                        except decoder.Cancelled:
                            raise
                        except decoder.DecodeError as exc:
                            reasons.append("Native offset comparison unavailable: " + str(exc))
                            continue
                        matches = verify.intersect(audio, picture)
                        for a, b in matches:
                            # Each fixed-offset run independently has native
                            # anchors at both ends; holes cannot borrow anchors.
                            if b - a < verify.MIN_RUN_MS:
                                continue
                            a, b = math.ceil(max(a, window.start_ms)), math.floor(min(b, window.end_ms))
                            if b - a >= verify.MIN_RUN_MS:
                                verified.append((a, b, ref, offset, unspecified))
                    decoder.check_current(ref)
                except decoder.Cancelled:
                    raise
                except (decoder.DecodeError, OSError, ValueError, KeyError, TypeError) as exc:
                    reasons.append("Native comparison unavailable: " + str(exc))
        decoder.check_current(media)
        for _, _, ref, _, _ in verified:
            decoder.check_current(ref)
        # One verdict ledger for the union of candidates. Failed alternative
        # hypotheses cannot create contradictory barriers over verified content.
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
                verify.PROFILE,
                window.kind,
                lo,
                hi,
                verdict,
                evidence_reference.id if evidence_reference else None,
                str(offset),
            )
            metrics = {
                "verification": verify.PROFILE,
                "candidate_source": window.source,
                "checked_audio_stream_ids": [t[0] for t in tracks] if support else [],
                "reference_offset_ms": float(offset) if offset is not None else None,
                "reference_offset_rational_ms": str(offset) if offset is not None else None,
                "unspecified_interpretation_fields": support[4] if support else [],
                "claim": "native-code-signal recurrence, not rendered equivalence",
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
