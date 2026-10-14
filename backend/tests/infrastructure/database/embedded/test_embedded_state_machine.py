"""The state machine of RFC-033 section 9.1, one row of its table at a time.

**No `initdb` and no server runs here.** The adapter gets doubles for the
three things that touch the machine: the commands (`initdb`, `pg_ctl`), the
process table and the port probe. The cluster's files are real files in a
temporary directory, because the files *are* the adapter's state --
`postmaster.pid` is read with the real reader, and `pg_control` is the real
twenty bytes.

What each double's "server" does is what the real one does and no more: a
start that works writes `postmaster.pid` and appears in the process table; a
stop that works removes both. The adapter is never given the chance to learn
the port, the PID or the state from anywhere else.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from app.infrastructure.config.settings import DatabaseSource, Settings
from app.infrastructure.database.embedded import cluster as cluster_module
from app.infrastructure.database.embedded.cluster import (
    EMBEDDED_SOURCE,
    ENVIRONMENT_VARIABLES,
    INCLUDE_LINE,
    SHARED_MEMORY_IN_USE,
    ClusterStatus,
    ClusterTimeouts,
    EmbeddedPostgres,
    Phase,
    PortProber,
    PostgresCommands,
    StopOutcome,
)
from app.infrastructure.database.embedded.cluster_home import (
    ClusterFiles,
    ClusterHome,
    DirectoryAcl,
)
from app.infrastructure.database.embedded.database_setup import ClusterConnection
from app.infrastructure.database.embedded.errors import (
    ClusterNotRunningError,
    ClusterRefusedError,
    ServerNotReadyError,
    StartupFailedError,
    StopFailedError,
)
from app.infrastructure.database.embedded.runtime import (
    MANIFEST_NAME,
    REQUIRED_FILES,
    PostgresRuntime,
)
from app.infrastructure.database.embedded.windows_processes import (
    ProcessInfo,
    ProcessTable,
)

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="The embedded cluster is Windows-only (RFC-033 section 4.1)",
)

LAST_PORT = 51336
DEAD_POSTMASTER = 4000
IN_PRODUCTION = 6
SHUT_DOWN = 1

BIND_FAILURE = (
    '2026-10-11 12:00:00.000 -03 [1] LOG:  could not bind IPv4 address "127.0.0.1": '
    "Only one usage of each socket address is normally permitted.\n"
    "2026-10-11 12:00:00.001 -03 [1] FATAL:  could not create any TCP/IP sockets\n"
)
SHARED_MEMORY_FAILURE = (
    f"2026-10-11 12:00:00.000 -03 [1] FATAL:  {SHARED_MEMORY_IN_USE}\n"
    "2026-10-11 12:00:00.000 -03 [1] HINT:  Check if there are any old server "
    "processes still running, and terminate them.\n"
)
REDIRECTED = (
    "2026-10-11 12:00:00.000 -03 [1] LOG:  redirecting log output to logging "
    "collector process\n"
    "2026-10-11 12:00:00.000 -03 [1] HINT:  Future log output will appear in "
    'directory "log".\n'
)
OTHER_FAILURE = (
    "2026-10-11 12:00:00.000 -03 [1] FATAL:  could not access file "
    '"$libdir/vector": No such file or directory\n'
)


class RecordingAcl(DirectoryAcl):
    def __init__(self) -> None:
        self.restricted: list[Path] = []

    def restrict_to_current_user(self, directory: Path) -> None:
        self.restricted.append(directory)


class FakeProcesses(ProcessTable):
    """A process table the test writes by hand."""

    def __init__(self) -> None:
        self.table: dict[int, tuple[int, Path]] = {}
        self.terminated: list[int] = []
        self.on_poll: Callable[[], None] | None = None
        self.on_snapshot: Callable[[], None] | None = None

    def add(self, pid: int, executable: Path, parent: int = 4) -> None:
        self.table[pid] = (parent, executable)

    def snapshot(self) -> list[ProcessInfo]:
        if self.on_snapshot is not None:
            self.on_snapshot()
        return [
            ProcessInfo(pid=pid, parent_pid=parent, name=executable.name)
            for pid, (parent, executable) in self.table.items()
        ]

    def executable(self, pid: int) -> Path | None:
        entry = self.table.get(pid)
        return entry[1] if entry else None

    def is_alive(self, pid: int) -> bool:
        if self.on_poll is not None:
            self.on_poll()
        return pid in self.table

    def terminate(self, pid: int) -> bool:
        self.terminated.append(pid)
        return self.table.pop(pid, None) is not None


class FakePorts(PortProber):
    def __init__(self) -> None:
        self.unusable: set[int] = set()
        self.free = iter(range(52001, 52100))
        self.probed: list[int] = []

    def usable(self, port: int) -> bool:
        self.probed.append(port)
        return port not in self.unusable

    def free_port(self) -> int:
        return next(self.free)


def write_pid_file(
    files: ClusterFiles, pid: int, port: int, status: str = "ready   "
) -> None:
    """`postmaster.pid` as PostgreSQL 16 writes it on Windows."""
    data = str(files.data).replace("\\", "/")
    files.postmaster_pid.write_text(
        f"{pid}\n{data}\n1791732090\n{port}\n\n127.0.0.1\n\n{status}\n",
        encoding="ascii",
    )


def write_pg_control(data: Path, state: int) -> None:
    """The first twenty bytes: identifier, version 1300, catalog, state."""
    (data / "global").mkdir(parents=True, exist_ok=True)
    header = (
        (7_000_000_000_000_000_000).to_bytes(8, "little")
        + (1300).to_bytes(4, "little")
        + (202307071).to_bytes(4, "little")
        + state.to_bytes(4, "little")
    )
    (data / "global" / "pg_control").write_bytes(header + bytes(8172))


class FakeCommands(PostgresCommands):
    """`initdb` and `pg_ctl`, reduced to what they leave on disk."""

    def __init__(self, runtime: PostgresRuntime, processes: FakeProcesses) -> None:
        super().__init__(runtime)
        self._processes = processes
        self.next_pid = 5000
        self.initdb_result = 0
        self.initdb_calls: list[tuple[Path, str]] = []
        self.start_script: list[Callable[[ClusterFiles], int]] = []
        self.ports_asked: list[int] = []
        self.pid_file_seen_at_start: list[str | None] = []
        self.stop_results = {"fast": 0, "immediate": 0}
        self.stop_calls: list[str] = []
        self.server_port: int | None = None
        self.startup_log_held_by: set[int] = set()
        self.lingering = 0
        self.reuse_pid_after_stop: int | None = None
        self.polls_after_stop: Callable[[], int] = lambda: 0

    def initdb(
        self, target: Path, password_file: Path, timeout: float
    ) -> tuple[int, str]:
        # Reading the file here is the assertion that it exists *before*
        # initdb runs (section 7.1, step 1).
        self.initdb_calls.append((target, password_file.read_text(encoding="ascii")))
        target.mkdir(parents=True)
        if self.initdb_result != 0:
            (target / "half-written").write_text("x", encoding="ascii")
            return self.initdb_result, "initdb: error: simulated failure"
        (target / "PG_VERSION").write_text("16\n", encoding="ascii")
        (target / "postgresql.conf").write_text("# initdb's own\n", encoding="utf-8")
        write_pg_control(target, SHUT_DOWN)
        return 0, "Success."

    def clear_startup_log(self, files: ClusterFiles) -> None:
        # Every server process holds the file open; so does a dead
        # postmaster's child, for as long as it lives.
        if self.startup_log_held_by & self._processes.table.keys():
            raise PermissionError(13, "Permission denied", str(files.startup_log))
        files.startup_log.write_bytes(b"")

    def start(self, files: ClusterFiles, timeout: int) -> int:
        self.ports_asked.append(configured_port(files))
        self.pid_file_seen_at_start.append(
            files.postmaster_pid.read_text(encoding="ascii")
            if files.postmaster_pid.exists()
            else None
        )
        step = self.start_script.pop(0) if self.start_script else self.succeed
        return step(files)

    def succeed(self, files: ClusterFiles) -> int:
        pid = self.next_pid
        self.next_pid += 1
        self._processes.add(pid, self._runtime.postgres)
        port = self.server_port or configured_port(files)
        write_pid_file(files, pid, port)
        write_pg_control(files.data, IN_PRODUCTION)
        return 0

    def fail_with(self, log: str) -> Callable[[ClusterFiles], int]:
        """A failure before the logging collector exists: into `startup.log`."""

        def step(files: ClusterFiles) -> int:
            files.startup_log.write_text(log, encoding="ascii")
            return 1

        return step

    def fail_in_server_log(
        self, log: str, returncode: int = 1
    ) -> Callable[[ClusterFiles], int]:
        """A failure after it: `startup.log` gets the two redirection lines,
        and the cause is appended to the collector's file of the day."""

        def step(files: ClusterFiles) -> int:
            files.startup_log.write_text(REDIRECTED, encoding="ascii")
            append_server_log(files, log)
            return returncode

        return step

    def stop(self, files: ClusterFiles, mode: str, timeout: int) -> int:
        self.stop_calls.append(mode)
        result = self.stop_results[mode]
        if result == 0:
            # The *server* removes its own lock on the way out. Its helper
            # processes outlive that by `lingering` polls of the table.
            pid = int(files.postmaster_pid.read_text(encoding="ascii").split()[0])
            self._processes.table.pop(pid, None)
            children = [
                child
                for child, (parent, executable) in self._processes.table.items()
                if parent == pid and self._runtime.contains(executable)
            ]
            polls = 0

            def leave() -> None:
                nonlocal polls
                polls += 1
                if polls > self.lingering:
                    for child in children:
                        # Only while it is still the server's helper: a PID
                        # given to another program is that program's.
                        if self._processes.table.get(child, (0,))[0] == pid:
                            self._processes.table.pop(child, None)

            self.polls_after_stop = lambda: polls
            self._processes.on_snapshot = leave
            files.postmaster_pid.unlink()
            write_pg_control(files.data, SHUT_DOWN)
            if self.reuse_pid_after_stop is not None:
                self._processes.add(
                    self.reuse_pid_after_stop,
                    Path(r"C:\Windows\System32\notepad.exe"),
                    parent=77,
                )
        return result


