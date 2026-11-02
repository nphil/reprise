"""Cross-episode, review-only recurrence analysis.

Matching full decoded frames and all checked audio samples is intentionally
conservative: alternate encodes often abstain. No detector output is approval.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

from . import media as decoder
from .models import AnalysisDraft, CandidateWindow, DecisionRange, Evidence, MediaVersion
from .recurrence import matching_second, shifts

MAX_CANDIDATE_MS = 180_000
REFERENCE_RADIUS_MS = 30_000
MAX_REFERENCES = 6


def _id(*parts):
    return hashlib.sha256(json.dumps(parts, sort_keys=True, separators=(',', ':')).encode()).hexdigest()[:32]


def _audio_suggestions(target, reference, start, end, ref_start, ref_end, target_track, ref_track, cancel, pause):
    # MPG's Chromaprint finder is only a proposal source: its intentionally
    # gap-tolerant runs are NEVER used as evidence that an interval is safe.
    try:
        import numpy as np
        from ..markers.audio.fingerprint import fingerprint_command
        from ..markers.audio.matcher import pair_runs
        def read_fp(item, a, b, track):
            command = fingerprint_command('ffmpeg', item.canonical_path, (b-a)/1000, audio_stream=track[1])
            command[command.index('-ss') + 1] = f'{a/1000:.3f}'
            return np.frombuffer(decoder.run(command, cancel, pause, 1024*1024), dtype='<u4')
        left = read_fp(target, start, end, target_track)
        right = read_fp(reference, ref_start, ref_end, ref_track)
        return [round(ref_start + 1000*r.b_start_s - start - 1000*r.a_start_s) for r in pair_runs(left, right)]
    except decoder.Cancelled:
        raise
    except (decoder.DecodeError, ImportError, ValueError, OSError):
        return []


def analyze(media: MediaVersion, candidates: Sequence[CandidateWindow], references: Sequence[MediaVersion], *, cancel_check, pause_check, progress_callback, ffmpeg_threads) -> AnalysisDraft:
    evidence, ranges, reasons = [], [], []
    cancel = cancel_check or (lambda: False)
    pause = pause_check or (lambda: False)
    threads = max(1, min(int(ffmpeg_threads or 1), 4))
    try:
        video, tracks, origin = decoder.probe(media, cancel, pause)
        ref_info = []
        for ref in references[:MAX_REFERENCES]:
            if ref.id == media.id:
                continue
            try:
                rv, rt, ro = decoder.probe(ref, cancel, pause)
                # Pairing is by the explicit, stable stream order and only if
                # every audio track is present with the same unaltered format.
                if len(rt) == len(tracks) and all(a[2:] == b[2:] for a, b in zip(tracks, rt)):
                    ref_info.append((ref, rv, rt, ro))
            except decoder.Cancelled:
                raise
            except (decoder.DecodeError, OSError, ValueError, KeyError, TypeError) as exc:
                reasons.append('Reference unavailable: ' + str(exc))
        for number, window in enumerate(candidates):
            if cancel():
                raise decoder.Cancelled('Analysis cancelled')
            if progress_callback:
                progress_callback(int(100 * number / max(1, len(candidates))), 'Comparing episode content')
            start, end = window.start_ms, window.end_ms
            if window.kind not in ('intro', 'credits', 'recap', 'preview') or not (0 <= start < end <= media.duration_ms) or end-start > MAX_CANDIDATE_MS:
                raise decoder.DecodeError('Invalid or oversized candidate window')
            first, last = ((start + 999) // 1000) * 1000, (end // 1000) * 1000
            target_start, target_end = max(0, start-2000), min(media.duration_ms, end+2000)
            target_frames = decoder.video_frames(media, video, target_start, target_end, cancel, pause, threads)
            best = None
            for ref, rv, rt, ro in ref_info:
                rs = max(0, start-REFERENCE_RADIUS_MS-2000)
                re = min(ref.duration_ms, end+REFERENCE_RADIUS_MS+2000)
                if re <= rs:
                    continue
                try:
                    rf = decoder.video_frames(ref, rv, rs, re, cancel, pause, threads)
                    proposals = shifts(target_frames, rf)
                    if not proposals and end-start >= 8000:
                        proposals = _audio_suggestions(media, ref, start, end, rs, re, tracks[0], rt[0], cancel, pause)
                    for offset in proposals:
                        matches = [s for s in range(first, last, 1000) if 0 <= s+offset and s+offset+1000 <= ref.duration_ms and matching_second(target_frames, rf, s, offset)]
                        score = len(matches)
                        if score and (best is None or score > best[0]):
                            best = (score, ref, rt, ro, offset, set(matches), rf)
                except decoder.Cancelled:
                    raise
                except (decoder.DecodeError, OSError, ValueError, KeyError, TypeError) as exc:
                    reasons.append('Reference comparison unavailable: ' + str(exc))
            verdicts = []
            audio_verdicts = {}
            if best and first < last:
                _, ref, rt, ro, offset, _, rf = best
                # Decode a contiguous bounded context once per track, rather
                # than repeatedly seeking and decoding each individual second.
                lo = max(((target_start + 999) // 1000) * 1000, ((-offset + 999) // 1000) * 1000)
                hi = min((target_end // 1000) * 1000, ((ref.duration_ms-offset) // 1000) * 1000)
                if lo < hi:
                    try:
                        audio_verdicts = {sec: True for sec in range(lo, hi, 1000)}
                        for track, ref_track in zip(tracks, rt):
                            left = memoryview(decoder.audio_pcm(media, track, lo, hi, origin, cancel, pause, threads))
                            right = memoryview(decoder.audio_pcm(ref, ref_track, lo+offset, hi+offset, ro, cancel, pause, threads))
                            width = track[2] * track[3] * 4
                            for sec in audio_verdicts:
                                pos = (sec-lo) // 1000 * width
                                if left[pos:pos+width] != right[pos:pos+width]:
                                    audio_verdicts[sec] = False
                    except decoder.Cancelled:
                        raise
                    except (decoder.DecodeError, OSError, ValueError, KeyError, TypeError) as exc:
                        audio_verdicts = {}
                        reasons.append('Audio verification unavailable: ' + str(exc))
                # Inspect the immediate surrounding context on the same
                # reference and time offset. Context is evidence, never a skip.
                for sec in range(lo, hi, 1000):
                    if first <= sec < last:
                        continue
                    context_verdict = 'unknown'
                    if sec in audio_verdicts and matching_second(target_frames, rf, sec, offset):
                        context_verdict = 'match' if audio_verdicts[sec] else 'different'
                    eid = _id(media.id, 'context', sec, context_verdict, ref.id, offset)
                    evidence.append(Evidence(eid, sec, sec+1000, 'audiovisual', context_verdict, 'context_'+context_verdict, 'Adjacent context checked against the same reference timeline.', ref.id, sec+offset, sec+offset+1000, {'context': True, 'reference_offset_ms': offset}))
            for sec in range(first, last, 1000):
                verdict = 'unknown'
                if best and sec in best[5] and sec in audio_verdicts:
                    verdict = 'match' if audio_verdicts[sec] else 'different'
                verdicts.append((sec, sec+1000, verdict))
            if start < first:
                verdicts.insert(0, (start, min(end, first), 'unknown'))
            if last < end and last >= first:
                verdicts.append((max(start, last), end, 'unknown'))
            if not verdicts:
                verdicts.append((start, end, 'unknown'))
            # Merge only directly adjacent seconds with the same observed
            # verdict. Changed/unknown seconds can never be bridged.
            grouped = []
            for lo, hi, verdict in verdicts:
                if lo >= hi:
                    continue
                if grouped and grouped[-1][1] == lo and grouped[-1][2] == verdict:
                    grouped[-1] = (grouped[-1][0], hi, verdict)
                else:
                    grouped.append((lo, hi, verdict))
            for lo, hi, verdict in grouped:
                ref_id = best[1].id if best else None
                offset = best[4] if best else None
                eid = _id(media.id, window.kind, window.source, lo, hi, verdict, ref_id, offset)
                text = {'match': 'Complete decoded pictures and checked audio match one reference timeline.', 'different': 'Audio differs from the reference.', 'unknown': 'Content or timing could not be verified; preserve it.'}[verdict]
                evidence.append(Evidence(eid, lo, hi, 'audiovisual', verdict, 'full_content_'+verdict, text, ref_id, lo+offset if offset is not None else None, hi+offset if offset is not None else None, {'candidate_source': window.source, 'checked_audio_stream_ids': [t[0] for t in tracks] if verdict == 'match' else [], 'reference_offset_ms': offset, 'verification': 'native-frame-and-pcm-exact'}))
                action = 'skip' if verdict == 'match' else 'keep'
                ranges.append(DecisionRange(_id('range', eid), window.kind, action, lo, hi, 'proposed', False, 'detector', 'full_content_'+verdict, text, (eid,), tuple(t[0] for t in tracks) if verdict == 'match' else (), False))
            decoder.check_current(media)
            if best:
                decoder.check_current(best[1])
        if progress_callback:
            progress_callback(100, 'Analysis complete')
        # Identical or overlapping candidate windows can produce the same
        # context/proposal twice; storage keys are unique per media version.
        unique_evidence = tuple({item.id: item for item in evidence}.values())
        unique_ranges = tuple({item.id: item for item in ranges}.values())
        return AnalysisDraft(unique_evidence, unique_ranges, 'complete', tuple(reasons))
    except decoder.Cancelled as exc:
        return AnalysisDraft((), (), 'cancelled', (str(exc),))
    except (decoder.DecodeError, OSError, ValueError, KeyError, TypeError) as exc:
        return AnalysisDraft((), (), 'failed', (str(exc),))
