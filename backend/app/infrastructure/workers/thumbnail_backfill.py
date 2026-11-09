"""Dá às imagens já indexadas uma thumbnail, sem o modelo (RFC-030 §7.2).

    python -m app.infrastructure.workers.thumbnail_backfill --root PATH
    python -m app.infrastructure.workers.thumbnail_backfill --root PATH --force
    python -m app.infrastructure.workers.thumbnail_backfill --root PATH --dry-run

As imagens indexadas antes do RFC-030 não têm thumbnail, e nenhuma imagem cuja
thumbnail falhou ao ser renderizada durante a indexação tem. O scan de
indexação não conserta nenhum dos dois casos: renderizar é uma decodificação
completa, e fazê-la para cada arquivo que um novo scan pula transformaria um
`stat` por arquivo numa decodificação por arquivo. Este comando é onde esse
trabalho é pago, uma vez, quando o operador pede.

**Ele nunca carrega, importa ou constrói o modelo de embedding**, a
propriedade que o `capture_date_backfill` estabeleceu (hoje `exif_backfill`,
RFC-032) e que `tests/infrastructure/workers/test_thumbnail_backfill.py`
verifica no grafo real de imports. O Pillow é importado; o torch, não.

Ele só lê a coleção. As thumbnails são gravadas em
`settings.thumbnail_directory` e em nenhum outro lugar (RFC-030 §7.1), e essa
pasta é pulada pela varredura mesmo que fique sob `--root`.

O disco precisa estar conectado, porque as fotos estão nele (RFC-027 §7).
**`--root` é obrigatório e não tem padrão**, pelo motivo que o worker de
indexação dá.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from app.application.use_cases.backfill_thumbnails import (
    BackfillThumbnailsUseCase,
    ThumbnailBackfillSummary,
)
from app.application.use_cases.index_or_update_images import IndexingFailure
from app.infrastructure.logging.logger import get_logger
from app.infrastructure.workers.indexing_worker import (
    discovered_candidates,
    register_device,
)

logger = get_logger(__name__)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.infrastructure.workers.thumbnail_backfill",
        description=(
            "Render a thumbnail for every indexed image under ROOT that has "
            "none, without recomputing any embedding (RFC-030 section 7.2)."
        ),
    )
    parser.add_argument(
        "--root",
        type=Path,
        required=True,
        help=(
            "Directory to read. Required, with no default, so this command can "
            "never walk a photo collection nobody named (same reasoning as the "
            "indexing worker). The disk holding it must be connected."
        ),
    )
    parser.add_argument(
        "--label",
        default="",
        help=(
            "User-facing name for the disk, used only if the volume has no "
            "label yet -- the same rule as the indexing worker."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Count what would be rendered, rendering and writing nothing.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--only-missing",
        dest="force",
        action="store_false",
        help=(
            "The default. Render only rows that have no thumbnail. Idempotent: "
            "a second run over the same disk renders nothing."
        ),
    )
    mode.add_argument(
        "--force",
        dest="force",
        action="store_true",
        help=(
            "Render every indexed row again, replacing stored thumbnails -- "
            "for after THUMBNAIL_MAX_EDGE changed. A row whose file fails to "
            "render keeps the thumbnail it had."
        ),
    )
    parser.set_defaults(force=False)
    return parser


def _log_summary(summary: ThumbnailBackfillSummary) -> None:
    for failure in summary.failures:
        logger.error(
            "Failed to render a thumbnail for %s", failure.path, exc_info=failure.error
        )
    logger.info("%s", summary.format_report())


def main() -> None:
    """Compõe o backfill e o roda contra uma raiz nomeada explicitamente.

    Imports dentro da função, como em `exif_backfill`, para que importar este
    módulo pelo parser ou pelos testes não traga nenhum driver de banco.
    `app.presentation.dependencies` falta na lista pelo mesmo motivo por que
    falta lá: é onde o adaptador CLIP é composto.
    """
    from app.application.use_cases.thumbnail_writer import ThumbnailWriter
    from app.infrastructure.config.settings import settings
    from app.infrastructure.filesystem.filesystem_image_provider import (
        FilesystemImageProvider,
    )
    from app.infrastructure.filesystem.thumbnail_generator import (
        PillowThumbnailGenerator,
    )
    from app.infrastructure.filesystem.thumbnail_store import (
        FilesystemThumbnailStore,
    )
    from app.infrastructure.filesystem.volume_identity_provider import (
        WindowsVolumeIdentityProvider,
    )
    from app.infrastructure.persistence.postgres_device_repository import (
        PostgresDeviceRepository,
    )
    from app.infrastructure.persistence.postgres_image_repository import (
        PostgresImageRepository,
    )
    from app.infrastructure.persistence.session import SessionLocal

    args = _build_arg_parser().parse_args()
    root = args.root.resolve()

    session = SessionLocal()
    try:
        device, volume = register_device(
            volume_provider=WindowsVolumeIdentityProvider(),
            device_repository=PostgresDeviceRepository(session),
            root=root,
            label=args.label,
            persist=not args.dry_run,
        )
        logger.info(
            "Backfilling thumbnails under %s on device %s (%s), mounted at %s, "
            "into %s",
            root,
            device.label,
            device.id,
            volume.mount_point,
            settings.thumbnail_directory,
        )
        undiscoverable: list[IndexingFailure] = []
        # Nenhuma data de captura é lida: este comando renderiza imagens, e uma
        # leitura de EXIF por arquivo seria um custo sem consumidor.
        provider = FilesystemImageProvider(
            root,
            settings.supported_extensions,
            extract_capture_date=False,
            extract_gps=False,
            excluded_directories=(settings.thumbnail_directory,),
        )
        summary = BackfillThumbnailsUseCase(
            repository=PostgresImageRepository(session),
            thumbnail_writer=ThumbnailWriter(
                generator=PillowThumbnailGenerator(),
                store=FilesystemThumbnailStore(settings.thumbnail_directory),
                max_edge=settings.thumbnail_max_edge,
            ),
            metadata_prefetch_size=settings.metadata_prefetch_size,
            force=args.force,
            dry_run=args.dry_run,
        ).execute(
            discovered_candidates(provider, device, volume.mount_point, undiscoverable)
        )
        summary.discovered += len(undiscoverable)
        summary.failures.extend(undiscoverable)
        _log_summary(summary)
    finally:
        session.close()


if __name__ == "__main__":
    main()
