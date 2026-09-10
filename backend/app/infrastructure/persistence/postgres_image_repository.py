"""PostgreSQL-backed implementation of the image repository port."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import Select, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.domain.entities.image import Image
from app.domain.exceptions import (
    EmbeddingDimensionMismatchError,
    ImageAlreadyExistsError,
)
from app.domain.repositories.image_repository import ImageRepository
from app.domain.value_objects.device_id import DeviceId
from app.domain.value_objects.embedding_vector import EmbeddingVector
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.index_metadata import IndexMetadata
from app.domain.value_objects.indexing_record import IndexingRecord
from app.domain.value_objects.search_filters import SearchFilters
from app.domain.value_objects.search_hit import SearchHit, SearchHits
from app.infrastructure.database.models.image_model import (
    EMBEDDING_DIMENSION,
    ImageModel,
)


class PostgresImageRepository(ImageRepository):
    """Repository implementation backed by PostgreSQL via SQLAlchemy."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def save(self, image: Image) -> None:
        """Persist an image, translating location collisions into a domain error.

        The collision it names is now `(device_id, relative_path)` rather
        than the old absolute `path`, which is the constraint that finally
        means what this method always claimed: two rows for the same file
        on the same disk are rejected, including when the disk mounted
        under a different letter the second time (RFC-027 section 2.1).
        """
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
            model.device_id = record.image.device_id.value
            model.relative_path = str(record.image.relative_path)
            model.filename = record.image.filename
            model.extension = record.image.extension

        model.embedding = list(record.embedding.values)
        model.file_size = record.file_size
        model.file_modified_at = record.file_modified_at
        model.content_hash = record.content_hash
        return model

    def search_similar(
        self,
        embedding: EmbeddingVector,
        limit: int,
        filters: SearchFilters | None = None,
    ) -> SearchHits:
        """Rank the indexed images against `embedding` inside PostgreSQL.

        The ranking is the database's job and stays there. Reading the
        vectors back to sort them in Python would move 100,000 x 512
        floats across the wire to answer a question the server can answer
        with the HNSW index the RFC-023 migration already built -- roughly
        200 MB per search, to return ten rows.

        Three details are load-bearing:

        `<=>` (`cosine_distance`) is the operator the index was created
        for (`vector_cosine_ops`). Ordering by anything else -- L2, inner
        product, or a Python-side expression -- silently gives up the
        index and changes the ranking.

        The distance-to-similarity conversion happens here, at the edge.
        pgvector returns cosine *distance* in [0, 2]; `1 - distance` is
        cosine similarity in [-1, 1]. It is not clamped or rescaled: a
        negative score means the vectors genuinely oppose each other, and
        nothing above this layer should have to know that a distance was
        ever involved.

        The id is the tie-break. Without it, equally distant rows come
        back in whatever order the plan produces, which is not stable
        across plans or across the seq-scan/index-scan boundary, and
        `limit` would then cut an arbitrary one of them.

        Only the columns the domain entity needs are selected. The
        `embedding` column is deliberately not among them: the caller
        cannot use it, and hydrating full rows would drag a 512-float
        vector back per hit for nothing. `absolute_path` is left unset on
        every hit, because the database does not know where a device is
        mounted and must not pretend to (RFC-027 section 7).

        **The device filter is a `WHERE`, and what PostgreSQL does with it
        is a measurement rather than a deduction.** The planner is free to
        abandon the HNSW index and scan the subset sequentially, which is
        both fast and exact for a selective filter; or to keep the index
        and apply the predicate to what the traversal returned, which is
        nearly free for an unselective one. The middle is the risk: an
        index scan explores at most `hnsw.ef_search` candidates, so
        post-filtering can discard enough of them to return **fewer than
        `limit`** rows while more matching rows exist. Known mitigations,
        none chosen here: `hnsw.iterative_scan`, raising `ef_search` when
        a filter is present, or partial HNSW indexes per device.
        `experiments/rfc-027-devices/planner_check.py` measures the three
        regimes with `EXPLAIN ANALYZE`.

        **The result is best-effort once the planner uses the index.**
        HNSW is an approximate index: a scan explores at most
        `hnsw.ef_search` (40 by default) candidates, and any of those that
        are dead-but-not-yet-vacuumed tuples are spent from that budget
        and then filtered out. So an index scan can return *fewer* hits
        than `limit` while more matching rows exist, and its top-K is not
        guaranteed to be the true top-K. Measured, not theorized: with a
        deliberately bloated index the shipped query returned 1 of 3 live
        rows (RFC-025 section 7.3). At the sizes the planner answers with
        a sequential scan the ranking is exact. Tuning `ef_search` is out
        of scope here and belongs with the scale benchmark.
        """
        self._require_indexed_dimension(embedding)

        distance = ImageModel.embedding.cosine_distance(list(embedding.values)).label(
            "distance"
        )
        statement = (
            select(
                ImageModel.id,
                ImageModel.device_id,
                ImageModel.relative_path,
                ImageModel.filename,
                ImageModel.extension,
                distance,
            )
            .where(ImageModel.embedding.is_not(None))
            .order_by(distance, ImageModel.id)
            .limit(limit)
        )
        statement = self._apply_filters(statement, filters)

        return [
            SearchHit(
                image=Image(
                    id=ImageId(row.id),
                    device_id=DeviceId(row.device_id),
                    relative_path=ImagePath(row.relative_path),
                    filename=row.filename,
                    extension=row.extension,
                ),
                similarity=1.0 - row.distance,
            )
            for row in self._session.execute(statement)
        ]

    @staticmethod
    def _apply_filters(
        statement: Select[Any], filters: SearchFilters | None
    ) -> Select[Any]:
        """Add the narrowing clauses, or none at all.

        An absent or empty `SearchFilters` adds nothing, so the emitted
        SQL is character-for-character the query RFC-025 shipped and its
        contract tests keep describing the unfiltered behaviour. The
        alternative -- always emitting `device_id IN (...)` and letting an
        empty list mean "everything" -- is the bug this branch exists to
        avoid: an empty `IN ()` matches no rows, so a defaulted argument
        would silently return nothing.

        RFC-028 adds its date range here, beside this clause and not
        instead of it.
        """
        if filters is None or filters.is_empty():
            return statement
        return statement.where(
            ImageModel.device_id.in_(
                [device_id.value for device_id in filters.device_ids]
            )
        )

    @staticmethod
    def _require_indexed_dimension(embedding: EmbeddingVector) -> None:
        """Fail a wrong-width query vector before it reaches the server.

        PostgreSQL rejects it too, but not reliably and not legibly: the
        `vector(512)` column only complains once a row is actually
        compared, so the same bad call raises against a populated table
        and returns `[]` against an empty one. Checking up front makes the
        failure identical everywhere, and identical to the in-memory
        implementations, which is the point of the shared contract test.
        """
        if len(embedding.values) != EMBEDDING_DIMENSION:
            raise EmbeddingDimensionMismatchError(
                f"Query embedding has {len(embedding.values)} dimensions, but "
                f"the images.embedding column holds {EMBEDDING_DIMENSION}."
            )

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
