"""This image's FFmpeg is built without Vulkan on purpose: it is read from the build recipe, and says so quietly."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest
from loguru import logger

from media_preview_generator.gpu import ffmpeg_capabilities as caps
from media_preview_generator.gpu import vulkan_probe

IMAGE_RECIPE = "--prefix=/opt/ffmpeg-gpl\n--disable-autodetect\n--enable-gpl\n--enable-nvdec\n--enable-vaapi\n"


def _ffmpeg_tree(tmp_path, recipe: str | None):
    """An install laid out like the image's: ``bin/ffmpeg`` and ``share/source/ffmpeg/configure-args.txt``."""
    binary = tmp_path / "opt" / "ffmpeg-gpl" / "bin" / "ffmpeg"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    if recipe is not None:
        recipe_file = tmp_path / "opt" / "ffmpeg-gpl" / "share" / "source" / "ffmpeg" / "configure-args.txt"
        recipe_file.parent.mkdir(parents=True)
        recipe_file.write_text(recipe)
    link = tmp_path / "usr" / "local" / "bin" / "ffmpeg"
    link.parent.mkdir(parents=True)
    link.symlink_to(binary)
    return link


class TestRecipe:
    @pytest.fixture
    def run_with(self, monkeypatch):
        def run(binary) -> bool:
            monkeypatch.setattr(caps, "ffmpeg_binary", lambda: str(binary))
            return caps.ffmpeg_vulkan_left_out()

        return run

    def test_the_images_recipe_without_vulkan_is_a_deliberate_omission(self, tmp_path, run_with):
        assert run_with(_ffmpeg_tree(tmp_path, IMAGE_RECIPE)) is True

    @pytest.mark.parametrize("flag", ["--enable-vulkan", "--enable-libplacebo\n--enable-VULKAN"])
    def test_a_recipe_that_names_vulkan_is_a_build_that_should_have_it(self, tmp_path, run_with, flag):
        assert run_with(_ffmpeg_tree(tmp_path, IMAGE_RECIPE + flag + "\n")) is False

    @pytest.mark.parametrize("recipe", [None, "", "\n\n"])
    def test_no_recipe_means_nothing_is_known_to_be_deliberate(self, tmp_path, run_with, recipe):
        assert run_with(_ffmpeg_tree(tmp_path, recipe)) is False

    def test_a_missing_binary_is_not_deliberate(self, tmp_path, run_with):
        assert run_with(tmp_path / "nowhere" / "ffmpeg") is False

    def test_a_system_ffmpeg_with_no_recipe_beside_it_is_not_deliberate(self, tmp_path, run_with):
        binary = tmp_path / "usr" / "bin" / "ffmpeg"
        binary.parent.mkdir(parents=True)
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
        assert run_with(binary) is False
        assert os.path.exists(binary)


def _capture(level: str):
    messages: list[tuple[str, str]] = []
    sink = logger.add(lambda m: messages.append((m.record["level"].name, m.record["message"])), level=level)
    return messages, sink


class TestProbe:
    @pytest.mark.parametrize(
        ("has_vulkan", "recipe_leaves_out", "expected"),
        [(False, True, True), (False, False, False), (True, True, False), (True, False, False)],
    )
    def test_left_out_needs_both_no_vulkan_in_ffmpeg_and_a_recipe_that_says_so(
        self, has_vulkan, recipe_leaves_out, expected
    ):
        with (
            patch.object(vulkan_probe, "_is_hwaccel_available", return_value=has_vulkan),
            patch.object(vulkan_probe, "ffmpeg_vulkan_left_out", return_value=recipe_leaves_out),
        ):
            assert vulkan_probe._vulkan_left_out() is expected

    def test_nothing_is_probed_retried_or_warned_about(self):
        messages, sink = _capture("DEBUG")
        try:
            with (
                patch.object(vulkan_probe, "_is_hwaccel_available", return_value=False),
                patch.object(vulkan_probe, "ffmpeg_vulkan_left_out", return_value=True),
                patch.object(vulkan_probe.subprocess, "run") as run,
            ):
                assert vulkan_probe._probe_vulkan_device() is None
        finally:
            logger.remove(sink)

        run.assert_not_called()
        assert not [text for level, text in messages if level in ("WARNING", "ERROR")]

    def test_a_build_with_vulkan_that_finds_nothing_still_warns(self):
        nothing = MagicMock(returncode=0, stderr="")
        messages, sink = _capture("WARNING")
        try:
            with (
                patch.object(vulkan_probe, "ffmpeg_vulkan_left_out", return_value=False),
                patch.object(vulkan_probe, "_is_hwaccel_available", return_value=True),
                patch.object(vulkan_probe.subprocess, "run", return_value=nothing),
                patch.object(vulkan_probe, "_find_nvidia_icd_json", return_value=None),
                patch.object(vulkan_probe, "_find_nvidia_egl_vendor_json", return_value=None),
                patch.object(vulkan_probe, "_find_libegl_nvidia", return_value=None),
            ):
                assert vulkan_probe._probe_vulkan_device() is None
        finally:
            logger.remove(sink)

        assert any("couldn't be detected" in text for _, text in messages)

    @pytest.mark.parametrize(
        ("left_out", "expected", "unexpected"),
        [
            (True, "not part of this build's FFmpeg, on purpose", "Vulkan not available"),
            (False, "Vulkan not available in this container", "on purpose"),
        ],
    )
    def test_the_one_startup_line_says_which_kind_of_absence_it_is(self, left_out, expected, unexpected):
        vulkan_probe._reset_vulkan_device_cache()
        messages, sink = _capture("INFO")
        try:
            with (
                patch.object(vulkan_probe, "_probe_vulkan_device", return_value=None),
                patch.object(vulkan_probe, "_is_hwaccel_available", return_value=False),
                patch.object(vulkan_probe, "ffmpeg_vulkan_left_out", return_value=left_out),
            ):
                vulkan_probe.get_vulkan_device_info()
        finally:
            logger.remove(sink)
            vulkan_probe._reset_vulkan_device_cache()

        lines = [text for level, text in messages if "Vulkan" in text]
        assert len(lines) == 1 and expected in lines[0] and unexpected not in lines[0]
        assert all(level == "INFO" for level, text in messages if "Vulkan" in text)


class TestNotification:
    def test_a_neutral_note_never_becomes_a_notification_failure(self, monkeypatch):
        from media_preview_generator.gpu import vulkan_check as vc
        from media_preview_generator.web import notifications

        gpu = {"type": "NVIDIA", "name": "Tesla P40", "device": "cuda:0", "status": "ok"}
        note = vc.check_gpus(
            [gpu], vulkan_probe.VulkanProbeResult(None, False), ffmpeg_has_vulkan=False, vulkan_left_out=True
        )
        fault = vc.VulkanCheck(False, "driver_refused", "Refused", "Why", "Fix")
        monkeypatch.setattr("media_preview_generator.web.routes._helpers._ensure_gpu_cache", lambda: [gpu])
        monkeypatch.setattr(vc, "get_vulkan_checks", lambda gpus, diag=None: note)
        assert notifications._vulkan_failures() == []

        monkeypatch.setattr(vc, "get_vulkan_checks", lambda gpus, diag=None: [fault])
        assert notifications._vulkan_failures() == [(gpu, fault)]
