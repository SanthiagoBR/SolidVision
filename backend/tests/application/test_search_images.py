"""Unit tests for `SearchImagesUseCase` (RFC-025).

Fakes only, no database and no checkpoint. What is under test here is the
part of search the Application layer actually owns: what it refuses, what
it hands to the repository, and what it does to the answer on the way back
(nothing). Whether the ranking itself is correct is a repository property,
covered once for all three implementations in
`tests/infrastructure/persistence/test_search_similar_contract.py`, and
whether it is *useful* is a model property, measured end to end in
`tests/dataset/test_semantic_search_e2e.py`.
"""

from __future__ import annotations

import uuid

import pytest

from app.application.use_cases.search_images import (
    MAX_SEARCH_LIMIT,
    SearchImagesUseCase,
)
from app.domain.entities.image import Image
from app.domain.exceptions import EmptySearchQueryError, InvalidSearchLimitError
from app.domain.value_objects.embedding_vector import EmbeddingVector
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.search_hit import SearchHit, SearchHits
from app.infrastructure.ai.fake_embedding_model import FakeEmbeddingModel
from tests.application.fakes import FakeImageRepository

DEFAULT_LIMIT = 4


def _image(name: str) -> Image:
    return Image(
        id=ImageId(uuid.uuid4()),
        path=ImagePath(f"images/{name}.png"),
        filename=name,
        extension="png",
    )


def _build_use_case(
    repository: FakeImageRepository | None = None,
    default_limit: int = DEFAULT_LIMIT,
) -> tuple[SearchImagesUseCase, FakeImageRepository]:
    repository = repository if repository is not None else FakeImageRepository()
    use_case = SearchImagesUseCase(
        repository=repository,
        embedding_model=FakeEmbeddingModel(),
        default_limit=default_limit,
    )
    return use_case, repository


def _seed(repository: FakeImageRepository, count: int) -> list[Image]:
    """Seed `count` images whose embeddings are mutually distinguishable."""
    images = []
    for index in range(count):
        image = _image(f"seeded-{index}")
        values = [0.0] * 512
        values[index] = 1.0
        repository.seed_embedding(image, EmbeddingVector(values))
        images.append(image)
    return images


@pytest.mark.parametrize("query", ["", "   ", "\t\n"])
def test_a_blank_query_is_rejected(query: str) -> None:
    use_case, repository = _build_use_case()

    with pytest.raises(EmptySearchQueryError):
        use_case.execute(query)

    assert repository.search_similar_calls == []


@pytest.mark.parametrize("limit", [0, -1, -100])
def test_a_non_positive_limit_is_rejected(limit: int) -> None:
    use_case, repository = _build_use_case()

    with pytest.raises(InvalidSearchLimitError):
        use_case.execute("a lake", limit=limit)

    assert repository.search_similar_calls == []


def test_a_limit_above_the_maximum_is_rejected_rather_than_clamped() -> None:
    """Raising is the decision, and the assertion below is what pins it.

    A clamp would let a caller ask for 10,000 results, receive 100, and
    have no way to tell that from a corpus with 100 images in it.
    """
    use_case, repository = _build_use_case()

    with pytest.raises(InvalidSearchLimitError):
        use_case.execute("a lake", limit=MAX_SEARCH_LIMIT + 1)

    assert repository.search_similar_calls == []


def test_the_maximum_limit_itself_is_allowed() -> None:
    use_case, repository = _build_use_case()

    use_case.execute("a lake", limit=MAX_SEARCH_LIMIT)

    assert repository.search_similar_calls[0][1] == MAX_SEARCH_LIMIT


def test_an_invalid_default_limit_fails_at_construction() -> None:
    """A misconfigured `top_k_results` should not wait for a search to surface."""
    with pytest.raises(InvalidSearchLimitError):
        _build_use_case(default_limit=0)

    with pytest.raises(InvalidSearchLimitError):
        _build_use_case(default_limit=MAX_SEARCH_LIMIT + 1)


def test_the_default_limit_is_the_injected_one() -> None:
    """Not a constant baked into the use case.

    `default_limit` comes from `settings.top_k_results` at the composition
    root, following the `batch_size` precedent, so an unusual value has to
    reach the repository untouched.
    """
    use_case, repository = _build_use_case(default_limit=7)

    use_case.execute("a lake")

    assert repository.search_similar_calls[0][1] == 7


def test_an_explicit_limit_is_passed_through() -> None:
    use_case, repository = _build_use_case()

    use_case.execute("a lake", limit=3)

    assert repository.search_similar_calls[0][1] == 3


def test_the_repository_receives_the_encoded_query_vector() -> None:
    """The embedding, not the text, and not some other vector.

    This is the step RFC-025 exists to add: before it, the use case
    encoded the query and threw the result away. Asserting on the vector
    itself is what makes that regression impossible to reintroduce
    silently, since any repository call at all would otherwise look
    correct.
    """
    use_case, repository = _build_use_case()

    use_case.execute("a rural property with a lake")

    expected = FakeEmbeddingModel().encode_text("a rural property with a lake")
    passed_embedding = repository.search_similar_calls[0][0]
    assert passed_embedding == expected
    assert passed_embedding != FakeEmbeddingModel().encode_text("something else")


def test_the_hits_the_repository_returned_are_returned_unchanged() -> None:
    use_case, repository = _build_use_case()
    _seed(repository, count=3)

    results = use_case.execute("a lake", limit=3)

    assert results == repository.search_similar(
        FakeEmbeddingModel().encode_text("a lake"), 3
    )


def test_the_use_case_does_not_reorder_the_repository_result() -> None:
    """Ordering belongs to the repository, which ranked against real vectors.

    The stub below returns hits in an order no similarity sort would
    produce. A use case that "helpfully" sorted by `similarity` would
    reverse it -- and in doing so would overrule the only component that
    saw the stored embeddings.
    """

    class _UnsortedRepository(FakeImageRepository):
        def __init__(self, hits: list[SearchHit]) -> None:
            super().__init__()
            self._hits = hits

        def search_similar(self, embedding: EmbeddingVector, limit: int) -> SearchHits:
            self.search_similar_calls.append((embedding, limit))
            return list(self._hits)

    hits = [
        SearchHit(image=_image("third"), similarity=0.1),
        SearchHit(image=_image("first"), similarity=0.9),
        SearchHit(image=_image("second"), similarity=0.5),
    ]
    repository = _UnsortedRepository(hits)
    use_case = SearchImagesUseCase(
        repository=repository,
        embedding_model=FakeEmbeddingModel(),
        default_limit=DEFAULT_LIMIT,
    )

    assert use_case.execute("a lake") == hits


def test_no_results_is_an_empty_list_not_an_error() -> None:
    use_case, _ = _build_use_case()

    assert use_case.execute("a lake") == []