def append_server_log(files: ClusterFiles, text: str) -> None:
    files.server_log_directory.mkdir(exist_ok=True)
    with (files.server_log_directory / "postgresql-Sun.log").open("a") as handle:
        handle.write(text)


def configured_port(files: ClusterFiles) -> int:
    for line in files.server_conf.read_text(encoding="utf-8").splitlines():
        if line.startswith("port = "):
            return int(line.split("=")[1])
    raise AssertionError("solidvision.conf has no port")


@dataclass
class Rig:
    cluster: EmbeddedPostgres
    files: ClusterFiles
    home: ClusterHome
    runtime: PostgresRuntime
    other_runtime: PostgresRuntime
    processes: FakeProcesses
    commands: FakeCommands
    ports: FakePorts
    acl: RecordingAcl
    logins: list[ClusterConnection] = field(default_factory=list)
    login_refusals: list[Exception] = field(default_factory=list)
    phases: list[Phase] = field(default_factory=list)

    def start(self) -> cluster_module.StartedCluster:
        return self.cluster.start(on_phase=self.phases.append)

    def existing_cluster(
        self, *, port: int = LAST_PORT, state: int = SHUT_DOWN, version: str = "16"
    ) -> str:
        """A cluster that was created and stopped before the test began."""
        self.files.data.mkdir(parents=True)
        self.files.pg_version.write_text(version + "\n", encoding="ascii")
        self.files.postgresql_conf.write_text(
            f"# initdb's own\n{INCLUDE_LINE}\n", encoding="utf-8"
        )
        self.files.server_conf.write_text(f"port = {port}\n", encoding="utf-8")
        write_pg_control(self.files.data, state)
        return self.home.write_new_password(self.files)

    def running_server(self, pid: int = 3000, status: str = "ready   ") -> None:
        self.processes.add(pid, self.runtime.postgres)
        write_pid_file(self.files, pid, LAST_PORT, status)
        write_pg_control(self.files.data, IN_PRODUCTION)

    def stale_pid_file(self) -> None:
        """A postmaster that was killed: its lock stays, its PID is gone."""
        write_pid_file(self.files, DEAD_POSTMASTER, LAST_PORT)
        write_pg_control(self.files.data, IN_PRODUCTION)


def make_runtime(root: Path) -> PostgresRuntime:
    for relative in REQUIRED_FILES:
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_bytes(b"")
    (root / MANIFEST_NAME).write_text(
        '{"format": 1, "postgresql": "16.15", "pgvector": "0.8.6", '
        '"lockfile_sha256": "0"}',
        encoding="utf-8",
    )
    return PostgresRuntime(root)


