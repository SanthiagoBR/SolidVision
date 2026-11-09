"""Dá às imagens já indexadas a data de captura e a posição, sem o modelo.

    python -m app.infrastructure.workers.exif_backfill --root PATH
    python -m app.infrastructure.workers.exif_backfill --root PATH --force
    python -m app.infrastructure.workers.exif_backfill --root PATH --dry-run

**Este comando substitui o `capture_date_backfill`** (RFC-032 §8.1). Um backfill
de posição ao lado dele leria o mesmo cabeçalho dos mesmos arquivos numa
segunda passada -- dois seeks por arquivo num disco externo giratório -- para
preencher colunas que saem de uma única leitura. Um comando só agora grava os
dois fatos a partir dessa leitura. A troca de comando do operador fica
declarada aqui e no RFC-032, em vez de escondida atrás de um alias que ninguém
jamais removeria.

As linhas indexadas antes do RFC-028 têm `capture_source = NULL`, e as
indexadas antes do RFC-032 têm `position_source = NULL`. O próximo scan comum
de indexação do disco delas preenche os dois de qualquer jeito, então este
comando não é pré-requisito de nada. Ele existe para o operador que quer os
fatos *agora* sem compor o pipeline de indexação -- e para o único caso que
nenhum scan alcança: depois que a extração melhora, as linhas já marcadas
`unknown` precisam ser lidas de novo (`--force`). Tudo o que o RFC-028 §7
estabeleceu é mantido, por fato: `--only-unknown` é o padrão idempotente, e
`--force` nunca troca uma fonte gravada por uma mais fraca -- um `exif_gps`
sobrevive a um arquivo cujo GPS hoje dá `unknown`, e um arquivo pode perder a
data e manter a posição.

**Ele nunca carrega, importa ou constrói o modelo de embedding.** Esse é todo
o argumento de custo: uma leitura de cabeçalho por arquivo em vez das ~5 horas
de inferência que uma reindexação de 40.000 fotos levaria. A propriedade é
verificada contra o grafo real de imports, num interpretador novo, por
`tests/infrastructure/workers/test_exif_backfill.py` -- junto com a guarda da
guarda, que prova que a verificação pode falhar.

Ele só lê a coleção. Nada é escrito em arquivo algum (RFC-032 §10), e nenhuma
linha de imagem é criada: um arquivo sem linha é relatado como não indexado e
deixado para o worker de indexação. `--dry-run` lê tudo e não grava nada, nem
a linha do dispositivo.

O disco precisa estar conectado, porque os fatos estão dentro dos arquivos
(RFC-027 §7). As linhas de outros discos não são tocadas; rode o comando uma
vez por disco.

**`--root` é obrigatório e não tem padrão**, pelo motivo que o worker de
indexação dá: um comando que percorre uma coleção de fotos nunca deve escolher
uma por conta própria.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from app.application.use_cases.backfill_exif import (
    BackfillExifUseCase,
    ExifBackfillSummary,
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
        prog="python -m app.infrastructure.workers.exif_backfill",
        description=(
            "Read the EXIF capture date and GPS position of every indexed image "
            "under ROOT, in one read per file, and store them without "
            "recomputing any embedding (RFC-032 section 8.1)."
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
        help="Read every file and report what would be written, writing nothing.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--only-unknown",
        dest="force",
        action="store_false",
        help=(
            "The default. For each fact, examine only rows that have never been "
            "examined for it (capture_source or position_source NULL). "
            "Idempotent: a second run over the same disk writes nothing. Rows "
            "already examined and found to have nothing ('unknown') are not "
            "re-read -- use --force for that."
        ),
    )
    mode.add_argument(
        "--force",
        dest="force",
        action="store_true",
        help=(
            "Also re-examine rows already examined, for when the extraction "
            "has improved. Per fact, a stored source is only ever replaced by "
            "one at least as strong: exif_original is never overwritten by "
            "exif_digitized or unknown, and exif_gps never by unknown."
        ),
    )
    parser.set_defaults(force=False)
    return parser


def _log_summary(summary: ExifBackfillSummary) -> None:
    for failure in summary.failures:
        logger.error("Failed to backfill %s", failure.path, exc_info=failure.error)
    for fact, tally in (
        ("capture date", summary.dates),
        ("position", summary.positions),
    ):
        if tally.kept_stronger:
            logger.warning(
                "%d row(s) now read a weaker %s than the one stored and were "
                "kept as they were. Check those files for damage.",
                tally.kept_stronger,
                fact,
            )
    logger.info("%s", summary.format_report())


def main() -> None:
    """Compõe o backfill e o roda contra uma raiz nomeada explicitamente.

    Os imports concretos ficam dentro da função, como no worker de indexação,
    para que importar este módulo pelo parser de argumentos ou pelos testes
    não traga nenhum driver de banco. O que falta de propósito na lista é
    `app.presentation.dependencies`, o único lugar onde o adaptador CLIP é
    composto; o executor de jobs precisa dele, e este comando não pode.
    """
    from app.infrastructure.config.settings import settings
    from app.infrastructure.filesystem.filesystem_image_provider import (
        FilesystemImageProvider,
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
            "Backfilling EXIF facts under %s on device %s (%s), mounted at %s",
            root,
            device.label,
            device.id,
            volume.mount_point,
        )
        undiscoverable: list[IndexingFailure] = []
        # As duas extrações estão sempre ligadas aqui, digam o que disserem
        # `extract_capture_date` e `extract_gps`: ler esses dois fatos é a única
        # coisa que este comando faz, e eles saem de uma abertura de cada arquivo.
        provider = FilesystemImageProvider(
            root,
            settings.supported_extensions,
            extract_capture_date=True,
            extract_gps=True,
            excluded_directories=(settings.thumbnail_directory,),
        )
        summary = BackfillExifUseCase(
            repository=PostgresImageRepository(session),
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
