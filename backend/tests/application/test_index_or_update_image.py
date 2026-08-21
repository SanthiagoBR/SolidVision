from __future__ import annotations

import datetime
import uuid

from app.application.use_cases.index_or_update_image import IndexOrUpdateImageUseCase
from app.domain.entities.image import Image
from app.domain.services.embedding_model_port import EmbeddingModelPort
from app.domain.value_objects.embedding_vector import EmbeddingVector
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.indexing_record import IndexingRecord
from app.infrastructure.ai.fake_embedding_model import FakeEmbeddingModel
from tests.application.fakes import FakeImageRepository, StubContentHasher


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


def _build_image() -> Image:
    return Image(
        id=ImageId(uuid.uuid4()),
        path=ImagePath(f"images/{uuid.uuid4().hex}.png"),
        filename="example",
        extension="png",
    )


def test_new_image_generates_embedding_and_is_persisted() -> None:
    repository = FakeImageRepository()
    embedding_model = _RecordingEmbeddingModel()
    use_case = IndexOrUpdateImageUseCase(
        repository=repository,
        embedding_model=embedding_model,
        content_hasher=StubContentHasher(),
    )
    image = _build_image()
    modified_at = datetime.datetime.now(datetime.UTC)

    was_indexed = use_case.execute(image, file_size=1024, file_modified_at=modified_at)

    assert was_indexed is True
    assert len(embedding_model.encode_image_calls) == 1
    assert len(repository.save_indexed_calls) == 1
    record = repository.save_indexed_calls[0]
    assert record.image == image
    assert record.file_size == 1024
    assert record.file_modified_at == modified_at
    assert record.embedding == embedding_model._delegate.encode_image(image)


def test_unchanged_image_does_not_regenerate_embedding_or_persist() -> None:
    repository = FakeImageRepository()
    embedding_model = _RecordingEmbeddingModel()
    use_case = IndexOrUpdateImageUseCase(
        repository=repository,
        embedding_model=embedding_model,
        content_hasher=StubContentHasher(),
    )
    image = _build_image()
    modified_at = datetime.datetime.now(datetime.UTC)
    use_case.execute(image, file_size=1024, file_modified_at=modified_at)
    assert len(embedding_model.encode_image_calls) == 1

    was_indexed = use_case.execute(image, file_size=1024, file_modified_at=modified_at)

    assert was_indexed is False
    assert len(embedding_model.encode_image_calls) == 1
    assert len(repository.save_indexed_calls) == 1


def test_changed_image_regenerates_embedding_and_updates() -> None:
    """Size, mtime, AND content all moved -- the one case that must re-embed.

    RFC-024 made "the metadata changed" insufficient on its own: the
    hasher below has to report different bytes too, or this becomes the
    hash-identical case tested further down.
    """
    repository = FakeImageRepository()
    embedding_model = _RecordingEmbeddingModel()
    content_hasher = StubContentHasher()
    use_case = IndexOrUpdateImageUseCase(
        repository=repository,
        embedding_model=embedding_model,
        content_hasher=content_hasher,
    )
    image = _build_image()
    first_modified_at = datetime.datetime.now(datetime.UTC)
    use_case.execute(image, file_size=1024, file_modified_at=first_modified_at)

    content_hasher.default = "b" * 64
    second_modified_at = first_modified_at + datetime.timedelta(seconds=1)
    use_case.execute(image, file_size=2048, file_modified_at=second_modified_at)

    assert len(embedding_model.encode_image_calls) == 2
    assert len(repository.save_indexed_calls) == 2
    latest = repository.save_indexed_calls[-1]
    assert latest.file_size == 2048
    assert latest.file_modified_at == second_modified_at
    assert latest.content_hash == "b" * 64


def test_metadata_present_but_none_pair_is_treated_as_changed() -> None:
    """Covers D3: a row written by `IndexImageUseCase.save()` has no metadata.

    `get_index_metadata()` returns `IndexMetadata(None, None)` for such a
    row, which must NOT be confused with "unchanged" -- the image must be
    indexed and its metadata backfilled.
    """
    repository = FakeImageRepository()
    embedding_model = _RecordingEmbeddingModel()
    image = _build_image()
    repository.save(image)  # RFC-015 path: no metadata recorded

    use_case = IndexOrUpdateImageUseCase(
        repository=repository,
        embedding_model=embedding_model,
        content_hasher=StubContentHasher(),
    )
    modified_at = datetime.datetime.now(datetime.UTC)
    use_case.execute(image, file_size=512, file_modified_at=modified_at)

    assert len(embedding_model.encode_image_calls) == 1
    assert len(repository.save_indexed_calls) == 1
    record = repository.save_indexed_calls[0]
    assert record.file_size == 512
    assert record.file_modified_at == modified_at


