"""`EmbeddedPostgres`: the cluster's lifecycle, as a state machine (RFC-033).

    cluster = EmbeddedPostgres(
        runtime=PostgresRuntime(settings.embedded_postgres_runtime_directory),
        home=ClusterHome(settings.embedded_postgres_directory),
    )
    started = cluster.start(on_phase=report)  # creates if needed; starts or adopts
    cluster.prepare(started.connection)       # database and migrations, idempotent
    cluster.status()                          # absent | stopped | starting | ready
    cluster.locate()                          # the connection of a ready cluster
    cluster.stop()                            # fast; immediate if fast fails

What `start()` observes, and what it does (section 9.1):

    observed                                   action
    data.initdb exists                         delete it, then create (7.1)
    data does not exist                        atomic initdb
    PG_VERSION is not the manifest's major     refuse; never initdb over it
    no postmaster.pid                          start
    pid alive, this runtime, ready             adopt (8.4)
    the same, starting                         wait for ready (8.3)
    the same, stopping                         wait for the PID to go, then start
    pid alive, another runtime's postgres.exe  refuse, naming that runtime
    pid dead                                   pg_ctl start; the server recovers
    start fails: shared memory still in use    wait, end only our orphans (9.3),
                                               try once more
    start fails: bind                          another port (8.2, step 4)
    any other failure                          error with what the server logged

The adapter never deletes `postmaster.pid` (9.2) and never kills the
postmaster (8.5).
"""

from __future__ import annotations

import errno
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from app.infrastructure.logging.logger import get_logger

from .cluster_home import CREATE_NO_WINDOW, ClusterFiles, ClusterHome
from .database_setup import (
    LOOPBACK,
    SUPERUSER,
    ClusterConnection,
    PreparedDatabase,
    prepare_database,
    verify_login,
)
from .errors import (
    ClusterNotRunningError,
    ClusterRefusedError,
    EmbeddedPostgresError,
    ServerNotReadyError,
    StartupFailedError,
    StopFailedError,
)
from .postmaster_pid import (
    STATUS_READY,
    STATUS_STANDBY,
    STATUS_STOPPING,
    PostmasterPid,
    read_postmaster_pid,
)
from .postmaster_signal import FAST, IMMEDIATE, send
from .runtime import PostgresRuntime, RuntimeManifest
from .windows_processes import ProcessInfo, ProcessTable, WindowsProcesses

logger = get_logger(__name__)

INCLUDE_LINE = "include_if_exists = 'solidvision.conf'"

BIND_FAILURE_MARKERS = (
    "could not bind IPv4 address",
    "could not create any TCP/IP sockets",
)
"""Recognized by text, which is stable: this build has no translations, and
`--locale=C` set `lc_messages = 'C'` besides (section 8.1)."""

SHARED_MEMORY_IN_USE = "pre-existing shared memory block is still in use"

ENVIRONMENT_VARIABLES = (
    "DATABASE_SOURCE",
    "EMBEDDED_POSTGRES_DIRECTORY",
    "EMBEDDED_POSTGRES_RUNTIME_DIRECTORY",
)
"""What `run -- <command>` gives the command, named in one place (RFC-034 7.6).

The source and the two directories, and no longer host, port and password:
with these the command's own engine finds the cluster by `locate()`, as an
installed app does, and the password travels in no process's environment.

A test checks that every name is a field of `Settings`. A misspelt one would
not fail: `Settings` would fall back to the `.env`, the `.env` points at
Docker, and the suite would pass against the wrong database printing the
same `passed` (RFC-033a section 4.2).
"""

EMBEDDED_SOURCE = "embedded"
"""`DATABASE_SOURCE`'s value, spelt here because this package reads no
`settings`. The same test checks it against `DatabaseSource`."""

HOW_TO_START = "python -m app.infrastructure.database.embedded start"

_WSAEACCES = 10013
_WSAEADDRINUSE = 10048

_PG_CONTROL_VERSION = 1300
_DB_SHUTDOWNED = 1
_DB_SHUTDOWNED_IN_RECOVERY = 2

POLL_SECONDS = 0.1


class Phase(Enum):
    """What `on_phase` reports, as it happens.

    The first run takes tens of seconds (RFC-033a section 13), and a screen
    that does not move for that long looks frozen.
    """

    INITDB = "initdb"
    STARTING = "starting"
    RECOVERING = "recovering"
    MIGRATING = "migrating"


class ClusterStatus(Enum):
    ABSENT = "absent"
    STOPPED = "stopped"
    STARTING = "starting"
    READY = "ready"
    STOPPING = "stopping"
    OTHER_VERSION = "other version"
    OTHER_RUNTIME = "other runtime"


class Observation(Enum):
    """One row of section 9.1's table: what was seen, before any action."""

    INTERRUPTED_INITDB = "interrupted initdb"
    ABSENT = "absent"
    OTHER_MAJOR = "other major version"
    STOPPED = "stopped"
    STALE = "stale postmaster.pid"
    READY = "ready"
    STARTING = "starting"
    STOPPING = "stopping"
    FOREIGN = "held by another runtime"


@dataclass(frozen=True)
class ClusterState:
    observation: Observation
    pid_file: PostmasterPid | None = None
    detail: str = ""


@dataclass(frozen=True)
class StartedCluster:
    """The connection, and three facts about how it came to be (section 4.2).

    `started_here` is how the launcher of RFC-036 decides who stops the
    database at the end: the caller that started it, never one that adopted.
    """

    connection: ClusterConnection
    started_here: bool
    created: bool
    recovered: bool


