"""PostgreSQL-backed implementation of the image repository port."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Any, cast

from sqlalchemy import (
    ColumnElement,
    Double,
    Integer,
    Numeric,
    Select,
    Table,
    bindparam,
    case,
    func,
    literal,
    select,
    update,
)
from sqlalchemy import cast as sql_cast
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.domain.entities.image import Image
from app.domain.exceptions import (
    EmbeddingDimensionMismatchError,
    ImageAlreadyExistsError,
)
from app.domain.repositories.image_repository import ImageRepository
from app.domain.services.haversine import EARTH_RADIUS_M
from app.domain.value_objects.capture_date import CaptureDate
from app.domain.value_objects.capture_source import CaptureSource
from app.domain.value_objects.device_id import DeviceId
from app.domain.value_objects.embedding_vector import EmbeddingVector
from app.domain.value_objects.geo_circle import GeoCircle
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.index_metadata import IndexMetadata
from app.domain.value_objects.indexing_record import IndexingRecord
from app.domain.value_objects.job_scope import JobScope
from app.domain.value_objects.position import (
    BoundingBox,
    PositionCell,
    PositionCells,
    PositionReading,
)
from app.domain.value_objects.position_source import PositionSource
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
            model.captured_at = record.image.captured_at
            model.capture_source = _source_value(record.image.capture_source)
            model.latitude = record.image.latitude
            model.longitude = record.image.longitude
            model.position_source = _position_value(record.image.position_source)

        model.embedding = list(record.embedding.values)
        model.file_size = record.file_size
        model.file_modified_at = record.file_modified_at
        model.content_hash = record.content_hash
        model.thumbnail_path = record.thumbnail_path
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

        **A capture-date range is the harder filter of the two** (RFC-028
        section 8.1). Its selectivity is set by whatever interval the user
        asks for rather than known in advance, and there is no finite set
        of ranges to build one partial index per, so the mitigation that
        suits devices does not exist for dates.
        `experiments/rfc-028-capture-date/planner_check.py` measures it.

        **A circle is the worst of the three for the approximate index**
        (RFC-032 section 6.1): high cardinality like a date, and *clustered*
        -- in the pilot, 5 cells of ~100 m held 45% of the positions -- so a
        few kilometres around a much-photographed place is not selective at
        all, and lands in exactly the band RFC-028 measured returning 5 to
        9 rows of 10. `experiments/rfc-032-geolocation/planner_check.py`
        measures it on a clustered corpus, and RFC-032 section 6.1 decided
        the mitigation against a criterion stated before the number: the
        cheapest configuration returning `limit` of `limit` everywhere with
        a filtered p95 no worse than twice the unfiltered one.

        **No configuration met it, so none is applied here** -- the RFC's
        own rule for that outcome. Measured on 20,000 images: every
        mitigation (`hnsw.iterative_scan = strict_order`, `ef_search` 100 to
        400) made every query full, where the baseline came back short in
        13 of 20 queries at 20% selectivity; but the latency half failed for
        every configuration, the baseline included, because the planner
        answers low selectivities with *exact* sequential plans (23-65 ms
        server-side at 5-10%, and up to ~340 ms under a forced generic plan)
        against a 3.0 ms unfiltered p95. Those plans are untouched by any
        HNSW setting. The verdict and the numbers are in RFC-032 section
        6.1; adopting `strict_order` anyway would be a change to the
        criterion, which is a decision for the RFC, not for this method.

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
        statement = self.search_statement(embedding, limit, filters)

        return [
            SearchHit(
                image=Image(
                    id=ImageId(row.id),
                    device_id=DeviceId(row.device_id),
                    relative_path=ImagePath(row.relative_path),
                    filename=row.filename,
                    extension=row.extension,
                    captured_at=row.captured_at,
                    capture_source=_source_member(row.capture_source),
                    latitude=row.latitude,
                    longitude=row.longitude,
                    position_source=_position_member(row.position_source),
                ),
                similarity=1.0 - row.distance,
            )
            for row in self._session.execute(statement)
        ]

    @classmethod
    def search_statement(
        cls,
        embedding: EmbeddingVector,
        limit: int,
        filters: SearchFilters | None = None,
    ) -> Select[Any]:
        """The exact `SELECT` `search_similar()` runs, without running it.

        Public, and a `classmethod`, for one reader besides
        `search_similar()`: `experiments/rfc-032-geolocation/planner_check.py`,
        which has to `EXPLAIN` the query the application sends -- box
        pre-filter, haversine and all -- rather than a hand-copied imitation
        that would drift from it the first time either changed.
        """
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
                ImageModel.captured_at,
                ImageModel.capture_source,
                ImageModel.latitude,
                ImageModel.longitude,
                ImageModel.position_source,
                distance,
            )
            .where(ImageModel.embedding.is_not(None))
            .order_by(distance, ImageModel.id)
            .limit(limit)
        )
        return cls._apply_filters(statement, filters)

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

        Each field adds its own clause, or none, independently of the
        other. The same reasoning applies to the date range: an absent one
        is not `[datetime.min, datetime.max)`, which would exclude every
        image with a NULL `captured_at` and change the unfiltered query.

        The range is `captured_at >= start AND captured_at < end`, and the
        NULL rule of RFC-020 needs no clause of its own here: a comparison
        with NULL is NULL, which `WHERE` treats as false, so an image with
        an unknown date never matches. The in-memory implementations have
        no such free lunch and must say `is not None` explicitly.

        A circle (RFC-032) adds its clauses the same way, and the same NULL
        rule makes an unknown position fail them for free; see
        `_circle_clauses()`.
        """
        if filters is None or filters.is_empty():
            return statement
        if filters.device_ids:
            statement = statement.where(_device_clause(filters))
        if filters.captured_between is not None:
            statement = statement.where(*_date_clauses(filters))
        if filters.taken_within is not None:
            statement = statement.where(*_circle_clauses(filters.taken_within))
        return statement

    def count_unknown_capture_date(self, filters: SearchFilters) -> int:
        """Count, in one `COUNT(*)`, what a date range hid for having no date.

        Mirrors `search_similar()`'s `WHERE` minus the ranking and minus
        the date clause itself -- `embedding IS NOT NULL`, the device set
        if any, the circle if any (RFC-032), and `captured_at IS NULL` --
        so "the photos the filter hid" and "the photos the search could
        have shown" are drawn from the same definition of searchable.

        No query at all without a date range, so an unfiltered search
        never pays for a second round trip (RFC-028 section 8).
        """
        if filters.captured_between is None:
            return 0

        statement = (
            select(func.count())
            .select_from(ImageModel)
            .where(
                ImageModel.embedding.is_not(None),
                ImageModel.captured_at.is_(None),
            )
        )
        if filters.device_ids:
            statement = statement.where(_device_clause(filters))
        if filters.taken_within is not None:
            statement = statement.where(*_circle_clauses(filters.taken_within))
        return int(self._session.execute(statement).scalar_one())

    def count_unknown_position(self, filters: SearchFilters) -> int:
        """Count, in one `COUNT(*)`, the searchable images that have no position.

        `search_similar()`'s `WHERE` minus the ranking and minus the circle
        -- `embedding IS NOT NULL`, the device set, the date range, and
        `latitude IS NULL` (RFC-032 section 6.2). `latitude` alone is
        enough: `ck_images_position_pairing` guarantees `longitude` is NULL
        with it.

        Queries whether or not `filters` has a circle, because the map asks
        this exact question with none; not asking it for a search without a
        circle is `SearchImagesUseCase`'s rule.
        """
        statement = (
            select(func.count())
            .select_from(ImageModel)
            .where(
                ImageModel.embedding.is_not(None),
                ImageModel.latitude.is_(None),
            )
        )
        if filters.device_ids:
            statement = statement.where(_device_clause(filters))
        if filters.captured_between is not None:
            statement = statement.where(*_date_clauses(filters))
        return int(self._session.execute(statement).scalar_one())

    def aggregate_positions(
        self,
        filters: SearchFilters,
        area: BoundingBox,
        precision: int,
        cell_limit: int | None,
    ) -> PositionCells:
        """One `GROUP BY` over rounded coordinates (RFC-032 section 7).

        `round(latitude::numeric, p)`: PostgreSQL has no
        `round(double precision, int)`, and the cast is also what fixes the
        rounding rule -- the double's 15 significant digits, rounded half
        away from zero in decimal -- that `round_to_cell()` reproduces for
        the in-memory implementations.

        **The precision is rendered into the SQL, not bound.** It appears
        in the select list and in the `GROUP BY`, and with server-side
        binding two separate parameters would be two different expressions
        to PostgreSQL, which then refuses the select list as "not in GROUP
        BY". `literal_execute` inlines the integer at execution time; it is
        an `int` by type, so nothing a client sends reaches the text.

        The area is two `BETWEEN`s on the plain columns -- the same shape as
        a circle's pre-filter -- and the device and date clauses are the
        search's own, so the map's universe cannot drift from the search's.
        `filters.taken_within` is not applied (see the port).

        Whether an index serves this over a large table is measured, not
        assumed: `experiments/rfc-032-geolocation/planner_check.py`.
        """
        statement = self.aggregate_statement(filters, area, precision, cell_limit)
        return [
            PositionCell(
                latitude=float(row.cell_latitude),
                longitude=float(row.cell_longitude),
                count=int(row.photos),
            )
            for row in self._session.execute(statement)
        ]

    @staticmethod
    def aggregate_statement(
        filters: SearchFilters,
        area: BoundingBox,
        precision: int,
        cell_limit: int | None,
    ) -> Select[Any]:
        """The exact `SELECT` `aggregate_positions()` runs, for the same reader.

        Public for the reason `search_statement()` is: the measurement has
        to `EXPLAIN` what the endpoint sends.
        """
        digits = literal(int(precision), Integer, literal_execute=True)
        cell_latitude = func.round(sql_cast(ImageModel.latitude, Numeric), digits)
        cell_longitude = func.round(sql_cast(ImageModel.longitude, Numeric), digits)
        statement = (
            select(
                cell_latitude.label("cell_latitude"),
                cell_longitude.label("cell_longitude"),
                func.count().label("photos"),
            )
            .where(
                ImageModel.embedding.is_not(None),
                ImageModel.latitude.between(area.min_latitude, area.max_latitude),
                ImageModel.longitude.between(area.min_longitude, area.max_longitude),
            )
            .group_by(cell_latitude, cell_longitude)
            .order_by(cell_latitude, cell_longitude)
        )
        if filters.device_ids:
            statement = statement.where(_device_clause(filters))
        if filters.captured_between is not None:
            statement = statement.where(*_date_clauses(filters))
        if cell_limit is not None:
            statement = statement.limit(cell_limit)
        return statement

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
            capture_source=_source_member(model.capture_source),
            position_source=_position_member(model.position_source),
            thumbnail_path=model.thumbnail_path,
        )

    def get_index_metadata_many(
        self, image_ids: Sequence[ImageId]
    ) -> dict[ImageId, IndexMetadata]:
        """Read the metadata for many ids in one round trip.

        Selects the metadata columns by name rather than loading whole
        `ImageModel` rows. That is not premature tidiness: `embedding` is a
        512-float vector, so hydrating full rows would drag roughly two
        kilobytes per image across the wire to answer a question decided by
        two scalars -- on the very path that exists to make re-scanning a
        large unchanged collection cheap.

        `capture_source` is one of them since RFC-028, for the conditional
        capture-date write, and costs one short string per row in a query
        that was running anyway. `thumbnail_path` joined it in RFC-030 for
        the thumbnail backfill, on the same terms, and `position_source` in
        RFC-032 for the conditional position write.
        """
        if not image_ids:
            return {}

        statement = select(
            ImageModel.id,
            ImageModel.file_size,
            ImageModel.file_modified_at,
            ImageModel.content_hash,
            ImageModel.capture_source,
            ImageModel.position_source,
            ImageModel.thumbnail_path,
        ).where(ImageModel.id.in_([image_id.value for image_id in image_ids]))

        return {
            ImageId(row.id): IndexMetadata(
                file_size=row.file_size,
                file_modified_at=row.file_modified_at,
                content_hash=row.content_hash,
                capture_source=_source_member(row.capture_source),
                position_source=_position_member(row.position_source),
                thumbnail_path=row.thumbnail_path,
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

    def update_capture_date(self, image_id: ImageId, capture: CaptureDate) -> None:
        """Write one row's capture date with a targeted UPDATE.

        Like `update_index_metadata()`, and for the same reason: loading
        the row first would fetch the embedding this path exists to leave
        alone. An id with no row matches nothing and changes nothing.
        """
        self.update_capture_date_many({image_id: capture})

    def update_capture_date_many(self, captures: Mapping[ImageId, CaptureDate]) -> None:
        """Write many capture dates as one executemany UPDATE and one commit.

        A Core `UPDATE ... WHERE id = :target_id` executed with a list of
        parameter sets, rather than the ORM's bulk update by primary key.
        The ORM form checks that every key matched a row, and the port's
        contract is the opposite: an id with no row is skipped silently,
        the same as the single-row method.

        Rolled back on failure before the error propagates, so the caller
        can fall back to per-row writes on a clean session.
        """
        if not captures:
            return

        table = cast(Table, ImageModel.__table__)
        statement = (
            update(table)
            .where(table.c.id == bindparam("target_id"))
            .values(
                captured_at=bindparam("new_captured_at"),
                capture_source=bindparam("new_capture_source"),
            )
        )
        parameters = [
            {
                "target_id": image_id.value,
                "new_captured_at": capture.captured_at,
                "new_capture_source": capture.source.value,
            }
            for image_id, capture in captures.items()
        ]
        try:
            self._session.execute(statement, parameters)
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise

    def update_position(self, image_id: ImageId, reading: PositionReading) -> None:
        """Write one row's position with a targeted UPDATE (RFC-032 section 8)."""
        self.update_position_many({image_id: reading})

    def update_position_many(self, readings: Mapping[ImageId, PositionReading]) -> None:
        """Write many positions as one executemany UPDATE and one commit.

        `update_capture_date_many()`'s shape, for its reasons: a Core
        `UPDATE ... WHERE id = :target_id` so an id with no row is skipped
        rather than refused, no read of the row -- which would drag the
        embedding along -- and a rollback before re-raising so the caller's
        per-row fallback starts from a clean session.

        All three columns move in one statement, so the pairing CHECK never
        sees a row mid-write holding half a position.
        """
        if not readings:
            return

        table = cast(Table, ImageModel.__table__)
        statement = (
            update(table)
            .where(table.c.id == bindparam("target_id"))
            .values(
                latitude=bindparam("new_latitude"),
                longitude=bindparam("new_longitude"),
                position_source=bindparam("new_position_source"),
            )
        )
        parameters = [
            {
                "target_id": image_id.value,
                "new_latitude": reading.latitude,
                "new_longitude": reading.longitude,
                "new_position_source": reading.source.value,
            }
            for image_id, reading in readings.items()
        ]
        try:
            self._session.execute(statement, parameters)
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise

    def update_thumbnail_path(self, image_id: ImageId, location: str) -> None:
        """Record one row's thumbnail location with a targeted UPDATE."""
        self.update_thumbnail_path_many({image_id: location})

    def update_thumbnail_path_many(self, locations: Mapping[ImageId, str]) -> None:
        """Record many thumbnail locations as one executemany UPDATE.

        The shape of `update_capture_date_many()`, for its reasons: a Core
        `UPDATE` so that an id with no row is skipped rather than refused,
        no read of the row -- which would drag the embedding along -- and a
        rollback before re-raising so the caller's per-row fallback starts
        from a clean session.
        """
        if not locations:
            return

        table = cast(Table, ImageModel.__table__)
        statement = (
            update(table)
            .where(table.c.id == bindparam("target_id"))
            .values(thumbnail_path=bindparam("new_thumbnail_path"))
        )
        parameters = [
            {"target_id": image_id.value, "new_thumbnail_path": location}
            for image_id, location in locations.items()
        ]
        try:
            self._session.execute(statement, parameters)
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise

    def count_by_device(self) -> dict[DeviceId, int]:
        """One `GROUP BY device_id` for every device at once (RFC-031 section 4.3).

        A `COUNT(*)` per device would be N round trips behind a call that
        looks like one, and `GET /api/v1/devices` renders every disk in
        the sidebar -- so N is however many disks the user owns, on every
        page load.

        Devices with no rows produce no group and are therefore absent
        from the result, which is the contract: callers read it with
        `.get(device_id, 0)`.
        """
        statement = select(ImageModel.device_id, func.count()).group_by(
            ImageModel.device_id
        )
        return {
            DeviceId(device_id): int(total)
            for device_id, total in self._session.execute(statement).all()
        }

    def count_by_path_prefixes(
        self, device_id: DeviceId, parent: JobScope
    ) -> dict[str, int]:
        r"""One grouped query for every subfolder of `parent` (RFC-031 section 8.2).

        The whole reason this method exists rather than a per-folder
        `COUNT(*)`: a listing of forty folders must cost one query, not
        forty.

        The grouping key is `split_part(substr(relative_path, cut), '/',
        1)` -- everything after the parent prefix, up to the next
        separator. `cut` is one past the `/` that follows the prefix, and
        `1` for the whole-device scope, where the first part of
        `relative_path` is already the answer.

        **The `LIKE` is left-anchored on purpose and the trailing `/` is
        load-bearing.** `'2018b' LIKE '2018%'` is true and `2018b` is a
        different folder; `'2018b/x.jpg' LIKE '2018/%'` is false, which
        is the behaviour RFC-029 section 10 requires and `JobScope`
        enforces one layer up. `startswith(..., autoescape=True)` is used
        rather than a hand-built pattern so that a folder whose name
        contains `%` or `_` is matched literally instead of as a wildcard.

        Whether an index serves this is measured rather than assumed --
        see `experiments/rfc-031-devices-api/measure_folder_listing.log`
        for the `EXPLAIN` and the cluster's collation, which decide
        whether a B-tree on `(device_id, relative_path)` is usable for a
        left-anchored `LIKE` at all.

        The two kinds of key a caller must discard -- a file sitting
        directly in `parent`, and a folder renamed on disk since it was
        indexed -- are documented on the port and are deliberately not
        filtered here: this query cannot tell a directory from a file
        without touching the disk, and the caller already holds the
        names the filesystem reported.
        """
        prefix = str(parent)
        cut = len(prefix) + 2 if parent.parts else 1
        folder = func.split_part(func.substr(ImageModel.relative_path, cut), "/", 1)
        statement = select(folder, func.count()).where(
            ImageModel.device_id == device_id.value
        )
        if parent.parts:
            statement = statement.where(
                ImageModel.relative_path.startswith(f"{prefix}/", autoescape=True)
            )
        statement = statement.group_by(folder)
        return {
            str(name): int(total)
            for name, total in self._session.execute(statement).all()
        }


def _device_clause(filters: SearchFilters) -> ColumnElement[bool]:
    """The device-set predicate, shared by search, the counts and the map."""
    return ImageModel.device_id.in_(
        [device_id.value for device_id in filters.device_ids]
    )


def _date_clauses(filters: SearchFilters) -> tuple[ColumnElement[bool], ...]:
    """The half-open capture-date range, shared by search, the counts and the map."""
    date_range = filters.captured_between
    if date_range is None:
        return ()
    return (
        ImageModel.captured_at >= date_range.start,
        ImageModel.captured_at < date_range.end,
    )


def _circle_clauses(circle: GeoCircle) -> tuple[ColumnElement[bool], ...]:
    """A circle as SQL: a disposable box, then the exact distance (RFC-032 §6).

    **The last clause is the one that answers.** The box --
    `latitude BETWEEN ... AND longitude BETWEEN ...` -- exists only so an
    index *can* be used; the haversine distance against `radius_m` is the
    filter. A box alone would be a square, returning corners 1.41 times the
    requested radius away.

    **The box is omitted when it would be wrong** -- a circle over a pole,
    or across the 180th meridian, for which `GeoCircle.bounding_box()`
    returns `None` -- and then only the exact clause is emitted: slower,
    and right.

    No `IS NOT NULL`: a NULL latitude makes the box comparisons and the
    distance NULL, which `WHERE` reads as false, so an image of unknown
    position never matches, however large the circle (RFC-020). That holds
    only because every step of the distance propagates NULL -- see the
    clamp in `_distance_m()`, where the obvious `least()` did not.
    """
    clauses: list[ColumnElement[bool]] = []
    box = circle.bounding_box()
    if box is not None:
        clauses.append(ImageModel.latitude.between(box.min_latitude, box.max_latitude))
        clauses.append(
            ImageModel.longitude.between(box.min_longitude, box.max_longitude)
        )
    clauses.append(_distance_m(circle) <= circle.radius_m)
    return tuple(clauses)


def _distance_m(circle: GeoCircle) -> ColumnElement[float]:
    """The haversine distance from `circle.center` to each row, in metres.

    Term for term the formula of `app.domain.services.haversine`, with the
    same `EARTH_RADIUS_M` and the same order of operations -- differences
    taken before `radians()`, the clamp to 1 before `asin` -- so a
    point near a circle's edge falls on the same side here as in the
    in-memory implementations. Plain arithmetic over two columns; no
    extension (RFC-032 section 5.1).

    "Same side" up to the last bit of `sin` and `cos`, which come from the
    database server's maths library and not Python's: two libraries may
    round the same argument one unit apart. A point within a nanometre of
    the edge is not something a contract test can pin, and none tries.

    Every function is typed `Double` on purpose. Untyped, SQLAlchemy treats
    `radians(...) / 2` as a division of unknowns and renders the `2` as
    `CAST(... AS NUMERIC)`; PostgreSQL would coerce it back, but the SQL
    would no longer say what the Python says -- float arithmetic, start to
    finish.
    """
    center = circle.center

    def double(name: str, *arguments: Any) -> ColumnElement[float]:
        expression: ColumnElement[float] = getattr(func, name)(*arguments, type_=Double)
        return expression

    def squared_sine_of_half(angle: ColumnElement[float]) -> ColumnElement[float]:
        sine = double("sin", angle / 2.0)
        return sine * sine

    latitude_delta = double("radians", ImageModel.latitude - center.latitude)
    longitude_delta = double("radians", ImageModel.longitude - center.longitude)
    h = squared_sine_of_half(latitude_delta) + double(
        "cos", double("radians", literal(center.latitude, Double))
    ) * double("cos", double("radians", ImageModel.latitude)) * squared_sine_of_half(
        longitude_delta
    )
    # The clamp is a CASE and must stay one. `least(1, sqrt(h))` reads the
    # same and is a bug: PostgreSQL's LEAST *ignores* NULL arguments, so a
    # row with no position got `least(1, NULL) = 1` and a distance of
    # pi * R -- about 20,015 km -- instead of NULL. Any circle without a
    # pre-filter box and with a radius that large then matched every photo
    # with no position. `test_an_unknown_position_never_matches` caught it.
    root = double("sqrt", h)
    clamped: ColumnElement[float] = case(
        (root > literal(1.0, Double), literal(1.0, Double)), else_=root
    )
    return literal(2 * EARTH_RADIUS_M, Double) * double("asin", clamped)


def _source_value(source: CaptureSource | None) -> str | None:
    return source.value if source is not None else None


def _source_member(value: str | None) -> CaptureSource | None:
    """Read a stored `capture_source` back, keeping NULL as `None`.

    Never maps NULL to `CaptureSource.UNKNOWN`: NULL is "never examined",
    and the scan writes only rows in that state (RFC-028 section 4.2).
    """
    return CaptureSource(value) if value is not None else None


def _position_value(source: PositionSource | None) -> str | None:
    return source.value if source is not None else None


def _position_member(value: str | None) -> PositionSource | None:
    """Read a stored `position_source` back, keeping NULL as `None` (RFC-032 §4.4)."""
    return PositionSource(value) if value is not None else None
