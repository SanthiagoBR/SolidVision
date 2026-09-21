"""The five device use cases of RFC-031, with doubles that count.

**Most of what matters here is a call count, not a value.** Every one of
these use cases would return byte-identical results with an N+1 inside
it, so the assertions that actually protect the product are the ones
counting enumerations and queries -- `FakeVolumeCatalog` counts its two
methods separately and `FakeImageRepository` counts both of RFC-031's
reads. A test with one device or one folder cannot tell a grouped read
from a loop, so every count test uses N >= 2 and the important ones use
twenty.

The filesystem doubles are filesystem-backed, on `tmp_path`: a folder
either exists under the mount point or does not, and `has_children` is
answered by looking. That is what makes a test about expand arrows a
test about folders rather than about a flag the test set.
"""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest

from app.application.use_cases.describe_devices import (
    ACTIVE_STATUSES,
    DescribeDevicesUseCase,
)
from app.application.use_cases.list_device_folders import ListDeviceFoldersUseCase
from app.application.use_cases.list_devices import ListDevicesUseCase
from app.application.use_cases.list_mounted_volumes import ListMountedVolumesUseCase
from app.application.use_cases.register_device import RegisterDeviceUseCase
from app.application.use_cases.rename_device import RenameDeviceUseCase
from app.domain.entities.device import Device
from app.domain.entities.image import Image
from app.domain.entities.indexing_job import IndexingJob, JobStatus
from app.domain.exceptions import (
    DeviceNotConnectedError,
    DeviceNotFoundError,
    InvalidJobScopeError,
    InvalidVolumeIdentityError,
)
from app.domain.services.device_identity import compute_device_id
from app.domain.value_objects.device_id import DeviceId, VolumeIdentity, VolumeKind
from app.domain.value_objects.folder_state import FolderState
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.job_id import JobId
from app.domain.value_objects.job_scope import JobScope
from app.infrastructure.persistence.in_memory_indexing_job_repository import (
    InMemoryIndexingJobRepository,
)
from tests.application.fakes import (
    FakeDeviceLocator,
    FakeDeviceRepository,
    FakeImageRepository,
    FakeVolumeCatalog,
    make_mounted_volume,
)
from tests.conftest import TEST_DEVICE_ID, TEST_VOLUME_IDENTITY, make_test_device

NOW = datetime.datetime(2026, 9, 21, 12, 0, tzinfo=datetime.UTC)
EARLIER = datetime.datetime(2026, 3, 12, 9, 0, tzinfo=datetime.UTC)


def volume_identity(suffix: int) -> VolumeIdentity:
    return VolumeIdentity(
        value=f"\\\\?\\Volume{{00000000-0000-0000-0000-{suffix:012d}}}\\",
        kind=VolumeKind.WINDOWS_VOLUME_GUID,
    )


def device_for(identity: VolumeIdentity, label: str) -> Device:
    """Build a device whose id is derived exactly as production derives it."""
    return Device(
        id=compute_device_id(identity),
        volume_identity=identity,
        label=label,
        first_seen_at=EARLIER,
        last_seen_at=EARLIER,
    )


def image_at(relative_path: str, device_id: DeviceId = TEST_DEVICE_ID) -> Image:
    return Image(
        id=ImageId(compute_device_id(volume_identity(0)).value),
        device_id=device_id,
        relative_path=ImagePath(relative_path),
        filename=relative_path.rsplit("/", 1)[-1],
        extension="jpg",
    )


def job(
    status: JobStatus,
    scopes: tuple[str, ...] = (),
    created_at: datetime.datetime = NOW,
    device_id: DeviceId = TEST_DEVICE_ID,
) -> IndexingJob:
    return IndexingJob(
        id=JobId.new(),
        device_id=device_id,
        scopes=tuple(JobScope(scope) for scope in scopes),
        status=status,
        created_at=created_at,
    )


