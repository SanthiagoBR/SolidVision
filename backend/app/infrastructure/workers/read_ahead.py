"""Uma thread que lê cada arquivo uma vez, à frente do pipeline de indexação (RFC-045).

O pipeline do RFC-024 é uma thread só: descobre um arquivo, calcula o hash --
o que o lê inteiro --, depois deixa o CLIP decodificá-lo, depois a thumbnail
decodificá-lo de novo, e só então olha o próximo. No disco rígido externo do
usuário, 59% desse tempo é o disco, com o processador parado, e depois o
processador trabalha com o disco parado (RFC-045 §1). Este módulo sobrepõe os
dois sem tocar no caso de uso: fica onde os candidatos nascem.

    descoberta -> [thread: stat, EXIF, lê o arquivo inteiro, SHA-256] -> fila
               -> [thread do pipeline: decide, CLIP, thumbnail, grava]

**A ordem é a ordem da descoberta**, porque o checkpoint é uma posição nela
(RFC-029 §10). Na fila, o primeiro a entrar é o primeiro a sair, e o mesmo vale
para a reprodução dos eventos de progresso do scan, que viajam nela entre os
candidatos.

**A thread lê só o que a decisão vai pedir.** Antes de ela começar, o job
carrega, na thread principal, o tamanho e o mtime gravados de cada linha do
dispositivo (`ImageRepository.list_change_signals()`); um arquivo cujos sinais
batem é repassado sem ser lido, como o pipeline o pularia -- um `stat` por
arquivo, como antes, num novo scan de um disco indexado. A thread nunca toca o
banco: a sessão pertence à thread do pipeline (§5.2).

**Nada que grava roda aqui.** O `discovered()` e o `listed()` do scan eram
chamados de dentro da descoberta, e gravam a linha do job e a linha do
dispositivo pela sessão do job; com a descoberta nesta thread, eles vão para a
fila, e o consumidor -- a thread dona da sessão -- chama o observador real à
medida que os tira de lá (§5.4).
"""

from __future__ import annotations

import dataclasses
import queue
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

from app.application.use_cases.indexing_plan import (
    FileContent,
    IndexCandidate,
    is_unchanged,
)
from app.domain.services.indexing_observer import IndexingObserver
from app.domain.value_objects.change_signals import ChangeSignals
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.indexing_progress import IndexingProgress
from app.domain.value_objects.root_listing import RootListing
from app.infrastructure.filesystem.sha256_content_hasher import sha256_of
from app.infrastructure.logging.logger import get_logger

logger = get_logger(__name__)

POLL_SECONDS = 0.05
"""Quanto tempo cada lado espera na fila antes de levantar os olhos de novo.

O consumidor levanta os olhos para saber que a thread morreu sem dizer nada, e
para que uma interrupção o alcance numa plataforma cujas esperas em lock ela
não consegue cortar; a thread levanta os olhos para saber que lhe pediram para
parar enquanto a fila estava cheia. Curto o bastante para nenhum dos dois ser
notado, longo o bastante para não custar nada.
"""

INTERRUPTED_JOIN_SECONDS = 2.0
"""Quanto tempo um consumidor interrompido espera a thread antes de deixá-la.

Toda outra saída do bloco `with` espera a thread terminar, o que leva no
máximo a leitura em andamento (§5.1). Uma interrupção -- Ctrl+C, a parada do
launcher -- é o processo terminando, e a thread pode estar num ponto onde não
pode ser parada: a listagem de um disco inteiro a frio, um `sorted()` sobre um
`rglob`, onde o pipeline antes podia ser interrompido. A thread é daemon, então
o processo termina de qualquer jeito; isto limita quanto a parada a espera.
"""


