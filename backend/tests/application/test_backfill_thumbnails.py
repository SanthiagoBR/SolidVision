"""The thumbnail backfill's decisions, against fakes (RFC-030 section 7.2)."""

from __future__ import annotations

import uuid
from collections.abc import Mapping

import pytest

from app.application.use_cases.backfill_thumbnails import BackfillThumbnailsUseCase
from app.application.use_cases.indexing_plan import IndexCandidate
from app.application.use_cases.thumbnail_writer import ThumbnailWriter
from app.domain.entities.image import Image
from app.domain.value_objects.embedding_vector import EmbeddingVector
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.indexing_record import IndexingRecord
from tests.application.fakes import (
    FakeImageRepository,
    InMemoryThumbnailStore,
    RecordingThumbnailGenerator,
)
from tests.conftest import TEST_DEVICE_ID


def _image(name: str) -> Image:
    path = ImagePath(f"fotos/{name}.jpg")
    return Image(
        id=ImageId(uuid.uuid5(uuid.NAMESPACE_URL, str(path))),
        device_id=TEST_DEVICE_ID,
        relative_path=path,
        filename=name,
        extension="jpg",
    )


def _candidate(name: str) -> IndexCandidate:
    return IndexCandidate(image=_image(name), file_size=1, file_modified_at=None)


def _indexed(
    repository: FakeImageRepository, name: str, thumbnail_path: str | None = None
) -> None:
    repository.save_indexed(
        IndexingRecord(
            image=_image(name),
            embedding=EmbeddingVector([0.1] * 8),
            file_size=1,
            file_modified_at=None,
            thumbnail_path=thumbnail_path,
        )
    )


def _backfill(
    repository: FakeImageRepository,
    generator: RecordingThumbnailGenerator | None = None,
    force: bool = False,
    dry_run: bool = False,
    metadata_prefetch_size: int = 512,
) -> tuple[BackfillThumbnailsUseCase, RecordingThumbnailGenerator]:
    generator = generator or RecordingThumbnailGenerator()
    use_case = BackfillThumbnailsUseCase(
        repository=repository,
        thumbnail_writer=ThumbnailWriter(generator, InMemoryThumbnailStore(), 512),
        metadata_prefetch_size=metadata_prefetch_size,
        force=force,
        dry_run=dry_run,
    )
    return use_case, generator


def test_rows_without_a_thumbnail_get_one_and_rows_with_one_are_left_alone() -> None:
    repository = FakeImageRepository()
    _indexed(repository, "old")
    _indexed(repository, "new", thumbnail_path="xx/new.jpg")
    use_case, generator = _backfill(repository)

    summary = use_case.execute([_candidate("old"), _candidate("new")])

    assert [image.filename for image, _ in generator.generated] == ["old"]
    assert summary.written == 1
    assert summary.already_generated == 1
    old = repository.get_index_metadata(_image("old").id)
    new = repository.get_index_metadata(_image("new").id)
    assert old is not None and old.thumbnail_generated
    assert new is not None and new.thumbnail_path == "xx/new.jpg"


def test_a_second_run_renders_nothing() -> None:
    """Idempotent by default, like `--only-unknown` for capture dates."""
    repository = FakeImageRepository()
    _indexed(repository, "photo")
    first, _ = _backfill(repository)
    first.execute([_candidate("photo")])

    second, generator = _backfill(repository)
    summary = second.execute([_candidate("photo")])

    assert generator.generated == []
    assert summary.written == 0
    assert summary.already_generated == 1


def test_force_renders_rows_that_already_have_one() -> None:
    repository = FakeImageRepository()
    _indexed(repository, "photo", thumbnail_path="xx/photo.jpg")
    use_case, generator = _backfill(repository, force=True)

    summary = use_case.execute([_candidate("photo")])

    assert len(generator.generated) == 1
    assert summary.written == 1
    stored = repository.get_index_metadata(_image("photo").id)
    assert stored is not None
    assert stored.thumbnail_path == f"memory/{_image('photo').id.value}.jpg"


def test_a_file_that_fails_to_render_under_force_keeps_its_old_thumbnail() -> None:
    repository = FakeImageRepository()
    _indexed(repository, "photo", thumbnail_path="xx/photo.jpg")
    use_case, _ = _backfill(
        repository,
        generator=RecordingThumbnailGenerator(failing=frozenset({"photo"})),
        force=True,
    )

    summary = use_case.execute([_candidate("photo")])

    (failure,) = summary.failures
    assert failure.path.endswith("photo.jpg")
    stored = repository.get_index_metadata(_image("photo").id)
    assert stored is not None
    assert stored.thumbnail_path == "xx/photo.jpg"


def test_a_file_with_no_row_is_counted_and_never_given_one() -> None:
    repository = FakeImageRepository()
    use_case, generator = _backfill(repository)

    summary = use_case.execute([_candidate("stranger")])

    assert summary.not_indexed == 1
    assert generator.generated == []
    assert repository.list() == []


def test_one_failure_does_not_stop_the_run() -> None:
    repository = FakeImageRepository()
    for name in ("a", "b", "c"):
        _indexed(repository, name)
    use_case, _ = _backfill(
        repository, generator=RecordingThumbnailGenerator(failing=frozenset({"b"}))
    )

    summary = use_case.execute([_candidate(name) for name in ("a", "b", "c")])

    assert summary.written == 2
    (failure,) = summary.failures
    assert failure.path.endswith("b.jpg")
    stored = repository.get_index_metadata(_image("b").id)
    assert stored is not None and stored.thumbnail_path is None


def test_a_dry_run_renders_and_writes_nothing() -> None:
    repository = FakeImageRepository()
    _indexed(repository, "photo")
    use_case, generator = _backfill(repository, dry_run=True)

    summary = use_case.execute([_candidate("photo")])

    assert summary.written == 1
    assert generator.generated == []
    assert repository.update_thumbnail_path_many_calls == []


def test_locations_are_written_once_per_window() -> None:
    repository = FakeImageRepository()
    names = [f"photo_{index}" for index in range(5)]
    for name in names:
        _indexed(repository, name)
    use_case, _ = _backfill(repository, metadata_prefetch_size=2)

    use_case.execute([_candidate(name) for name in names])

    assert [len(call) for call in repository.update_thumbnail_path_many_calls] == [
        2,
        2,
        1,
    ]
    assert repository.update_thumbnail_path_calls == []


def test_a_failed_bulk_write_degrades_to_per_row() -> None:
    class _RejectsOne(FakeImageRepository):
        def update_thumbnail_path_many(self, locations: Mapping[ImageId, str]) -> None:
            raise RuntimeError("deadlock detected")

        def update_thumbnail_path(self, image_id: ImageId, location: str) -> None:
            if image_id == _image("b").id:
                raise RuntimeError("row rejected")
            super().update_thumbnail_path(image_id, location)

    repository = _RejectsOne()
    for name in ("a", "b", "c"):
        _indexed(repository, name)
    use_case, _ = _backfill(repository)

    summary = use_case.execute([_candidate(name) for name in ("a", "b", "c")])

    assert summary.written == 2
    (failure,) = summary.failures
    assert failure.path.endswith("b.jpg")


def test_a_nonsensical_prefetch_size_is_rejected() -> None:
    with pytest.raises(ValueError, match="metadata_prefetch_size"):
        _backfill(FakeImageRepository(), metadata_prefetch_size=0)


def test_the_report_names_the_mode() -> None:
    use_case, _ = _backfill(FakeImageRepository(), force=True, dry_run=True)

    report = use_case.execute([]).format_report()

    assert "force, dry run" in report
    assert "Would render:" in report
