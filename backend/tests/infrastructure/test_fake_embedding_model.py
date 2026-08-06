from __future__ import annotations

import inspect
import uuid

from app.domain.entities.image import Image
from app.domain.services.embedding_model_port import EmbeddingModelPort
from app.domain.value_objects.embedding_vector import EmbeddingVector
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.infrastructure.ai.fake_embedding_model import FakeEmbeddingModel
from app.infrastructure.config.settings import settings


def test_fake_embedding_model_implements_embedding_model_port() -> None:
    assert isinstance(FakeEmbeddingModel(), EmbeddingModelPort)


def test_encode_text_is_deterministic() -> None:
    model = FakeEmbeddingModel()

    first = model.encode_text("cat")
    second = model.encode_text("cat")

    assert first == second


def test_encode_image_is_deterministic() -> None:
    model = FakeEmbeddingModel()
    image = Image(
        id=ImageId(uuid.uuid4()),
        path=ImagePath("images/example.png"),
        filename="example",
        extension="png",
    )

    first = model.encode_image(image)
    second = model.encode_image(image)

    assert first == second


def test_different_texts_produce_different_embeddings() -> None:
    model = FakeEmbeddingModel()

    assert model.encode_text("ab") != model.encode_text("ba")


def test_different_images_produce_different_embeddings() -> None:
    model = FakeEmbeddingModel()
    first_image = Image(
        id=ImageId(uuid.uuid4()),
        path=ImagePath("images/one.png"),
        filename="one",
        extension="png",
    )
    second_image = Image(
        id=ImageId(uuid.uuid4()),
        path=ImagePath("images/two.png"),
        filename="two",
        extension="png",
    )

    assert model.encode_image(first_image) != model.encode_image(second_image)


def test_embedding_dimension_matches_settings() -> None:
    model = FakeEmbeddingModel()

    embedding = model.encode_text("hello")

    assert len(embedding.values) == settings.embedding_dimension


def test_methods_return_embedding_vector() -> None:
    model = FakeEmbeddingModel()

    assert isinstance(model.encode_text("hello"), EmbeddingVector)
    assert isinstance(
        model.encode_image(
            Image(
                id=ImageId(uuid.uuid4()),
                path=ImagePath("images/example.png"),
                filename="example",
                extension="png",
            )
        ),
        EmbeddingVector,
    )


def test_fake_embedding_model_only_imports_allowed_dependencies() -> None:
    module = inspect.getmodule(FakeEmbeddingModel)
    assert module is not None
    source = inspect.getsource(FakeEmbeddingModel)

    assert "numpy" not in source
    assert "torch" not in source
    assert "transformers" not in source
    assert "PIL" not in source
