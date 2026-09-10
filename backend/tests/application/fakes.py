from __future__ import annotations

import datetime
import uuid
from collections.abc import Sequence
from pathlib import Path

from app.domain.entities.device import Device
from app.domain.entities.image import Image
from app.domain.repositories.device_repository import DeviceRepository
from app.domain.repositories.image_repository import ImageRepository
from app.domain.services.content_hasher_port import ContentHasherPort
from app.domain.value_objects.device_id import DeviceId, VolumeIdentity, VolumeKind
from app.domain.value_objects.embedding_vector import EmbeddingVector
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.index_metadata import IndexMetadata
from app.domain.value_objects.indexing_record import IndexingRecord
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
    matches_filters,
)


class FakeImageRepository(ImageRepository):
    """In-memory repository double used by application use case tests."""

    def __init__(self, images: list[Image] | None = None) -> None:
        self._images = list(images or [])
        self._metadata: dict[uuid.UUID, IndexMetadata] = {}
        self._embeddings: dict[uuid.UUID, EmbeddingVector] = {}
        self.save_calls: list[Image] = []
        self.exists_calls: list[ImageId] = []
        self.list_calls = 0
        self.save_indexed_calls: list[IndexingRecord] = []
        self.save_indexed_many_calls: list[list[IndexingRecord]] = []
        self.get_index_metadata_calls: list[ImageId] = []
        self.get_index_metadata_many_calls: list[Sequence[ImageId]] = []
        self.update_index_metadata_calls: list[tuple[ImageId, IndexMetadata]] = []
        self.search_similar_calls: list[
            tuple[EmbeddingVector, int, SearchFilters | None]
        ] = []

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
        self.save_indexed_many_calls.append(list(records))
        for record in records:
            self.save_indexed(record)

    def get_index_metadata(self, image_id: ImageId) -> IndexMetadata | None:
        self.get_index_metadata_calls.append(image_id)
        if not any(image.id == image_id for image in self._images):
            return None
        return self._metadata.get(
            image_id.value, IndexMetadata(file_size=None, file_modified_at=None)
        )

    def get_index_metadata_many(
        self, image_ids: Sequence[ImageId]
    ) -> dict[ImageId, IndexMetadata]:
        self.get_index_metadata_many_calls.append(list(image_ids))
        found = {}
        for image_id in image_ids:
            if any(image.id == image_id for image in self._images):
                found[image_id] = self._metadata.get(
                    image_id.value,
                    IndexMetadata(file_size=None, file_modified_at=None),
                )
        return found

    def update_index_metadata(self, image_id: ImageId, metadata: IndexMetadata) -> None:
        self.update_index_metadata_calls.append((image_id, metadata))
        if any(image.id == image_id for image in self._images):
            self._metadata[image_id.value] = metadata


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
    from app.infrastructure.filesystem.device_identity import compute_device_id

    identity = VolumeIdentity(value=volume_value, kind=VolumeKind.WINDOWS_VOLUME_GUID)
    now = datetime.datetime.now(tz=datetime.UTC)
    return Device(
        id=compute_device_id(identity),
        volume_identity=identity,
        label=label,
        first_seen_at=now,
        last_seen_at=now,
    )
