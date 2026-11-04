"""A review-only setup (nothing is written into Plex) must not be told its Plex config folder is missing or wrong."""

from media_preview_generator.config.validation import _validate_plex_config, plex_config_folder_needed


def _plex(enabled: bool = True, **output) -> dict:
    return {"type": "plex", "enabled": enabled, "output": output}


def test_review_only_plex_needs_no_folder():
    assert plex_config_folder_needed([_plex(preview_thumbnails=False)]) is False


def test_preview_thumbnails_on_needs_the_folder():
    assert plex_config_folder_needed([_plex()]) is True
    assert plex_config_folder_needed([_plex(preview_thumbnails=True)]) is True


def test_chapter_thumbnails_need_the_folder():
    assert plex_config_folder_needed([_plex(preview_thumbnails=False, chapter_thumbnails=True)]) is True


def test_intro_credits_or_loudness_need_the_folder():
    base = _plex(preview_thumbnails=False)
    assert plex_config_folder_needed([{**base, "markers": {"enabled": True}}]) is True
    assert plex_config_folder_needed([{**base, "loudness": {"enabled": True}}]) is True
    assert plex_config_folder_needed([{**base, "markers": {"enabled": False}, "loudness": {}}]) is False


def test_disabled_plex_and_other_vendors_are_ignored():
    assert plex_config_folder_needed([_plex(enabled=False), {"type": "jellyfin", "enabled": True}]) is False
    assert plex_config_folder_needed([]) is False


def test_missing_folder_is_only_an_error_when_something_writes_into_plex(tmp_path):
    gone = str(tmp_path / "no-such-plex-folder")
    for check, expect_problem in ((True, True), (False, False)):
        missing: list = []
        errors: list = []
        _validate_plex_config("http://plex.invalid:32400", "token", gone, missing, errors, check_config_folder=check)
        assert bool(missing or errors) is expect_problem
