"""Filesystem indexing worker orchestrating discovery and incremental indexing."""

from __future__ import annotations

from app.application.use_cases.index_or_update_image import IndexOrUpdateImageUseCase
from app.domain.entities.image import Image
from app.domain.value_objects.image_path import ImagePath
from app.infrastructure.filesystem.filesystem_image_provider import (
    FilesystemImageProvider,
)
from app.infrastructure.filesystem.image_identity import compute_image_id
from app.infrastructure.logging.logger import get_logger, log_exception

logger = get_logger(__name__)


class IndexingWorker:
    """Discovers image files and drives incremental indexing for each one.

    A dependency-injected orchestration component: it does not construct
    its own filesystem provider or use case, and it never touches
    SQLAlchemy, pgvector, or embedding-model internals directly.
    """

    def __init__(
        self,
        filesystem_provider: FilesystemImageProvider,
        index_or_update_use_case: IndexOrUpdateImageUseCase,
    ) -> None:
        self._filesystem_provider = filesystem_provider
        self._index_or_update_use_case = index_or_update_use_case

    def run(self) -> None:
        """Discover and incrementally index every supported image file.

        A failure indexing one file is logged and does not stop the
        remaining, independent files from being processed.
        """
        logger.info("Indexing started")

        for discovered in self._filesystem_provider.discover():
            logger.info("Discovered file: %s", discovered.path)
            try:
                image_path = ImagePath(str(discovered.path))
                image = Image(
                    id=compute_image_id(image_path),
                    path=image_path,
                    filename=discovered.filename,
                    extension=discovered.extension,
                )
                was_indexed = self._index_or_update_use_case.execute(
                    image,
                    file_size=discovered.file_size,
                    file_modified_at=discovered.file_modified_at,
                )
            except Exception:
                log_exception(logger, f"Failed to index {discovered.path}")
                continue

            if was_indexed:
                logger.info("Indexed: %s", discovered.path)
            else:
                logger.info("Skipped unchanged file: %s", discovered.path)

        logger.info("Indexing finished")
