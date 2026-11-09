r"""O processo que roda os jobs de indexação, e o reaper que limpa depois dele.

    python -m app.infrastructure.workers.job_runner

**Um processo do host, não um contêiner, e isso não é provisório.** O RFC-029
§6 propôs um segundo serviço no `docker-compose.yml`. Isso não funciona aqui,
e seria errado se funcionasse: não há imagem do backend;
`WindowsVolumeIdentityProvider` se recusa a ser construído fora de `win32`; e,
mesmo com um bind mount, um contêiner vê um caminho montado, e não o
`\\?\Volume{GUID}\` sobre o qual repousa todo o esquema de identidade do
RFC-027. O produto é entregue como um processo nativo do Windows com
instalador, só com o PostgreSQL num contêiner -- então um executor Linux
perderia também a detecção de hot-plug, já que os bind mounts são fixados
quando o contêiner inicia.

A consequência para este arquivo: **nada nele sabe em que plataforma está.**
A reivindicação, o reaper, o checkpoint e o heartbeat são trabalho comum de
banco. A única coisa com cara de Windows fica atrás do `DeviceLocator`, então
uma futura implantação em Linux é um novo adaptador de volume, e não uma
reescrita.

Três pontos de entrada, um caminho de código:

* `JobRunner.run_forever()` -- o laço de polling, que é o executor;
* `JobRunner.run_claimed()` -- tudo o que acontece a um job reivindicado;
* `JobRunner.run_job()` -- reivindica *este* job e o roda, que é o que a CLI
  usa para que `indexing_worker --root PATH` não precise de um segundo
  processo rodando (RFC-029 §12).
"""

from __future__ import annotations

import argparse
import datetime
import time
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path, PurePath

from sqlalchemy.orm import Session

from app.application.use_cases.index_or_update_images import (
    IndexingFailure,
    IndexingSummary,
    IndexOrUpdateImagesUseCase,
)
from app.application.use_cases.indexing_plan import IndexCandidate
from app.application.use_cases.sweep_missing_images import SweepMissingImagesUseCase
from app.domain.entities.device import Device
from app.domain.entities.indexing_job import IndexingJob, JobStatus
from app.domain.repositories.device_repository import DeviceRepository
from app.domain.repositories.image_repository import ImageRepository
from app.domain.repositories.indexing_job_repository import IndexingJobRepository
from app.domain.services.device_locator import DeviceLocator
from app.domain.services.indexing_observer import IndexingObserver
from app.domain.value_objects.job_id import JobId
from app.domain.value_objects.job_scope import JobScope
from app.domain.value_objects.root_listing import RootListing
from app.infrastructure.filesystem.filesystem_image_provider import (
    FilesystemImageProvider,
)
from app.infrastructure.logging.logger import get_logger
from app.infrastructure.process.interrupts import deferred, deferring, install
from app.infrastructure.process.supervision import (
    SUPERVISED_FLAG,
    refuse_to_start,
    stop_on_stdin_eof,
)
from app.infrastructure.workers.indexing_worker import (
    discovered_candidates,
    listed_identity,
)
from app.infrastructure.workers.job_progress_observer import JobProgressObserver
from app.infrastructure.workers.read_ahead import ReadAheadCandidates, ReadAheadLimits

logger = get_logger(__name__)

SYSTEM_DIRECTORIES: tuple[str, ...] = ("$RECYCLE.BIN", "System Volume Information")
"""As pastas da raiz de um volume que nenhum scan lista (RFC-046 §5.6).

A lixeira, primeiro: o Explorer apaga uma foto movendo-a para lá, e um scan do
disco inteiro então a indexava como foto nova -- 1.268 linhas do `E:` no banco
de desenvolvimento, e os arquivos de índice `$I...`, que carregam a extensão
do arquivo apagado, falhando como fotos (RFC-045 §14.10). Com a exclusão, uma
foto apagada pelo Explorer sai da busca no próximo scan, como uma apagada de
vez; e restaurá-la a põe de volta onde estava, onde o próximo scan a desmarca.
`System Volume Information` é a outra pasta que o Windows mantém na raiz de
todo volume, e também não é do usuário.

Juntadas ao ponto de montagem a cada job. A exclusão compara caminhos, com a
regra de caixa da plataforma, então o `$Recycle.Bin` do `C:` é a mesma pasta.
"""


def utc_now() -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.UTC)


