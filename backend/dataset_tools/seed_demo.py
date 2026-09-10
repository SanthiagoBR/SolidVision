"""CLI entry point that seeds a running PostgreSQL database with the demo corpus.

Usage:
    python -m dataset_tools.seed_demo [--target PATH]

Materializes the manifest-described demo corpus into `--target` (default:
`data/demo`, resolved relative to the current working directory) with each
file's mtime stamped from the manifest, then runs the real indexing
pipeline -- `FilesystemImageProvider` -> `IndexingWorker` ->
`IndexOrUpdateImagesUseCase` -> `PostgresImageRepository` -- against it,
using `FakeEmbeddingModel` (no AI library is loaded).

The default target is deliberately NOT `settings.indexing_root_path`, even
though that is the directory the production worker CLI
(`python -m app.infrastructure.workers.indexing_worker`) scans.
Defaulting there would silently mix 40 demo photos into a user's real
photo collection the first time they configure `INDEXING_ROOT_PATH` and
run this script from habit. Anyone who wants the running app to actually
serve the demo data must point `INDEXING_ROOT_PATH` at `--target`
themselves -- a deliberate, visible `.env` change, not a script default.

Unlike the test suite, which uses a SAVEPOINT-isolated session rolled back
on teardown (see `backend/tests/conftest.py`), this commits for real: it
is meant to leave a working demo dataset behind for manual QA and
`/search` experimentation once that endpoint exists. Safe to re-run --
`IndexOrUpdateImagesUseCase` skips any file whose filesystem metadata is
unchanged (RFC-022 section 7).
"""

from __future__ import annotations

import argparse
from pathlib import Path

from app.application.use_cases.index_or_update_images import (
    IndexOrUpdateImagesUseCase,
)
from app.infrastructure.ai.fake_embedding_model import FakeEmbeddingModel
from app.infrastructure.config.settings import settings
from app.infrastructure.filesystem.filesystem_image_provider import (
    FilesystemImageProvider,
)
from app.infrastructure.filesystem.sha256_content_hasher import Sha256ContentHasher
from app.infrastructure.filesystem.volume_identity_provider import (
    WindowsVolumeIdentityProvider,
)
from app.infrastructure.logging.logger import get_logger
from app.infrastructure.persistence.postgres_device_repository import (
    PostgresDeviceRepository,
)
from app.infrastructure.persistence.postgres_image_repository import (
    PostgresImageRepository,
)
from app.infrastructure.persistence.session import SessionLocal
from app.infrastructure.workers.indexing_worker import (
    IndexingWorker,
    register_device,
)
from dataset_tools.manifest import DEMO_CORPUS_ROOT, DEMO_MANIFEST_PATH, load_manifest
from dataset_tools.materialize import materialize

logger = get_logger(__name__)

DEFAULT_SEED_TARGET = Path("data/demo")


def seed_demo(target_root: Path) -> None:
    """Materialize the demo corpus into `target_root` and index it into PostgreSQL."""
    manifest = load_manifest(DEMO_MANIFEST_PATH)
    manifest.verify_matches_directory(DEMO_CORPUS_ROOT)

    logger.info(
        "Materializing %d demo images into %s", len(manifest.images), target_root
    )
    materialize(manifest, DEMO_CORPUS_ROOT, target_root)

    session = SessionLocal()
    try:
        # RFC-027: an image belongs to a device, so the volume holding the
        # target has to be registered before anything is indexed onto it.
        # The demo corpus lives wherever the operator pointed it, which is
        # usually the system drive -- the label says what it is so that a
        # seeded database does not present the developer's own disk under
        # a name suggesting it holds a real photo collection.
        device, volume = register_device(
            volume_provider=WindowsVolumeIdentityProvider(),
            device_repository=PostgresDeviceRepository(session),
            root=target_root,
            label="DEMO-CORPUS",
        )
        repository = PostgresImageRepository(session)
        worker = IndexingWorker(
            filesystem_provider=FilesystemImageProvider(
                target_root, settings.supported_extensions
            ),
            index_or_update_images_use_case=IndexOrUpdateImagesUseCase(
                repository=repository,
                embedding_model=FakeEmbeddingModel(),
                content_hasher=Sha256ContentHasher(),
                batch_size=settings.batch_size,
                metadata_prefetch_size=settings.metadata_prefetch_size,
            ),
            device=device,
            mount_point=volume.mount_point,
        )
        worker.run()
    finally:
        session.close()


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target",
        type=Path,
        default=DEFAULT_SEED_TARGET,
        help=(
            "Directory to materialize the demo corpus into before indexing "
            "(default: %(default)s -- deliberately separate from "
            "indexing_root_path, see module docstring)"
        ),
    )
    return parser


def main() -> None:
    """Parse CLI arguments and run `seed_demo` against the resolved target."""
    args = _build_arg_parser().parse_args()
    seed_demo(args.target.resolve())


if __name__ == "__main__":
    main()
