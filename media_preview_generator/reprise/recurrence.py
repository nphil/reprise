"""Conservative sequence alignment, inspired by RCD's frame-feature matcher.

Unlike RCD's global colour histogram, the descriptor is a full decoded-frame
checksum; matching it cannot confuse spatially rearranged pictures. No gaps are
filled and all timestamps are presentation timestamps. MPG's audio matcher is
used only to suggest alignments; it cannot certify a skip.
"""
from collections import Counter, defaultdict
from fractions import Fraction


def shifts(target, reference, maximum=16):
    """Propose coherent offsets from repeated full-frame descriptors.

    Highly repetitive descriptors are ignored to bound work and avoid choosing
    an alignment just because both episodes contain long black frames.
    """
    positions = defaultdict(list)
    for pts, digest in reference:
        if len(positions[digest]) < 33:
            positions[digest].append(pts)
    votes = Counter()
    for pts, digest in target:
        matches = positions.get(digest, ())
        if len(matches) > 32:
            continue
        for other in matches:
            delta = other - pts
            # All later checks retain the rational PTS, so rounding here only
            # collects suggestions, it never establishes a match.
            votes[round(delta)] += 1
    return [offset for offset, count in votes.most_common(maximum) if count >= 3]


def matching_second(target, reference, start_ms, offset_ms):
    """Require identical frames and timing over the complete second and edges."""
    lo, hi = Fraction(start_ms), Fraction(start_ms + 1000)
    def slice_with_edges(frames, start, end):
        inside = [(p, h) for p, h in frames if start <= p < end]
        before = next(((p, h) for p, h in reversed(frames) if p < start), None)
        after = next(((p, h) for p, h in frames if p >= end), None)
        if not inside or before is None or after is None:
            return None
        return [before, *inside, after]
    a = slice_with_edges(target, lo, hi)
    b = slice_with_edges(reference, lo + offset_ms, hi + offset_ms)
    if a is None or b is None or len(a) != len(b):
        return False
    if any(h != k or abs((q - p) - offset_ms) > Fraction(1, 2) for (p, h), (q, k) in zip(a, b)):
        return False
    # Missing or sparse frames are unknown, rather than proof of continuity.
    return all(q[0] - p[0] <= 250 for p, q in zip(a, a[1:]))