class TestListDevices:
    """RFC-031 section 4: one enumeration and a fixed number of queries."""

    def build(self, count: int) -> tuple[ListDevicesUseCase, FakeVolumeCatalog]:
        """Compose the use case over `count` devices, all of them mounted."""
        devices = [device_for(volume_identity(n), f"HD{n}") for n in range(count)]
        catalog = FakeVolumeCatalog(
            [
                make_mounted_volume(
                    Path(f"/mnt/{n}"), volume_value=device.volume_identity.value
                )
                for n, device in enumerate(devices)
            ]
        )
        return (
            ListDevicesUseCase(
                device_repository=FakeDeviceRepository(devices),
                describer=DescribeDevicesUseCase(
                    image_repository=FakeImageRepository(),
                    job_repository=InMemoryIndexingJobRepository(),
                    volume_catalog=catalog,
                ),
            ),
            catalog,
        )

    def test_twenty_devices_cost_one_enumeration(self) -> None:
        """The assertion RFC-031 section 4.1 exists for.

        The JSON is identical whether the filesystem was enumerated once
        or twenty times, so nothing but a counter could notice -- and the
        tempting implementation, `DeviceLocator.mount_point()` per
        device, is the one RFC-030 blessed one layer over.
        """
        use_case, catalog = self.build(20)

        use_case.execute()

        assert catalog.mount_points_calls == 1

    def test_it_never_pays_for_labels_and_capacities(self) -> None:
        """`list_mounted()` reads a capacity per volume and can wake a disk.

        This route must use the cheap method; the expensive one belongs
        to `GET /volumes` and to registration, which need the label and
        the size to write them down (RFC-031 section 4.1).
        """
        use_case, catalog = self.build(20)

        use_case.execute()

        assert catalog.list_mounted_calls == 0

    def test_twenty_devices_cost_one_count_and_two_job_queries(self) -> None:
        devices = [device_for(volume_identity(n), f"HD{n}") for n in range(20)]
        images = FakeImageRepository()
        jobs = CountingJobRepository()
        ListDevicesUseCase(
            device_repository=FakeDeviceRepository(devices),
            describer=DescribeDevicesUseCase(
                image_repository=images,
                job_repository=jobs,
                volume_catalog=FakeVolumeCatalog(),
            ),
        ).execute()

        assert images.count_by_device_calls == 1
        assert jobs.list_calls == [(None, JobStatus.RUNNING), (None, JobStatus.PENDING)]

    def test_the_active_statuses_are_exactly_the_ones_the_domain_names(self) -> None:
        """Guards the two-query plan against a state added to only one side.

        `JobStatus.is_active` is shared with the partial unique index of
        RFC-029 section 9. A sixth status marked active and not listed
        here would silently stop appearing in the sidebar.
        """
        assert set(ACTIVE_STATUSES) == {
            status for status in JobStatus if status.is_active
        }

    def test_connected_reflects_the_moment_of_each_request(self) -> None:
        """Two requests, a cable pulled between them (RFC-031 section 14)."""
        device = make_test_device("HD3")
        catalog = FakeVolumeCatalog(
            [make_mounted_volume(Path("/mnt/hd3"), TEST_VOLUME_IDENTITY.value)]
        )
        use_case = ListDevicesUseCase(
            device_repository=FakeDeviceRepository([device]),
            describer=DescribeDevicesUseCase(
                image_repository=FakeImageRepository(),
                job_repository=InMemoryIndexingJobRepository(),
                volume_catalog=catalog,
            ),
        )

        (before,) = use_case.execute()
        catalog.detach(TEST_VOLUME_IDENTITY)
        (after,) = use_case.execute()

        assert before.connected is True
        assert before.mount_point == Path("/mnt/hd3")
        assert after.connected is False
        assert after.mount_point is None

    def test_a_device_with_no_images_counts_zero_rather_than_disappearing(
        self,
    ) -> None:
        use_case, _ = self.build(2)

        summaries = use_case.execute()

        assert [summary.indexed_images for summary in summaries] == [0, 0]

    def test_each_device_gets_its_own_count(self) -> None:
        first = device_for(volume_identity(1), "HD1")
        second = device_for(volume_identity(2), "HD2")
        images = FakeImageRepository(
            [
                image_at("a.jpg", first.id),
                image_at("b.jpg", first.id),
                image_at("c.jpg", second.id),
            ]
        )
        summaries = ListDevicesUseCase(
            device_repository=FakeDeviceRepository([first, second]),
            describer=DescribeDevicesUseCase(
                image_repository=images,
                job_repository=InMemoryIndexingJobRepository(),
                volume_catalog=FakeVolumeCatalog(),
            ),
        ).execute()

        assert {s.device.label: s.indexed_images for s in summaries} == {
            "HD1": 2,
            "HD2": 1,
        }

    def test_the_active_job_lands_on_its_own_device(self) -> None:
        first = device_for(volume_identity(1), "HD1")
        second = device_for(volume_identity(2), "HD2")
        jobs = InMemoryIndexingJobRepository()
        running = jobs.create(job(JobStatus.PENDING, device_id=second.id))

        summaries = ListDevicesUseCase(
            device_repository=FakeDeviceRepository([first, second]),
            describer=DescribeDevicesUseCase(
                image_repository=FakeImageRepository(),
                job_repository=jobs,
                volume_catalog=FakeVolumeCatalog(),
            ),
        ).execute()

        by_label = {s.device.label: s.active_job for s in summaries}
        assert by_label["HD1"] is None
        assert by_label["HD2"] is not None
        assert by_label["HD2"].id == running.id

    def test_a_finished_job_is_not_an_active_one(self) -> None:
        device = device_for(volume_identity(1), "HD1")
        jobs = InMemoryIndexingJobRepository()
        created = jobs.create(job(JobStatus.PENDING, device_id=device.id))
        jobs.save_if_status(
            created.claim(NOW).complete(NOW), expected=JobStatus.PENDING
        )

        (summary,) = ListDevicesUseCase(
            device_repository=FakeDeviceRepository([device]),
            describer=DescribeDevicesUseCase(
                image_repository=FakeImageRepository(),
                job_repository=jobs,
                volume_catalog=FakeVolumeCatalog(),
            ),
        ).execute()

        assert summary.active_job is None

    def test_devices_come_back_sorted_by_label_case_insensitively(self) -> None:
        devices = [
            device_for(volume_identity(1), "zulu"),
            device_for(volume_identity(2), "Alpha"),
            device_for(volume_identity(3), "beta"),
        ]

        summaries = ListDevicesUseCase(
            device_repository=FakeDeviceRepository(devices),
            describer=DescribeDevicesUseCase(
                image_repository=FakeImageRepository(),
                job_repository=InMemoryIndexingJobRepository(),
                volume_catalog=FakeVolumeCatalog(),
            ),
        ).execute()

        assert [s.device.label for s in summaries] == ["Alpha", "beta", "zulu"]


