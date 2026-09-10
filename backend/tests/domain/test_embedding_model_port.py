from __future__ import annotations

import inspect
import uuid
from collections.abc import Sequence
from typing import get_type_hints

import pytest
from tests.conftest import TEST_DEVICE_ID

from app.domain.entities.image import Image
from app.domain.services.embedding_model_port import EmbeddingModelPort
from app.domain.value_objects.embedding_vector import EmbeddingVector
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath


def test_embedding_model_port_is_abstract() -> None:
    assert inspect.isabstract(EmbeddingModelPort)


def test_embedding_model_port_cannot_be_instantiated() -> None:
    with pytest.raises(TypeError):
        # The instantiation mypy refuses is the behaviour under test: the
        # port must stay abstract so an adapter that forgets `encode_text`
        # fails at construction. Narrow ignore, and `warn_unused_ignores`
        # flags it if the port ever stops being abstract.
        EmbeddingModelPort()  # type: ignore[abstract]


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


class TestBatchEncoding:
    """RFC-024 section 5: the contract change batching required."""

    class _SingleOnly(EmbeddingModelPort):
        """Implements only what the port demands, and nothing more."""

        def __init__(self) -> None:
            self.calls: list[Image] = []

        def encode_image(self, image: Image) -> EmbeddingVector:
            self.calls.append(image)
            return EmbeddingVector([float(len(image.filename))] * 4)

        def encode_text(self, text: str) -> EmbeddingVector:
            return EmbeddingVector([float(len(text))] * 4)

    @staticmethod
    def _image(name: str) -> Image:
        path = ImagePath(f"images/{name}.png")
        return Image(
            id=ImageId(uuid.uuid5(uuid.NAMESPACE_URL, str(path))),
            device_id=TEST_DEVICE_ID,
            relative_path=path,
            filename=name,
            extension="png",
        )

    def test_encode_images_is_not_abstract(self) -> None:
        """An optimization must not become a burden on every implementation.

        `FakeEmbeddingModel` and every future adapter inherit a working
        default; only an implementation that genuinely has a faster path
        overrides it.
        """
        assert "encode_images" not in EmbeddingModelPort.__abstractmethods__
        assert (
            getattr(EmbeddingModelPort.encode_images, "__isabstractmethod__", False)
            is False
        )

    def test_the_default_returns_one_embedding_per_image_in_order(self) -> None:
        model = self._SingleOnly()
        images = [self._image("a"), self._image("bb"), self._image("ccc")]

        results = model.encode_images(images)

        assert [result.values[0] for result in results] == [1.0, 2.0, 3.0]

    def test_the_default_matches_calling_encode_image_directly(self) -> None:
        """The equivalence the batch path must preserve, stated on the port."""
        model = self._SingleOnly()
        images = [self._image("one"), self._image("two")]

        assert model.encode_images(images) == [
            model.encode_image(image) for image in images
        ]

    def test_an_empty_sequence_encodes_nothing(self) -> None:
        model = self._SingleOnly()

        assert model.encode_images([]) == []
        assert model.calls == []

    def test_encode_images_uses_domain_types_only(self) -> None:
        hints = get_type_hints(EmbeddingModelPort.encode_images)

        assert hints["images"] == Sequence[Image]
        assert hints["return"] == list[EmbeddingVector]

    def test_errors_propagate_out_of_the_default_implementation(self) -> None:
        """The caller owns isolation; swallowing here would hide which image."""

        class _Explodes(EmbeddingModelPort):
            def encode_image(self, image: Image) -> EmbeddingVector:
                raise RuntimeError(f"cannot encode {image.filename}")

            def encode_text(self, text: str) -> EmbeddingVector:
                raise NotImplementedError

        with pytest.raises(RuntimeError, match="cannot encode a"):
            _Explodes().encode_images([self._image("a")])
