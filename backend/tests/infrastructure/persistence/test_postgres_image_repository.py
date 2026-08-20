"""Integration tests for the PostgreSQL-backed image repository.

These tests exercise a real PostgreSQL connection through the `db_session`
fixture (see `tests/conftest.py`), which wraps every test in a SAVEPOINT
that is rolled back on teardown. No row written here is ever committed to
the development database.
"""

from __future__ import annotations

import datetime
import uuid

import pytest
from sqlalchemy.orm import Session

from app.domain.entities.image import Image
from app.domain.exceptions import ImageAlreadyExistsError
from app.domain.value_objects.embedding_vector import EmbeddingVector
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.indexing_record import IndexingRecord
from app.infrastructure.database.models.image_model import ImageModel
from app.infrastructure.persistence.postgres_image_repository import (
    PostgresImageRepository,
)


def _build_image(path: str | None = None) -> Image:
    unique = uuid.uuid4().hex
    return Image(
        id=ImageId(uuid.uuid4()),
        path=ImagePath(path or f"images/{unique}.png"),
        filename=unique,
        extension="png",
    )


def _build_embedding(seed: float = 0.1) -> EmbeddingVector:
    return EmbeddingVector([seed] * 512)


def test_save_persists_all_fields(db_session: Session) -> None:
    repository = PostgresImageRepository(db_session)
    image = _build_image()

    repository.save(image)

    row = db_session.get(ImageModel, image.id.value)
    assert row is not None
    assert row.id == image.id.value
    assert row.path == str(image.path)
    assert row.filename == image.filename
    assert row.extension == image.extension


def test_save_leaves_embedding_none(db_session: Session) -> None:
    repository = PostgresImageRepository(db_session)
    image = _build_image()

    repository.save(image)

    row = db_session.get(ImageModel, image.id.value)
    assert row is not None
    assert row.embedding is None


def test_save_leaves_incremental_metadata_none(db_session: Session) -> None:
    repository = PostgresImageRepository(db_session)
    image = _build_image()

    repository.save(image)

    row = db_session.get(ImageModel, image.id.value)
    assert row is not None
    assert row.file_size is None
    assert row.file_modified_at is None


def test_get_returns_domain_image_not_orm_model(db_session: Session) -> None:
    repository = PostgresImageRepository(db_session)
    image = _build_image()
    repository.save(image)

    result = repository.get(image.id)

    assert isinstance(result, Image)
    assert not isinstance(result, ImageModel)


def test_get_preserves_all_domain_fields(db_session: Session) -> None:
    repository = PostgresImageRepository(db_session)
    image = _build_image()
    repository.save(image)

    result = repository.get(image.id)

    assert result is not None
    assert result.id == image.id
    assert result.path == image.path
    assert result.filename == image.filename
    assert result.extension == image.extension


def test_get_result_is_independent_of_persistence_metadata(
    db_session: Session,
) -> None:
    repository = PostgresImageRepository(db_session)
    image = _build_image()
    repository.save(image)

    result = repository.get(image.id)

    assert result is not None
    assert not hasattr(result, "file_size")
    assert not hasattr(result, "file_modified_at")


def test_get_missing_image_returns_none(db_session: Session) -> None:
    repository = PostgresImageRepository(db_session)

    result = repository.get(ImageId(uuid.uuid4()))

    assert result is None


def test_exists_returns_false_for_missing_image(db_session: Session) -> None:
    repository = PostgresImageRepository(db_session)

    assert repository.exists(ImageId(uuid.uuid4())) is False


def test_exists_returns_true_for_saved_image(db_session: Session) -> None:
    repository = PostgresImageRepository(db_session)
    image = _build_image()
    repository.save(image)

    assert repository.exists(image.id) is True


def test_save_duplicate_path_raises_image_already_exists_error(
    db_session: Session,
) -> None:
    repository = PostgresImageRepository(db_session)
    shared_path = f"images/{uuid.uuid4().hex}.png"

    first = _build_image(path=shared_path)
    repository.save(first)

    second = _build_image(path=shared_path)
    with pytest.raises(ImageAlreadyExistsError):
        repository.save(second)


def test_repository_remains_usable_after_duplicate_path_error(
    db_session: Session,
) -> None:
    repository = PostgresImageRepository(db_session)
    shared_path = f"images/{uuid.uuid4().hex}.png"

    first = _build_image(path=shared_path)
    repository.save(first)

    second = _build_image(path=shared_path)
    with pytest.raises(ImageAlreadyExistsError):
        repository.save(second)

    third = _build_image()
    repository.save(third)

    assert repository.exists(third.id) is True


