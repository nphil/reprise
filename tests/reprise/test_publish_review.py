from dataclasses import replace

import pytest

from media_preview_generator.reprise.models import AnalysisResult, DecisionRange, PlaybackIdentity
from media_preview_generator.reprise.review import plex_projection


def _range(**changes):
    base = DecisionRange("a", "intro", "skip", 1000, 5000, "approved", True, "manual", "", "", (), ("audio",))
    return replace(base, **changes)


def _result(*ranges):
    return AnalysisResult("version", "analysis", 1, "complete", "current", "signature", "1", ranges, (), ())


def _identity():
    return PlaybackIdentity("machine", "1", "2", "3", "/part", 100, 100000, "audio")


def test_projection_keeps_exact_approved_range():
    r = _range()
    assert plex_projection(_result(r), _identity(), (r,)) == (r,)


def test_projection_rejects_trimming_and_disjoint_ranges():
    r = _range()
    with pytest.raises(ValueError):
        plex_projection(_result(r), _identity(), (replace(r, end_ms=4000),))
    other = _range(id="b", start_ms=6000, end_ms=9000)
    with pytest.raises(ValueError):
        plex_projection(_result(r, other), _identity(), (r, other))


def test_projection_refuses_unreviewed_track_and_end_hazard():
    r = _range(audio_stream_ids=("other",))
    with pytest.raises(ValueError):
        plex_projection(_result(r), _identity(), (r,))
    r = _range(kind="credits", start_ms=90000, end_ms=99000)
    with pytest.raises(ValueError):
        plex_projection(_result(r), _identity(), (r,))


def test_projection_rejects_keep_and_unknown_evidence_gaps():
    from media_preview_generator.reprise.models import Evidence
    from media_preview_generator.reprise.policy import eligible_ranges

    r = _range()
    keep = replace(r, id="keep", action="keep", start_ms=2000, end_ms=3000)
    result = _result(r, keep)
    with pytest.raises(ValueError):
        plex_projection(result, _identity(), eligible_ranges(result, "audio"))
    unknown = Evidence("unknown", 2000, 3000, "video", "unknown", "undecoded", "", None, None, None, {})
    result = replace(_result(r), evidence=(unknown,))
    with pytest.raises(ValueError):
        plex_projection(result, _identity(), eligible_ranges(result, "audio"))


def test_projection_rejects_stale_and_partial_analysis():
    r = _range()
    for result in (replace(_result(r), freshness="stale"), replace(_result(r), analysis_status="failed")):
        with pytest.raises(ValueError):
            plex_projection(result, _identity(), (r,))