@dataclass(frozen=True)
class ClusterReport:
    """`status()`: what is there now. Never the password."""

    status: ClusterStatus
    data_directory: Path
    port: int | None = None
    pid: int | None = None
    detail: str = ""


class StopOutcome(Enum):
    NOT_RUNNING = "not running"
    FAST = "stopped (fast)"
    IMMEDIATE = "stopped (immediate, after fast failed)"


@dataclass(frozen=True)
class ClusterTimeouts:
    """Every wait the adapter makes, with the measurement each comes from."""

    pg_ctl_start: int = 30
    """`T` of section 8.3: a normal start with room to spare.

    Measured (RFC-033 section 18): `pg_ctl start -w` took 0.6 to 1.9 s on a
    clean start, and up to 9.6 s when the server had a crash to recover from.
    Past `T`, a live postmaster still in `starting` is recovering, not hung,
    and the adapter keeps waiting up to `recovery_ceiling`.
    """

    recovery_ceiling: float = 600.0
    """How long a crash recovery may take before the adapter gives up waiting.

    It does not stop the server when it gives up. This is a choice, not a
    measurement: the longest recovery measured (RFC-033 section 18) took
    13.4 s, on an SSD, and recovery grows with the WAL written since the last
    checkpoint and with the disk. What a slow disk needs was not measured.
    """

    pg_ctl_stop: int = 60
    lock: float = 900.0
    """Longer than anything another holder of the lock can be doing: an
    `initdb` (38.9 s, the worst RFC-033a measured) plus a start up to
    `recovery_ceiling`."""

    orphan_grace: float = 5.0
    """How long a dead postmaster's children get to exit on their own.

    Measured (RFC-033 section 18): on an idle cluster the last child is
    gone 0.05 to 0.17 s after the postmaster. Only a child that outlives the
    grace is ended, which happened unprovoked once in 27 kills of the tree.
    """

    stopping_wait: float = 60.0
    initdb: float = 600.0
    rename: float = 10.0
    max_port_attempts: int = 3
    """Starts in a row refused by `bind` before giving up (section 8.2)."""


