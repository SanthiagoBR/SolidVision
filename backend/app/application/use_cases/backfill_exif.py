"""Dá às imagens já indexadas os seus fatos EXIF, sem embedding (RFC-032 §8.1).

**Um backfill, não dois.** O RFC-028 §7 entregou um backfill de data de captura;
um backfill irmão de posição leria os mesmos bytes dos mesmos arquivos numa
segunda passada -- dois seeks por arquivo num disco externo giratório, para
preencher colunas que saem de um só cabeçalho. Este caso de uso grava os dois
fatos a partir da única leitura que o scan já faz (`read_exif_facts()`), e
substitui `BackfillCaptureDatesUseCase` em vez de ficar ao lado dele.

Continua sendo o scan de indexação com a inferência desligada: os mesmos
candidatos descobertos, a mesma leitura antecipada de metadados por janela e as
mesmas políticas de gravação -- `capture_date_to_write()` e `position_to_write()`
-- decidindo quais linhas podem ser gravadas. O que ele tira é tudo o que custa:
o hash, o modelo, a gravação do embedding.

**Os dois fatos são julgados de forma independente, sempre.** Cada um tem a sua
fonte guardada, a sua decisão e a sua gravação; um arquivo que perdeu a data
hoje mantém a posição, e vice-versa (RFC-032 §8.1).

Não precisa de modelo de embedding nem de hasher de conteúdo, e não recebe
nenhum dos dois -- a propriedade que
`tests/infrastructure/workers/test_exif_backfill.py` verifica no grafo de
imports.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import TypeVar

from app.application.use_cases.capture_date_plan import capture_date_to_write
from app.application.use_cases.index_or_update_images import (
    IndexingFailure,
    windowed,
)
from app.application.use_cases.indexing_plan import IndexCandidate
from app.application.use_cases.position_plan import position_to_write
from app.domain.repositories.image_repository import ImageRepository
from app.domain.value_objects.capture_date import CaptureDate
from app.domain.value_objects.capture_source import CaptureSource
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.position import PositionReading
from app.domain.value_objects.position_source import PositionSource


@dataclass
class FactTally:
    """O que um backfill fez com um fato -- a data de captura ou a posição."""

    written: int = 0
    """Linhas que receberam este fato -- ou, num dry run, as que receberiam."""

    written_by_source: dict[str, int] = field(default_factory=dict)
    """`written`, dividido pela fonte gravada: "que fração tem uma fonte real?"."""

    already_examined: int = 0
    """Linhas puladas porque um scan ou backfill anterior já olhou.

    Quase tudo numa segunda execução sem `--force`: o modo é idempotente, e
    este contador é como isso aparece.
    """

    kept_stronger: int = 0
    """Linhas do `--force` em que o arquivo agora lê mais fraco que o guardado.

    Mantidas, não sobrescritas. Uma contagem diferente de zero merece um olhar:
    dano nos arquivos, ou uma regressão no leitor.
    """

    def count(self, source: str) -> None:
        self.written += 1
        self.written_by_source[source] = self.written_by_source.get(source, 0) + 1


@dataclass
class ExifBackfillSummary:
    """O que uma execução do backfill achou e fez, como dados (RFC-024 §11)."""

    force: bool
    dry_run: bool
    discovered: int = 0
    dates: FactTally = field(default_factory=FactTally)
    positions: FactTally = field(default_factory=FactTally)

    not_indexed: int = 0
    """Arquivos no disco sem linha. O backfill preenche linhas; nunca as cria."""

    not_readable: int = 0
    """Arquivos que não abriram agora. Ficam sem exame; uma execução futura tenta."""

    elapsed_seconds: float = 0.0
    failures: list[IndexingFailure] = field(default_factory=list)

    def format_report(self) -> str:
        mode = "force" if self.force else "only-unknown"
        verb = "Would write" if self.dry_run else "Written"

        def by_source(tally: FactTally) -> str:
            return (
                ", ".join(
                    f"{source}={count}"
                    for source, count in sorted(tally.written_by_source.items())
                )
                or "none"
            )

        def row(label: str, read: Callable[[FactTally], int]) -> str:
            return f"{label:<19}{read(self.dates):>8d}{read(self.positions):>10d}"

        return "\n".join(
            [
                f"EXIF backfill finished ({mode}"
                f"{', dry run' if self.dry_run else ''})",
                "",
                f"Discovered:        {self.discovered:6d}",
                f"Not indexed:       {self.not_indexed:6d}   (no row to fill)",
                f"Not readable:      {self.not_readable:6d}   (left for a later run)",
                "",
                f"{'':<19}{'date':>8}{'position':>10}",
                row(verb + ":", lambda tally: tally.written),
                row("Already examined:", lambda tally: tally.already_examined),
                row("Kept stronger:", lambda tally: tally.kept_stronger),
                "",
                f"Dates by source:     {by_source(self.dates)}",
                f"Positions by source: {by_source(self.positions)}",
                "",
                f"Failed:            {len(self.failures):6d}",
                f"Elapsed:           {self.elapsed_seconds:6.2f}s",
            ]
        )


Fact = TypeVar("Fact", CaptureDate, PositionReading)
Source = CaptureSource | PositionSource


class BackfillExifUseCase:
    """Grava datas de captura e posições das linhas indexadas por trás de um scan.

    **Dois modos, e a diferença entre eles é o motivo de `NULL` e `'unknown'`
    serem valores diferentes** (RFC-028 §4.2, RFC-032 §4.4).

    O padrão -- `--only-unknown` -- examina só as linhas cuja fonte é NULL,
    para cada fato separadamente. Idempotente: uma segunda execução sobre o
    mesmo disco não grava nada.

    `force=True` reexamina também as linhas já examinadas, para uma única
    situação: a extração melhorou, então linhas marcadas `unknown` podem ter um
    valor agora, e nada mudou nesses arquivos para um scan perceber. Ele
    **nunca troca uma fonte guardada por uma mais fraca**, fato a fato: um
    `exif_original` sobrevive a um arquivo cuja data hoje lê `unknown`, e um
    `exif_gps` sobrevive a um arquivo cuja posição lê, cada um julgado por si.
    """

    def __init__(
        self,
        repository: ImageRepository,
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
        self._metadata_prefetch_size = metadata_prefetch_size
        self._force = force
        self._dry_run = dry_run

    def execute(self, candidates: Iterable[IndexCandidate]) -> ExifBackfillSummary:
        """Percorre `candidates` em janelas, gravando no máximo duas vezes em cada."""
        summary = ExifBackfillSummary(force=self._force, dry_run=self._dry_run)
        started = time.perf_counter()

        for window in windowed(candidates, self._metadata_prefetch_size):
            summary.discovered += len(window)
            existing = self._repository.get_index_metadata_many(
                [candidate.image.id for candidate in window]
            )
            dates: dict[ImageId, tuple[IndexCandidate, CaptureDate]] = {}
            positions: dict[ImageId, tuple[IndexCandidate, PositionReading]] = {}

            for candidate in window:
                metadata = existing.get(candidate.image.id)
                if metadata is None:
                    summary.not_indexed += 1
                    continue
                read_date = candidate.image.capture_date
                read_position = candidate.image.position_reading
                if read_date is None and read_position is None:
                    summary.not_readable += 1
                    continue

                if read_date is not None:
                    date = capture_date_to_write(
                        metadata.capture_source, read_date, force=self._force
                    )
                    if date is not None:
                        dates[candidate.image.id] = (candidate, date)
                    else:
                        self._tally_skip(
                            summary.dates, metadata.capture_source, read_date.source
                        )

                if read_position is not None:
                    position = position_to_write(
                        metadata.position_source, read_position, force=self._force
                    )
                    if position is not None:
                        positions[candidate.image.id] = (candidate, position)
                    else:
                        self._tally_skip(
                            summary.positions,
                            metadata.position_source,
                            read_position.source,
                        )

            self._write(
                dates,
                self._repository.update_capture_date_many,
                self._repository.update_capture_date,
                summary.dates,
                summary,
            )
            self._write(
                positions,
                self._repository.update_position_many,
                self._repository.update_position,
                summary.positions,
                summary,
            )

        summary.elapsed_seconds = time.perf_counter() - started
        return summary

    def _tally_skip(
        self, tally: FactTally, stored: Source | None, read: Source
    ) -> None:
        """Diz por que um fato não foi gravado: mantido mais forte, ou já examinado."""
        if self._force and stored is not None and read.precedence < stored.precedence:
            tally.kept_stronger += 1
        else:
            tally.already_examined += 1

    def _write(
        self,
        to_write: dict[ImageId, tuple[IndexCandidate, Fact]],
        write_many: Callable[[Mapping[ImageId, Fact]], None],
        write_one: Callable[[ImageId, Fact], None],
        tally: FactTally,
        summary: ExifBackfillSummary,
    ) -> None:
        """Uma gravação em massa por janela e fato; em caso de falha, linha a linha.

        A mesma troca que toda gravação em massa do pipeline faz (RFC-024
        §7.2): tentar o lote e, quando ele falha, pagar o preço sequencial uma
        vez, para que uma linha ruim custe um fato e não a janela inteira.
        """
        if not to_write:
            return
        if self._dry_run:
            for _, fact in to_write.values():
                tally.count(fact.source.value)
            return

        try:
            write_many({image_id: fact for image_id, (_, fact) in to_write.items()})
        except Exception:
            for image_id, (candidate, fact) in to_write.items():
                try:
                    write_one(image_id, fact)
                except Exception as exc:
                    summary.failures.append(
                        IndexingFailure(
                            path=str(candidate.image.display_path), error=exc
                        )
                    )
                    continue
                tally.count(fact.source.value)
            return

        for _, fact in to_write.values():
            tally.count(fact.source.value)