class CountingJobRepository(InMemoryIndexingJobRepository):
    """Records every `list()` call's arguments, to prove there were two.

    A subclass rather than a fresh double, for the reason
    `FakeDeviceRepository` delegates to `InMemoryDeviceRepository`: a
    second implementation of the port is the first thing to stop agreeing
    with the contract test.
    """

    def __init__(self) -> None:
        super().__init__()
        self.list_calls: list[tuple[DeviceId | None, JobStatus | None]] = []

    def list(
        self,
        device_id: DeviceId | None = None,
        status: JobStatus | None = None,
    ) -> list[IndexingJob]:
        self.list_calls.append((device_id, status))
        return super().list(device_id=device_id, status=status)


class TestListMountedVolumes:
    """RFC-031 section 5: what is plugged in, and what of it we know."""

    def test_a_registered_volume_carries_its_device_id(self) -> None:
        device = make_test_device("HD3")
        catalog = FakeVolumeCatalog(
            [make_mounted_volume(Path("/mnt/hd3"), TEST_VOLUME_IDENTITY.value)]
        )

        (entry,) = ListMountedVolumesUseCase(
            volume_catalog=catalog,
            device_repository=FakeDeviceRepository([device]),
        ).execute()

        assert entry.device_id == device.id
        assert entry.is_registered is True

    def test_an_unregistered_volume_carries_none(self) -> None:
        """What lets the 'add a device' screen grey out what is already added."""
        catalog = FakeVolumeCatalog(
            [make_mounted_volume(Path("/mnt/new"), volume_identity(9).value)]
        )

        (entry,) = ListMountedVolumesUseCase(
            volume_catalog=catalog, device_repository=FakeDeviceRepository([])
        ).execute()

        assert entry.device_id is None
        assert entry.is_registered is False

    def test_a_known_device_that_is_not_plugged_in_is_simply_absent(self) -> None:
        """`/volumes` is about the machine, not about what we have on file."""
        entries = ListMountedVolumesUseCase(
            volume_catalog=FakeVolumeCatalog(),
            device_repository=FakeDeviceRepository([make_test_device("HD3")]),
        ).execute()

        assert entries == []

    def test_volumes_come_back_sorted_by_mount_point(self) -> None:
        catalog = FakeVolumeCatalog(
            [
                make_mounted_volume(Path("/mnt/g"), volume_identity(7).value),
                make_mounted_volume(Path("/mnt/d"), volume_identity(4).value),
            ]
        )

        entries = ListMountedVolumesUseCase(
            volume_catalog=catalog, device_repository=FakeDeviceRepository([])
        ).execute()

        assert [str(e.volume.mount_point) for e in entries] == sorted(
            str(e.volume.mount_point) for e in entries
        )

    def test_it_uses_the_detailed_enumeration(self) -> None:
        """The label and the capacity are in the body, so the cost is the point."""
        catalog = FakeVolumeCatalog(
            [
                make_mounted_volume(
                    Path("/mnt/g"),
                    volume_identity(7).value,
                    filesystem_label="DJI_ARCHIVE",
                    total_bytes=4_000_787_030_016,
                )
            ]
        )

        (entry,) = ListMountedVolumesUseCase(
            volume_catalog=catalog, device_repository=FakeDeviceRepository([])
        ).execute()

        assert catalog.list_mounted_calls == 1
        assert entry.volume.filesystem_label == "DJI_ARCHIVE"
        assert entry.volume.total_bytes == 4_000_787_030_016


