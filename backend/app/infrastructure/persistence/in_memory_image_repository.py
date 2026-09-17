"""In-memory image repository implementation for development and dependency wiring."""

from __future__ import annotations

import dataclasses
import math
import uuid
from collections.abc import Iterable, Mapping, Sequence

from app.domain.entities.image import Image
from app.domain.exceptions import EmbeddingDimensionMismatchError
from app.domain.repositories.image_repository import ImageRepository
from app.domain.value_objects.capture_date import CaptureDate
from app.domain.value_objects.embedding_vector import EmbeddingVector
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.index_metadata import IndexMetadata
from app.domain.value_objects.indexing_record import IndexingRecord
from app.domain.value_objects.search_filters import SearchFilters
from app.domain.value_objects.search_hit import SearchHit, SearchHits
from app.infrastructure.config.settings import settings


def cosine_search(
    query: EmbeddingVector,
    indexed: Iterable[tuple[Image, EmbeddingVector]],
    limit: int,
) -> SearchHits:
    """Rank `indexed` against `query` the way PostgreSQL would, in Python.

    Shared by the two in-process `ImageRepository` doubles -- this module's
    and `tests/application/fakes.py`'s -- rather than written twice. The
    whole reason they implement search at all is to be held to the same
    contract test as `PostgresImageRepository`, and two hand-rolled cosine
    loops would be two chances to drift from it and from each other.

    Deliberately computes the full cosine, dividing by both norms, instead
    of a bare dot product. Callers happen to store L2-normalized vectors
    today because `ClipEmbeddingModel` normalizes, but pgvector's `<=>`
    does not assume that, and a double that silently required it would
    disagree with PostgreSQL the first time a test used a hand-built
    vector of any other length.

    Ties break on the image id, matching the ORDER BY in
    `PostgresImageRepository.search_similar()`: PostgreSQL compares UUIDs
    byte by byte and Python compares `UUID.int`, which is the same
    ordering over the same 16 bytes.
    """
    _require_indexed_dimension(query)

    hits = [
        SearchHit(image=image, similarity=_cosine_similarity(query, embedding))
        for image, embedding in indexed
    ]
    hits.sort(key=lambda hit: (-hit.similarity, hit.image.id.value))
    return hits[:limit]


def matches_filters(image: Image, filters: SearchFilters | None) -> bool:
    """Return whether `image` survives `filters`, for an in-process ranking.

    Shared by both in-process `ImageRepository` doubles for the same
    reason `cosine_search()` is: one predicate written once cannot drift
    from the other, and both are held to the PostgreSQL contract by
    `test_search_similar_contract.py`.

    `None` and an empty `SearchFilters()` both mean "no narrowing", never
    "an empty set of devices". Getting that backwards would make a
    defaulted argument silently return nothing at all.

    Each field narrows on its own and only when set, as each adds its own
    `WHERE` clause in PostgreSQL.

    The `captured_at is not None` check is explicit on purpose, and it is
    the line most likely to be "simplified" away. PostgreSQL gets the
    RFC-020 rule for free -- a comparison with NULL is never true -- but
    Python does not: `start <= None` raises `TypeError` instead of
    returning `False`, so without the check an unknown date would crash
    the in-memory search where the database quietly excludes the row.
    """
    if filters is None or filters.is_empty():
        return True
    if filters.device_ids and image.device_id not in filters.device_ids:
        return False
    if filters.captured_between is not None:
        if image.captured_at is None:
            return False
        return filters.captured_between.contains(image.captured_at)
    return True


def count_unknown_capture_date(indexed: Iterable[Image], filters: SearchFilters) -> int:
    """Count what a date range hid for having no date, over searchable images.

    `indexed` must already be restricted to images with an embedding,
    exactly as `cosine_search()` expects. The device set is applied through
    `matches_filters()` with the date range removed, so the definition of
    "every other clause" cannot drift from the search predicate.

    Shared by both in-process doubles for the reason `matches_filters()`
    is.
    """
    if filters.captured_between is None:
        return 0
    without_date = SearchFilters(device_ids=filters.device_ids)
    return sum(
        1
        for image in indexed
        if image.captured_at is None and matches_filters(image, without_date)
    )


def store_thumbnail(thumbnails: dict[uuid.UUID, str], record: IndexingRecord) -> None:
    """Apply a record's thumbnail location the way `save_indexed()` writes it.

    `None` removes the stored location instead of leaving it, because the
    record carries new bytes and a thumbnail of the old ones no longer
    depicts the file -- the PostgreSQL implementation writes the NULL.
    Shared by both in-process doubles so they cannot disagree about it.
    """
    if record.thumbnail_path is None:
        thumbnails.pop(record.image.id.value, None)
    else:
        thumbnails[record.image.id.value] = record.thumbnail_path


