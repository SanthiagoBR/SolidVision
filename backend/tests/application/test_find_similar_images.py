"""Unit tests for `FindSimilarImagesUseCase` (RFC-038 section 4.1).

*"Parecidas com esta"*: the stored vector of an indexed photo, ranked against
the rest by the repository, with the photo itself taken out. Fakes only, and
no model anywhere -- the use case has none, which is the point of it.
"""

from __future__ import annotations

import datetime
import uuid

import pytest

from app.application.use_cases.search_images import (
    MAX_SEARCH_LIMIT,
    FindSimilarImagesUseCase,
)
from app.domain.entities.image import Image
from app.domain.exceptions import (
    ImageNotFoundError,
    ImageNotIndexedError,
    InvalidGeoCircleError,
    InvalidSearchLimitError,
)
from app.domain.value_objects.date_range import DateRange
from app.domain.value_objects.embedding_vector import EmbeddingVector
from app.domain.value_objects.geo_circle import GeoCircle
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.position import Position
from app.domain.value_objects.search_filters import SearchFilters
from tests.application.fakes import FakeImageRepository
from tests.conftest import TEST_DEVICE_ID

DEFAULT_LIMIT = 3


def _image(name: str) -> Image:
    return Image(
        id=ImageId(uuid.uuid4()),
        device_id=TEST_DEVICE_ID,
        relative_path=ImagePath(f"fotos/{name}.jpg"),
        filename=name,
        extension="jpg",
    )


def _vector(*leading: float) -> EmbeddingVector:
    values = [0.0] * 512
    values[: len(leading)] = leading
    return EmbeddingVector(values)


def _build(
    repository: FakeImageRepository, min_radius_m: float = 0.0
) -> FindSimilarImagesUseCase:
    return FindSimilarImagesUseCase(
        repository=repository,
        default_limit=DEFAULT_LIMIT,
        min_radius_m=min_radius_m,
    )


@pytest.fixture
def repository() -> FakeImageRepository:
    return FakeImageRepository()


@pytest.fixture
def source(repository: FakeImageRepository) -> Image:
    image = _image("DJI_0042")
    repository.seed_embedding(image, _vector(1.0))
    return image


def test_ranks_with_the_stored_vector(
    repository: FakeImageRepository, source: Image
) -> None:
    near = _image("near")
    far = _image("far")
    repository.seed_embedding(near, _vector(1.0, 0.2))
    repository.seed_embedding(far, _vector(0.2, 1.0))

    hits = _build(repository).execute(source.id)

    assert [hit.image for hit in hits] == [near, far]
    ((embedding, _, _),) = repository.search_similar_calls
    assert embedding == _vector(1.0)
    assert repository.get_embedding_calls == [source.id]


def test_the_source_is_out_and_its_copy_comes_first(
    repository: FakeImageRepository, source: Image
) -> None:
    """The same vector under another id is *"where else is this photo"*."""
    copy = _image("DJI_0042-copia")
    other = _image("other")
    repository.seed_embedding(copy, _vector(1.0))
    repository.seed_embedding(other, _vector(1.0, 0.5))

    hits = _build(repository).execute(source.id)

    assert source.id not in [hit.image.id for hit in hits]
    assert hits[0].image == copy
    assert hits[0].similarity == pytest.approx(1.0)


def test_the_page_is_full_after_the_source_is_removed(
    repository: FakeImageRepository, source: Image
) -> None:
    for index in range(DEFAULT_LIMIT + 2):
        repository.seed_embedding(_image(f"n{index}"), _vector(1.0, 0.1 * index))

    hits = _build(repository).execute(source.id)

    assert len(hits) == DEFAULT_LIMIT
    ((_, asked, _),) = repository.search_similar_calls
    assert asked == DEFAULT_LIMIT + 1


def test_the_order_is_the_repositorys(
    repository: FakeImageRepository, source: Image
) -> None:
    for index in range(4):
        repository.seed_embedding(_image(f"n{index}"), _vector(1.0, 0.1 * index))

    hits = _build(repository).execute(source.id, limit=4)
    expected = [
        hit
        for hit in repository.search_similar(_vector(1.0), 5)
        if hit.image.id != source.id
    ]

    assert hits == expected


def test_an_unknown_id_is_not_found(repository: FakeImageRepository) -> None:
    with pytest.raises(ImageNotFoundError):
        _build(repository).execute(ImageId(uuid.uuid4()))

    assert repository.search_similar_calls == []


def test_a_row_without_an_embedding_is_not_indexed(
    repository: FakeImageRepository,
) -> None:
    image = _image("never-indexed")
    repository.save(image)

    with pytest.raises(ImageNotIndexedError):
        _build(repository).execute(image.id)

    assert repository.search_similar_calls == []


@pytest.mark.parametrize("limit", [0, MAX_SEARCH_LIMIT + 1])
def test_an_unusable_limit_is_refused_before_anything_is_read(
    repository: FakeImageRepository, source: Image, limit: int
) -> None:
    with pytest.raises(InvalidSearchLimitError):
        _build(repository).execute(source.id, limit=limit)

    assert repository.get_embedding_calls == []


def test_the_maximum_is_allowed_and_one_more_row_is_asked_for(
    repository: FakeImageRepository, source: Image
) -> None:
    _build(repository).execute(source.id, limit=MAX_SEARCH_LIMIT)

    ((_, asked, _),) = repository.search_similar_calls
    assert asked == MAX_SEARCH_LIMIT + 1


def test_a_radius_below_the_floor_is_refused(
    repository: FakeImageRepository, source: Image
) -> None:
    circle = GeoCircle(center=Position(-23.5, -46.6), radius_m=200.0)

    with pytest.raises(InvalidGeoCircleError, match="below the minimum of 300 m"):
        _build(repository, min_radius_m=300.0).execute(
            source.id, filters=SearchFilters(taken_within=circle)
        )

    assert repository.get_embedding_calls == []


def test_the_filters_reach_the_repository(
    repository: FakeImageRepository, source: Image
) -> None:
    filters = SearchFilters(
        captured_between=DateRange(
            start=datetime.datetime(2018, 1, 1), end=datetime.datetime(2019, 1, 1)
        )
    )

    _build(repository).execute(source.id, filters=filters)

    ((_, _, passed),) = repository.search_similar_calls
    assert passed == filters


def test_the_counts_follow_the_rules_of_the_text_search(
    repository: FakeImageRepository, source: Image
) -> None:
    use_case = _build(repository)

    assert use_case.count_hidden_by_unknown_date(None) is None
    assert use_case.count_hidden_by_unknown_position(SearchFilters()) is None
    assert repository.count_unknown_capture_date_calls == []
    assert repository.count_unknown_position_calls == []
