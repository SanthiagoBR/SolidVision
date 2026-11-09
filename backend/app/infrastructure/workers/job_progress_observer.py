"""O adaptador que transforma callbacks do pipeline em linhas de job (RFC-029).

`IndexingObserver` é uma porta do Domínio que não sabe nada de jobs. Esta é a
metade que não sabe de mais nada: guarda um id de job, grava os contadores,
mantém o heartbeat andando e lê de volta se alguém pediu para a execução parar.

Duas políticas moram aqui e em nenhum outro lugar.

**Quando gravar.** O fim de uma janela de prefetch e o fim de um lote são
gravados toda vez -- isso já é no máximo uma escrita a cada 512 arquivos ou a
cada 8 imagens, a taxa que o RFC-029 §7.3 escolheu. O callback do scan dispara
por arquivo, então esse é limitado por tempo.

**Quando parar.** `should_stop()` nunca consulta: responde a partir do que a
última escrita leu de volta. Isso funciona porque a escrita acontece a cada
lote e a cada janela, que é exatamente onde o RFC-029 §8 diz que a flag deve
ser observada, então a resposta nunca tem mais de um lote de atraso.

E um repasse que não grava nada aqui: a listagem de um disco inteiro, que
`listed()` entrega a qualquer função que o runner lhe tenha dado -- o runner
conhece o dispositivo e onde ele foi montado, e este objeto não conhece nenhum
dos dois (RFC-039 §4.1; desde o RFC-046, a própria listagem, não o tamanho).
"""

from __future__ import annotations

import datetime
import time
from collections.abc import Callable

from app.domain.repositories.indexing_job_repository import IndexingJobRepository
from app.domain.services.indexing_observer import IndexingObserver
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.indexing_progress import IndexingProgress
from app.domain.value_objects.job_id import JobId
from app.domain.value_objects.root_listing import RootListing
from app.infrastructure.logging.logger import get_logger

logger = get_logger(__name__)


