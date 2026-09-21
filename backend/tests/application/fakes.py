from __future__ import annotations

import dataclasses
import datetime
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from app.domain.entities.device import Device
from app.domain.entities.image import Image
from app.domain.repositories.device_repository import DeviceRepository
from app.domain.repositories.image_repository import ImageRepository
from app.domain.services.content_hasher_port import ContentHasherPort
from app.domain.services.device_locator import DeviceLocator, FolderEntry
from app.domain.services.file_revealer_port import FileRevealerPort
from app.domain.services.thumbnail_generator_port import ThumbnailGeneratorPort
from app.domain.services.thumbnail_store_port import ThumbnailStorePort
from app.domain.services.volume_catalog import VolumeCatalog
from app.domain.value_objects.capture_date import CaptureDate
from app.domain.value_objects.device_id import DeviceId, VolumeIdentity, VolumeKind
from app.domain.value_objects.embedding_vector import EmbeddingVector
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.index_metadata import IndexMetadata
from app.domain.value_objects.indexing_record import IndexingRecord
from app.domain.value_objects.job_scope import JobScope
from app.domain.value_objects.mounted_volume import MountedVolume
from app.domain.value_objects.search_filters import SearchFilters
from app.domain.value_objects.search_hit import SearchHits
from app.infrastructure.filesystem.sha256_content_hasher import Sha256ContentHasher
from app.infrastructure.filesystem.volume_identity_provider import (
    ResolvedVolume,
    VolumeIdentityProvider,
)
from app.infrastructure.persistence.in_memory_device_repository import (
    InMemoryDeviceRepository,
)
from app.infrastructure.persistence.in_memory_image_repository import (
    cosine_search,
    count_images_by_device,
    count_images_by_path_prefix,
    count_unknown_capture_date,
    matches_filters,
    store_thumbnail,
    with_capture_date,
)


