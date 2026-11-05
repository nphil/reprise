"""The exact speed-ups of an analysis: references searched together, a bar that moves, the audit read beside the decode.

None of them may change what an analysis returns: the same draft comes back whatever the number of workers, with the
scan cache on or off, and whichever search finishes first.
"""

import threading
import time

import pytest

from media_preview_generator.reprise import analysis, scan_cache
from media_preview_generator.reprise import media as decoder
from media_preview_generator.reprise.analysis import analyze
from media_preview_generator.reprise.models import CandidateWindow
from tests.reprise.test_analysis_recurrence import CHIRP, catalog, make_video  # noqa: F401 - catalog is a fixture

WINDOWS = [CandidateWindow("intro", 1000, 6000, "test")]
# addopts has --timeout=30 (method thread: a test past it kills its xdist worker outright, "node down"). The first test
# runs four analyses of three references, about 700 FFmpeg and ffprobe processes and 35 CPU-seconds, which a shared
# 4-core CI runner cannot always do in 30 s.
pytestmark = pytest.mark.timeout(150)


@pytest.fixture
def season(tmp_path, catalog):  # noqa: F811
    """A target and three references: one that repeats it, one that repeats it but for a changed picture, one with
    other sound."""
    target = make_video(tmp_path / "target.mkv", sound=f"aevalsrc={CHIRP}:s=48000:d=7")
    same = make_video(tmp_path / "same.mkv", sound=f"aevalsrc={CHIRP}:s=48000:d=7")
    picture = make_video(tmp_path / "picture.mkv", picture=(3, 4), sound=f"aevalsrc={CHIRP}:s=48000:d=7")
    sound = make_video(tmp_path / "sound.mkv", sound="aevalsrc=0.3*sin(2*PI*(300*t+9*t*t)):s=48000:d=7")
    return target, [same, picture, sound], catalog(target, same, picture, sound)


def run(season, progress=None, cancel=None, windows=WINDOWS):
    target, references, store = season
    return analyze(
        target,
        windows,
        references,
        store=store,
        cancel_check=cancel or (lambda: False),
        pause_check=lambda: False,
        progress_callback=progress,
        ffmpeg_threads=1,
    )


@pytest.fixture(autouse=True)
def no_cache():
    scan_cache.configure(None)
    yield
    scan_cache.configure(None)


def test_the_same_draft_comes_back_for_one_worker_or_three_with_and_without_the_scan_cache(
    season, monkeypatch, tmp_path
):
    monkeypatch.setattr(analysis, "SEARCH_WORKERS", 1)
    sequential = run(season)
    assert sequential.status == "complete" and any(r.action == "skip" for r in sequential.ranges)
    monkeypatch.setattr(analysis, "SEARCH_WORKERS", 3)
    assert run(season) == sequential
    scan_cache.configure(str(tmp_path / "scan.db"))
    assert run(season) == sequential  # fills the cache
    assert scan_cache.cache.hits == 0
    assert run(season) == sequential  # answered from it
    assert scan_cache.cache.hits >= 4


def test_the_first_search_to_fail_in_order_is_the_failure_whichever_finishes_first(season, monkeypatch):
    real = analysis._scan
    references = season[1]

    def scan(item, stream, start, end, cancel, pause, threads, **kwargs):
        if item.id == references[1].id:  # slow, and first in order
            time.sleep(0.4)
            raise decoder.DecodeError("first by order")
        if item.id == references[2].id:  # quick, later in order
            raise decoder.DecodeError("second by order")
        return real(item, stream, start, end, cancel, pause, threads, **kwargs)

    monkeypatch.setattr(analysis, "_scan", scan)
    monkeypatch.setattr(analysis, "SEARCH_WORKERS", 3)
    result = run(season)
    assert result.status == "failed" and "first by order" in result.reasons


def test_searches_after_a_failure_are_stopped_but_earlier_ones_finish(season, monkeypatch):
    real = analysis._scan
    references = season[1]
    seen = {}

    def scan(item, stream, start, end, cancel, pause, threads, **kwargs):
        if item.id == references[0].id:
            deadline = time.time() + 5
            while not cancel() and time.time() < deadline:  # waits for the failure of a later search to stop it
                time.sleep(0.02)
            seen["first_was_stopped"] = cancel()
            return real(item, stream, start, end, lambda: False, pause, threads, **kwargs)
        if item.id == references[1].id:
            raise decoder.DecodeError("boom")
        seen["third_was_stopped"] = True
        return real(item, stream, start, end, cancel, pause, threads, **kwargs)

    monkeypatch.setattr(analysis, "_scan", scan)
    monkeypatch.setattr(analysis, "SEARCH_WORKERS", 3)
    result = run(season)
    assert result.status == "failed" and "boom" in result.reasons
    assert seen["first_was_stopped"] is False  # an earlier search is not cut short by a later one's failure


def test_a_cancellation_reaches_every_search(season, monkeypatch):
    monkeypatch.setattr(analysis, "SEARCH_WORKERS", 3)
    started = threading.Event()
    real = analysis._scan

    def scan(item, stream, start, end, cancel, pause, threads, **kwargs):
        started.set()
        return real(item, stream, start, end, cancel, pause, threads, **kwargs)

    monkeypatch.setattr(analysis, "_scan", scan)
    result = run(season, cancel=lambda: started.is_set())
    assert result.status == "cancelled"


