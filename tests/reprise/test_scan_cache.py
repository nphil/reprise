"""The scan cache: a hit is the scan, exactly; a stale or foreign entry is never served; the store stays bounded."""

import json
import sqlite3
from fractions import Fraction
from types import SimpleNamespace

import numpy as np
import pytest

from media_preview_generator.reprise import analysis, scan_cache
from media_preview_generator.reprise import media as decoder
from media_preview_generator.reprise.verification_profile import PROFILE_FINGERPRINT
from tests.reprise.test_analysis_recurrence import make_video

DECLARATIONS = (1920, 1080, "yuv420p10le", 10, Fraction(1, 1), "tv", "bt709", "bt709", "bt709", "left", ())
UNSPECIFIED = ["color_space"]


def item(version="v1", path="/media/a.mkv"):
    return SimpleNamespace(id=version, canonical_path=path)


def frames(start, end, step=Fraction(1001, 24), seed=0):
    rng = np.random.default_rng(seed)
    out, pts = [], Fraction(start)
    while pts < end:
        out.append((pts, rng.random(48).astype(np.float32) * 255, float(rng.random() * 40)))
        pts += step
    return out


@pytest.fixture
def cache(tmp_path):
    kept = scan_cache.ScanCache(str(tmp_path / "scan.db"), max_bytes=10**9)
    yield kept
    kept.close()


def same(left, right):
    assert len(left) == len(right)
    for a, b in zip(left, right, strict=True):
        assert a[0] == b[0] and a[2] == b[2] and np.array_equal(a[1], b[1]) and a[1].dtype == b[1].dtype


def test_a_stored_scan_comes_back_as_it_went_in(cache):
    hints = frames(1000, 9000)
    cache.store(item(), 0, 1000, 9000, hints, DECLARATIONS, UNSPECIFIED)
    got, declarations, unspecified = cache.lookup(item(), 0, 1000, 9000)
    same(got, hints)
    assert declarations == DECLARATIONS and unspecified == UNSPECIFIED


def test_a_window_inside_a_stored_one_is_the_stored_frames_in_it(cache):
    hints = frames(1000, 9000)
    cache.store(item(), 0, 1000, 9000, hints, DECLARATIONS, UNSPECIFIED)
    got, *_ = cache.lookup(item(), 0, 2500, 7000)
    same(got, [h for h in hints if 2500 <= h[0] < 7000])
    assert cache.lookup(item(), 0, 900, 7000) is None  # starts before what was scanned
    assert cache.lookup(item(), 0, 2500, 9001) is None  # ends after it
    assert cache.lookup(item(), 1, 2500, 7000) is None  # another stream of the file


def test_overlapping_and_touching_scans_merge_but_a_gap_does_not(cache):
    whole = frames(0, 12000)
    for lo, hi in ((0, 5000), (4000, 8000), (8000, 12000)):
        cache.store(item(), 0, lo, hi, [h for h in whole if lo <= h[0] < hi], DECLARATIONS, UNSPECIFIED)
    got, *_ = cache.lookup(item(), 0, 1000, 11000)  # spans all three
    same(got, [h for h in whole if 1000 <= h[0] < 11000])
    other = item("v2", "/media/b.mkv")
    for lo, hi in ((0, 3000), (6000, 9000)):
        cache.store(other, 0, lo, hi, [h for h in whole if lo <= h[0] < hi], DECLARATIONS, UNSPECIFIED)
    assert cache.lookup(other, 0, 2000, 7000) is None  # the hole between 3000 and 6000 was never scanned
    assert cache.lookup(other, 0, 6500, 8500) is not None


def test_scans_with_different_declarations_are_not_merged(cache):
    hints = frames(0, 6000)
    cache.store(item(), 0, 0, 3000, [h for h in hints if h[0] < 3000], DECLARATIONS, UNSPECIFIED)
    cache.store(item(), 0, 2000, 6000, [h for h in hints if h[0] >= 2000], (*DECLARATIONS[:-2], "top", ()), [])
    assert cache.lookup(item(), 0, 500, 5500) is None


def test_a_scan_with_a_frame_outside_its_window_or_no_frames_is_not_kept(cache):
    cache.store(item(), 0, 2000, 4000, frames(1000, 3000), DECLARATIONS, UNSPECIFIED)
    cache.store(item(), 0, 2000, 4000, [], None, [])
    assert cache.lookup(item(), 0, 2000, 4000) is None


def test_another_generation_of_the_file_is_never_served_and_replaces_the_old_one(cache):
    hints = frames(0, 5000)
    cache.store(item("v1"), 0, 0, 5000, hints, DECLARATIONS, UNSPECIFIED)
    assert cache.lookup(item("v2"), 0, 0, 5000) is None
    cache.store(item("v2"), 0, 0, 5000, hints, DECLARATIONS, UNSPECIFIED)
    assert cache.lookup(item("v1"), 0, 0, 5000) is None  # the old generation of /media/a.mkv is gone
    assert cache.lookup(item("v2"), 0, 0, 5000) is not None
    cache.store(item("v3", "/media/other.mkv"), 0, 0, 5000, hints, DECLARATIONS, UNSPECIFIED)
    assert cache.lookup(item("v2"), 0, 0, 5000) is not None  # a different path is not touched


