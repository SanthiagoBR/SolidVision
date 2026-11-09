r"""O supervisor: a ordem do início, o ready, os reinícios e a parada (RFC-036).

Uma thread, um loop, um tick de 100 ms. Tudo o que toca o mundo -- os filhos, o
cluster, o relógio, a sonda de `/health`, as portas, os eventos e o arquivo de
estado -- entra pelo construtor, para que os testes de §10 rodem a máquina
inteira contra dublês e um relógio que só anda quando a máquina dorme.

O início (§5.1, com o segundo ramo que §16 fez valer)::

    a porta da API (5.4), e a API -- depois do cluster numa primeira execução
    o cluster e as migrations, quando a fonte é embarcada, enquanto isso
    o job runner
    ready: o primeiro 200 do /health, perguntado a cada tick

O que a supervisão faz quando algo sai (6.1, 6.2)::

    saída                              o supervisor
    durante a parada                   nada: é esperado
    78, a qualquer momento             nenhum reinício; `child` com a última
                                       linha. A API: para tudo, 78. O job
                                       runner: segue, indexação indisponível
    3 da API antes de ela responder    a porta foi tomada: outra porta
    qualquer outra coisa               reinicia, depois da espera da vez
    o postmaster (status, a cada 2 s)  start() de novo, sob o mesmo limite

A parada (7.2): `stopping`; o stdin fechado nos dois filhos ao mesmo tempo; cada
um esperado até o seu prazo, depois `TerminateProcess`; o cluster por último, e
só quando um launcher o iniciou (7.3) -- ou quando a sessão está terminando, o
que o encerraria de qualquer jeito (7.5), e então pelo pipe de sinal do
postmaster, porque nenhum processo inicia durante um logoff; `stopped`. Um
segundo pedido durante a parada pula os prazos. O código de saída de cada filho
vai para o log, e um já morto quando a parada começa é dito como tal: num
logoff, é assim que o log diz quem o Windows encerrou primeiro, e se o
`release()` falhou.

**Os pedidos de parada são atendidos em pontos seguros, nunca no meio de uma
chamada.** Chegam como contagens, consultadas a cada tick: um Ctrl+C ou o fim do
stdin pelo signal handler de `__main__`, um `stop` pelo evento nomeado, o fim da
sessão pela sua janela. Um `initdb` ou uma crash recovery não é cortado ao meio
por nenhum deles: a parada começa quando a chamada retorna.
"""

from __future__ import annotations

import http.client
import json
import os
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import NamedTuple, Protocol

from app.infrastructure.config.constants import EXIT_CANNOT_START
from app.infrastructure.database.embedded.cluster import (
    ClusterReport,
    ClusterStatus,
    Phase,
    StartedCluster,
    StopOutcome,
)
from app.infrastructure.database.embedded.database_setup import (
    ClusterConnection,
    PreparedDatabase,
)
from app.infrastructure.database.embedded.errors import (
    ClusterRefusedError,
    EmbeddedPostgresError,
    RuntimeInvalidError,
)
from app.infrastructure.logging.logger import get_logger
from app.infrastructure.process.supervision import SUPERVISED_FLAG

from .children import API, JOB_RUNNER, Child, Program, last_line
from .events import Event, EventSink
from .instance import ClusterArrival, InstanceRecord, LauncherState, StopRequests

logger = get_logger(__name__)

LOOPBACK = "127.0.0.1"

UVICORN_STARTUP_FAILURE = 3
"""`STARTUP_FAILURE`, em `uvicorn/config.py`: o código com que o uvicorn sai
quando não consegue fazer o bind da porta. Escrito aqui porque o launcher não
importa o uvicorn."""

DATABASE = "database"
"""O `name` do cluster num evento `child`."""

PROBE_SECONDS = 2.0
"""Quanto tempo uma sonda de `/health` espera pela resposta."""