@pytest.mark.parametrize("workers", [1, 3])
def test_the_bar_moves_before_the_first_reference_is_done_and_never_goes_back(season, monkeypatch, workers):
    monkeypatch.setattr(analysis, "SEARCH_WORKERS", workers)
    monkeypatch.setattr(analysis, "PROGRESS_EVERY_FRAMES", 5)
    steps = []
    result = run(season, progress=lambda percent, message: steps.append((percent, message)))
    assert result.status == "complete"
    percents = [p for p, _ in steps]
    assert percents == sorted(percents) and percents[0] == 0 and percents[-1] == 100
    assert all(0 <= p <= 100 for p in percents)
    # It used to stay at 0 until the second reference began: now it moves through the target's scan and the first
    # reference's, before the message says the second one is being compared.
    first_two = next((i for i, (_, m) in enumerate(steps) if "episode 2 of" in m), len(steps))
    assert len({p for p in percents[:first_two]}) >= 3 or workers > 1
    assert len(set(percents)) >= 6


def test_the_search_loop_never_waits_on_a_finished_future(season, monkeypatch):
    """Waiting on a set that holds a finished future returns at once: a busy loop that starves the searches of the GIL
    (it made an analysis five times slower)."""
    real = analysis.wait
    seen = []

    def spy(futures, **kwargs):
        seen.append(all(not f.done() for f in futures))
        return real(futures, **kwargs)

    monkeypatch.setattr(analysis, "wait", spy)
    monkeypatch.setattr(analysis, "SEARCH_WORKERS", 1)
    assert run(season).status == "complete"
    assert seen and all(seen)


def test_a_window_without_references_still_reports_through_the_end(tmp_path, catalog):  # noqa: F811
    target = make_video(tmp_path / "target.mkv")
    steps = []
    result = analyze(
        target,
        WINDOWS,
        [],
        store=catalog(target),
        cancel_check=lambda: False,
        pause_check=lambda: False,
        progress_callback=lambda percent, message: steps.append(percent),
        ffmpeg_threads=1,
    )
    assert result.status == "complete" and steps[-1] == 100


# ---- the audit read beside the decode ---------------------------------------------------------------------------------


def test_a_pending_audit_waits_on_first_use_and_re_raises_its_failure():
    gate = threading.Event()

    def slow():
        gate.wait(5)
        return {1, 2, 3}

    pending = decoder._PendingAudit(slow)
    gate.set()
    assert 2 in pending and sorted(pending) == [1, 2, 3]

    def failing():
        raise decoder.DecodeError("Ambiguous native video chroma audit")

    broken = decoder._PendingAudit(failing)
    with pytest.raises(decoder.DecodeError, match="Ambiguous"):
        broken.result()
    with pytest.raises(decoder.DecodeError, match="Ambiguous"):
        broken.raise_if_failed()


def test_closing_an_audit_waits_for_its_ffprobe_instead_of_leaving_it_behind():
    finished = []

    def work():
        time.sleep(0.2)
        finished.append(True)
        return {1}

    pending = decoder._PendingAudit(work)
    time.sleep(0.05)
    pending.close()
    assert finished == [True]
    decoder._close_audit(None)  # nothing to close


def test_a_failed_audit_is_the_error_even_when_the_stream_is_never_read(tmp_path, monkeypatch):
    media = make_video(tmp_path / "episode.mkv")
    stream = decoder.probe(media, lambda: False, lambda: False)[0]
    real = decoder._native_audit

    def audit(*args):
        result = list(real(*args))
        result[10] = decoder._PendingAudit(
            lambda: (_ for _ in ()).throw(decoder.DecodeError("Empty native video chroma audit"))
        )
        return tuple(result)

    monkeypatch.setattr(decoder, "_native_audit", audit)
    monkeypatch.setattr(decoder.acceleration, "plan", lambda ffmpeg: [])
    with pytest.raises(decoder.DecodeError, match="Empty native video chroma audit"):
        with decoder.native_frames(media, stream, 0, 1000, lambda: False, lambda: False, 1):
            pass


def test_frames_are_not_accepted_when_the_audit_disagrees_with_the_decode(tmp_path, monkeypatch):
    media = make_video(tmp_path / "episode.mkv")
    stream = decoder.probe(media, lambda: False, lambda: False)[0]
    real = decoder._native_audit

    def audit(*args):
        result = list(real(*args))
        genuine = set(result[10])
        result[10] = decoder._PendingAudit(lambda: set(sorted(genuine)[:-1]))  # one decoded frame was never audited
        return tuple(result)

    monkeypatch.setattr(decoder, "_native_audit", audit)
    monkeypatch.setattr(decoder.acceleration, "plan", lambda ffmpeg: [])
    with pytest.raises(decoder.DecodeError, match="does not match chroma audit"):
        with decoder.native_frames(media, stream, 0, 7000, lambda: False, lambda: False, 1) as frames:
            list(frames)


# ---- spreading searches over decoders (off unless asked: measured slower, see DESIGN.md) ------------------------------


def test_decoders_rotate_by_search_slot_only_when_spreading_is_on(monkeypatch):
    from media_preview_generator.reprise import acceleration

    p40 = acceleration.Accelerator("cuda", "cuda:0", "NVIDIA", "Tesla P40", ())
    intel = acceleration.Accelerator("vaapi", "/dev/dri/renderD129", "INTEL", "UHD 630", ())
    devices = [p40, intel]
    monkeypatch.delenv("REPRISE_SPREAD_DECODERS", raising=False)
    with acceleration.pinned(1):
        assert acceleration.order(devices) == devices  # off by default
    monkeypatch.setenv("REPRISE_SPREAD_DECODERS", "1")
    assert acceleration.order(devices) == devices  # no slot: not inside a search
    with acceleration.pinned(0):
        assert acceleration.order(devices) == [p40, intel]
    with acceleration.pinned(1):
        assert acceleration.order(devices) == [intel, p40]
        assert acceleration.order([p40]) == [p40]  # one device: nothing to spread
    with acceleration.pinned(2):
        assert acceleration.order(devices) == [p40, intel]
    assert acceleration.order(devices) == devices  # the slot does not outlive its search