def test_hints_of_another_profile_or_format_are_dropped_when_the_cache_opens(tmp_path):
    path = str(tmp_path / "scan.db")
    first = scan_cache.ScanCache(path)
    first.store(item(), 0, 0, 5000, frames(0, 5000), DECLARATIONS, UNSPECIFIED)
    first.close()
    db = sqlite3.connect(path)
    db.execute("UPDATE scans SET fingerprint='an older profile'")
    db.commit()
    db.close()
    second = scan_cache.ScanCache(path)
    assert second.lookup(item(), 0, 0, 5000) is None and second.size() == 0
    second.close()
    assert PROFILE_FINGERPRINT != "an older profile"


def test_the_cache_stays_under_its_bound_and_drops_the_least_recently_used(tmp_path):
    one = len(scan_cache._pack(frames(0, 5000)))
    kept = scan_cache.ScanCache(str(tmp_path / "scan.db"), max_bytes=int(one * 3.5))
    for n in range(4):
        kept.store(item(f"v{n}", f"/media/{n}.mkv"), 0, 0, 5000, frames(0, 5000, seed=n), DECLARATIONS, UNSPECIFIED)
        assert kept.lookup(item("v0", "/media/0.mkv"), 0, 0, 5000) is not None  # v0 is used every round
    assert kept.size() <= one * 3.5
    assert kept.lookup(item("v0", "/media/0.mkv"), 0, 0, 5000) is not None
    assert kept.lookup(item("v1", "/media/1.mkv"), 0, 0, 5000) is None  # the least recently used went
    kept.close()


def test_a_storage_error_turns_the_cache_off_instead_of_failing_a_scan(tmp_path):
    blocked = tmp_path / "file"
    blocked.write_text("not a directory")
    kept = scan_cache.ScanCache(str(blocked / "scan.db"))
    assert kept.lookup(item(), 0, 0, 5000) is None
    kept.store(item(), 0, 0, 5000, frames(0, 5000), DECLARATIONS, UNSPECIFIED)
    assert kept.lookup(item(), 0, 0, 5000) is None and kept._failed


def test_configure_keeps_one_cache_per_path_and_none_turns_it_off(tmp_path):
    try:
        scan_cache.configure(str(tmp_path / "a.db"))
        first = scan_cache.cache
        scan_cache.configure(str(tmp_path / "a.db"))
        assert scan_cache.cache is first
        scan_cache.configure(None)
        assert scan_cache.cache is None
    finally:
        scan_cache.configure(None)


# ---- against real decodes ---------------------------------------------------------------------------------------------


def direct(media, stream, start, end):
    scan_cache.configure(None)
    return analysis._scan(media, stream, start, end, lambda: False, lambda: False, 1)


def test_a_cache_hit_is_the_scan_a_decode_of_that_window_returns(tmp_path):
    media = make_video(tmp_path / "episode.mkv")
    stream = decoder.probe(media, lambda: False, lambda: False)[0]
    try:
        scan_cache.configure(str(tmp_path / "scan.db"))
        wide = analysis._scan(media, stream, 500, 6500, lambda: False, lambda: False, 1)  # a miss, stored
        assert scan_cache.cache.hits == 0 and scan_cache.cache.misses == 1
        for start, end in ((500, 6500), (1000, 6000), (2000, 3000), (1234, 5678)):
            scan_cache.configure(str(tmp_path / "scan.db"))
            hit = analysis._scan(media, stream, start, end, lambda: False, lambda: False, 1)
            assert scan_cache.cache.hits == 1
            expected = direct(media, stream, start, end)
            same(hit[0], expected[0])
            assert hit[1:] == expected[1:]
            scan_cache.configure(str(tmp_path / "scan.db"))
        assert wide[0]
    finally:
        scan_cache.configure(None)


def test_a_hit_still_checks_that_the_file_is_the_generation_that_was_scanned(tmp_path):
    media = make_video(tmp_path / "episode.mkv")
    stream = decoder.probe(media, lambda: False, lambda: False)[0]
    try:
        scan_cache.configure(str(tmp_path / "scan.db"))
        analysis._scan(media, stream, 500, 6500, lambda: False, lambda: False, 1)
        (tmp_path / "episode.mkv").write_bytes((tmp_path / "episode.mkv").read_bytes() + b"\0")
        with pytest.raises(decoder.DecodeError, match="changed"):
            analysis._scan(media, stream, 1000, 2000, lambda: False, lambda: False, 1)
    finally:
        scan_cache.configure(None)


def test_declarations_round_trip_through_json():
    encoded = scan_cache._encode(DECLARATIONS)
    assert scan_cache._decode(json.loads(json.dumps(encoded))) == DECLARATIONS