@dataclass(frozen=True)
class ReadAheadLimits:
    """Até onde a thread pode se adiantar, e quantos bytes pode segurar.

    As três configurações do RFC-045 §5.7 além do interruptor, como um valor
    só, para que um runner tenha todas elas ou não leia nada adiantado.
    """

    files: int
    """O tamanho da fila, em itens -- candidatos e eventos do scan."""
    budget_bytes: int
    """O máximo de bytes lidos e ainda não liberados, somando todos os arquivos."""
    max_file_bytes: int
    """Um arquivo maior é repassado sem os bytes, para ser lido pelo caminho."""

    def __post_init__(self) -> None:
        for name in ("files", "budget_bytes", "max_file_bytes"):
            value = getattr(self, name)
            if value < 1:
                raise ValueError(f"{name} must be at least 1, got {value}")


class ByteBudget:
    """Os bytes que a leitura antecipada segura, contados pelas duas threads.

    Tomados pela thread de leitura antes de ler um arquivo; devolvidos por
    `FileContent.release()` na thread do pipeline, ou pelo garbage collector --
    em qualquer thread em que ele rode -- quando um conteúdo que ninguém liberou
    é coletado. Daí o lock.
    """

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._held = 0
        self._peak = 0
        self._lock = threading.Lock()

    @property
    def held(self) -> int:
        """Bytes lidos e ainda não devolvidos."""
        with self._lock:
            return self._held

    @property
    def peak(self) -> int:
        """O máximo de bytes seguros de uma vez, desde que este orçamento foi criado."""
        with self._lock:
            return self._peak

    def try_take(self, size: int) -> bool:
        """Reserva `size` bytes se couberem no limite; nunca espera."""
        with self._lock:
            if self._held + size > self._limit:
                return False
            self._held += size
            self._peak = max(self._peak, self._held)
            return True

    def settle(self, reserved: int, actual: int) -> None:
        """Troca uma reserva pelo que a leitura de fato devolveu.

        Um arquivo pode mudar entre o `stat` e a leitura. O que fica seguro é o
        que foi lido, já que é isso que o `FileContent` vai devolver.
        """
        with self._lock:
            self._held += actual - reserved
            self._peak = max(self._peak, self._held)

    def give_back(self, size: int) -> None:
        """Devolve `size` bytes ao orçamento."""
        with self._lock:
            self._held -= size


@dataclass
class ReadAheadStats:
    """O que a thread fez com cada candidato, para o relatório e os testes.

    Escrito só pela thread de leitura e lido depois que ela terminou, então
    não precisa de lock.
    """

    read: int = 0
    """Arquivos lidos inteiros, com hash e entregues com os bytes."""
    bytes_read: int = 0
    unchanged: int = 0
    """Arquivos que o snapshot disse inalterados, repassados sem leitura."""
    too_large: int = 0
    """Arquivos acima de `max_file_bytes`, ou sem tamanho, a ler pelo caminho."""
    over_budget: int = 0
    """Arquivos que teriam passado de `budget_bytes`, a ler pelo caminho."""
    failed: int = 0
    """Leituras que falharam; o pipeline lê pelo caminho, falha igual e reporta."""


@dataclass(frozen=True)
class _Discovered:
    files_seen: int
    complete: bool


@dataclass(frozen=True)
class _Listed:
    listing: RootListing


@dataclass(frozen=True)
class _Raised:
    error: BaseException


class _End:
    """O scan terminou normalmente."""


_END = _End()

type _Item = IndexCandidate | _Discovered | _Listed | _Raised | _End


class _QueueingObserver(IndexingObserver):
    """O observador que o scan recebe na thread: põe na fila, e não grava nada.

    Só `discovered()` e `listed()` são chamados por um scan. Os outros três
    pertencem ao pipeline, que os chama no observador real, na própria thread;
    aqui eles não fazem nada.
    """

    def __init__(self, put: Callable[[_Item], bool]) -> None:
        self._put = put

    def discovered(self, files_seen: int, complete: bool) -> None:
        self._put(_Discovered(files_seen, complete))

    def listed(self, listing: RootListing) -> None:
        self._put(_Listed(listing))

    def window_decided(
        self, progress: IndexingProgress, durable_through: ImagePath | None
    ) -> None:
        return None

    def batch_persisted(
        self, progress: IndexingProgress, durable_through: ImagePath | None
    ) -> None:
        return None

    def should_stop(self) -> bool:
        return False