class JobProgressObserver(IndexingObserver):
    """Reporta o progresso de uma execução na linha do job e repassa o cancelamento."""

    def __init__(
        self,
        repository: IndexingJobRepository,
        job_id: JobId,
        clock: Callable[[], datetime.datetime],
        heartbeat_interval: float,
    ) -> None:
        self._repository = repository
        self._job_id = job_id
        self._clock = clock
        self._heartbeat_interval = heartbeat_interval

        self._scanned = 0
        self._scan_complete = False
        self._pipeline = IndexingProgress()
        self._checkpoint: ImagePath | None = None
        self._last_write = 0.0
        self._on_listed: Callable[[RootListing], None] | None = None

        self.cancelled = False
        """Se a rota pediu para este job parar."""

        self.lost = False
        """Se este job deixou de ser nosso -- o reaper o tomou de volta.

        Distinto de `cancelled` porque os desfechos diferem: um job cancelado
        cabe a nós marcar `cancelled`, e um perdido não pode ser gravado de
        jeito nenhum, porque outro worker pode já estar rodando-o.
        """

    def discovered(self, files_seen: int, complete: bool) -> None:
        """Registra até onde o scan chegou, no máximo uma vez por heartbeat.

        O callback que mantém um job vivo quando nada mais acontece: um novo
        scan de um disco indexado não completa nenhum lote, e uma retomada
        pula aqui a maioria dos arquivos sem entregar nenhum (RFC-029 §9.1).
        Limitado por tempo porque dispara por arquivo, e uma escrita por
        arquivo seriam 40.000 escritas para acompanhar 40.000 arquivos.

        `complete` sempre grava: é o momento em que `discovered_files` deixa
        de ser uma contagem corrente e vira um total, a única coisa de que um
        cliente precisa para mostrar uma porcentagem em vez de "scanning
        N files".
        """
        self._scanned = files_seen
        self._scan_complete = complete
        if complete or self._heartbeat_is_due():
            self._write()

    def relay_listing_to(self, record: Callable[[RootListing], None]) -> None:
        """Envia a listagem do disco inteiro para `record`.

        Dada pelo runner depois que ele resolveu o dispositivo e o ponto de
        montagem, o que acontece depois de este objeto existir: o runner
        constrói o observador primeiro para que toda saída de uma execução
        possa ler `lost`. Só um job sem escopos recebe uma, e só o scan de um
        job assim chama `listed()`. O `_record_scan()` do runner grava o
        denominador a partir dela e a guarda para a varredura no fim do job
        (RFC-046 §5.3).
        """
        self._on_listed = record

    def listed(self, listing: RootListing) -> None:
        """Repassa a listagem, e engole o que der errado com ela.

        **Nunca levanta exceção**, pelo motivo que `_write()` dá: isto roda
        dentro do gerador da descoberta, e uma exceção ali o fecharia e
        truncaria o scan em silêncio. Um disco cuja porcentagem não pôde ser
        registrada é um disco sem porcentagem; um job interrompido por isso
        seria um job que se diz completo com metade do disco sem ler.
        """
        record = self._on_listed
        if record is None:
            return
        try:
            record(listing)
        except Exception:
            logger.warning(
                "Could not record the scan of job %s's device; the run continues",
                self._job_id,
                exc_info=True,
            )

    def window_decided(
        self, progress: IndexingProgress, durable_through: ImagePath | None
    ) -> None:
        self._record(progress, durable_through)

    def batch_persisted(
        self, progress: IndexingProgress, durable_through: ImagePath | None
    ) -> None:
        self._record(progress, durable_through)

    def should_stop(self) -> bool:
        """Se esta execução deve se encerrar no próximo ponto seguro."""
        return self.cancelled or self.lost

    def _record(
        self, progress: IndexingProgress, durable_through: ImagePath | None
    ) -> None:
        """Pega os contadores e o checkpoint do pipeline, e os grava.

        Um `durable_through` igual a `None` é mantido em vez de gravado:
        significa que o buffer do lote ainda guarda o arquivo não decidido
        mais antigo, então o checkpoint anterior é o ponto mais adiante de
        onde é de fato seguro retomar. Sobrescrevê-lo com `None` jogaria fora
        uma posição de que um crash ainda precisa.
        """
        self._pipeline = progress
        if durable_through is not None:
            self._checkpoint = durable_through
        self._write()

    def _heartbeat_is_due(self) -> bool:
        return (time.monotonic() - self._last_write) >= self._heartbeat_interval

    def _merged(self) -> IndexingProgress:
        """Combina o que o scan sabe com o que o pipeline sabe.

        Duas fontes, porque nenhuma tem o quadro inteiro. O scan é o único
        que sabe quantos arquivos existem e se terminou de procurar; o
        pipeline é o único que sabe quantos deles receberam embedding, foram
        pulados ou falharam. O scan sempre corre à frente do pipeline, então a
        contagem dele é a maior das duas -- mas `max` em vez de uma atribuição
        simples, porque um contador que um cliente está acompanhando nunca
        pode andar para trás.
        """
        return IndexingProgress(
            discovered_files=max(self._scanned, self._pipeline.discovered_files),
            processed_images=self._pipeline.processed_images,
            skipped_images=self._pipeline.skipped_images,
            failed_images=self._pipeline.failed_images,
            discovery_complete=self._scan_complete,
        )

    def _write(self) -> None:
        """Grava o progresso, e sabe pela mesma chamada se deve parar.

        **Nunca levanta exceção**, e o motivo é específico, não cautela
        genérica: esses callbacks rodam dentro do consumo preguiçoso do
        gerador da descoberta, e uma exceção levantada ali fecha o gerador e
        trunca o scan em silêncio -- a mesma falha que
        `discovered_candidates()` já documenta. Um job que não consegue
        reportar o progresso deve terminar um pouco desatualizado, e não parar
        no meio do caminho e se dizer completo.
        """
        self._last_write = time.monotonic()
        try:
            stored = self._repository.record_progress(
                self._job_id, self._merged(), self._checkpoint, self._clock()
            )
        except Exception:
            logger.warning(
                "Could not record progress for job %s; the run continues",
                self._job_id,
                exc_info=True,
            )
            return

        if stored is None:
            # A linha não está mais `running`: o reaper concluiu que este
            # worker estava morto e devolveu o job à fila, ou o encerrou.
            # Outro worker pode já estar com ele.
            self.lost = True
            logger.warning(
                "Job %s is no longer assigned to this worker; winding up",
                self._job_id,
            )
            return

        if stored.cancel_requested and not self.cancelled:
            self.cancelled = True
            logger.info("Job %s was asked to stop; finishing the batch", self._job_id)
