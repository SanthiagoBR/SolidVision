"""Level 1 of RFC-026's three: the route, and nothing underneath it.

`TestClient` plus `app.dependency_overrides`, so no database, no model,
and no network are involved. What this level can prove is exactly what
the route is responsible for -- the status code, the shape of the body,
and that the two query parameters reach the use case unchanged and come
back unrearranged. What it cannot prove is that any of it is wired to
anything real, which is why levels 2 and 3 exist.

The error tests deliberately use a *real* `SearchImagesUseCase` composed
with in-memory fakes rather than a stub that raises on cue. A stub told
to raise `EmptySearchQueryError` would return 400 even if the use case
had stopped rejecting blank queries, which is the one thing those tests
are for; the real use case with fake collaborators is still offline and
still fast, because validation refuses the request before either
collaborator is touched.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Iterator
from pathlib import Path

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
from app.application.use_cases.search_images import (
    MAX_SEARCH_LIMIT,
    SearchImagesUseCase,
)
from app.domain.entities.image import Image
from app.domain.value_objects.capture_source import CaptureSource
from app.domain.value_objects.date_range import DateRange
from app.domain.value_objects.device_id import DeviceId
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.search_filters import SearchFilters
from app.domain.value_objects.search_hit import SearchHit
from app.infrastructure.ai.fake_embedding_model import FakeEmbeddingModel
from app.infrastructure.config.settings import settings
from app.presentation.api import app
from app.presentation.dependencies import (
    get_resolve_image_location_use_case,
    get_search_images_use_case,
)

SEARCH_URL = "/api/v1/images/search"


class RecordingSearchUseCase:
    """A stand-in that returns what it is told and remembers what it was asked.

    Not a subclass of `SearchImagesUseCase`: the point is to observe the
    call the route makes without any of the behaviour underneath it, and
    FastAPI does not validate the type of a `Depends`-provided argument.
    """

    def __init__(self, hits: list[SearchHit]) -> None:
        self.hits = hits
        self.calls: list[tuple[str, int | None]] = []
        self.filters: list[SearchFilters | None] = []
        self.excluded_unknown_date: int | None = None
        self.count_calls: list[SearchFilters | None] = []

    def execute(
        self,
        query: str,
        limit: int | None = None,
        filters: SearchFilters | None = None,
    ) -> list[SearchHit]:
        self.calls.append((query, limit))
        self.filters.append(filters)
        return self.hits

    def count_hidden_by_unknown_date(self, filters: SearchFilters | None) -> int | None:
        self.count_calls.append(filters)
        return self.excluded_unknown_date


def make_hit(
    filename: str,
    similarity: float,
    captured_at: datetime.datetime | None = None,
    capture_source: CaptureSource | None = None,
) -> SearchHit:
    image = Image(
        id=ImageId(uuid.uuid4()),
        device_id=TEST_DEVICE_ID,
        relative_path=ImagePath(f"private/photos/{filename}.jpg"),
        filename=filename,
        extension="jpg",
        captured_at=captured_at,
        capture_source=capture_source,
    )
    return SearchHit(image=image, similarity=similarity)


@pytest.fixture
def locator() -> FakeDeviceLocator:
    """Where the test device is mounted: nowhere, unless a test connects it.

    Installed for every test in this file, because since RFC-030 the route
    resolves each hit's location after searching. Without the override
    that would reach the real device repository -- a database session --
    and the real volume enumeration, neither of which this level may touch.
    """
    fake = FakeDeviceLocator()
    app.dependency_overrides[get_resolve_image_location_use_case] = lambda: (
        ResolveImageLocationUseCase(
            device_repository=FakeDeviceRepository([make_test_device("HD-TEST")]),
            device_locator=fake,
        )
    )
    return fake


@pytest.fixture
def client(locator: FakeDeviceLocator) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def stub() -> RecordingSearchUseCase:
    """Install a recording stub as the search use case for one test."""
    use_case = RecordingSearchUseCase(hits=[])
    app.dependency_overrides[get_search_images_use_case] = lambda: use_case
    return use_case


@pytest.fixture
def real_use_case() -> SearchImagesUseCase:
    """Install the real use case over fakes, for the validation tests."""
    use_case = SearchImagesUseCase(
        repository=FakeImageRepository(),
        embedding_model=FakeEmbeddingModel(),
        default_limit=settings.top_k_results,
    )
    app.dependency_overrides[get_search_images_use_case] = lambda: use_case
    return use_case


def test_a_normal_query_returns_the_documented_body(
    client: TestClient, stub: RecordingSearchUseCase
) -> None:
    hit = make_hit("fish_ponds_02", 0.3255)
    stub.hits = [hit]

    response = client.get(SEARCH_URL, params={"q": "fish ponds", "limit": 10})

    assert response.status_code == 200
    assert response.json() == {
        "query": "fish ponds",
        "limit": 10,
        "results": [
            {
                "id": str(hit.image.id.value),
                "filename": "fish_ponds_02",
                "similarity": 0.3255,
                "captured_at": None,
                "capture_source": None,
                "device": {
                    "id": str(TEST_DEVICE_ID.value),
                    "label": "HD-TEST",
                    "connected": False,
                },
                "relative_path": "private/photos/fish_ponds_02.jpg",
                "absolute_path": None,
            }
        ],
        "excluded_unknown_date": None,
    }


def test_the_response_echoes_the_raw_query_not_the_prompt(
    client: TestClient, stub: RecordingSearchUseCase
) -> None:
    """RFC-026 section 5.2: the client gets back what it sent.

    The string CLIP is actually given for this query is
    `a photo of . a rural property with a lake` -- the RFC-023 template
    wrapped around Marian's output, leading `". "` included (RFC-025
    section 11.5). Echoing that would publish an artifact of the
    translation path as part of the contract, and would change this
    response the day the template changes.
    """
    query = "uma propriedade rural com um lago"

    response = client.get(SEARCH_URL, params={"q": query})

    assert response.json()["query"] == query
    assert "a photo of" not in response.json()["query"]


def test_an_omitted_limit_falls_back_to_the_configured_default(
    client: TestClient, stub: RecordingSearchUseCase
) -> None:
    response = client.get(SEARCH_URL, params={"q": "fish ponds"})

    assert response.status_code == 200
    assert response.json()["limit"] == settings.top_k_results
    assert stub.calls == [("fish ponds", settings.top_k_results)]


def test_the_route_passes_both_parameters_through_unmodified(
    client: TestClient, stub: RecordingSearchUseCase
) -> None:
    """No trimming, casing, or rewriting on the way in (RFC-026 section 4).

    The route is not allowed to decide what a query means. Whitespace is
    significant to a tokenizer, and deciding a blank query is unusable is
    the Application layer's call, made once, in one place.
    """
    response = client.get(SEARCH_URL, params={"q": "  Fish  PONDS  ", "limit": 3})

    assert response.status_code == 200
    assert stub.calls == [("  Fish  PONDS  ", 3)]
    assert response.json()["query"] == "  Fish  PONDS  "


def test_results_keep_the_repositorys_order(
    client: TestClient, stub: RecordingSearchUseCase
) -> None:
    """The ranking belongs to the database, and survives the trip out.

    The similarities here are deliberately out of descending order. A
    route that re-sorted -- a natural-looking "make sure the best is
    first" -- would reorder them and pass every other test in this file.
    """
    stub.hits = [
        make_hit("second", 0.10),
        make_hit("first", 0.90),
        make_hit("third", 0.50),
    ]

    response = client.get(SEARCH_URL, params={"q": "anything"})

    filenames = [result["filename"] for result in response.json()["results"]]
    assert filenames == ["second", "first", "third"]


def test_similarity_survives_as_a_float_including_negative_values(
    client: TestClient, stub: RecordingSearchUseCase
) -> None:
    """RFC-025 measured -0.1183 as a real score; it must not be clamped.

    Rescaling into [0, 1] for a progress bar would collapse "unrelated"
    (~0) and "opposite" (~-1) into the same neighbourhood, and this layer
    knows the least about what the number means.
    """
    stub.hits = [make_hit("opposite", -0.1183), make_hit("related", 0.3737)]

    results = client.get(SEARCH_URL, params={"q": "anything"}).json()["results"]

    assert [result["similarity"] for result in results] == [-0.1183, 0.3737]
    assert all(isinstance(result["similarity"], float) for result in results)


def test_a_result_publishes_where_the_file_is_and_nothing_more(
    client: TestClient, stub: RecordingSearchUseCase
) -> None:
    """RFC-030 reversed RFC-026 here, and this test used to pin the opposite.

    It was `test_no_result_exposes_a_server_path`, asserting that
    `private/photos` never appeared in a response. RFC-030 section 2 kept
    one of RFC-026's two arguments -- a path is not an identifier -- and
    dropped the other, because a local-first server's filesystem is the
    user's own. The field set is still pinned exactly: the paths arrive
    under their own names and nowhere else.
    """
    stub.hits = [make_hit("fish_ponds_02", 0.3255)]

    response = client.get(SEARCH_URL, params={"q": "fish ponds"})

    (result,) = response.json()["results"]
    assert set(result) == {
        "id",
        "filename",
        "similarity",
        "captured_at",
        "capture_source",
        "device",
        "relative_path",
        "absolute_path",
    }
    assert set(result["device"]) == {"id", "label", "connected"}
    assert result["relative_path"] == "private/photos/fish_ponds_02.jpg"


def test_a_hit_on_a_disconnected_disk_is_a_200_with_no_absolute_path(
    client: TestClient, stub: RecordingSearchUseCase
) -> None:
    """RFC-030 section 4.1: "it is on HD-TEST, in private/photos" is the answer."""
    stub.hits = [make_hit("fish_ponds_02", 0.3255)]

    response = client.get(SEARCH_URL, params={"q": "fish ponds"})

    assert response.status_code == 200
    (result,) = response.json()["results"]
    assert result["absolute_path"] is None
    assert result["device"]["connected"] is False
    assert result["device"]["label"] == "HD-TEST"


def test_a_hit_on_a_connected_disk_carries_a_forward_slashed_absolute_path(
    client: TestClient,
    stub: RecordingSearchUseCase,
    locator: FakeDeviceLocator,
    tmp_path: Path,
) -> None:
    """Pitfall 9 of the build prompt: `ImagePath` publishes `/`, never `\\`.

    A JSON client should not have to know which operating system answered
    in order to split a path.
    """
    locator.connect(TEST_DEVICE_ID, tmp_path)
    stub.hits = [make_hit("fish_ponds_02", 0.3255)]

    (result,) = client.get(SEARCH_URL, params={"q": "fish ponds"}).json()["results"]

    assert result["device"]["connected"] is True
    expected = (tmp_path / "private/photos/fish_ponds_02.jpg").as_posix()
    assert result["absolute_path"] == expected
    assert "\\" not in result["absolute_path"]


def test_the_disk_is_asked_about_once_per_page_not_once_per_hit(
    client: TestClient, stub: RecordingSearchUseCase, locator: FakeDeviceLocator
) -> None:
    """RFC-030 section 4.2, at the edge: ten hits on one disk, one enumeration."""
    stub.hits = [make_hit(f"photo_{index}", 0.5) for index in range(10)]

    client.get(SEARCH_URL, params={"q": "anything"})

    assert locator.mount_point_calls == [TEST_DEVICE_ID]


def test_an_empty_result_set_is_a_200_not_a_404(
    client: TestClient, stub: RecordingSearchUseCase
) -> None:
    """No image matched is a successful search that found nothing.

    404 would say the *endpoint* was not found, which is a different
    claim and one a client would reasonably retry differently.
    """
    stub.hits = []

    response = client.get(SEARCH_URL, params={"q": "no such thing"})

    assert response.status_code == 200
    assert response.json()["results"] == []


def test_a_blank_query_is_refused_with_400(
    client: TestClient, real_use_case: SearchImagesUseCase
) -> None:
    response = client.get(SEARCH_URL, params={"q": "   "})

    assert response.status_code == 400
    assert response.json() == {
        "detail": "Search query cannot be empty or only whitespace."
    }


@pytest.mark.parametrize("limit", [0, -1, MAX_SEARCH_LIMIT + 1])
def test_a_limit_outside_application_policy_is_refused_with_400(
    client: TestClient, real_use_case: SearchImagesUseCase, limit: int
) -> None:
    """400 rather than 422, because the request is well formed.

    `limit=101` parses, matches the endpoint, and is then refused by
    policy -- which is what 400 means. A 422 here would mean the route
    had grown its own copy of `MAX_SEARCH_LIMIT` (RFC-026 section 8.2).
    """
    response = client.get(SEARCH_URL, params={"q": "fish ponds", "limit": limit})

    assert response.status_code == 400
    assert "Search limit must be" in response.json()["detail"]


def test_a_missing_query_parameter_is_a_422(
    client: TestClient, stub: RecordingSearchUseCase
) -> None:
    """The other half of the boundary: not a well-formed request at all.

    Flattening this into 400 for tidiness would erase the difference
    between a request this endpoint cannot parse and one it understood
    and refused.
    """
    response = client.get(SEARCH_URL)

    assert response.status_code == 422
    assert stub.calls == []


def test_a_non_integer_limit_is_a_422(
    client: TestClient, stub: RecordingSearchUseCase
) -> None:
    response = client.get(SEARCH_URL, params={"q": "fish ponds", "limit": "abc"})

    assert response.status_code == 422
    assert stub.calls == []


class TestDeviceFilterParameter:
    """RFC-027 section 9, at the HTTP edge.

    The route does one thing with `device_id`: turns it into a
    `SearchFilters` and hands it down. Everything about what a filter
    *means* is below this layer, so what these cases pin is the
    translation -- and specifically the one translation that can be wrong
    in a way nothing else notices.
    """

    def test_an_omitted_device_id_means_every_device(
        self, client: TestClient, stub: RecordingSearchUseCase
    ) -> None:
        """Absent must mean *all*, never *none*.

        A route that turned a missing parameter into a one-element or
        empty `IN ()` would make every ordinary search return zero
        results, and the request that caused it would look completely
        normal.
        """
        client.get(SEARCH_URL, params={"q": "lake"})

        (filters,) = stub.filters
        assert filters is not None
        assert filters.is_empty()
        assert filters.device_ids == frozenset()

    def test_one_device_id_reaches_the_use_case(
        self, client: TestClient, stub: RecordingSearchUseCase
    ) -> None:
        device_id = uuid.uuid4()

        client.get(SEARCH_URL, params={"q": "lake", "device_id": str(device_id)})

        (filters,) = stub.filters
        assert filters is not None
        assert filters.device_ids == frozenset({DeviceId(device_id)})

    def test_the_parameter_repeats_for_several_devices(
        self, client: TestClient, stub: RecordingSearchUseCase
    ) -> None:
        first, second = uuid.uuid4(), uuid.uuid4()

        client.get(
            SEARCH_URL,
            params=[
                ("q", "lake"),
                ("device_id", str(first)),
                ("device_id", str(second)),
            ],
        )

        (filters,) = stub.filters
        assert filters is not None
        assert filters.device_ids == frozenset({DeviceId(first), DeviceId(second)})

    def test_a_repeated_device_id_is_carried_once(
        self, client: TestClient, stub: RecordingSearchUseCase
    ) -> None:
        """It is a set: a disk named twice is one disk."""
        device_id = uuid.uuid4()

        client.get(
            SEARCH_URL,
            params=[
                ("q", "lake"),
                ("device_id", str(device_id)),
                ("device_id", str(device_id)),
            ],
        )

        (filters,) = stub.filters
        assert filters is not None
        assert len(filters.device_ids) == 1

    def test_a_malformed_device_id_is_rejected_by_validation(
        self, client: TestClient, stub: RecordingSearchUseCase
    ) -> None:
        """422, and the use case is never reached.

        The id is declared as a `UUID`, so a client sending "HD2" gets a
        well-formed complaint from FastAPI rather than a search that
        quietly matches nothing.
        """
        response = client.get(SEARCH_URL, params={"q": "lake", "device_id": "HD2"})

        assert response.status_code == 422
        assert stub.calls == []

    def test_an_unknown_device_id_is_a_valid_request(
        self, client: TestClient, stub: RecordingSearchUseCase
    ) -> None:
        """A filter restricts a set; it does not assert that it exists.

        A search naming a disk the system has never seen correctly matches
        nothing, and answering 200 with no results says exactly that.
        """
        response = client.get(
            SEARCH_URL, params={"q": "lake", "device_id": str(uuid.uuid4())}
        )

        assert response.status_code == 200
        assert response.json()["results"] == []

    def test_the_response_shape_is_unchanged_by_filtering(
        self, client: TestClient, stub: RecordingSearchUseCase
    ) -> None:
        """RFC-027 narrows the question; RFC-030 changed the answer, for both.

        A device filter adds an input and nothing else: the fields present
        are the same with or without it. Until RFC-030 this also asserted
        that no field named a device; RFC-030 added `device` to every
        result, filtered or not, which is the property that remains.
        """
        stub.hits = [make_hit("fish_ponds_02", 0.3255)]

        unfiltered = client.get(SEARCH_URL, params={"q": "lake"}).json()
        filtered = client.get(
            SEARCH_URL, params={"q": "lake", "device_id": str(uuid.uuid4())}
        ).json()

        assert set(filtered) == set(unfiltered)
        assert set(filtered["results"][0]) == set(unfiltered["results"][0])
        assert "device" in unfiltered["results"][0]
        assert filtered["excluded_unknown_date"] is None


SHOT = datetime.datetime(2018, 7, 14, 15, 32, 5)


class TestCaptureDateParameters:
    """RFC-028 at the HTTP edge: two optional bounds, camera-local, zoneless."""

    def test_omitted_bounds_mean_no_date_filter(
        self, client: TestClient, stub: RecordingSearchUseCase
    ) -> None:
        """Not `[min, max)`, which would silently drop every undated photo."""
        client.get(SEARCH_URL, params={"q": "lake"})

        (filters,) = stub.filters
        assert filters is not None
        assert filters.captured_between is None
        assert filters.is_empty()

    def test_both_bounds_become_a_half_open_range(
        self, client: TestClient, stub: RecordingSearchUseCase
    ) -> None:
        client.get(
            SEARCH_URL,
            params={
                "q": "lake",
                "captured_from": "2018-01-01",
                "captured_to": "2019-01-01T00:00:00",
            },
        )

        (filters,) = stub.filters
        assert filters is not None
        assert filters.captured_between == DateRange(
            datetime.datetime(2018, 1, 1), datetime.datetime(2019, 1, 1)
        )

    def test_only_a_start_leaves_the_end_open(
        self, client: TestClient, stub: RecordingSearchUseCase
    ) -> None:
        client.get(SEARCH_URL, params={"q": "lake", "captured_from": "2018-01-01"})

        (filters,) = stub.filters
        assert filters is not None
        assert filters.captured_between == DateRange(
            datetime.datetime(2018, 1, 1), datetime.datetime.max
        )

    def test_only_an_end_leaves_the_start_open(
        self, client: TestClient, stub: RecordingSearchUseCase
    ) -> None:
        client.get(SEARCH_URL, params={"q": "lake", "captured_to": "2019-01-01"})

        (filters,) = stub.filters
        assert filters is not None
        assert filters.captured_between == DateRange(
            datetime.datetime.min, datetime.datetime(2019, 1, 1)
        )

    def test_the_bounds_reach_the_use_case_naive(
        self, client: TestClient, stub: RecordingSearchUseCase
    ) -> None:
        client.get(
            SEARCH_URL,
            params={"q": "lake", "captured_from": "2018-01-01T10:00:00"},
        )

        (filters,) = stub.filters
        assert filters is not None
        assert filters.captured_between is not None
        assert filters.captured_between.start.tzinfo is None

    @pytest.mark.parametrize(
        "bound",
        [
            "2018-01-01T00:00:00Z",
            "2018-01-01T00:00:00-03:00",
            "2018-01-01T00:00:00+00:00",
        ],
        ids=["utc-z", "negative-offset", "zero-offset"],
    )
    @pytest.mark.parametrize("parameter", ["captured_from", "captured_to"])
    def test_a_bound_with_a_zone_is_refused_not_converted(
        self,
        client: TestClient,
        real_use_case: SearchImagesUseCase,
        parameter: str,
        bound: str,
    ) -> None:
        """400 with a message about the zone -- never a silent conversion.

        Converting would need a zone to convert into, and a camera-local
        capture date has none (RFC-028 section 5). Refused with 400 rather
        than 422 because the value parses; it is the application that
        refuses what it means.
        """
        response = client.get(SEARCH_URL, params={"q": "lake", parameter: bound})

        assert response.status_code == 400
        assert "time zone" in response.json()["detail"]

    def test_a_unix_timestamp_is_refused_as_zoned(
        self, client: TestClient, real_use_case: SearchImagesUseCase
    ) -> None:
        """Pydantic reads `1514764800` as an aware UTC instant; that is a zone too."""
        response = client.get(
            SEARCH_URL, params={"q": "lake", "captured_from": "1514764800"}
        )

        assert response.status_code == 400

    def test_an_inverted_range_is_refused_with_400(
        self, client: TestClient, real_use_case: SearchImagesUseCase
    ) -> None:
        response = client.get(
            SEARCH_URL,
            params={
                "q": "lake",
                "captured_from": "2019-01-01",
                "captured_to": "2018-01-01",
            },
        )

        assert response.status_code == 400
        assert "after its end" in response.json()["detail"]

    def test_a_value_that_is_not_a_date_is_a_422(
        self, client: TestClient, stub: RecordingSearchUseCase
    ) -> None:
        response = client.get(
            SEARCH_URL, params={"q": "lake", "captured_from": "last summer"}
        )

        assert response.status_code == 422
        assert stub.calls == []

    def test_an_equal_from_and_to_is_a_valid_empty_range(
        self, client: TestClient, stub: RecordingSearchUseCase
    ) -> None:
        response = client.get(
            SEARCH_URL,
            params={
                "q": "lake",
                "captured_from": "2018-01-01",
                "captured_to": "2018-01-01",
            },
        )

        assert response.status_code == 200


class TestCaptureDateInTheResponse:
    def test_captured_at_is_serialized_without_z_or_offset(
        self, client: TestClient, stub: RecordingSearchUseCase
    ) -> None:
        """RFC-028 section 5, at the last place it could be undone.

        A trailing `Z` would tell every client that the camera clock read
        UTC, and a client converting it to local time would move the shot
        -- New Year's Eve into the next year -- which is the exact error
        the zoneless column exists to prevent.
        """
        stub.hits = [make_hit("dji", 0.5, SHOT, CaptureSource.EXIF_ORIGINAL)]

        response = client.get(SEARCH_URL, params={"q": "lake"})

        (result,) = response.json()["results"]
        assert result["captured_at"] == "2018-07-14T15:32:05"
        assert '"captured_at":"2018-07-14T15:32:05"' in response.text
        assert "2018-07-14T15:32:05Z" not in response.text
        assert "2018-07-14T15:32:05+" not in response.text
        assert "2018-07-14T15:32:05-" not in response.text
        assert result["capture_source"] == "exif_original"

    def test_an_unknown_date_is_null_with_its_source(
        self, client: TestClient, stub: RecordingSearchUseCase
    ) -> None:
        stub.hits = [
            make_hit("scan", 0.5, None, CaptureSource.UNKNOWN),
            make_hit("legacy", 0.4),
        ]

        results = client.get(SEARCH_URL, params={"q": "lake"}).json()["results"]

        assert results[0]["captured_at"] is None
        assert results[0]["capture_source"] == "unknown"
        assert results[1]["captured_at"] is None
        assert results[1]["capture_source"] is None

    def test_the_excluded_count_is_published_for_a_date_filter(
        self, client: TestClient, stub: RecordingSearchUseCase
    ) -> None:
        """RFC-028 section 4.1: an empty 2018 must not read as "no such photo"."""
        stub.excluded_unknown_date = 312

        body = client.get(
            SEARCH_URL, params={"q": "lake", "captured_from": "2018-01-01"}
        ).json()

        assert body["excluded_unknown_date"] == 312
        (filters,) = stub.count_calls
        assert filters is not None
        assert filters.captured_between is not None

    def test_the_excluded_count_is_null_without_a_date_filter(
        self, client: TestClient, real_use_case: SearchImagesUseCase
    ) -> None:
        """Null, not 0: there was no date filter to hide anything."""
        body = client.get(SEARCH_URL, params={"q": "lake"}).json()

        assert body["excluded_unknown_date"] is None