def test_execute_checks_metadata_before_generating_embedding() -> None:
    repository = FakeImageRepository()
    embedding_model = _RecordingEmbeddingModel()
    use_case = IndexOrUpdateImageUseCase(
        repository=repository,
        embedding_model=embedding_model,
        content_hasher=StubContentHasher(),
    )
    image = _build_image()
    modified_at = datetime.datetime.now(datetime.UTC)

    use_case.execute(image, file_size=1, file_modified_at=modified_at)

    assert repository.get_index_metadata_calls == [image.id]


def _make_use_case(
    repository: FakeImageRepository,
    embedding_model: EmbeddingModelPort,
    content_hasher: StubContentHasher,
) -> IndexOrUpdateImageUseCase:
    return IndexOrUpdateImageUseCase(
        repository=repository,
        embedding_model=embedding_model,
        content_hasher=content_hasher,
    )


def test_unchanged_metadata_never_reaches_the_hasher() -> None:
    """ARCHITECTURE.md section 16: hashing is the expensive step, so it is last.

    A file whose size and mtime both match is decided from the directory
    entry alone. Reading its bytes to reach the same conclusion would undo
    the entire point of the cost-ascending order.
    """
    repository = FakeImageRepository()
    content_hasher = StubContentHasher()
    use_case = _make_use_case(repository, _RecordingEmbeddingModel(), content_hasher)
    image = _build_image()
    modified_at = datetime.datetime.now(datetime.UTC)
    use_case.execute(image, file_size=1024, file_modified_at=modified_at)
    hashed_after_first_run = len(content_hasher.hashed)

    use_case.execute(image, file_size=1024, file_modified_at=modified_at)

    assert len(content_hasher.hashed) == hashed_after_first_run


def test_touched_file_with_identical_content_is_not_reembedded() -> None:
    """The saving RFC-024 section 4 exists for: an mtime touch is not a change."""
    repository = FakeImageRepository()
    embedding_model = _RecordingEmbeddingModel()
    use_case = _make_use_case(repository, embedding_model, StubContentHasher())
    image = _build_image()
    first_modified_at = datetime.datetime.now(datetime.UTC)
    use_case.execute(image, file_size=1024, file_modified_at=first_modified_at)

    touched_at = first_modified_at + datetime.timedelta(days=1)
    was_indexed = use_case.execute(image, file_size=1024, file_modified_at=touched_at)

    assert was_indexed is False
    assert len(embedding_model.encode_image_calls) == 1
    assert len(repository.save_indexed_calls) == 1


def test_touched_file_with_identical_content_still_records_the_new_metadata() -> None:
    """Skipping the embedding must not skip the bookkeeping.

    If the refreshed mtime were not written back, every subsequent run
    would re-hash the same file forever, having learned nothing.
    """
    repository = FakeImageRepository()
    use_case = _make_use_case(
        repository, _RecordingEmbeddingModel(), StubContentHasher()
    )
    image = _build_image()
    first_modified_at = datetime.datetime.now(datetime.UTC)
    use_case.execute(image, file_size=1024, file_modified_at=first_modified_at)

    touched_at = first_modified_at + datetime.timedelta(days=1)
    use_case.execute(image, file_size=1024, file_modified_at=touched_at)

    assert len(repository.update_index_metadata_calls) == 1
    updated_id, updated_metadata = repository.update_index_metadata_calls[0]
    assert updated_id == image.id
    assert updated_metadata.file_modified_at == touched_at
    assert updated_metadata.content_hash == "0" * 64

    stored = repository.get_index_metadata(image.id)
    assert stored is not None
    assert stored.file_modified_at == touched_at


def test_a_null_stored_hash_forces_reembedding() -> None:
    """Pre-RFC-024 rows carry NULL, which means unknown, never "matches".

    Assuming a match here would silently keep a stale embedding for a file
    whose content really did change while the column was still NULL.
    """
    repository = FakeImageRepository()
    embedding_model = _RecordingEmbeddingModel()
    image = _build_image()
    repository.save_indexed(
        IndexingRecord(
            image=image,
            embedding=EmbeddingVector([0.1] * 8),
            file_size=1024,
            file_modified_at=datetime.datetime.now(datetime.UTC),
            content_hash=None,
        )
    )
    stored = repository.get_index_metadata(image.id)
    assert stored is not None and stored.content_hash is None

    use_case = _make_use_case(repository, embedding_model, StubContentHasher())
    use_case.execute(
        image,
        file_size=2048,
        file_modified_at=datetime.datetime.now(datetime.UTC),
    )

    assert len(embedding_model.encode_image_calls) == 1
    assert repository.update_index_metadata_calls == []


def test_a_new_file_is_hashed_so_the_next_run_can_use_the_check() -> None:
    """A row persisted without a hash would leave the mechanism dormant."""
    repository = FakeImageRepository()
    content_hasher = StubContentHasher()
    use_case = _make_use_case(repository, _RecordingEmbeddingModel(), content_hasher)
    image = _build_image()

    use_case.execute(
        image,
        file_size=1024,
        file_modified_at=datetime.datetime.now(datetime.UTC),
    )

    assert content_hasher.hashed == [image]
    assert repository.save_indexed_calls[0].content_hash == "0" * 64
