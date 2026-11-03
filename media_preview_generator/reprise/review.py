"""Review is independent of publication; legacy marker writes are not authorization."""

from .models import ORIGIN_PLEX


def reject_legacy_publication() -> None:
    """Stop all old marker publishers, including requests arriving at a Plex node."""
    from ..markers.publishers.base import PublishError

    raise PublishError("Legacy marker publication is disabled; use a reviewed Reprise publication")


def save_review(store, media_version_id, expected_revision, edits, actor):
    """Save owner decisions without publishing them to any media server."""
    return store.save_review(media_version_id, expected_revision, edits, actor)


def plex_projection(result, identity, eligible):
    """Return exact approved skips Plex can represent, otherwise refuse.

    ``eligible`` must come from the central policy for this exact identity.
    A trimmed or split decision is not the approved representation. A range Plex itself suggested is already in
    Plex, so it is neither published nor counted: only what Reprise confirmed is a projection.
    """
    if result.analysis_status != "complete" or result.freshness != "current":
        raise ValueError("Analysis is not complete and current")
    approved = sorted(
        (r for r in result.ranges if r.action == "skip" and r.review_status == "approved" and r.origin != ORIGIN_PLEX),
        key=lambda r: (r.start_ms, r.end_ms, r.id),
    )
    eligible = sorted((r for r in eligible if r.origin != ORIGIN_PLEX), key=lambda r: (r.start_ms, r.end_ms, r.id))
    if not approved and any(r.review_status == "approved" and r.origin == ORIGIN_PLEX for r in result.ranges):
        raise ValueError("Plex's own suggestions are already in Plex; there is nothing to publish")
    if not approved or approved != eligible:
        raise ValueError("Approved skips are not fully eligible for this playback identity")
    seen = set()
    previous_end = 0
    for item in approved:
        if item.kind not in ("intro", "credits") or item.kind in seen:
            raise ValueError("Plex cannot represent this set of skip ranges exactly")
        if (
            type(item.start_ms) is not int
            or type(item.end_ms) is not int
            or not 0 <= item.start_ms < item.end_ms <= identity.duration_ms
            or item.start_ms < previous_end
        ):
            raise ValueError("Skip ranges overlap or have invalid bounds")
        if item.terminal or item.end_ms >= identity.duration_ms - 2000:
            raise ValueError("The final two seconds cannot be projected safely to Plex")
        if item.kind == "credits" and item.start_ms < 2000:
            raise ValueError("Plex cannot represent this credits start exactly")
        if identity.audio_stream_id not in item.audio_stream_ids:
            raise ValueError("Selected audio track was not reviewed")
        seen.add(item.kind)
        previous_end = item.end_ms
    return tuple(approved)
