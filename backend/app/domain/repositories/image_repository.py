"""Abstract repository port for image persistence operations."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

from app.domain.entities.image import Image
from app.domain.value_objects.embedding_vector import EmbeddingVector
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.index_metadata import IndexMetadata
from app.domain.value_objects.indexing_record import IndexingRecord
from app.domain.value_objects.search_filters import SearchFilters
from app.domain.value_objects.search_hit import SearchHits


class ImageRepository(ABC):
    """Repository contract for managing Image domain entities."""

    @abstractmethod
    def save(self, image: Image) -> None:
        """Persist a new image in the repository.

        Kept as the original RFC-015 create-once contract, used by
        `IndexImageUseCase`. Incremental indexing uses `save_indexed()`
        instead.
        """

    @abstractmethod
    def get(self, image_id: ImageId) -> Image | None:
        """Retrieve an image by its identifier."""

    @abstractmethod
    def exists(self, image_id: ImageId) -> bool:
        """Return whether an image exists for the provided identifier."""

    @abstractmethod
    def delete(self, image_id: ImageId) -> None:
        """Remove an image from the repository."""

    @abstractmethod
    def list(self) -> list[Image]:
        """Return all images known to the repository."""

    @abstractmethod
    def save_indexed(self, record: IndexingRecord) -> None:
        """Create or update an image together with its embedding and metadata."""

    @abstractmethod
    def save_indexed_many(self, records: Sequence[IndexingRecord]) -> None:
        """Create or update many images as a single unit of work.

        All or nothing: either every record is persisted or none is. That
        is what makes the method worth having -- one commit instead of N --
        and it is also what makes it dangerous, since a single bad row
        discards the embeddings of every other row in the batch, each of
        which cost real inference time to produce.

        Callers are therefore expected to fall back to `save_indexed()` per
        record when this raises, so that only the genuinely bad row is
        lost (RFC-024 section 7.2). An implementation must leave itself
        usable for exactly that: on failure it must roll back cleanly
        rather than leaving half-applied state behind.

        Constraint violations propagate as they do from `save_indexed()`;
        they are real errors, not duplicate-create signals.
        """

    @abstractmethod
    def search_similar(
        self,
        embedding: EmbeddingVector,
        limit: int,
        filters: SearchFilters | None = None,
    ) -> SearchHits:
        """Return the images closest to `embedding`, best match first.

        The counterpart of `save_indexed()`: that method is how an
        embedding enters the repository, and this is the only way one is
        used once it is there. The repository owns the ranking -- callers
        receive an ordered list and must not re-sort it, since an
        implementation is free to rank by whatever it stores rather than
        by recomputing anything in the caller's process.

        The contract every implementation owes:

        - Results are ordered from most similar to least similar, and
          `SearchHit.similarity` is cosine similarity in [-1, 1]. It is
          never rescaled to [0, 1]; a negative score is a real answer
          meaning the vectors oppose each other.
        - An image with no stored embedding never appears. It was never
          compared, and inventing a score for it would put unindexed
          files in front of indexed ones.
        - At most `limit` hits come back, fewer when fewer images have
          embeddings, and `[]` when none do. A short result means the
          repository ran out of candidates, never that it ran out of
          quality: filtering by a minimum score is not part of this
          contract.
        - Equally similar images come back in a stable, deterministic
          order, so that repeating a search repeats its result.
        - Without `filters`, the search covers every image known to the
          repository. RFC-025 wrote that there was "no scoping argument,
          by collection or otherwise" and named the condition for adding
          one: a real table, a real foreign key, and ownership rules.
          RFC-027 delivers those for *devices*, which are a physical fact
          about where bytes are rather than a modelling choice, so
          `filters` narrows by device and by nothing else yet.
        - `filters=None` and `filters=SearchFilters()` mean the same
          thing: no narrowing, and byte-for-byte the query RFC-025
          shipped. Neither may be read as "an empty set of devices",
          which would match nothing and turn a defaulted argument into a
          search that silently returns zero results.
        - A filtered search obeys every rule above, including ordering,
          the cosine range, and skipping images with no embedding. It
          restricts the candidate set; it does not change the ranking
          within it.
        - Filtering on a device that has no images returns `[]`, and
          filtering on an unknown device id is not an error. The set is a
          restriction, not an assertion that its members exist.
        - An `embedding` whose width does not match the indexed vectors
          raises `EmbeddingDimensionMismatchError`. It is a caller error,
          not a search that happens to match nothing, and it must fail
          identically in every implementation -- the plausible bug is an
          implementation that truncates to the shorter vector and returns
          a confident, meaningless ranking.

        `limit` is expected to be a positive number the caller has already
        vetted; policy about how large a page may be belongs to the
        Application layer, not here.

        **A filter may cost recall, and that is measured rather than
        assumed.** PostgreSQL does not push an arbitrary predicate into
        an HNSW graph traversal; it applies the filter to what the index
        already returned, so a scan that explored `ef_search` candidates
        can hand back *fewer* than `limit` rows while more matching rows
        exist. That is the same mechanism RFC-025 section 7.3 measured by
        accident with dead tuples. The filter is justified by usefulness
        -- "search only the disk in my hand" -- not by speed (RFC-027
        section 9.1).
        """

    @abstractmethod
    def get_index_metadata(self, image_id: ImageId) -> IndexMetadata | None:
        """Return the persisted filesystem metadata for an image, if any.

        Returns `None` when no row exists for `image_id` (new file).
        Returns `IndexMetadata(None, None)` when a row exists but has no
        metadata yet -- callers must treat that as changed, not unchanged.
        """

    @abstractmethod
    def get_index_metadata_many(
        self, image_ids: Sequence[ImageId]
    ) -> dict[ImageId, IndexMetadata]:
        """Return the persisted filesystem metadata for many images at once.

        The bulk counterpart of `get_index_metadata()`, and the reason
        RFC-024 added it: a re-index over an unchanged collection asks the
        skip question once per discovered file, so the per-file version
        turns a 100,000-image scan into 100,000 round trips *before* any
        inference happens. That is a Big-O problem in the number of files,
        independent of how fast the model is.

        Ids with no row are simply absent from the result -- the mapping is
        not padded with `None` values. Callers must therefore distinguish
        "absent" (new file) from "present with `None` fields" (row exists,
        metadata never written) exactly as they do for the single-id
        version.
        """

    @abstractmethod
    def update_index_metadata(self, image_id: ImageId, metadata: IndexMetadata) -> None:
        """Refresh an existing row's filesystem metadata, leaving its embedding.

        The write half of the content-hash check (ARCHITECTURE.md 16 step
        3): when a file's mtime or size moved but its bytes did not, the
        stored embedding is still correct and re-computing it would be pure
        waste. Only the metadata that drives the *next* skip decision needs
        to catch up.

        Does nothing when no row exists for `image_id`; a caller that wants
        a row created must go through `save_indexed()`, which is the only
        contract carrying an embedding.
        """