@dataclass(frozen=True)
class Timings:
    """Toda espera do supervisor. Escolhas, a menos que a docstring diga medido."""

    tick: float = 0.1
    """O período do loop, e o período da sonda de `/health` durante o início
    (§5.1)."""
    ready_deadline: float = 120.0
    """Da criação da API ao seu primeiro 200, antes de `failed`.

    Um banco que nunca responde -- o contêiner Docker de um checkout, parado --
    é para isso que ele serve. O valor é fixado contra a medição de §16, em
    `experiments/rfc-036-launcher/measure_launcher.py`."""
    cluster_check: float = 2.0
    """Com que frequência se pergunta ao `status()` se há um servidor (§6.2)."""
    restart_probe: float = 0.5
    """Com que frequência se pergunta a uma API reiniciada se ela já responde."""
    api_stop: float = 15.0
    """O prazo da API na parada: os 5 s do uvicorn para as requisições em
    andamento, o lifespan e a margem (§7.2)."""
    job_runner_stop: float = 30.0
    """O prazo do job runner na parada: o `release()` do job."""
    session_api_stop: float = 3.0
    session_job_runner_stop: float = 10.0
    """Os prazos no fim da sessão (§7.5), mais curtos, porque a paciência do
    sistema também é."""
    session_cluster_stop: float = 6.0
    """No fim da sessão, cada espera da parada pelo pipe de sinal: o lock, o
    fast shutdown, o immediate. Com os 10 s do job runner, dentro dos 30 s que
    a janela dá à parada inteira. Os fast shutdowns nos logs do servidor
    levaram de 0,1 a 2,6 s."""
    restart_delays: tuple[float, ...] = (0.0, 2.0, 5.0, 15.0, 30.0)
    """A espera antes de cada reinício seguido (§6.1)."""
    restart_window: float = 600.0
    restart_limit: int = 5
    """Cinco saídas em dez minutos, e o supervisor desiste daquele filho como
    se ele tivesse saído com 78."""
    stable_after: float = 600.0
    """No ar por esse tempo, e a próxima saída volta a esperar zero."""
    port_attempts: int = 3
    """Inícios seguidos da API recusados pelo `bind`: o número do RFC-033."""


