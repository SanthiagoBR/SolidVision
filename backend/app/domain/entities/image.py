"""Image entity for the domain layer."""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath


@dataclass(frozen=True)
class Image:
    """Immutable business entity representing an image known by the system."""

    id: ImageId
    path: ImagePath
    filename: str
    extension: str

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Image):
            return NotImplemented
        return self.id == other.id

    def __hash__(self) -> int:
        return hash(self.id)
