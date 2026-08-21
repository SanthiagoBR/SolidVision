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
from app.domain.value_objects.index_metadata import IndexMetadata
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


def _build_record(
    image: Image,
    embedding: EmbeddingVector | None = None,
    file_size: int = 1024,
    content_hash: str | None = None,
) -> IndexingRecord:
    return IndexingRecord(
        image=image,
        embedding=embedding or _build_embedding(),
        file_size=file_size,
        file_modified_at=datetime.datetime.now(datetime.UTC),
        content_hash=content_hash,
    )


class TestContentHashPersistence:
    """RFC-024 section 4 reaching the real column."""

    def test_save_indexed_writes_the_content_hash(self, db_session: Session) -> None:
        repository = PostgresImageRepository(db_session)
        image = _build_image()
        digest = "a" * 64

        repository.save_indexed(_build_record(image, content_hash=digest))

        row = db_session.get(ImageModel, image.id.value)
        assert row is not None
        assert row.content_hash == digest

    def test_get_index_metadata_reads_the_content_hash_back(
        self, db_session: Session
    ) -> None:
        repository = PostgresImageRepository(db_session)
        image = _build_image()
        digest = "b" * 64
        repository.save_indexed(_build_record(image, content_hash=digest))

        metadata = repository.get_index_metadata(image.id)

        assert metadata is not None
        assert metadata.content_hash == digest

    def test_a_row_written_without_a_hash_reads_back_as_none(
        self, db_session: Session
    ) -> None:
        """The pre-RFC-024 state, and the state `save()` still produces."""
        repository = PostgresImageRepository(db_session)
        image = _build_image()
        repository.save(image)

        metadata = repository.get_index_metadata(image.id)

        assert metadata is not None
        assert metadata.content_hash is None

    def test_the_hash_does_not_participate_in_identity(
        self, db_session: Session
    ) -> None:
        """RFC-022 7.1: byte-identical twins are still two distinct rows.

        Nothing in the schema stops two rows sharing a hash, and this pins
        that -- an accidental unique constraint would fail right here.
        """
        repository = PostgresImageRepository(db_session)
        first = _build_image()
        second = _build_image()
        digest = "c" * 64

        repository.save_indexed(_build_record(first, content_hash=digest))
        repository.save_indexed(_build_record(second, content_hash=digest))

        assert repository.exists(first.id)
        assert repository.exists(second.id)
        assert first.id != second.id


class TestBulkMetadataPrefetch:
    """RFC-024 section 7.1."""

    def test_it_returns_the_same_answers_as_the_per_id_reads(
        self, db_session: Session
    ) -> None:
        """The prefetch is only a safe substitute if it agrees exactly."""
        repository = PostgresImageRepository(db_session)
        images = [_build_image() for _ in range(4)]
        for index, image in enumerate(images):
            repository.save_indexed(
                _build_record(
                    image, file_size=100 + index, content_hash=f"{index:064d}"
                )
            )
        ids = [image.id for image in images]

        bulk = repository.get_index_metadata_many(ids)
        individually = {
            image_id: repository.get_index_metadata(image_id) for image_id in ids
        }

        assert bulk == individually

    def test_unknown_ids_are_absent_rather_than_mapped_to_none(
        self, db_session: Session
    ) -> None:
        """Absent means "new file"; a None value would be ambiguous."""
        repository = PostgresImageRepository(db_session)
        known = _build_image()
        repository.save_indexed(_build_record(known))
        unknown = ImageId(uuid.uuid4())

        result = repository.get_index_metadata_many([known.id, unknown])

        assert set(result) == {known.id}

    def test_an_empty_request_returns_an_empty_mapping(
        self, db_session: Session
    ) -> None:
        assert PostgresImageRepository(db_session).get_index_metadata_many([]) == {}

    def test_a_row_saved_without_metadata_comes_back_with_none_fields(
        self, db_session: Session
    ) -> None:
        """Present-but-empty must stay distinguishable from absent."""
        repository = PostgresImageRepository(db_session)
        image = _build_image()
        repository.save(image)

        result = repository.get_index_metadata_many([image.id])

        assert image.id in result
        assert result[image.id].file_size is None
        assert result[image.id].file_modified_at is None
        assert result[image.id].content_hash is None


class TestUpdateIndexMetadata:
    """RFC-024 section 4: refresh the bookkeeping, keep the embedding."""

    def test_it_updates_every_metadata_field(self, db_session: Session) -> None:
        repository = PostgresImageRepository(db_session)
        image = _build_image()
        repository.save_indexed(_build_record(image, content_hash="d" * 64))

        refreshed_at = datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=1)
        repository.update_index_metadata(
            image.id,
            IndexMetadata(
                file_size=4096,
                file_modified_at=refreshed_at,
                content_hash="e" * 64,
            ),
        )

        metadata = repository.get_index_metadata(image.id)
        assert metadata is not None
        assert metadata.file_size == 4096
        assert metadata.file_modified_at == refreshed_at
        assert metadata.content_hash == "e" * 64

    def test_it_leaves_the_embedding_untouched(self, db_session: Session) -> None:
        """The entire reason this method exists instead of `save_indexed()`."""
        repository = PostgresImageRepository(db_session)
        image = _build_image()
        repository.save_indexed(_build_record(image, embedding=_build_embedding(0.7)))

        repository.update_index_metadata(
            image.id,
            IndexMetadata(
                file_size=2048,
                file_modified_at=datetime.datetime.now(datetime.UTC),
                content_hash="f" * 64,
            ),
        )

        row = db_session.get(ImageModel, image.id.value)
        assert row is not None
        assert row.embedding is not None
        assert list(row.embedding) == pytest.approx([0.7] * 512)

    def test_it_leaves_the_domain_fields_untouched(self, db_session: Session) -> None:
        repository = PostgresImageRepository(db_session)
        image = _build_image()
        repository.save_indexed(_build_record(image))

        repository.update_index_metadata(
            image.id,
            IndexMetadata(file_size=1, file_modified_at=None, content_hash=None),
        )

        stored = repository.get(image.id)
        assert stored is not None
        assert stored.path == image.path
        assert stored.filename == image.filename
        assert stored.extension == image.extension

    def test_updating_a_missing_row_is_a_no_op(self, db_session: Session) -> None:
        """Creating a row here would mean inventing a row with no embedding."""
        repository = PostgresImageRepository(db_session)
        missing = ImageId(uuid.uuid4())

        repository.update_index_metadata(
            missing,
            IndexMetadata(file_size=1, file_modified_at=None, content_hash="0" * 64),
        )

        assert repository.get_index_metadata(missing) is None


