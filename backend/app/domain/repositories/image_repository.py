"""Abstract repository port for image persistence operations."""

from abc import ABC, abstractmethod

from app.domain.entities.image import Image
from app.domain.value_objects.image_id import ImageId


class ImageRepository(ABC):
    """Repository contract for managing Image domain entities."""

    @abstractmethod
    def save(self, image: Image) -> None:
        """Persist an image in the repository."""

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