class FakeImageRepository(ImageRepository):
    """In-memory repository double used by application use case tests."""

    def __init__(self, images: list[Image] | None = None) -> None:
        self._images = list(images or [])
        self._metadata: dict[uuid.UUID, IndexMetadata] = {}
        self._embeddings: dict[uuid.UUID, EmbeddingVector] = {}
        self._thumbnails: dict[uuid.UUID, str] = {}
        self.save_calls: list[Image] = []
        self.exists_calls: list[ImageId] = []
        self.list_calls = 0
        self.save_indexed_calls: list[IndexingRecord] = []
        self.save_indexed_many_calls: list[list[IndexingRecord]] = []
        self.get_index_metadata_calls: list[ImageId] = []
        self.get_index_metadata_many_calls: list[Sequence[ImageId]] = []
        self.update_index_metadata_calls: list[tuple[ImageId, IndexMetadata]] = []
        self.update_capture_date_calls: list[tuple[ImageId, CaptureDate]] = []
        self.update_capture_date_many_calls: list[dict[ImageId, CaptureDate]] = []
        self.count_unknown_capture_date_calls: list[SearchFilters] = []
        self.count_by_device_calls = 0
        """How many times the grouped device count was asked for.

        A counter rather than a list because the method takes no
        arguments, and the only thing worth asserting about it is that
        `GET /api/v1/devices` asks **once** for twenty devices rather
        than twenty times (RFC-031 section 4.3).
        """

        self.count_by_path_prefixes_calls: list[tuple[DeviceId, str]] = []
        """Every grouped folder count, so a test can prove there was one.

        Forty folders must produce one query, not forty, and a spy that
        only counted calls without recording the prefix could not tell a
        single grouped read from a loop over one folder.
        """

        self.update_thumbnail_path_calls: list[tuple[ImageId, str]] = []
        self.update_thumbnail_path_many_calls: list[dict[ImageId, str]] = []
        self.search_similar_calls: list[
            tuple[EmbeddingVector, int, SearchFilters | None]
        ] = []
        self.on_save_indexed_many: Callable[[], None] | None = None
        """Run just before a bulk write lands, for tests that need the middle.

        Some things can only be asserted about a run while it is running:
        a disk unplugged halfway (RFC-029 section 15), an operator pressing
        `Ctrl+C`. Both are events in the world rather than in the
        repository, and this is the only place a test can reach into a
        pipeline that is otherwise a single call.
        """

    def seed_embedding(self, image: Image, embedding: EmbeddingVector) -> None:
        """Make `image` findable by search, without going through indexing.

        This fake is constructed as `FakeImageRepository(images=[...])`
        from bare `Image` entities, which carry no embedding -- so before
        RFC-025 there was no way to set up a ranking scenario at all. The
        alternative is building a full `IndexingRecord` with file sizes
        and timestamps that a search test does not care about and would
        have to invent.

        Records nothing: seeding is arrangement, not behavior under test.
        """
        if all(existing.id != image.id for existing in self._images):
            self._images.append(image)
        self._embeddings[image.id.value] = embedding

    def save(self, image: Image) -> None:
        self.save_calls.append(image)
        self._images.append(image)

    def get(self, image_id: ImageId) -> Image | None:
        for image in self._images:
            if image.id == image_id:
                return image
        return None

    def exists(self, image_id: ImageId) -> bool:
        self.exists_calls.append(image_id)
        return any(image.id == image_id for image in self._images)

    def delete(self, image_id: ImageId) -> None:
        self._images = [image for image in self._images if image.id != image_id]
        self._metadata.pop(image_id.value, None)
        self._embeddings.pop(image_id.value, None)
        self._thumbnails.pop(image_id.value, None)

    def list(self) -> list[Image]:
        self.list_calls += 1
        return list(self._images)

    def save_indexed(self, record: IndexingRecord) -> None:
        self.save_indexed_calls.append(record)
        self._images = [image for image in self._images if image.id != record.image.id]
        self._images.append(record.image)
        self._metadata[record.image.id.value] = IndexMetadata(
            file_size=record.file_size,
            file_modified_at=record.file_modified_at,
            content_hash=record.content_hash,
        )
        self._embeddings[record.image.id.value] = record.embedding
        store_thumbnail(self._thumbnails, record)

    def search_similar(
        self,
        embedding: EmbeddingVector,
        limit: int,
        filters: SearchFilters | None = None,
    ) -> SearchHits:
        """Rank the seeded embeddings, using the same code as the real double.

        Delegates to `cosine_search()` and `matches_filters()` rather than
        repeating either, so that this fake, `InMemoryImageRepository`,
        and `PostgresImageRepository` are held to one contract by
        `tests/infrastructure/persistence/test_search_similar_contract.py`.
        A second hand-written cosine here, or a second hand-written filter
        predicate, would be a second thing to keep in agreement with
        pgvector, and the first to quietly stop agreeing.
        """
        self.search_similar_calls.append((embedding, limit, filters))
        return cosine_search(
            embedding,
            (
                (image, self._embeddings[image.id.value])
                for image in self._images
                if image.id.value in self._embeddings
                and matches_filters(image, filters)
            ),
            limit,
        )

    def save_indexed_many(self, records: Sequence[IndexingRecord]) -> None:
        if self.on_save_indexed_many is not None:
            self.on_save_indexed_many()
        self.save_indexed_many_calls.append(list(records))
        for record in records:
            self.save_indexed(record)

    def get_index_metadata(self, image_id: ImageId) -> IndexMetadata | None:
        self.get_index_metadata_calls.append(image_id)
        return self._stored_metadata(image_id)

    def get_index_metadata_many(
        self, image_ids: Sequence[ImageId]
    ) -> dict[ImageId, IndexMetadata]:
        self.get_index_metadata_many_calls.append(list(image_ids))
        found = {}
        for image_id in image_ids:
            metadata = self._stored_metadata(image_id)
            if metadata is not None:
                found[image_id] = metadata
        return found

    def _stored_metadata(self, image_id: ImageId) -> IndexMetadata | None:
        """The metadata for one id, with the capture source read off the image.

        Same rule as `InMemoryImageRepository.get_index_metadata()`: the
        entity holds the capture date, and `_metadata` does not keep a
        second copy of it that could disagree.
        """
        image = self.get(image_id)
        if image is None:
            return None
        stored = self._metadata.get(
            image_id.value, IndexMetadata(file_size=None, file_modified_at=None)
        )
        return dataclasses.replace(
            stored,
            capture_source=image.capture_source,
            thumbnail_path=self._thumbnails.get(image_id.value),
        )

    def update_index_metadata(self, image_id: ImageId, metadata: IndexMetadata) -> None:
        self.update_index_metadata_calls.append((image_id, metadata))
        if any(image.id == image_id for image in self._images):
            self._metadata[image_id.value] = metadata

    def update_capture_date(self, image_id: ImageId, capture: CaptureDate) -> None:
        self.update_capture_date_calls.append((image_id, capture))
        self._apply_capture_dates({image_id: capture})

    def update_capture_date_many(self, captures: Mapping[ImageId, CaptureDate]) -> None:
        self.update_capture_date_many_calls.append(dict(captures))
        self._apply_capture_dates(captures)

    def _apply_capture_dates(self, captures: Mapping[ImageId, CaptureDate]) -> None:
        self._images = [
            (
                with_capture_date(image, captures[image.id])
                if image.id in captures
                else image
            )
            for image in self._images
        ]

    def update_thumbnail_path(self, image_id: ImageId, location: str) -> None:
        self.update_thumbnail_path_calls.append((image_id, location))
        self._apply_thumbnail_paths({image_id: location})

    def update_thumbnail_path_many(self, locations: Mapping[ImageId, str]) -> None:
        self.update_thumbnail_path_many_calls.append(dict(locations))
        self._apply_thumbnail_paths(locations)

    def _apply_thumbnail_paths(self, locations: Mapping[ImageId, str]) -> None:
        known = {image.id for image in self._images}
        for image_id, location in locations.items():
            if image_id in known:
                self._thumbnails[image_id.value] = location

    def count_unknown_capture_date(self, filters: SearchFilters) -> int:
        """Delegates to the shared counter, and records that it was asked.

        The record is what lets a use-case test prove the count is *not*
        requested for a search without a date range (RFC-028 section 8):
        the extra query must not exist at all, not merely return 0.
        """
        self.count_unknown_capture_date_calls.append(filters)
        return count_unknown_capture_date(
            (image for image in self._images if image.id.value in self._embeddings),
            filters,
        )

    def count_by_device(self) -> dict[DeviceId, int]:
        """Delegates to the shared tally, and records that it was asked.

        Shared for the reason `search_similar()` delegates to
        `cosine_search()`: `test_image_counting_contract.py` holds this
        fake, `InMemoryImageRepository` and PostgreSQL to one answer, and
        a second hand-written tally here would be the first to drift.
        """
        self.count_by_device_calls += 1
        return count_images_by_device(self._images)

    def count_by_path_prefixes(
        self, device_id: DeviceId, parent: JobScope
    ) -> dict[str, int]:
        """The grouped folder count, delegated and recorded; see above."""
        self.count_by_path_prefixes_calls.append((device_id, str(parent)))
        return count_images_by_path_prefix(self._images, device_id, parent)