@pytest.fixture()
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Rig:
    monkeypatch.setattr(cluster_module, "POLL_SECONDS", 0.01)
    runtime = make_runtime(tmp_path / "runtime" / "postgres")
    other_runtime = make_runtime(tmp_path / "installed" / "postgres")
    acl = RecordingAcl()
    home = ClusterHome(tmp_path / "SolidVision" / "postgres", acl=acl)
    files = home.files(16)
    home.ensure_directory(files)
    processes = FakeProcesses()
    commands = FakeCommands(runtime, processes)
    ports = FakePorts()
    logins: list[ClusterConnection] = []
    login_refusals: list[Exception] = []

    def login(connection: ClusterConnection) -> None:
        logins.append(connection)
        if login_refusals:
            raise login_refusals.pop(0)

    cluster = EmbeddedPostgres(
        runtime,
        home,
        processes=processes,
        commands=commands,
        ports=ports,
        login_check=login,
        timeouts=ClusterTimeouts(
            recovery_ceiling=0.5,
            lock=5.0,
            orphan_grace=0.2,
            stopping_wait=1.0,
            initdb=1.0,
            rename=1.0,
        ),
    )
    return Rig(
        cluster=cluster,
        files=files,
        home=home,
        runtime=runtime,
        other_runtime=other_runtime,
        processes=processes,
        commands=commands,
        ports=ports,
        acl=acl,
        logins=logins,
        login_refusals=login_refusals,
    )


# --------------------------------------------------------------------------
# `data` does not exist: first run, atomic initdb (section 7.1)
# --------------------------------------------------------------------------


def test_the_first_run_creates_the_cluster_and_starts_it(rig: Rig) -> None:
    started = rig.start()

    assert started.created is True
    assert started.started_here is True
    assert started.recovered is False
    assert rig.files.data.is_dir()
    assert rig.phases == [Phase.INITDB, Phase.STARTING]


def test_initdb_writes_into_data_initdb_and_only_a_rename_makes_it_data(
    rig: Rig,
) -> None:
    """A `data\\` half way through must not be mistaken for a cluster."""
    rig.start()

    ((target, _),) = rig.commands.initdb_calls
    assert target == rig.files.data_initdb
    assert not rig.files.data_initdb.exists()
    assert rig.files.pg_version.read_text(encoding="ascii").strip() == "16"


def test_the_password_is_on_disk_before_initdb_and_is_the_one_it_gets(
    rig: Rig,
) -> None:
    started = rig.start()

    ((_, password_initdb_read),) = rig.commands.initdb_calls
    assert password_initdb_read.strip() == started.connection.password
    assert rig.home.read_password(rig.files) == started.connection.password


def test_the_directory_is_secured_before_the_password_is_written(rig: Rig) -> None:
    rig.start()

    assert rig.acl.restricted == [rig.home.root]


def test_postgresql_conf_gains_the_include_and_nothing_else(rig: Rig) -> None:
    rig.start()

    conf = rig.files.postgresql_conf.read_text(encoding="utf-8")
    assert conf.startswith("# initdb's own\n")
    assert conf.count(INCLUDE_LINE) == 1


def test_a_password_without_a_data_is_discarded_and_generated_again(rig: Rig) -> None:
    """The password and the cluster are a pair (section 7.1)."""
    rig.files.password.write_text("left-by-a-killed-first-run\n", encoding="ascii")

    started = rig.start()

    assert started.connection.password != "left-by-a-killed-first-run"
    assert rig.home.read_password(rig.files) == started.connection.password


def test_an_initdb_that_fails_leaves_no_data_and_no_data_initdb(rig: Rig) -> None:
    rig.commands.initdb_result = 1

    with pytest.raises(StartupFailedError, match="simulated failure"):
        rig.start()

    assert not rig.files.data.exists()
    assert not rig.files.data_initdb.exists()
    assert rig.commands.ports_asked == []


# --------------------------------------------------------------------------
# `data.initdb` exists: an initdb was interrupted
# --------------------------------------------------------------------------


def test_a_leftover_data_initdb_is_deleted_and_the_cluster_created_anew(
    rig: Rig,
) -> None:
    rig.files.data_initdb.mkdir()
    (rig.files.data_initdb / "half-written").write_text("x", encoding="ascii")

    started = rig.start()

    assert started.created is True
    assert not rig.files.data_initdb.exists()
    assert not (rig.files.data / "half-written").exists()
    assert len(rig.commands.initdb_calls) == 1


def test_a_leftover_data_initdb_beside_a_whole_cluster_costs_no_new_initdb(
    rig: Rig,
) -> None:
    rig.existing_cluster()
    rig.files.data_initdb.mkdir()

    started = rig.start()

    assert started.created is False
    assert not rig.files.data_initdb.exists()
    assert rig.commands.initdb_calls == []


def test_an_initdb_of_this_runtime_still_running_is_waited_for(rig: Rig) -> None:
    """Orphaned by a parent killed alone, it is still writing in there."""
    rig.files.data_initdb.mkdir()
    rig.processes.add(6000, rig.runtime.initdb)
    polls = 0
    original_snapshot = rig.processes.snapshot

    def snapshot() -> list[ProcessInfo]:
        nonlocal polls
        polls += 1
        if polls >= 3:
            rig.processes.table.pop(6000, None)
        return original_snapshot()

    rig.processes.snapshot = snapshot  # type: ignore[method-assign]

    rig.start()

    assert polls >= 3
    assert rig.processes.terminated == []


# --------------------------------------------------------------------------
# PG_VERSION is another major: refuse
# --------------------------------------------------------------------------


def test_a_cluster_of_another_major_version_is_refused_and_left_alone(
    rig: Rig,
) -> None:
    rig.existing_cluster(version="15")
    before = sorted(path.name for path in rig.files.data.iterdir())

    with pytest.raises(ClusterRefusedError, match="another major version"):
        rig.start()

    assert rig.commands.initdb_calls == []
    assert rig.commands.ports_asked == []
    assert sorted(path.name for path in rig.files.data.iterdir()) == before
    assert rig.files.pg_version.read_text(encoding="ascii").strip() == "15"


def test_a_data_directory_without_pg_version_is_not_initialized_over(rig: Rig) -> None:
    rig.files.data.mkdir()
    (rig.files.data / "something").write_text("x", encoding="ascii")

    with pytest.raises(ClusterRefusedError):
        rig.start()

    assert rig.commands.initdb_calls == []


# --------------------------------------------------------------------------
# No postmaster.pid: start
# --------------------------------------------------------------------------


def test_a_stopped_cluster_is_started_not_created(rig: Rig) -> None:
    password = rig.existing_cluster()

    started = rig.start()

    assert started.started_here is True
    assert started.created is False
    assert started.recovered is False
    assert started.connection.password == password
    assert rig.commands.initdb_calls == []
    assert rig.phases == [Phase.STARTING]


