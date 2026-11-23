"""Review is independent of publication; legacy marker writes are not authorization."""

from dataclasses import replace

from .models import ORIGIN_PLEX, PublicationRefused, is_owner_span
from .policy import blocked_sentence, owner_blockers, span_limit


def reject_legacy_publication() -> None:
    """Stop all old marker publishers, including requests arriving at a Plex node."""
    from ..markers.publishers.base import PublishError

    raise PublishError("Legacy marker publication is disabled; use a reviewed Reprise publication")


def save_review(store, media_version_id, expected_revision, edits, actor):
    """Save owner decisions without publishing them to any media server."""
    return store.save_review(media_version_id, expected_revision, edits, actor)


def plex_projection(result, identity, eligible, plex_now=None):
    """Return exact approved skips Plex can represent, otherwise refuse.

    ``eligible`` must come from the central policy for this exact identity.
    A trimmed or split decision is not the approved representation. A range Plex itself suggested is already in
    Plex, so it is neither published nor counted: only what Reprise confirmed is a projection. The owner's own exact
    skip for a kind (an owner span) replaces every other skip of that kind, Plex's included: it is sent when its
    times differ from Plex's marker, and when they are the same there is nothing to send. ``plex_now`` is the
    ``(kind, start_ms, end_ms)`` of each marker Plex holds right now (none when Plex holds none), for a caller that
    has read them. It is the truth about what Plex has, which the saved ranges are not: a re-analysis cuts Plex's
    suggestion away from under a span, and a span already sent is no suggestion at all. Without it Plex is taken to
    hold the ranges it suggested.
    """
    if result.analysis_status != "complete" or result.freshness != "current":
        raise ValueError("Analysis is not complete and current")
    spans = [r for r in result.ranges if is_owner_span(r) and r.review_status == "approved"]
    for span in spans:
        blockers = owner_blockers(result.ranges, span)
        if blockers:
            raise PublicationRefused(blocked_sentence(span, blockers))
    owned = {r.kind for r in spans}
    if plex_now is None:
        plex_now = {
            (r.kind, r.start_ms, r.end_ms) for r in result.ranges if r.action == "skip" and r.origin == ORIGIN_PLEX
        }

    def sent(r):
        """Whether Reprise would write this range to Plex: an owner span unless Plex has exactly it already, and any
        other skip unless it is Plex's own or the owner's span replaced its kind."""
        if is_owner_span(r):
            return (r.kind, r.start_ms, r.end_ms) not in plex_now
        return r.origin != ORIGIN_PLEX and r.kind not in owned

    approved = sorted(
        (r for r in result.ranges if r.action == "skip" and r.review_status == "approved" and sent(r)),
        key=lambda r: (r.start_ms, r.end_ms, r.id),
    )
    eligible = sorted((r for r in eligible if sent(r)), key=lambda r: (r.start_ms, r.end_ms, r.id))
    if spans and not approved:
        raise PublicationRefused("Plex already has exactly the skip you set, so there is nothing to publish")
    if not approved and any(r.review_status == "approved" and r.origin == ORIGIN_PLEX for r in result.ranges):
        raise PublicationRefused("Plex's own suggestions are already in Plex; there is nothing to publish")
    if not approved or approved != eligible:
        raise ValueError("Approved skips are not fully eligible for this playback identity")
    seen = set()
    previous = None
    merged = []
    for item in approved:
        # Pieces of one kind that touch exactly (one confirmed run split by evidence) are one Plex marker; any gap
        # between them is a protected moment Plex can't represent, so it still refuses.
        joins = previous is not None and previous.kind == item.kind and item.start_ms == previous.end_ms
        if item.kind not in ("intro", "credits") or (item.kind in seen and not joins):
            raise ValueError("Plex cannot represent this set of skip ranges exactly")
        if (
            type(item.start_ms) is not int
            or type(item.end_ms) is not int
            or not 0 <= item.start_ms < item.end_ms <= identity.duration_ms
            or (previous is not None and item.start_ms < previous.end_ms)
        ):
            raise ValueError("Skip ranges overlap or have invalid bounds")
        limit = span_limit(item.kind, item.start_ms, item.end_ms, identity.duration_ms, item.terminal)
        if limit:
            raise ValueError(limit)
        if identity.audio_stream_id not in item.audio_stream_ids:
            raise ValueError("Selected audio track was not reviewed")
        if joins:
            merged[-1] = replace(merged[-1], end_ms=item.end_ms)
        else:
            merged.append(item)
        seen.add(item.kind)
        previous = item
    return tuple(merged)