class _KeptListing:
    """A listagem do disco inteiro, guardada por `_record_scan()` para a varredura.

    Uma por job, preenchida de dentro do scan e lida depois que o pipeline
    terminou. `mount` é onde o disco estava quando foi listado: a varredura só
    roda se ele ainda estiver lá (RFC-046 §5.4).
    """

    def __init__(self) -> None:
        self.listing: RootListing | None = None
        self.mount: Path | None = None


class JobRunner:
    """Reivindica jobs da fila, os roda e resolve os que ninguém está rodando.

    Dependências injetadas de ponta a ponta, inclusive o relógio. O relógio é
    o que torna o reaper testável: a tabela de validação do RFC-029 pede um
    worker que morreu no meio de um job, e o jeito honesto de testar isso é
    avançar o tempo em vez de matar um processo -- o que no Windows
    significaria `subprocess` e um sinal que lá não existe.
    """

    def __init__(
        self,
        jobs: IndexingJobRepository,
        devices: DeviceRepository,
        locator: DeviceLocator,
        indexer: IndexOrUpdateImagesUseCase,
        supported_extensions: Iterable[str],
        extract_capture_date: bool = True,
        clock: Callable[[], datetime.datetime] = utc_now,
        poll_interval: float = 2.0,
        heartbeat_interval: float = 10.0,
        stale_timeout: float = 120.0,
        max_attempts: int = 3,
        sleep: Callable[[float], None] = time.sleep,
        warm_up: Callable[[], None] | None = None,
        excluded_directories: tuple[Path, ...] = (),
        extract_gps: bool = True,
        read_ahead: ReadAheadLimits | None = None,
        images: ImageRepository | None = None,
    ) -> None:
        if read_ahead is not None and images is None:
            raise ValueError(
                "Reading ahead needs the image repository, for the snapshot of "
                "the device's change signals"
            )
        self._jobs = jobs
        self._devices = devices
        self._locator = locator
        self._indexer = indexer
        self._supported_extensions = tuple(supported_extensions)
        self._extract_capture_date = extract_capture_date
        self._extract_gps = extract_gps
        """`settings.extract_gps`, entregue a todo scan (RFC-032 §4.2).

        Por último na assinatura, como keyword, para que os chamadores e
        testes do RFC-029 que passam argumentos por posição não se mexam.
        """
        self._clock = clock
        self._poll_interval = poll_interval
        self._heartbeat_interval = heartbeat_interval
        self._stale_timeout = stale_timeout
        self._max_attempts = max_attempts
        self._sleep = sleep
        self._excluded_directories = excluded_directories
        """Pastas de onde nenhum scan pode entregar arquivos -- o cache de thumbnails.

        Veja `FilesystemImageProvider`: sem isto, um job sobre um disco de
        sistema indexaria as próprias thumbnails do aplicativo como fotografias
        (RFC-030 §7.1).
        """
        self._read_ahead = read_ahead
        """Se, e quanto, cada job lê os arquivos adiantado (RFC-045).

        `None` é o pipeline do RFC-024, uma thread, cada arquivo lido pelo
        caminho -- o que `INDEXING_READ_AHEAD=false` dá, e todo teste escrito
        antes do RFC-045. Por último na assinatura junto com `images`, como
        keywords, para que os argumentos de nenhum chamador se mexam.
        """
        self._images = images
        """De onde o snapshot dos sinais de mudança de um dispositivo é lido.

        O repositório pelo qual o indexador grava, na mesma sessão: o snapshot
        é lido nesta thread, antes de a thread de leitura começar, e nunca a
        partir dela (RFC-045 §5.2).

        Desde o RFC-046, também onde um job do disco inteiro marca, no fim, as
        fotos que não achou. Sem ele -- todo teste escrito antes, a menos que
        peça -- nenhum job faz a varredura.
        """
        self._sweeper = (
            SweepMissingImagesUseCase(images, clock) if images is not None else None
        )
        self._warm_up = warm_up
        """Carrega o modelo antes de o laço começar; veja `run_forever()`.

        Guardado pelo runner em vez de deixado para a raiz de composição
        lembrar, porque a ordem é uma regra de correção, não uma conveniência:
        um worker que reivindicasse um job primeiro o seguraria, calado, pelos
        vários segundos que o checkpoint do CLIP leva para carregar, e com um
        stale timeout curto poderia ser recolhido pelo reaper antes de ter
        feito qualquer coisa (RFC-029 §9.1).
        """
        self._interrupted = False
        """Ligado quando uma interrupção caiu dentro de um job; o laço então termina.

        `run_claimed()` captura a interrupção para devolver o job, então, sem
        isto, o laço seguiria e reivindicaria o mesmo job de novo na próxima
        volta: um executor a quem se pediu para parar manteria o job `running`,
        e o launcher do RFC-036 o encerraria no prazo (§7.2).
        """

    # ------------------------------------------------------------------
    # O laço

    def run_forever(self) -> None:
        """Recolhe, reivindica, roda, repete -- até o operador parar o processo.

        O reaper roda entre um poll e outro, e não num timer próprio, que é o
        arranjo que o RFC-029 §9.1 descreve: é o mesmo processo, fazendo a
        consulta barata enquanto não tem mais nada a fazer. Um executor,
        portanto, limpa o que uma encarnação anterior dele mesmo deixou, que é
        o caso comum -- a máquina foi reiniciada.
        """
        self.warm_up_now()
        logger.info("Job executor started")
        try:
            while not self._interrupted:
                self.reap()
                if not self.claim_and_run():
                    self._sleep(self._poll_interval)
        except KeyboardInterrupt:
            # Chegar aqui significa que a interrupção caiu entre dois jobs,
            # então não há nada a devolver. Uma interrupção *durante* um job
            # é capturada em `run_claimed()`, que o devolve à fila e encerra o
            # laço por meio de `_interrupted`.
            pass
        logger.info("Job executor stopped by the operator")

    def warm_up_now(self) -> None:
        """Carrega o modelo agora, sem entrar no laço de polling.

        `run_forever()` chama isto primeiro. `--once` também chama, porque uma
        medição da latência de partida que pulasse a carga estaria medindo um
        processo diferente do que roda de verdade.
        """
        if self._warm_up is not None:
            logger.info("Loading the embedding model before polling for work")
            self._warm_up()

    def run_job(self, job_id: JobId) -> IndexingJob | None:
        """Reivindica um job nomeado e o roda aqui, ou reporta que outro o tem.

        O que `indexing_worker --root PATH` chama, para que a CLI execute o
        job no próprio processo em vez de esperar um executor que talvez
        ninguém tenha iniciado (RFC-029 §12). É a mesma reivindicação e a
        mesma execução do laço de polling -- um caminho de código, e nunca é
        preciso um segundo processo.

        Devolve `None` quando o job não podia ser reivindicado, o que não é
        falha: um executor separado chegou primeiro, e quem chama acompanha
        esse job por polling.
        """
        claimed = self._jobs.claim(job_id, self._clock())
        if claimed is None:
            return None
        return self.run_claimed(claimed)

    def claim_and_run(self) -> bool:
        """Pega o job mais antigo da fila e o roda; reporta se havia algum."""
        claimed = self._jobs.claim_next(self._clock())
        if claimed is None:
            return False
        self.run_claimed(claimed)
        return True

    # ------------------------------------------------------------------
    # O reaper

    def reap(self) -> list[IndexingJob]:
        """Resolve todo job rodando cujo worker parou de provar que está vivo.

        A consulta está aqui, e o *veredito* não: `IndexingJob.expire()`
        decide entre devolver à fila, cancelar e falhar, porque essa é uma
        regra sobre o que um job é. Isto aplica o veredito por meio de uma
        escrita condicional, então um job que emitiu um heartbeat entre a
        consulta e a escrita é deixado em paz -- o worker que o segura está
        vivo, afinal, e tomá-lo poria dois workers num disco só.

        Devolve o que de fato mudou, para que quem chama (e um teste) possa
        ver isso em vez de deduzi-lo de uma linha de log.
        """
        cutoff = self._clock() - datetime.timedelta(seconds=self._stale_timeout)
        resolved: list[IndexingJob] = []

        for abandoned in self._jobs.list_stale(cutoff):
            verdict = abandoned.expire(self._clock(), self._max_attempts)
            written = self._jobs.save_if_status(verdict, JobStatus.RUNNING)
            if written is None:
                continue
            resolved.append(written)
            logger.warning(
                "Job %s had no heartbeat since %s; it is now %s (attempt %d of %d)",
                abandoned.id,
                abandoned.last_heartbeat_at,
                written.status.value,
                written.attempts,
                self._max_attempts,
            )
        return resolved

    # ------------------------------------------------------------------
    # Um job

    def run_claimed(self, job: IndexingJob) -> IndexingJob | None:
        """Roda um job que este runner já reivindicou, e grava o desfecho.

        Toda saída daqui grava um estado terminal ou devolve o job, então um
        job reivindicado nunca fica `running` por causa de algo que este
        processo fez. O único caso que o deixa `running` é aquele que nada
        consegue gravar: o processo morrer, que é para o que serve o reaper.
        """
        logger.info("Running job %s on device %s", job.id, job.device_id)
        observer = JobProgressObserver(
            self._jobs, job.id, self._clock, self._heartbeat_interval
        )
        kept = _KeptListing()

        try:
            summary = self._index(job, observer, kept)
        except KeyboardInterrupt:
            # Um operador parando o processo não é um crash, então nenhuma
            # tentativa é consumida -- do contrário, três reinícios comuns
            # fariam falhar para sempre um job que nunca deu errado.
            logger.info("Job %s interrupted; returning it to the queue", job.id)
            self._no_sweep(job, "the job was interrupted")
            self._interrupted = True
            return self._finish(job, lambda current: current.release(self._clock()))
        except Exception as error:
            # A mensagem é tirada aqui, e não dentro da lambda: o Python
            # desvincula um nome de `except ... as` quando o bloco termina,
            # então uma closure sobre ele é um `NameError` latente, esperando
            # o dia em que a chamada sair do bloco.
            reason = str(error)
            logger.exception("Job %s failed", job.id)
            self._no_sweep(job, "the job failed")
            return self._finish(
                job, lambda current: current.fail(self._clock(), reason)
            )

        if observer.lost:
            # O reaper deu este job a outro enquanto trabalhávamos. Gravar
            # qualquer coisa agora sobrescreveria o que aquele worker fez
            # desde então.
            logger.warning("Job %s was taken over; leaving it alone", job.id)
            self._no_sweep(job, "the job was taken over by another executor")
            return None

        for failure in summary.failures:
            logger.error("Failed to index %s", failure.path, exc_info=failure.error)
        for failure in summary.thumbnail_failures:
            logger.warning(
                "Indexed %s without a thumbnail", failure.path, exc_info=failure.error
            )
        logger.info("%s", summary.format_report())

        if summary.stopped:
            self._no_sweep(job, "the job was cancelled")
            return self._finish(job, lambda current: current.cancel(self._clock()))

        self._sweep_missing(job, kept)
        return self._finish(job, self._completion_of(job))

    # ------------------------------------------------------------------
    # As fotos que não estão mais no disco (RFC-046)

    def _sweep_missing(self, job: IndexingJob, kept: _KeptListing) -> None:
        """Marca as linhas que a listagem não achou, e desmarca as que ela achou.

        Aqui, depois de o pipeline terminar e antes de o job ser concluído, e
        **só se quatro coisas valerem** (RFC-046 §5.4): o job é do disco
        inteiro; a listagem dele foi guardada; o pipeline terminou sem
        exceção, cancelamento ou interrupção, e o job ainda é deste runner --
        os retornos antecipados de `run_claimed()` cuidam disso; e o disco está
        **no ponto de montagem em que foi listado**. `_completion_of()` só
        pergunta se o disco está conectado; para isto, um disco de volta sob
        outra letra é outro disco, pelo motivo de `_record_scan()`. Quando uma
        falha, nada é marcado e uma linha de INFO diz qual.

        **Uma varredura que falha não faz o job falhar.** A indexação está
        feita, e o próximo job do disco inteiro varre de novo; o erro vai para
        o log em ERROR, como `_record_scan()` faz com o denominador.

        Nenhum outro escritor pode se meter entre a listagem e isto: o runner
        roda um job por vez, e a purga recusa um disco com job.
        """
        if job.scopes:
            self._no_sweep(job, "the job is of some folders")
            return
        if self._sweeper is None:
            self._no_sweep(job, "this runner has no image repository")
            return
        listing = kept.listing
        if listing is None:
            self._no_sweep(job, "the listing of the disk was not kept")
            return
        device = self._devices.get(job.device_id)
        mounted = self._locator.mount_point(device) if device is not None else None
        if mounted is None or mounted != kept.mount:
            self._no_sweep(
                job,
                f"the disk was listed at {kept.mount} and is now "
                + (f"at {mounted}" if mounted is not None else "not connected"),
            )
            return

        started = time.perf_counter()
        try:
            listed = {listed_identity(job.device_id, path)[1] for path in listing.files}
            unreadable = tuple(JobScope(folder) for folder in listing.unreadable)
            report = self._sweeper.execute(job.device_id, listed, unreadable)
        except Exception:
            logger.exception(
                "The sweep of device %s failed; job %s is not failed by it",
                job.device_id,
                job.id,
            )
            return

        logger.info(
            "Device %s: %d photos marked as not found, %d found again, %d kept "
            "visible by unreadable folders (%d listed, %.2f s)",
            job.device_id,
            report.marked,
            report.unmarked,
            report.protected,
            listing.file_count,
            time.perf_counter() - started,
        )
        for folder, protected in report.protected_by_folder:
            logger.warning(
                "Device %s: the folder %s could not be read; %d of its photos "
                "stay visible",
                job.device_id,
                str(folder) or "(the root)",
                protected,
            )

    def _no_sweep(self, job: IndexingJob, reason: str) -> None:
        """Diz, em INFO, qual condição da varredura falhou (RFC-046 §5.4).

        Um job de algumas pastas sempre diz isso, aconteça o que mais lhe tiver
        acontecido: essa é a primeira condição, e a que decide a questão.
        """
        if job.scopes:
            reason = "the job is of some folders"
        logger.info("Job %s marks no photo as not found: %s", job.id, reason)

    def _completion_of(self, job: IndexingJob) -> Callable[[IndexingJob], IndexingJob]:
        """Decide entre `completed` e `failed` olhando o disco de novo.

        **Um scan cujo volume sumiu no meio do caminho não levanta exceção.**
        `discover()` retorna em silêncio quando a raiz some, e um `rglob`
        sobre um volume que desapareceu pode simplesmente parar de produzir
        arquivos -- então, sem esta verificação, o job seria marcado
        `completed` com metade do escopo indexada, o pior desfecho possível,
        porque parece sucesso (RFC-029 §15).

        O ponto de montagem é comparado, e não só enumerado de novo: um disco
        que foi desconectado e conectado de volta sob outra letra não é o
        disco que esta execução estava lendo, e os arquivos a que ela não
        chegou não são os arquivos a que ela teria chegado.
        """
        device = self._devices.get(job.device_id)
        mount = self._locator.mount_point(device) if device is not None else None

        def decide(current: IndexingJob) -> IndexingJob:
            if device is None or mount is None:
                return current.fail(
                    self._clock(),
                    "The device was disconnected before the scan finished; "
                    "the work already done is kept and the next job will "
                    "skip it.",
                )
            return current.complete(self._clock())

        return decide

    def _index(
        self, job: IndexingJob, observer: JobProgressObserver, kept: _KeptListing
    ) -> IndexingSummary:
        """Monta o pipeline de um job e o roda.

        A conexão é validada de novo *aqui*, depois da reivindicação, porque
        um disco que estava conectado quando o job foi criado pode estar numa
        gaveta quando o job sai da fila (RFC-029 §7.1). Uma pasta também: um
        escopo que não existe mais é recusado, em vez de varrido para nada,
        porque "indexou zero arquivos" e "aquela pasta sumiu" são respostas
        diferentes.
        """
        device = self._devices.get(job.device_id)
        if device is None:
            raise RuntimeError(f"Device {job.device_id} is no longer known.")

        mount = self._locator.mount_point(device)
        if mount is None:
            raise RuntimeError(
                f"Device {device.label!r} is not connected. Plug it in and "
                "create the job again."
            )

        for scope in job.scopes:
            if self._locator.resolve_scope(device, scope) is None:
                raise RuntimeError(
                    f"{str(scope)!r} is no longer a folder on device "
                    f"{device.label!r}."
                )

        if not job.scopes:
            # A listagem do disco inteiro é o denominador dele (RFC-039 §4.1),
            # e aquilo com que a varredura compara as linhas no fim (RFC-046
            # §5.3). Um job de algumas pastas não registra nada: duas pastas
            # não são o disco.
            observer.relay_listing_to(
                lambda listing: self._record_scan(device, mount, listing, kept)
            )

        # O cache de thumbnails, e as pastas de sistema da raiz deste volume.
        excluded = self._excluded_directories + tuple(
            mount / name for name in SYSTEM_DIRECTORIES
        )

        def scan(scan_observer: IndexingObserver) -> Iterator[IndexCandidate]:
            provider = FilesystemImageProvider(
                mount,
                self._supported_extensions,
                extract_capture_date=self._extract_capture_date,
                extract_gps=self._extract_gps,
                scopes=job.scopes,
                resume_after=_checkpoint_of(job),
                observer=scan_observer,
                excluded_directories=excluded,
            )
            return self._candidates(provider, device, mount)

        if self._read_ahead is None:
            return self._indexer.execute(scan(observer), observer)
        return self._index_reading_ahead(device, scan, observer, self._read_ahead)

    def _index_reading_ahead(
        self,
        device: Device,
        scan: Callable[[IndexingObserver], Iterator[IndexCandidate]],
        observer: JobProgressObserver,
        limits: ReadAheadLimits,
    ) -> IndexingSummary:
        """Roda o pipeline sobre um scan que uma segunda thread lê adiantado (RFC-045).

        **O snapshot primeiro, aqui**, na thread dona da sessão: uma consulta
        pelo tamanho e o mtime de cada linha do dispositivo, que a thread de
        leitura consulta para ler só os arquivos de que a decisão vai calcular
        o hash (§5.2). A thread nunca consulta o banco.

        **O scan é montado na thread**, com um observador que põe os eventos
        dele na fila; esta thread os entrega a `observer` à medida que os tira
        de lá, para que toda escrita que o scan dispara -- o heartbeat de
        `discovered()`, a linha do dispositivo de `listed()` -- aconteça na
        thread dona da sessão do job (§5.4).

        O bloco `with` é o que encerra a thread, em toda saída do pipeline: o
        fim do scan, um cancelamento, uma exceção, uma interrupção (§5.1).
        """
        assert self._images is not None
        started = time.perf_counter()
        signals = self._images.list_change_signals(device.id)
        logger.info(
            "Read the change signals of %d indexed files of device %s in %.2f s",
            len(signals),
            device.id,
            time.perf_counter() - started,
        )

        with ReadAheadCandidates(scan, observer, signals, limits) as candidates:
            summary = self._indexer.execute(candidates, observer)
        stats = candidates.stats
        logger.info(
            "Read ahead: %d files (%.1f MB), %d unchanged, %d too large, "
            "%d over the budget, %d unreadable; at most %.1f MB held",
            stats.read,
            stats.bytes_read / 1e6,
            stats.unchanged,
            stats.too_large,
            stats.over_budget,
            stats.failed,
            candidates.budget.peak / 1e6,
        )
        return summary

    def _record_scan(
        self,
        device: Device,
        started_on: Path,
        listing: RootListing,
        kept: _KeptListing,
    ) -> None:
        """Grava o tamanho da listagem do disco inteiro, se o disco ainda está lá.

        Chamado de dentro do scan, quando a listagem da raiz termina e antes de
        o primeiro arquivo ser lido. **O ponto de montagem é comparado
        primeiro**, pelo motivo que `_completion_of()` dá: uma listagem sobre
        um volume que desapareceu pode simplesmente parar, sem erro, e a
        contagem dela seria um denominador errado com toda a confiança. Um
        disco que não está montado onde o job começou -- sumiu, ou voltou sob
        outra letra -- não é o disco que foi listado, e nada é gravado.

        `record_scan()` grava as duas colunas, e não a linha: a cópia do
        dispositivo guardada aqui é do início do job, e um renome feito desde
        então precisa ficar (RFC-039 §4.1). O que ele levantar cabe ao
        observador engolir, com um WARNING; o job segue.

        **A mesma verificação decide se a listagem é guardada** para a
        varredura no fim do job (RFC-046 §5.3), e ela é guardada antes da
        escrita: um denominador que não pôde ser gravado não é motivo para não
        comparar as linhas.
        """
        mounted = self._locator.mount_point(device)
        if mounted != started_on:
            logger.warning(
                "The scan of device %s is not recorded: it was listed at %s, and "
                "the disk is now %s",
                device.id,
                started_on,
                f"at {mounted}" if mounted is not None else "not connected",
            )
            return
        kept.listing = listing
        kept.mount = started_on
        self._devices.record_scan(device.id, listing.file_count, self._clock())
        logger.info(
            "Device %s: the listing found %d supported files",
            device.id,
            listing.file_count,
        )

    def _candidates(
        self,
        provider: FilesystemImageProvider,
        device: Device,
        mount: Path,
    ) -> Iterator[IndexCandidate]:
        """Entrega os arquivos descobertos como candidatos, exatamente como a CLI.

        `discovered_candidates()` é importada, e não reimplementada: é onde o
        `ImageId` é gerado, e uma segunda cópia seria uma segunda chance de
        calcular um id diferente para o mesmo arquivo (RFC-029 §12 -- um
        caminho de código de indexação, não dois).
        """
        undiscoverable: list[IndexingFailure] = []
        yield from discovered_candidates(provider, device, mount, undiscoverable)
        for failure in undiscoverable:
            logger.error("Could not read %s", failure.path, exc_info=failure.error)

    def _finish(
        self,
        job: IndexingJob,
        transition: Callable[[IndexingJob], IndexingJob],
    ) -> IndexingJob | None:
        """Aplica uma transição terminal ao job **como ele está agora**.

        Lido de novo, em vez de transicionado a partir da cópia reivindicada
        minutos atrás, porque a linha mudou desde então: o observador vem
        gravando nela contadores e um checkpoint, e a rota pode ter ligado
        `cancel_requested`. Gravar a cópia velha apagaria o progresso da
        execução que acabou de terminar.

        Condicionado a a linha ainda estar `running`, para que um job que o
        reaper tomou de volta no último instante não seja sobrescrito.
        """
        current = self._jobs.get(job.id)
        if current is None:
            logger.warning("Job %s disappeared before it could be finished", job.id)
            return None
        if current.status is not JobStatus.RUNNING:
            logger.warning(
                "Job %s is %s and no longer this worker's to finish",
                job.id,
                current.status.value,
            )
            return None

        written = self._jobs.save_if_status(transition(current), JobStatus.RUNNING)
        if written is None:
            logger.warning("Job %s changed hands while finishing", job.id)
            return None

        logger.info("Job %s is %s", written.id, written.status.value)
        return written