def test_the_last_port_that_worked_is_tried_first(rig: Rig) -> None:
    rig.existing_cluster(port=LAST_PORT)

    started = rig.start()

    assert rig.ports.probed == [LAST_PORT]
    assert rig.commands.ports_asked == [LAST_PORT]
    assert started.connection.port == LAST_PORT


def test_a_last_port_that_will_not_do_is_replaced_before_the_start(rig: Rig) -> None:
    """Section 8.2, steps 2 and 3: the probe said no, in use or reserved."""
    rig.existing_cluster(port=LAST_PORT)
    rig.ports.unusable.add(LAST_PORT)

    started = rig.start()

    assert rig.commands.ports_asked == [52001]
    assert started.connection.port == 52001


def test_the_port_comes_from_postmaster_pid_not_from_the_conf(rig: Rig) -> None:
    """The conf says what the adapter asked for; the server says what it uses."""
    rig.existing_cluster(port=LAST_PORT)
    rig.commands.server_port = 60000

    started = rig.start()

    assert configured_port(rig.files) == LAST_PORT
    assert started.connection.port == 60000


def test_the_conf_listens_on_loopback_only_and_rotates_seven_logs(rig: Rig) -> None:
    rig.existing_cluster()

    rig.start()

    conf = rig.files.server_conf.read_text(encoding="utf-8")
    assert "listen_addresses = '127.0.0.1'\n" in conf
    assert "logging_collector = on\n" in conf
    assert "log_filename = 'postgresql-%a.log'\n" in conf
    assert "log_truncate_on_rotation = on\n" in conf


def test_the_login_is_checked_with_the_password_in_the_file(rig: Rig) -> None:
    password = rig.existing_cluster()

    rig.start()

    (login,) = rig.logins
    assert (login.host, login.port, login.password) == (
        "127.0.0.1",
        LAST_PORT,
        password,
    )


def test_a_cluster_without_its_password_file_is_refused(rig: Rig) -> None:
    rig.existing_cluster()
    rig.files.password.unlink()

    with pytest.raises(ClusterRefusedError, match="pair is lost"):
        rig.start()

    assert rig.commands.ports_asked == []
    assert rig.commands.initdb_calls == []


# --------------------------------------------------------------------------
# postmaster.pid, PID alive, this runtime
# --------------------------------------------------------------------------


def test_a_cluster_that_is_up_is_adopted_not_restarted(rig: Rig) -> None:
    password = rig.existing_cluster()
    rig.running_server(pid=3000)

    started = rig.start()

    assert started.started_here is False
    assert started.created is False
    assert started.connection.port == LAST_PORT
    assert started.connection.password == password
    assert rig.commands.ports_asked == []
    assert rig.commands.stop_calls == []
    assert rig.phases == []


def test_adoption_needs_the_password_to_be_accepted(rig: Rig) -> None:
    """Section 8.4, the third condition: the pair is lost, so refuse."""
    rig.existing_cluster()
    rig.running_server()
    rig.login_refusals.append(ClusterRefusedError("rejected the password"))

    with pytest.raises(ClusterRefusedError, match="rejected the password"):
        rig.start()

    assert rig.commands.ports_asked == []
    assert rig.commands.stop_calls == []
    assert len(rig.logins) == 1, "a rejected password is not tried again"


def test_a_server_reinitializing_behind_a_ready_lock_file_is_waited_for(
    rig: Rig,
) -> None:
    """Measured: after one backend is killed the server refuses connections
    for seconds with "in recovery mode", and `postmaster.pid` goes on saying
    `ready`. The lock file calls for an adoption; the connection decides."""
    rig.existing_cluster()
    rig.running_server()
    rig.login_refusals += [ServerNotReadyError("in recovery mode")] * 3

    started = rig.start()

    assert started.started_here is False
    assert len(rig.logins) == 4
    assert rig.phases == [Phase.RECOVERING]
    assert rig.commands.ports_asked == []


def test_a_server_that_never_takes_connections_is_an_error_at_the_ceiling(
    rig: Rig,
) -> None:
    rig.existing_cluster()
    rig.running_server(pid=3000)
    rig.login_refusals += [ServerNotReadyError("in recovery mode")] * 10_000

    with pytest.raises(ServerNotReadyError):
        rig.start()

    assert rig.processes.terminated == []
    assert 3000 in rig.processes.table


def test_a_cluster_that_is_starting_is_waited_for_then_adopted(rig: Rig) -> None:
    rig.existing_cluster()
    rig.running_server(pid=3000, status="starting")
    polls = 0

    def becomes_ready() -> None:
        nonlocal polls
        polls += 1
        if polls == 4:
            write_pid_file(rig.files, 3000, LAST_PORT, "ready   ")

    rig.processes.on_poll = becomes_ready

    started = rig.start()

    assert started.started_here is False
    assert rig.commands.ports_asked == []
    assert rig.phases == [Phase.RECOVERING]


def test_a_cluster_that_dies_while_starting_is_an_error_with_the_log(rig: Rig) -> None:
    rig.existing_cluster()
    rig.running_server(pid=3000, status="starting")
    rig.files.startup_log.write_text(OTHER_FAILURE, encoding="ascii")
    polls = 0

    def dies() -> None:
        nonlocal polls
        polls += 1
        if polls == 3:
            rig.processes.table.pop(3000, None)

    rig.processes.on_poll = dies

    with pytest.raises(StartupFailedError, match="vector"):
        rig.start()


def test_a_cluster_that_is_stopping_is_waited_out_then_started(rig: Rig) -> None:
    rig.existing_cluster()
    rig.running_server(pid=3000, status="stopping")
    polls = 0

    def finishes_stopping() -> None:
        nonlocal polls
        polls += 1
        if polls == 4:
            rig.processes.table.pop(3000, None)
            rig.files.postmaster_pid.unlink()
            write_pg_control(rig.files.data, SHUT_DOWN)

    rig.processes.on_poll = finishes_stopping

    started = rig.start()

    assert started.started_here is True
    assert rig.commands.ports_asked == [LAST_PORT]


# --------------------------------------------------------------------------
# postmaster.pid held by a postgres.exe of another runtime: refuse
# --------------------------------------------------------------------------


def test_a_cluster_held_by_another_runtime_is_refused_naming_that_runtime(
    rig: Rig,
) -> None:
    """Section 8.4: the installed app's cluster, met by a checkout's code."""
    rig.existing_cluster()
    rig.processes.add(3000, rig.other_runtime.postgres)
    write_pid_file(rig.files, 3000, LAST_PORT)

    with pytest.raises(ClusterRefusedError) as refusal:
        rig.start()

    assert str(rig.other_runtime.postgres) in str(refusal.value)
    assert rig.commands.ports_asked == []
    assert rig.logins == []