class TestRegisterDevice:
    """RFC-031 sections 6 and 6.1."""

    def build(
        self, devices: list[Device] | None = None, mounted: bool = True
    ) -> tuple[RegisterDeviceUseCase, FakeDeviceRepository]:
        repository = FakeDeviceRepository(devices or [])
        catalog = FakeVolumeCatalog(
            [
                make_mounted_volume(
                    Path("/mnt/hd3"),
                    TEST_VOLUME_IDENTITY.value,
                    filesystem_label="Seagate Backup",
                    total_bytes=2_000_398_934_016,
                )
            ]
            if mounted
            else []
        )
        return (
            RegisterDeviceUseCase(
                volume_catalog=catalog,
                device_repository=repository,
                clock=lambda: NOW,
            ),
            repository,
        )

    def test_a_new_volume_is_created(self) -> None:
        use_case, repository = self.build()

        registered = use_case.execute(
            TEST_VOLUME_IDENTITY.value, "windows-volume-guid", "HD3"
        )

        assert registered.created is True
        assert registered.device.id == TEST_DEVICE_ID
        assert registered.device.label == "HD3"
        assert repository.list() == [registered.device]

    def test_the_id_is_derived_rather_than_allocated(self) -> None:
        """Which is what makes registering twice land on one row."""
        use_case, _ = self.build()

        registered = use_case.execute(TEST_VOLUME_IDENTITY.value, "windows-volume-guid")

        assert registered.device.id == compute_device_id(TEST_VOLUME_IDENTITY)

    def test_registering_twice_creates_one_device_and_applies_the_label(self) -> None:
        """RFC-031 section 6.1, all three claims in one place."""
        use_case, repository = self.build()

        first = use_case.execute(
            TEST_VOLUME_IDENTITY.value, "windows-volume-guid", "HD3"
        )
        second = use_case.execute(
            TEST_VOLUME_IDENTITY.value, "windows-volume-guid", "HD3 — aéreas"
        )

        assert first.created is True
        assert second.created is False
        assert len(repository.list()) == 1
        assert repository.list()[0].label == "HD3 — aéreas"

    def test_a_second_registration_without_a_label_keeps_the_name(self) -> None:
        use_case, repository = self.build()
        use_case.execute(TEST_VOLUME_IDENTITY.value, "windows-volume-guid", "HD3")

        second = use_case.execute(TEST_VOLUME_IDENTITY.value, "windows-volume-guid")

        assert second.device.label == "HD3"

    def test_without_a_label_it_falls_back_to_the_filesystem_label(self) -> None:
        use_case, _ = self.build()

        registered = use_case.execute(TEST_VOLUME_IDENTITY.value, "windows-volume-guid")

        assert registered.device.label == "Seagate Backup"

    def test_a_second_registration_preserves_first_seen_and_the_scan_counters(
        self,
    ) -> None:
        """The scan counters are §4.2's denominator; registering is not a scan."""
        existing = Device(
            id=TEST_DEVICE_ID,
            volume_identity=TEST_VOLUME_IDENTITY,
            label="HD3",
            first_seen_at=EARLIER,
            last_seen_at=EARLIER,
            last_scan_at=EARLIER,
            last_scan_file_count=48210,
        )
        use_case, _ = self.build([existing])

        registered = use_case.execute(TEST_VOLUME_IDENTITY.value, "windows-volume-guid")

        assert registered.device.first_seen_at == EARLIER
        assert registered.device.last_seen_at == NOW
        assert registered.device.last_scan_at == EARLIER
        assert registered.device.last_scan_file_count == 48210

    def test_an_identity_nobody_is_holding_is_refused_and_writes_nothing(self) -> None:
        """An invented identity is not a location; a path would have been one."""
        use_case, repository = self.build(mounted=False)

        with pytest.raises(DeviceNotConnectedError):
            use_case.execute(TEST_VOLUME_IDENTITY.value, "windows-volume-guid")

        assert repository.save_calls == []

    def test_an_unknown_kind_is_a_domain_error_not_a_crash(self) -> None:
        """`VolumeKind('nope')` raises `ValueError`, which would be a 500."""
        use_case, repository = self.build()

        with pytest.raises(InvalidVolumeIdentityError):
            use_case.execute(TEST_VOLUME_IDENTITY.value, "linux-btrfs-uuid")

        assert repository.save_calls == []

    def test_an_empty_identity_is_refused_by_the_value_object(self) -> None:
        """No second check was written for it; `__post_init__` already refuses."""
        use_case, repository = self.build()

        with pytest.raises(InvalidVolumeIdentityError):
            use_case.execute("   ", "windows-volume-guid")

        assert repository.save_calls == []

    def test_register_applies_the_merge_without_touching_the_platform(self) -> None:
        """The CLI's entry point: it arrives with a volume already resolved."""
        use_case, repository = self.build(mounted=False)
        volume = make_mounted_volume(Path("/mnt/hd3"), TEST_VOLUME_IDENTITY.value)

        device = use_case.register(volume, label="HD3")

        assert device.id == TEST_DEVICE_ID
        assert repository.save_calls == [device]

    def test_register_with_persist_false_writes_nothing(self) -> None:
        """What `device_reconcile --dry-run` needs: the id is derived, not allocated."""
        use_case, repository = self.build(mounted=False)
        volume = make_mounted_volume(Path("/mnt/hd3"), TEST_VOLUME_IDENTITY.value)

        device = use_case.register(volume, label="HD3", persist=False)

        assert device.id == TEST_DEVICE_ID
        assert repository.save_calls == []


