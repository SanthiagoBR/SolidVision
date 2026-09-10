from __future__ import annotations

import inspect
import math
import uuid

import pytest
from tests.conftest import TEST_DEVICE_ID

from app.domain.entities.image import Image
from app.domain.services.embedding_model_port import EmbeddingModelPort
from app.domain.value_objects.embedding_vector import EmbeddingVector
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.infrastructure.ai.fake_embedding_model import FakeEmbeddingModel
from app.infrastructure.config.settings import settings


def _cosine_similarity(a: EmbeddingVector, b: EmbeddingVector) -> float:
    dot = sum(x * y for x, y in zip(a.values, b.values, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a.values))
    norm_b = math.sqrt(sum(y * y for y in b.values))
    return dot / (norm_a * norm_b)


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
        device_id=TEST_DEVICE_ID,
        relative_path=ImagePath("images/example.png"),
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
        device_id=TEST_DEVICE_ID,
        relative_path=ImagePath("images/one.png"),
        filename="one",
        extension="png",
    )
    second_image = Image(
        id=ImageId(uuid.uuid4()),
        device_id=TEST_DEVICE_ID,
        relative_path=ImagePath("images/two.png"),
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
                device_id=TEST_DEVICE_ID,
                relative_path=ImagePath("images/example.png"),
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


def test_embedding_is_unit_normalized() -> None:
    """RFC-022 7.4: vectors are L2-normalized so cosine math behaves sanely."""
    model = FakeEmbeddingModel()

    embedding = model.encode_text("a rural property with a lake")

    norm = math.sqrt(sum(value * value for value in embedding.values))
    assert norm == pytest.approx(1.0, abs=1e-9)


def test_embedding_is_mean_centered() -> None:
    """RFC-022 7.4: vectors are mean-centered, not a monotonic ramp."""
    model = FakeEmbeddingModel()

    embedding = model.encode_text("a rural property with a lake")

    mean = sum(embedding.values) / len(embedding.values)
    assert mean == pytest.approx(0.0, abs=1e-9)


def test_different_seeds_do_not_collapse_to_near_identical_vectors() -> None:
    """RFC-022 7.4: the historical bug made every embedding's cosine ~1.0.

    A positional term that grew linearly with dimension index (reaching
    ~144 at the top of a 1152-dimension vector) swamped the content-derived
    term, which stayed within (0, 1]. Every vector was effectively the same
    ramp, so any two images were reported as ~100% similar regardless of
    content -- silently defeating similarity search and making an HNSW
    recall benchmark meaningless. This pins the fix: unrelated seeds must
    land measurably apart, not clustered at cosine ~1.0.
    """
    model = FakeEmbeddingModel()
    seeds = ["cat", "dog", "rural property with a lake", "downtown office tower"]
    embeddings = [model.encode_text(seed) for seed in seeds]

    similarities = [
        _cosine_similarity(embeddings[i], embeddings[j])
        for i in range(len(embeddings))
        for j in range(i + 1, len(embeddings))
    ]

    assert max(similarities) < 0.5


def test_identical_seed_has_cosine_similarity_one_with_itself() -> None:
    model = FakeEmbeddingModel()

    embedding = model.encode_text("cat")

    assert _cosine_similarity(embedding, embedding) == pytest.approx(1.0, abs=1e-9)
