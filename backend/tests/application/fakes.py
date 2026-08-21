from __future__ import annotations

import uuid
from collections.abc import Sequence

from app.domain.entities.image import Image
from app.domain.repositories.image_repository import ImageRepository
from app.domain.services.content_hasher_port import ContentHasherPort
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.index_metadata import IndexMetadata
from app.domain.value_objects.indexing_record import IndexingRecord
from app.infrastructure.filesystem.sha256_content_hasher import Sha256ContentHasher


class FakeImageRepository(ImageRepository):
    """In-memory repository double used by application use case tests."""

    def __init__(self, images: list[Image] | None = None) -> None:
        self._images = list(images or [])
        self._metadata: dict[uuid.UUID, IndexMetadata] = {}
        self.save_calls: list[Image] = []
        self.exists_calls: list[ImageId] = []
        self.list_calls = 0
        self.save_indexed_calls: list[IndexingRecord] = []
        self.save_indexed_many_calls: list[list[IndexingRecord]] = []
        self.get_index_metadata_calls: list[ImageId] = []
        self.get_index_metadata_many_calls: list[Sequence[ImageId]] = []
        self.update_index_metadata_calls: list[tuple[ImageId, IndexMetadata]] = []

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
            content_hash=record.content_hash,
        )

    def save_indexed_many(self, records: Sequence[IndexingRecord]) -> None:
        self.save_indexed_many_calls.append(list(records))
        for record in records:
            self.save_indexed(record)

    def get_index_metadata(self, image_id: ImageId) -> IndexMetadata | None:
        self.get_index_metadata_calls.append(image_id)
        if not any(image.id == image_id for image in self._images):
            return None
        return self._metadata.get(
            image_id.value, IndexMetadata(file_size=None, file_modified_at=None)
        )

    def get_index_metadata_many(
        self, image_ids: Sequence[ImageId]
    ) -> dict[ImageId, IndexMetadata]:
        self.get_index_metadata_many_calls.append(list(image_ids))
        found = {}
        for image_id in image_ids:
            if any(image.id == image_id for image in self._images):
                found[image_id] = self._metadata.get(
                    image_id.value,
                    IndexMetadata(file_size=None, file_modified_at=None),
                )
        return found

    def update_index_metadata(self, image_id: ImageId, metadata: IndexMetadata) -> None:
        self.update_index_metadata_calls.append((image_id, metadata))
        if any(image.id == image_id for image in self._images):
            self._metadata[image_id.value] = metadata


class RecordingContentHasher(ContentHasherPort):
    """Real SHA-256 hashing, with a record of which images it was asked about.

    Delegates rather than faking a digest: the cost-ascending rule in
    ARCHITECTURE.md section 16 is about *when* hashing happens, so the
    tests that matter count calls, and a fake digest would only add a way
    for the tests to disagree with production.
    """

    def __init__(self) -> None:
        self._delegate = Sha256ContentHasher()
        self.hashed: list[Image] = []

    def hash_image(self, image: Image) -> str:
        self.hashed.append(image)
        return self._delegate.hash_image(image)


class StubContentHasher(ContentHasherPort):
    """Hashes nothing, reads nothing, and answers whatever it was told to.

    The Application-layer tests build images at paths that do not exist,
    on purpose -- the use cases are supposed to work in terms of ports, not
    files. Handing them the real hasher would make every one of those tests
    depend on a real filesystem to answer a question about control flow.

    `digests` maps a path (as `str(image.path)`) to the digest to return;
    anything absent gets `default`.
    """

    def __init__(
        self,
        digests: dict[str, str] | None = None,
        default: str = "0" * 64,
    ) -> None:
        self.digests = digests or {}
        self.default = default
        self.hashed: list[Image] = []

    def hash_image(self, image: Image) -> str:
        self.hashed.append(image)
        return self.digests.get(str(image.path), self.default)
