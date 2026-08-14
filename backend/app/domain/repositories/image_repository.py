"""Abstract repository port for image persistence operations."""

from abc import ABC, abstractmethod

from app.domain.entities.image import Image
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.index_metadata import IndexMetadata
from app.domain.value_objects.indexing_record import IndexingRecord


class ImageRepository(ABC):
    """Repository contract for managing Image domain entities."""

    @abstractmethod
    def save(self, image: Image) -> None:
        """Persist a new image in the repository.

        Kept as the original RFC-015 create-once contract, used by
        `IndexImageUseCase`. Incremental indexing uses `save_indexed()`
        instead.
        """

    @abstractmethod
    def get(self, image_id: ImageId) -> Image | None:
        """Retrieve an image by its identifier."""

    @abstractmethod
    def exists(self, image_id: ImageId) -> bool:
        """Return whether an image exists for the provided identifier."""

    @abstractmethod
    def delete(self, image_id: ImageId) -> None:
        """Remove an image from the repository."""

    @abstractmethod
    def list(self) -> list[Image]:
        """Return all images known to the repository."""

    @abstractmethod
    def save_indexed(self, record: IndexingRecord) -> None:
        """Create or update an image together with its embedding and metadata."""

    @abstractmethod
    def get_index_metadata(self, image_id: ImageId) -> IndexMetadata | None:
        """Return the persisted filesystem metadata for an image, if any.

        Returns `None` when no row exists for `image_id` (new file).
        Returns `IndexMetadata(None, None)` when a row exists but has no
        metadata yet -- callers must treat that as changed, not unchanged.
        """