class TestRenameDevice:
    """RFC-031 section 7."""

    def build(
        self, devices: list[Device], mounted: bool = False
    ) -> tuple[RenameDeviceUseCase, FakeDeviceRepository]:
        repository = FakeDeviceRepository(devices)
        catalog = FakeVolumeCatalog(
            [make_mounted_volume(Path("/mnt/hd3"), TEST_VOLUME_IDENTITY.value)]
            if mounted
            else []
        )
        return (
            RenameDeviceUseCase(
                device_repository=repository,
                describer=DescribeDevicesUseCase(
                    image_repository=FakeImageRepository(),
                    job_repository=InMemoryIndexingJobRepository(),
                    volume_catalog=catalog,
                ),
            ),
            repository,
        )

    def test_it_renames_and_leaves_everything_else_alone(self) -> None:
        original = Device(
            id=TEST_DEVICE_ID,
            volume_identity=TEST_VOLUME_IDENTITY,
            label="HD3",
            filesystem_label="Seagate Backup",
            total_bytes=2_000_398_934_016,
            first_seen_at=EARLIER,
            last_seen_at=EARLIER,
            last_scan_at=EARLIER,
            last_scan_file_count=48210,
        )
        use_case, _ = self.build([original])

        summary = use_case.execute(TEST_DEVICE_ID, "HD3 — aéreas 2018")

        renamed = summary.device
        assert renamed.label == "HD3 — aéreas 2018"
        assert renamed.filesystem_label == original.filesystem_label
        assert renamed.total_bytes == original.total_bytes
        assert renamed.first_seen_at == original.first_seen_at
        assert renamed.last_seen_at == original.last_seen_at
        assert renamed.last_scan_at == original.last_scan_at
        assert renamed.last_scan_file_count == original.last_scan_file_count

    def test_it_works_with_the_disk_in_a_drawer(self) -> None:
        """RFC-027 section 2.3: an unplugged disk is a device we know."""
        use_case, repository = self.build([make_test_device("HD3")], mounted=False)

        summary = use_case.execute(TEST_DEVICE_ID, "HD3 renamed")

        assert summary.device.label == "HD3 renamed"
        assert summary.connected is False
        assert repository.get(TEST_DEVICE_ID) is not None

    def test_the_response_reports_the_disk_as_connected_when_it_is(self) -> None:
        """A hardcoded `connected: false` would blank out a live row in the UI."""
        use_case, _ = self.build([make_test_device("HD3")], mounted=True)

        summary = use_case.execute(TEST_DEVICE_ID, "HD3 renamed")

        assert summary.connected is True
        assert summary.mount_point == Path("/mnt/hd3")

    def test_an_unknown_id_is_not_found(self) -> None:
        use_case, repository = self.build([])

        with pytest.raises(DeviceNotFoundError):
            use_case.execute(compute_device_id(volume_identity(9)), "whatever")

        assert repository.save_calls == []


