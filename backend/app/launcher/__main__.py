r"""The launcher's commands (RFC-036 section 4.3), and the one module that reads
the `Settings`.

    python -m app.launcher                 start everything and supervise it,
                                           until stopped
    python -m app.launcher --supervised    the same, and stop when stdin ends
    python -m app.launcher status          what is up, and where
    python -m app.launcher stop            ask the launcher that is up to stop,
                                           and wait for it

    exit  when
    0     stopped in order; or another launcher was up, and this one said where
    78    could not start, and trying again does not change that: the
          configuration, the cluster refused, the API that gave up
    1     anything else, with the traceback in the log

**The order of the first lines is the order of section 5.1**, and it is why
most imports here are inside functions. The signal handler first, so that a
Ctrl+C or the end of stdin is never a `KeyboardInterrupt` in the middle of
something. Then the lock of the data root: a second launcher reads the state
file, writes `already_running`, and leaves with 0 -- without ever reading the
configuration, whose import alone could refuse. Only then the `Settings`, the
logger, and everything that imports them.

`stdout` is the events' and nothing else's: the real stream is kept for them,
and `sys.stdout` points at stderr, so that a stray `print` -- of this code or
of a library -- cannot break a line of the protocol.

In a checkout, run from the repository root with `backend` on the path, as the
cluster's CLI is (RFC-033 section 11). Installed, the shortcut or the window
run the same module with the installation's interpreter.
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
"""How long `stop` waits for the launcher to let go of its lock: the deadlines
of both children (15 s and 30 s), the cluster's `stop()`, and the margin."""

EVENT_WAIT_SECONDS = 5.0
"""How long `stop` retries a launcher that holds the lock and has not created
its event yet -- it is in its first instants."""

SESSION_STOP_SECONDS = 30.0
"""How long the window of the session end waits for the stop (section 7.5)."""


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
# The launcher


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
    """Say at once that this launcher holds the data root.

    Before the configuration is read, so that a second opening in the next
    instants says `starting` and this PID, and not what the last launcher left
    -- measured: `already_running` with `"state": "stopped"` and no PID, for a
    launcher 0.5 s into its start. What the last one left about the cluster
    and the port is kept: if this launcher dies before it writes its own, the
    next one still needs them (sections 5.4 and 7.3).
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
        # The children would read the same file in the same mode, and refuse
        # the same way: the launcher refuses for them (section 5.1).
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
    from .session_end import SessionEndWindow
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

    window = SessionEndWindow(stop_for_the_session)
    window.start()
    try:
        return supervisor.run()
    finally:
        window.close()


def _streams_for_the_protocol() -> TextIO:
    """The real stdout, kept for the events; `sys.stdout` pointed at stderr.

    Both in UTF-8: the children's lines pass through stderr, and a window
    reading the pipe would otherwise get them in the ANSI code page, with
    whatever does not fit in it escaped.
    """
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="backslashreplace")
    events_stream = sys.stdout
    if events_stream is None:
        # No stdout at all (`pythonw`): nobody reads the events.
        events_stream = open(os.devnull, "w", encoding="utf-8")
    sys.stdout = sys.stderr
    return events_stream


def _listen_for_interrupts(requests: StopRequests) -> None:
    """Ctrl+C, Ctrl+Break, and the SIGINT of the stdin watch: a request, counted.

    Never a `KeyboardInterrupt`: the supervisor honours the request at the next
    safe point, and nothing -- an `initdb`, a recovery, a migration -- is cut
    in half. The second request during the stop is the order that skips the
    deadlines (section 7.2).
    """

    def interrupted(signum: int, frame: FrameType | None) -> None:
        requests.ask()

    signal.signal(signal.SIGINT, interrupted)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, interrupted)
    if sys.platform == "win32":
        # Ignoring Ctrl+C is an attribute a process inherits from its parent,
        # and a parent may have left it on: measured, a launcher started that
        # way never saw the Ctrl+C sent to its console, while Ctrl+Break got
        # through. Restored here, for this process only -- the children have
        # consoles of their own, which no Ctrl+C of this one reaches.
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.SetConsoleCtrlHandler.argtypes = [ctypes.c_void_p, wintypes.BOOL]
        kernel32.SetConsoleCtrlHandler(None, False)


# ----------------------------------------------------------------------
# status and stop


def _say(message: str) -> None:
    print(f"launcher: {message}", file=sys.stderr, flush=True)


def _status(where: Layout) -> int:
    """What is up, and where. Never the password; reads, and starts nothing."""
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
    """Ask the launcher of this data root to stop, and wait for it to go.

    With no launcher up, the state file decides (section 4.3): a cluster a
    launcher started, and that is still up, is stopped -- what is left of a
    launcher that died (7.4). Anything else is left alone.
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