def test_stop_does_not_stop_another_runtimes_cluster(rig: Rig) -> None:
    rig.existing_cluster()
    rig.processes.add(3000, rig.other_runtime.postgres)
    write_pid_file(rig.files, 3000, LAST_PORT)

    with pytest.raises(ClusterRefusedError):
        rig.cluster.stop()

    assert rig.commands.stop_calls == []


# --------------------------------------------------------------------------
# postmaster.pid with a dead PID: dirty shutdown
# --------------------------------------------------------------------------


def test_a_dead_postmaster_means_pg_ctl_start_and_a_recovery(rig: Rig) -> None:
    rig.existing_cluster()
    rig.stale_pid_file()

    started = rig.start()

    assert started.started_here is True
    assert started.recovered is True
    assert rig.phases == [Phase.STARTING, Phase.RECOVERING]
    assert rig.commands.ports_asked == [LAST_PORT]


def test_the_adapter_never_deletes_postmaster_pid(rig: Rig) -> None:
    """Section 9.2. The server decides whether the lock holds, so the stale
    file is still there, untouched, when `pg_ctl start` runs."""
    rig.existing_cluster()
    rig.stale_pid_file()
    stale = rig.files.postmaster_pid.read_text(encoding="ascii")

    rig.start()

    assert rig.commands.pid_file_seen_at_start == [stale]


def test_a_pid_reused_by_another_program_is_a_stale_lock(rig: Rig) -> None:
    """The PID is alive, but it is not a postmaster: the server tells them
    apart by its signal pipe, and the adapter by the image name."""
    rig.existing_cluster()
    rig.stale_pid_file()
    rig.processes.add(DEAD_POSTMASTER, Path(r"C:\Windows\System32\notepad.exe"))

    started = rig.start()

    assert started.started_here is True
    assert rig.processes.terminated == []


def test_a_clean_shutdown_without_a_lock_is_not_called_a_recovery(rig: Rig) -> None:
    rig.existing_cluster(state=SHUT_DOWN)

    assert rig.start().recovered is False


def test_an_immediate_shutdown_leaves_no_lock_and_still_needs_recovery(
    rig: Rig,
) -> None:
    """`pg_ctl stop -m immediate` removes `postmaster.pid` but writes no
    checkpoint; `pg_control` is what says the next start recovers."""
    rig.existing_cluster(state=IN_PRODUCTION)

    started = rig.start()

    assert started.recovered is True
    assert Phase.RECOVERING in rig.phases


def test_a_start_that_outlasts_pg_ctl_is_recovery_not_a_hang(rig: Rig) -> None:
    """Section 8.3: `-t` ran out with the server alive and still `starting`."""
    rig.existing_cluster()

    def times_out(files: ClusterFiles) -> int:
        rig.processes.add(5000, rig.runtime.postgres)
        write_pid_file(files, 5000, LAST_PORT, "starting")
        return 1

    rig.commands.start_script = [times_out]
    polls = 0

    def becomes_ready() -> None:
        nonlocal polls
        polls += 1
        if polls == 5:
            write_pid_file(rig.files, 5000, LAST_PORT, "ready   ")

    rig.processes.on_poll = becomes_ready

    started = rig.start()

    assert started.started_here is True
    assert len(rig.commands.ports_asked) == 1
    assert rig.phases == [Phase.STARTING, Phase.RECOVERING]


def test_a_recovery_past_the_ceiling_is_an_error_and_the_server_is_left_running(
    rig: Rig,
) -> None:
    rig.existing_cluster()

    def times_out(files: ClusterFiles) -> int:
        rig.processes.add(5000, rig.runtime.postgres)
        write_pid_file(files, 5000, LAST_PORT, "starting")
        return 1

    rig.commands.start_script = [times_out]

    with pytest.raises(StartupFailedError, match="left running"):
        rig.start()

    assert rig.processes.terminated == []
    assert 5000 in rig.processes.table


# --------------------------------------------------------------------------
# The start fails: shared memory still in use (section 9.3)
# --------------------------------------------------------------------------


def orphan_scene(rig: Rig) -> None:
    """A killed postmaster, one child of it still alive, and two decoys."""
    rig.existing_cluster()
    rig.stale_pid_file()
    rig.processes.add(4001, rig.runtime.postgres, parent=DEAD_POSTMASTER)
    # Same parent PID, but an executable from another runtime's copy.
    rig.processes.add(7001, rig.other_runtime.postgres, parent=DEAD_POSTMASTER)
    # This runtime's executable, but the child of another postmaster: another
    # Windows account's cluster, or a second cluster of this one.
    rig.processes.add(7002, rig.runtime.postgres, parent=9999)
    rig.commands.start_script = [rig.commands.fail_with(SHARED_MEMORY_FAILURE)]


def test_only_the_orphans_of_this_cluster_are_ended_and_the_start_retried(
    rig: Rig,
) -> None:
    orphan_scene(rig)

    started = rig.start()

    assert rig.processes.terminated == [4001]
    assert 7001 in rig.processes.table, "another runtime's process must survive"
    assert 7002 in rig.processes.table, "another postmaster's child must survive"
    assert started.started_here is True
    assert len(rig.commands.ports_asked) == 2


def test_an_orphan_that_leaves_by_itself_within_the_grace_is_not_ended(
    rig: Rig,
) -> None:
    orphan_scene(rig)
    polls = 0

    def orphan_exits() -> None:
        nonlocal polls
        polls += 1
        if polls == 6:
            rig.processes.table.pop(4001, None)

    rig.processes.on_poll = orphan_exits

    rig.start()

    assert rig.processes.terminated == []


def test_the_retry_after_orphans_happens_once(rig: Rig) -> None:
    orphan_scene(rig)
    rig.commands.start_script.append(rig.commands.fail_with(SHARED_MEMORY_FAILURE))

    with pytest.raises(StartupFailedError, match="shared memory"):
        rig.start()

    assert len(rig.commands.ports_asked) == 2


def test_children_of_a_reused_pid_are_nobody_to_end(rig: Rig) -> None:
    """The "dead" PID is alive again, so its children belong to someone else."""
    orphan_scene(rig)
    rig.processes.add(DEAD_POSTMASTER, Path(r"C:\Windows\System32\notepad.exe"))

    rig.start()

    assert rig.processes.terminated == []
    assert 4001 in rig.processes.table


