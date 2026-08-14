from __future__ import annotations

import inspect
from abc import ABC

from app.domain.entities.image import Image
from app.domain.repositories.image_repository import ImageRepository
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.indexing_record import IndexingRecord

ALL_METHODS = (
    "save",
    "get",
    "exists",
    "delete",
    "list",
    "save_indexed",
    "get_index_metadata",
)


def test_image_repository_is_abstract() -> None:
    assert issubclass(ImageRepository, ABC)
    assert ImageRepository.__abstractmethods__ == {
        "save",
        "get",
        "exists",
        "delete",
        "list",
        "save_indexed",
        "get_index_metadata",
    }


def test_image_repository_cannot_be_instantiated() -> None:
    try:
        ImageRepository()
    except TypeError:
        pass
    else:
        raise AssertionError("ImageRepository should not be instantiable")


def test_image_repository_methods_use_domain_types_only() -> None:
    signatures = {
        name: inspect.signature(getattr(ImageRepository, name)) for name in ALL_METHODS
    }

    assert signatures["save"].parameters["image"].annotation is Image
    assert signatures["get"].parameters["image_id"].annotation is ImageId
    assert signatures["exists"].parameters["image_id"].annotation is ImageId
    assert signatures["delete"].parameters["image_id"].annotation is ImageId
    assert signatures["list"].return_annotation == list[Image]
    assert signatures["save_indexed"].parameters["record"].annotation is IndexingRecord
    assert signatures["get_index_metadata"].parameters["image_id"].annotation is ImageId


def test_image_repository_methods_are_abstract() -> None:
    for method_name in ALL_METHODS:
        method = getattr(ImageRepository, method_name)
        assert getattr(method, "__isabstractmethod__", False) is True