class RecordingContentHasher(ContentHasherPort):
    """Real SHA-256 hashing, with a record of which images it was asked about.

    Delegates rather than faking a digest: the cost-ascending rule in
    ARCHITECTURE.md section 16 is about *when* hashing happens, so the
    tests that matter count calls, and a fake digest would only add a way
    for the tests to disagree with production.
    """

    def __init__(self) -> None:
        self._delegate = Sha256ContentHasher()
        self.hashed: list[Image] = []

    def hash_image(self, image: Image) -> str:
        self.hashed.append(image)
        return self._delegate.hash_image(image)


class RecordingThumbnailGenerator(ThumbnailGeneratorPort):
    """Draws nothing and reads nothing; records what it was asked to render.

    The pipeline tests ask about control flow -- which images get a
    thumbnail, and what a failure does -- and the images they build sit at
    paths that do not exist. The real Pillow adapter is covered on its own
    by `tests/infrastructure/filesystem/test_thumbnail_generator.py`.

    `failing` names the filenames to refuse, the way `_BatchFailsForFilename`
    does for the model.
    """

    def __init__(self, failing: frozenset[str] = frozenset()) -> None:
        self.failing = failing
        self.generated: list[tuple[Image, int]] = []

    def generate(self, image: Image, max_edge: int) -> bytes:
        self.generated.append((image, max_edge))
        if image.filename in self.failing:
            raise OSError(f"cannot decode {image.filename}")
        return f"thumbnail:{image.id}:{max_edge}".encode()