def test_delete_removes_image(db_session: Session) -> None:
    repository = PostgresImageRepository(db_session)
    image = _build_image()
    repository.save(image)

    repository.delete(image.id)

    assert repository.exists(image.id) is False
    assert repository.get(image.id) is None


def test_delete_nonexistent_image_is_noop(db_session: Session) -> None:
    repository = PostgresImageRepository(db_session)

    repository.delete(ImageId(uuid.uuid4()))


def test_list_includes_saved_images(db_session: Session) -> None:
    repository = PostgresImageRepository(db_session)
    before = {image.id for image in repository.list()}

    image = _build_image()
    repository.save(image)

    after = repository.list()
    after_ids = {result.id for result in after}

    assert after_ids - before == {image.id}
    saved = next(result for result in after if result.id == image.id)
    assert saved.path == image.path
    assert saved.filename == image.filename
    assert saved.extension == image.extension


def test_round_trip_preserves_each_field_individually(db_session: Session) -> None:
    repository = PostgresImageRepository(db_session)
    original = _build_image()

    repository.save(original)
    reconstructed = repository.get(original.id)

    assert reconstructed is not None
    assert reconstructed.id == original.id
    assert reconstructed.path == original.path
    assert reconstructed.filename == original.filename
    assert reconstructed.extension == original.extension


def test_get_index_metadata_returns_none_for_missing_image(
    db_session: Session,
) -> None:
    repository = PostgresImageRepository(db_session)

    assert repository.get_index_metadata(ImageId(uuid.uuid4())) is None


def test_get_index_metadata_returns_none_pair_for_image_saved_via_save(
    db_session: Session,
) -> None:
    repository = PostgresImageRepository(db_session)
    image = _build_image()
    repository.save(image)

    metadata = repository.get_index_metadata(image.id)

    assert metadata is not None
    assert metadata.file_size is None
    assert metadata.file_modified_at is None


def test_save_indexed_creates_new_row_with_embedding_and_metadata(
    db_session: Session,
) -> None:
    repository = PostgresImageRepository(db_session)
    image = _build_image()
    embedding = _build_embedding(0.5)
    modified_at = datetime.datetime.now(datetime.UTC)
    record = IndexingRecord(
        image=image,
        embedding=embedding,
        file_size=2048,
        file_modified_at=modified_at,
    )

    repository.save_indexed(record)

    row = db_session.get(ImageModel, image.id.value)
    assert row is not None
    assert row.embedding is not None
    assert list(row.embedding) == pytest.approx(list(embedding.values))
    assert row.file_size == 2048
    assert row.file_modified_at == modified_at


def test_save_indexed_result_is_readable_via_get(db_session: Session) -> None:
    repository = PostgresImageRepository(db_session)
    image = _build_image()
    record = IndexingRecord(
        image=image,
        embedding=_build_embedding(),
        file_size=1,
        file_modified_at=datetime.datetime.now(datetime.UTC),
    )

    repository.save_indexed(record)
    result = repository.get(image.id)

    assert result is not None
    assert result.id == image.id
    assert result.path == image.path
    assert result.filename == image.filename
    assert result.extension == image.extension
    assert not hasattr(result, "embedding")
    assert not hasattr(result, "file_size")


def test_save_indexed_updates_existing_row_without_duplicating(
    db_session: Session,
) -> None:
    repository = PostgresImageRepository(db_session)
    image = _build_image()
    first_modified_at = datetime.datetime.now(datetime.UTC)
    repository.save_indexed(
        IndexingRecord(
            image=image,
            embedding=_build_embedding(0.1),
            file_size=100,
            file_modified_at=first_modified_at,
        )
    )

    second_modified_at = first_modified_at + datetime.timedelta(seconds=5)
    repository.save_indexed(
        IndexingRecord(
            image=image,
            embedding=_build_embedding(0.9),
            file_size=200,
            file_modified_at=second_modified_at,
        )
    )

    matching_rows = [row for row in repository.list() if row.id == image.id]
    assert len(matching_rows) == 1

    row = db_session.get(ImageModel, image.id.value)
    assert row is not None
    assert row.file_size == 200
    assert row.file_modified_at == second_modified_at
    assert list(row.embedding) == pytest.approx([0.9] * 512)


def test_save_indexed_does_not_raise_already_exists_for_updates(
    db_session: Session,
) -> None:
    repository = PostgresImageRepository(db_session)
    image = _build_image()
    repository.save_indexed(
        IndexingRecord(
            image=image,
            embedding=_build_embedding(),
            file_size=1,
            file_modified_at=datetime.datetime.now(datetime.UTC),
        )
    )

    repository.save_indexed(
        IndexingRecord(
            image=image,
            embedding=_build_embedding(),
            file_size=2,
            file_modified_at=datetime.datetime.now(datetime.UTC),
        )
    )
