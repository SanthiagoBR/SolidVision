from __future__ import annotations

import uuid

from app.domain.entities.image import Image
from app.domain.repositories.image_repository import ImageRepository
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.index_metadata import IndexMetadata
from app.domain.value_objects.indexing_record import IndexingRecord


class FakeImageRepository(ImageRepository):
    """In-memory repository double used by application use case tests."""

    def __init__(self, images: list[Image] | None = None) -> None:
        self._images = list(images or [])
        self._metadata: dict[uuid.UUID, IndexMetadata] = {}
        self.save_calls: list[Image] = []
        self.exists_calls: list[ImageId] = []
        self.list_calls = 0
        self.save_indexed_calls: list[IndexingRecord] = []
        self.get_index_metadata_calls: list[ImageId] = []

    def save(self, image: Image) -> None:
        self.save_calls.append(image)
        self._images.append(image)

    def get(self, image_id: ImageId) -> Image | None:
        for image in self._images:
            if image.id == image_id:
                return image
        return None

    def exists(self, image_id: ImageId) -> bool:
        self.exists_calls.append(image_id)
        return any(image.id == image_id for image in self._images)

    def delete(self, image_id: ImageId) -> None:
        self._images = [image for image in self._images if image.id != image_id]
        self._metadata.pop(image_id.value, None)

    def list(self) -> list[Image]:
        self.list_calls += 1
        return list(self._images)

    def save_indexed(self, record: IndexingRecord) -> None:
        self.save_indexed_calls.append(record)
        self._images = [image for image in self._images if image.id != record.image.id]
        self._images.append(record.image)
        self._metadata[record.image.id.value] = IndexMetadata(
            file_size=record.file_size,
            file_modified_at=record.file_modified_at,
        )

    def get_index_metadata(self, image_id: ImageId) -> IndexMetadata | None:
        self.get_index_metadata_calls.append(image_id)
        if not any(image.id == image_id for image in self._images):
            return None
        return self._metadata.get(
            image_id.value, IndexMetadata(file_size=None, file_modified_at=None)
        )
