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


# A range's origin when it is Plex's own intro/credits marker, offered as a suggestion that no comparison of episodes
# confirmed. It is never eligible to skip until the owner approves that exact range.
ORIGIN_PLEX = "plex"
REASON_PLEX_SUGGESTION = "plex_suggestion"


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
