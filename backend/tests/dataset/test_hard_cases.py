"""Pin the indexing pipeline's behavior on structural edge cases (RFC-022 6.2).

Runs the real production path -- `FilesystemImageProvider` -> `IndexingWorker`
-> `IndexOrUpdateImagesUseCase` -> repository -- against generated files, using
the in-memory repository and the fake embedding model so no PostgreSQL and no
AI library is required.
"""

from __future__ import annotations

from pathlib import Path

import pytest
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
from app.infrastructure.persistence.in_memory_image_repository import (
    InMemoryImageRepository,
)
from app.infrastructure.workers.indexing_worker import IndexingWorker
from dataset_tools.generators.hard_cases import (
    HARD_CASES,
    IGNORED,
    INDEXED,
    HardCase,
    generate,
)


@pytest.fixture()
def hard_cases_root(tmp_path: Path) -> Path:
    """Materialize the hard-case corpus into an isolated directory."""
    root = tmp_path / "hard_cases"
    generate(root)
    return root


def _run_worker(root: Path, batch_size: int = 8) -> InMemoryImageRepository:
    repository = InMemoryImageRepository()
    worker = IndexingWorker(
        filesystem_provider=FilesystemImageProvider(root, SUPPORTED_IMAGE_EXTENSIONS),
        index_or_update_images_use_case=IndexOrUpdateImagesUseCase(
            repository=repository,
            embedding_model=FakeEmbeddingModel(),
            content_hasher=Sha256ContentHasher(),
            batch_size=batch_size,
            metadata_prefetch_size=512,
        ),
        device=make_test_device(),
        mount_point=root,
    )
    worker.run()
    return repository


def _indexed_paths(repository: InMemoryImageRepository) -> set[str]:
    return {str(image.relative_path) for image in repository.list()}


def test_generator_writes_exactly_the_declared_cases(hard_cases_root: Path) -> None:
    """The generated tree and the declared case table must not drift apart."""
    on_disk = {
        path.relative_to(hard_cases_root).as_posix()
        for path in hard_cases_root.rglob("*")
        if path.is_file()
    }
    declared = {case.relative_path for case in HARD_CASES}
    assert on_disk == declared


@pytest.mark.parametrize(
    "case",
    [case for case in HARD_CASES if case.expect == INDEXED],
    ids=lambda case: case.relative_path,
)
def test_expected_files_are_indexed(case: HardCase, hard_cases_root: Path) -> None:
    """Every case declared `indexed` produces a row on a first run."""
    repository = _run_worker(hard_cases_root)
    expected = case.relative_path
    assert expected in _indexed_paths(repository), case.description


@pytest.mark.parametrize(
    "case",
    [case for case in HARD_CASES if case.expect == IGNORED],
    ids=lambda case: case.relative_path,
)
def test_unsupported_extensions_never_reach_the_repository(
    case: HardCase, hard_cases_root: Path
) -> None:
    """Cases declared `ignored` are filtered at discovery, not at indexing."""
    repository = _run_worker(hard_cases_root)
    rejected = case.relative_path
    assert rejected not in _indexed_paths(repository), case.description


def test_one_broken_file_does_not_abort_the_run(hard_cases_root: Path) -> None:
    """A corrupt file must not prevent unrelated files from being indexed."""
    repository = _run_worker(hard_cases_root)
    expected_indexed = sum(1 for case in HARD_CASES if case.expect == INDEXED)
    assert len(repository.list()) == expected_indexed


def test_identical_content_at_two_paths_produces_two_rows(
    hard_cases_root: Path,
) -> None:
    """Identity is path-derived, so byte-identical twins are distinct images.

    Pins the deliberate behavior documented in `image_identity.py` and
    RFC-022 7.1, so that changing the id scheme fails loudly here.
    """
    repository = _run_worker(hard_cases_root)
    twins = {
        str(image.id)
        for image in repository.list()
        if image.filename.startswith("duplicate_content_")
    }
    assert len(twins) == 2


def test_uppercase_extension_is_persisted_lowercased(hard_cases_root: Path) -> None:
    """`.JPG` on disk must normalize to `jpg` on the entity."""
    repository = _run_worker(hard_cases_root)
    uppercase = next(
        image for image in repository.list() if image.filename == "uppercase_extension"
    )
    assert uppercase.extension == "jpg"


def test_accented_filename_round_trips(hard_cases_root: Path) -> None:
    """A non-ASCII filename survives discovery and `ImagePath` normalization."""
    repository = _run_worker(hard_cases_root)
    assert any(image.filename == "fazenda São João" for image in repository.list())


def test_second_run_skips_every_unchanged_file(hard_cases_root: Path) -> None:
    """Incremental indexing re-indexes nothing when the filesystem is untouched.

    Asserted once here rather than per case: `skipped` is a property of a
    second run over unchanged files, uniform across everything that indexed
    on the first (see the outcome vocabulary in `hard_cases.py`).
    """
    repository = InMemoryImageRepository()
    use_case = IndexOrUpdateImageUseCase(
        repository=repository,
        embedding_model=FakeEmbeddingModel(),
        content_hasher=Sha256ContentHasher(),
    )
    provider = FilesystemImageProvider(hard_cases_root, SUPPORTED_IMAGE_EXTENSIONS)

    def run_pass() -> list[bool]:
        return [
            use_case.execute(
                image=_to_image(discovered, hard_cases_root),
                file_size=discovered.file_size,
                file_modified_at=discovered.file_modified_at,
            )
            for discovered in provider.discover()
        ]

    first_run = run_pass()
    second_run = run_pass()

    assert all(first_run), "every discovered file should index on a first run"
    assert not any(second_run), "an unchanged file must not be re-indexed"


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
