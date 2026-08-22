"""One contract test for `ImageRepository.search_similar()`, three implementations.

Written once and run against `PostgresImageRepository`,
`InMemoryImageRepository`, and `tests.application.fakes.FakeImageRepository`,
because the risk this file exists to remove is *divergence*: a test double
that ranks, truncates, or fails slightly differently from PostgreSQL turns
every green unit test above it into evidence about the double rather than
about the product. The two in-memory implementations are only useful while
they are indistinguishable from the real one here.

The vectors are 512-dimensional on purpose. `EmbeddingVector` validates
only that it is non-empty, so a hand-written 3-dimensional vector passes
every domain check, works in Python, and is rejected by the `vector(512)`
column -- a class of test that passes twice and fails once, for reasons
that have nothing to do with what it was written to check.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from tests.application.fakes import FakeImageRepository

from app.domain.entities.image import Image
from app.domain.exceptions import EmbeddingDimensionMismatchError
from app.domain.repositories.image_repository import ImageRepository
from app.domain.value_objects.embedding_vector import EmbeddingVector
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.indexing_record import IndexingRecord
from app.infrastructure.database.models.image_model import EMBEDDING_DIMENSION
from app.infrastructure.persistence.in_memory_image_repository import (
    InMemoryImageRepository,
)
from app.infrastructure.persistence.postgres_image_repository import (
    PostgresImageRepository,
)


@pytest.fixture(params=["postgres", "in_memory", "fake"])
def repository(request: pytest.FixtureRequest) -> Iterator[ImageRepository]:
    """Yield each `ImageRepository` implementation in turn.

    `empty_db_session` is resolved lazily rather than declared as a
    parameter, so the two in-memory rounds do not require a database to
    run. The PostgreSQL round needs an empty table for the same reason
    every test here builds its own vectors: a top-K query has no way to
    ignore rows it did not create.
    """
    if request.param == "postgres":
        yield PostgresImageRepository(request.getfixturevalue("empty_db_session"))
    elif request.param == "in_memory":
        yield InMemoryImageRepository()
    else:
        yield FakeImageRepository()


def one_hot(index: int, sign: float = 1.0) -> EmbeddingVector:
    """Build a 512-dimensional axis vector, so similarities are exact.

    One-hot vectors make the expected cosine values arithmetic rather than
    approximate: identical is exactly 1, any two distinct axes are exactly
    0, and a negated axis is exactly -1. A test that asserted on
    "roughly ordered" scores from arbitrary vectors could not tell a
    correct implementation from one that dropped the sign.
    """
    values = [0.0] * EMBEDDING_DIMENSION
    values[index] = sign
    return EmbeddingVector(values)


def _image(image_id: uuid.UUID | None = None) -> Image:
    unique = uuid.uuid4().hex
    return Image(
        id=ImageId(image_id or uuid.uuid4()),
        path=ImagePath(f"images/search/{unique}.png"),
        filename=unique,
        extension="png",
    )


def index_image(
    repository: ImageRepository,
    embedding: EmbeddingVector,
    image_id: uuid.UUID | None = None,
) -> Image:
    """Persist one searchable image, through the same door production uses."""
    image = _image(image_id)
    repository.save_indexed(
        IndexingRecord(
            image=image,
            embedding=embedding,
            file_size=1,
            file_modified_at=None,
        )
    )
    return image


def store_without_embedding(repository: ImageRepository) -> Image:
    """Persist an image the way `IndexImageUseCase` does: no embedding at all."""
    image = _image()
    repository.save(image)
    return image


def test_results_are_ordered_from_most_to_least_similar(
    repository: ImageRepository,
) -> None:
    query = one_hot(0)
    identical = index_image(repository, one_hot(0))
    orthogonal = index_image(repository, one_hot(1))
    opposite = index_image(repository, one_hot(0, sign=-1.0))

    hits = repository.search_similar(query, limit=10)

    assert [hit.image for hit in hits] == [identical, orthogonal, opposite]


def test_similarity_spans_the_full_cosine_range(
    repository: ImageRepository,
) -> None:
    """The scores are cosine similarity in [-1, 1], not a [0, 1] rescale.

    The opposite vector is the assertion that matters. A repository that
    returned pgvector's raw distance, or clamped the score into [0, 1],
    or normalized the range across the result set would still produce the
    right *order* and would fail here.
    """
    query = one_hot(0)
    index_image(repository, one_hot(0))
    index_image(repository, one_hot(1))
    index_image(repository, one_hot(0, sign=-1.0))

    similarities = [hit.similarity for hit in repository.search_similar(query, 10)]

    assert similarities == [
        pytest.approx(1.0),
        pytest.approx(0.0),
        pytest.approx(-1.0),
    ]


def test_limit_is_respected_exactly(repository: ImageRepository) -> None:
    for axis in range(5):
        index_image(repository, one_hot(axis))

    assert len(repository.search_similar(one_hot(0), limit=2)) == 2
    assert len(repository.search_similar(one_hot(0), limit=1)) == 1


def test_fewer_candidates_than_limit_returns_what_exists(
    repository: ImageRepository,
) -> None:
    index_image(repository, one_hot(0))
    index_image(repository, one_hot(1))

    assert len(repository.search_similar(one_hot(0), limit=10)) == 2


def test_images_without_an_embedding_never_appear(
    repository: ImageRepository,
) -> None:
    """Even when they are the overwhelming majority of the table.

    An unindexed row has no score, and the tempting implementations --
    treating a missing vector as zeros, or falling back to unranked rows
    once the ranked ones run out -- both put files nobody indexed in front
    of files somebody did.
    """
    indexed = index_image(repository, one_hot(0))
    for _ in range(6):
        store_without_embedding(repository)

    hits = repository.search_similar(one_hot(0), limit=10)

    assert [hit.image for hit in hits] == [indexed]


def test_an_empty_repository_returns_no_hits(repository: ImageRepository) -> None:
    assert repository.search_similar(one_hot(0), limit=10) == []


def test_ties_are_broken_deterministically_by_id(
    repository: ImageRepository,
) -> None:
    """Equal similarity must not mean arbitrary order.

    Without an explicit tie-break, PostgreSQL returns equally distant rows
    in whatever order the plan produced them -- which changes between a
    sequential scan and an index scan, i.e. as soon as the table grows --
    and `limit` would then cut an arbitrary one of them. The ids are
    seeded in reverse so that insertion order cannot be what produces the
    expected answer.
    """
    second = index_image(repository, one_hot(0), image_id=uuid.UUID(int=2))
    first = index_image(repository, one_hot(0), image_id=uuid.UUID(int=1))

    hits = repository.search_similar(one_hot(0), limit=10)

    assert [hit.image for hit in hits] == [first, second]
    assert [hit.image for hit in repository.search_similar(one_hot(0), limit=1)] == [
        first
    ]


def test_a_wrong_sized_query_vector_raises(repository: ImageRepository) -> None:
    """The failure that would otherwise be a plausible wrong answer.

    In Python, `zip` over vectors of different lengths truncates in
    silence, so a 3-dimensional query against 512-dimensional rows would
    return a confident ranking computed from three dimensions. Every
    implementation raises instead, including on an empty repository, where
    PostgreSQL would otherwise never evaluate the comparison at all.
    """
    index_image(repository, one_hot(0))

    with pytest.raises(EmbeddingDimensionMismatchError):
        repository.search_similar(EmbeddingVector([1.0, 0.0, 0.0]), limit=10)


def test_a_wrong_sized_query_vector_raises_on_an_empty_repository(
    repository: ImageRepository,
) -> None:
    with pytest.raises(EmbeddingDimensionMismatchError):
        repository.search_similar(EmbeddingVector([1.0, 0.0, 0.0]), limit=10)
