"""Conservative half-open interval policy; never fill or merge gaps."""

from dataclasses import replace

from .models import ORIGIN_OWNER, SUGGESTION_ORIGINS, AnalysisResult, DecisionRange, is_owner_span
from .verification_profile import POLICY_VERSION, is_current_profile

# Plex cannot hold a skip that reaches into the last two seconds of a file, or a credits skip that starts in the first.
END_MARGIN_MS = 2000


def clock(ms: int) -> str:
    """``m:ss`` (``h:mm:ss`` from an hour): a time as a person reads it off a player."""
    total = max(0, int(ms) // 1000)
    hours, minutes, seconds = total // 3600, total // 60 % 60, total % 60
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


def span_limit(
    kind: str, start_ms: int, end_ms: int, duration_ms: int, terminal: bool = False, owner: bool = False
) -> str | None:
    """Why Plex cannot hold a skip exactly where it sits in the file, or None: the last two seconds cannot be
    projected safely, and a credits skip cannot start in the first two. The one exception is a credits skip the owner
    set to end exactly at the file's end: Plex holds that exactly, as its final credits marker."""
    final_credits = owner and kind == "credits" and end_ms == duration_ms and not terminal
    if not final_credits and (terminal or end_ms >= duration_ms - END_MARGIN_MS):
        return "The final two seconds cannot be projected safely to Plex"
    if kind == "credits" and start_ms < END_MARGIN_MS:
        return "Plex cannot represent this credits start exactly"
    return None


def protects(r: DecisionRange) -> bool:
    """Whether a range is something the owner made, locked or approved, which a skip they set must never cover. A keep
    the analysis made itself (``native_signal_unknown``) is not: it says only that comparing episodes could not
    confirm that stretch."""
    if r.review_status == "rejected":
        return False
    return r.origin in ("manual", ORIGIN_OWNER) or r.locked or (r.action == "keep" and r.review_status == "approved")


def owner_blockers(ranges, span: DecisionRange) -> list[DecisionRange]:
    """The owner's own protected scenes and locked or set sections that ``span`` overlaps, in time order. The other
    skips of the span's own kind never block it: the owner's span replaces them."""
    return sorted(
        (
            r
            for r in ranges
            if r.id != span.id
            and not (r.action == "skip" and r.kind == span.kind)
            and protects(r)
            and r.start_ms < span.end_ms
            and span.start_ms < r.end_ms
        ),
        key=lambda r: (r.start_ms, r.end_ms, r.id),
    )


def blocked_sentence(span: DecisionRange, blockers) -> str:
    """The plain sentence that says an owner span would cover something the owner protected."""
    first = blockers[0]
    what = "a scene you protected" if first.action == "keep" else "a skip you set or locked"
    return (
        f"The {span.kind} skip you set would cover {what} (from {clock(first.start_ms)} to {clock(first.end_ms)}). "
        "Change the skip, or remove that first."
    )


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
    # The owner decides the whole span of a kind they set an exact skip for: that one skip replaces every other skip
    # of its kind.
    owned = {r.kind for r in result.ranges if is_owner_span(r) and r.review_status == "approved"}
    pieces: list[DecisionRange] = []
    for item in sorted(result.ranges, key=lambda r: (r.start_ms, r.end_ms, r.id)):
        if item.action != "skip" or item.review_status != "approved" or audio_stream_id not in item.audio_stream_ids:
            continue
        if is_owner_span(item):
            # The owner's own exact skip: nothing confirmed it and nothing has to. It may cover what the analysis
            # could not confirm and the keeps the analysis made itself; only a scene the owner protected, or a
            # section they locked, stops it.
            intervals = [(item.start_ms, item.end_ms)]
            barriers = [(r.start_ms, r.end_ms) for r in owner_blockers(result.ranges, item)]
        elif item.kind in owned:
            continue
        elif item.origin in SUGGESTION_ORIGINS:
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