class InMemoryThumbnailStore(ThumbnailStorePort):
    """Keeps thumbnails in a dict, and has no file to hand back.

    `locate()` answers `None` always and records that it was asked, which
    is what the conditional-request tests need: a `304` must be decided
    without the store being consulted at all.
    """

    def __init__(self) -> None:
        self.saved: dict[ImageId, bytes] = {}
        self.locate_calls: list[str] = []

    def save(self, image_id: ImageId, data: bytes) -> str:
        self.saved[image_id] = data
        return f"memory/{image_id.value}.jpg"

    def locate(self, location: str) -> Path | None:
        self.locate_calls.append(location)
        return None


class FakeFileRevealer(FileRevealerPort):
    """Opens no window; answers "is the file there" from the real filesystem.

    Filesystem-backed for the reason `FakeDeviceLocator` is: the `/reveal`
    tests put a real file under `tmp_path` and delete it to get a 410, so
    the missing-file rule is exercised against a disk rather than against a
    flag the test set. **Nothing in the default suite may start
    `explorer.exe`** -- `WindowsFileRevealer` is tested with its process
    launcher replaced.
    """

    def __init__(self) -> None:
        self.revealed: list[ImagePath] = []

    def reveal(self, path: ImagePath) -> None:
        if not path.value.is_file():
            raise FileNotFoundError(f"No file at {path}")
        self.revealed.append(path)


class StubContentHasher(ContentHasherPort):
    """Hashes nothing, reads nothing, and answers whatever it was told to.

    The Application-layer tests build images at paths that do not exist,
    on purpose -- the use cases are supposed to work in terms of ports, not
    files. Handing them the real hasher would make every one of those tests
    depend on a real filesystem to answer a question about control flow.

    `digests` maps a path (as `str(image.relative_path)`) to the digest to
    return;
    anything absent gets `default`.
    """

    def __init__(
        self,
        digests: dict[str, str] | None = None,
        default: str = "0" * 64,
    ) -> None:
        self.digests = digests or {}
        self.default = default
        self.hashed: list[Image] = []

    def hash_image(self, image: Image) -> str:
        self.hashed.append(image)
        return self.digests.get(str(image.relative_path), self.default)


class FakeDeviceRepository(DeviceRepository):
    """In-memory device repository double with call recording.

    Delegates every operation to `InMemoryDeviceRepository` rather than
    keeping a second dict, for the reason `FakeImageRepository.search_similar`
    delegates to `cosine_search`: a double that stores devices its own way
    is a second implementation of the port, and the first one to stop
    agreeing with the contract test.
    """

    def __init__(self, devices: list[Device] | None = None) -> None:
        self._delegate = InMemoryDeviceRepository()
        for device in devices or []:
            self._delegate.save(device)
        self.save_calls: list[Device] = []
        self.get_by_volume_identity_calls: list[VolumeIdentity] = []

    def save(self, device: Device) -> None:
        self.save_calls.append(device)
        self._delegate.save(device)

    def get(self, device_id: DeviceId) -> Device | None:
        return self._delegate.get(device_id)

    def get_by_volume_identity(self, identity: VolumeIdentity) -> Device | None:
        self.get_by_volume_identity_calls.append(identity)
        return self._delegate.get_by_volume_identity(identity)

    def list(self) -> list[Device]:
        return self._delegate.list()

    def delete(self, device_id: DeviceId) -> None:
        self._delegate.delete(device_id)


