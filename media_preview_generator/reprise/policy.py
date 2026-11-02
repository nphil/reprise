"""Conservative half-open interval policy; never fill or merge gaps."""
from dataclasses import replace
from .models import AnalysisResult, DecisionRange


def eligible_ranges(result: AnalysisResult, audio_stream_id: str) -> tuple[DecisionRange, ...]:
    if result.analysis_status != 'complete' or result.freshness != 'current' or not audio_stream_id:
        return ()
    barriers = [(r.start_ms, r.end_ms) for r in result.ranges if r.action == 'keep']
    barriers += [(e.start_ms, e.end_ms) for e in result.evidence if e.verdict.lower() != 'match']
    pieces = []
    for item in sorted(result.ranges, key=lambda r: (r.start_ms, r.end_ms, r.id)):
        if item.action != 'skip' or item.review_status != 'approved' or audio_stream_id not in item.audio_stream_ids:
            continue
        intervals = sorted((max(item.start_ms, e.start_ms), min(item.end_ms, e.end_ms))
                           for e in result.evidence
                           if e.id in item.evidence_ids and e.verdict.lower() == 'match'
                           and e.modality == 'audiovisual'
                           and audio_stream_id in e.metrics.get('checked_audio_stream_ids', ())
                           and e.reference_version_id is not None
                           and max(item.start_ms, e.start_ms) < min(item.end_ms, e.end_ms))
        for start, end in barriers:
            next_intervals = []
            for lo, hi in intervals:
                if end <= lo or start >= hi:
                    next_intervals.append((lo, hi))
                else:
                    if lo < start:
                        next_intervals.append((lo, start))
                    if end < hi:
                        next_intervals.append((end, hi))
            intervals = next_intervals
        for lo, hi in intervals:
            if pieces and lo < pieces[-1].end_ms:
                lo = pieces[-1].end_ms
            if lo < hi:
                pieces.append(replace(item, start_ms=lo, end_ms=hi, terminal=item.terminal and hi == item.end_ms))
    return tuple(pieces)