class Clock(Protocol):
    def monotonic(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class Cluster(Protocol):
    """O que o supervisor pede ao cluster: `EmbeddedPostgres`, estruturalmente."""

    def start(
        self, on_phase: Callable[[Phase], None] | None = None
    ) -> StartedCluster: ...

    def prepare(
        self,
        connection: ClusterConnection,
        on_phase: Callable[[Phase], None] | None = None,
    ) -> PreparedDatabase: ...

    def status(self) -> ClusterReport: ...

    def stop(self) -> StopOutcome: ...

    def stop_by_signal(self, timeout: float) -> StopOutcome: ...


class Prober(Protocol):
    """`PortProber` do adaptador do cluster, estruturalmente (§5.4)."""

    def usable(self, port: int) -> bool: ...

    def free_port(self) -> int: ...


class StateWriter(Protocol):
    def write(self, record: InstanceRecord) -> bool: ...


@dataclass(frozen=True)
class HealthAnswer:
    status: int
    search: str | None


HealthProbe = Callable[[int], HealthAnswer | None]
"""`GET /health` numa porta de `127.0.0.1`: a resposta, ou `None` se não houve."""

Spawn = Callable[[Program, Sequence[str]], Child]
"""Cria um filho de `program` com esses argumentos, já iniciado."""


def probe_health(port: int) -> HealthAnswer | None:
    """Um `GET /health`. Uma conexão recusada é `None`: "ainda não"."""
    connection = http.client.HTTPConnection(LOOPBACK, port, timeout=PROBE_SECONDS)
    try:
        connection.request("GET", "/health")
        response = connection.getresponse()
        body = response.read()
    except (OSError, http.client.HTTPException):
        return None
    finally:
        connection.close()
    search: str | None = None
    try:
        payload = json.loads(body)
    except ValueError:
        payload = None
    if isinstance(payload, dict) and isinstance(payload.get("search"), str):
        search = payload["search"]
    return HealthAnswer(response.status, search)


class PortRefusedError(Exception):
    """`API_PORT` está definida, e essa porta não pode ser usada."""


class ApiPorts:
    """A porta da API (§5.4).

    1. A porta do último início que chegou a `ready`, se um `bind` exclusivo a
       aceitar. 2. Senão, uma que o sistema der. `API_PORT` a fixa: essa porta
    ou uma recusa, nunca outra, porque uma porta fixa é uma promessa a alguém.
    """

    def __init__(self, prober: Prober, *, fixed: int | None, last: int | None) -> None:
        self._prober = prober
        self._fixed = fixed
        self._last = last

    def first(self) -> int:
        if self._fixed is not None:
            if not self._prober.usable(self._fixed):
                raise PortRefusedError(self._taken(self._fixed))
            return self._fixed
        if self._last is not None and self._prober.usable(self._last):
            return self._last
        return self._prober.free_port()

    def another(self, taken: int) -> int:
        """A API saiu com 3: `taken` foi para outro nesse meio-tempo."""
        if self._fixed is not None:
            raise PortRefusedError(self._taken(self._fixed))
        return self._prober.free_port()

    def again(self, current: int | None) -> int:
        """Para um reinício: a mesma porta primeiro, para a janela manter a URL."""
        if self._fixed is not None:
            return self._fixed
        if current is not None and self._prober.usable(current):
            return current
        return self._prober.free_port()

    @staticmethod
    def _taken(port: int) -> str:
        return (
            f"API_PORT is {port}, and that port cannot be used on {LOOPBACK}: "
            "another program holds it, or Windows reserved it. Free it, or remove "
            "API_PORT and SolidVision chooses a port."
        )


class RestartPolicy:
    """§6.1: a espera cresce, e cinco saídas em dez minutos são o fim."""

    def __init__(self, timings: Timings) -> None:
        self._timings = timings
        self._exits: list[float] = []
        self._in_a_row = 0

    def after_exit(self, now: float, started_at: float) -> float | None:
        """A espera antes do próximo início, ou `None` para desistir."""
        if now - started_at >= self._timings.stable_after:
            self._in_a_row = 0
        window = self._timings.restart_window
        self._exits = [moment for moment in self._exits if now - moment < window]
        self._exits.append(now)
        if len(self._exits) >= self._timings.restart_limit:
            return None
        delays = self._timings.restart_delays
        delay = delays[min(self._in_a_row, len(delays) - 1)]
        self._in_a_row += 1
        return delay


class StopReason(StrEnum):
    REQUESTED = "requested"
    SESSION_END = "session_end"
    FAILURE = "failure"


class _Ending(NamedTuple):
    code: int
    reason: StopReason
    detail: str | None = None


class _StartRefusedError(Exception):
    """O início não pode seguir: `failed`, com esta linha."""

    def __init__(self, line: str, code: int = EXIT_CANNOT_START) -> None:
        super().__init__(line)
        self.line = line
        self.code = code


@dataclass
class _Supervised:
    """Um programa sob supervisão, através dos filhos que ele já foi."""

    program: Program
    restarts: RestartPolicy
    child: Child | None = None
    started_at: float = 0.0
    restart_at: float | None = None
    unavailable: bool = False
    port: int | None = None
    answering: bool = False
    """A API respondeu algo em `port` nesta encarnação: ela fez o bind da
    porta, e uma saída 3 não pode mais significar que a porta foi tomada."""
    port_failures: int = 0
    next_probe: float = 0.0


class Supervisor:
    """A máquina de estados do launcher. `run()` retorna o código de saída."""

    def __init__(
        self,
        *,
        spawn: Spawn,
        ports: ApiPorts,
        events: EventSink,
        state: StateWriter,
        requests: StopRequests,
        cluster: Cluster | None,
        probe: HealthProbe = probe_health,
        previous: InstanceRecord | None = None,
        clock: Clock | None = None,
        timings: Timings | None = None,
    ) -> None:
        self._spawn_child = spawn
        self._ports = ports
        self._events = events
        self._state = state
        self._requests = requests
        self._cluster = cluster
        self._probe = probe
        self._previous = previous
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._timings = timings if timings is not None else Timings()
        self._api = _Supervised(API, RestartPolicy(self._timings))
        self._job_runner = _Supervised(JOB_RUNNER, RestartPolicy(self._timings))
        self._cluster_restarts = RestartPolicy(self._timings)
        self._cluster_started_at = 0.0
        self._cluster_restart_at: float | None = None
        self._arrival: ClusterArrival | None = None
        self._postmaster_pid: int | None = None
        self._ready = False
        self._announced_port: int | None = None
        self._last_ready_port = previous.last_ready_port if previous else None
        self.finished = threading.Event()
        """Definido quando `run()` terminou: o que o fim da sessão espera."""

    # ------------------------------------------------------------------
    # A vida inteira

    def run(self) -> int:
        try:
            return self._run()
        finally:
            self.finished.set()

    def _run(self) -> int:
        try:
            ready = self._start()
        except _StartRefusedError as refusal:
            logger.error("The launcher could not start: %s", refusal.line)
            self._events.emit(Event.FAILED, detail=refusal.line)
            self._stop(_Ending(refusal.code, StopReason.FAILURE), announce=False)
            return refusal.code
        except Exception as error:
            logger.exception("The launcher failed while starting")
            self._events.emit(
                Event.FAILED,
                detail=f"The launcher failed while starting ({error}); the "
                "traceback is in launcher.log.",
            )
            self._stop(_Ending(1, StopReason.FAILURE), announce=False)
            return 1
        if not ready:
            ending = _Ending(0, self._requested_reason())
        else:
            try:
                ending = self._supervise()
            except Exception as error:
                logger.exception("The supervision failed")
                ending = _Ending(
                    1,
                    StopReason.FAILURE,
                    f"The launcher failed ({error}); the traceback is in "
                    "launcher.log.",
                )
        self._stop(ending, announce=True)
        return ending.code

    # ------------------------------------------------------------------
    # O início (§5.1)

    def _start(self) -> bool:
        """`True` quando ficou pronto; `False` quando uma parada foi pedida antes.

        **A API é criada junto com o cluster** -- o segundo ramo de §5.1, que
        §16 fez valer: a quente, o cluster e as migrations levaram 2,1 s e a API
        3,6 s da criação ao primeiro 200, um depois do outro, 6,4 s até `ready`
        contra um critério de 5. Lado a lado, o import da API cobre o início do
        cluster. O que custa, declarado: a API responde 503 até o cluster subir,
        e poderia servir durante as migrations -- a ninguém a quem tenham dito
        a sua URL, que só o `ready` dá. O job runner ainda espera as migrations:
        a sua primeira consulta, a do reaper, viria de outro modo antes de um
        primeiro `initdb` terminar.

        **Exceto na primeira execução, quando ainda não há cluster.** Medido
        também: o aquecimento da API então importa o `torch` e calcula o hash de
        924 MB de modelos no mesmo disco do `initdb`, que foi de 45 para 99 s, e
        o início a frio de 68 para 109 s. Sem cluster a iniciar, a API espera o
        `initdb`, como no primeiro ramo.
        """
        self._write_state(LauncherState.STARTING)
        if self._stop_asked():
            return False
        try:
            port = self._ports.first()
        except PortRefusedError as refusal:
            raise _StartRefusedError(str(refusal)) from None
        with_the_cluster = self._cluster is None or not self._first_run(self._cluster)
        if with_the_cluster:
            self._spawn_api(port, first=True)
        if self._cluster is None:
            self._arrival = ClusterArrival.EXTERNAL
        else:
            self._bring_up_cluster(self._cluster)
        self._write_state(LauncherState.STARTING)
        if self._stop_asked():
            return False
        if not with_the_cluster:
            self._spawn_api(port, first=True)
        self._spawn_job_runner(first=True)
        return self._wait_until_ready()

    @staticmethod
    def _first_run(cluster: Cluster) -> bool:
        """Se `start()` está para criar o cluster: um `initdb` pela frente."""
        try:
            return cluster.status().status is ClusterStatus.ABSENT
        except EmbeddedPostgresError:
            return False

    def _bring_up_cluster(self, cluster: Cluster) -> None:
        try:
            started = cluster.start(on_phase=self._phase)
            self._arrival = self._arrival_of(cluster, started)
            self._cluster_started_at = self._clock.monotonic()
            self._write_state(LauncherState.STARTING)
            if self._stop_asked():
                return
            prepared = cluster.prepare(started.connection, on_phase=self._phase)
        except EmbeddedPostgresError as error:
            raise _StartRefusedError(str(error)) from None
        for warning in prepared.warnings:
            logger.warning("%s", warning)

    def _arrival_of(self, cluster: Cluster, started: StartedCluster) -> ClusterArrival:
        """§7.3: um launcher para o cluster que um launcher iniciou.

        Inclusive o que um launcher que morreu deixou para trás, que este adota
        e que ninguém mais pararia: o arquivo de estado de um launcher cujo lock
        está livre diz `started`, sobre este mesmo servidor.
        """
        try:
            self._postmaster_pid = cluster.status().pid
        except EmbeddedPostgresError as error:
            logger.warning("The cluster's status could not be read: %s", error)
            self._postmaster_pid = None
        if started.started_here:
            return ClusterArrival.STARTED
        previous = self._previous
        if (
            previous is not None
            and previous.cluster is ClusterArrival.STARTED
            and previous.postmaster_pid is not None
            and previous.postmaster_pid == self._postmaster_pid
        ):
            logger.info(
                "The cluster (PID %s) was started by a launcher that is gone; this "
                "one inherits it, and stops it at the end",
                self._postmaster_pid,
            )
            return ClusterArrival.STARTED
        return ClusterArrival.ADOPTED

    def _phase(self, phase: Phase) -> None:
        if self._ready:
            logger.info("Database: %s", phase.value)
        else:
            self._events.emit(Event.PHASE, phase=phase.value)

    def _wait_until_ready(self) -> bool:
        deadline = self._clock.monotonic() + self._timings.ready_deadline
        last_status: int | None = None
        while True:
            if self._stop_asked():
                return False
            fatal = self._watch_children()
            if fatal is not None:
                raise _StartRefusedError(fatal)
            self._restart_due()
            api = self._api
            if api.child is not None and api.port is not None:
                answer = self._probe(api.port)
                if answer is not None:
                    api.answering = True
                    last_status = answer.status
                    if answer.status == 200:
                        self._announce_ready(api.port, answer)
                        return True
            if self._clock.monotonic() >= deadline:
                raise _StartRefusedError(self._not_ready(last_status))
            self._clock.sleep(self._timings.tick)

    def _not_ready(self, last_status: int | None) -> str:
        seconds = f"{self._timings.ready_deadline:.0f} s"
        if last_status == 503:
            return (
                f"The API answered /health with 503 for {seconds}: it cannot reach "
                "the database."
            )
        if last_status is not None:
            return f"The API answered /health with {last_status} for {seconds}."
        detail = ""
        if self._api.child is not None:
            line = last_line(self._api.child.last_lines())
            if line:
                detail = f" Its last line: {line}"
        return f"The API did not answer on port {self._api.port} in {seconds}.{detail}"

    def _announce_ready(self, port: int, answer: HealthAnswer) -> None:
        self._ready = True
        self._announced_port = port
        self._last_ready_port = port
        self._write_state(LauncherState.READY)
        url = _url(port)
        logger.info("Ready at %s (search %s)", url, answer.search)
        self._events.emit(
            Event.READY, url=url, search=answer.search, indexing=self._indexing()
        )

    def _indexing(self) -> str:
        if self._job_runner.unavailable:
            return "unavailable"
        if self._job_runner.child is None:
            return "restarting"
        return "running"

    # ------------------------------------------------------------------
    # A supervisão (§6)

    def _supervise(self) -> _Ending:
        next_cluster_check = self._clock.monotonic() + self._timings.cluster_check
        while True:
            if self._stop_asked():
                return _Ending(0, self._requested_reason())
            fatal = self._watch_children()
            if fatal is not None:
                return _Ending(EXIT_CANNOT_START, StopReason.FAILURE, fatal)
            self._restart_due()
            self._probe_restarted_api()
            if (
                self._cluster is not None
                and self._clock.monotonic() >= next_cluster_check
            ):
                fatal = self._watch_cluster(self._cluster)
                if fatal is not None:
                    return _Ending(EXIT_CANNOT_START, StopReason.FAILURE, fatal)
                next_cluster_check = (
                    self._clock.monotonic() + self._timings.cluster_check
                )
            self._clock.sleep(self._timings.tick)

    def _watch_children(self) -> str | None:
        """Trata cada filho que saiu; uma linha quando a API se perde."""
        for supervised in (self._api, self._job_runner):
            child = supervised.child
            if child is None:
                continue
            code = child.poll()
            if code is None:
                continue
            supervised.child = None
            child.drain()
            fatal = self._exited(supervised, code, child.pid, child.last_lines())
            self._write_state(
                LauncherState.READY if self._ready else LauncherState.STARTING
            )
            if fatal is not None:
                return fatal
        return None

    def _exited(
        self, supervised: _Supervised, code: int, pid: int, lines: tuple[str, ...]
    ) -> str | None:
        name = supervised.program.name
        detail = last_line(lines)
        if code == EXIT_CANNOT_START:
            self._log_exit(name, code, pid, lines)
            return self._give_up(supervised, code, detail)
        if (
            supervised is self._api
            and code == UVICORN_STARTUP_FAILURE
            and not supervised.answering
        ):
            return self._port_taken(supervised)
        self._log_exit(name, code, pid, lines)
        now = self._clock.monotonic()
        delay = supervised.restarts.after_exit(now, supervised.started_at)
        if delay is None:
            minutes = self._timings.restart_window / 60
            return self._give_up(
                supervised,
                code,
                f"{name} exited {self._timings.restart_limit} times in "
                f"{minutes:.0f} minutes, and is not restarted again. Its last "
                f"line: {detail}",
            )
        supervised.restart_at = now + delay
        logger.warning("%s exited with %s; restarting in %.0f s", name, code, delay)
        self._events.emit(
            Event.CHILD,
            name=name,
            state="restarting",
            exit_code=code,
            delay=delay,
            detail=detail,
        )
        return None

    def _port_taken(self, api: _Supervised) -> str | None:
        """§5.4, passo 3: entre a sonda e o `bind` do uvicorn, a porta foi para
        outro processo. Outra porta, na hora."""
        api.port_failures += 1
        taken = api.port
        if api.port_failures >= self._timings.port_attempts:
            return self._give_up(
                api,
                UVICORN_STARTUP_FAILURE,
                f"The API could not bind a port {api.port_failures} times in a row.",
            )
        try:
            port = self._ports.another(taken if taken is not None else 0)
        except PortRefusedError as refusal:
            return self._give_up(api, UVICORN_STARTUP_FAILURE, str(refusal))
        logger.warning(
            "Port %s was taken before the API bound it; trying %s", taken, port
        )
        self._spawn_api(port, port_retry=True)
        return None

    def _give_up(
        self, supervised: _Supervised, code: int, detail: str | None
    ) -> str | None:
        """Nada mais de reinícios. Perder a API é perder o app."""
        supervised.unavailable = True
        supervised.restart_at = None
        name = supervised.program.name
        logger.error("%s is not restarted: %s", name, detail)
        self._events.emit(
            Event.CHILD, name=name, state="unavailable", exit_code=code, detail=detail
        )
        if supervised is self._api:
            return detail or f"The API exited with {code}."
        return None

    @staticmethod
    def _log_exit(
        name: str,
        code: int,
        pid: int,
        lines: tuple[str, ...],
        *,
        before_the_stop: bool = False,
    ) -> None:
        """O log do launcher recebe as últimas linhas de quem morreu, e só essas."""
        logger.warning(
            "%s (PID %s) exited with %s%s; its last %d lines follow",
            name,
            pid,
            exit_code(code),
            " before the stop asked it to" if before_the_stop else "",
            len(lines),
        )
        for line in lines:
            logger.warning("%s | %s", name, line)

    def _restart_due(self) -> None:
        now = self._clock.monotonic()
        for supervised in (self._api, self._job_runner):
            if (
                supervised.restart_at is None
                or supervised.child is not None
                or now < supervised.restart_at
            ):
                continue
            supervised.restart_at = None
            if supervised is self._api:
                self._spawn_api(self._ports.again(supervised.port))
            else:
                self._spawn_job_runner()

    def _probe_restarted_api(self) -> None:
        """Uma API reiniciada é acompanhada até responder, para dizer onde está."""
        api = self._api
        now = self._clock.monotonic()
        if (
            api.child is None
            or api.answering
            or api.port is None
            or now < api.next_probe
        ):
            return
        api.next_probe = now + self._timings.restart_probe
        answer = self._probe(api.port)
        if answer is None:
            return
        api.answering = True
        if answer.status == 200:
            self._last_ready_port = api.port
        if api.port != self._announced_port:
            self._announced_port = api.port
            logger.warning("The API came back on another port: %s", _url(api.port))
            self._events.emit(Event.ADDRESS, url=_url(api.port))
        self._events.emit(Event.CHILD, name=api.program.name, state="running")
        self._write_state(LauncherState.READY)

    def _watch_cluster(self, cluster: Cluster) -> str | None:
        """§6.2: só um postmaster morto é assunto do supervisor.

        A morte de um processo do servidor que não é o postmaster faz o
        postmaster se reinicializar sozinho, com `ready` ainda em
        `postmaster.pid`; `status()` diz `ready` o tempo todo, e nada é feito.
        """
        try:
            report = cluster.status()
        except EmbeddedPostgresError as error:
            logger.warning("The cluster's status could not be read: %s", error)
            return None
        if report.status in (
            ClusterStatus.READY,
            ClusterStatus.STARTING,
            ClusterStatus.STOPPING,
        ):
            return None
        if report.status is not ClusterStatus.STOPPED:
            found = report.status.value
            if report.detail:
                found = f"{found} ({report.detail})"
            return f"The database in {report.data_directory} is {found}."
        now = self._clock.monotonic()
        if self._cluster_restart_at is None:
            delay = self._cluster_restarts.after_exit(now, self._cluster_started_at)
            if delay is None:
                minutes = self._timings.restart_window / 60
                detail = (
                    f"The database stopped {self._timings.restart_limit} times in "
                    f"{minutes:.0f} minutes, and is not restarted again."
                )
                self._events.emit(
                    Event.CHILD, name=DATABASE, state="unavailable", detail=detail
                )
                return detail
            logger.warning("The database server is gone; starting it again")
            self._events.emit(
                Event.CHILD, name=DATABASE, state="restarting", delay=delay
            )
            self._cluster_restart_at = now + delay
        if now < self._cluster_restart_at:
            return None
        self._cluster_restart_at = None
        try:
            started = cluster.start(on_phase=self._phase)
        except (ClusterRefusedError, RuntimeInvalidError) as error:
            self._events.emit(
                Event.CHILD, name=DATABASE, state="unavailable", detail=str(error)
            )
            return str(error)
        except EmbeddedPostgresError as error:
            logger.warning("The database did not start again: %s", error)
            return None
        self._arrival = self._arrival_of(cluster, started)
        self._cluster_started_at = self._clock.monotonic()
        self._write_state(LauncherState.READY)
        self._events.emit(Event.CHILD, name=DATABASE, state="running")
        return None

    # ------------------------------------------------------------------
    # Os filhos

    def _spawn_api(
        self, port: int, *, first: bool = False, port_retry: bool = False
    ) -> None:
        """O `phase` uma vez, para a primeira API. Uma reiniciada diz `running`
        quando responde (`_probe_restarted_api`), ou o `ready` diz."""
        api = self._api
        if not port_retry:
            api.port_failures = 0
        api.port = port
        api.answering = False
        api.next_probe = 0.0
        api.started_at = self._clock.monotonic()
        api.child = self._spawn_child(API, ["--port", str(port), SUPERVISED_FLAG])
        logger.info("Started the API (PID %s) on port %s", api.child.pid, port)
        if first:
            self._events.emit(Event.PHASE, phase=API.name)
        self._write_state(
            LauncherState.READY if self._ready else LauncherState.STARTING
        )

    def _spawn_job_runner(self, *, first: bool = False) -> None:
        """O `phase` para o primeiro; `running` para cada um que vier depois."""
        runner = self._job_runner
        runner.started_at = self._clock.monotonic()
        runner.child = self._spawn_child(JOB_RUNNER, [SUPERVISED_FLAG])
        logger.info("Started the job runner (PID %s)", runner.child.pid)
        if first:
            self._events.emit(Event.PHASE, phase=JOB_RUNNER.name)
        else:
            self._events.emit(Event.CHILD, name=JOB_RUNNER.name, state="running")
        self._write_state(
            LauncherState.READY if self._ready else LauncherState.STARTING
        )

    # ------------------------------------------------------------------
    # A parada (§7)

    def _stop_asked(self) -> bool:
        return self._requests.count() > 0 or self._requests.session_ending()

    def _requested_reason(self) -> StopReason:
        if self._requests.session_ending():
            return StopReason.SESSION_END
        return StopReason.REQUESTED

    def _stop(self, ending: _Ending, *, announce: bool) -> None:
        if announce:
            fields: dict[str, object] = {"reason": ending.reason.value}
            if ending.detail:
                fields["detail"] = ending.detail
            self._events.emit(Event.STOPPING, **fields)
        self._write_state(LauncherState.STOPPING)
        self._stop_children()
        left_up = self._stop_cluster()
        self._state.write(
            InstanceRecord(
                state=LauncherState.STOPPED,
                cluster=ClusterArrival.STARTED if left_up else None,
                postmaster_pid=self._postmaster_pid if left_up else None,
                last_ready_port=self._last_ready_port,
            )
        )
        if announce:
            self._events.emit(Event.STOPPED, exit_code=ending.code)
        logger.info("Stopped (exit code %s)", ending.code)

    def _stop_children(self) -> None:
        """Os dois ao mesmo tempo, cada um até o seu prazo; `TerminateProcess` depois.

        Nada depende do outro: a API e o job runner precisam só do banco, e por
        isso o banco é parado depois deles -- o `release()` de um job é uma
        escrita.
        """
        stopping: list[_Supervised] = []
        for supervised in (self._api, self._job_runner):
            child = supervised.child
            if child is None:
                continue
            supervised.restart_at = None
            code = child.poll()
            if code is not None:
                # Morto desde o último tick: "stopped in 0.0 s" atribuiria à
                # parada uma saída que ela nunca causou (§7.5).
                child.drain()
                supervised.child = None
                self._log_exit(
                    supervised.program.name,
                    code,
                    child.pid,
                    child.last_lines(),
                    before_the_stop=True,
                )
                continue
            child.request_stop()
            stopping.append(supervised)
        started_at = self._clock.monotonic()
        asked = self._requests.count()
        while stopping:
            now = self._clock.monotonic()
            skip = self._requests.count() > asked
            for supervised in list(stopping):
                child = supervised.child
                assert child is not None
                code = child.poll()
                if code is not None:
                    child.drain()
                    supervised.child = None
                    stopping.remove(supervised)
                    # O código diz se ele parou ou quebrou no caminho: um
                    # `release()` que falhou sai com um traceback e 1.
                    logger.info(
                        "%s stopped in %.1f s, with %s",
                        supervised.program.name,
                        now - started_at,
                        exit_code(code),
                    )
                    continue
                if skip or now - started_at >= self._deadline(supervised):
                    self._terminate(supervised, child, skipped=skip)
                    supervised.child = None
                    stopping.remove(supervised)
            if stopping:
                self._clock.sleep(self._timings.tick)

    def _deadline(self, supervised: _Supervised) -> float:
        session = self._requests.session_ending()
        if supervised is self._api:
            return self._timings.session_api_stop if session else self._timings.api_stop
        return (
            self._timings.session_job_runner_stop
            if session
            else self._timings.job_runner_stop
        )

    def _terminate(
        self, supervised: _Supervised, child: Child, *, skipped: bool
    ) -> None:
        name = supervised.program.name
        why = (
            "a second request skipped the deadline"
            if skipped
            else f"it did not stop within {self._deadline(supervised):.0f} s"
        )
        # O launcher não sabe se o job runner detinha um job: se ele ainda
        # estava importando o `torch` -- a carga de uma DLL não volta ao Python
        # por segundos, e a interrupção a espera --, não detinha nenhum.
        cost = (
            "the requests in flight were cut"
            if supervised is self._api
            else "a job it held, if any, stays running until the reaper takes it "
            "back, up to 120 s after its last heartbeat, and costs an attempt"
        )
        logger.warning("Ended %s with TerminateProcess: %s; %s", name, why, cost)
        child.terminate()

    def _stop_cluster(self) -> bool:
        """Para o cluster se um launcher o iniciou; diz se ele ficou no ar.

        Um adotado também, quando a sessão está terminando: o Windows o encerra
        um instante depois de qualquer jeito, e uma parada ordenada poupa o
        próximo início de uma crash recovery (§7.5). Ele continua não sendo
        deste launcher, e uma parada que falha não deixa posse para trás.
        """
        if self._cluster is None:
            return False
        owned = self._arrival is ClusterArrival.STARTED
        ending_anyway = (
            self._arrival is ClusterArrival.ADOPTED and self._requests.session_ending()
        )
        if not (owned or ending_anyway):
            return False
        if ending_anyway:
            logger.info(
                "The end of the session ends the cluster this launcher adopted; "
                "stopping it in order first"
            )
        started_at = time.monotonic()
        try:
            if self._requests.session_ending():
                # O `pg_ctl` é um processo, e nenhum inicia durante um logoff.
                outcome = self._cluster.stop_by_signal(
                    self._timings.session_cluster_stop
                )
            else:
                outcome = self._cluster.stop()
        except EmbeddedPostgresError as error:
            logger.error("The database could not be stopped: %s", error)
            return owned
        logger.info(
            "Database: %s in %.1f s", outcome.value, time.monotonic() - started_at
        )
        return False

    # ------------------------------------------------------------------

    def _write_state(self, state: LauncherState) -> None:
        port = self._announced_port or self._api.port
        self._state.write(
            InstanceRecord(
                state=state,
                pid=os.getpid(),
                url=_url(port) if port is not None else None,
                children={
                    supervised.program.name: supervised.child.pid
                    for supervised in (self._api, self._job_runner)
                    if supervised.child is not None
                },
                cluster=self._arrival,
                postmaster_pid=self._postmaster_pid,
                last_ready_port=self._last_ready_port,
            )
        )


def exit_code(code: int) -> str:
    """Um código de saída como o log o diz; um NTSTATUS também em hex, que é
    como se procura um -- 3221225786 é `0xC000013A`, o Ctrl+C de um console."""
    if 0 <= code <= 255:
        return str(code)
    return f"{code} (0x{code & 0xFFFFFFFF:08X})"


def _url(port: int) -> str:
    return f"http://{LOOPBACK}:{port}"