class TestListDeviceFolders:
    """RFC-031 section 8, including the five states of section 8.2."""

    @pytest.fixture()
    def disk(self, tmp_path: Path) -> Path:
        """A small tree: `2018` with three months, one of which has a subfolder."""
        for folder in ("2018/janeiro/casamento", "2018/fevereiro", "2018/junho"):
            (tmp_path / folder).mkdir(parents=True)
        (tmp_path / "2019").mkdir()
        (tmp_path / "2018" / "loose.jpg").write_bytes(b"\xff\xd8")
        return tmp_path

    def build(
        self,
        disk: Path | None,
        images: FakeImageRepository | None = None,
        jobs: InMemoryIndexingJobRepository | None = None,
    ) -> ListDeviceFoldersUseCase:
        mounts = {TEST_DEVICE_ID: disk} if disk is not None else {}
        return ListDeviceFoldersUseCase(
            device_repository=FakeDeviceRepository([make_test_device("HD3")]),
            image_repository=images or FakeImageRepository(),
            job_repository=jobs or InMemoryIndexingJobRepository(),
            device_locator=FakeDeviceLocator(mounts),
        )

    def test_it_lists_one_level_and_sorts_by_name(self, disk: Path) -> None:
        """The double returns them reversed on purpose; the sorting is the route's."""
        listing = self.build(disk).execute(TEST_DEVICE_ID, "2018")

        assert [folder.name for folder in listing.folders] == [
            "fevereiro",
            "janeiro",
            "junho",
        ]

    def test_files_are_not_listed(self, disk: Path) -> None:
        """What goes back in `scopes[]` is a folder, and a file is not one."""
        listing = self.build(disk).execute(TEST_DEVICE_ID, "2018")

        assert "loose.jpg" not in [folder.name for folder in listing.folders]

    def test_has_children_distinguishes_a_leaf_from_a_branch(self, disk: Path) -> None:
        listing = self.build(disk).execute(TEST_DEVICE_ID, "2018")

        by_name = {folder.name: folder.has_children for folder in listing.folders}
        assert by_name == {"janeiro": True, "fevereiro": False, "junho": False}

    def test_an_absent_path_lists_the_device_root(self, disk: Path) -> None:
        listing = self.build(disk).execute(TEST_DEVICE_ID)

        assert str(listing.scope) == ""
        assert [folder.name for folder in listing.folders] == ["2018", "2019"]

    @pytest.mark.parametrize("path", ["", "."])
    def test_empty_and_dot_both_mean_the_root(self, disk: Path, path: str) -> None:
        """`JobScope` already decides this; no second handling was written."""
        listing = self.build(disk).execute(TEST_DEVICE_ID, path)

        assert str(listing.scope) == ""

    def test_the_paths_are_joined_onto_the_canonical_parent(self, disk: Path) -> None:
        listing = self.build(disk).execute(TEST_DEVICE_ID, "2018")

        assert [str(folder.scope) for folder in listing.folders] == [
            "2018/fevereiro",
            "2018/janeiro",
            "2018/junho",
        ]

    def test_parent_is_one_level_up_and_the_root_is_its_own_parent(
        self, disk: Path
    ) -> None:
        nested = self.build(disk).execute(TEST_DEVICE_ID, "2018/janeiro")
        root = self.build(disk).execute(TEST_DEVICE_ID, "")

        assert str(nested.parent) == "2018"
        assert str(root.parent) == ""

    def test_an_unknown_device_is_not_found(self, disk: Path) -> None:
        with pytest.raises(DeviceNotFoundError):
            self.build(disk).execute(compute_device_id(volume_identity(9)))

    def test_a_disconnected_disk_is_refused_by_name(self) -> None:
        """409 naming the disk: this route needs bytes (RFC-031 section 8)."""
        with pytest.raises(DeviceNotConnectedError) as raised:
            self.build(None).execute(TEST_DEVICE_ID, "2018")

        assert "HD3" in str(raised.value)

    @pytest.mark.parametrize(
        "path", ["..", "../..", "2018/../..", "C:\\", "//server/share", "/absolute"]
    )
    def test_a_path_that_escapes_the_device_is_refused(
        self, disk: Path, path: str
    ) -> None:
        """Refused by `JobScope` before anything is opened, or by the locator after."""
        locator = FakeDeviceLocator({TEST_DEVICE_ID: disk})
        use_case = ListDeviceFoldersUseCase(
            device_repository=FakeDeviceRepository([make_test_device("HD3")]),
            image_repository=FakeImageRepository(),
            job_repository=InMemoryIndexingJobRepository(),
            device_locator=locator,
        )

        with pytest.raises(InvalidJobScopeError):
            use_case.execute(TEST_DEVICE_ID, path)

        assert locator.list_folders_calls == []

    def test_a_path_that_is_not_a_folder_is_refused(self, disk: Path) -> None:
        with pytest.raises(InvalidJobScopeError):
            self.build(disk).execute(TEST_DEVICE_ID, "2018/loose.jpg")

    def test_forty_folders_cost_one_count_query(self, tmp_path: Path) -> None:
        """RFC-031 section 8.2: one grouped read, not one per row."""
        for n in range(40):
            (tmp_path / "2018" / f"m{n:02d}").mkdir(parents=True)
        images = FakeImageRepository()

        listing = self.build(tmp_path, images=images).execute(TEST_DEVICE_ID, "2018")

        assert len(listing.folders) == 40
        assert images.count_by_path_prefixes_calls == [(TEST_DEVICE_ID, "2018")]

    def test_a_folder_with_no_indexed_image_is_zero(self, disk: Path) -> None:
        listing = self.build(disk).execute(TEST_DEVICE_ID, "2018")

        assert all(folder.indexed_images == 0 for folder in listing.folders)

    def test_an_orphan_count_does_not_become_a_row(self, disk: Path) -> None:
        """A folder renamed on disk keeps its rows under the old name.

        The count is real and belongs to nothing the user can see, so it
        is dropped rather than rendered as a folder that is not there
        (RFC-031 section 4.10 of the build prompt).
        """
        images = FakeImageRepository([image_at("2018/dezembro/old.jpg")])

        listing = self.build(disk, images=images).execute(TEST_DEVICE_ID, "2018")

        assert "dezembro" not in [folder.name for folder in listing.folders]

    def test_an_image_directly_in_the_parent_does_not_become_a_row(
        self, disk: Path
    ) -> None:
        """The other key the grouped count returns that is not a folder."""
        images = FakeImageRepository([image_at("2018/loose.jpg")])

        listing = self.build(disk, images=images).execute(TEST_DEVICE_ID, "2018")

        assert "loose.jpg" not in [folder.name for folder in listing.folders]

    def test_the_count_covers_the_whole_subtree(self, disk: Path) -> None:
        images = FakeImageRepository(
            [
                image_at("2018/janeiro/a.jpg"),
                image_at("2018/janeiro/casamento/b.jpg"),
                image_at("2018/fevereiro/c.jpg"),
            ]
        )

        listing = self.build(disk, images=images).execute(TEST_DEVICE_ID, "2018")

        by_name = {folder.name: folder.indexed_images for folder in listing.folders}
        assert by_name == {"janeiro": 2, "fevereiro": 1, "junho": 0}


