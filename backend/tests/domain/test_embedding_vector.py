from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from app.domain.exceptions import InvalidEmbeddingVectorError
from app.domain.value_objects.embedding_vector import EmbeddingVector


def test_embedding_vector_rejects_empty_input() -> None:
    with pytest.raises(InvalidEmbeddingVectorError):
        EmbeddingVector([])


def test_embedding_vector_accepts_list_input() -> None:
    vector = EmbeddingVector([0.1, 0.2, 0.3])

    assert vector.values == (0.1, 0.2, 0.3)


def test_embedding_vector_accepts_tuple_input() -> None:
    vector = EmbeddingVector((0.4, 0.5, 0.6))

    assert vector.values == (0.4, 0.5, 0.6)


def test_embedding_vector_accepts_generator_input() -> None:
    vector = EmbeddingVector(value for value in [0.7, 0.8, 0.9])

    assert vector.values == (0.7, 0.8, 0.9)


def test_embedding_vector_compares_by_value() -> None:
    assert EmbeddingVector([1.0, 2.0]) == EmbeddingVector((1.0, 2.0))


def test_embedding_vector_is_immutable() -> None:
    vector = EmbeddingVector([1.0, 2.0, 3.0])

    with pytest.raises(FrozenInstanceError):
        vector.values = (4.0, 5.0, 6.0)  # type: ignore[misc]


def test_embedding_vector_does_not_depend_on_numpy_or_torch() -> None:
    import inspect

    module = inspect.getmodule(EmbeddingVector)
    assert module is not None

    source = inspect.getsource(EmbeddingVector)
    assert "numpy" not in source
    assert "torch" not in source
