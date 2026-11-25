"""A window Plex or an online database suggests goes on to further episodes when the nearest confirm too little of it.

Real synthetic media, no mocks of the comparison (see test_analysis_recurrence): seven-second episodes whose picture is
the same everywhere, so only the sound tells which of them repeat the target. That is the shape of what happened to
Community S03 under rule set v8: the nearest three episodes of E08 and E12 had the picture but not the sound mix (which
only the other of the two has, 7 and 8 episodes away), and E13's intro was shared mostly by E15, its fourth nearest.
"""

from fractions import Fraction
from types import SimpleNamespace

import pytest

from media_preview_generator.reprise import admission, analysis, scan_cache
from media_preview_generator.reprise import media as decoder
from media_preview_generator.reprise.analysis import _select_runs, _window_confirmed, analyze
from media_preview_generator.reprise.models import CandidateWindow
from media_preview_generator.reprise.verification_profile import PROFILE_SPEC
from tests.reprise.test_analysis_recurrence import (  # noqa: F401 - catalog is a fixture
    catalog,
    chirp,
    make_video,
    matches,
    overlaps,
    skips,
)

pytestmark = pytest.mark.timeout(200)  # addopts has --timeout=30; each case compares up to six episodes
PLEX = CandidateWindow("intro", 1000, 6000, "plex")
OTHER_SOUND = "0.3*sin(2*PI*(300*t+9*t*t))"


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    """The target, nine episodes with another mix (same picture, other sound), one that repeats the target's sound and
    picture, and one that repeats its sound but not its picture."""
    folder = tmp_path_factory.mktemp("season")
    return SimpleNamespace(
        target=make_video(folder / "target.mkv", sound=chirp()),
        others=[make_video(folder / f"other{n}.mkv", sound=chirp(OTHER_SOUND)) for n in range(9)],
        repeats=make_video(folder / "repeats.mkv", sound=chirp()),
        redrawn=make_video(folder / "redrawn.mkv", picture=(2.0, 5.5), sound=chirp()),
    )


@pytest.fixture(autouse=True)
def no_cache():
    scan_cache.configure(None)
    yield
    scan_cache.configure(None)


@pytest.fixture
def scanned(monkeypatch):
    """The ids of the files whose picture was scanned, in order."""
    seen = []
    real = analysis._scan

    def spy(item, *args, **kwargs):
        seen.append(item.id)
        return real(item, *args, **kwargs)

    monkeypatch.setattr(analysis, "_scan", spy)
    return seen


def compare(catalog, media, references, window=PLEX):  # noqa: F811
    result = analyze(
        media.target,
        [window],
        references,
        store=catalog(media.target, *references),
        cancel_check=lambda: False,
        pause_check=lambda: False,
        progress_callback=None,
        ffmpeg_threads=1,
    )
    assert result.status == "complete", result.reasons
    return result


def suggestions(result):
    return [r for r in result.ranges if r.origin == "plex"]


def test_the_profile_names_the_reach_and_the_one_second_sound_search():
    assert PROFILE_SPEC["candidate"]["max_references"] == admission.MAX_REFERENCES == 3
    assert PROFILE_SPEC["candidate"]["extra_references"] == admission.EXTRA_REFERENCES == analysis.EXTRA_REFERENCES == 6
    assert PROFILE_SPEC["sound_offset"]["search_ms"] == analysis.SOUND_SEARCH_MS == 1000


# ---- the share a window needs, from the runs verified so far --------------------------------------------------------


def run(start, end, reference="a", offset=0):
    """A verified run of the shape the engine keeps: start, end, reference, picture offset, unspecified colour fields,
    anchors, sound offset, channels, speed, levels."""
    ref = SimpleNamespace(id=reference)
    return (start, end, ref, Fraction(offset), [], {}, Fraction(offset), {}, Fraction(1), {})


@pytest.mark.parametrize(
    ("runs", "confirmed"),
    [
        ([], False),
        ([run(1000, 2000)], False),  # a fifth
        ([run(1000, 3500)], True),  # exactly half
        ([run(1000, 3499)], False),  # a millisecond short
        ([run(1000, 2000), run(2500, 4000)], True),  # a hole of 500 ms between the two is filled: 1000 + 500 + 1500
        ([run(1000, 2000), run(5100, 6000)], False),  # a hole of 3.1 s is longer than may be filled
        ([run(1000, 3000, "a"), run(1500, 3000, "b")], False),  # the same stretch from two references counts once
        ([run(1000, 2400, "a"), run(2000, 3600, "b")], False),  # one that starts inside the one before it is dropped
        ([run(1000, 2400, "a"), run(2400, 3600, "b")], True),  # two references side by side add up: 1.4 + 1.2 s of 5
    ],
)
def test_a_window_is_confirmed_by_the_runs_that_stand_plus_the_short_holes_between_them(runs, confirmed):
    assert _window_confirmed(PLEX, runs, ()) is confirmed


def test_a_hole_the_owner_keeps_is_not_filled_when_deciding_whether_a_window_is_confirmed():
    runs = [run(1000, 2000), run(2500, 3900)]  # 1.0 + 1.4 s of 5 is short of half, and the 0.5 s hole between makes it
    assert _window_confirmed(PLEX, runs, ()) is True
    assert _window_confirmed(PLEX, runs, [(2100, 2400)]) is False  # a range the owner keeps lies across the hole