def with_capture_date(image: Image, capture: CaptureDate) -> Image:
    """Return `image` carrying `capture`, for the frozen entity's two fields."""
    return dataclasses.replace(
        image, captured_at=capture.captured_at, capture_source=capture.source
    )


def _require_indexed_dimension(query: EmbeddingVector) -> None:
    """Reject a query vector that cannot be compared with the stored ones.

    Checked against the configured dimension rather than against whatever
    happens to be stored, so that the error does not depend on the
    repository being non-empty: PostgreSQL rejects a mismatched vector at
    the `vector(512)` column whether or not any row exists, and a double
    that answered `[]` for an empty store would be the more permissive of
    the two exactly where a test is least likely to notice.
    """
    if len(query.values) != settings.embedding_dimension:
        raise EmbeddingDimensionMismatchError(
            f"Query embedding has {len(query.values)} dimensions, but the "
            f"index holds {settings.embedding_dimension}."
        )


def _cosine_similarity(left: EmbeddingVector, right: EmbeddingVector) -> float:
    """Return cosine similarity in [-1, 1], never a truncated approximation.

    The length check is why this is a function rather than an inline
    expression. `zip` stops at the shorter operand, so comparing a
    3-dimensional vector with a 512-dimensional one would otherwise return
    a plausible number computed from three dimensions -- a wrong answer
    that looks exactly like a right one. `save_indexed_many()` below
    documents the same class of hazard for atomicity: a double must not be
    quietly more forgiving than the database it stands in for.
    """
    if len(left.values) != len(right.values):
        raise EmbeddingDimensionMismatchError(
            f"Cannot compare a {len(left.values)}-dimensional vector with a "
            f"{len(right.values)}-dimensional one."
        )

    dot = sum(x * y for x, y in zip(left.values, right.values))
    left_norm = math.sqrt(sum(x * x for x in left.values))
    right_norm = math.sqrt(sum(y * y for y in right.values))
    return dot / (left_norm * right_norm)