class TestBulkUpsert:
    """RFC-024 section 7.2: one commit per batch, and what happens when it fails."""

    def test_every_record_in_the_batch_is_written(self, db_session: Session) -> None:
        repository = PostgresImageRepository(db_session)
        images = [_build_image() for _ in range(5)]

        repository.save_indexed_many(
            [
                _build_record(
                    image, file_size=100 + index, content_hash=f"{index:064d}"
                )
                for index, image in enumerate(images)
            ]
        )

        for index, image in enumerate(images):
            row = db_session.get(ImageModel, image.id.value)
            assert row is not None
            assert row.file_size == 100 + index
            assert row.content_hash == f"{index:064d}"
            assert row.embedding is not None

    def test_an_empty_batch_is_a_no_op(self, db_session: Session) -> None:
        PostgresImageRepository(db_session).save_indexed_many([])

    def test_it_upserts_rather_than_duplicating_existing_rows(
        self, db_session: Session
    ) -> None:
        repository = PostgresImageRepository(db_session)
        image = _build_image()
        repository.save_indexed(_build_record(image, file_size=1))

        repository.save_indexed_many(
            [_build_record(image, embedding=_build_embedding(0.4), file_size=2)]
        )

        assert len([row for row in repository.list() if row.id == image.id]) == 1
        row = db_session.get(ImageModel, image.id.value)
        assert row is not None
        assert row.file_size == 2
        assert row.embedding is not None
        assert list(row.embedding) == pytest.approx([0.4] * 512)

    def test_a_constraint_violation_discards_the_whole_batch(
        self, db_session: Session
    ) -> None:
        """All-or-nothing is the contract, and the reason a fallback exists.

        `file_size >= 0` is the CHECK constraint RFC-020 added; a negative
        value is the cheapest way to make PostgreSQL reject exactly one row
        of an otherwise valid batch.
        """
        repository = PostgresImageRepository(db_session)
        good_first, bad, good_last = _build_image(), _build_image(), _build_image()

        with pytest.raises(Exception):
            repository.save_indexed_many(
                [
                    _build_record(good_first),
                    _build_record(bad, file_size=-1),
                    _build_record(good_last),
                ]
            )

        assert not repository.exists(good_first.id)
        assert not repository.exists(bad.id)
        assert not repository.exists(good_last.id)

    def test_the_session_survives_a_failed_batch(self, db_session: Session) -> None:
        """The per-row fallback runs on this same session, so it must work.

        A repository that left the session in a failed transaction would
        turn one bad row into a whole run of failures -- the exact opposite
        of what the fallback is for.
        """
        repository = PostgresImageRepository(db_session)
        bad = _build_image()

        with pytest.raises(Exception):
            repository.save_indexed_many([_build_record(bad, file_size=-1)])

        recovered = _build_image()
        repository.save_indexed(_build_record(recovered))

        assert repository.exists(recovered.id)

    def test_the_per_row_fallback_saves_everything_except_the_bad_row(
        self, db_session: Session
    ) -> None:
        """End to end: the degradation RFC-024 section 7.2 requires."""
        repository = PostgresImageRepository(db_session)
        good_first, bad, good_last = _build_image(), _build_image(), _build_image()
        records = [
            _build_record(good_first),
            _build_record(bad, file_size=-1),
            _build_record(good_last),
        ]

        try:
            repository.save_indexed_many(records)
        except Exception:
            for record in records:
                try:
                    repository.save_indexed(record)
                except Exception:
                    continue

        assert repository.exists(good_first.id)
        assert repository.exists(good_last.id)
        assert not repository.exists(bad.id)


def test_a_rejected_row_does_not_poison_the_next_save(db_session: Session) -> None:
    """One bad row must cost one row, not the rest of the run.

    Without a rollback after a failed commit, SQLAlchemy leaves the session
    in a pending-rollback state and every later `save_indexed()` raises
    `PendingRollbackError` instead of doing its job -- so a single corrupt
    file would silently take every file after it down with it.
    """
    repository = PostgresImageRepository(db_session)

    with pytest.raises(Exception):
        repository.save_indexed(_build_record(_build_image(), file_size=-1))

    survivor = _build_image()
    repository.save_indexed(_build_record(survivor))

    assert repository.exists(survivor.id)
