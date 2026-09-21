"""RFC-031's two counting methods, held to one contract by three implementations.

`GET /api/v1/devices` and `GET /api/v1/devices/{id}/folders` are both
built on the promise that N rows cost one query, and both read their
numbers through `ImageRepository`. So the numbers have to be the same
whichever implementation answered -- otherwise every Application test
above them is evidence about the double rather than about the system,
which is the argument `test_search_similar_contract.py` makes at length.

Two behaviours are pinned here that a natural implementation gets wrong
in *different* directions, which is exactly why they are written down
rather than left to each implementation's judgement:

* the first-segment grouping returns keys that are not folders -- a file
  sitting directly in the listed folder comes back under its own
  filename. Neither the SQL nor the Python version can tell a file from
  a directory without touching the disk, so both return it and the use
  case discards it;
* a folder renamed on disk since it was indexed still has its rows under
  the old name, so the old name comes back too, matching nothing the
  filesystem reported. Same fate.

And the trap that motivates holding them to one contract at all:
`'2018b' LIKE '2018%'` is **true**. The separator on the prefix is what
stops `2018` from counting `2018b`'s photos, and a Python implementation
that used `str.startswith(prefix)` without it would agree with a SQL one
that had the same bug and with nothing else.
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
from app.domain.repositories.image_repository import ImageRepository
from app.domain.services.device_identity import compute_device_id
from app.domain.value_objects.device_id import DeviceId, VolumeIdentity, VolumeKind
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.job_scope import JobScope
from app.infrastructure.persistence.in_memory_image_repository import (
    InMemoryImageRepository,
)
from app.infrastructure.persistence.postgres_device_repository import (
    PostgresDeviceRepository,
)
from app.infrastructure.persistence.postgres_image_repository import (
    PostgresImageRepository,
)

SECOND_VOLUME = VolumeIdentity(
    value="\\\\?\\Volume{00000000-0000-0000-0000-0000000000fe}\\",
    kind=VolumeKind.WINDOWS_VOLUME_GUID,
)
SECOND_DEVICE_ID: DeviceId = compute_device_id(SECOND_VOLUME)
"""A second device, derived exactly as production derives one.