def _checkpoint_of(job: IndexingJob) -> PurePath | None:
    """Reconstrói o checkpoint gravado como um caminho do tipo da plataforma atual.

    **Nunca comparado como string.** O checkpoint é uma posição numa ordem
    estabelecida por `sorted()` sobre caminhos, que no Windows compara parte
    por parte e sem diferenciar caixa; a comparação de texto dá outra resposta
    e a collation do PostgreSQL uma terceira, então uma retomada guiada por
    qualquer uma das outras duas pula e repete arquivos arbitrários.
    """
    stored = job.last_processed_relative_path
    return None if stored is None else Path(str(stored))


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.infrastructure.workers.job_runner",
        description=(
            "Run queued indexing jobs, and resolve jobs whose worker died. "
            "A host process, not a container (RFC-029 section 6)."
        ),
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help=(
            "Reap, take at most one queued job, run it, and exit. Intended "
            "for measurement scripts and for a scheduled run; the default is "
            "to keep polling."
        ),
    )
    parser.add_argument(
        SUPERVISED_FLAG,
        action="store_true",
        help=(
            "Stop as Ctrl+C would when stdin reaches its end: a job in hand "
            "goes back to the queue. How the launcher runs this program "
            "(RFC-036 section 7.1)."
        ),
    )
    return parser