def test_runs_that_stand_are_in_target_order_and_never_take_a_reference_back_in_time():
    early, late = run(1000, 2000, "a", offset=500), run(3000, 4000, "a", offset=-1000)
    assert _select_runs([late, early]) == [early]  # the later run would play the reference backwards
    assert _select_runs([run(3000, 4000, "b"), run(1000, 2000, "a")]) == [run(1000, 2000, "a"), run(3000, 4000, "b")]


# ---- going on to further episodes -----------------------------------------------------------------------------------


def test_a_suggested_window_goes_on_to_the_next_episodes_when_the_nearest_confirm_nothing(catalog, media, scanned):  # noqa: F811
    result = compare(catalog, media, [*media.others[:3], media.repeats])
    assert overlaps(3000, 4000, skips(result)), result.reasons
    assert {e.reference_version_id for e in matches(result)} == {media.repeats.id}
    assert not suggestions(result)  # confirmed, so no bare suggestion is left
    assert scanned.count(media.repeats.id) >= 1


def test_the_nearest_decide_when_they_confirm_the_window_and_the_rest_are_never_read(catalog, media, scanned):  # noqa: F811
    result = compare(catalog, media, [media.repeats, *media.others[:4]])
    assert overlaps(3000, 4000, skips(result)), result.reasons
    assert {e.reference_version_id for e in matches(result)} == {media.repeats.id}
    assert not {media.others[2].id, media.others[3].id} & set(scanned)


def test_a_window_nothing_suggested_is_compared_with_the_nearest_only(catalog, media, scanned):  # noqa: F811
    result = compare(catalog, media, [*media.others[:3], media.repeats], CandidateWindow("intro", 1000, 6000, "test"))
    assert not skips(result) and not matches(result), result.ranges
    assert media.repeats.id not in scanned
    assert any(reason.startswith("Reprise compared this with 3 other episodes") for reason in result.reasons), (
        result.reasons
    )


def test_a_further_episode_confirms_only_what_is_identical_in_picture_and_sound(catalog, media, scanned):  # noqa: F811
    result = compare(catalog, media, [*media.others[:3], media.redrawn])
    assert media.redrawn.id in scanned  # it was searched, and its sound is the target's
    assert not matches(result), result.ranges  # but a changed picture confirms nothing
    assert [(r.kind, r.start_ms, r.end_ms, r.review_status) for r in suggestions(result)] == [
        ("intro", 1000, 6000, "proposed")
    ]


@pytest.mark.parametrize(("before", "found"), [(4, True), (5, False)])
def test_no_episode_after_the_allowed_number_is_read(catalog, media, scanned, monkeypatch, before, found):  # noqa: F811
    monkeypatch.setattr(analysis, "MAX_REFERENCES", 2)
    monkeypatch.setattr(analysis, "EXTRA_REFERENCES", 3)  # two nearest, two more, one more: the fifth is the last read
    result = compare(catalog, media, [*media.others[:before], media.repeats])
    if found:
        assert overlaps(3000, 4000, skips(result)), result.reasons
        assert {e.reference_version_id for e in matches(result)} == {media.repeats.id}
    else:
        assert not matches(result) and suggestions(result), result.reasons
        assert media.repeats.id not in scanned
        assert all(other.id in scanned for other in media.others[:5])


def test_further_episodes_are_taken_a_batch_at_a_time_and_the_batch_that_confirms_is_the_last(
    catalog,  # noqa: F811
    media,
    scanned,
    monkeypatch,
):
    monkeypatch.setattr(analysis, "MAX_REFERENCES", 2)
    monkeypatch.setattr(analysis, "EXTRA_REFERENCES", 4)
    references = [media.others[0], media.others[1], media.repeats, media.others[2], *media.others[3:5]]
    result = compare(catalog, media, references)
    assert {e.reference_version_id for e in matches(result)} == {media.repeats.id}
    assert media.others[2].id in scanned  # the second of the batch that held the match is read with it
    assert not {media.others[3].id, media.others[4].id} & set(scanned)  # the next batch is not


def test_an_episode_that_cannot_be_read_is_replaced_and_does_not_use_up_the_allowance(
    catalog,  # noqa: F811
    media,
    monkeypatch,
):
    monkeypatch.setattr(analysis, "MAX_REFERENCES", 2)
    monkeypatch.setattr(analysis, "EXTRA_REFERENCES", 2)
    damaged = media.others[2]
    real = analysis._scan

    def scan(item, *args, **kwargs):
        if item.id == damaged.id:
            raise decoder.DecodeError("damaged reference")
        return real(item, *args, **kwargs)

    monkeypatch.setattr(analysis, "_scan", scan)
    references = [media.others[0], media.others[1], damaged, media.others[3], media.repeats]
    result = compare(catalog, media, references)
    assert overlaps(3000, 4000, skips(result)), result.reasons
    assert {e.reference_version_id for e in matches(result)} == {media.repeats.id}
    assert any("could not be read, so it was left out" in reason for reason in result.reasons), result.reasons
