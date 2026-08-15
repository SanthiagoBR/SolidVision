from __future__ import annotations

import datetime
import uuid

from app.application.use_cases.index_or_update_image import IndexOrUpdateImageUseCase
from app.domain.entities.image import Image
from app.domain.services.embedding_model_port import EmbeddingModelPort
from app.domain.value_objects.embedding_vector import EmbeddingVector
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.infrastructure.ai.fake_embedding_model import FakeEmbeddingModel
from tests.application.fakes import FakeImageRepository


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
        repository=repository, embedding_model=embedding_model
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
        repository=repository, embedding_model=embedding_model
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
    repository = FakeImageRepository()
    embedding_model = _RecordingEmbeddingModel()
    use_case = IndexOrUpdateImageUseCase(
        repository=repository, embedding_model=embedding_model
    )
    image = _build_image()
    first_modified_at = datetime.datetime.now(datetime.UTC)
    use_case.execute(image, file_size=1024, file_modified_at=first_modified_at)

    second_modified_at = first_modified_at + datetime.timedelta(seconds=1)
    use_case.execute(image, file_size=2048, file_modified_at=second_modified_at)

    assert len(embedding_model.encode_image_calls) == 2
    assert len(repository.save_indexed_calls) == 2
    latest = repository.save_indexed_calls[-1]
    assert latest.file_size == 2048
    assert latest.file_modified_at == second_modified_at


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
        repository=repository, embedding_model=embedding_model
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
        repository=repository, embedding_model=embedding_model
    )
    image = _build_image()
    modified_at = datetime.datetime.now(datetime.UTC)

    use_case.execute(image, file_size=1, file_modified_at=modified_at)

    assert repository.get_index_metadata_calls == [image.id]
