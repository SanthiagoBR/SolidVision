"""Indexing never writes into the collection, thumbnails included (RFC-030 section 7.1).

RFC-028 section 10 declared that the system never writes to a user's
collection; RFC-030 is the first RFC that writes files at all, so this is
where the rule stops being true by default and has to be checked.

The run goes through `JobRunner` -- the path a real job takes -- with the
real Pillow generator and the real on-disk store, over a stand-in disk of
real JPEGs. The disk is compared before and after file by file: names,
sizes and modification times, not just a count.
"""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest
from PIL import Image as PILImage
from tests.application.fakes import (
    FakeDeviceLocator,
    FakeDeviceRepository,
    FakeImageRepository,
    StubContentHasher,
    make_device,
)

from app.application.use_cases.index_or_update_images import IndexOrUpdateImagesUseCase
from app.application.use_cases.thumbnail_writer import ThumbnailWriter
from app.domain.entities.indexing_job import IndexingJob, JobStatus
from app.domain.value_objects.job_id import JobId
from app.infrastructure.ai.fake_embedding_model import FakeEmbeddingModel
from app.infrastructure.config.settings import settings
from app.infrastructure.filesystem.thumbnail_generator import (
    PillowThumbnailGenerator,
)
from app.infrastructure.filesystem.thumbnail_store import FilesystemThumbnailStore
from app.infrastructure.persistence.in_memory_indexing_job_repository import (
    InMemoryIndexingJobRepository,
)
from app.infrastructure.workers.job_runner import JobRunner, build_runner

Snapshot = dict[str, tuple[int, int]]


def snapshot(root: Path) -> Snapshot:
    """Every file under `root`, with its size and modification time."""
    return {
        path.relative_to(root).as_posix(): (
            path.stat().st_size,
            path.stat().st_mtime_ns,
        )
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


@pytest.fixture()
def disk(tmp_path: Path) -> Path:
    root = tmp_path / "disk"
    for folder in ("2018/junho", "2019"):
        (root / folder).mkdir(parents=True)
        for index in range(3):
            PILImage.new("RGB", (900, 600), (index * 60, 90, 120)).save(
                root / folder / f"DJI_{index:04d}.JPG"
            )
    return root


def run_one_job(disk: Path, cache: Path) -> FakeImageRepository:
    device = make_device(label="HD3")
    jobs = InMemoryIndexingJobRepository()
    images = FakeImageRepository()
    runner = JobRunner(
        jobs=jobs,
        devices=FakeDeviceRepository([device]),
        locator=FakeDeviceLocator({device.id: disk}),
        indexer=IndexOrUpdateImagesUseCase(
            repository=images,
            embedding_model=FakeEmbeddingModel(),
            content_hasher=StubContentHasher(),
            batch_size=4,
            metadata_prefetch_size=8,
            thumbnail_writer=ThumbnailWriter(
                PillowThumbnailGenerator(), FilesystemThumbnailStore(cache), 128
            ),
        ),
        supported_extensions=settings.supported_extensions,
        extract_capture_date=False,
        excluded_directories=(cache,),
    )
    job = jobs.create(
        IndexingJob(
            id=JobId.new(),
            device_id=device.id,
            created_at=datetime.datetime.now(tz=datetime.UTC),
        )
    )
    finished = runner.run_job(job.id)
    assert finished is not None
    assert finished.status is JobStatus.COMPLETED
    return images


def test_indexing_leaves_every_file_on_the_disk_exactly_as_it_was(
    disk: Path, tmp_path: Path
) -> None:
    cache = tmp_path / "app-data" / "thumbnails"
    before = snapshot(disk)

    images = run_one_job(disk, cache)

    assert snapshot(disk) == before
    assert len(images.list()) == 6
    assert len([path for path in cache.rglob("*.jpg")]) == 6


def test_every_indexed_image_points_at_a_thumbnail_in_the_cache(
    disk: Path, tmp_path: Path
) -> None:
    cache = tmp_path / "app-data" / "thumbnails"

    images = run_one_job(disk, cache)

    store = FilesystemThumbnailStore(cache)
    for image in images.list():
        metadata = images.get_index_metadata(image.id)
        assert metadata is not None and metadata.thumbnail_path is not None
        located = store.locate(metadata.thumbnail_path)
        assert located is not None
        assert located.is_relative_to(cache.resolve())
        assert not located.is_relative_to(disk.resolve())


def test_a_cache_inside_the_disk_is_never_indexed_as_photos(disk: Path) -> None:
    """The whole-system-disk case: the app's data directory lives on the disk.

    Two runs, each against an empty repository, because the failure is a
    loop that only shows on the second one: without the exclusion it would
    discover the six thumbnails the first run wrote, index them as photos,
    and report twelve images.
    """
    cache = disk / "Users" / "someone" / "AppData" / "Local" / "SolidVision"
    run_one_job(disk, cache)
    assert len(list(cache.rglob("*.jpg"))) == 6

    images = run_one_job(disk, cache)

    indexed = sorted(str(image.relative_path) for image in images.list())
    assert len(indexed) == 6
    assert not any(path.startswith("Users/") for path in indexed)
    assert len(list(cache.rglob("*.jpg"))) == 6


def test_the_production_runner_renders_thumbnails_and_excludes_the_cache() -> None:
    """`build_runner()` is the one composition real jobs use; it must wire both.

    The sessions are never touched: the repositories only store them, and
    the CLIP adapter defers loading its checkpoint until the first encode.
    """
    runner = build_runner(job_session=object(), image_session=object())  # type: ignore[arg-type]

    assert runner._excluded_directories == (settings.thumbnail_directory,)
    writer = runner._indexer._thumbnail_writer
    assert writer is not None
    assert isinstance(writer._generator, PillowThumbnailGenerator)
    assert isinstance(writer._store, FilesystemThumbnailStore)
    assert writer._store.directory == settings.thumbnail_directory.resolve()
    assert writer._max_edge == settings.thumbnail_max_edge
