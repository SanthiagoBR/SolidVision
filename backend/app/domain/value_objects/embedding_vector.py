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


# A dedicated SearchQuery value object was considered by RFC-025 and
# deliberately deferred. Search now validates its query -- rejecting blank
# text -- but that is the whole of the behavior, and a value object whose
# only job is to carry an already-checked string buys nothing the use case
# does not already have: every construction site would be one call away
# from the one place that validates. It becomes worth its own type when
# there is domain behavior to put on it (normalization the repository must
# agree with, structured filters, a query the model rewrites), at which
# point the validation moves here with it.
