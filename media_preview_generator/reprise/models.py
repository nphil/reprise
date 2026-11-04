"""Value types shared by detection, review and clients."""

from dataclasses import dataclass


@dataclass(frozen=True)
class PlaybackIdentity:
    plex_machine_identifier: str
    rating_key: str
    media_id: str
    part_id: str
    part_key: str
    part_size_bytes: int
    duration_ms: int
    audio_stream_id: str


@dataclass(frozen=True)
class MediaVersion:
    id: str
    canonical_path: str
    size_bytes: int
    mtime_ns: int
    ctime_ns: int
    duration_ms: int
    stream_signature: str
    device: int | None = None
    inode: int | None = None
    digest_assurance: str = "stat-only"
    digest: str | None = None


@dataclass(frozen=True)
class Evidence:
    id: str
    start_ms: int
    end_ms: int
    modality: str
    verdict: str
    reason_code: str
    reason: str
    reference_version_id: str | None
    reference_start_ms: int | None
    reference_end_ms: int | None
    metrics: dict


# A range's origin when it is a suggestion no comparison of episodes confirmed: Plex's own intro/credits marker
# (``plex``), or the times an online database (IntroDB, SkipDB, TheIntroDB) gives for the episode (``online``). A
# suggestion is never eligible to skip until the owner approves that exact range. When Plex and a database agree it is
# one suggestion, origin ``plex`` (Plex already has the marker), naming both in ``suggested_by``.
ORIGIN_PLEX = "plex"
ORIGIN_ONLINE = "online"
SUGGESTION_ORIGINS = (ORIGIN_PLEX, ORIGIN_ONLINE)
REASON_PLEX_SUGGESTION = "plex_suggestion"
REASON_ONLINE_SUGGESTION = "online_suggestion"


@dataclass(frozen=True)
class DecisionRange:
    id: str
    kind: str
    action: str
    start_ms: int
    end_ms: int
    review_status: str
    locked: bool
    origin: str
    reason_code: str
    reason: str
    evidence_ids: tuple[str, ...]
    audio_stream_ids: tuple[str, ...]
    terminal: bool = False
    # Who suggested a suggestion range, in display order ("Plex", "IntroDB", ...); empty on every other range, and on
    # Plex suggestions stored before online databases were used (those are Plex's).
    suggested_by: tuple[str, ...] = ()


@dataclass(frozen=True)
class AnalysisResult:
    media_version_id: str
    analysis_id: str
    result_revision: int
    analysis_status: str
    freshness: str
    evidence_signature: str
    policy_version: str
    ranges: tuple[DecisionRange, ...]
    evidence: tuple[Evidence, ...]
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class CandidateWindow:
    kind: str
    start_ms: int
    end_ms: int
    source: str


@dataclass(frozen=True)
class OnlineWindow:
    """Where online databases put one episode's intro or credits, read on that file's own clock.

    ``sources`` names every database that gave these times (agreeing within a few seconds), best first; ``dissent``
    names those that gave the same kind other times and were left out."""

    kind: str
    start_ms: int
    end_ms: int
    sources: tuple[str, ...]
    dissent: tuple[str, ...] = ()


@dataclass(frozen=True)
class AnalysisDraft:
    evidence: tuple[Evidence, ...]
    ranges: tuple[DecisionRange, ...]
    status: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class ClientPrincipal:
    id: str
    name: str
    scopes: tuple[str, ...]


class AnalysisRefused(ValueError):
    """A request the owner can put right; the message is a plain sentence that is safe to show as is."""


class ReviewRefused(ValueError):
    """A review the client can put right; the message is a plain sentence that is safe to show as is."""
