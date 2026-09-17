"""The RFC-030 read use cases -- details, thumbnail, reveal -- against fakes.

The HTTP tests in `tests/presentation/` pin the statuses; these pin the
decisions underneath them, including the two that are easy to get subtly
wrong: that a disconnected disk is an answer for details and a refusal for
reveal, and that a current `If-None-Match` is decided without the thumbnail
store ever being asked.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from app.application.use_cases.get_image_details import GetImageDetailsUseCase
from app.application.use_cases.get_thumbnail import (
    GetThumbnailUseCase,
    ThumbnailFile,
    ThumbnailUnchanged,
)
from app.application.use_cases.resolve_image_location import (
    ResolveImageLocationUseCase,
)
from app.application.use_cases.reveal_image import RevealImageUseCase
from app.domain.entities.image import Image
from app.domain.exceptions import (
    DeviceNotConnectedError,
    FileGoneError,
    ImageNotFoundError,
    ThumbnailNotFoundError,
)
from app.domain.value_objects.embedding_vector import EmbeddingVector
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.indexing_record import IndexingRecord
from app.infrastructure.filesystem.thumbnail_store import FilesystemThumbnailStore
from tests.application.fakes import (
    FakeDeviceLocator,
    FakeDeviceRepository,
    FakeFileRevealer,
    FakeImageRepository,
    InMemoryThumbnailStore,
    make_device,
)

HD3 = make_device(label="HD3")
RELATIVE = "fotos/2018/junho/DJI_0042.JPG"


def indexed_image(
    repository: FakeImageRepository,
    content_hash: str | None = "a" * 64,
    thumbnail_path: str | None = None,
) -> Image:
    image = Image(
        id=ImageId(uuid.uuid4()),
        device_id=HD3.id,
        relative_path=ImagePath(RELATIVE),
        filename="DJI_0042",
        extension="jpg",
    )
    repository.save_indexed(
        IndexingRecord(
            image=image,
            embedding=EmbeddingVector([0.1] * 8),
            file_size=1,
            file_modified_at=None,
            content_hash=content_hash,
            thumbnail_path=thumbnail_path,
        )
    )
    return image


def resolver(locator: FakeDeviceLocator) -> ResolveImageLocationUseCase:
    return ResolveImageLocationUseCase(FakeDeviceRepository([HD3]), locator)


class TestGetImageDetails:
    def test_an_unknown_id_is_not_found(self) -> None:
        use_case = GetImageDetailsUseCase(
            FakeImageRepository(), resolver(FakeDeviceLocator())
        )

        with pytest.raises(ImageNotFoundError):
            use_case.execute(ImageId(uuid.uuid4()))

    def test_a_disconnected_disk_is_an_answer_not_an_error(self) -> None:
        repository = FakeImageRepository()
        image = indexed_image(repository)
        use_case = GetImageDetailsUseCase(repository, resolver(FakeDeviceLocator()))

        located = use_case.execute(image.id)

        assert not located.connected
        assert located.image.absolute_path is None
        assert located.device.label == "HD3"
        assert str(located.image.relative_path) == RELATIVE

    def test_a_connected_disk_gives_the_absolute_path(self, tmp_path: Path) -> None:
        repository = FakeImageRepository()
        image = indexed_image(repository)
        use_case = GetImageDetailsUseCase(
            repository, resolver(FakeDeviceLocator({HD3.id: tmp_path}))
        )

        located = use_case.execute(image.id)

        assert located.connected
        assert located.image.absolute_path == ImagePath(tmp_path / RELATIVE)


class TestGetThumbnail:
    def test_an_unknown_id_is_not_found(self) -> None:
        use_case = GetThumbnailUseCase(FakeImageRepository(), InMemoryThumbnailStore())

        with pytest.raises(ImageNotFoundError):
            use_case.execute(ImageId(uuid.uuid4()))

    def test_an_image_without_a_thumbnail_is_a_thumbnail_not_found(self) -> None:
        repository = FakeImageRepository()
        image = indexed_image(repository, thumbnail_path=None)

        with pytest.raises(ThumbnailNotFoundError):
            GetThumbnailUseCase(repository, InMemoryThumbnailStore()).execute(image.id)

    def test_the_file_is_returned_with_the_photos_content_hash_as_its_version(
        self, tmp_path: Path
    ) -> None:
        repository = FakeImageRepository()
        store = FilesystemThumbnailStore(tmp_path)
        image = indexed_image(repository)
        location = store.save(image.id, b"jpeg")
        repository.update_thumbnail_path(image.id, location)

        result = GetThumbnailUseCase(repository, store).execute(image.id)

        assert isinstance(result, ThumbnailFile)
        assert result.path.read_bytes() == b"jpeg"
        assert result.version == "a" * 64

    def test_a_current_version_is_confirmed_without_asking_the_store(self) -> None:
        """RFC-030 section 12: a revalidation reads no thumbnail from disk."""
        repository = FakeImageRepository()
        image = indexed_image(repository, thumbnail_path="aa/x.jpg")
        store = InMemoryThumbnailStore()

        result = GetThumbnailUseCase(repository, store).execute(
            image.id, held_versions={"f" * 64, "a" * 64}
        )

        assert result == ThumbnailUnchanged(version="a" * 64)
        assert store.locate_calls == []

    def test_a_stale_version_is_answered_with_the_file(self, tmp_path: Path) -> None:
        repository = FakeImageRepository()
        store = FilesystemThumbnailStore(tmp_path)
        image = indexed_image(repository, content_hash="b" * 64)
        repository.update_thumbnail_path(image.id, store.save(image.id, b"new"))

        result = GetThumbnailUseCase(repository, store).execute(
            image.id, held_versions={"a" * 64}
        )

        assert isinstance(result, ThumbnailFile)
        assert result.version == "b" * 64

    def test_a_row_with_no_content_hash_never_claims_to_be_unchanged(
        self, tmp_path: Path
    ) -> None:
        """Rows indexed before RFC-024 have no version to validate against."""
        repository = FakeImageRepository()
        store = FilesystemThumbnailStore(tmp_path)
        image = indexed_image(repository, content_hash=None)
        repository.update_thumbnail_path(image.id, store.save(image.id, b"jpeg"))

        result = GetThumbnailUseCase(repository, store).execute(
            image.id, held_versions={"None", ""}
        )

        assert isinstance(result, ThumbnailFile)
        assert result.version is None

    def test_a_thumbnail_cleaned_out_of_the_cache_is_not_found(self) -> None:
        """The cache is the app's own; its absence is a 404, never a 410."""
        repository = FakeImageRepository()
        image = indexed_image(repository, thumbnail_path="aa/cleaned.jpg")

        with pytest.raises(ThumbnailNotFoundError):
            GetThumbnailUseCase(repository, InMemoryThumbnailStore()).execute(image.id)

    def test_the_device_is_never_asked_about(self, tmp_path: Path) -> None:
        """Thumbnails live on a disk that is always there (RFC-030 section 4.1).

        The use case takes no locator at all, which is the whole guarantee;
        this pins that the file is served from the cache with the photo's
        disk nowhere in sight.
        """
        repository = FakeImageRepository()
        store = FilesystemThumbnailStore(tmp_path)
        image = indexed_image(repository)
        repository.update_thumbnail_path(image.id, store.save(image.id, b"jpeg"))

        result = GetThumbnailUseCase(repository, store).execute(image.id)

        assert isinstance(result, ThumbnailFile)