class StubVolumeIdentityProvider(VolumeIdentityProvider):
    r"""Answers with a volume the test invented, touching no real disk.

    Every test that exercises the device pipeline needs a volume identity,
    and no test may depend on which disks the machine running it happens
    to have. `WindowsVolumeIdentityProvider` is covered on its own by
    `tests/infrastructure/filesystem/test_volume_identity.py`, which is
    where real `kernel32` calls belong.

    `mount_point` defaults to whatever `resolve()` is asked about, which
    makes the common case -- "pretend this tmp_path is a whole disk" --
    a one-liner, and makes `relative_path` come out relative to the
    test's own directory rather than to `C:\`.
    """

    def __init__(
        self,
        identity: VolumeIdentity | None = None,
        mount_point: Path | None = None,
        filesystem_label: str | None = None,
        total_bytes: int | None = None,
    ) -> None:
        self.identity = identity or VolumeIdentity(
            value="\\\\?\\Volume{00000000-0000-0000-0000-000000000001}\\",
            kind=VolumeKind.WINDOWS_VOLUME_GUID,
        )
        self.mount_point = mount_point
        self.filesystem_label = filesystem_label
        self.total_bytes = total_bytes
        self.resolve_calls: list[Path] = []

    def resolve(self, path: Path) -> ResolvedVolume:
        self.resolve_calls.append(path)
        return ResolvedVolume(
            identity=self.identity,
            mount_point=self.mount_point or path,
            filesystem_label=self.filesystem_label,
            total_bytes=self.total_bytes,
        )

    def mounted_volumes(self) -> dict[VolumeIdentity, Path]:
        if self.mount_point is None:
            return {}
        return {self.identity: self.mount_point}


class FakeDeviceLocator(DeviceLocator):
    """Answers "where is this disk" and "is this folder on it" from a real tmp dir.

    Filesystem-backed rather than a dict of declared answers, so that a
    test asking about a scope gets the same answer the Windows adapter
    would: `tmp_path` stands in for a mount point, and a folder either
    exists under it or does not.

    `disconnect()` is what makes the RFC-029 section 15 case testable
    without unplugging anything -- a disk that was present when the job
    was created and gone by the time it finished. A locator that cached
    would be unable to express it, which is exactly why the port forbids
    caching.
    """

    def __init__(self, mounts: dict[DeviceId, Path] | None = None) -> None:
        self._mounts = dict(mounts or {})
        self.mount_point_calls: list[DeviceId] = []
        self.resolve_scope_calls: list[tuple[DeviceId, str]] = []
        self.list_folders_calls: list[tuple[DeviceId, str]] = []

    def mount_point(self, device: Device) -> Path | None:
        self.mount_point_calls.append(device.id)
        return self._mounts.get(device.id)

    def resolve_scope(self, device: Device, scope: JobScope) -> JobScope | None:
        """Resolve against the real `tmp_path`, and return what the disk says.

        **The returned scope is the filesystem's spelling, not the one
        that was asked for**, which is half the reason `resolve_scope()`
        exists at all: Windows is case-insensitive while
        `normalize_scopes()` compares parts exactly, so a locator that
        echoed `FOTOS` back would hand `POST /jobs` a scope it would
        rewrite into a different one (RFC-031 section 4.5 of the build
        prompt).

        This double used to return `scope` unchanged, which was
        indistinguishable from correct on every test that asked for a
        folder by its real name -- and would have made the canonical-
        spelling test in `test_devices_api.py` a test of this method
        rather than of the route. `Path.resolve()` is what
        `MountedDeviceLocator` uses for exactly this, and on Windows it
        returns the real case.
        """
        self.resolve_scope_calls.append((device.id, str(scope)))
        mount = self._mounts.get(device.id)
        if mount is None:
            return None
        root = mount.resolve()
        target = (root / str(scope)) if scope.parts else root
        resolved = target.resolve()
        if not resolved.is_dir():
            return None
        if resolved != root and not resolved.is_relative_to(root):
            return None
        if resolved == root:
            return JobScope()
        return JobScope(resolved.relative_to(root))

    def list_folders(self, device: Device, scope: JobScope) -> list[FolderEntry] | None:
        """List real subdirectories of a real `tmp_path`, in the worst order.

        Filesystem-backed like the rest of this double, so `has_children`
        is answered by looking rather than by a flag the test set --
        which is what makes a test about expand arrows a test about
        folders.

        **The order is deliberately reversed, and that is not
        arbitrariness.** `DeviceLocator.list_folders()` promises no order
        at all, because `scandir` has none to promise, and the sorting
        belongs to whoever renders the list (RFC-031 section 4.5 of the
        build prompt). Returning entries already sorted would let a use
        case that forgot to sort pass every test here and then hand a
        real Windows client its folders in NTFS order.
        """
        self.list_folders_calls.append((device.id, str(scope)))
        mount = self._mounts.get(device.id)
        if mount is None:
            return None
        target = mount / str(scope) if scope.parts else mount
        if not target.is_dir():
            return None
        return [
            FolderEntry(
                name=child.name,
                has_children=any(grandchild.is_dir() for grandchild in child.iterdir()),
            )
            for child in sorted(target.iterdir(), key=lambda p: p.name, reverse=True)
            if child.is_dir()
        ]

    def connect(self, device_id: DeviceId, mount_point: Path) -> None:
        self._mounts[device_id] = mount_point

    def disconnect(self, device_id: DeviceId) -> None:
        """Pull the cable, as far as anything asking this locator can tell."""
        self._mounts.pop(device_id, None)


