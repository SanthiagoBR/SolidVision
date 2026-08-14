from __future__ import annotations

import datetime
import inspect
import logging
from pathlib import Path

import pytest
from tests.application.fakes import FakeImageRepository

from app.application.use_cases.index_or_update_image import IndexOrUpdateImageUseCase
from app.domain.entities.image import Image
from app.domain.services.embedding_model_port import EmbeddingModelPort
from app.domain.value_objects.embedding_vector import EmbeddingVector
from app.domain.value_objects.image_path import ImagePath
from app.infrastructure.ai.fake_embedding_model import FakeEmbeddingModel
from app.infrastructure.filesystem.filesystem_image_provider import (
    FilesystemImageProvider,
)
from app.infrastructure.filesystem.image_identity import compute_image_id
from app.infrastructure.workers.indexing_worker import IndexingWorker

SUPPORTED_EXTENSIONS = (".jpg", ".jpeg", ".png", ".tiff", ".bmp", ".webp")


class _RecordingEmbeddingModel(EmbeddingModelPort):
    """Wraps `FakeEmbeddingModel` to record how many times it was invoked."""

    def __init__(self) -> None:
        self._delegate = FakeEmbeddingModel()
        self.encode_image_calls: list[Image] = []

    def encode_image(self, image: Image) -> EmbeddingVector:
        self.encode_image_calls.append(image)
        return self._delegate.encode_image(image)

    def encode_text(self, text: str) -> EmbeddingVector:
        return self._delegate.encode_text(text)


class _FailsForFilename(EmbeddingModelPort):
    """Raises when encoding a specific filename, otherwise delegates."""

    def __init__(self, failing_filename: str) -> None:
        self._delegate = FakeEmbeddingModel()
        self._failing_filename = failing_filename

    def encode_image(self, image: Image) -> EmbeddingVector:
        if image.filename == self._failing_filename:
            raise RuntimeError("boom")
        return self._delegate.encode_image(image)

    def encode_text(self, text: str) -> EmbeddingVector:
        return self._delegate.encode_text(text)


def _make_worker(
    root: Path,
    repository: FakeImageRepository,
    embedding_model: EmbeddingModelPort,
) -> IndexingWorker:
    provider = FilesystemImageProvider(root, SUPPORTED_EXTENSIONS)
    use_case = IndexOrUpdateImageUseCase(
        repository=repository, embedding_model=embedding_model
    )
    return IndexingWorker(
        filesystem_provider=provider, index_or_update_use_case=use_case
    )


def test_discovers_supported_images_and_ignores_unsupported(tmp_path: Path) -> None:
    (tmp_path / "photo.png").write_bytes(b"data")
    (tmp_path / "notes.txt").write_bytes(b"data")

    repository = FakeImageRepository()
    worker = _make_worker(tmp_path, repository, _RecordingEmbeddingModel())

    worker.run()

    assert len(repository.save_indexed_calls) == 1
    assert repository.save_indexed_calls[0].image.filename == "photo"


def test_extension_matching_is_case_insensitive(tmp_path: Path) -> None:
    (tmp_path / "one.JPG").write_bytes(b"data")

    repository = FakeImageRepository()
    worker = _make_worker(tmp_path, repository, _RecordingEmbeddingModel())

    worker.run()

    assert len(repository.save_indexed_calls) == 1


def test_discovers_nested_directories(tmp_path: Path) -> None:
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    (nested / "deep.png").write_bytes(b"data")

    repository = FakeImageRepository()
    worker = _make_worker(tmp_path, repository, _RecordingEmbeddingModel())

    worker.run()

    assert len(repository.save_indexed_calls) == 1
    assert repository.save_indexed_calls[0].image.filename == "deep"


def test_image_construction_derives_expected_fields(tmp_path: Path) -> None:
    (tmp_path / "photo.png").write_bytes(b"data")

    repository = FakeImageRepository()
    worker = _make_worker(tmp_path, repository, _RecordingEmbeddingModel())

    worker.run()

    record = repository.save_indexed_calls[0]
    expected_path = ImagePath(str(tmp_path / "photo.png"))
    assert record.image.path == expected_path
    assert record.image.filename == "photo"
    assert record.image.extension == "png"
    assert record.image.id == compute_image_id(expected_path)


