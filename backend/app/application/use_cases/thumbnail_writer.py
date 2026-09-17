"""Rendering a thumbnail and keeping it, composed once (RFC-030 section 7).

Two ports and a size, and two callers that need all three together: the
indexing pipeline, which renders a thumbnail for every image it embeds, and
the thumbnail backfill, which renders one for every image indexed before
RFC-030. Composing them here once keeps the two callers from disagreeing
about which size a thumbnail is, or where it goes.
"""

from __future__ import annotations

from app.domain.entities.image import Image
from app.domain.services.thumbnail_generator_port import ThumbnailGeneratorPort
from app.domain.services.thumbnail_store_port import ThumbnailStorePort


class ThumbnailWriter:
    """Render `image` at `max_edge` and store the result under its id."""

    def __init__(
        self,
        generator: ThumbnailGeneratorPort,
        store: ThumbnailStorePort,
        max_edge: int,
    ) -> None:
        """Compose the writer; `max_edge` is injected, never read from settings.

        The Application layer must not import `Settings`
        (`test_application_architecture.py`), so the composition root
        passes `settings.thumbnail_max_edge` in -- the arrangement
        `IndexOrUpdateImagesUseCase` uses for `batch_size`.
        """
        if max_edge < 1:
            raise ValueError(f"max_edge must be at least 1, got {max_edge}")
        self._generator = generator
        self._store = store
        self._max_edge = max_edge

    def write(self, image: Image) -> str:
        """Render and store one thumbnail, returning the stored location.

        Errors from either port propagate. Whether a failed thumbnail
        matters is the caller's decision, and both callers decided it does
        not stop anything: the pipeline still persists the embedding, the
        backfill moves on to the next file.
        """
        return self._store.save(
            image.id, self._generator.generate(image, self._max_edge)
        )