def read_whole_file(path: Path) -> bytes:
    """O arquivo inteiro, numa leitura só (RFC-045 §5.1)."""
    return path.read_bytes()


class ReadAheadCandidates:
    """Candidatos lidos adiantados numa thread, consumidos na ordem em que surgiram.

    Um context manager que também é o iterável de candidatos:

        with ReadAheadCandidates(source, observer, signals, limits) as candidates:
            summary = use_case.execute(candidates, observer)

    Entrar inicia a thread, que chama `source` com um observador que põe os
    eventos do scan na fila, e põe na fila -- limitada a `limits.files` itens
    -- cada candidato em ordem, com os bytes quando ela os leu, os eventos do
    scan entre eles e, no fim, um marcador ou a exceção que encerrou o scan.
    Iterar os tira de lá, entrega os eventos a `observer` e os candidatos a
    quem chama, e levanta aquela exceção onde ela aconteceu.

    **O que a thread não lê**, repassando o candidato sem bytes para ser lido
    pelo caminho, como antes: um arquivo que o snapshot diz inalterado; um
    arquivo acima de `max_file_bytes`; um arquivo que levaria os bytes seguros
    acima de `budget_bytes` -- sem esperar que eles voltem, porque o
    consumidor pode estar esperando esta thread encher uma janela (§5.1); e um
    arquivo cuja leitura falha, cujo erro o pipeline então encontra pelo
    caminho e reporta, um lugar só para as falhas.

    **Sair do bloco encerra a thread**, seja qual for a saída: a parada é
    sinalizada, a fila esvaziada para que um `put` bloqueado retorne, e a
    thread aguardada -- ela olha o sinal entre um arquivo e outro, então a
    espera é no máximo a leitura em andamento. Uma interrupção espera no
    máximo `INTERRUPTED_JOIN_SECONDS` (veja lá). Os bytes ainda na fila são
    liberados.
    """

    def __init__(
        self,
        source: Callable[[IndexingObserver], Iterable[IndexCandidate]],
        observer: IndexingObserver,
        change_signals: Mapping[ImageId, ChangeSignals],
        limits: ReadAheadLimits,
        read_file: Callable[[Path], bytes] = read_whole_file,
    ) -> None:
        """Prepara, sem iniciar nada.

        `source` monta o scan com o observador que recebe -- o que põe na fila
        -- e é chamado na thread. `observer` é o real, chamado só da thread que
        itera. `read_file` é uma costura para os testes, que precisam de uma
        leitura que bloqueia ou falha.
        """
        self._source = source
        self._observer = observer
        self._signals = change_signals
        self._limits = limits
        self._read_file = read_file
        self._queue: queue.Queue[_Item] = queue.Queue(maxsize=limits.files)
        self._stopping = threading.Event()
        self._relay = _QueueingObserver(self._put)
        self._thread: threading.Thread | None = None
        self.budget = ByteBudget(limits.budget_bytes)
        self.stats = ReadAheadStats()

    # ------------------------------------------------------------------
    # O lado do consumidor

    def __enter__(self) -> ReadAheadCandidates:
        if self._thread is not None:
            raise RuntimeError("A ReadAheadCandidates can be entered only once")
        self._thread = threading.Thread(
            target=self._read_ahead, name="solidvision-read-ahead", daemon=True
        )
        self._thread.start()
        return self

    def __iter__(self) -> Iterator[IndexCandidate]:
        if self._thread is None:
            raise RuntimeError("Iterate a ReadAheadCandidates inside its `with` block")
        while True:
            item = self._next_item()
            if isinstance(item, IndexCandidate):
                yield item
            elif isinstance(item, _Discovered):
                self._observer.discovered(item.files_seen, item.complete)
            elif isinstance(item, _Listed):
                self._observer.listed(item.listing)
            elif isinstance(item, _Raised):
                raise item.error
            else:
                return

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close(
            interrupted=exc_type is not None and not issubclass(exc_type, Exception)
        )

    def close(self, interrupted: bool = False) -> None:
        """Para a thread, libera o que está na fila e espera que ela termine."""
        self._stopping.set()
        thread = self._thread
        if thread is None:
            return
        deadline = time.monotonic() + INTERRUPTED_JOIN_SECONDS if interrupted else None
        while thread.is_alive():
            self._drain()
            thread.join(timeout=POLL_SECONDS)
            if deadline is not None and time.monotonic() >= deadline:
                logger.warning(
                    "The read-ahead thread is still busy after an interrupt; "
                    "the process will end it"
                )
                break
        self._drain()

    def _next_item(self) -> _Item:
        thread = self._thread
        assert thread is not None
        while True:
            try:
                return self._queue.get(timeout=POLL_SECONDS)
            except queue.Empty:
                if not thread.is_alive() and self._queue.empty():
                    raise RuntimeError(
                        "The read-ahead thread ended without saying why"
                    ) from None

    def _drain(self) -> None:
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            _release(item)

    # ------------------------------------------------------------------
    # O lado da thread

    def _read_ahead(self) -> None:
        candidates: Iterator[IndexCandidate] | None = None
        try:
            candidates = iter(self._source(self._relay))
            for candidate in candidates:
                if self._stopping.is_set():
                    return
                if not self._put(self._prepared(candidate)):
                    return
            self._put(_END)
        except BaseException as error:  # entregue ao consumidor, que a levanta
            self._put(_Raised(error))
        finally:
            # Um gerador deixado suspenso é fechado aqui, na thread que o rodou,
            # para que a limpeza dele rode onde foi escrita para rodar.
            close = getattr(candidates, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:
                    logger.warning("Closing the scan raised", exc_info=True)

    def _put(self, item: _Item) -> bool:
        """Põe `item` na fila, salvo se o consumidor mandou a thread parar."""
        while not self._stopping.is_set():
            try:
                self._queue.put(item, timeout=POLL_SECONDS)
                return True
            except queue.Full:
                continue
        _release(item)
        return False

    def _prepared(self, candidate: IndexCandidate) -> IndexCandidate:
        """`candidate`, com os bytes se a decisão for querê-los e eles couberem."""
        if not self._will_be_hashed(candidate):
            self.stats.unchanged += 1
            return candidate

        size = candidate.file_size
        if size is None or size > self._limits.max_file_bytes:
            self.stats.too_large += 1
            return candidate
        if not self.budget.try_take(size):
            self.stats.over_budget += 1
            return candidate

        try:
            data = self._read_file(candidate.image.require_absolute_path().value)
        except Exception:
            # Não é reportado aqui: o pipeline calcula o hash pelo caminho,
            # encontra o mesmo erro e o reporta com o arquivo (RFC-024 §6).
            self.budget.give_back(size)
            self.stats.failed += 1
            return candidate

        self.budget.settle(size, len(data))
        self.stats.read += 1
        self.stats.bytes_read += len(data)
        content = FileContent(sha256_of(data), data, on_release=self.budget.give_back)
        return dataclasses.replace(candidate, content=content)

    def _will_be_hashed(self, candidate: IndexCandidate) -> bool:
        """Os passos 1 e 2 de `plan_indexing()`, contra o snapshot."""
        signals = self._signals.get(candidate.image.id)
        return signals is None or not is_unchanged(
            candidate, signals.file_size, signals.file_modified_at
        )


def _release(item: _Item) -> None:
    if isinstance(item, IndexCandidate) and item.content is not None:
        item.content.release()
