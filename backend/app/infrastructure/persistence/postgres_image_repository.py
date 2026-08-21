"""PostgreSQL-backed implementation of the image repository port."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.domain.entities.image import Image
from app.domain.exceptions import ImageAlreadyExistsError
from app.domain.repositories.image_repository import ImageRepository
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.index_metadata import IndexMetadata
from app.domain.value_objects.indexing_record import IndexingRecord
from app.infrastructure.database.models.image_model import ImageModel


class PostgresImageRepository(ImageRepository):
    """Repository implementation backed by PostgreSQL via SQLAlchemy."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def save(self, image: Image) -> None:
        """Persist an image, translating path collisions into a domain error."""
        model = ImageModel.from_domain(image)
        self._session.add(model)
        try:
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            raise ImageAlreadyExistsError() from exc

    def get(self, image_id: ImageId) -> Image | None:
        model = self._session.get(ImageModel, image_id.value)
        return model.to_domain() if model is not None else None

    def exists(self, image_id: ImageId) -> bool:
        statement = select(ImageModel.id).where(ImageModel.id == image_id.value)
        return self._session.execute(statement).scalar_one_or_none() is not None

    def delete(self, image_id: ImageId) -> None:
        model = self._session.get(ImageModel, image_id.value)
        if model is not None:
            self._session.delete(model)
            self._session.commit()

    def list(self) -> list[Image]:
        statement = select(ImageModel)
        models = self._session.execute(statement).scalars().all()
        return [model.to_domain() for model in models]

    def save_indexed(self, record: IndexingRecord) -> None:
        """Create or update an image row together with its embedding and metadata.

        Unlike `save()`, this is a genuine upsert and does not translate
        `IntegrityError` into `ImageAlreadyExistsError` -- a constraint
        violation here (e.g. the `file_size` CHECK) is a real error, not a
        duplicate-create signal.

        The rollback on failure is load-bearing, not defensive tidiness.
        Every caller of this method indexes many files in sequence and
        isolates failures per file, so the session has to survive a
        rejected row: without the rollback, SQLAlchemy leaves the
        transaction in a pending-rollback state and the *next* file fails
        with `PendingRollbackError` instead of succeeding, turning one bad
        row into a cascade. RFC-024's per-row fallback (section 7.2) walks
        exactly that path on the same session.
        """
        try:
            self._stage_indexed(record)
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise

    def save_indexed_many(self, records: Sequence[IndexingRecord]) -> None:
        """Upsert many rows as one read, one flush, and one commit.

        The commit is the cheap part, which is the opposite of what RFC-024
        assumed going in: profiling a real run showed a single commit of 45
        rows costing 2.5 ms, against ~48 ms per row for everything else.
        The cost was the *read* half -- see the comment below -- so this
        method collapses the reads rather than just sharing a commit.

        On failure the session is rolled back before the error propagates,
        so the caller's per-row retry starts from a clean session rather
        than one poisoned by a half-applied batch.
        """
        if not records:
            return

        try:
            # Two details make this a bulk write rather than a loop that
            # merely shares a commit, and both were found by profiling
            # (RFC-024 section 7.2):
            #
            # `session.get()` per record would issue one SELECT per row --
            # and, worse, each of those SELECTs autoflushes the rows staged
            # so far, so the ORM emits an INSERT round trip per record
            # anyway. One `IN (...)` query up front removes both.
            #
            # `no_autoflush` then keeps the staging loop from flushing
            # partway through for any other reason, so the batch reaches
            # the database as one flush followed by one commit.
            with self._session.no_autoflush:
                staged = self._existing_models(
                    [record.image.id.value for record in records]
                )
                for record in records:
                    staged[record.image.id.value] = self._stage_indexed(
                        record, staged.get(record.image.id.value)
                    )
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise

    def _existing_models(self, ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, ImageModel]:
        """Load whichever of `ids` already have rows, in one query."""
        statement = select(ImageModel).where(ImageModel.id.in_(ids))
        return {
            model.id: model
            for model in self._session.execute(statement).scalars().all()
        }

    def _stage_indexed(
        self, record: IndexingRecord, model: ImageModel | None = None
    ) -> ImageModel:
        """Apply one record to the session without committing it.

        `model` is the already-loaded row when the caller has one; passing
        `None` means "look it up", which is what the single-record path
        does.
        """
        if model is None:
            model = self._session.get(ImageModel, record.image.id.value)

        if model is None:
            model = ImageModel.from_domain(record.image)
            self._session.add(model)
        else:
            model.path = str(record.image.path)
            model.filename = record.image.filename
            model.extension = record.image.extension

        model.embedding = list(record.embedding.values)
        model.file_size = record.file_size
        model.file_modified_at = record.file_modified_at
        model.content_hash = record.content_hash
        return model

    def get_index_metadata(self, image_id: ImageId) -> IndexMetadata | None:
        model = self._session.get(ImageModel, image_id.value)
        if model is None:
            return None
        return IndexMetadata(
            file_size=model.file_size,
            file_modified_at=model.file_modified_at,
            content_hash=model.content_hash,
        )

    def get_index_metadata_many(
        self, image_ids: Sequence[ImageId]
    ) -> dict[ImageId, IndexMetadata]:
        """Read the metadata for many ids in one round trip.

        Selects the four metadata columns by name rather than loading whole
        `ImageModel` rows. That is not premature tidiness: `embedding` is a
        512-float vector, so hydrating full rows would drag roughly two
        kilobytes per image across the wire to answer a question decided by
        two scalars -- on the very path that exists to make re-scanning a
        large unchanged collection cheap.
        """
        if not image_ids:
            return {}

        statement = select(
            ImageModel.id,
            ImageModel.file_size,
            ImageModel.file_modified_at,
            ImageModel.content_hash,
        ).where(ImageModel.id.in_([image_id.value for image_id in image_ids]))

        return {
            ImageId(row.id): IndexMetadata(
                file_size=row.file_size,
                file_modified_at=row.file_modified_at,
                content_hash=row.content_hash,
            )
            for row in self._session.execute(statement)
        }

    def update_index_metadata(self, image_id: ImageId, metadata: IndexMetadata) -> None:
        """Refresh one row's filesystem metadata, leaving its embedding alone.

        A targeted UPDATE rather than a read-modify-write, because the whole
        point of this path is that no embedding needs to be produced or
        moved; loading the row would fetch the 512-float vector this method
        exists to avoid touching.
        """
        statement = (
            update(ImageModel)
            .where(ImageModel.id == image_id.value)
            .values(
                file_size=metadata.file_size,
                file_modified_at=metadata.file_modified_at,
                content_hash=metadata.content_hash,
            )
        )
        self._session.execute(statement)
        self._session.commit()