def test_same_path_produces_same_id_across_multiple_runs(tmp_path: Path) -> None:
    (tmp_path / "photo.png").write_bytes(b"data")

    first_repository = FakeImageRepository()
    _make_worker(tmp_path, first_repository, _RecordingEmbeddingModel()).run()
    first_id = first_repository.save_indexed_calls[0].image.id

    second_repository = FakeImageRepository()
    _make_worker(tmp_path, second_repository, _RecordingEmbeddingModel()).run()
    second_id = second_repository.save_indexed_calls[0].image.id

    assert first_id == second_id


def test_metadata_is_collected_and_propagated(tmp_path: Path) -> None:
    (tmp_path / "photo.png").write_bytes(b"0123456789")

    repository = FakeImageRepository()
    worker = _make_worker(tmp_path, repository, _RecordingEmbeddingModel())

    worker.run()

    record = repository.save_indexed_calls[0]
    assert record.file_size == 10
    assert isinstance(record.file_modified_at, datetime.datetime)


def test_new_image_generates_embedding_and_persists(tmp_path: Path) -> None:
    (tmp_path / "photo.png").write_bytes(b"data")

    repository = FakeImageRepository()
    embedding_model = _RecordingEmbeddingModel()
    worker = _make_worker(tmp_path, repository, embedding_model)

    worker.run()

    assert len(embedding_model.encode_image_calls) == 1
    assert len(repository.save_indexed_calls) == 1


def test_unchanged_image_is_not_reembedded_on_second_run(tmp_path: Path) -> None:
    (tmp_path / "photo.png").write_bytes(b"data")

    repository = FakeImageRepository()
    embedding_model = _RecordingEmbeddingModel()
    worker = _make_worker(tmp_path, repository, embedding_model)
    worker.run()
    assert len(embedding_model.encode_image_calls) == 1

    worker.run()

    assert len(embedding_model.encode_image_calls) == 1
    assert len(repository.save_indexed_calls) == 1


def test_changed_image_is_reindexed(tmp_path: Path) -> None:
    file_path = tmp_path / "photo.png"
    file_path.write_bytes(b"data")

    repository = FakeImageRepository()
    embedding_model = _RecordingEmbeddingModel()
    worker = _make_worker(tmp_path, repository, embedding_model)
    worker.run()

    file_path.write_bytes(b"much larger data than before")
    worker.run()

    assert len(embedding_model.encode_image_calls) == 2
    assert len(repository.save_indexed_calls) == 2


def test_empty_directory_results_in_no_operations(tmp_path: Path) -> None:
    repository = FakeImageRepository()
    worker = _make_worker(tmp_path, repository, _RecordingEmbeddingModel())

    worker.run()

    assert repository.save_indexed_calls == []


def test_failure_on_one_file_does_not_stop_others(tmp_path: Path) -> None:
    (tmp_path / "a.png").write_bytes(b"data")
    (tmp_path / "b.png").write_bytes(b"data")
    (tmp_path / "c.png").write_bytes(b"data")

    repository = FakeImageRepository()
    worker = _make_worker(tmp_path, repository, _FailsForFilename("b"))

    worker.run()

    indexed_filenames = {
        record.image.filename for record in repository.save_indexed_calls
    }
    assert indexed_filenames == {"a", "c"}


def test_failure_is_logged_with_the_affected_path(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (tmp_path / "bad.png").write_bytes(b"data")

    repository = FakeImageRepository()
    worker = _make_worker(tmp_path, repository, _FailsForFilename("bad"))

    with caplog.at_level(
        logging.ERROR, logger="app.infrastructure.workers.indexing_worker"
    ):
        worker.run()

    assert any("bad.png" in message for message in caplog.messages)
    assert repository.save_indexed_calls == []


def test_worker_does_not_construct_infrastructure_dependencies_internally() -> None:
    source = inspect.getsource(IndexingWorker)

    assert "Session" not in source
    assert "PostgresImageRepository" not in source
    assert "FakeEmbeddingModel" not in source
    assert "Engine" not in source
    assert "Vector" not in source
