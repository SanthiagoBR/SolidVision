"""Application use case for incremental filesystem-driven image indexing."""

from __future__ import annotations

import datetime

from app.domain.entities.image import Image
from app.domain.repositories.image_repository import ImageRepository
from app.domain.services.embedding_model_port import EmbeddingModelPort
from app.domain.value_objects.indexing_record import IndexingRecord


class IndexOrUpdateImageUseCase:
    """Create, update, or skip an image based on its filesystem metadata.

    Separate from `IndexImageUseCase` (RFC-015), which always creates and
    rejects duplicates. This use case compares the currently persisted
    `file_size`/`file_modified_at` against the values just read from the
    filesystem to decide whether re-indexing is necessary:

    - no persisted metadata at all (`get_index_metadata` returns `None`)
      -> new file, index it;
    - persisted metadata exists but both fields are `None` (the image was
      written by `IndexImageUseCase`'s plain `save()`) -> treated as
      changed, not unchanged, so it gets indexed and backfilled;
    - persisted metadata matches the filesystem exactly -> skip, without
      generating a new embedding;
    - persisted metadata differs -> re-index and replace it.
    """

    def __init__(
        self,
        repository: ImageRepository,
        embedding_model: EmbeddingModelPort,
    ) -> None:
        self._repository = repository
        self._embedding_model = embedding_model

    def execute(
        self,
        image: Image,
        file_size: int | None,
        file_modified_at: datetime.datetime | None,
    ) -> bool:
        """Index `image`, skipping it when its filesystem metadata is unchanged.

        Returns `True` when the image was (re-)indexed, `False` when it was
        skipped because its metadata already matched what was persisted.
        """
        existing = self._repository.get_index_metadata(image.id)

        if (
            existing is not None
            and existing.file_size == file_size
            and existing.file_modified_at == file_modified_at
        ):
            return False

        embedding = self._embedding_model.encode_image(image)
        record = IndexingRecord(
            image=image,
            embedding=embedding,
            file_size=file_size,
            file_modified_at=file_modified_at,
        )
        self._repository.save_indexed(record)
        return True
