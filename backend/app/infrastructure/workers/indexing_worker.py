"""Filesystem indexing worker orchestrating discovery and incremental indexing.

Also the worker CLI entry point promised by `AI_Context.md`:

    python -m app.infrastructure.workers.indexing_worker --root PATH

`AI_Context.md` writes that path as `infrastructure.workers.indexing_worker`,
without the `app.` prefix. That form has never been runnable -- the package is
`app.infrastructure`, and `pyproject.toml` puts `backend/` (not `backend/app/`)
on `pythonpath` -- so the documented path was corrected to match the code
rather than the code reshaped to match the documentation (RFC-024 section 12).
"""

from __future__ import annotations

import argparse
from collections.abc import Iterator
from pathlib import Path

from app.application.use_cases.index_or_update_images import (
    IndexingFailure,
    IndexingSummary,
    IndexOrUpdateImagesUseCase,
)
from app.application.use_cases.indexing_plan import IndexCandidate
from app.domain.entities.image import Image
from app.domain.value_objects.image_path import ImagePath
from app.infrastructure.filesystem.filesystem_image_provider import (
    FilesystemImageProvider,
)
from app.infrastructure.filesystem.image_identity import compute_image_id
from app.infrastructure.logging.logger import get_logger

logger = get_logger(__name__)


class IndexingWorker:
    """Discovers image files and drives incremental indexing for all of them.

    A dependency-injected orchestration component: it does not construct
    its own filesystem provider or use case, and it never touches
    SQLAlchemy, pgvector, or embedding-model internals directly.

    RFC-024 moved the per-file `try/except` that used to live in `run()`
    into the use case, because batching is where isolation is actually at
    risk -- a batch that fails as a unit has to be retried one image at a
    time, and only the component assembling batches can do that. What
    stays here is the guard around building the domain entity itself,
    which happens before a file is ever a candidate, and the logging of
    everything the run reports back.
    """

    def __init__(
        self,
        filesystem_provider: FilesystemImageProvider,
        index_or_update_images_use_case: IndexOrUpdateImagesUseCase,
    ) -> None:
        self._filesystem_provider = filesystem_provider
        self._index_or_update_images_use_case = index_or_update_images_use_case

    def run(self) -> IndexingSummary:
        """Discover and incrementally index every supported image file.

        Returns the run's counters and timings so a caller -- a test, a
        benchmark, a future scheduler -- can assert on them instead of
        parsing log output. Failures are logged here rather than inside the
        use case, keeping the logging factory an Infrastructure concern.
        """
        logger.info("Indexing started")

        undiscoverable: list[IndexingFailure] = []
        summary = self._index_or_update_images_use_case.execute(
            self._candidates(undiscoverable)
        )

        summary.discovered += len(undiscoverable)
        summary.failures.extend(undiscoverable)

        for fallback in summary.inference_fallbacks:
            logger.warning(
                "Inference batch of %d failed and was retried one image at a "
                "time: %s",
                len(fallback.paths),
                fallback.error,
            )

        for fallback in summary.persistence_fallbacks:
            logger.warning(
                "Bulk write of %d rows failed and degraded to per-row: %s",
                len(fallback.paths),
                fallback.error,
            )

        for failure in summary.failures:
            logger.error(
                "Failed to index %s",
                failure.path,
                exc_info=failure.error,
            )

        logger.info("%s", summary.format_report())
        return summary

    def _candidates(
        self, undiscoverable: list[IndexingFailure]
    ) -> Iterator[IndexCandidate]:
        """Stream discovered files as Application-layer candidates.

        A generator, not a list: the use case consumes it lazily, so
        discovery, hashing, inference, and persistence interleave and the
        pipeline never holds the whole collection in memory.

        Building `ImagePath`/`ImageId` can reject a path outright, and one
        such file must not end the run. Those failures are collected rather
        than raised, because raising out of a generator would close it and
        silently truncate the scan at that file.
        """
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
            except Exception as exc:
                undiscoverable.append(
                    IndexingFailure(path=str(discovered.path), error=exc)
                )
                continue

            yield IndexCandidate(
                image=image,
                file_size=discovered.file_size,
                file_modified_at=discovered.file_modified_at,
            )


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.infrastructure.workers.indexing_worker",
        description="Index every supported image under ROOT into PostgreSQL.",
    )
    parser.add_argument(
        "--root",
        type=Path,
        required=True,
        help=(
            "Directory to scan. Required on purpose: there is no default, "
            "not even settings.indexing_root_path, so that running this "
            "command can never start indexing a real photo collection "
            "nobody asked it to touch (same reasoning as "
            "dataset_tools/seed_demo.py)."
        ),
    )
    return parser


def main() -> None:
    """Compose the real pipeline and run it against an explicitly named root.

    This function is the composition root -- the one place allowed to know
    every concrete class at once. `IndexingWorker` itself stays injected
    and knows none of them.

    The provider imports are function-local rather than module-level so
    that importing `IndexingWorker` never drags Presentation, and through
    it the CLIP adapter, into an Infrastructure import graph. Most of the
    test suite imports this module; paying a torch import for a CLI that
    is not being run would be a real cost, not a hypothetical one.
    """
    from app.infrastructure.config.settings import settings
    from app.infrastructure.filesystem.sha256_content_hasher import Sha256ContentHasher
    from app.infrastructure.persistence.postgres_image_repository import (
        PostgresImageRepository,
    )
    from app.infrastructure.persistence.session import SessionLocal
    from app.presentation.dependencies import get_embedding_model

    args = _build_arg_parser().parse_args()
    root = args.root.resolve()

    session = SessionLocal()
    try:
        worker = IndexingWorker(
            filesystem_provider=FilesystemImageProvider(
                root, settings.supported_extensions
            ),
            index_or_update_images_use_case=IndexOrUpdateImagesUseCase(
                repository=PostgresImageRepository(session),
                embedding_model=get_embedding_model(),
                content_hasher=Sha256ContentHasher(),
                batch_size=settings.batch_size,
                metadata_prefetch_size=settings.metadata_prefetch_size,
            ),
        )
        worker.run()
    finally:
        session.close()


if __name__ == "__main__":
    main()