`count_by_device()` is a `GROUP BY`, and a test with one device cannot
tell a grouped count from `SELECT count(*)`.
"""


@pytest.fixture(params=["postgres", "in_memory", "fake"])
def repository(request: pytest.FixtureRequest) -> Iterator[ImageRepository]:
    """Yield each implementation in turn, over an empty `images` table.

    The PostgreSQL round uses `empty_db_session` rather than `db_session`:
    every assertion below is about a total over the whole table, and a row
    another test committed would make the totals simply wrong rather than
    noisy. The second device is created there because `images.device_id`
    is a foreign key.
    """
    if request.param == "postgres":
        session = request.getfixturevalue("empty_db_session")
        PostgresDeviceRepository(session).save(
            _device_row(SECOND_DEVICE_ID, SECOND_VOLUME)
        )
        yield PostgresImageRepository(session)
    elif request.param == "in_memory":
        yield InMemoryImageRepository()
    else:
        yield FakeImageRepository()


def _device_row(device_id: DeviceId, identity: VolumeIdentity) -> Device:
    now = datetime.datetime.now(tz=datetime.UTC)
    return Device(
        id=device_id,
        volume_identity=identity,
        label="HD-SECOND",
        first_seen_at=now,
        last_seen_at=now,
    )


def save(
    repository: ImageRepository,
    relative_path: str,
    device_id: DeviceId = TEST_DEVICE_ID,
) -> Image:
    """Write one row at `relative_path`, with no embedding.

    `save()` rather than `save_indexed()` on purpose: `count_by_device()`
    counts files the system knows, not files it can search, and a row
    without an embedding is still one of ours. A test built on
    `save_indexed()` could not tell the two definitions apart.
    """
    image = Image(
        id=ImageId(uuid.uuid4()),
        device_id=device_id,
        relative_path=ImagePath(relative_path),
        filename=relative_path.rsplit("/", 1)[-1],
        extension="jpg",
    )
    repository.save(image)
    return image


class TestCountByDevice:
    def test_an_empty_repository_returns_an_empty_mapping(
        self, repository: ImageRepository
    ) -> None:
        assert repository.count_by_device() == {}

    def test_it_groups_rather_than_totals(self, repository: ImageRepository) -> None:
        """With one device a total and a group look identical; with two they do not."""
        save(repository, "fotos/a.jpg")
        save(repository, "fotos/b.jpg")
        save(repository, "outras/c.jpg", device_id=SECOND_DEVICE_ID)

        assert repository.count_by_device() == {
            TEST_DEVICE_ID: 2,
            SECOND_DEVICE_ID: 1,
        }

    def test_a_device_with_no_images_is_absent_rather_than_zero(
        self, repository: ImageRepository
    ) -> None:
        """The contract callers read with `.get(device_id, 0)`.

        A `GROUP BY` produces no row for a device with nothing, and an
        implementation that padded the mapping would have to know which
        devices exist -- which is the other repository's question.
        """
        save(repository, "fotos/a.jpg")

        assert SECOND_DEVICE_ID not in repository.count_by_device()


class TestCountByPathPrefixes:
    def test_it_groups_by_the_first_part_below_the_parent(
        self, repository: ImageRepository
    ) -> None:
        save(repository, "2018/janeiro/a.jpg")
        save(repository, "2018/janeiro/b.jpg")
        save(repository, "2018/fevereiro/c.jpg")

        counts = repository.count_by_path_prefixes(TEST_DEVICE_ID, JobScope("2018"))

        assert counts == {"janeiro": 2, "fevereiro": 1}

    def test_it_counts_the_whole_subtree_not_just_the_level_below(
        self, repository: ImageRepository
    ) -> None:
        """`indexed_images` on a folder row means everything beneath it."""
        save(repository, "2018/janeiro/casamento/a.jpg")
        save(repository, "2018/janeiro/casamento/detalhes/b.jpg")

        counts = repository.count_by_path_prefixes(TEST_DEVICE_ID, JobScope("2018"))

        assert counts == {"janeiro": 2}

    def test_the_whole_device_scope_groups_by_the_first_part(
        self, repository: ImageRepository
    ) -> None:
        save(repository, "2018/janeiro/a.jpg")
        save(repository, "2019/b.jpg")

        counts = repository.count_by_path_prefixes(TEST_DEVICE_ID, JobScope())

        assert counts == {"2018": 1, "2019": 1}

    def test_a_sibling_folder_sharing_a_prefix_is_not_counted(
        self, repository: ImageRepository
    ) -> None:
        """`'2018b' LIKE '2018%'` is true, and `2018b` is a different folder.

        The separator on the prefix is the whole of the fix, in both
        implementations, and this is the assertion that notices if either
        one loses it (RFC-029 section 10).
        """
        save(repository, "2018/janeiro/a.jpg")
        save(repository, "2018b/janeiro/b.jpg")

        counts = repository.count_by_path_prefixes(TEST_DEVICE_ID, JobScope("2018"))

        assert counts == {"janeiro": 1}

    def test_another_device_is_not_counted(self, repository: ImageRepository) -> None:
        save(repository, "2018/janeiro/a.jpg")
        save(repository, "2018/janeiro/b.jpg", device_id=SECOND_DEVICE_ID)

        counts = repository.count_by_path_prefixes(TEST_DEVICE_ID, JobScope("2018"))

        assert counts == {"janeiro": 1}

    def test_an_image_directly_in_the_parent_comes_back_under_its_filename(
        self, repository: ImageRepository
    ) -> None:
        """Documented, deliberate, and identical in all three implementations.

        Neither the query nor the in-process tally can tell a file from a
        folder without touching the disk, so both return the key and the
        use case drops it -- it will not be among the names `readdir`
        reported. The alternative, excluding it with a second `LIKE`,
        would be this layer guessing at something it cannot see.
        """
        save(repository, "2018/loose.jpg")
        save(repository, "2018/janeiro/a.jpg")

        counts = repository.count_by_path_prefixes(TEST_DEVICE_ID, JobScope("2018"))

        assert counts == {"loose.jpg": 1, "janeiro": 1}

    def test_a_folder_renamed_on_disk_still_comes_back_under_its_old_name(
        self, repository: ImageRepository
    ) -> None:
        """The other key the caller discards, and the reason it discards by name.

        The rows still say `janeiro`; the disk now says `01-janeiro`. The
        count is real and belongs to nothing the user can see, so it is
        dropped rather than rendered as a folder that is not there.
        """
        save(repository, "2018/janeiro/a.jpg")

        counts = repository.count_by_path_prefixes(TEST_DEVICE_ID, JobScope("2018"))

        assert counts == {"janeiro": 1}
        assert "01-janeiro" not in counts

    def test_a_folder_with_no_images_is_absent_rather_than_zero(
        self, repository: ImageRepository
    ) -> None:
        """Callers read this with `.get(name, 0)`; zero is theirs to supply."""
        save(repository, "2018/janeiro/a.jpg")

        counts = repository.count_by_path_prefixes(TEST_DEVICE_ID, JobScope("2018"))

        assert "marco" not in counts

    def test_an_unknown_parent_returns_an_empty_mapping(
        self, repository: ImageRepository
    ) -> None:
        save(repository, "2018/janeiro/a.jpg")

        assert repository.count_by_path_prefixes(TEST_DEVICE_ID, JobScope("1999")) == {}

    def test_a_folder_name_containing_a_wildcard_is_matched_literally(
        self, repository: ImageRepository
    ) -> None:
        """`_` matches any character in `LIKE`, so the prefix must be escaped.

        Unlikely and cheap to be right about: without escaping,
        `100_ND750` would also count `100XND750`'s photos, and the two
        implementations would disagree -- Python's `startswith` has no
        wildcards at all.
        """
        save(repository, "100_ND750/janeiro/a.jpg")
        save(repository, "100XND750/janeiro/b.jpg")

        counts = repository.count_by_path_prefixes(
            TEST_DEVICE_ID, JobScope("100_ND750")
        )

        assert counts == {"janeiro": 1}
