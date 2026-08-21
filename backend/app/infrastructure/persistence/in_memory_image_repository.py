"""In-memory image repository implementation for development and dependency wiring."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from app.domain.entities.image import Image
from app.domain.repositories.image_repository import ImageRepository
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.index_metadata import IndexMetadata
from app.domain.value_objects.indexing_record import IndexingRecord


class InMemoryImageRepository(ImageRepository):
    """Repository implementation backed by an in-memory list."""

    def __init__(self) -> None:
        self._images: list[Image] = []
        self._metadata: dict[uuid.UUID, IndexMetadata] = {}

    def save(self, image: Image) -> None:
        self._images.append(image)

    def get(self, image_id: ImageId) -> Image | None:
        for image in self._images:
            if image.id == image_id:
                return image
        return None

    def exists(self, image_id: ImageId) -> bool:
        return any(image.id == image_id for image in self._images)

    def delete(self, image_id: ImageId) -> None:
        self._images = [image for image in self._images if image.id != image_id]
        self._metadata.pop(image_id.value, None)

    def list(self) -> list[Image]:
        return list(self._images)

    def save_indexed(self, record: IndexingRecord) -> None:
        self._images = [image for image in self._images if image.id != record.image.id]
        self._images.append(record.image)
        self._metadata[record.image.id.value] = IndexMetadata(
            file_size=record.file_size,
            file_modified_at=record.file_modified_at,
            content_hash=record.content_hash,
        )

    def save_indexed_many(self, records: Sequence[IndexingRecord]) -> None:
        """Persist every record, or none of them.

        A dict and a list have no transaction to commit, so atomicity has
        to be arranged by hand: the writes are applied to copies and only
        swapped in once all of them have succeeded. Skipping that would
        make this implementation quietly more forgiving than the real one,
        and the per-row fallback it exists to trigger would then go
        untested everywhere except against PostgreSQL.
        """
        images = list(self._images)
        metadata = dict(self._metadata)

        for record in records:
            images = [image for image in images if image.id != record.image.id]
            images.append(record.image)
            metadata[record.image.id.value] = IndexMetadata(
                file_size=record.file_size,
                file_modified_at=record.file_modified_at,
                content_hash=record.content_hash,
            )

        self._images = images
        self._metadata = metadata

    def get_index_metadata(self, image_id: ImageId) -> IndexMetadata | None:
        if not self.exists(image_id):
            return None
        return self._metadata.get(
            image_id.value, IndexMetadata(file_size=None, file_modified_at=None)
        )

    def get_index_metadata_many(
        self, image_ids: Sequence[ImageId]
    ) -> dict[ImageId, IndexMetadata]:
        """Return metadata for every supplied id that has a row.

        Composed from `get_index_metadata()` rather than reimplemented, so
        the two can never disagree. There is nothing to batch in a dict
        lookup: the bulk method exists to collapse *network* round trips,
        which this implementation does not make.
        """
        found = {}
        for image_id in image_ids:
            metadata = self.get_index_metadata(image_id)
            if metadata is not None:
                found[image_id] = metadata
        return found

    def update_index_metadata(self, image_id: ImageId, metadata: IndexMetadata) -> None:
        if not self.exists(image_id):
            return
        self._metadata[image_id.value] = metadata
