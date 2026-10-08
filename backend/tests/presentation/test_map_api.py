"""`GET /api/v1/images/map` at the HTTP edge (RFC-032 section 7).

The real `AggregatePositionsUseCase` over the fake repository, through
`app.dependency_overrides`: no database, no model. The first test is the one
RFC-032 names by itself -- the route must not be swallowed by `/{image_id}`,
which would refuse it as a malformed UUID with a 422 for a route that exists.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from tests.application.fakes import FakeImageRepository
from tests.conftest import TEST_DEVICE_ID

from app.application.use_cases.aggregate_positions import AggregatePositionsUseCase
from app.domain.entities.image import Image
from app.domain.value_objects.capture_source import CaptureSource
from app.domain.value_objects.device_id import DeviceId
from app.domain.value_objects.embedding_vector import EmbeddingVector
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.position_source import PositionSource
from app.presentation.api import app
from app.presentation.dependencies import (
    get_aggregate_positions_use_case,
    get_image_details_use_case,
    get_search_images_use_case,
)

MAP_URL = "/api/v1/images/map"
AREA = {"min_lat": -27, "min_lon": -49.5, "max_lat": -26, "max_lon": -48.5}
EMBEDDING = EmbeddingVector([0.1] * 512)


def place(
    repository: FakeImageRepository,
    latitude: float | None,
    longitude: float | None,
    device_id: uuid.UUID | None = None,
    captured_at: datetime.datetime | None = None,
) -> None:
    image = Image(
        id=ImageId(uuid.uuid4()),
        device_id=TEST_DEVICE_ID if device_id is None else DeviceId(device_id),
        relative_path=ImagePath(f"fotos/{uuid.uuid4().hex}.jpg"),
        filename="photo",
        extension="jpg",
        captured_at=captured_at,
        capture_source=CaptureSource.EXIF_ORIGINAL if captured_at else None,
        latitude=latitude,
        longitude=longitude,
        position_source=(
            PositionSource.EXIF_GPS if latitude is not None else PositionSource.UNKNOWN
        ),
    )
    repository.seed_embedding(image, EMBEDDING)


@pytest.fixture
def repository() -> FakeImageRepository:
    return FakeImageRepository()


@pytest.fixture
def client(repository: FakeImageRepository) -> Iterator[TestClient]:
    app.dependency_overrides[get_aggregate_positions_use_case] = lambda: (
        AggregatePositionsUseCase(repository=repository, max_cells=1000)
    )
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


class TestTheRouteExists:
    def test_images_map_is_a_200_not_a_422(
        self, client: TestClient, repository: FakeImageRepository
    ) -> None:
        """RFC-032 section 7's trap: declared after `/{image_id}` this would be 422."""
        response = client.get(MAP_URL, params=AREA)

        assert response.status_code == 200

    def test_the_details_route_is_never_reached_for_map(
        self, client: TestClient
    ) -> None:
        """A route that answered `image_id="map"` would have to ask this use case."""
        calls: list[object] = []

        def refuse() -> object:
            calls.append("details")
            raise AssertionError("/images/map reached /{image_id}")

        app.dependency_overrides[get_image_details_use_case] = refuse

        client.get(MAP_URL, params=AREA)

        assert calls == []

    def test_the_map_never_composes_the_search_model(self, client: TestClient) -> None:
        """The map ignores the query, so it has no business loading CLIP."""

        def refuse() -> object:
            raise AssertionError("the map composed the search use case")

        app.dependency_overrides[get_search_images_use_case] = refuse

        assert client.get(MAP_URL, params=AREA).status_code == 200


class TestTheBody:
    def test_cells_precision_and_the_unplaced_count(
        self, client: TestClient, repository: FakeImageRepository
    ) -> None:
        place(repository, -26.32141, -48.81631)
        place(repository, -26.32139, -48.81629)
        place(repository, None, None)

        body = client.get(MAP_URL, params=AREA).json()

        assert body == {
            "precision_applied": 3,
            "cells": [{"latitude": -26.321, "longitude": -48.816, "count": 2}],
            "excluded_unknown_position": 1,
        }

    def test_the_query_text_is_ignored(
        self, client: TestClient, repository: FakeImageRepository
    ) -> None:
        """Two questions, two endpoints: `q` changes nothing here (§7)."""
        place(repository, -26.5, -49.0)

        without = client.get(MAP_URL, params=AREA).json()
        with_query = client.get(MAP_URL, params={**AREA, "q": "telhado"}).json()

        assert with_query == without

    def test_the_search_filters_apply_by_the_same_parameters(
        self, client: TestClient, repository: FakeImageRepository
    ) -> None:
        other = uuid.uuid4()
        place(repository, -26.5, -49.0, captured_at=datetime.datetime(2018, 7, 1))
        place(repository, -26.6, -49.1, captured_at=datetime.datetime(2011, 7, 1))
        place(repository, -26.7, -49.2, device_id=other)

        body = client.get(
            MAP_URL,
            params={
                **AREA,
                "device_id": str(TEST_DEVICE_ID.value),
                "captured_from": "2018-01-01",
                "captured_to": "2019-01-01",
            },
        ).json()

        assert body["cells"] == [{"latitude": -26.5, "longitude": -49.0, "count": 1}]


class TestTheCeiling:
    def test_too_many_cells_coarsen_and_the_response_says_so(
        self, repository: FakeImageRepository
    ) -> None:
        for index in range(12):
            place(repository, -26.9 + index * 0.05, -49.4 + index * 0.05)
        app.dependency_overrides[get_aggregate_positions_use_case] = lambda: (
            AggregatePositionsUseCase(repository=repository, max_cells=5)
        )
        try:
            with TestClient(app) as client:
                body = client.get(MAP_URL, params={**AREA, "precision": 3}).json()
        finally:
            app.dependency_overrides.clear()

        assert body["precision_applied"] < 3
        assert len(body["cells"]) <= 5
        assert sum(cell["count"] for cell in body["cells"]) == 12

    def test_an_omitted_precision_is_the_default(self, client: TestClient) -> None:
        assert client.get(MAP_URL, params=AREA).json()["precision_applied"] == 3


class TestInvalidRequests:
    def test_an_inverted_latitude_is_a_400(self, client: TestClient) -> None:
        response = client.get(MAP_URL, params={**AREA, "min_lat": -25, "max_lat": -26})

        assert response.status_code == 400
        assert "north" in response.json()["detail"]

    def test_a_box_across_the_antimeridian_is_a_400(self, client: TestClient) -> None:
        """Declared unsupported in RFC-032 section 11, and said so."""
        response = client.get(MAP_URL, params={**AREA, "min_lon": 170, "max_lon": -170})

        assert response.status_code == 400
        assert "180th meridian" in response.json()["detail"]

    def test_a_corner_off_the_planet_is_a_400(self, client: TestClient) -> None:
        response = client.get(MAP_URL, params={**AREA, "min_lat": -91})

        assert response.status_code == 400

    @pytest.mark.parametrize("missing", ["min_lat", "min_lon", "max_lat", "max_lon"])
    def test_a_missing_corner_is_a_422(self, client: TestClient, missing: str) -> None:
        params = {name: value for name, value in AREA.items() if name != missing}

        assert client.get(MAP_URL, params=params).status_code == 422

    def test_a_negative_precision_is_a_422(self, client: TestClient) -> None:
        assert client.get(MAP_URL, params={**AREA, "precision": -1}).status_code == 422

    def test_a_corner_that_is_not_a_number_is_a_422(self, client: TestClient) -> None:
        assert client.get(MAP_URL, params={**AREA, "min_lat": "sul"}).status_code == 422
