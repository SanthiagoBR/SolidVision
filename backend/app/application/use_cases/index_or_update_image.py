"""Application use case for incremental filesystem-driven image indexing."""

from __future__ import annotations

import datetime

from app.application.use_cases.indexing_plan import (
    IndexAction,
    IndexCandidate,
    plan_indexing,
)
from app.domain.entities.image import Image
from app.domain.repositories.image_repository import ImageRepository
from app.domain.services.content_hasher_port import ContentHasherPort
from app.domain.services.embedding_model_port import EmbeddingModelPort
from app.domain.value_objects.index_metadata import IndexMetadata
from app.domain.value_objects.indexing_record import IndexingRecord


class IndexOrUpdateImageUseCase:
    """Create, update, or skip a single image based on its filesystem state.

    Separate from `IndexImageUseCase` (RFC-015), which always creates and
    rejects duplicates. This use case runs the three-step incremental check
    from `ARCHITECTURE.md` section 16 -- see `plan_indexing()`, which owns
    the decision itself:

    - no persisted metadata at all -> new file, index it;
    - persisted metadata exists but both fields are `None` (the image was
      written by `IndexImageUseCase`'s plain `save()`) -> treated as
      changed, not unchanged, so it gets indexed and backfilled;
    - persisted metadata matches the filesystem exactly -> skip, without
      reading the file at all;
    - metadata differs but the content hash proves the bytes are identical
      -> refresh the stored metadata and keep the existing embedding;
    - metadata differs and the content genuinely changed -> re-index.

    RFC-024 kept this class deliberately, rather than retiring it in favour
    of `IndexOrUpdateImagesUseCase`. It is the single-file entry point: the
    decision logic is shared with the batch coordinator through
    `plan_indexing()`, so there is no second copy of anything, but errors
    here propagate to the caller instead of being collected into a run
    summary. That is the right contract for indexing one known file --
    a future single-file reindex or file-watcher path wants to be told
    that its one file failed, not handed a report saying zero of one
    succeeded.
    """

    def __init__(
        self,
        repository: ImageRepository,
        embedding_model: EmbeddingModelPort,
        content_hasher: ContentHasherPort,
    ) -> None:
        self._repository = repository
        self._embedding_model = embedding_model
        self._content_hasher = content_hasher

    def execute(
        self,
        image: Image,
        file_size: int | None,
        file_modified_at: datetime.datetime | None,
    ) -> bool:
        """Index `image`, skipping it when its content is unchanged.

        Returns `True` when the image was (re-)indexed, `False` when no new
        embedding was needed -- either because nothing about the file
        changed, or because only its filesystem metadata did.
        """
        candidate = IndexCandidate(
            image=image,
            file_size=file_size,
            file_modified_at=file_modified_at,
        )
        plan = plan_indexing(
            candidate,
            self._repository.get_index_metadata(image.id),
            self._content_hasher,
        )

        if plan.action is IndexAction.SKIP_UNCHANGED:
            return False

        if plan.action is IndexAction.REFRESH_METADATA:
            self._repository.update_index_metadata(
                image.id,
                IndexMetadata(
                    file_size=file_size,
                    file_modified_at=file_modified_at,
                    content_hash=plan.content_hash,
                ),
            )
            return False

        embedding = self._embedding_model.encode_image(image)
        record = IndexingRecord(
            image=image,
            embedding=embedding,
            file_size=file_size,
            file_modified_at=file_modified_at,
            content_hash=plan.content_hash,
        )
        self._repository.save_indexed(record)
        return True
