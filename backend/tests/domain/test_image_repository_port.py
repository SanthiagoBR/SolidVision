from __future__ import annotations

import inspect
from abc import ABC
from collections.abc import Sequence

from app.domain.entities.image import Image
from app.domain.repositories.image_repository import ImageRepository
from app.domain.value_objects.embedding_vector import EmbeddingVector
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.index_metadata import IndexMetadata
from app.domain.value_objects.indexing_record import IndexingRecord
from app.domain.value_objects.search_hit import SearchHit

ALL_METHODS = (
    "save",
    "get",
    "exists",
    "delete",
    "list",
    "save_indexed",
    "save_indexed_many",
    "search_similar",
    "get_index_metadata",
    "get_index_metadata_many",
    "update_index_metadata",
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
        "save_indexed_many",
        "search_similar",
        "get_index_metadata",
        "get_index_metadata_many",
        "update_index_metadata",
    }


def test_image_repository_cannot_be_instantiated() -> None:
    try:
        ImageRepository()
    except TypeError:
        pass
    else:
        raise AssertionError("ImageRepository should not be instantiable")


def test_image_repository_methods_use_domain_types_only() -> None:
    # `eval_str` because the port declares `from __future__ import
    # annotations`, which it needs: `ImageRepository.list()` shadows the
    # builtin `list` inside the class body, so `-> list[SearchHit]` on a
    # method defined after it would be evaluated against the method.
    signatures = {
        name: inspect.signature(getattr(ImageRepository, name), eval_str=True)
        for name in ALL_METHODS
    }

    assert signatures["save"].parameters["image"].annotation is Image
    assert signatures["get"].parameters["image_id"].annotation is ImageId
    assert signatures["exists"].parameters["image_id"].annotation is ImageId
    assert signatures["delete"].parameters["image_id"].annotation is ImageId
    assert signatures["list"].return_annotation == list[Image]
    assert signatures["save_indexed"].parameters["record"].annotation is IndexingRecord
    assert (
        signatures["save_indexed_many"].parameters["records"].annotation
        == Sequence[IndexingRecord]
    )
    assert signatures["get_index_metadata"].parameters["image_id"].annotation is ImageId
    assert (
        signatures["get_index_metadata_many"].parameters["image_ids"].annotation
        == Sequence[ImageId]
    )
    assert (
        signatures["get_index_metadata_many"].return_annotation
        == dict[ImageId, IndexMetadata]
    )
    assert (
        signatures["update_index_metadata"].parameters["image_id"].annotation is ImageId
    )
    assert (
        signatures["update_index_metadata"].parameters["metadata"].annotation
        is IndexMetadata
    )
    assert (
        signatures["search_similar"].parameters["embedding"].annotation
        is EmbeddingVector
    )
    assert signatures["search_similar"].parameters["limit"].annotation is int
    assert signatures["search_similar"].return_annotation == list[SearchHit]


def test_image_repository_methods_are_abstract() -> None:
    for method_name in ALL_METHODS:
        method = getattr(ImageRepository, method_name)
        assert getattr(method, "__isabstractmethod__", False) is True
