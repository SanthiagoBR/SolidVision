"""`inspect_query_image()`: what the model is spared (RFC-038 section 4.2).

The HTTP refusals are in `tests/presentation/test_search_by_image_api.py`;
these are the cases the route cannot produce, because its signature check
runs first.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from app.domain.exceptions import UnreadableQueryImageError
from app.infrastructure.filesystem.query_image import inspect_query_image


def picture(path: Path, format_: str) -> Path:
    Image.new("RGB", (16, 12), (10, 20, 30)).save(path, format=format_)
    return path


def test_a_jpeg_and_a_png_decode(tmp_path: Path) -> None:
    inspect_query_image(picture(tmp_path / "a.jpg", "JPEG"), "image/jpeg")
    inspect_query_image(picture(tmp_path / "a.png", "PNG"), "image/png")


def test_a_dji_jpeg_is_mpo_to_pillow_and_is_accepted(tmp_path: Path) -> None:
    """A JPEG with a preview inside, as a DJI drone writes it (RFC-038 §14)."""
    path = tmp_path / "DJI_0321.JPG"
    photo = Image.new("RGB", (64, 48), (10, 20, 30))
    preview = Image.new("RGB", (16, 12), (30, 20, 10))
    photo.save(path, format="MPO", save_all=True, append_images=[preview])
    with Image.open(path) as reopened:
        assert reopened.format == "MPO"
    assert path.read_bytes().startswith(b"\xff\xd8\xff")

    inspect_query_image(path, "image/jpeg")


def test_an_mpo_is_not_a_png(tmp_path: Path) -> None:
    path = tmp_path / "a.png"
    Image.new("RGB", (16, 12)).save(
        path, format="MPO", save_all=True, append_images=[Image.new("RGB", (8, 6))]
    )

    with pytest.raises(UnreadableQueryImageError):
        inspect_query_image(path, "image/png")


def test_a_format_other_than_the_declared_one_is_refused(tmp_path: Path) -> None:
    with pytest.raises(UnreadableQueryImageError, match="is PNG"):
        inspect_query_image(picture(tmp_path / "a.jpg", "PNG"), "image/jpeg")


def test_a_type_that_is_not_a_query_type_is_refused(tmp_path: Path) -> None:
    path = picture(tmp_path / "a.gif", "GIF")

    with pytest.raises(UnreadableQueryImageError):
        inspect_query_image(path, "image/gif")


def test_the_pixel_limit_is_checked_from_the_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = picture(tmp_path / "a.png", "PNG")
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 16 * 12)
    inspect_query_image(path, "image/png")

    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 16 * 12 - 1)
    with pytest.raises(UnreadableQueryImageError, match="16 x 12 pixels"):
        inspect_query_image(path, "image/png")