class InMemoryImageRepository(ImageRepository):
    """Repository implementation backed by an in-memory list."""

    def __init__(self) -> None:
        self._images: list[Image] = []
        self._metadata: dict[uuid.UUID, IndexMetadata] = {}
        # Kept apart from `_images` because not every image has one:
        # `save()` creates a row with no embedding, exactly as the
        # PostgreSQL column is nullable, and search must skip those.
        self._embeddings: dict[uuid.UUID, EmbeddingVector] = {}
        # Apart from `_metadata` too, although it travels back inside an
        # `IndexMetadata`: `update_index_metadata()` replaces that entry
        # wholesale with whatever the caller built, and a caller refreshing
        # change signals never knows the thumbnail. Kept in `_metadata`, a
        # refresh would erase it; the PostgreSQL `UPDATE` does not.
        self._thumbnails: dict[uuid.UUID, str] = {}

    def save(self, image: Image) -> None:
        self._images.append(image)

    def get(self, image_id: ImageId) -> Image | None:
        for image in self._images:
            if image.id == image_id:
                return image
        return None

    def exists(self, image_id: ImageId) -> bool:
        return any(image.id == image_id for image in self._images)

    def delete(self, image_id: ImageId) -> None:
        self._images = [image for image in self._images if image.id != image_id]
        self._metadata.pop(image_id.value, None)
        self._embeddings.pop(image_id.value, None)
        self._thumbnails.pop(image_id.value, None)

    def list(self) -> list[Image]:
        return list(self._images)

    def save_indexed(self, record: IndexingRecord) -> None:
        """Store the image, its metadata, and -- since RFC-025 -- its embedding.

        The embedding used to be dropped on the floor here, which was
        harmless while nothing read one back and became a lie the moment
        `search_similar()` existed: a repository that accepts a vector and
        then finds nothing is not a stand-in for one that can.
        """
        self._images = [image for image in self._images if image.id != record.image.id]
        self._images.append(record.image)
        self._metadata[record.image.id.value] = IndexMetadata(
            file_size=record.file_size,
            file_modified_at=record.file_modified_at,
            content_hash=record.content_hash,
        )
        self._embeddings[record.image.id.value] = record.embedding
        store_thumbnail(self._thumbnails, record)

    def save_indexed_many(self, records: Sequence[IndexingRecord]) -> None:
        """Persist every record, or none of them.

        A dict and a list have no transaction to commit, so atomicity has
        to be arranged by hand: the writes are applied to copies and only
        swapped in once all of them have succeeded. Skipping that would
        make this implementation quietly more forgiving than the real one,
        and the per-row fallback it exists to trigger would then go
        untested everywhere except against PostgreSQL.
        """
        images = list(self._images)
        metadata = dict(self._metadata)
        embeddings = dict(self._embeddings)
        thumbnails = dict(self._thumbnails)

        for record in records:
            images = [image for image in images if image.id != record.image.id]
            images.append(record.image)
            metadata[record.image.id.value] = IndexMetadata(
                file_size=record.file_size,
                file_modified_at=record.file_modified_at,
                content_hash=record.content_hash,
            )
            embeddings[record.image.id.value] = record.embedding
            store_thumbnail(thumbnails, record)

        self._images = images
        self._metadata = metadata
        self._embeddings = embeddings
        self._thumbnails = thumbnails

    def search_similar(
        self,
        embedding: EmbeddingVector,
        limit: int,
        filters: SearchFilters | None = None,
    ) -> SearchHits:
        """Rank every stored embedding against `embedding`, in Python.

        Scoring the whole store on every call is fine here and would not
        be fine in production: this implementation exists for tests and
        for wiring an application together before a database is up, where
        the store holds tens of images. `PostgresImageRepository` is the
        real path, and it ranks in the database precisely so that 100,000
        vectors never cross into this process.

        The filters are applied *before* ranking, matching a `WHERE`
        clause rather than a post-hoc trim of the top-K. Filtering
        afterwards would return fewer than `limit` rows whenever the
        excluded images happened to rank high -- which is exactly the
        HNSW recall hazard RFC-027 section 9.1 describes, and a test
        double must not reproduce a physical-index artefact as if it were
        contract.
        """
        return cosine_search(
            embedding,
            (
                (image, self._embeddings[image.id.value])
                for image in self._images
                if image.id.value in self._embeddings
                and matches_filters(image, filters)
            ),
            limit,
        )

    def count_unknown_capture_date(self, filters: SearchFilters) -> int:
        return count_unknown_capture_date(
            (image for image in self._images if image.id.value in self._embeddings),
            filters,
        )

    def get_index_metadata(self, image_id: ImageId) -> IndexMetadata | None:
        """Return the stored change signals plus the image's capture source.

        The capture source is read off the stored `Image`, not kept in
        `_metadata` as well. The entity is where the capture date lives in
        this implementation; a second copy here would be free to disagree
        with it -- for instance after `update_index_metadata()` stored a
        caller's `IndexMetadata` that never had a source in it.
        """
        image = self.get(image_id)
        if image is None:
            return None
        stored = self._metadata.get(
            image_id.value, IndexMetadata(file_size=None, file_modified_at=None)
        )
        return dataclasses.replace(
            stored,
            capture_source=image.capture_source,
            thumbnail_path=self._thumbnails.get(image_id.value),
        )

    def get_index_metadata_many(
        self, image_ids: Sequence[ImageId]
    ) -> dict[ImageId, IndexMetadata]:
        """Return metadata for every supplied id that has a row.

        Composed from `get_index_metadata()` rather than reimplemented, so
        the two can never disagree. There is nothing to batch in a dict
        lookup: the bulk method exists to collapse *network* round trips,
        which this implementation does not make.
        """
        found = {}
        for image_id in image_ids:
            metadata = self.get_index_metadata(image_id)
            if metadata is not None:
                found[image_id] = metadata
        return found

    def update_index_metadata(self, image_id: ImageId, metadata: IndexMetadata) -> None:
        if not self.exists(image_id):
            return
        self._metadata[image_id.value] = metadata

    def update_capture_date(self, image_id: ImageId, capture: CaptureDate) -> None:
        self.update_capture_date_many({image_id: capture})

    def update_capture_date_many(self, captures: Mapping[ImageId, CaptureDate]) -> None:
        """Replace each named image with a copy carrying its capture date.

        `Image` is frozen, so "updating a column" is swapping the entity
        for `dataclasses.replace()` of it. Ids with no image are skipped,
        as they are by the PostgreSQL `UPDATE`.
        """
        self._images = [
            (
                with_capture_date(image, captures[image.id])
                if image.id in captures
                else image
            )
            for image in self._images
        ]

    def update_thumbnail_path(self, image_id: ImageId, location: str) -> None:
        self.update_thumbnail_path_many({image_id: location})

    def update_thumbnail_path_many(self, locations: Mapping[ImageId, str]) -> None:
        """Record each location whose image exists; skip the rest."""
        known = {image.id for image in self._images}
        for image_id, location in locations.items():
            if image_id in known:
                self._thumbnails[image_id.value] = location
