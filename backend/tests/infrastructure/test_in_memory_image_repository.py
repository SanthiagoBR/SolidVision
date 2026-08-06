from __future__ import annotations

import uuid

from app.domain.entities.image import Image
from app.domain.repositories.image_repository import ImageRepository
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.infrastructure.persistence.in_memory_image_repository import (
    InMemoryImageRepository,
)


def test_in_memory_image_repository_implements_repository_interface() -> None:
    repository = InMemoryImageRepository()
    assert isinstance(repository, ImageRepository)


def test_in_memory_image_repository_persists_and_reads_images() -> None:
    repository = InMemoryImageRepository()
    image = Image(
        id=ImageId(uuid.uuid4()),
        path=ImagePath("images/example.png"),
        filename="example",
        extension="png",
    )

    repository.save(image)

    assert repository.get(image.id) == image
    assert repository.exists(image.id) is True
    assert repository.list() == [image]


def test_in_memory_image_repository_deletes_and_lists_images() -> None:
    repository = InMemoryImageRepository()
    image = Image(
        id=ImageId(uuid.uuid4()),
        path=ImagePath("images/example.png"),
        filename="example",
        extension="png",
    )

    repository.save(image)
    repository.delete(image.id)

    assert repository.get(image.id) is None
    assert repository.exists(image.id) is False
    assert repository.list() == []