def test_shared_memory_in_use_without_a_dead_postmaster_is_just_an_error(
    rig: Rig,
) -> None:
    """No stale lock, so no PID whose children could be called orphans."""
    rig.existing_cluster()
    rig.processes.add(4001, rig.runtime.postgres, parent=DEAD_POSTMASTER)
    rig.commands.start_script = [rig.commands.fail_with(SHARED_MEMORY_FAILURE)]

    with pytest.raises(StartupFailedError):
        rig.start()

    assert rig.processes.terminated == []


# --------------------------------------------------------------------------
# The orphans hold startup.log before any server finds the shared memory
# --------------------------------------------------------------------------


def test_startup_log_held_by_an_orphan_is_waited_for_within_the_grace(
    rig: Rig,
) -> None:
    """Measured, not designed: the children of a killed postmaster keep
    `startup.log` open as their stdout, so rewriting it is the first thing
    that fails -- and they leave by themselves within tens of milliseconds."""
    orphan_scene(rig)
    rig.commands.start_script = []
    rig.commands.startup_log_held_by = {4001}
    polls = 0

    def orphan_exits() -> None:
        nonlocal polls
        polls += 1
        if polls == 6:
            rig.processes.table.pop(4001, None)

    rig.processes.on_poll = orphan_exits

    started = rig.start()

    assert started.started_here is True
    assert rig.processes.terminated == []
    assert len(rig.commands.ports_asked) == 1


def test_an_orphan_that_keeps_holding_startup_log_is_ended_and_only_it(
    rig: Rig,
) -> None:
    orphan_scene(rig)
    rig.commands.start_script = []
    rig.commands.startup_log_held_by = {4001}

    started = rig.start()

    assert rig.processes.terminated == [4001]
    assert 7001 in rig.processes.table and 7002 in rig.processes.table
    assert started.started_here is True
    assert len(rig.commands.ports_asked) == 1


def test_startup_log_held_by_a_process_that_is_nobodys_orphan_is_an_error(
    rig: Rig,
) -> None:
    """No dead postmaster to name, so there is nobody the adapter may end."""
    rig.existing_cluster()
    rig.processes.add(7002, rig.runtime.postgres, parent=9999)
    rig.commands.startup_log_held_by = {7002}

    with pytest.raises(StartupFailedError, match="startup.log"):
        rig.start()

    assert rig.processes.terminated == []
    assert rig.commands.ports_asked == []


# --------------------------------------------------------------------------
# pg_ctl's exit code is a hint: the answer is a live server in postmaster.pid
# --------------------------------------------------------------------------


def test_a_pg_ctl_fooled_by_the_stale_lock_file_is_not_believed(rig: Rig) -> None:
    """On Windows `pg_ctl -w` cannot match the file to the process it
    started, and takes a stale `ready` not older than two seconds for the
    new server's. The adapter waits for a live postmaster of its own."""
    rig.existing_cluster()
    rig.stale_pid_file()
    rig.commands.start_script = [lambda files: 0]
    polls = 0

    def new_server_comes_up() -> None:
        nonlocal polls
        polls += 1
        if polls == 4:
            rig.processes.add(5000, rig.runtime.postgres)
            write_pid_file(rig.files, 5000, 60000, "starting")
        if polls == 8:
            write_pid_file(rig.files, 5000, 60000, "ready   ")

    rig.processes.on_poll = new_server_comes_up

    started = rig.start()

    assert started.connection.port == 60000, "not the dead server's port"
    assert started.recovered is True
    assert len(rig.commands.ports_asked) == 1


def test_a_fooled_pg_ctl_whose_server_died_is_read_from_the_log(rig: Rig) -> None:
    """`pg_ctl` said yes over the stale file, and the new server never came:
    the refused `bind` in its log ends the wait, and the bind rule applies."""
    rig.existing_cluster()
    rig.stale_pid_file()
    rig.commands.start_script = [
        rig.commands.fail_in_server_log(BIND_FAILURE, returncode=0)
    ]

    started = rig.start()

    assert rig.commands.ports_asked == [LAST_PORT, 52001]
    assert started.connection.port == 52001


# --------------------------------------------------------------------------
# The start fails: bind (section 8.2, step 4)
# --------------------------------------------------------------------------


def test_a_port_taken_between_the_probe_and_the_start_gets_another(rig: Rig) -> None:
    """The refusal is read where PostgreSQL 16 writes it: the collector is
    up before the sockets are, so it is in the server's log, not in
    `startup.log` -- the opposite of what RFC-033 section 8.1 first assumed."""
    rig.existing_cluster()
    rig.commands.start_script = [rig.commands.fail_in_server_log(BIND_FAILURE)]

    started = rig.start()

    assert rig.commands.ports_asked == [LAST_PORT, 52001]
    assert started.connection.port == 52001
    assert configured_port(rig.files) == 52001


def test_bind_failures_are_retried_a_bounded_number_of_times(rig: Rig) -> None:
    rig.existing_cluster()
    rig.commands.start_script = [rig.commands.fail_in_server_log(BIND_FAILURE)] * 5

    with pytest.raises(StartupFailedError, match="could not bind"):
        rig.start()

    assert len(rig.commands.ports_asked) == ClusterTimeouts().max_port_attempts


def test_a_bind_refused_on_an_earlier_start_is_not_this_starts_failure(
    rig: Rig,
) -> None:
    """The collector's file is appended to by every start of the day. Only
    what this start wrote decides what this start's failure was."""
    rig.existing_cluster()
    append_server_log(rig.files, BIND_FAILURE)
    rig.commands.start_script = [rig.commands.fail_with(OTHER_FAILURE)]

    with pytest.raises(StartupFailedError) as failure:
        rig.start()

    assert len(rig.commands.ports_asked) == 1, "not retried as a bind failure"
    assert "could not bind" not in str(failure.value)
    assert '"$libdir/vector"' in str(failure.value)


def test_a_failure_in_the_servers_log_is_in_the_error_too(rig: Rig) -> None:
    rig.existing_cluster()
    rig.commands.start_script = [
        rig.commands.fail_in_server_log(
            "2026-10-11 12:00:00.000 -03 [1] FATAL:  could not load pg_hba.conf\n"
        )
    ]

    with pytest.raises(StartupFailedError, match="could not load pg_hba.conf"):
        rig.start()


# --------------------------------------------------------------------------
# Any other failure: an error with the end of startup.log, no retry
# --------------------------------------------------------------------------


def test_any_other_failure_is_reported_with_the_log_and_not_retried(rig: Rig) -> None:
    rig.existing_cluster()
    rig.commands.start_script = [rig.commands.fail_with(OTHER_FAILURE)]

    with pytest.raises(StartupFailedError) as failure:
        rig.start()

    assert '"$libdir/vector"' in str(failure.value)
    assert len(rig.commands.ports_asked) == 1
    assert rig.processes.terminated == []