def build_runner(job_session: Session, image_session: Session) -> JobRunner:
    """Compõe um runner sobre duas sessões já abertas.

    **Duas sessões, e elas não podem ser uma só** (RFC-029 §4.11). Do
    contrário, o heartbeat e o checkpoint dividiriam uma transação com as
    escritas de imagens, e o `rollback()` do fallback de escrita em lote do
    RFC-024 §6 desfaria progresso já reportado -- ou o commit do heartbeat
    commitaria meio lote.

    Separada como função para que o executor e a CLI componham o mesmo runner,
    e não dois que se desencontram.

    **Esta é a composição que renderiza thumbnails** (RFC-030 §7.2), e o cache
    de thumbnails fica excluído de todo scan que ela roda. Os dois estão aqui,
    e não como padrão dentro do `JobRunner`, porque a pasta é configuração, e
    é aqui que a configuração vira argumento.

    **Todo repositório é `deferring()`** (RFC-036 §7.5): num processo que
    instalou o handler, uma interrupção espera a chamada ao banco em
    andamento, para que o `release()` a que ela leva encontre a conexão
    inteira e não abra nenhuma -- num logoff, uma conexão nova é um novo
    processo do servidor que não consegue iniciar, e derruba o cluster junto.
    Sem o handler (a CLI), o invólucro não muda nada.

    **É aqui que a leitura antecipada é ligada** (RFC-045 §5.7): com
    `indexing_read_ahead`, todo job lê os arquivos numa segunda thread,
    `read_ahead_files` à frente, e o pipeline decide janelas desse tamanho em
    vez de `metadata_prefetch_size` -- uma janela segura os bytes de cada um
    dos arquivos dela. A CLI também compõe o runner dela aqui, então lê
    adiantado como o executor; os backfills compõem o próprio e não leem.
    """
    from app.application.use_cases.thumbnail_writer import ThumbnailWriter
    from app.infrastructure.config.settings import settings
    from app.infrastructure.filesystem.mounted_device_locator import (
        MountedDeviceLocator,
    )
    from app.infrastructure.filesystem.sha256_content_hasher import Sha256ContentHasher
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
    from app.infrastructure.persistence.postgres_indexing_job_repository import (
        PostgresIndexingJobRepository,
    )
    from app.presentation.dependencies import get_embedding_model

    read_ahead = (
        ReadAheadLimits(
            files=settings.read_ahead_files,
            budget_bytes=settings.read_ahead_bytes,
            max_file_bytes=settings.read_ahead_max_file_bytes,
        )
        if settings.indexing_read_ahead
        else None
    )
    images = deferring(PostgresImageRepository(image_session))

    return JobRunner(
        jobs=deferring(PostgresIndexingJobRepository(job_session)),
        devices=deferring(PostgresDeviceRepository(job_session)),
        locator=MountedDeviceLocator(WindowsVolumeIdentityProvider()),
        indexer=IndexOrUpdateImagesUseCase(
            repository=images,
            embedding_model=get_embedding_model(),
            content_hasher=Sha256ContentHasher(),
            batch_size=settings.batch_size,
            metadata_prefetch_size=(
                read_ahead.files
                if read_ahead is not None
                else settings.metadata_prefetch_size
            ),
            thumbnail_writer=ThumbnailWriter(
                generator=PillowThumbnailGenerator(),
                store=FilesystemThumbnailStore(settings.thumbnail_directory),
                max_edge=settings.thumbnail_max_edge,
            ),
        ),
        supported_extensions=settings.supported_extensions,
        extract_capture_date=settings.extract_capture_date,
        extract_gps=settings.extract_gps,
        warm_up=get_embedding_model().warm_up,
        poll_interval=settings.job_poll_interval,
        heartbeat_interval=settings.job_heartbeat_interval,
        stale_timeout=settings.job_stale_timeout,
        max_attempts=settings.job_max_attempts,
        excluded_directories=(settings.thumbnail_directory,),
        read_ahead=read_ahead,
        images=images,
    )


