from __future__ import annotations

from app.domain.entities.image import Image
from app.domain.repositories.image_repository import ImageRepository
from app.domain.value_objects.image_id import ImageId


class FakeImageRepository(ImageRepository):
    """In-memory repository double used by application use case tests."""

    def __init__(self, images: list[Image] | None = None) -> None:
        self._images = list(images or [])
        self.save_calls: list[Image] = []
        self.exists_calls: list[ImageId] = []
        self.list_calls = 0

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

    def list(self) -> list[Image]:
        self.list_calls += 1
        return list(self._images)
