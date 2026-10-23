"""`GET /api/v1/images/browse` at the HTTP edge (RFC-044 section 5.1).

The real provider, `get_browse_images_use_case()`, over the fake repository,
so that "no model" is a test of the wiring: the model's provider is replaced by
one that fails if anything calls it, and the photos' disk is not plugged in.
The first test is the one every literal path of this router has: the route
must not be swallowed by `/{image_id}`.
"""

from __future__ import annotations

import datetime
import logging
import uuid
from collections.abc import Iterator
from typing import Any

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
from app.domain.value_objects.position_source import PositionSource
from app.infrastructure.config.settings import settings
from app.presentation.api import app
from app.presentation.dependencies import (
    get_embedding_model,
    get_image_repository,
    get_resolve_image_location_use_case,
)

URL = "/api/v1/images/browse"
LATITUDE, LONGITUDE = -26.32141, -48.81637
CIRCLE = {"near_lat": LATITUDE, "near_lon": LONGITUDE, "radius_m": 1000}


def place(
    repository: FakeImageRepository,
    name: str,
    captured_at: datetime.datetime | None = None,
    placed: bool = True,
) -> Image:
    image = Image(
        id=ImageId(uuid.uuid4()),
        device_id=TEST_DEVICE_ID,
        relative_path=ImagePath(f"obra/{name}.JPG"),
        filename=name,
        extension="jpg",
        captured_at=captured_at,
        capture_source=CaptureSource.EXIF_ORIGINAL if captured_at else None,
        latitude=LATITUDE if placed else None,
        longitude=LONGITUDE if placed else None,
        position_source=PositionSource.EXIF_GPS if placed else None,
    )
    repository.seed_embedding(image, EmbeddingVector([0.1] * 512))
    return image


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
        raise AssertionError("/browse asked for the embedding model")

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
def app_records(caplog: pytest.LogCaptureFixture) -> Iterator[pytest.LogCaptureFixture]:
    """The app's loggers do not propagate to the root, where `caplog` listens."""
    logger = logging.getLogger("app")
    previous = logger.level
    logger.addHandler(caplog.handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield caplog
    finally:
        logger.setLevel(previous)
        logger.removeHandler(caplog.handler)


def ids(body: dict[str, Any]) -> list[str]:
    return [result["id"] for result in body["results"]]


class TestTheRouteExists:
    def test_images_browse_is_a_200_not_a_422(self, client: TestClient) -> None:
        """Declared after `/{image_id}`, it would be `image_id="browse"`."""
        response = client.get(URL, params=CIRCLE)

        assert response.status_code == 200

    def test_without_a_model_and_with_the_disk_unplugged(
        self,
        client: TestClient,
        repository: FakeImageRepository,
        model_calls: list[str],
    ) -> None:
        image = place(repository, "DJI_0042", datetime.datetime(2018, 7, 14, 15, 32, 5))

        response = client.get(URL, params=CIRCLE)

        assert response.status_code == 200
        (result,) = response.json()["results"]
        assert result == {
            "id": str(image.id.value),
            "filename": "DJI_0042",
            "captured_at": "2018-07-14T15:32:05",
            "capture_source": "exif_original",
            "latitude": LATITUDE,
            "longitude": LONGITUDE,
            "position_source": "exif_gps",
            "device": {
                "id": str(TEST_DEVICE_ID.value),
                "label": "HD3",
                "connected": False,
            },
            "relative_path": "obra/DJI_0042.JPG",
            "absolute_path": None,
        }
        assert model_calls == []


class TestTheAnswer:
    def test_the_first_page_has_the_summary_and_no_similarity(
        self, client: TestClient, repository: FakeImageRepository
    ) -> None:
        old = place(repository, "old", datetime.datetime(2015, 1, 3, 9, 0))
        new = place(repository, "new", datetime.datetime(2026, 10, 2, 17, 45))
        undated = place(repository, "undated")
        place(repository, "nowhere", datetime.datetime(2020, 1, 1), placed=False)

        body = client.get(URL, params=CIRCLE).json()

        assert body["order"] == "newest"
        assert body["limit"] == settings.top_k_results
        assert ids(body) == [
            str(new.id.value),
            str(old.id.value),
            str(undated.id.value),
        ]
        assert all("similarity" not in result for result in body["results"])
        assert body["next_after"] is None
        assert body["summary"] == {
            "total": 3,
            "undated": 1,
            "oldest_captured_at": "2015-01-03T09:00:00",
            "newest_captured_at": "2026-10-02T17:45:00",
            "excluded_unknown_date": None,
            "excluded_unknown_position": 1,
        }

    def test_oldest_first(
        self, client: TestClient, repository: FakeImageRepository
    ) -> None:
        old = place(repository, "old", datetime.datetime(2015, 1, 3))
        new = place(repository, "new", datetime.datetime(2026, 10, 2))
        undated = place(repository, "undated")

        body = client.get(URL, params={**CIRCLE, "order": "oldest"}).json()

        assert body["order"] == "oldest"
        assert ids(body) == [
            str(old.id.value),
            str(new.id.value),
            str(undated.id.value),
        ]

    def test_page_after_page_with_next_after(
        self, client: TestClient, repository: FakeImageRepository
    ) -> None:
        images = [
            place(repository, f"m{month:02}", datetime.datetime(2025, month, 1))
            for month in range(1, 6)
        ]

        first = client.get(URL, params={**CIRCLE, "limit": 2}).json()
        second = client.get(
            URL, params={**CIRCLE, "limit": 2, "after": first["next_after"]}
        ).json()
        third = client.get(
            URL, params={**CIRCLE, "limit": 2, "after": second["next_after"]}
        ).json()

        newest = [str(image.id.value) for image in reversed(images)]
        assert ids(first) + ids(second) + ids(third) == newest
        assert first["next_after"] == newest[1]
        assert second["summary"] is None
        assert third["next_after"] is None

    def test_the_dates_of_a_range_count_the_undated_out(
        self, client: TestClient, repository: FakeImageRepository
    ) -> None:
        inside = place(repository, "inside", datetime.datetime(2018, 6, 1))
        place(repository, "undated")

        body = client.get(
            URL,
            params={
                **CIRCLE,
                "captured_from": "2018-01-01",
                "captured_to": "2019-01-01",
            },
        ).json()

        assert ids(body) == [str(inside.id.value)]
        assert body["summary"]["undated"] == 0
        assert body["summary"]["excluded_unknown_date"] == 1

    def test_no_filters_at_all_is_answered(
        self, client: TestClient, repository: FakeImageRepository
    ) -> None:
        place(repository, "here", datetime.datetime(2018, 1, 1))
        place(repository, "nowhere", datetime.datetime(2019, 1, 1), placed=False)

        body = client.get(URL).json()

        assert len(body["results"]) == 2
        assert body["summary"]["excluded_unknown_position"] is None


class TestRefusals:
    def test_an_unknown_order_is_a_422(self, client: TestClient) -> None:
        assert client.get(URL, params={**CIRCLE, "order": "random"}).status_code == 422

    def test_an_after_that_is_not_a_uuid_is_a_422(self, client: TestClient) -> None:
        response = client.get(URL, params={**CIRCLE, "after": "obra/DJI_0042.JPG"})

        assert response.status_code == 422

    def test_an_after_that_names_no_photo_is_a_404(
        self, client: TestClient, repository: FakeImageRepository
    ) -> None:
        place(repository, "a", datetime.datetime(2018, 1, 1))

        response = client.get(URL, params={**CIRCLE, "after": str(uuid.uuid4())})

        assert response.status_code == 404

    @pytest.mark.parametrize(
        "params",
        [
            {**CIRCLE, "limit": 0},
            {**CIRCLE, "limit": 101},
            {**CIRCLE, "radius_m": 1},
            {"near_lat": LATITUDE, "near_lon": LONGITUDE},
            {"captured_from": "2019-01-01", "captured_to": "2018-01-01"},
            {"captured_from": "2018-01-01T00:00:00Z"},
        ],
        ids=["limit-0", "limit-101", "radius-1", "half-circle", "inverted", "zoned"],
    )
    def test_the_searchs_400s(self, client: TestClient, params: dict[str, Any]) -> None:
        assert client.get(URL, params=params).status_code == 400


class TestTheLog:
    def test_one_line_without_coordinates_dates_or_cursor(
        self,
        client: TestClient,
        repository: FakeImageRepository,
        app_records: pytest.LogCaptureFixture,
    ) -> None:
        first = place(repository, "a", datetime.datetime(2018, 3, 4))
        place(repository, "b", datetime.datetime(2018, 2, 4))
        cursor = str(first.id.value)

        client.get(
            URL,
            params={
                **CIRCLE,
                "captured_from": "2018-01-07",
                "captured_to": "2018-12-29",
                "after": cursor,
                "order": "newest",
            },
        )

        lines = [
            record.getMessage()
            for record in app_records.records
            if "browse" in record.getMessage()
        ]
        assert len(lines) == 1
        (line,) = lines
        assert "order=newest" in line
        assert "cursor=True" in line
        assert "circle=True" in line
        assert "date_range=True" in line
        everything = "\n".join(record.getMessage() for record in app_records.records)
        for secret in ("26.32", "48.81", "2018-01-07", "2018-12-29", cursor):
            assert secret not in everything
