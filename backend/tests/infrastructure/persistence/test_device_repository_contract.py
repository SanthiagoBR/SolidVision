"""One contract test for `DeviceRepository`, two implementations.

Written once and run against `PostgresDeviceRepository` and
`InMemoryDeviceRepository`, for the reason
`test_search_similar_contract.py` gives: a double that stores or matches
devices slightly differently from PostgreSQL turns every green test above
it into evidence about the double.

What the contract has to nail down here is narrower than for images but
sharper. A device is looked up by a key the operating system minted, its
id is derived from that key rather than allocated, and the same disk
arriving a second time must land on the same row -- because "the same disk
arriving a second time" is what every worker run is (RFC-027 section 2.1),
and a repository that appended instead would rebuild the duplication the
whole RFC exists to remove.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from tests.conftest import TEST_DEVICE_ID, TEST_VOLUME_IDENTITY

from app.domain.entities.device import Device
from app.domain.entities.image import Image
from app.domain.repositories.device_repository import DeviceRepository
from app.domain.value_objects.device_id import DeviceId, VolumeIdentity, VolumeKind
from app.domain.value_objects.embedding_vector import EmbeddingVector
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.indexing_record import IndexingRecord
from app.infrastructure.database.models.device_model import DeviceModel
from app.infrastructure.database.models.image_model import (
    EMBEDDING_DIMENSION,
    ImageModel,
)
from app.infrastructure.filesystem.device_identity import compute_device_id
from app.infrastructure.persistence.in_memory_device_repository import (
    InMemoryDeviceRepository,
)
from app.infrastructure.persistence.postgres_device_repository import (
    PostgresDeviceRepository,
)
from app.infrastructure.persistence.postgres_image_repository import (
    PostgresImageRepository,
)


@pytest.fixture(params=["postgres", "in_memory"])
def repository(request: pytest.FixtureRequest) -> Iterator[DeviceRepository]:
    """Yield each `DeviceRepository` implementation in turn.

    The PostgreSQL round empties both tables, devices last, because
    `images.device_id` is a foreign key -- and it removes the shared test
    device that `db_session` installs, since several assertions below are
    about what `list()` contains and cannot be written against a table
    somebody else already put a row in.
    """
    if request.param == "postgres":
        session = request.getfixturevalue("empty_db_session")
        session.execute(delete(ImageModel))
        session.execute(delete(DeviceModel))
        session.commit()
        yield PostgresDeviceRepository(session)
    else:
        yield InMemoryDeviceRepository()


def volume(
    suffix: int, kind: VolumeKind = VolumeKind.WINDOWS_VOLUME_GUID
) -> VolumeIdentity:
    """Build a volume identity shaped like the real thing."""
    return VolumeIdentity(
        value=f"\\\\?\\Volume{{{uuid.UUID(int=suffix)}}}\\", kind=kind
    )


def device(identity: VolumeIdentity, label: str = "HD1", **overrides: object) -> Device:
    """Build a device whose id is derived exactly as production derives it."""
    now = datetime.datetime.now(tz=datetime.UTC)
    fields: dict[str, object] = {
        "id": compute_device_id(identity),
        "volume_identity": identity,
        "label": label,
        "first_seen_at": now,
        "last_seen_at": now,
    }
    fields.update(overrides)
    return Device(**fields)  # type: ignore[arg-type]


def test_a_saved_device_is_retrievable_by_id(repository: DeviceRepository) -> None:
    saved = device(volume(1))

    repository.save(saved)

    assert repository.get(saved.id) == saved


def test_an_unknown_id_returns_none(repository: DeviceRepository) -> None:
    assert repository.get(DeviceId(uuid.uuid4())) is None


def test_a_device_is_found_by_the_identity_the_platform_reports(
    repository: DeviceRepository,
) -> None:
    """The lookup that makes remounting free (RFC-027 section 2.1).

    A worker resolves whatever disk it was pointed at, asks this, and
    finds the device it indexed last month -- under a different drive
    letter, and it does not matter.
    """
    identity = volume(2)
    repository.save(device(identity, label="HD2"))

    found = repository.get_by_volume_identity(identity)

    assert found is not None
    assert found.label == "HD2"


def test_an_unknown_volume_identity_returns_none(
    repository: DeviceRepository,
) -> None:
    assert repository.get_by_volume_identity(volume(3)) is None


def test_the_platform_kind_is_part_of_the_lookup(
    repository: DeviceRepository,
) -> None:
    """Two platforms naming a volume identically are still two volumes.

    Matching on the opaque string alone would merge them, which is what
    the `volume_kind` discriminator exists to prevent -- and it has to be
    prevented in the query, not left to a constraint.
    """
    windows = volume(4, VolumeKind.WINDOWS_VOLUME_GUID)
    repository.save(device(windows))

    same_string_on_linux = VolumeIdentity(
        value=windows.value, kind=VolumeKind.LINUX_FS_UUID
    )

    assert repository.get_by_volume_identity(same_string_on_linux) is None


def test_saving_the_same_volume_twice_updates_one_row(
    repository: DeviceRepository,
) -> None:
    """Every worker run saves a disk it has probably seen before.

    An implementation that appended would produce a second device for the
    same physical disk, and the images already on the first one would
    stop being findable under the device the next run resolves.
    """
    identity = volume(5)
    repository.save(device(identity, label="HD5"))
    repository.save(device(identity, label="Renamed"))

    assert len(repository.list()) == 1
    stored = repository.get_by_volume_identity(identity)
    assert stored is not None
    assert stored.label == "Renamed"


def test_the_derived_id_is_what_the_row_is_keyed_on(
    repository: DeviceRepository,
) -> None:
    identity = volume(6)
    repository.save(device(identity))

    assert repository.get(compute_device_id(identity)) is not None


def test_list_returns_every_saved_device(repository: DeviceRepository) -> None:
    first = device(volume(7), label="HD7")
    second = device(volume(8), label="HD8")
    repository.save(first)
    repository.save(second)

    assert {stored.id for stored in repository.list()} == {first.id, second.id}


def test_list_is_empty_when_nothing_was_saved(repository: DeviceRepository) -> None:
    assert repository.list() == []


def test_delete_removes_the_device(repository: DeviceRepository) -> None:
    saved = device(volume(9))
    repository.save(saved)

    repository.delete(saved.id)

    assert repository.get(saved.id) is None
    assert repository.list() == []


def test_deleting_an_unknown_device_is_not_an_error(
    repository: DeviceRepository,
) -> None:
    repository.delete(DeviceId(uuid.uuid4()))


def test_optional_fields_round_trip(repository: DeviceRepository) -> None:
    """Including the scan denominator, which nothing else can reconstruct."""
    scanned_at = datetime.datetime(2026, 3, 12, tzinfo=datetime.UTC)
    saved = device(
        volume(10),
        filesystem_label="Untitled",
        total_bytes=2_000_398_934_016,
        last_scan_at=scanned_at,
        last_scan_file_count=40_000,
    )

    repository.save(saved)

    stored = repository.get(saved.id)
    assert stored is not None
    assert stored.filesystem_label == "Untitled"
    assert stored.total_bytes == 2_000_398_934_016
    assert stored.last_scan_at == scanned_at
    assert stored.last_scan_file_count == 40_000


def test_a_device_that_has_never_been_scanned_has_no_denominator(
    repository: DeviceRepository,
) -> None:
    """`None` must survive as `None`, never arrive back as zero.

    A missing denominator has to render as "not scanned yet". Zero would
    render as "0% indexed", which is a confident claim about a disk nobody
    has looked at.
    """
    saved = device(volume(11))

    repository.save(saved)

    stored = repository.get(saved.id)
    assert stored is not None
    assert stored.last_scan_at is None
    assert stored.last_scan_file_count is None


def test_first_seen_at_is_not_overwritten_by_a_later_save(
    repository: DeviceRepository,
) -> None:
    """It answers a question about the past, so it has to stay true.

    An upsert that refreshed it would turn "known since March" into
    "known since just now" on every single worker run.
    """
    identity = volume(12)
    march = datetime.datetime(2026, 3, 1, tzinfo=datetime.UTC)
    september = datetime.datetime(2026, 9, 8, tzinfo=datetime.UTC)
    repository.save(device(identity, first_seen_at=march, last_seen_at=march))

    repository.save(device(identity, first_seen_at=september, last_seen_at=september))

    stored = repository.get_by_volume_identity(identity)
    assert stored is not None
    assert stored.first_seen_at == march
    assert stored.last_seen_at == september


def test_devices_carry_no_mount_point_or_connection_flag(
    repository: DeviceRepository,
) -> None:
    """RFC-027 sections 4 and 7, checked through the port.

    Neither value can be kept correct: a drive letter is assigned by mount
    order, and nothing tells this process that a disk was unplugged. The
    repository is the tempting place for either to reappear as a
    convenience.
    """
    saved = device(volume(13))
    repository.save(saved)

    stored = repository.get(saved.id)

    assert stored is not None
    assert not hasattr(stored, "mount_point")
    assert not hasattr(stored, "drive_letter")
    assert not hasattr(stored, "is_connected")


def test_deleting_a_device_with_images_is_refused(
    empty_db_session: Session,
) -> None:
    """PostgreSQL only, because only PostgreSQL holds the foreign key.

    The refusal is the feature. Each of those image rows is an embedding
    that cost real inference time -- RFC-024 measured 2.2 images/second on
    CPU -- and RFC-027 section 6.3 is explicit that they must never be
    discarded as a side effect of tidying something else up.
    """
    devices = PostgresDeviceRepository(empty_db_session)
    images = PostgresImageRepository(empty_db_session)
    images.save_indexed(
        IndexingRecord(
            image=Image(
                id=ImageId(uuid.uuid4()),
                device_id=TEST_DEVICE_ID,
                relative_path=ImagePath("fotos/2018/DJI_0042.JPG"),
                filename="DJI_0042",
                extension="jpg",
            ),
            embedding=EmbeddingVector([0.0] * EMBEDDING_DIMENSION),
            file_size=1,
            file_modified_at=None,
        )
    )

    with pytest.raises(IntegrityError):
        devices.delete(TEST_DEVICE_ID)


def test_the_session_survives_a_refused_delete(empty_db_session: Session) -> None:
    """A rejected write must not poison the next unrelated one.

    Same reasoning as `PostgresImageRepository.save_indexed()`: without
    the rollback, SQLAlchemy leaves the transaction pending-rollback and
    the *next* statement fails with `PendingRollbackError` instead of
    succeeding, turning one refusal into a cascade.
    """
    devices = PostgresDeviceRepository(empty_db_session)
    images = PostgresImageRepository(empty_db_session)
    images.save_indexed(
        IndexingRecord(
            image=Image(
                id=ImageId(uuid.uuid4()),
                device_id=TEST_DEVICE_ID,
                relative_path=ImagePath("fotos/keepme.jpg"),
                filename="keepme",
                extension="jpg",
            ),
            embedding=EmbeddingVector([0.0] * EMBEDDING_DIMENSION),
            file_size=1,
            file_modified_at=None,
        )
    )

    with pytest.raises(IntegrityError):
        devices.delete(TEST_DEVICE_ID)

    assert devices.get_by_volume_identity(TEST_VOLUME_IDENTITY) is not None
