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

import datetime
import uuid
from collections.abc import Iterator

import pytest
from tests.application.fakes import FakeImageRepository
from tests.conftest import TEST_DEVICE_ID

from app.domain.entities.device import Device
from app.domain.entities.image import Image
from app.domain.exceptions import EmbeddingDimensionMismatchError
from app.domain.repositories.image_repository import ImageRepository
from app.domain.value_objects.capture_date import CaptureDate
from app.domain.value_objects.capture_source import CaptureSource
from app.domain.value_objects.date_range import DateRange
from app.domain.value_objects.device_id import DeviceId, VolumeIdentity, VolumeKind
from app.domain.value_objects.embedding_vector import EmbeddingVector
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.indexing_record import IndexingRecord
from app.domain.value_objects.search_filters import SearchFilters
from app.infrastructure.database.models.image_model import EMBEDDING_DIMENSION
from app.infrastructure.filesystem.device_identity import compute_device_id
from app.infrastructure.persistence.in_memory_image_repository import (
    InMemoryImageRepository,
)
from app.infrastructure.persistence.postgres_device_repository import (
    PostgresDeviceRepository,
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


SECOND_VOLUME_IDENTITY = VolumeIdentity(
    value="\\\\?\\Volume{00000000-0000-0000-0000-0000000000fe}\\",
    kind=VolumeKind.WINDOWS_VOLUME_GUID,
)
SECOND_DEVICE_ID: DeviceId = compute_device_id(SECOND_VOLUME_IDENTITY)


@pytest.fixture()
def second_device(request: pytest.FixtureRequest) -> DeviceId:
    """A second disk for the filter cases to exclude.

    Real only where it has to be. PostgreSQL enforces
    `images.device_id -> devices.id`, so the row must exist there before
    an image can point at it; the in-memory implementations have no such
    constraint and nothing to create. Writing it unconditionally would
    make every in-memory round of this file need a database.
    """
    if "postgres" in request.node.callspec.id:
        now = datetime.datetime.now(tz=datetime.UTC)
        PostgresDeviceRepository(request.getfixturevalue("empty_db_session")).save(
            Device(
                id=SECOND_DEVICE_ID,
                volume_identity=SECOND_VOLUME_IDENTITY,
                label="SECOND-DEVICE",
                first_seen_at=now,
                last_seen_at=now,
            )
        )
    return SECOND_DEVICE_ID


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


def _image(
    image_id: uuid.UUID | None = None,
    device_id: DeviceId | None = None,
    capture: CaptureDate | None = None,
) -> Image:
    unique = uuid.uuid4().hex
    return Image(
        id=ImageId(image_id or uuid.uuid4()),
        device_id=device_id or TEST_DEVICE_ID,
        relative_path=ImagePath(f"images/search/{unique}.png"),
        filename=unique,
        extension="png",
        captured_at=capture.captured_at if capture else None,
        capture_source=capture.source if capture else None,
    )


def index_image(
    repository: ImageRepository,
    embedding: EmbeddingVector,
    image_id: uuid.UUID | None = None,
    device_id: DeviceId | None = None,
    captured_at: datetime.datetime | None = None,
    capture: CaptureDate | None = None,
) -> Image:
    """Persist one searchable image, through the same door production uses.

    `captured_at` is the short form for the common case -- a date read from
    `DateTimeOriginal`; `capture` states any other examined outcome,
    including `CaptureDate.unknown()`.
    """
    if captured_at is not None:
        capture = CaptureDate(captured_at, CaptureSource.EXIF_ORIGINAL)
    image = _image(image_id, device_id, capture)
    repository.save_indexed(
        IndexingRecord(
            image=image,
            embedding=embedding,
            file_size=1,
            file_modified_at=None,
        )
    )
    return image


def store_without_embedding(
    repository: ImageRepository, captured_at: datetime.datetime | None = None
) -> Image:
    """Persist an image the way `IndexImageUseCase` does: no embedding at all."""
    capture = (
        CaptureDate(captured_at, CaptureSource.EXIF_ORIGINAL) if captured_at else None
    )
    image = _image(capture=capture)
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


class TestDeviceFilter:
    """RFC-027 section 9: the first thing a search may be narrowed by.

    RFC-025 scoped search globally and named the condition for changing
    that -- a real table, a real foreign key, ownership rules. Devices
    satisfy it, so these cases join the ones above rather than replacing
    them: the unfiltered contract is still the contract, and a filtered
    search has to obey every clause of it within its subset.

    The second device is `SECOND_DEVICE_ID`, which
    `second_device_in_the_database` makes real for the PostgreSQL round
    only -- the in-memory implementations have no foreign key to satisfy.
    """

    def test_an_absent_filter_searches_every_device(
        self, repository: ImageRepository, second_device: DeviceId
    ) -> None:
        here = index_image(repository, one_hot(0))
        elsewhere = index_image(repository, one_hot(1), device_id=second_device)

        hits = repository.search_similar(one_hot(0), limit=10)

        assert {hit.image for hit in hits} == {here, elsewhere}

    def test_an_empty_filter_is_the_same_as_no_filter(
        self, repository: ImageRepository, second_device: DeviceId
    ) -> None:
        """The bug this case exists to prevent, stated as a test.

        An implementation that always emitted `device_id IN (...)` would
        turn `SearchFilters()` into an empty `IN ()`, which matches
        nothing -- so every unfiltered search would silently return zero
        results, and a defaulted argument would be the cause.
        """
        index_image(repository, one_hot(0))
        index_image(repository, one_hot(1), device_id=second_device)

        unfiltered = repository.search_similar(one_hot(0), limit=10)
        empty_filter = repository.search_similar(
            one_hot(0), limit=10, filters=SearchFilters()
        )

        assert len(empty_filter) == len(unfiltered) == 2
        assert [hit.image for hit in empty_filter] == [hit.image for hit in unfiltered]

    def test_a_filter_excludes_images_on_other_devices(
        self, repository: ImageRepository, second_device: DeviceId
    ) -> None:
        wanted = index_image(repository, one_hot(0))
        index_image(repository, one_hot(0), device_id=second_device)

        hits = repository.search_similar(
            one_hot(0),
            limit=10,
            filters=SearchFilters(device_ids=frozenset({TEST_DEVICE_ID})),
        )

        assert [hit.image for hit in hits] == [wanted]

    def test_several_devices_can_be_named_at_once(
        self, repository: ImageRepository, second_device: DeviceId
    ) -> None:
        here = index_image(repository, one_hot(0))
        elsewhere = index_image(repository, one_hot(1), device_id=second_device)

        hits = repository.search_similar(
            one_hot(0),
            limit=10,
            filters=SearchFilters(
                device_ids=frozenset({TEST_DEVICE_ID, second_device})
            ),
        )

        assert {hit.image for hit in hits} == {here, elsewhere}

    def test_filtering_on_a_device_with_no_images_returns_nothing(
        self, repository: ImageRepository, second_device: DeviceId
    ) -> None:
        index_image(repository, one_hot(0))

        hits = repository.search_similar(
            one_hot(0),
            limit=10,
            filters=SearchFilters(device_ids=frozenset({second_device})),
        )

        assert hits == []

    def test_filtering_on_an_unknown_device_is_not_an_error(
        self, repository: ImageRepository
    ) -> None:
        """A filter restricts a set; it does not assert that it exists.

        Rejecting an unknown id would mean the repository -- or something
        above it -- keeping a list of devices just to refuse a search that
        already answers correctly.
        """
        index_image(repository, one_hot(0))

        hits = repository.search_similar(
            one_hot(0),
            limit=10,
            filters=SearchFilters(device_ids=frozenset({DeviceId(uuid.uuid4())})),
        )

        assert hits == []

    def test_ordering_and_scores_are_unchanged_within_the_subset(
        self, repository: ImageRepository, second_device: DeviceId
    ) -> None:
        """A filter narrows candidates; it must not touch the ranking."""
        identical = index_image(repository, one_hot(0))
        orthogonal = index_image(repository, one_hot(1))
        opposite = index_image(repository, one_hot(0, sign=-1.0))
        index_image(repository, one_hot(0), device_id=second_device)

        hits = repository.search_similar(
            one_hot(0),
            limit=10,
            filters=SearchFilters(device_ids=frozenset({TEST_DEVICE_ID})),
        )

        assert [hit.image for hit in hits] == [identical, orthogonal, opposite]
        assert [hit.similarity for hit in hits] == [
            pytest.approx(1.0),
            pytest.approx(0.0),
            pytest.approx(-1.0),
        ]

    def test_a_filter_does_not_resurrect_images_without_an_embedding(
        self, repository: ImageRepository
    ) -> None:
        indexed = index_image(repository, one_hot(0))
        store_without_embedding(repository)

        hits = repository.search_similar(
            one_hot(0),
            limit=10,
            filters=SearchFilters(device_ids=frozenset({TEST_DEVICE_ID})),
        )

        assert [hit.image for hit in hits] == [indexed]

    def test_limit_still_applies_inside_the_filtered_subset(
        self, repository: ImageRepository
    ) -> None:
        for axis in range(5):
            index_image(repository, one_hot(axis))

        hits = repository.search_similar(
            one_hot(0),
            limit=2,
            filters=SearchFilters(device_ids=frozenset({TEST_DEVICE_ID})),
        )

        assert len(hits) == 2

    def test_a_wrong_sized_query_vector_still_raises_with_a_filter(
        self, repository: ImageRepository
    ) -> None:
        index_image(repository, one_hot(0))

        with pytest.raises(EmbeddingDimensionMismatchError):
            repository.search_similar(
                EmbeddingVector([1.0, 0.0, 0.0]),
                limit=10,
                filters=SearchFilters(device_ids=frozenset({TEST_DEVICE_ID})),
            )


NEW_YEAR_2018 = datetime.datetime(2018, 1, 1)
NEW_YEAR_2019 = datetime.datetime(2019, 1, 1)
YEAR_2018 = DateRange(start=NEW_YEAR_2018, end=NEW_YEAR_2019)


def in_2018() -> SearchFilters:
    return SearchFilters(captured_between=YEAR_2018)


class TestCaptureDateFilter:
    """RFC-028 section 8: a half-open range over the camera-local capture date.

    These cases join the device ones rather than replacing them, for the
    same reason those joined RFC-025's: the unfiltered contract is still
    the contract. Every assertion here holds for all three repositories
    **at the scale this file runs at**, where PostgreSQL answers from a
    sequential scan and is therefore exact. At a scale where the planner
    keeps the approximate HNSW index, a date filter can return fewer than
    `limit` rows while more exist; that is measured by
    `experiments/rfc-028-capture-date/planner_check.py`, and asserting it
    here would make this test flaky by construction.
    """

    def test_a_range_excludes_images_outside_it(
        self, repository: ImageRepository
    ) -> None:
        inside = index_image(
            repository, one_hot(0), captured_at=datetime.datetime(2018, 7, 14)
        )
        index_image(repository, one_hot(0), captured_at=datetime.datetime(2017, 7, 14))
        index_image(repository, one_hot(0), captured_at=datetime.datetime(2020, 7, 14))

        hits = repository.search_similar(one_hot(0), limit=10, filters=in_2018())

        assert [hit.image for hit in hits] == [inside]

    def test_an_unknown_capture_date_never_matches(
        self, repository: ImageRepository
    ) -> None:
        """RFC-020's rule, and RFC-028 section 4.1: unknown is never "matches".

        Both kinds of unknown -- a row never examined, and a row examined
        with no date -- are excluded from the widest range there is.
        """
        dated = index_image(
            repository, one_hot(0), captured_at=datetime.datetime(2018, 7, 14)
        )
        index_image(repository, one_hot(0))
        index_image(repository, one_hot(0), capture=CaptureDate.unknown())

        everything = SearchFilters(
            captured_between=DateRange(datetime.datetime.min, datetime.datetime.max)
        )
        hits = repository.search_similar(one_hot(0), limit=10, filters=everything)

        assert [hit.image for hit in hits] == [dated]

    def test_the_start_bound_is_included(self, repository: ImageRepository) -> None:
        at_start = index_image(repository, one_hot(0), captured_at=NEW_YEAR_2018)

        hits = repository.search_similar(one_hot(0), limit=10, filters=in_2018())

        assert [hit.image for hit in hits] == [at_start]

    def test_the_end_bound_is_excluded(self, repository: ImageRepository) -> None:
        """A shot at the stroke of midnight belongs to 2019, not to both years."""
        index_image(repository, one_hot(0), captured_at=NEW_YEAR_2019)

        assert repository.search_similar(one_hot(0), limit=10, filters=in_2018()) == []

    def test_the_last_instant_before_the_end_is_included(
        self, repository: ImageRepository
    ) -> None:
        """The New Year's Eve shot a closed `<= 2018-12-31` range would drop."""
        last = index_image(
            repository,
            one_hot(0),
            captured_at=datetime.datetime(2018, 12, 31, 23, 59, 59, 999_999),
        )

        hits = repository.search_similar(one_hot(0), limit=10, filters=in_2018())

        assert [hit.image for hit in hits] == [last]

    def test_an_empty_range_matches_nothing(self, repository: ImageRepository) -> None:
        index_image(repository, one_hot(0), captured_at=NEW_YEAR_2018)

        empty = SearchFilters(captured_between=DateRange(NEW_YEAR_2018, NEW_YEAR_2018))

        assert repository.search_similar(one_hot(0), limit=10, filters=empty) == []

    def test_date_and_device_filters_combine_with_and(
        self, repository: ImageRepository, second_device: DeviceId
    ) -> None:
        wanted = index_image(
            repository, one_hot(0), captured_at=datetime.datetime(2018, 7, 14)
        )
        index_image(
            repository,
            one_hot(0),
            device_id=second_device,
            captured_at=datetime.datetime(2018, 7, 14),
        )
        index_image(repository, one_hot(0), captured_at=datetime.datetime(2016, 1, 1))

        hits = repository.search_similar(
            one_hot(0),
            limit=10,
            filters=SearchFilters(
                device_ids=frozenset({TEST_DEVICE_ID}), captured_between=YEAR_2018
            ),
        )

        assert [hit.image for hit in hits] == [wanted]

    def test_a_date_only_filter_does_not_restrict_devices(
        self, repository: ImageRepository, second_device: DeviceId
    ) -> None:
        """The empty device set still means *all* when a date is given."""
        here = index_image(
            repository, one_hot(0), captured_at=datetime.datetime(2018, 7, 14)
        )
        elsewhere = index_image(
            repository,
            one_hot(1),
            device_id=second_device,
            captured_at=datetime.datetime(2018, 8, 1),
        )

        hits = repository.search_similar(one_hot(0), limit=10, filters=in_2018())

        assert {hit.image for hit in hits} == {here, elsewhere}

    def test_a_date_filter_does_not_resurrect_images_without_an_embedding(
        self, repository: ImageRepository
    ) -> None:
        indexed = index_image(
            repository, one_hot(0), captured_at=datetime.datetime(2018, 7, 14)
        )
        store_without_embedding(repository, captured_at=datetime.datetime(2018, 7, 15))

        hits = repository.search_similar(one_hot(0), limit=10, filters=in_2018())

        assert [hit.image for hit in hits] == [indexed]

    def test_ordering_and_scores_are_unchanged_within_the_range(
        self, repository: ImageRepository
    ) -> None:
        shot = datetime.datetime(2018, 7, 14)
        identical = index_image(repository, one_hot(0), captured_at=shot)
        orthogonal = index_image(repository, one_hot(1), captured_at=shot)
        opposite = index_image(repository, one_hot(0, sign=-1.0), captured_at=shot)
        index_image(repository, one_hot(0), captured_at=datetime.datetime(2011, 1, 1))

        hits = repository.search_similar(one_hot(0), limit=10, filters=in_2018())

        assert [hit.image for hit in hits] == [identical, orthogonal, opposite]
        assert [hit.similarity for hit in hits] == [
            pytest.approx(1.0),
            pytest.approx(0.0),
            pytest.approx(-1.0),
        ]

    def test_limit_still_applies_inside_the_range(
        self, repository: ImageRepository
    ) -> None:
        for axis in range(5):
            index_image(
                repository, one_hot(axis), captured_at=datetime.datetime(2018, 7, 14)
            )

        hits = repository.search_similar(one_hot(0), limit=2, filters=in_2018())

        assert len(hits) == 2

    def test_hits_carry_their_capture_date_naive(
        self, repository: ImageRepository
    ) -> None:
        """The response needs the date and its source; the date stays naive."""
        shot = datetime.datetime(2018, 12, 31, 23, 30)
        index_image(repository, one_hot(0), captured_at=shot)

        (hit,) = repository.search_similar(one_hot(0), limit=10, filters=in_2018())

        assert hit.image.captured_at == shot
        assert hit.image.captured_at is not None
        assert hit.image.captured_at.tzinfo is None
        assert hit.image.capture_source is CaptureSource.EXIF_ORIGINAL

    def test_an_unfiltered_hit_reports_a_never_examined_row_as_none(
        self, repository: ImageRepository
    ) -> None:
        index_image(repository, one_hot(0))

        (hit,) = repository.search_similar(one_hot(0), limit=10)

        assert hit.image.captured_at is None
        assert hit.image.capture_source is None


class TestUnknownCaptureDateCount:
    """RFC-028 section 4.1: how many photos a date filter hid for having no date."""

    def test_no_date_range_means_zero(self, repository: ImageRepository) -> None:
        index_image(repository, one_hot(0))

        assert repository.count_unknown_capture_date(SearchFilters()) == 0

    def test_a_device_only_filter_means_zero(self, repository: ImageRepository) -> None:
        """Nothing was hidden by a date clause that was not there."""
        index_image(repository, one_hot(0))

        filters = SearchFilters(device_ids=frozenset({TEST_DEVICE_ID}))

        assert repository.count_unknown_capture_date(filters) == 0

    def test_both_kinds_of_unknown_are_counted(
        self, repository: ImageRepository
    ) -> None:
        index_image(repository, one_hot(0))
        index_image(repository, one_hot(1), capture=CaptureDate.unknown())
        index_image(repository, one_hot(2), captured_at=datetime.datetime(2018, 7, 1))
        index_image(repository, one_hot(3), captured_at=datetime.datetime(2011, 7, 1))

        assert repository.count_unknown_capture_date(in_2018()) == 2

    def test_images_without_an_embedding_are_not_counted(
        self, repository: ImageRepository
    ) -> None:
        """Only an image a search could have returned was hidden by the filter."""
        store_without_embedding(repository)
        index_image(repository, one_hot(0))

        assert repository.count_unknown_capture_date(in_2018()) == 1

    def test_the_device_clause_still_applies(
        self, repository: ImageRepository, second_device: DeviceId
    ) -> None:
        index_image(repository, one_hot(0))
        index_image(repository, one_hot(1), device_id=second_device)

        filters = SearchFilters(
            device_ids=frozenset({second_device}), captured_between=YEAR_2018
        )

        assert repository.count_unknown_capture_date(filters) == 1

    def test_the_count_is_not_bounded_by_the_search_limit(
        self, repository: ImageRepository
    ) -> None:
        """It counts the table under the filters, not the ranked page."""
        for axis in range(7):
            index_image(repository, one_hot(axis))

        assert repository.search_similar(one_hot(0), limit=2, filters=in_2018()) == []
        assert repository.count_unknown_capture_date(in_2018()) == 7
