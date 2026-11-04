"""Conservative half-open interval policy; never fill or merge gaps."""

from dataclasses import replace

from .models import SUGGESTION_ORIGINS, AnalysisResult, DecisionRange
from .verification_profile import POLICY_VERSION, is_current_profile


def eligible_ranges(result: AnalysisResult, audio_stream_id: str) -> tuple[DecisionRange, ...]:
    if (
        result.analysis_status != "complete"
        or result.freshness != "current"
        or result.policy_version != POLICY_VERSION
        or not all(is_current_profile(e.metrics) for e in result.evidence)
        or not audio_stream_id
    ):
        return ()
    keep_barriers = [(r.start_ms, r.end_ms) for r in result.ranges if r.action == "keep"]
    unconfirmed = [(e.id, e.start_ms, e.end_ms) for e in result.evidence if e.verdict.lower() != "match"]
    pieces: list[DecisionRange] = []
    for item in sorted(result.ranges, key=lambda r: (r.start_ms, r.end_ms, r.id)):
        if item.action != "skip" or item.review_status != "approved" or audio_stream_id not in item.audio_stream_ids:
            continue
        if item.origin in SUGGESTION_ORIGINS:
            # A suggestion (Plex's marker or an online database's times) has no recurrence evidence to intersect with:
            # the owner's approval of this exact range stands in for it. That nothing confirmed it is why it is a
            # suggestion, so its own unconfirmed evidence is not a veto; every keep and any other unconfirmed
            # evidence still is.
            intervals = [(item.start_ms, item.end_ms)]
            barriers = keep_barriers + [(lo, hi) for key, lo, hi in unconfirmed if key not in item.evidence_ids]
        else:
            intervals = sorted(
                (max(item.start_ms, e.start_ms), min(item.end_ms, e.end_ms))
                for e in result.evidence
                if e.id in item.evidence_ids
                and e.verdict.lower() == "match"
                and e.modality == "audiovisual"
                and audio_stream_id in e.metrics.get("checked_audio_stream_ids", ())
                and e.reference_version_id is not None
                and max(item.start_ms, e.start_ms) < min(item.end_ms, e.end_ms)
            )
            barriers = keep_barriers + [(lo, hi) for _, lo, hi in unconfirmed]
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