class PortProber:
    """Section 8.2, steps 2 and 3: probe a port, or ask the system for one."""

    def usable(self, port: int) -> bool:
        """Try to bind it, and see whether Windows lets us.

        Both "in use" (`WSAEADDRINUSE`) and "access denied" (`WSAEACCES`, a
        range Hyper-V, WSL or Docker reserved) mean "no". The probe asks for
        the port exclusively, so it also says "no" to a port another socket
        holds in a way that would let a second bind share it. A wrong "no"
        costs a new port number; a wrong "yes" costs a start that fails and
        is retried (step 4).
        """
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
            if exclusive is not None:
                probe.setsockopt(socket.SOL_SOCKET, exclusive, 1)
            try:
                probe.bind((LOOPBACK, port))
            except OSError as error:
                codes = {getattr(error, "winerror", None), error.errno}
                refusals = {
                    _WSAEADDRINUSE,
                    _WSAEACCES,
                    errno.EADDRINUSE,
                    errno.EACCES,
                }
                if codes & refusals:
                    return False
                raise
            return True

    def free_port(self) -> int:
        """Port 0: the system picks a free one, outside the excluded ranges."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind((LOOPBACK, 0))
            return int(probe.getsockname()[1])


class PostgresCommands:
    """`initdb` and `pg_ctl`, run the way section 8.3 says.

    `CREATE_NO_WINDOW`: `pg_ctl` gets a console without a window and the
    server inherits it, so no black window opens for a caller without a
    console, and a Ctrl+C in the caller's terminal does not reach a console
    the server does not share. Output always to a file, never to a pipe: the
    server would inherit the pipe, and whoever waited for `pg_ctl` to finish
    would wait for the server to finish.
    """

    def __init__(self, runtime: PostgresRuntime) -> None:
        self._runtime = runtime

    def initdb(
        self, target: Path, password_file: Path, timeout: float
    ) -> tuple[int, str]:
        """RFC-033a's flags: scram, the password file, `UTF8` and `C`.

        A pipe is safe here: every process `initdb` starts has exited by the
        time it does.
        """
        completed = subprocess.run(
            [
                str(self._runtime.initdb),
                "-D",
                str(target),
                "-U",
                SUPERUSER,
                "--auth=scram-sha-256",
                f"--pwfile={password_file}",
                "--encoding=UTF8",
                "--locale=C",
            ],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            env=self._runtime.child_environment(),
            creationflags=CREATE_NO_WINDOW,
            timeout=timeout,
        )
        return completed.returncode, _decode(completed.stdout + completed.stderr)

    def clear_startup_log(self, files: ClusterFiles) -> None:
        """Empty `startup.log`: it is rewritten on every start (section 8.1).

        Raises `PermissionError` while any process of the server is alive.
        `pg_ctl` does not start `postgres.exe` itself: it starts
        `cmd.exe /D /C ""postgres.exe" -D "..." < "nul" >> "startup.log" 2>&1"`
        (read from the running `cmd.exe`), and from then until the last
        process of that server has exited the file cannot be opened for
        writing by anyone else.
        """
        files.startup_log.write_bytes(b"")

    def start(self, files: ClusterFiles, timeout: int) -> int:
        return self._pg_ctl(
            files,
            [
                "start",
                "-w",
                "-t",
                str(timeout),
                "-D",
                str(files.data),
                "-l",
                str(files.startup_log),
            ],
            timeout,
        )

    def stop(self, files: ClusterFiles, mode: str, timeout: int) -> int:
        return self._pg_ctl(
            files,
            ["stop", "-m", mode, "-w", "-t", str(timeout), "-D", str(files.data)],
            timeout,
        )

    def _pg_ctl(self, files: ClusterFiles, arguments: list[str], timeout: int) -> int:
        with files.pg_ctl_log.open("wb") as output:
            completed = subprocess.run(
                [str(self._runtime.pg_ctl), *arguments],
                stdout=output,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                env=self._runtime.child_environment(),
                creationflags=CREATE_NO_WINDOW,
                timeout=timeout + 60,
            )
        return completed.returncode


def _decode(raw: bytes) -> str:
    return raw.decode("mbcs" if sys.platform == "win32" else "utf-8", "replace")


def _tail(text: str, lines: int = 15) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


def _ignore(_: Phase) -> None:
    return None


class EmbeddedPostgres:
    """The adapter. Holds no state between calls: the files are the state."""

    def __init__(
        self,
        runtime: PostgresRuntime,
        home: ClusterHome,
        *,
        processes: ProcessTable | None = None,
        commands: PostgresCommands | None = None,
        ports: PortProber | None = None,
        login_check: Callable[[ClusterConnection], None] = verify_login,
        timeouts: ClusterTimeouts | None = None,
        signal: Callable[[int, int], bool] = send,
    ) -> None:
        if sys.platform != "win32":
            raise EmbeddedPostgresError(
                "EmbeddedPostgres requires Windows; this process is running on "
                f"{sys.platform!r}."
            )
        self._runtime = runtime
        self._home = home
        self._processes = processes if processes is not None else WindowsProcesses()
        self._commands = commands if commands is not None else PostgresCommands(runtime)
        self._ports = ports if ports is not None else PortProber()
        self._login_check = login_check
        self._timeouts = timeouts if timeouts is not None else ClusterTimeouts()
        self._signal = signal

    # -- the contract (section 4.2) ---------------------------------------

    def start(self, on_phase: Callable[[Phase], None] | None = None) -> StartedCluster:
        """Create if needed; start, or adopt a cluster that is already up."""
        report = on_phase or _ignore
        manifest = self._runtime.manifest()
        files = self._home.files(manifest.major)
        self._home.ensure_directory(files)
        files = self._home.for_commands(files)
        with self._home.transition_lock(files, self._timeouts.lock):
            state = self._observe(files, manifest)
            if state.observation is Observation.INTERRUPTED_INITDB:
                logger.warning(
                    "Removing %s, left by an interrupted initdb", files.data_initdb
                )
                self._discard_interrupted_initdb(files)
                state = self._observe(files, manifest)
            created = False
            if state.observation is Observation.ABSENT:
                self._create(files, report)
                created = True
                state = self._observe(files, manifest)
            self._refuse_foreign(files, state)
            password = self._home.read_password(files)
            if password is None:
                raise ClusterRefusedError(
                    f"{files.data} holds a cluster, but {files.password} is "
                    "missing: the password-cluster pair is lost."
                )
            if state.observation is Observation.STOPPING:
                self._wait_until_gone(files, state)
                state = self._observe(files, manifest)
                self._refuse_foreign(files, state)
            if state.observation in (Observation.READY, Observation.STARTING):
                return self._adopt(files, state, password, report)
            recovered = not self._shut_down_cleanly(files)
            port = self._start_server(files, state, report, recovered)
            connection = ClusterConnection(port=port, password=password)
            self._login(connection, report)
            logger.info(
                "Embedded cluster started on %s:%s (created: %s, recovered: %s)",
                LOOPBACK,
                port,
                created,
                recovered,
            )
            return StartedCluster(
                connection=connection,
                started_here=True,
                created=created,
                recovered=recovered,
            )

    def prepare(
        self,
        connection: ClusterConnection,
        on_phase: Callable[[Phase], None] | None = None,
    ) -> PreparedDatabase:
        """The database, the migrations and the facts (section 7.4)."""
        report = on_phase or _ignore
        manifest = self._runtime.manifest()
        return prepare_database(
            connection,
            major=manifest.major,
            pgvector=manifest.pgvector,
            on_migrating=lambda: report(Phase.MIGRATING),
        )

    def status(self) -> ClusterReport:
        """A read of the files and the process table; takes no lock."""
        manifest = self._runtime.manifest()
        shown = self._home.files(manifest.major).data
        files = self._home.for_commands(self._home.files(manifest.major))
        if not files.data.exists():
            return ClusterReport(ClusterStatus.ABSENT, shown)
        state = self._observe(files, manifest, leftover_initdb=False)
        status = {
            Observation.OTHER_MAJOR: ClusterStatus.OTHER_VERSION,
            Observation.STOPPED: ClusterStatus.STOPPED,
            Observation.STALE: ClusterStatus.STOPPED,
            Observation.READY: ClusterStatus.READY,
            Observation.STARTING: ClusterStatus.STARTING,
            Observation.STOPPING: ClusterStatus.STOPPING,
            Observation.FOREIGN: ClusterStatus.OTHER_RUNTIME,
        }[state.observation]
        running = status in (
            ClusterStatus.READY,
            ClusterStatus.STARTING,
            ClusterStatus.STOPPING,
            ClusterStatus.OTHER_RUNTIME,
        )
        pid_file = state.pid_file if running else None
        return ClusterReport(
            status=status,
            data_directory=shown,
            port=pid_file.port if pid_file else None,
            pid=pid_file.pid if pid_file else None,
            detail=state.detail,
        )

    def locate(self) -> ClusterConnection:
        """The connection of a cluster that is up. Reads; starts nothing.

        RFC-034 section 7.2. No lock, no connection, no waiting: the port the
        server is *using*, from line 4 of `postmaster.pid`, and the password
        from its file, after the checks `status()` makes -- the PID is alive,
        the executable is this runtime's, and the lock file names this
        `data\\`. Anything but `ready` is `ClusterNotRunningError`.

        `ready` in the file is not `ready` in the server (RFC-033 section
        8.4). A connection opened while the server reinitializes is refused
        by the server, and the caller gets an ordinary database error.
        """
        manifest = self._runtime.manifest()
        shown = self._home.files(manifest.major).data
        files = self._home.for_commands(self._home.files(manifest.major))
        if not files.data.exists():
            raise ClusterNotRunningError(
                f"There is no embedded cluster in {shown} yet. "
                f"Create and start it with `{HOW_TO_START}`."
            )
        state = self._observe(files, manifest, leftover_initdb=False)
        if state.observation is not Observation.READY or state.pid_file is None:
            found = state.observation.value
            if state.detail:
                found = f"{found} ({state.detail})"
            raise ClusterNotRunningError(
                f"The embedded cluster in {shown} is not ready to be connected "
                f"to: {found}. Start it with `{HOW_TO_START}`."
            )
        if state.pid_file.port is None:
            raise ClusterNotRunningError(
                f"The embedded cluster in {shown} is up, but "
                f"{files.postmaster_pid} names no port yet."
            )
        password = self._home.read_password(files)
        if password is None:
            raise ClusterRefusedError(
                f"{shown} holds a running cluster, but {files.password} is "
                "missing: the password-cluster pair is lost."
            )
        return ClusterConnection(port=state.pid_file.port, password=password)

    def environment(self) -> dict[str, str]:
        """`ENVIRONMENT_VARIABLES` for a child process, pointing it at this cluster.

        Above the `.env` in a checkout (RFC-004). Neither the port nor the
        password is here: the child reads both from the cluster, by `locate()`.
        """
        return {
            "DATABASE_SOURCE": EMBEDDED_SOURCE,
            "EMBEDDED_POSTGRES_DIRECTORY": str(self._home.root),
            "EMBEDDED_POSTGRES_RUNTIME_DIRECTORY": str(self._runtime.root),
        }

    def stop(self) -> StopOutcome:
        """`fast`; `immediate` if `fast` fails; an error if both do (8.5)."""
        manifest = self._runtime.manifest()
        files = self._home.for_commands(self._home.files(manifest.major))
        if not files.data.exists():
            return StopOutcome.NOT_RUNNING
        with self._home.transition_lock(files, self._timeouts.lock):
            state = self._observe(files, manifest, leftover_initdb=False)
            self._refuse_foreign(files, state)
            running = (Observation.READY, Observation.STARTING, Observation.STOPPING)
            if state.observation not in running:
                return StopOutcome.NOT_RUNNING
            assert state.pid_file is not None
            postmaster = state.pid_file.pid
            timeout = self._timeouts.pg_ctl_stop
            if self._commands.stop(files, "fast", timeout) == 0:
                self._wait_for_exit(postmaster)
                logger.info("Embedded cluster stopped (fast)")
                return StopOutcome.FAST
            logger.warning("Fast shutdown failed; trying immediate")
            if self._commands.stop(files, "immediate", timeout) == 0:
                self._wait_for_exit(postmaster)
                return StopOutcome.IMMEDIATE
            raise StopFailedError(
                f"Neither a fast nor an immediate shutdown stopped the cluster in "
                f"{files.data}. The adapter does not kill the postmaster. "
                f"pg_ctl said:\n{_tail(self._read_text(files.pg_ctl_log))}"
            )

    def stop_by_signal(self, timeout: float) -> StopOutcome:
        """`stop()` without starting a process: for a session that is ending.

        RFC-036 section 7.5. During a logoff `pg_ctl` cannot start, so the
        postmaster is sent the signal `pg_ctl` would have sent, through its
        signal pipe: `fast`, and `immediate` if the server is still there
        `timeout` later. Each waits for the processes to be gone, not for
        `postmaster.pid` -- the next start finds no server either way. Up to
        `timeout` for the lock too: at a logoff, nobody else should hold it,
        and the session will not wait 15 minutes. The same refusals and the
        same error as `stop()`.
        """
        manifest = self._runtime.manifest()
        files = self._home.for_commands(self._home.files(manifest.major))
        if not files.data.exists():
            return StopOutcome.NOT_RUNNING
        with self._home.transition_lock(files, timeout):
            state = self._observe(files, manifest, leftover_initdb=False)
            self._refuse_foreign(files, state)
            running = (Observation.READY, Observation.STARTING, Observation.STOPPING)
            if state.observation not in running:
                return StopOutcome.NOT_RUNNING
            assert state.pid_file is not None
            postmaster = state.pid_file.pid
            # Listed before the signal: a process on its way out may no longer
            # say what it runs, and would pass for gone (measured).
            server = sorted({postmaster, *self._server_processes(postmaster)})
            if self._signal(postmaster, FAST) and self._gone(server, timeout):
                logger.info("Embedded cluster stopped (fast, by its signal pipe)")
                return StopOutcome.FAST
            logger.warning("Fast shutdown by the signal pipe failed; trying immediate")
            if self._signal(postmaster, IMMEDIATE) and self._gone(server, timeout):
                return StopOutcome.IMMEDIATE
            raise StopFailedError(
                f"Neither a fast nor an immediate shutdown, sent through the "
                f"signal pipe of PID {postmaster}, stopped the cluster in "
                f"{files.data} within {timeout:.0f} s each. The adapter does not "
                "kill the postmaster."
            )

    def _gone(self, server: list[int], timeout: float) -> bool:
        """Whether every process in `server` exited within `timeout`."""
        deadline = time.monotonic() + timeout
        while any(self._processes.is_alive(pid) for pid in server):
            if time.monotonic() >= deadline:
                return False
            time.sleep(POLL_SECONDS / 10)
        return True

    def _server_processes(self, postmaster: int) -> list[int]:
        """The postmaster and its children that exist right now, by identity.

        Each one counted here is a program of this runtime, and not only a
        number. A process records its parent's PID once and Windows never
        updates it, so a process that has nothing to do with the cluster
        shows up as a "child" of the postmaster whenever its own long-gone
        parent had the number the postmaster has now -- on the machine this
        was measured on, 18 of 328 processes named a parent PID that
        belonged to nobody any more.
        """
        return [
            process.pid
            for process in self._processes.snapshot()
            if (process.pid == postmaster or process.parent_pid == postmaster)
            and self._ours(process.pid)
        ]

    def _wait_for_exit(self, postmaster: int) -> None:
        """Return from `stop()` only when the processes are gone, not just the lock.

        `pg_ctl stop -w` waits for `postmaster.pid` to disappear, which the
        postmaster does on its way out; the processes themselves are still
        there for a moment after. A caller told "stopped" should find no
        `postgres.exe` of this cluster, and a `start()` that follows should
        find `startup.log` free. Nobody is ended here: the lock is released,
        and what is left is leaving.
        """
        deadline = time.monotonic() + self._timeouts.orphan_grace
        while self._server_processes(postmaster):
            if time.monotonic() >= deadline:
                logger.warning(
                    "The cluster released its lock, but some of its processes "
                    "are still exiting after %.0f s",
                    self._timeouts.orphan_grace,
                )
                return
            time.sleep(POLL_SECONDS / 10)

    # -- observing (section 9.1) ------------------------------------------

    def _observe(
        self,
        files: ClusterFiles,
        manifest: RuntimeManifest,
        *,
        leftover_initdb: bool = True,
    ) -> ClusterState:
        if leftover_initdb and files.data_initdb.exists():
            return ClusterState(Observation.INTERRUPTED_INITDB)
        if not files.data.exists():
            return ClusterState(Observation.ABSENT)
        version = self._read_text(files.pg_version).strip()
        if version != str(manifest.major):
            return ClusterState(
                Observation.OTHER_MAJOR,
                detail=f"PG_VERSION says {version or 'nothing'!r}; this runtime is "
                f"PostgreSQL {manifest.major}",
            )
        pid_file = read_postmaster_pid(files.postmaster_pid)
        if pid_file is None:
            if files.postmaster_pid.exists():
                return ClusterState(
                    Observation.STALE, detail="postmaster.pid has no PID"
                )
            return ClusterState(Observation.STOPPED)
        if not self._processes.is_alive(pid_file.pid):
            return ClusterState(Observation.STALE, pid_file)
        executable = self._processes.executable(pid_file.pid)
        if executable is None or executable.name.lower() != "postgres.exe":
            # A PID reused by another program, or one this user cannot
            # inspect. Stale as far as the adapter can tell; on start, the
            # server itself decides whether the lock holds (section 9.2).
            return ClusterState(
                Observation.STALE,
                pid_file,
                detail=f"PID {pid_file.pid} is {executable or 'not inspectable'}",
            )
        if not self._runtime.contains(executable):
            return ClusterState(Observation.FOREIGN, pid_file, detail=str(executable))
        if not pid_file.points_to(files.data):
            return ClusterState(
                Observation.STALE,
                pid_file,
                detail=f"postmaster.pid names {pid_file.data_directory}",
            )
        if pid_file.status == STATUS_STOPPING:
            return ClusterState(Observation.STOPPING, pid_file)
        if pid_file.status in (STATUS_READY, STATUS_STANDBY):
            return ClusterState(Observation.READY, pid_file)
        return ClusterState(Observation.STARTING, pid_file)

    def _refuse_foreign(self, files: ClusterFiles, state: ClusterState) -> None:
        if state.observation is Observation.OTHER_MAJOR:
            raise ClusterRefusedError(
                f"{files.data} holds a cluster of another major version "
                f"({state.detail}). It is left untouched: never an initdb over it, "
                "never a deletion."
            )
        if state.observation is Observation.FOREIGN:
            raise ClusterRefusedError(
                f"The cluster in {files.data} is running from another runtime: "
                f"{state.detail}. Stop it there, or point this one elsewhere with "
                "EMBEDDED_POSTGRES_DIRECTORY."
            )

    def _shut_down_cleanly(self, files: ClusterFiles) -> bool:
        """Whether `pg_control` says the last shutdown was clean.

        Read before the start, so `recovered` is known rather than inferred
        from a log: the state is the fifth 32-bit field of `pg_control`,
        whose layout version 1300 has not changed since PostgreSQL 13.
        """
        try:
            with files.pg_control.open("rb") as handle:
                header = handle.read(20)
        except OSError:
            return True
        if len(header) < 20:
            return True
        version = int.from_bytes(header[8:12], "little")
        state = int.from_bytes(header[16:20], "little")
        if version != _PG_CONTROL_VERSION:
            return True
        return state in (_DB_SHUTDOWNED, _DB_SHUTDOWNED_IN_RECOVERY)

    # -- creating (section 7.1) -------------------------------------------

    def _create(self, files: ClusterFiles, report: Callable[[Phase], None]) -> None:
        report(Phase.INITDB)
        logger.info("Creating the embedded cluster in %s", files.data)
        self._home.secure()
        self._home.discard_password(files)
        self._home.write_new_password(files)
        returncode, output = self._commands.initdb(
            files.data_initdb, files.password, self._timeouts.initdb
        )
        if returncode != 0:
            shutil.rmtree(files.data_initdb, ignore_errors=True)
            raise StartupFailedError(f"initdb exited {returncode}:\n{_tail(output)}")
        conf = files.data_initdb / files.postgresql_conf.name
        with conf.open("a", encoding="utf-8") as handle:
            handle.write(f"\n# SolidVision (RFC-033 section 8.1)\n{INCLUDE_LINE}\n")
        self._rename(files.data_initdb, files.data)

    def _rename(self, source: Path, target: Path) -> None:
        """Atomic on one volume; retried while something holds a handle.

        A scanner opening the files `initdb` just wrote can make Windows
        refuse a directory rename for a moment.
        """
        deadline = time.monotonic() + self._timeouts.rename
        while True:
            try:
                source.rename(target)
                return
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(POLL_SECONDS)

    def _discard_interrupted_initdb(self, files: ClusterFiles) -> None:
        """Delete a `data.initdb` left by a creation that did not finish.

        An `initdb` of this runtime still running -- orphaned by a parent
        killed on its own -- is waited for first: it would otherwise keep
        writing into the directory being deleted.
        """
        deadline = time.monotonic() + self._timeouts.initdb
        while self._running_initdb() and time.monotonic() < deadline:
            time.sleep(POLL_SECONDS)
        if self._running_initdb():
            raise EmbeddedPostgresError(
                f"An initdb from {self._runtime.root} is still running; "
                f"{files.data_initdb} is left in place until it ends."
            )
        deadline = time.monotonic() + self._timeouts.rename
        while files.data_initdb.exists():
            shutil.rmtree(files.data_initdb, ignore_errors=True)
            if files.data_initdb.exists():
                if time.monotonic() >= deadline:
                    raise EmbeddedPostgresError(
                        f"{files.data_initdb} could not be removed."
                    )
                time.sleep(POLL_SECONDS)

    def _running_initdb(self) -> list[ProcessInfo]:
        return [
            process
            for process in self._processes.snapshot()
            if process.name.lower() == "initdb.exe" and self._ours(process.pid)
        ]

    # -- starting (sections 8.2 and 8.3) ------------------------------------

    def _start_server(
        self,
        files: ClusterFiles,
        state: ClusterState,
        report: Callable[[Phase], None],
        recovery_expected: bool,
    ) -> int:
        stale_pid = (
            state.pid_file.pid
            if state.observation is Observation.STALE and state.pid_file
            else None
        )
        report(Phase.STARTING)
        if recovery_expected:
            report(Phase.RECOVERING)
        port = self._configured_port(files)
        bind_failures = 0
        orphans_cleared = False
        while True:
            if port is None or not self._ports.usable(port):
                port = self._ports.free_port()
            self._write_server_conf(files, port)
            self._clear_startup_log(files, stale_pid)
            marks = self._server_log_marks(files)
            returncode = self._commands.start(files, self._timeouts.pg_ctl_start)
            # `pg_ctl`'s exit code is a hint, not the answer. On Windows it
            # cannot match `postmaster.pid` to the process it started -- what
            # it started is `cmd.exe` -- so it accepts any file whose start
            # time is at most two seconds old, including the *stale* file of
            # a postmaster that has just been killed, which still says
            # `ready`. The answer is a live postmaster of this runtime in
            # the file, and what that file says.
            pid_file = self._live_postmaster(files, marks, wait=returncode == 0)
            if pid_file is not None:
                if pid_file.status not in (STATUS_READY, STATUS_STANDBY):
                    # Alive and still `starting` after `-t` ran out, or after
                    # a stale `ready`: crash recovery, not a hang (8.3).
                    if not recovery_expected:
                        report(Phase.RECOVERING)
                    pid_file = self._wait_until_ready(files, pid_file, marks)
                break
            log = self._start_output(files, marks)
            if any(marker in log for marker in BIND_FAILURE_MARKERS):
                bind_failures += 1
                logger.warning(
                    "Port %s refused by bind (attempt %s)", port, bind_failures
                )
                if bind_failures >= self._timeouts.max_port_attempts:
                    raise StartupFailedError(
                        f"The server could not bind a port {bind_failures} times in "
                        f"a row. What it logged:\n{_tail(log)}"
                    )
                port = None
                continue
            if (
                SHARED_MEMORY_IN_USE in log
                and stale_pid is not None
                and not orphans_cleared
            ):
                orphans_cleared = True
                self._clear_orphans(stale_pid)
                continue
            raise StartupFailedError(
                f"pg_ctl start exited {returncode}. What the server logged "
                f"({files.startup_log}, then {files.server_log_directory}):"
                f"\n{_tail(log)}"
            )
        if pid_file.port is None:
            raise StartupFailedError(
                f"The server started, but {files.postmaster_pid} has no port."
            )
        return pid_file.port

    def _live_postmaster(
        self, files: ClusterFiles, marks: dict[Path, int], *, wait: bool
    ) -> PostmasterPid | None:
        """The `postmaster.pid` of a live server of this runtime, if there is one.

        With `wait`, for as long as a normal start may take: `pg_ctl` said the
        server is up, so a file that does not show it yet is one the new
        postmaster has not rewritten. A `FATAL` in `startup.log`, or a
        refused `bind` in the server's own log, ends the wait early -- that
        server is not coming.
        """
        deadline = time.monotonic() + (self._timeouts.pg_ctl_start if wait else 0)
        while True:
            pid_file = read_postmaster_pid(files.postmaster_pid)
            if pid_file is not None and self._is_our_postmaster(files, pid_file):
                return pid_file
            if time.monotonic() >= deadline:
                return None
            if "FATAL" in self._read_text(files.startup_log):
                return None
            written = self._server_log_since(files, marks)
            if any(marker in written for marker in BIND_FAILURE_MARKERS):
                return None
            time.sleep(POLL_SECONDS)

    # -- what the server said (section 8.1) ---------------------------------

    def _server_log_marks(self, files: ClusterFiles) -> dict[Path, int]:
        """The size of each of the collector's files, before a start.

        The files are one per weekday and every start of that day appends to
        the same one, so "what this start logged" is what lies past these
        sizes.
        """
        marks: dict[Path, int] = {}
        if files.server_log_directory.is_dir():
            for path in files.server_log_directory.iterdir():
                try:
                    marks[path] = path.stat().st_size
                except OSError:
                    continue
        return marks

    def _server_log_since(self, files: ClusterFiles, marks: dict[Path, int]) -> str:
        if not files.server_log_directory.is_dir():
            return ""
        written = []
        for path in sorted(files.server_log_directory.iterdir()):
            try:
                with path.open("rb") as handle:
                    mark = marks.get(path, 0)
                    # A file smaller than its mark was truncated by a
                    # rotation: all of it is new.
                    handle.seek(mark if path.stat().st_size >= mark else 0)
                    written.append(_decode(handle.read()))
            except OSError:
                continue
        return "".join(written)

    def _start_output(self, files: ClusterFiles, marks: dict[Path, int]) -> str:
        """Everything the server logged in this start, in the order it did.

        **Two logs, and not the split RFC-033 section 8.1 first assumed.**
        PostgreSQL 16 starts the logging collector *before* it opens its
        sockets, so `startup.log` -- `pg_ctl -l` -- holds only what comes
        earlier still: the lock file, the configuration, the shared memory.
        A refused `bind` is in the collector's file, with everything after.
        """
        return self._read_text(files.startup_log) + self._server_log_since(files, marks)

    def _clear_startup_log(self, files: ClusterFiles, stale_pid: int | None) -> None:
        """Rewrite `startup.log`, waiting out whoever still holds it.

        After a postmaster dies by force, its children are the first thing
        in the way: they hold `startup.log` until they exit, before any new
        server gets far enough to find the shared memory in use. So the
        orphan rule of section 9.3 is applied here as well -- wait the grace,
        end only the orphans that are this cluster's, and wait again. With
        no dead postmaster to name, nobody is ended at all.
        """
        if self._startup_log_cleared(files):
            return
        if stale_pid is not None:
            self._clear_orphans(stale_pid)
        deadline = time.monotonic() + self._timeouts.orphan_grace
        while not self._startup_log_cleared(files):
            if time.monotonic() >= deadline:
                raise StartupFailedError(
                    f"{files.startup_log} is held open by another process -- a "
                    "server process of this cluster that has not exited. The "
                    "adapter ends only the children of this cluster's own dead "
                    "postmaster."
                )
            time.sleep(POLL_SECONDS)

    def _startup_log_cleared(self, files: ClusterFiles) -> bool:
        try:
            self._commands.clear_startup_log(files)
        except PermissionError:
            return False
        return True

    def _configured_port(self, files: ClusterFiles) -> int | None:
        """The port of the last start, as `solidvision.conf` asked for it."""
        for line in self._read_text(files.server_conf).splitlines():
            name, _, value = line.partition("=")
            if name.strip() == "port":
                digits = value.split("#", 1)[0].strip()
                if digits.isdigit():
                    return int(digits)
        return None

    def _write_server_conf(self, files: ClusterFiles, port: int) -> None:
        """Section 8.1. Nothing goes through `pg_ctl -o`: someone who runs
        `pg_ctl` by hand to diagnose a problem starts the same server, on the
        same port."""
        files.server_conf.write_text(
            "# Written by the SolidVision embedded adapter before every start\n"
            "# (RFC-033 section 8.1). Edits here are overwritten.\n"
            f"listen_addresses = '{LOOPBACK}'\n"
            f"port = {port}\n"
            "log_line_prefix = '%m [%p] '\n"
            "logging_collector = on\n"
            f"log_directory = '{files.server_log_directory.name}'\n"
            "log_filename = 'postgresql-%a.log'\n"
            "log_rotation_age = 1d\n"
            "log_rotation_size = 0\n"
            "log_truncate_on_rotation = on\n",
            encoding="utf-8",
        )

    def _wait_until_ready(
        self,
        files: ClusterFiles,
        pid_file: PostmasterPid,
        marks: dict[Path, int] | None = None,
    ) -> PostmasterPid:
        if marks is None:
            marks = self._server_log_marks(files)
        deadline = time.monotonic() + self._timeouts.recovery_ceiling
        while True:
            current = read_postmaster_pid(files.postmaster_pid)
            if current is not None and current.pid == pid_file.pid:
                if current.status in (STATUS_READY, STATUS_STANDBY):
                    return current
            if not self._processes.is_alive(pid_file.pid):
                raise StartupFailedError(
                    f"The server (PID {pid_file.pid}) exited while starting. What "
                    f"it logged:\n{_tail(self._start_output(files, marks))}"
                )
            if time.monotonic() >= deadline:
                raise StartupFailedError(
                    f"The server (PID {pid_file.pid}) is still starting after "
                    f"{self._timeouts.recovery_ceiling:.0f} s. It was left running."
                )
            time.sleep(POLL_SECONDS)

    def _wait_until_gone(self, files: ClusterFiles, state: ClusterState) -> None:
        assert state.pid_file is not None
        deadline = time.monotonic() + self._timeouts.stopping_wait
        while self._processes.is_alive(state.pid_file.pid):
            if time.monotonic() >= deadline:
                raise StartupFailedError(
                    f"The server (PID {state.pid_file.pid}) has been stopping for "
                    f"more than {self._timeouts.stopping_wait:.0f} s."
                )
            time.sleep(POLL_SECONDS)

    def _adopt(
        self,
        files: ClusterFiles,
        state: ClusterState,
        password: str,
        report: Callable[[Phase], None],
    ) -> StartedCluster:
        """Section 8.4: this directory, this runtime, and the password works."""
        assert state.pid_file is not None
        pid_file = state.pid_file
        if state.observation is Observation.STARTING:
            report(Phase.RECOVERING)
            pid_file = self._wait_until_ready(files, pid_file)
        if pid_file.port is None:
            raise StartupFailedError(f"{files.postmaster_pid} has no port.")
        connection = ClusterConnection(port=pid_file.port, password=password)
        self._login(connection, report)
        logger.info("Adopted the embedded cluster on %s:%s", LOOPBACK, pid_file.port)
        return StartedCluster(
            connection=connection, started_here=False, created=False, recovered=False
        )

    def _login(
        self, connection: ClusterConnection, report: Callable[[Phase], None]
    ) -> None:
        """The third condition of section 8.4, and the last word on "ready".

        A server reinitializing after one of its own processes died still has
        `ready` in `postmaster.pid`; what tells the truth is the connection,
        refused with "in recovery mode". That refusal is waited out, like any
        other recovery, up to the same ceiling. A rejected password is not.
        """
        deadline = time.monotonic() + self._timeouts.recovery_ceiling
        reported = False
        while True:
            try:
                self._login_check(connection)
                return
            except ServerNotReadyError:
                if time.monotonic() >= deadline:
                    raise
                if not reported:
                    report(Phase.RECOVERING)
                    reported = True
                time.sleep(POLL_SECONDS)

    # -- orphans (section 9.3) ----------------------------------------------

    def _ours(self, pid: int) -> bool:
        executable = self._processes.executable(pid)
        return executable is not None and self._runtime.contains(executable)

    def _is_our_postmaster(self, files: ClusterFiles, pid_file: PostmasterPid) -> bool:
        return (
            self._processes.is_alive(pid_file.pid)
            and self._ours(pid_file.pid)
            and pid_file.points_to(files.data)
        )

    def _orphans_of(self, dead_pid: int) -> list[ProcessInfo]:
        """Children of *this* data directory's dead postmaster, from *this* runtime.

        Both conditions, always together: they are what protects a
        PostgreSQL the user installed and another Windows account's cluster
        on the same machine. And nothing at all if the "dead" PID is alive
        again -- reused, so its children are not the ones being looked for.
        """
        if self._processes.is_alive(dead_pid):
            return []
        return [
            process
            for process in self._processes.snapshot()
            if process.parent_pid == dead_pid
            and process.pid != dead_pid
            and self._ours(process.pid)
        ]

    def _clear_orphans(self, dead_pid: int) -> None:
        deadline = time.monotonic() + self._timeouts.orphan_grace
        orphans = self._orphans_of(dead_pid)
        while orphans and time.monotonic() < deadline:
            time.sleep(POLL_SECONDS)
            orphans = self._orphans_of(dead_pid)
        for orphan in orphans:
            logger.warning(
                "Ending PID %s, a child of the dead postmaster %s that outlived "
                "the %.0f s grace",
                orphan.pid,
                dead_pid,
                self._timeouts.orphan_grace,
            )
            self._processes.terminate(orphan.pid)
        deadline = time.monotonic() + self._timeouts.orphan_grace
        while self._orphans_of(dead_pid) and time.monotonic() < deadline:
            time.sleep(POLL_SECONDS)

    @staticmethod
    def _read_text(path: Path) -> str:
        try:
            return _decode(path.read_bytes())
        except FileNotFoundError:
            return ""
