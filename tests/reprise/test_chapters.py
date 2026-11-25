"""The video file's own chapter marks as suggested intro and credits times."""

import subprocess

from media_preview_generator.reprise import chapters
from media_preview_generator.reprise.analysis import _with_online
from media_preview_generator.reprise.models import CandidateWindow, OnlineWindow

NAME = chapters.SOURCE_NAME
MIN = 60000


def episode(opening=(0, 90000), ending=(1290000, 1380000), tail=(("Preview", 1380000, 1440000),)):
    return [
        ("Opening", *opening),
        ("Part A", opening[1], ending[0]),
        ("Ending", *ending),
        *tail,
    ]


def test_the_ending_runs_to_the_file_end_when_only_a_preview_follows():
    found = chapters.spans(episode(), 1440000)
    assert found == {"intro": (0, 90000, 90000), "credits": (1290000, 1440000, 90000)}


def test_the_ending_stops_at_its_chapter_when_something_else_follows():
    found = chapters.spans(episode(tail=(("Post-credits scene", 1380000, 1440000),)), 1440000)
    assert found["credits"] == (1290000, 1380000, 90000)


def test_an_ending_with_nothing_after_it_runs_to_the_file_end_even_a_little_short():
    found = chapters.spans(episode(ending=(1290000, 1439500), tail=()), 1440000)
    assert found["credits"] == (1290000, 1440000, 149500)


def test_titles_are_read_loosely_and_other_titles_are_not_taken():
    for title in ("ED", "End Credits", "Outro", "Ending 2", "credits"):
        assert "credits" in chapters.spans([("Part", 0, 100000), (title, 100000, 160000)], 160000), title
    for title in ("OP", "Intro", "Opening"):
        assert "intro" in chapters.spans([(title, 0, 60000), ("Part", 60000, 160000)], 160000), title
    assert chapters.spans([("Chapter 1", 0, 100000), ("Scene", 100000, 160000)], 160000) == {}


def test_a_chapter_that_does_not_fit_the_file_is_ignored():
    assert chapters.spans([("Ending", 160000, 200000)], 150000) == {}


def test_a_chapter_that_is_not_the_shows_usual_length_is_flagged_on_the_target_only():
    files = {f"e{n}": episode() for n in range(5)}
    files["odd"] = episode(ending=(1290000, 1365000), tail=())
    durations = {mid: 1440000 for mid in files}
    found = chapters.windows(files, durations, "odd")
    assert "75.0 s long" in found["odd"][1].note and "about 90.0 s" in found["odd"][1].note
    assert all(w.note == "" for w in found["e0"])
    assert all(w.note == "" for w in chapters.windows(files, durations, "e0")["e0"])


def test_a_show_with_too_few_chapters_or_scattered_lengths_flags_nothing():
    few = {"a": episode(ending=(1290000, 1365000)), "b": episode()}
    assert all(w.note == "" for w in chapters.windows(few, {"a": 1440000, "b": 1440000}, "a")["a"])
    mixed = {
        "a": episode(),
        "b": episode(ending=(1290000, 1330000)),
        "c": episode(ending=(1290000, 1450000)),
        "d": episode(),
    }
    assert all(w.note == "" for w in chapters.windows(mixed, dict.fromkeys(mixed, 1440000), "d")["d"])
    sparse = {"a": episode(), "b": [("Part", 0, 1440000)], "c": [("Part", 0, 1440000)], "d": [("Part", 0, 1440000)]}
    assert all(w.note == "" for w in chapters.windows(sparse, dict.fromkeys(sparse, 1440000), "a")["a"])


def test_movies_ask_for_credits_only():
    found = chapters.windows({"m": episode()}, {"m": 1440000}, "m", kinds=("credits",))
    assert [w.kind for w in found["m"]] == ["credits"]


def test_a_database_time_keeps_its_place_and_names_the_chapters_when_they_agree():
    db = {"m": (OnlineWindow("credits", 1291000, 1440000, ("IntroDB",)),)}
    ours = chapters.windows({"m": episode()}, {"m": 1440000}, "m")
    merged = chapters.merge(db, ours)
    credits = [w for w in merged["m"] if w.kind == "credits"]
    assert [(w.start_ms, w.sources) for w in credits] == [(1291000, ("IntroDB", NAME))]
    assert [w.kind for w in merged["m"]] == ["credits", "intro"]
    far = {"m": (OnlineWindow("credits", 1000000, 1100000, ("IntroDB",)),)}
    assert [(w.start_ms, w.sources) for w in chapters.merge(far, ours)["m"] if w.kind == "credits"] == [
        (1000000, ("IntroDB",))
    ]


def test_a_chapter_note_reaches_the_result_notes():
    window = OnlineWindow("credits", 1290000, 1440000, (NAME,), note="check it")
    windows, names, notes = _with_online([CandidateWindow("credits", 1300000, 1440000, "plex")], (window,))
    assert "check it" in notes


def test_chapters_are_read_from_a_real_file(tmp_path):
    meta = tmp_path / "m.txt"
    meta.write_text(
        ";FFMETADATA1\n"
        + "".join(
            f"[CHAPTER]\nTIMEBASE=1/1000\nSTART={a}\nEND={b}\ntitle={t}\n"
            for t, a, b in (("Opening", 0, 1000), ("Part", 1000, 3000), ("Ending", 3000, 4000), ("Preview", 4000, 5000))
        )
    )
    path = tmp_path / "x.mkv"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=64x64:r=10:d=5",
            "-i",
            str(meta),
            "-map_metadata",
            "1",
            "-c:v",
            "ffv1",
            "-y",
            str(path),
        ],
        check=True,
    )
    read = chapters.read("ffprobe", str(path))
    assert read == [("Opening", 0, 1000), ("Part", 1000, 3000), ("Ending", 3000, 4000), ("Preview", 4000, 5000)]
    assert chapters.spans(read, 5000)["credits"] == (3000, 5000, 1000)
    assert chapters.read("ffprobe", str(tmp_path / "missing.mkv")) == []
