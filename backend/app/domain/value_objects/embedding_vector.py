"""Immutable embedding vector value object for the domain layer."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from app.domain.exceptions import InvalidEmbeddingVectorError


@dataclass(frozen=True)
class EmbeddingVector:
    """Immutable value object representing a semantic embedding vector."""

    values: tuple[float, ...]

    def __init__(self, values: Iterable[float]) -> None:
        normalized_values = self._normalize(values)
        object.__setattr__(self, "values", normalized_values)

    @staticmethod
    def _normalize(values: Iterable[float]) -> tuple[float, ...]:
        normalized_values = tuple(float(value) for value in values)
        if not normalized_values:
            raise InvalidEmbeddingVectorError("Embedding vector cannot be empty")
        return normalized_values


# Future RFCs may introduce a dedicated SearchQuery value object
# once its domain behavior (e.g. query validation, normalization) becomes necessary.
