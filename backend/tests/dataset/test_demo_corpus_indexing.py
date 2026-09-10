"""Run the real indexing pipeline against the materialized demo corpus.

Uses `PostgresImageRepository` and the `db_session` fixture (SAVEPOINT-
isolated, rolled back on teardown -- see `backend/tests/conftest.py`), so
these tests require a running database but never leave rows behind.

Assertions target specific manifest entries by their computed id rather
than the repository's total row count, since the shared development
database may hold unrelated rows committed by other work.
"""

from __future__ import annotations

import datetime
import os
from pathlib import Path

from sqlalchemy.orm import Session
from tests.conftest import TEST_DEVICE_ID, make_test_device

from app.application.use_cases.index_or_update_image import IndexOrUpdateImageUseCase
from app.application.use_cases.index_or_update_images import (
    IndexOrUpdateImagesUseCase,
)
from app.domain.entities.image import Image
from app.domain.value_objects.image_path import ImagePath
from app.infrastructure.ai.fake_embedding_model import FakeEmbeddingModel
from app.infrastructure.config.constants import SUPPORTED_IMAGE_EXTENSIONS
from app.infrastructure.filesystem.discovered_image_file import DiscoveredImageFile
from app.infrastructure.filesystem.filesystem_image_provider import (
    FilesystemImageProvider,
)
from app.infrastructure.filesystem.image_identity import compute_image_id
from app.infrastructure.filesystem.sha256_content_hasher import Sha256ContentHasher
from app.infrastructure.persistence.postgres_image_repository import (
    PostgresImageRepository,
)
from app.infrastructure.workers.indexing_worker import IndexingWorker
from dataset_tools.manifest import DEMO_MANIFEST_PATH, load_manifest

DEMO_MANIFEST = load_manifest(DEMO_MANIFEST_PATH)


def _to_image(discovered: DiscoveredImageFile, mount_point: Path) -> Image:
    """Build the domain entity the worker would build for a discovered file.

    `mount_point` is the corpus root, standing in for a whole volume:
    since RFC-027 an image is identified by its device and its path
    *within* that device, so the identity must not contain the temporary
    directory pytest happened to hand this run.
    """
    relative_path = ImagePath(discovered.path.relative_to(mount_point))
    return Image(
        id=compute_image_id(TEST_DEVICE_ID, relative_path),
        device_id=TEST_DEVICE_ID,
        relative_path=relative_path,
        absolute_path=ImagePath(str(discovered.path)),
        filename=discovered.filename,
        extension=discovered.extension,
    )


def test_worker_indexes_every_manifest_entry(
    demo_corpus: Path, db_session: Session
) -> None:
    """A first run indexes all 40 committed photos into PostgreSQL."""
    repository = PostgresImageRepository(db_session)
    worker = IndexingWorker(
        filesystem_provider=FilesystemImageProvider(
            demo_corpus, SUPPORTED_IMAGE_EXTENSIONS
        ),
        index_or_update_images_use_case=IndexOrUpdateImagesUseCase(
            repository=repository,
            embedding_model=FakeEmbeddingModel(),
            content_hasher=Sha256ContentHasher(),
            batch_size=8,
            metadata_prefetch_size=512,
        ),
        device=make_test_device(),
        mount_point=demo_corpus,
    )
    worker.run()

    for entry in DEMO_MANIFEST.images:
        expected_id = compute_image_id(TEST_DEVICE_ID, ImagePath(entry.relative_path))
        assert repository.exists(expected_id), entry.relative_path


def test_worker_persists_embeddings_of_the_configured_dimension(
    demo_corpus: Path, db_session: Session
) -> None:
    """A spot check that indexing actually reaches the vector column."""
    repository = PostgresImageRepository(db_session)
    worker = IndexingWorker(
        filesystem_provider=FilesystemImageProvider(
            demo_corpus, SUPPORTED_IMAGE_EXTENSIONS
        ),
        index_or_update_images_use_case=IndexOrUpdateImagesUseCase(
            repository=repository,
            embedding_model=FakeEmbeddingModel(),
            content_hasher=Sha256ContentHasher(),
            batch_size=8,
            metadata_prefetch_size=512,
        ),
        device=make_test_device(),
        mount_point=demo_corpus,
    )
    worker.run()

    sample_entry = DEMO_MANIFEST.images[0]
    sample_id = compute_image_id(TEST_DEVICE_ID, ImagePath(sample_entry.relative_path))
    metadata = repository.get_index_metadata(sample_id)
    assert metadata is not None
    assert metadata.file_size is not None
    assert metadata.file_modified_at == sample_entry.file_modified_at


