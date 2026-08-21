"""Abstract repository port for image persistence operations."""

from abc import ABC, abstractmethod
from collections.abc import Sequence

from app.domain.entities.image import Image
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.index_metadata import IndexMetadata
from app.domain.value_objects.indexing_record import IndexingRecord


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
