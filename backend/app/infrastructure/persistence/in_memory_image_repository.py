"""In-memory image repository implementation for development and dependency wiring."""

from __future__ import annotations

from app.domain.entities.image import Image
from app.domain.repositories.image_repository import ImageRepository
from app.domain.value_objects.image_id import ImageId


class InMemoryImageRepository(ImageRepository):
    """Repository implementation backed by an in-memory list."""

    def __init__(self) -> None:
        self._images: list[Image] = []

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

    def list(self) -> list[Image]:
        return list(self._images)