def main() -> None:
    """Compõe o executor e o roda.

    A raiz de composição -- o único lugar autorizado a conhecer todas as
    classes concretas de uma vez. Os imports ficam dentro da função pelo
    motivo que `indexing_worker.main()` dá: importar `JobRunner` não pode
    arrastar o adaptador CLIP para um grafo de imports da Infraestrutura, e a
    maior parte da suíte de testes importa este módulo.

    **O modelo é carregado antes do primeiro poll**, e não depois de um job
    ser reivindicado -- e é o `JobRunner` que é dono dessa ordem, não esta
    função, então ela vale seja qual for o jeito de iniciar o executor.

    **Sem os modelos, o executor se recusa a iniciar** (RFC-035 §6.5): uma
    linha no stderr e o código de saída 78, como um `ConfigurationError`. Ele
    não tem nada a fazer sem o CLIP, e ficar de pé só reivindicaria jobs para
    fazer cada um falhar. A presença dos dois modelos é verificada primeiro, o
    que é barato; a carga do CLIP então verifica os hashes. Reiniciar não
    conserta nada disso, e o 78 é como o launcher do RFC-036 sabe (§6.1).

    **Parado antes do laço, ele sai em silêncio, com 0** (RFC-036 §7.1). Com
    `--supervised`, a parada pode chegar enquanto o `torch` está sendo
    importado ou o modelo carregado, e não há job em mãos nessa hora: nada a
    devolver, e uma parada que foi pedida não é falha.

    **Uma interrupção espera a chamada ao banco em andamento** (§7.5):
    `install()` antes de qualquer coisa poder levantar uma, e `build_runner()`
    entrega ao runner repositórios que a adiam.
    """
    args = _build_arg_parser().parse_args()
    install()
    try:
        if args.supervised:
            stop_on_stdin_eof()
        _run(once=args.once)
    except KeyboardInterrupt:
        logger.info("Job executor stopped by the operator before it started polling")


def _run(*, once: bool) -> None:
    from app.infrastructure.ai.model_runtime import (
        ModelUnavailableError,
        default_runtime,
    )
    from app.infrastructure.persistence.session import SessionLocal

    job_session = SessionLocal()
    image_session = SessionLocal()
    try:
        # Antes de `build_runner()`, que é o que importa o `torch`: uma árvore
        # ausente é recusada em um segundo, e não depois desse import.
        default_runtime().check_presence()
        runner = build_runner(job_session, image_session)
        if once:
            runner.warm_up_now()
            runner.reap()
            runner.claim_and_run()
        else:
            runner.run_forever()
    except ModelUnavailableError as error:
        refuse_to_start(f"SolidVision cannot start: {error}")
    finally:
        # Um rollback cada, nas conexões que os repositórios usaram.
        with deferred():
            image_session.close()
            job_session.close()


if __name__ == "__main__":
    main()