class TestRevealImage:
    def reveal(
        self, repository: FakeImageRepository, locator: FakeDeviceLocator
    ) -> tuple[RevealImageUseCase, FakeFileRevealer]:
        revealer = FakeFileRevealer()
        return RevealImageUseCase(repository, resolver(locator), revealer), revealer

    def test_the_server_builds_the_path_from_the_row_and_the_mount(
        self, tmp_path: Path
    ) -> None:
        photo = tmp_path / RELATIVE
        photo.parent.mkdir(parents=True)
        photo.write_bytes(b"x")
        repository = FakeImageRepository()
        image = indexed_image(repository)
        use_case, revealer = self.reveal(
            repository, FakeDeviceLocator({HD3.id: tmp_path})
        )

        use_case.execute(image.id)

        assert revealer.revealed == [ImagePath(photo)]

    def test_an_unknown_id_is_not_found_and_reveals_nothing(self) -> None:
        use_case, revealer = self.reveal(FakeImageRepository(), FakeDeviceLocator())

        with pytest.raises(ImageNotFoundError):
            use_case.execute(ImageId(uuid.uuid4()))

        assert revealer.revealed == []

    def test_a_disconnected_disk_is_a_conflict_naming_the_disk(self) -> None:
        repository = FakeImageRepository()
        image = indexed_image(repository)
        use_case, revealer = self.reveal(repository, FakeDeviceLocator())

        with pytest.raises(DeviceNotConnectedError, match="HD3"):
            use_case.execute(image.id)

        assert revealer.revealed == []

    def test_a_file_gone_from_a_connected_disk_is_gone(self, tmp_path: Path) -> None:
        repository = FakeImageRepository()
        image = indexed_image(repository)
        use_case, revealer = self.reveal(
            repository, FakeDeviceLocator({HD3.id: tmp_path})
        )

        with pytest.raises(FileGoneError, match="HD3"):
            use_case.execute(image.id)

        assert revealer.revealed == []

    def test_execute_accepts_an_id_and_nothing_else(self) -> None:
        """RFC-030 section 5.1, at the layer below the route."""
        import inspect

        parameters = inspect.signature(RevealImageUseCase.execute).parameters

        assert list(parameters) == ["self", "image_id"]
        assert parameters["image_id"].annotation in (ImageId, "ImageId")
