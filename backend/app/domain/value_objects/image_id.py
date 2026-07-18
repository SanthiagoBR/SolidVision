"""Image identifier value object for the domain layer."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from app.domain.exceptions import InvalidImageIdentifierError


@dataclass(frozen=True)
class ImageId:
    """Strongly typed identifier for an image."""

    value: UUID

    def __init__(self, value: UUID | str) -> None:
        normalized_value = self._normalize(value)
        object.__setattr__(self, "value", normalized_value)

    @staticmethod
    def _normalize(value: UUID | str) -> UUID:
        if isinstance(value, UUID):
            return value

        if isinstance(value, str):
            if not value.strip():
                raise InvalidImageIdentifierError("Image identifier cannot be empty")

            try:
                return UUID(value)
            except ValueError as exc:
                raise InvalidImageIdentifierError(
                    "Image identifier must be a valid UUID"
                ) from exc

        raise InvalidImageIdentifierError("Image identifier must be a UUID or string")
