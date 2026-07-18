from __future__ import annotations

from pathlib import Path

import pytest

from app.domain.exceptions import InvalidImagePathError
from app.domain.value_objects.image_path import ImagePath


def test_image_path_accepts_valid_paths() -> None:
    image_path = ImagePath("images/example.png")

    assert image_path.value == Path("images/example.png")
    assert str(image_path) == "images/example.png"


def test_image_path_normalizes_separators() -> None:
    image_path = ImagePath("images\\example.png")

    assert image_path.value == Path("images/example.png")


def test_image_path_rejects_empty_paths() -> None:
    with pytest.raises(InvalidImagePathError):
        ImagePath("")


def test_image_path_does_not_require_file_to_exist() -> None:
    image_path = ImagePath("missing/image.png")

    assert image_path.value == Path("missing/image.png")