def test_second_run_skips_every_unchanged_file(
    demo_corpus: Path, db_session: Session
) -> None:
    """Incremental indexing re-indexes nothing when the corpus is untouched.

    Calls `IndexOrUpdateImageUseCase.execute()` directly (mirroring the
    body of `IndexingWorker.run()`) to capture the per-file skip/index
    result, which `IndexingWorker.run()` itself only logs.
    """
    repository = PostgresImageRepository(db_session)
    use_case = IndexOrUpdateImageUseCase(
        repository=repository,
        embedding_model=FakeEmbeddingModel(),
        content_hasher=Sha256ContentHasher(),
    )
    provider = FilesystemImageProvider(demo_corpus, SUPPORTED_IMAGE_EXTENSIONS)

    def run_pass() -> list[bool]:
        return [
            use_case.execute(
                image=_to_image(discovered, demo_corpus),
                file_size=discovered.file_size,
                file_modified_at=discovered.file_modified_at,
            )
            for discovered in provider.discover()
        ]

    first_run = run_pass()
    second_run = run_pass()

    assert len(first_run) == len(DEMO_MANIFEST.images)
    assert all(first_run), "every manifest entry should index on a first run"
    assert not any(second_run), "an unchanged demo photo must not be re-indexed"


def test_touching_one_file_does_not_reindex_it_when_the_bytes_match(
    demo_corpus: Path, db_session: Session
) -> None:
    """RFC-024 section 4 changed this outcome deliberately.

    Before content hashing, moving a file's mtime was enough to force a
    full re-embed -- the behaviour a copy, a backup restore, or a
    `git checkout` triggers across a whole collection. Step 3 now reads
    the bytes, finds them identical, and keeps the existing embedding.
    Nothing else in the corpus is disturbed either way.
    """
    repository = PostgresImageRepository(db_session)
    use_case = IndexOrUpdateImageUseCase(
        repository=repository,
        embedding_model=FakeEmbeddingModel(),
        content_hasher=Sha256ContentHasher(),
    )
    provider = FilesystemImageProvider(demo_corpus, SUPPORTED_IMAGE_EXTENSIONS)

    def run_pass() -> dict[str, bool]:
        return {
            str(discovered.path): use_case.execute(
                image=_to_image(discovered, demo_corpus),
                file_size=discovered.file_size,
                file_modified_at=discovered.file_modified_at,
            )
            for discovered in provider.discover()
        }

    run_pass()

    touched_entry = DEMO_MANIFEST.images[0]
    touched_path = demo_corpus / touched_entry.relative_path
    new_timestamp = (
        touched_entry.file_modified_at + datetime.timedelta(days=1)
    ).timestamp()
    os.utime(touched_path, (new_timestamp, new_timestamp))

    second_pass = run_pass()

    assert not any(second_pass.values())

    # The refreshed mtime must still have been written back, or the next
    # run would re-hash the same file forever having learned nothing.
    stored = repository.get_index_metadata(
        compute_image_id(TEST_DEVICE_ID, ImagePath(touched_entry.relative_path))
    )
    assert stored is not None
    assert stored.file_modified_at == touched_entry.file_modified_at + (
        datetime.timedelta(days=1)
    )
    assert stored.content_hash is not None


def test_changing_one_file_reindexes_only_that_file(
    demo_corpus: Path, db_session: Session
) -> None:
    """A genuine content change is still a re-index, and still only one file."""
    repository = PostgresImageRepository(db_session)
    use_case = IndexOrUpdateImageUseCase(
        repository=repository,
        embedding_model=FakeEmbeddingModel(),
        content_hasher=Sha256ContentHasher(),
    )
    provider = FilesystemImageProvider(demo_corpus, SUPPORTED_IMAGE_EXTENSIONS)

    def run_pass() -> dict[str, bool]:
        return {
            str(discovered.path): use_case.execute(
                image=_to_image(discovered, demo_corpus),
                file_size=discovered.file_size,
                file_modified_at=discovered.file_modified_at,
            )
            for discovered in provider.discover()
        }

    run_pass()

    changed_entry = DEMO_MANIFEST.images[0]
    changed_path = demo_corpus / changed_entry.relative_path
    changed_path.write_bytes(changed_path.read_bytes() + b"appended-bytes")

    second_pass = run_pass()

    assert second_pass[str(changed_path)] is True
    untouched_results = {
        path: result
        for path, result in second_pass.items()
        if path != str(changed_path)
    }
    assert not any(untouched_results.values())