class FakeVolumeCatalog(VolumeCatalog):
    """Answers with volumes a test invented, and counts how often it was asked.

    The counting is the point rather than a convenience. RFC-031 section
    4.1 requires `GET /api/v1/devices` to enumerate the filesystem
    **once** however many devices are registered, and the only way to
    know that is to count -- a use case that called `mount_points()` per
    device would return exactly the same JSON.

    The two methods are counted separately because they cost differently:
    `list_mounted()` reads a label and a capacity per volume, and reading
    a capacity can wake a sleeping external disk. A route that reached
    for the rich one where the cheap one would do is a real regression
    with no visible symptom, and this is what notices.
    """

    def __init__(self, volumes: list[MountedVolume] | None = None) -> None:
        self._volumes = list(volumes or [])
        self.mount_points_calls = 0
        self.list_mounted_calls = 0

    def mount_points(self) -> dict[VolumeIdentity, Path]:
        self.mount_points_calls += 1
        return {volume.identity: volume.mount_point for volume in self._volumes}

    def list_mounted(self) -> list[MountedVolume]:
        self.list_mounted_calls += 1
        return list(self._volumes)

    def attach(self, volume: MountedVolume) -> None:
        self._volumes.append(volume)

    def detach(self, identity: VolumeIdentity) -> None:
        """Pull the cable, as far as anything asking this catalogue can tell."""
        self._volumes = [
            volume for volume in self._volumes if volume.identity != identity
        ]


def make_mounted_volume(
    mount_point: Path,
    volume_value: str = "\\\\?\\Volume{00000000-0000-0000-0000-000000000001}\\",
    filesystem_label: str | None = None,
    total_bytes: int | None = None,
) -> MountedVolume:
    """Build a `MountedVolume` shaped like one the Windows adapter would report."""
    return MountedVolume(
        identity=VolumeIdentity(
            value=volume_value, kind=VolumeKind.WINDOWS_VOLUME_GUID
        ),
        mount_point=mount_point,
        filesystem_label=filesystem_label,
        total_bytes=total_bytes,
    )


def make_device(
    volume_value: str = "\\\\?\\Volume{00000000-0000-0000-0000-000000000001}\\",
    label: str = "HD-TEST",
) -> Device:
    """Build a `Device` whose id is derived exactly as production derives it.

    Uses `compute_device_id()` rather than a literal UUID so that a test
    fixture cannot drift from the derivation every real device goes
    through -- the id is half of every `ImageId`, so a hand-picked one
    would make the fixture describe a device the system could never
    produce.
    """
    from app.domain.services.device_identity import compute_device_id

    identity = VolumeIdentity(value=volume_value, kind=VolumeKind.WINDOWS_VOLUME_GUID)
    now = datetime.datetime.now(tz=datetime.UTC)
    return Device(
        id=compute_device_id(identity),
        volume_identity=identity,
        label=label,
        first_seen_at=now,
        last_seen_at=now,
    )
