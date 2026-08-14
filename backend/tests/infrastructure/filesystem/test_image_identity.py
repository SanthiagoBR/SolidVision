from __future__ import annotations

from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.infrastructure.filesystem.image_identity import (
    SOLIDVISION_PATH_NAMESPACE,
    compute_image_id,
)


def test_namespace_constant_is_frozen_to_its_expected_value() -> None:
    """Guard against accidental regeneration of the namespace constant.

    Changing this constant would silently change the `ImageId` produced
    for every already-indexed file. If this test fails because the
    constant's literal value changed, that change was not authorized by
    this test suite and must not be merged without an explicit,
    documented migration strategy.
    """
    assert str(SOLIDVISION_PATH_NAMESPACE) == "5dc64f53-522e-4303-951e-ee6123b10dd8"


def test_same_path_produces_same_id_across_multiple_calls() -> None:
    path = ImagePath("images/example.png")

    first = compute_image_id(path)
    second = compute_image_id(path)

    assert first == second


def test_different_paths_produce_different_ids() -> None:
    first = compute_image_id(ImagePath("images/one.png"))
    second = compute_image_id(ImagePath("images/two.png"))

    assert first != second


def test_backslash_and_forward_slash_paths_produce_the_same_id() -> None:
    windows_style = compute_image_id(ImagePath("images\\example.png"))
    posix_style = compute_image_id(ImagePath("images/example.png"))

    assert windows_style == posix_style


def test_compute_image_id_returns_image_id() -> None:
    result = compute_image_id(ImagePath("images/example.png"))

    assert isinstance(result, ImageId)
