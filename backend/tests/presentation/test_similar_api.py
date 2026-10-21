"""`GET /api/v1/images/{id}/similar` at the HTTP edge (RFC-038 section 4.1).

*"Parecidas com esta"*: the stored embedding of a photo, ranked against the
rest. The real provider, `get_find_similar_images_use_case()`, over the fake
repository, so that the test of "no model" is a test of the wiring: the
model's provider is replaced by one that fails if anything calls it, and the
photo's disk is not plugged in.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from tests.application.fakes import (
    FakeDeviceLocator,
    FakeDeviceRepository,
    FakeImageRepository,
)
from tests.conftest import TEST_DEVICE_ID, make_test_device

from app.application.use_cases.resolve_image_location import (
    ResolveImageLocationUseCase,
)
from app.domain.entities.image import Image
from app.domain.value_objects.capture_source import CaptureSource
from app.domain.value_objects.embedding_vector import EmbeddingVector
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.infrastructure.config.settings import settings
from app.presentation.api import app
from app.presentation.dependencies import (
    get_embedding_model,
    get_image_repository,
    get_resolve_image_location_use_case,
)


def url(image_id: ImageId | uuid.UUID) -> str:
    value = image_id.value if isinstance(image_id, ImageId) else image_id
    return f"/api/v1/images/{value}/similar"


def vector(*leading: float) -> EmbeddingVector:
    values = [0.0] * 512
    values[: len(leading)] = leading
    return EmbeddingVector(values)


def photo(
    name: str,
    captured_at: datetime.datetime | None = None,
) -> Image:
    return Image(
        id=ImageId(uuid.uuid4()),
        device_id=TEST_DEVICE_ID,
        relative_path=ImagePath(f"fotos/2018/{name}.JPG"),
        filename=name,
        extension="jpg",
        captured_at=captured_at,
        capture_source=CaptureSource.EXIF_ORIGINAL if captured_at else None,
    )


@pytest.fixture
def repository() -> FakeImageRepository:
    return FakeImageRepository()


@pytest.fixture
def model_calls() -> list[str]:
    return []


@pytest.fixture
def client(
    repository: FakeImageRepository, model_calls: list[str]
) -> Iterator[TestClient]:
    """The production provider of the use case, with no model and no disk."""

    def no_model() -> object:
        model_calls.append("get_embedding_model")
        raise AssertionError("/similar asked for the embedding model")

    app.dependency_overrides[get_image_repository] = lambda: repository
    app.dependency_overrides[get_embedding_model] = no_model
    app.dependency_overrides[get_resolve_image_location_use_case] = lambda: (
        ResolveImageLocationUseCase(
            device_repository=FakeDeviceRepository([make_test_device("HD3")]),
            device_locator=FakeDeviceLocator(),
        )
    )
    with TestClient(app, base_url="http://127.0.0.1") as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def source(repository: FakeImageRepository) -> Image:
    image = photo("DJI_0042")
    repository.seed_embedding(image, vector(1.0))
    return image


class TestWithoutAModelOrADisk:
    def test_a_200_with_the_disk_unplugged_and_no_model(
        self,
        client: TestClient,
        repository: FakeImageRepository,
        source: Image,
        model_calls: list[str],
    ) -> None:
        near = photo("DJI_0043")
        repository.seed_embedding(near, vector(1.0, 0.3))

        response = client.get(url(source.id))

        assert response.status_code == 200
        body = response.json()
        assert body["image_id"] == str(source.id.value)
        (result,) = body["results"]
        assert result["id"] == str(near.id.value)
        assert result["device"] == {
            "id": str(TEST_DEVICE_ID.value),
            "label": "HD3",
            "connected": False,
        }
        assert result["absolute_path"] is None
        assert model_calls == []


class TestTheRanking:
    def test_the_source_is_out_and_a_copy_of_it_comes_first(
        self, client: TestClient, repository: FakeImageRepository, source: Image
    ) -> None:
        copy = photo("DJI_0042-HD4")
        other = photo("DJI_0099")
        repository.seed_embedding(other, vector(1.0, 0.5))
        repository.seed_embedding(copy, vector(1.0))

        ids = [result["id"] for result in client.get(url(source.id)).json()["results"]]

        assert str(source.id.value) not in ids
        assert ids == [str(copy.id.value), str(other.id.value)]

    def test_the_limit_is_full_after_the_source_is_taken_out(
        self, client: TestClient, repository: FakeImageRepository, source: Image
    ) -> None:
        for index in range(5):
            repository.seed_embedding(photo(f"n{index}"), vector(1.0, 0.1 * index))

        body = client.get(url(source.id), params={"limit": 3}).json()

        assert body["limit"] == 3
        assert len(body["results"]) == 3

    def test_the_default_limit_is_the_configured_one(
        self, client: TestClient, source: Image
    ) -> None:
        assert client.get(url(source.id)).json()["limit"] == settings.top_k_results

    def test_the_filters_are_the_searchs(
        self, client: TestClient, repository: FakeImageRepository, source: Image
    ) -> None:
        dated = photo("dated", captured_at=datetime.datetime(2018, 6, 1, 10, 0))
        undated = photo("undated")
        repository.seed_embedding(dated, vector(1.0, 0.2))
        repository.seed_embedding(undated, vector(1.0, 0.1))

        body = client.get(
            url(source.id),
            params={"captured_from": "2018-01-01", "captured_to": "2019-01-01"},
        ).json()

        assert [result["id"] for result in body["results"]] == [str(dated.id.value)]
        # The source has no date either, and is counted: the count is over the
        # index under the filters, not over this page (RFC-028 section 8.2).
        assert body["excluded_unknown_date"] == 2
        assert body["excluded_unknown_position"] is None

    def test_a_device_filter_naming_another_disk_matches_nothing(
        self, client: TestClient, repository: FakeImageRepository, source: Image
    ) -> None:
        repository.seed_embedding(photo("n"), vector(1.0, 0.2))

        body = client.get(url(source.id), params={"device_id": str(uuid.uuid4())})

        assert body.status_code == 200
        assert body.json()["results"] == []


class TestRefusals:
    def test_an_unknown_id_is_a_404(self, client: TestClient) -> None:
        assert client.get(url(uuid.uuid4())).status_code == 404

    def test_a_row_without_an_embedding_is_a_409(
        self, client: TestClient, repository: FakeImageRepository
    ) -> None:
        image = photo("never-indexed")
        repository.save(image)

        assert client.get(url(image.id)).status_code == 409

    def test_a_limit_above_the_maximum_is_a_400(
        self, client: TestClient, source: Image
    ) -> None:
        assert client.get(url(source.id), params={"limit": 101}).status_code == 400

    def test_a_radius_below_the_minimum_is_a_400(
        self, client: TestClient, source: Image
    ) -> None:
        response = client.get(
            url(source.id),
            params={"near_lat": -26.3, "near_lon": -48.8, "radius_m": 200},
        )

        assert response.status_code == 400
        assert "below the minimum" in response.json()["detail"]

    def test_half_a_circle_is_a_400_as_on_search(
        self, client: TestClient, source: Image
    ) -> None:
        response = client.get(url(source.id), params={"near_lat": -26.3})

        assert response.status_code == 400
        assert "missing: near_lon, radius_m" in response.json()["detail"]