class TestTheFiveFolderStates:
    """The table of RFC-031 section 8.2, one test per row plus the edges."""

    @pytest.fixture()
    def disk(self, tmp_path: Path) -> Path:
        (tmp_path / "2018" / "janeiro").mkdir(parents=True)
        return tmp_path

    def state_of(
        self,
        disk: Path,
        history: list[IndexingJob],
        images: list[Image] | None = None,
    ) -> tuple[FolderState, IndexingJob | None]:
        """Derive the state of `2018/janeiro` against a hand-built history."""
        jobs = StoredJobRepository(history)
        listing = ListDeviceFoldersUseCase(
            device_repository=FakeDeviceRepository([make_test_device("HD3")]),
            image_repository=FakeImageRepository(images or []),
            job_repository=jobs,
            device_locator=FakeDeviceLocator({TEST_DEVICE_ID: disk}),
        ).execute(TEST_DEVICE_ID, "2018")
        (folder,) = listing.folders
        return folder.state, folder.job

    def test_never_indexed_needs_no_job_and_no_images(self, disk: Path) -> None:
        state, found = self.state_of(disk, [])

        assert state is FolderState.NEVER_INDEXED
        assert found is None

    def test_indexing_when_a_running_job_covers_it(self, disk: Path) -> None:
        state, found = self.state_of(disk, [job(JobStatus.RUNNING, ("2018",))])

        assert state is FolderState.INDEXING
        assert found is not None

    def test_queued_when_a_pending_job_covers_it(self, disk: Path) -> None:
        state, _ = self.state_of(disk, [job(JobStatus.PENDING, ("2018/janeiro",))])

        assert state is FolderState.QUEUED

    def test_indexed_when_the_latest_covering_job_completed(self, disk: Path) -> None:
        state, _ = self.state_of(disk, [job(JobStatus.COMPLETED, ("2018",))])

        assert state is FolderState.INDEXED

    @pytest.mark.parametrize("status", [JobStatus.CANCELLED, JobStatus.FAILED])
    def test_partial_when_the_latest_covering_job_stopped_early(
        self, disk: Path, status: JobStatus
    ) -> None:
        state, found = self.state_of(disk, [job(status, ("2018/janeiro",))])

        assert state is FolderState.PARTIAL
        assert found is not None

    def test_an_empty_scope_list_covers_the_whole_device(self, disk: Path) -> None:
        """`scopes == []` means the whole disk (RFC-029 section 7.1)."""
        state, _ = self.state_of(disk, [job(JobStatus.COMPLETED, ())])

        assert state is FolderState.INDEXED

    def test_partial_by_partial_coverage_even_when_the_job_completed(
        self, disk: Path
    ) -> None:
        """A job over `2018/janeiro/casamento` leaves `2018/janeiro` partly done.

        The case RFC-031 section 8.2 spells out and the one a naive
        one-directional prefix check gets wrong in the direction that
        matters: it would report the folder as fully indexed.
        """
        state, _ = self.state_of(
            disk, [job(JobStatus.COMPLETED, ("2018/janeiro/casamento",))]
        )

        assert state is FolderState.PARTIAL

    def test_a_sibling_sharing_a_prefix_does_not_cover_it(self, disk: Path) -> None:
        """`2018/janeiro` is not covered by `2018/janeiro-b`; parts, not text."""
        state, _ = self.state_of(disk, [job(JobStatus.COMPLETED, ("2018/janeiro-b",))])

        assert state is FolderState.NEVER_INDEXED

    def test_the_newest_covering_job_wins(self, disk: Path) -> None:
        older = job(
            JobStatus.COMPLETED,
            ("2018",),
            created_at=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
        )
        newer = job(
            JobStatus.CANCELLED,
            ("2018",),
            created_at=datetime.datetime(2026, 6, 1, tzinfo=datetime.UTC),
        )

        state, _ = self.state_of(disk, [newer, older])

        assert state is FolderState.PARTIAL

    def test_a_job_for_another_folder_is_ignored(self, disk: Path) -> None:
        state, _ = self.state_of(disk, [job(JobStatus.COMPLETED, ("2019",))])

        assert state is FolderState.NEVER_INDEXED

    def test_images_with_no_job_to_explain_them_are_partial_not_never(
        self, disk: Path
    ) -> None:
        """`never_indexed` needs both halves of the rule, not one.

        A disk indexed before jobs existed has rows the search can
        already return; calling that "never indexed" would be false.
        """
        state, found = self.state_of(disk, [], images=[image_at("2018/janeiro/a.jpg")])

        assert state is FolderState.PARTIAL
        assert found is None


class StoredJobRepository(InMemoryIndexingJobRepository):
    """Holds a history handed to it whole, newest first, without `create()`.

    `create()` refuses a second active job for one device, which is
    correct and which several rows of the section 8.2 table need to
    violate -- a cancelled job and a running one on the same disk is an
    ordinary history, just not an ordinary *present*. So the history is
    installed directly, and `list()` still sorts it by the contract's
    rule rather than returning whatever order it was given.
    """

    def __init__(self, history: list[IndexingJob]) -> None:
        super().__init__()
        for stored in history:
            self._jobs[stored.id.value] = stored