# --------------------------------------------------------------------------
# stop (section 8.5)
# --------------------------------------------------------------------------


def test_stop_is_a_fast_shutdown(rig: Rig) -> None:
    rig.existing_cluster()
    rig.running_server()

    assert rig.cluster.stop() is StopOutcome.FAST
    assert rig.commands.stop_calls == ["fast"]


def test_stop_returns_when_the_processes_are_gone_not_just_the_lock(
    rig: Rig,
) -> None:
    """`pg_ctl stop -w` returns when `postmaster.pid` disappears. Measured:
    up to two of the server's seven processes are still alive at that
    instant, and gone 0.18 s later at worst (RFC-033 section 18)."""
    rig.existing_cluster()
    rig.running_server(pid=3000)
    rig.processes.add(3001, rig.runtime.postgres, parent=3000)
    rig.commands.lingering = 4

    assert rig.cluster.stop() is StopOutcome.FAST

    assert rig.commands.polls_after_stop() > 4, "it waited for the helper"
    assert 3001 not in rig.processes.table
    assert rig.processes.terminated == [], "and ended nobody"


def test_a_pid_windows_gave_to_another_program_is_not_waited_for(rig: Rig) -> None:
    """A PID does not name a process for long. The helper has exited, and its
    PID now belongs to a program that has nothing to do with the cluster."""
    rig.existing_cluster()
    rig.running_server(pid=3000)
    rig.processes.add(3001, rig.runtime.postgres, parent=3000)
    rig.commands.reuse_pid_after_stop = 3001

    assert rig.cluster.stop() is StopOutcome.FAST

    assert rig.commands.polls_after_stop() <= 2, "nothing of ours was left"
    assert 3001 in rig.processes.table, "the other program is none of our business"
    assert rig.processes.terminated == []


def test_a_stranger_whose_dead_parent_had_the_postmasters_pid_is_not_waited_for(
    rig: Rig,
) -> None:
    """A process records its parent's PID once, and Windows never updates
    it. This one was started long ago by a program that has since exited,
    and the postmaster happens to have been given that program's number: in
    a snapshot it looks like a child of the server, and it is not."""
    rig.existing_cluster()
    rig.running_server(pid=3000)
    rig.processes.add(8000, Path(r"C:\Windows\explorer.exe"), parent=3000)

    assert rig.cluster.stop() is StopOutcome.FAST

    assert rig.commands.polls_after_stop() <= 2
    assert 8000 in rig.processes.table
    assert rig.processes.terminated == []


def test_immediate_is_tried_only_when_fast_fails(rig: Rig) -> None:
    rig.existing_cluster()
    rig.running_server()
    rig.commands.stop_results["fast"] = 1

    assert rig.cluster.stop() is StopOutcome.IMMEDIATE
    assert rig.commands.stop_calls == ["fast", "immediate"]


def test_when_both_fail_the_postmaster_is_reported_not_killed(rig: Rig) -> None:
    rig.existing_cluster()
    rig.running_server(pid=3000)
    rig.commands.stop_results = {"fast": 1, "immediate": 1}

    with pytest.raises(StopFailedError, match="does not kill"):
        rig.cluster.stop()

    assert rig.processes.terminated == []
    assert 3000 in rig.processes.table


def test_stopping_a_stopped_cluster_runs_nothing(rig: Rig) -> None:
    rig.existing_cluster()

    assert rig.cluster.stop() is StopOutcome.NOT_RUNNING
    assert rig.commands.stop_calls == []


def test_stopping_where_there_is_no_cluster_creates_nothing(rig: Rig) -> None:
    assert rig.cluster.stop() is StopOutcome.NOT_RUNNING
    assert not rig.files.data.exists()


def test_stop_leaves_a_stale_lock_where_it_is(rig: Rig) -> None:
    rig.existing_cluster()
    rig.stale_pid_file()

    assert rig.cluster.stop() is StopOutcome.NOT_RUNNING
    assert rig.files.postmaster_pid.exists()
    assert rig.commands.stop_calls == []


# --------------------------------------------------------------------------
# status (section 4.2)
# --------------------------------------------------------------------------


def test_status_of_nothing_is_absent(rig: Rig) -> None:
    assert rig.cluster.status().status is ClusterStatus.ABSENT


def test_status_of_an_interrupted_first_run_is_absent(rig: Rig) -> None:
    rig.files.data_initdb.mkdir()

    assert rig.cluster.status().status is ClusterStatus.ABSENT
    assert rig.files.data_initdb.exists(), "status only looks"


def test_status_of_a_stopped_cluster_is_stopped(rig: Rig) -> None:
    rig.existing_cluster()

    report = rig.cluster.status()

    assert report.status is ClusterStatus.STOPPED
    assert report.port is None and report.pid is None


def test_status_of_a_dead_postmaster_is_stopped(rig: Rig) -> None:
    rig.existing_cluster()
    rig.stale_pid_file()

    assert rig.cluster.status().status is ClusterStatus.STOPPED


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        ("starting", ClusterStatus.STARTING),
        ("ready   ", ClusterStatus.READY),
        ("stopping", ClusterStatus.STOPPING),
    ],
)
def test_status_of_a_live_server_is_what_its_lock_file_says(
    rig: Rig, written: str, expected: ClusterStatus
) -> None:
    rig.existing_cluster()
    rig.running_server(pid=3000, status=written)

    report = rig.cluster.status()

    assert report.status is expected
    assert (report.port, report.pid) == (LAST_PORT, 3000)


def test_status_of_another_major_version_says_so(rig: Rig) -> None:
    rig.existing_cluster(version="15")

    assert rig.cluster.status().status is ClusterStatus.OTHER_VERSION


def test_status_of_a_cluster_held_by_another_runtime_says_so(rig: Rig) -> None:
    rig.existing_cluster()
    rig.processes.add(3000, rig.other_runtime.postgres)
    write_pid_file(rig.files, 3000, LAST_PORT)

    report = rig.cluster.status()

    assert report.status is ClusterStatus.OTHER_RUNTIME
    assert str(rig.other_runtime.postgres) in report.detail


def test_status_never_carries_the_password(rig: Rig) -> None:
    password = rig.existing_cluster()
    rig.running_server()

    assert password not in repr(rig.cluster.status())


# --------------------------------------------------------------------------
# locate (RFC-034 section 7.2): read, never start
# --------------------------------------------------------------------------


