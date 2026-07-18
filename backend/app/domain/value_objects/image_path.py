"""Image path value object for the domain layer."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.domain.exceptions import InvalidImagePathError


@dataclass(frozen=True)
class ImagePath:
    """A filesystem path represented as a value object."""

    value: Path

    def __init__(self, value: str | Path) -> None:
        normalized_value = self._normalize(value)
        object.__setattr__(self, "value", normalized_value)

    @staticmethod
    def _normalize(value: str | Path) -> Path:
        if isinstance(value, Path):
            candidate = str(value)
        elif isinstance(value, str):
            candidate = value
        else:
            raise InvalidImagePathError("Image path must be a string or pathlib.Path")

        if not candidate.strip():
            raise InvalidImagePathError("Image path cannot be empty")

        return Path(candidate.replace("\\", "/"))

    def __str__(self) -> str:
        return self.value.as_posix()
