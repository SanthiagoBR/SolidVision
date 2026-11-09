r"""Os comandos do launcher (RFC-036 §4.3), e o único módulo que lê os `Settings`.

    python -m app.launcher                 inicia tudo e supervisiona, até ser
                                           parado
    python -m app.launcher --supervised    o mesmo, e para quando o stdin acaba
    python -m app.launcher status          o que está de pé, e onde
    python -m app.launcher stop            pede ao launcher de pé que pare, e
                                           espera por ele

    saída quando
    0     parou em ordem; ou outro launcher estava de pé, e este disse onde
    78    não pôde iniciar, e tentar de novo não muda isso: a configuração, o
          cluster recusou, a API que desistiu
    1     qualquer outra coisa, com o traceback no log

**A ordem das primeiras linhas é a ordem de §5.1**, e é por isso que a maioria
dos imports aqui fica dentro de funções. Primeiro o handler de sinais, para que
um Ctrl+C ou o fim do stdin nunca seja um `KeyboardInterrupt` no meio de
alguma coisa. Depois o lock da raiz de dados: um segundo launcher lê o arquivo
de estado, escreve `already_running` e sai com 0 -- sem nunca ler a
configuração, cujo import sozinho poderia recusar. Só então os `Settings`, o
logger e tudo o que os importa.

O `stdout` é dos eventos e de mais nada: o stream real fica guardado para eles,
e `sys.stdout` aponta para o stderr, para que um `print` perdido -- deste
código ou de uma biblioteca -- não quebre uma linha do protocolo.

Num checkout, rode a partir da raiz do repositório com `backend` no path, como
a CLI do cluster (RFC-033 §11). Instalado, o atalho ou a janela rodam o mesmo
módulo com o interpretador da instalação.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import signal
import sys
import time
from collections.abc import Sequence
from ctypes import wintypes
from types import FrameType
from typing import TextIO

from app.infrastructure.config.constants import EXIT_CANNOT_START
from app.infrastructure.config.layout import Layout, layout
from app.infrastructure.process.supervision import (
    SUPERVISED_FLAG,
    CannotStart,
    stop_on_stdin_eof,
)

from .events import PROTOCOL, Event, JsonLines
from .instance import (
    ClusterArrival,
    Instance,
    InstanceLock,
    InstanceRecord,
    LauncherState,
    StateFile,
    StopEvent,
    StopRequests,
    administrators_enabled,
)

STOP_WAIT_SECONDS = 180.0
"""Quanto tempo o `stop` espera o launcher soltar o lock: os prazos dos dois
filhos (15 s e 30 s), o `stop()` do cluster e a margem."""

EVENT_WAIT_SECONDS = 5.0
"""Por quanto tempo o `stop` insiste com um launcher que segura o lock e ainda
não criou o evento -- ele está nos primeiros instantes."""

SESSION_STOP_SECONDS = 30.0
"""Quanto tempo a janela do fim de sessão espera pela parada (§7.5)."""


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _build_arg_parser().parse_args(argv)
    if arguments.command == "status":
        return _status(layout)
    if arguments.command == "stop":
        return _stop(layout)
    return _launch(layout, supervised=arguments.supervised)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.launcher",
        description="Start the SolidVision backend, supervise it, and stop it in "
        "order (RFC-036).",
    )
    parser.add_argument(
        SUPERVISED_FLAG,
        action="store_true",
        help="Stop in order when stdin reaches its end: how a window runs it.",
    )
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("status", help="Say what is up, and where.")
    commands.add_parser(
        "stop", help="Ask the launcher that is up to stop, and wait for it."
    )
    return parser


# ----------------------------------------------------------------------
# O launcher


def _launch(where: Layout, *, supervised: bool) -> int:
    events_stream = _streams_for_the_protocol()
    requests = StopRequests()
    _listen_for_interrupts(requests)
    if supervised:
        stop_on_stdin_eof()
    events = JsonLines(events_stream)
    instance = Instance(where.data_directory)
    lock = InstanceLock(instance.lock)
    state = StateFile(instance.state)
    if not lock.acquire():
        record = state.read()
        events.emit(
            Event.ALREADY_RUNNING,
            protocol=PROTOCOL,
            pid=record.pid if record else None,
            url=record.url if record else None,
            state=record.state.value if record else None,
        )
        return 0
    try:
        previous = state.read()
        _claim(state, previous, LauncherState.STARTING)
        stop_event = StopEvent.create(instance.stop_event_name)
        requests.poll(stop_event.is_set)
        try:
            return _run(where, events, state, previous, requests)
        finally:
            stop_event.close()
    finally:
        lock.release()


def _claim(
    state: StateFile, previous: InstanceRecord | None, now: LauncherState
) -> None:
    """Diz de imediato que este launcher segura a raiz de dados.

    Antes de a configuração ser lida, para que uma segunda abertura nos
    instantes seguintes diga `starting` e este PID, e não o que o último
    launcher deixou -- medido: `already_running` com `"state": "stopped"` e
    sem PID, para um launcher a 0,5 s do início. O que o último deixou sobre o
    cluster e a porta é mantido: se este launcher morrer antes de gravar os
    seus, o próximo ainda precisa deles (§5.4 e §7.3).
    """
    state.write(
        InstanceRecord(
            state=now,
            pid=os.getpid() if now is LauncherState.STARTING else None,
            cluster=previous.cluster if previous else None,
            postmaster_pid=previous.postmaster_pid if previous else None,
            last_ready_port=previous.last_ready_port if previous else None,
        )
    )


def _run(
    where: Layout,
    events: JsonLines,
    state: StateFile,
    previous: InstanceRecord | None,
    requests: StopRequests,
) -> int:
    starting: dict[str, object] = {
        "protocol": PROTOCOL,
        "pid": os.getpid(),
        "mode": where.environment.value,
    }
    try:
        from app.infrastructure.config.settings import DatabaseSource, settings
    except CannotStart as refusal:
        # Os filhos leriam o mesmo arquivo no mesmo modo, e recusariam do
        # mesmo jeito: o launcher recusa por eles (§5.1).
        events.emit(Event.STARTING, **starting)
        events.emit(Event.FAILED, detail=refusal.line)
        _claim(state, previous, LauncherState.STOPPED)
        return EXIT_CANNOT_START
    events.emit(Event.STARTING, **starting, database=settings.database_source.value)

    from app.infrastructure.database.embedded.cluster import (
        EmbeddedPostgres,
        PortProber,
    )
    from app.infrastructure.database.embedded.cluster_home import ClusterHome
    from app.infrastructure.database.embedded.runtime import PostgresRuntime
    from app.infrastructure.logging.logger import get_logger

    from .children import ChildProcess, Program, StderrEcho, child_command
    from .session_end import SessionEndWindow, end_before_the_children
    from .supervisor import ApiPorts, Supervisor

    logger = get_logger("launcher")
    if administrators_enabled():
        logger.warning(
            "This launcher runs with the Administrators group in force in its "
            "token -- an installer that opened the app elevated. Its data root "
            "is %s: the profile of the account it runs as (RFC-034 section 13)",
            where.data_directory,
        )

    cluster = None
    if settings.database_source is DatabaseSource.EMBEDDED:
        cluster = EmbeddedPostgres(
            runtime=PostgresRuntime(settings.embedded_postgres_runtime_directory),
            home=ClusterHome(settings.embedded_postgres_directory),
        )
    else:
        logger.info(
            "The database source is external: the launcher starts, migrates and "
            "stops no database (RFC-036 section 9)"
        )

    echo = StderrEcho(sys.stderr)

    def spawn(program: Program, arguments: Sequence[str]) -> ChildProcess:
        return ChildProcess.start(
            program.name, child_command(program, arguments), echo=echo
        )

    supervisor = Supervisor(
        spawn=spawn,
        ports=ApiPorts(
            PortProber(),
            fixed=settings.api_port,
            last=previous.last_ready_port if previous else None,
        ),
        events=events,
        state=state,
        requests=requests,
        cluster=cluster,
        previous=previous,
    )

    def stop_for_the_session() -> None:
        requests.end_session()
        supervisor.finished.wait(SESSION_STOP_SECONDS)

    end_before_the_children()
    window = SessionEndWindow(stop_for_the_session)
    window.start()
    try:
        return supervisor.run()
    finally:
        window.close()


def _streams_for_the_protocol() -> TextIO:
    """O stdout real, guardado para os eventos; `sys.stdout` apontado para o stderr.

    Os dois em UTF-8: as linhas dos filhos passam pelo stderr, e uma janela
    lendo o pipe as receberia, do contrário, na code page ANSI, com o que não
    cabe nela escapado.
    """
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="backslashreplace")
    events_stream = sys.stdout
    if events_stream is None:
        # Nenhum stdout (`pythonw`): ninguém lê os eventos.
        events_stream = open(os.devnull, "w", encoding="utf-8")
    sys.stdout = sys.stderr
    return events_stream


def _listen_for_interrupts(requests: StopRequests) -> None:
    """Ctrl+C, Ctrl+Break e o SIGINT da vigia do stdin: um pedido, contado.

    Nunca um `KeyboardInterrupt`: o supervisor atende o pedido no próximo ponto
    seguro, e nada -- um `initdb`, uma recuperação, uma migration -- é cortado
    ao meio. O segundo pedido durante a parada é a ordem que pula os prazos
    (§7.2).
    """

    def interrupted(signum: int, frame: FrameType | None) -> None:
        requests.ask()

    signal.signal(signal.SIGINT, interrupted)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, interrupted)
    if sys.platform == "win32":
        # Ignorar o Ctrl+C é um atributo que um processo herda do pai, e um pai
        # pode tê-lo deixado ligado: medido, um launcher iniciado assim nunca
        # viu o Ctrl+C mandado ao seu console, enquanto o Ctrl+Break passou.
        # Restaurado aqui, só para este processo -- os filhos têm consoles
        # próprios, que nenhum Ctrl+C deste alcança.
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.SetConsoleCtrlHandler.argtypes = [ctypes.c_void_p, wintypes.BOOL]
        kernel32.SetConsoleCtrlHandler(None, False)


# ----------------------------------------------------------------------
# status e stop


def _say(message: str) -> None:
    print(f"launcher: {message}", file=sys.stderr, flush=True)


def _status(where: Layout) -> int:
    """O que está de pé, e onde. Nunca a senha; lê, e não inicia nada."""
    instance = Instance(where.data_directory)
    running = InstanceLock(instance.lock).held_elsewhere()
    record = StateFile(instance.state).read()
    print(f"data:      {where.data_directory}")
    if not running:
        print("launcher:  not running")
        if record is not None and record.cluster is ClusterArrival.STARTED:
            print(
                "cluster:   a launcher that is gone started it, and it may still be "
                "up: `python -m app.launcher stop` stops it"
            )
        if record is not None and record.last_ready_port is not None:
            print(f"last port: {record.last_ready_port}")
        return 0
    if record is None:
        print("launcher:  running (its state is not readable yet)")
        return 0
    print(f"launcher:  running, {record.state.value} (PID {record.pid})")
    if record.url:
        print(f"url:       {record.url}")
    for name, pid in record.children.items():
        print(f"{name + ':':<11}PID {pid}")
    if record.cluster is not None:
        print(f"cluster:   {record.cluster.value}")
    return 0


def _stop(where: Layout) -> int:
    """Pede ao launcher desta raiz de dados que pare, e espera que ele saia.

    Sem launcher de pé, o arquivo de estado decide (§4.3): um cluster que um
    launcher iniciou, e que ainda está de pé, é parado -- o que sobrou de um
    launcher que morreu (§7.4). Qualquer outra coisa fica como está.
    """
    instance = Instance(where.data_directory)
    lock = InstanceLock(instance.lock)
    if lock.acquire(attempts=1):
        try:
            return _stop_what_a_launcher_left(where, StateFile(instance.state))
        finally:
            lock.release()
    deadline = time.monotonic() + EVENT_WAIT_SECONDS
    while not StopEvent.signal(instance.stop_event_name):
        if time.monotonic() >= deadline:
            _say("a launcher holds the data root, and does not answer")
            return 1
        time.sleep(0.1)
    _say("asked the launcher to stop; waiting for it")
    deadline = time.monotonic() + STOP_WAIT_SECONDS
    while lock.held_elsewhere():
        if time.monotonic() >= deadline:
            _say(f"the launcher is still running after {STOP_WAIT_SECONDS:.0f} s")
            return 1
        time.sleep(0.2)
    _say("stopped")
    return 0


def _stop_what_a_launcher_left(where: Layout, state: StateFile) -> int:
    record = state.read()
    if record is None or record.cluster is not ClusterArrival.STARTED:
        _say("no launcher is running, and nothing a launcher started is up")
        return 0
    try:
        from app.infrastructure.config.settings import DatabaseSource, settings
    except CannotStart:
        return EXIT_CANNOT_START
    if settings.database_source is not DatabaseSource.EMBEDDED:
        _say("the database source is external: there is no cluster to stop")
        return 0

    from app.infrastructure.database.embedded.cluster import EmbeddedPostgres
    from app.infrastructure.database.embedded.cluster_home import ClusterHome
    from app.infrastructure.database.embedded.errors import EmbeddedPostgresError
    from app.infrastructure.database.embedded.runtime import PostgresRuntime

    cluster = EmbeddedPostgres(
        runtime=PostgresRuntime(settings.embedded_postgres_runtime_directory),
        home=ClusterHome(settings.embedded_postgres_directory),
    )
    try:
        report = cluster.status()
        if record.postmaster_pid is not None and report.pid != record.postmaster_pid:
            _say(
                "the cluster that is up is not the one a launcher started; it is "
                "left up"
            )
        else:
            _say(f"stopping the cluster a launcher started: {cluster.stop().value}")
    except EmbeddedPostgresError as error:
        _say(f"error: {error}")
        return 1
    state.write(
        InstanceRecord(
            state=LauncherState.STOPPED, last_ready_port=record.last_ready_port
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