def test_locate_gives_the_connection_of_a_cluster_that_is_up(rig: Rig) -> None:
    password = rig.existing_cluster()
    rig.running_server()

    found = rig.cluster.locate()

    assert (found.host, found.port) == ("127.0.0.1", LAST_PORT)
    assert found.password == password
    assert (found.user, found.database) == ("solidvision", "solidvision")


def test_locate_reads_the_port_the_server_is_using_not_the_one_asked_for(
    rig: Rig,
) -> None:
    """Line 4 of `postmaster.pid`. The conf only says what the adapter asked."""
    rig.existing_cluster(port=LAST_PORT)
    rig.processes.add(3000, rig.runtime.postgres)
    write_pid_file(rig.files, 3000, 60000)

    assert configured_port(rig.files) == LAST_PORT
    assert rig.cluster.locate().port == 60000


def test_locate_follows_the_cluster_to_another_port(rig: Rig) -> None:
    """Asked again after a restart, it answers again: nothing is remembered."""
    rig.existing_cluster()
    rig.running_server()
    assert rig.cluster.locate().port == LAST_PORT

    write_pid_file(rig.files, 3000, 60000)

    assert rig.cluster.locate().port == 60000


def test_locate_starts_nothing_and_connects_to_nothing(rig: Rig) -> None:
    """No `pg_ctl`, no login, and no lock: `adapter.lock` is not even created."""
    rig.existing_cluster()
    rig.running_server()
    before = sorted(path.name for path in rig.files.directory.iterdir())

    rig.cluster.locate()

    assert rig.commands.ports_asked == []
    assert rig.commands.stop_calls == []
    assert rig.logins == []
    assert sorted(path.name for path in rig.files.directory.iterdir()) == before
    assert not rig.files.lock.exists()


def test_locate_where_there_is_no_cluster_says_so_and_creates_nothing(
    rig: Rig,
) -> None:
    with pytest.raises(ClusterNotRunningError, match="no embedded cluster") as failure:
        rig.cluster.locate()

    assert "embedded start" in str(failure.value), "who starts it"
    assert rig.commands.initdb_calls == []
    assert not rig.files.data.exists()


def test_locate_does_not_take_an_interrupted_first_run_for_a_cluster(
    rig: Rig,
) -> None:
    rig.files.data_initdb.mkdir()

    with pytest.raises(ClusterNotRunningError):
        rig.cluster.locate()

    assert rig.files.data_initdb.exists(), "locate only looks"


def test_locate_on_a_stopped_cluster_says_what_it_found(rig: Rig) -> None:
    rig.existing_cluster()

    with pytest.raises(ClusterNotRunningError, match="stopped"):
        rig.cluster.locate()

    assert rig.commands.ports_asked == [], "and does not start it"


def test_locate_does_not_believe_a_dead_postmasters_lock_file(rig: Rig) -> None:
    """The file still says `ready` and still names a port. The PID is gone."""
    rig.existing_cluster()
    rig.stale_pid_file()

    with pytest.raises(ClusterNotRunningError, match="stale"):
        rig.cluster.locate()


@pytest.mark.parametrize("written", ["starting", "stopping"])
def test_locate_does_not_wait_for_a_server_that_is_not_ready(
    rig: Rig, written: str
) -> None:
    rig.existing_cluster()
    rig.running_server(status=written)

    with pytest.raises(ClusterNotRunningError, match=written):
        rig.cluster.locate()


def test_locate_refuses_a_cluster_of_another_major_version(rig: Rig) -> None:
    rig.existing_cluster(version="15")

    with pytest.raises(ClusterNotRunningError, match="other major version"):
        rig.cluster.locate()


def test_locate_refuses_a_cluster_held_by_another_runtime(rig: Rig) -> None:
    """Its port and this directory's password are not a pair."""
    rig.existing_cluster()
    rig.processes.add(3000, rig.other_runtime.postgres)
    write_pid_file(rig.files, 3000, LAST_PORT)

    with pytest.raises(ClusterNotRunningError, match="another runtime") as failure:
        rig.cluster.locate()

    assert str(rig.other_runtime.postgres) in str(failure.value)


def test_locate_on_a_running_cluster_without_its_password_refuses(rig: Rig) -> None:
    rig.existing_cluster()
    rig.running_server()
    rig.files.password.unlink()

    with pytest.raises(ClusterRefusedError, match="pair is lost"):
        rig.cluster.locate()


def test_no_error_from_locate_carries_the_password(rig: Rig) -> None:
    password = rig.existing_cluster()

    with pytest.raises(ClusterNotRunningError) as failure:
        rig.cluster.locate()

    assert password not in str(failure.value)
    rig.running_server()
    assert password not in repr(rig.cluster.locate())


# --------------------------------------------------------------------------
# What `run --` gives its command (RFC-034 section 7.6)
# --------------------------------------------------------------------------


def test_the_environment_names_the_source_and_the_two_directories(rig: Rig) -> None:
    environment = rig.cluster.environment()

    assert tuple(environment) == ENVIRONMENT_VARIABLES
    assert environment == {
        "DATABASE_SOURCE": "embedded",
        "EMBEDDED_POSTGRES_DIRECTORY": str(rig.home.root),
        "EMBEDDED_POSTGRES_RUNTIME_DIRECTORY": str(rig.runtime.root),
    }


def test_neither_the_port_nor_the_password_travels_in_the_environment(
    rig: Rig,
) -> None:
    """The child reads both from the cluster. A port exported here would be
    wrong the day the cluster restarted on another (section 2.2)."""
    password = rig.existing_cluster()
    rig.running_server()

    environment = rig.cluster.environment()

    assert password not in "".join(environment.values())
    assert str(LAST_PORT) not in "".join(environment.values())
    assert not [name for name in environment if "PASSWORD" in name or "PORT" in name]


def test_every_environment_variable_is_a_settings_field() -> None:
    """A misspelt name would not fail: `Settings` would fall back to the
    `.env`, the `.env` points at Docker, and the suite would pass against the
    wrong database printing the same `passed` (RFC-033a section 4.2)."""
    for variable in ENVIRONMENT_VARIABLES:
        assert variable.lower() in Settings.model_fields, variable
    assert DatabaseSource(EMBEDDED_SOURCE) is DatabaseSource.EMBEDDED


def test_the_environment_points_the_projects_settings_at_this_cluster(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In a checkout, the environment is above the `.env` (RFC-004)."""
    for name, value in rig.cluster.environment().items():
        monkeypatch.setenv(name, value)

    loaded = Settings()

    assert loaded.database_source is DatabaseSource.EMBEDDED
    assert loaded.embedded_postgres_directory == rig.home.root
    assert loaded.embedded_postgres_runtime_directory == rig.runtime.root
