"""Dá uma thumbnail às imagens já indexadas, sem o modelo (RFC-030 §7.2).

A forma de `BackfillExifUseCase` -- a do RFC-032, que substituiu o backfill de
data de captura do RFC-028 --, e de propósito: os mesmos candidatos descobertos,
a mesma leitura antecipada de metadados por janela, a mesma gravação em massa
por janela com recuo linha a linha. O que muda é quanto custa um arquivo. Um
fato EXIF é a leitura de um cabeçalho; uma thumbnail é uma decodificação
completa, um redimensionamento e uma codificação -- ainda uma pequena fração da
inferência que uma reindexação pagaria, e nunca o modelo.

Não precisa de modelo de embedding nem de hasher de conteúdo, e não recebe
nenhum dos dois. `tests/infrastructure/workers/test_thumbnail_backfill.py`
procura os dois no grafo de imports do comando, como faz o teste do backfill de
EXIF.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass, field

from app.application.use_cases.index_or_update_images import (
    IndexingFailure,
    windowed,
)
from app.application.use_cases.indexing_plan import IndexCandidate
from app.application.use_cases.thumbnail_writer import ThumbnailWriter
from app.domain.repositories.image_repository import ImageRepository
from app.domain.value_objects.image_id import ImageId


@dataclass
class ThumbnailBackfillSummary:
    """O que um backfill de thumbnails achou e fez, como dados (RFC-024 §11)."""

    force: bool
    dry_run: bool
    discovered: int = 0

    written: int = 0
    """Linhas que receberam uma thumbnail -- ou, num dry run, as que receberiam."""

    already_generated: int = 0
    """Linhas puladas porque já têm uma.

    Quase tudo numa segunda execução sem `--force`: o modo padrão é
    idempotente, e este contador é como isso aparece.
    """

    not_indexed: int = 0
    """Arquivos no disco sem linha. Não são renderizados: aqui nunca se cria linha."""

    elapsed_seconds: float = 0.0
    failures: list[IndexingFailure] = field(default_factory=list)
    """Arquivos que não puderam ser renderizados ou registrados.

    Ficam sem thumbnail e, por isso, são pegos de novo pela próxima execução.
    """

    def format_report(self) -> str:
        mode = "force" if self.force else "missing only"
        verb = "Would render" if self.dry_run else "Rendered"
        return "\n".join(
            [
                f"Thumbnail backfill finished ({mode}"
                f"{', dry run' if self.dry_run else ''})",
                "",
                f"Discovered:         {self.discovered:6d}",
                f"{verb + ':':<20}{self.written:6d}",
                f"Already generated:  {self.already_generated:6d}",
                f"Not indexed:        {self.not_indexed:6d}   (no row to attach to)",
                f"Failed:             {len(self.failures):6d}   "
                "(left for a later run)",
                f"Elapsed:            {self.elapsed_seconds:6.2f}s",
            ]
        )


class BackfillThumbnailsUseCase:
    """Renderiza e registra thumbnails das linhas indexadas de um fluxo de arquivos.

    **Dois modos.** O padrão renderiza só as linhas sem thumbnail -- tudo o que
    foi indexado antes do RFC-030, e toda imagem cuja thumbnail falhou durante
    a indexação. É idempotente: uma segunda execução sobre o mesmo disco não
    renderiza nada.

    `force=True` renderiza de novo toda linha indexada, substituindo o que está
    guardado. Serve para quando a própria renderização mudou --
    `thumbnail_max_edge` subiu, ou o adaptador aprendeu a tratar um formato que
    antes errava -- e nada mudou nas fotos, então nenhum scan perceberia.

    Ao contrário do `--force` do backfill de data de captura, não há resultado
    mais fraco a recusar: uma thumbnail que falha ao renderizar hoje é relatada
    e a guardada fica no lugar, porque a localização só é gravada para uma
    renderização bem-sucedida.
    """

    def __init__(
        self,
        repository: ImageRepository,
        thumbnail_writer: ThumbnailWriter,
        metadata_prefetch_size: int,
        force: bool = False,
        dry_run: bool = False,
    ) -> None:
        if metadata_prefetch_size < 1:
            raise ValueError(
                "metadata_prefetch_size must be at least 1, "
                f"got {metadata_prefetch_size}"
            )
        self._repository = repository
        self._writer = thumbnail_writer
        self._metadata_prefetch_size = metadata_prefetch_size
        self._force = force
        self._dry_run = dry_run

    def execute(self, candidates: Iterable[IndexCandidate]) -> ThumbnailBackfillSummary:
        """Percorre `candidates` em janelas, gravando uma vez por janela."""
        summary = ThumbnailBackfillSummary(force=self._force, dry_run=self._dry_run)
        started = time.perf_counter()

        for window in windowed(candidates, self._metadata_prefetch_size):
            summary.discovered += len(window)
            existing = self._repository.get_index_metadata_many(
                [candidate.image.id for candidate in window]
            )
            locations: dict[ImageId, tuple[IndexCandidate, str]] = {}

            for candidate in window:
                metadata = existing.get(candidate.image.id)
                if metadata is None:
                    summary.not_indexed += 1
                    continue
                if metadata.thumbnail_generated and not self._force:
                    summary.already_generated += 1
                    continue
                if self._dry_run:
                    summary.written += 1
                    continue

                try:
                    location = self._writer.write(candidate.image)
                except Exception as exc:
                    summary.failures.append(
                        IndexingFailure(
                            path=str(candidate.image.display_path), error=exc
                        )
                    )
                    continue
                locations[candidate.image.id] = (candidate, location)

            self._record(locations, summary)

        summary.elapsed_seconds = time.perf_counter() - started
        return summary

    def _record(
        self,
        locations: dict[ImageId, tuple[IndexCandidate, str]],
        summary: ThumbnailBackfillSummary,
    ) -> None:
        """Uma gravação em massa por janela; se falhar, linha a linha, isolando a má."""
        if not locations:
            return

        try:
            self._repository.update_thumbnail_path_many(
                {image_id: location for image_id, (_, location) in locations.items()}
            )
        except Exception:
            for image_id, (candidate, location) in locations.items():
                try:
                    self._repository.update_thumbnail_path(image_id, location)
                except Exception as exc:
                    summary.failures.append(
                        IndexingFailure(
                            path=str(candidate.image.display_path), error=exc
                        )
                    )
                    continue
                summary.written += 1
            return

        summary.written += len(locations)
