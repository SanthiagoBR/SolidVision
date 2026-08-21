from __future__ import annotations

import datetime
import inspect
import logging
from collections.abc import Sequence
from pathlib import Path

import pytest
from tests.application.fakes import FakeImageRepository, RecordingContentHasher

from app.application.use_cases.index_or_update_images import IndexOrUpdateImagesUseCase
from app.domain.entities.image import Image
from app.domain.services.embedding_model_port import EmbeddingModelPort
from app.domain.value_objects.embedding_vector import EmbeddingVector
from app.domain.value_objects.image_path import ImagePath
from app.infrastructure.ai.fake_embedding_model import FakeEmbeddingModel
from app.infrastructure.filesystem.filesystem_image_provider import (
    FilesystemImageProvider,
)
from app.infrastructure.filesystem.image_identity import compute_image_id
from app.infrastructure.workers.indexing_worker import (
    IndexingWorker,
    _build_arg_parser,
    main,
)

SUPPORTED_EXTENSIONS = (".jpg", ".jpeg", ".png", ".tiff", ".bmp", ".webp")


class _RecordingEmbeddingModel(EmbeddingModelPort):
    """Wraps `FakeEmbeddingModel` to record how it was invoked.

    Records single and batched calls separately, because RFC-024's whole
    claim is about which of the two the pipeline actually reaches for.
    Deliberately does NOT override `encode_images`: inheriting the port's
    default loop is what a batching-unaware implementation does, and the
    pipeline has to stay correct against exactly that.
    """

    def __init__(self) -> None:
        self._delegate = FakeEmbeddingModel()
        self.encode_image_calls: list[Image] = []
        self.batch_sizes: list[int] = []

    def encode_image(self, image: Image) -> EmbeddingVector:
        self.encode_image_calls.append(image)
        return self._delegate.encode_image(image)

    def encode_text(self, text: str) -> EmbeddingVector:
        return self._delegate.encode_text(text)

    def encode_images(self, images: Sequence[Image]) -> list[EmbeddingVector]:
        self.batch_sizes.append(len(images))
        return super().encode_images(images)


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
    batch_size: int = 8,
) -> IndexingWorker:
    provider = FilesystemImageProvider(root, SUPPORTED_EXTENSIONS)
    use_case = IndexOrUpdateImagesUseCase(
        repository=repository,
        embedding_model=embedding_model,
        content_hasher=RecordingContentHasher(),
        batch_size=batch_size,
        metadata_prefetch_size=512,
    )
    return IndexingWorker(
        filesystem_provider=provider, index_or_update_images_use_case=use_case
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


def test_the_content_hash_reaches_persistence(tmp_path: Path) -> None:
    """RFC-024 section 4: the row must carry the hash, or step 3 stays dormant."""
    import hashlib

    payload = b"0123456789"
    (tmp_path / "photo.png").write_bytes(payload)

    repository = FakeImageRepository()
    _make_worker(tmp_path, repository, _RecordingEmbeddingModel()).run()

    assert (
        repository.save_indexed_calls[0].content_hash
        == hashlib.sha256(payload).hexdigest()
    )


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


def test_the_run_summary_is_returned_and_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """RFC-024 section 11: counters are data first, log output second."""
    (tmp_path / "a.png").write_bytes(b"one")
    (tmp_path / "b.png").write_bytes(b"two")

    repository = FakeImageRepository()
    worker = _make_worker(tmp_path, repository, _RecordingEmbeddingModel())

    with caplog.at_level(
        logging.INFO, logger="app.infrastructure.workers.indexing_worker"
    ):
        summary = worker.run()

    assert summary.discovered == 2
    assert summary.indexed == 2
    assert summary.failed == 0
    assert any("Indexing finished" in message for message in caplog.messages)


def test_worker_does_not_construct_infrastructure_dependencies_internally() -> None:
    source = inspect.getsource(IndexingWorker)

    assert "Session" not in source
    assert "PostgresImageRepository" not in source
    assert "FakeEmbeddingModel" not in source
    assert "Engine" not in source
    assert "Vector" not in source


class TestBatchAssembly:
    """RFC-024 sections 5 and 8: what actually reaches the model, and when."""

    def test_candidates_are_encoded_as_batches_not_one_at_a_time(
        self, tmp_path: Path
    ) -> None:
        """Five files at batch size 2 become 2 + 2 + a remainder of 1.

        The remainder does not appear in `batch_sizes` because a group of
        one is routed straight to `encode_image()`: a batch of one has no
        per-call overhead to amortize, and going through the batch path
        first would only run the same call twice whenever it fails.
        `inference_batches` still counts all three.
        """
        for index in range(5):
            (tmp_path / f"photo_{index}.png").write_bytes(f"data-{index}".encode())

        embedding_model = _RecordingEmbeddingModel()
        summary = _make_worker(
            tmp_path, FakeImageRepository(), embedding_model, batch_size=2
        ).run()

        assert embedding_model.batch_sizes == [2, 2]
        assert summary.inference_batches == 3
        assert summary.indexed == 5

    def test_a_second_run_over_an_unchanged_corpus_invokes_the_model_zero_times(
        self, tmp_path: Path
    ) -> None:
        """The skip decision runs before batches exist, so none get formed."""
        for index in range(5):
            (tmp_path / f"photo_{index}.png").write_bytes(f"data-{index}".encode())

        repository = FakeImageRepository()
        embedding_model = _RecordingEmbeddingModel()
        worker = _make_worker(tmp_path, repository, embedding_model, batch_size=2)
        worker.run()

        embedding_model.batch_sizes.clear()
        embedding_model.encode_image_calls.clear()
        summary = worker.run()

        assert embedding_model.batch_sizes == []
        assert embedding_model.encode_image_calls == []
        assert summary.skipped_unchanged == 5
        assert summary.inference_batches == 0

    def test_batches_are_never_padded_with_already_indexed_images(
        self, tmp_path: Path
    ) -> None:
        """Only the survivors of the skip decision may enter a batch."""
        for index in range(6):
            (tmp_path / f"photo_{index}.png").write_bytes(f"data-{index}".encode())

        repository = FakeImageRepository()
        embedding_model = _RecordingEmbeddingModel()
        worker = _make_worker(tmp_path, repository, embedding_model, batch_size=4)
        worker.run()

        (tmp_path / "photo_2.png").write_bytes(b"genuinely different content")
        embedding_model.batch_sizes.clear()
        embedding_model.encode_image_calls.clear()
        summary = worker.run()

        # One survivor, so one group of one -- not a group of four topped
        # up with the five files that were already indexed.
        assert summary.inference_batches == 1
        assert len(embedding_model.encode_image_calls) == 1
        assert embedding_model.encode_image_calls[0].filename == "photo_2"
        assert summary.indexed == 1
        assert summary.skipped_unchanged == 5


class TestErrorIsolationUnderBatching:
    """RFC-024 section 6: batching must not cost per-file isolation."""

    def test_one_bad_file_in_a_batch_still_indexes_the_others(
        self, tmp_path: Path
    ) -> None:
        for name in ("a", "b", "c", "d"):
            (tmp_path / f"{name}.png").write_bytes(f"data-{name}".encode())

        repository = FakeImageRepository()
        summary = _make_worker(
            tmp_path, repository, _FailsForFilename("c"), batch_size=4
        ).run()

        indexed = {record.image.filename for record in repository.save_indexed_calls}
        assert indexed == {"a", "b", "d"}
        assert summary.indexed == 3
        assert summary.failed == 1

    def test_the_failure_names_the_real_path_and_the_real_exception(
        self, tmp_path: Path
    ) -> None:
        """Not a generic wrapper -- the operator has to know what to fix."""
        for name in ("a", "b", "c"):
            (tmp_path / f"{name}.png").write_bytes(f"data-{name}".encode())

        summary = _make_worker(
            tmp_path, FakeImageRepository(), _FailsForFilename("b"), batch_size=3
        ).run()

        (failure,) = summary.failures
        assert failure.path.endswith("b.png")
        assert isinstance(failure.error, RuntimeError)
        assert str(failure.error) == "boom"

    def test_the_degraded_batch_is_reported_rather_than_hidden(
        self, tmp_path: Path
    ) -> None:
        for name in ("a", "b", "c"):
            (tmp_path / f"{name}.png").write_bytes(f"data-{name}".encode())

        summary = _make_worker(
            tmp_path, FakeImageRepository(), _FailsForFilename("b"), batch_size=3
        ).run()

        (fallback,) = summary.inference_fallbacks
        assert len(fallback.paths) == 3
        assert isinstance(fallback.error, RuntimeError)


class TestCommandLineEntryPoint:
    """RFC-024 section 12."""

    def test_the_root_argument_is_required(self) -> None:
        """No default, so the command can never wander into real photos."""
        with pytest.raises(SystemExit):
            _build_arg_parser().parse_args([])

    def test_an_explicit_root_is_accepted(self, tmp_path: Path) -> None:
        args = _build_arg_parser().parse_args(["--root", str(tmp_path)])

        assert args.root == tmp_path

    def test_no_default_points_at_the_configured_indexing_root(self) -> None:
        """`seed_demo.py` documents why this default would be dangerous."""
        (root_action,) = [
            action for action in _build_arg_parser()._actions if action.dest == "root"
        ]

        assert root_action.default is None
        assert root_action.required is True

    def test_main_composes_the_real_pipeline_against_the_given_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The entry point is a composition root, and this pins what it wires.

        The session and the embedding model are the two collaborators that
        would otherwise open a real connection and load a real checkpoint,
        so both are replaced; everything else is the production wiring.
        """
        from app.infrastructure.filesystem.sha256_content_hasher import (
            Sha256ContentHasher,
        )

        (tmp_path / "photo.png").write_bytes(b"data")
        repository = FakeImageRepository()
        embedding_model = _RecordingEmbeddingModel()
        closed: list[bool] = []

        class _Session:
            def close(self) -> None:
                closed.append(True)

        constructed: list[IndexingWorker] = []
        real_worker_init = IndexingWorker.__init__

        def capturing_init(
            self: IndexingWorker,
            filesystem_provider: FilesystemImageProvider,
            index_or_update_images_use_case: IndexOrUpdateImagesUseCase,
        ) -> None:
            real_worker_init(self, filesystem_provider, index_or_update_images_use_case)
            constructed.append(self)

        monkeypatch.setattr(
            "app.infrastructure.persistence.session.SessionLocal", lambda: _Session()
        )
        monkeypatch.setattr(
            "app.presentation.dependencies.get_embedding_model",
            lambda: embedding_model,
        )
        monkeypatch.setattr(
            "app.infrastructure.persistence.postgres_image_repository."
            "PostgresImageRepository",
            lambda session: repository,
        )
        monkeypatch.setattr(IndexingWorker, "__init__", capturing_init)
        monkeypatch.setattr("sys.argv", ["indexing_worker", "--root", str(tmp_path)])

        main()

        (worker,) = constructed
        use_case = worker._index_or_update_images_use_case
        assert use_case._repository is repository
        assert use_case._embedding_model is embedding_model
        assert isinstance(use_case._content_hasher, Sha256ContentHasher)
        assert len(repository.save_indexed_calls) == 1
        assert closed == [True]
