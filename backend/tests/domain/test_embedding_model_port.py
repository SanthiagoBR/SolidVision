from __future__ import annotations

import inspect
from typing import get_type_hints

import pytest

from app.domain.entities.image import Image
from app.domain.services.embedding_model_port import EmbeddingModelPort
from app.domain.value_objects.embedding_vector import EmbeddingVector


def test_embedding_model_port_is_abstract() -> None:
    assert inspect.isabstract(EmbeddingModelPort)


def test_embedding_model_port_cannot_be_instantiated() -> None:
    with pytest.raises(TypeError):
        EmbeddingModelPort()


def test_embedding_model_port_methods_are_abstract() -> None:
    assert "encode_image" in EmbeddingModelPort.__abstractmethods__
    assert "encode_text" in EmbeddingModelPort.__abstractmethods__


def test_embedding_model_port_uses_domain_types_only() -> None:
    encode_image_hints = get_type_hints(EmbeddingModelPort.encode_image)
    encode_text_hints = get_type_hints(EmbeddingModelPort.encode_text)

    assert encode_image_hints["image"] is Image
    assert encode_image_hints["return"] is EmbeddingVector
    assert encode_text_hints["text"] is str
    assert encode_text_hints["return"] is EmbeddingVector
