"""Check the dimensions and format of the existing rendered image assets."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

REPO_ROOT = Path(__file__).resolve().parent.parent
SITE_IMG = REPO_ROOT / "docs" / "assets" / "img"
APP_IMG = REPO_ROOT / "media_preview_generator" / "web" / "static" / "images"


@pytest.mark.parametrize(
    ("path", "size"),
    [
        (SITE_IMG / "favicon-32.png", 32),
        (SITE_IMG / "apple-touch-icon.png", 180),
        (REPO_ROOT / "docs" / "images" / "icon.png", 512),
        (APP_IMG / "favicon-32.png", 32),
        (APP_IMG / "icon.png", 512),
    ],
    ids=["site-favicon-32", "apple-touch-180", "docs-icon-512", "app-favicon-32", "app-icon-512"],
)
def test_rendered_pngs_have_their_declared_size(path: Path, size: int) -> None:
    with Image.open(path) as image:
        assert image.size == (size, size)
        assert image.mode == "RGBA"
