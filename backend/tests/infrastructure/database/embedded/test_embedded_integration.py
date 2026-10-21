"""The adapter against the real runtime (RFC-033 section 12).

Marked `embedded` and deselected by default, like `slow`: these start real
PostgreSQL servers. Run them with `pytest -m embedded`. Without the runtime
they are skipped with the command that materializes it.

**One `initdb` for the whole file.** A session fixture creates one cluster
and stops it; every test that needs a cluster gets a *copy* of that
directory, because copying a stopped cluster produces a valid cluster. Each
copy lives in the test's own temporary directory and is removed with it, so
nothing here goes near `%LOCALAPPDATA%\\SolidVision`.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
import pytest
import sqlalchemy
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import QueuePool

from app.infrastructure.config.layout import Environment, Layout, layout_at
from app.infrastructure.config.settings import Settings, load_settings, settings
from app.infrastructure.database.embedded.cluster import (
    INCLUDE_LINE,
    ClusterStatus,
    EmbeddedPostgres,
    Phase,
    PortProber,
    StartedCluster,
    StopOutcome,
)
from app.infrastructure.database.embedded.cluster_home import ClusterFiles, ClusterHome
from app.infrastructure.database.embedded.code_page import (
    fits_ansi_code_page,
    short_path,
)
from app.infrastructure.database.embedded.database_setup import (
    ClusterConnection,
    code_revisions,
)
from app.infrastructure.database.embedded.errors import (
    ClusterNotRunningError,
    ClusterRefusedError,
    EmbeddedPostgresError,
    RuntimeInvalidError,
)
from app.infrastructure.database.embedded.postmaster_pid import read_postmaster_pid
from app.infrastructure.database.embedded.postmaster_signal import (
    FAST,
    IMMEDIATE,
    send,
)
from app.infrastructure.database.embedded.runtime import PostgresRuntime
from app.infrastructure.database.embedded.windows_processes import WindowsProcesses
from app.infrastructure.persistence.engine import build_engine

pytestmark = [
    pytest.mark.embedded,
    pytest.mark.skipif(
        sys.platform != "win32",
        reason="The embedded cluster is Windows-only (RFC-033 section 4.1)",
    ),
]

BACKEND = Path(__file__).resolve().parents[4]

NO_PARAMETER_ANSWERS = {
    "PGTZ": "Mars/Phobos",
    "PGDATESTYLE": "banana",
    "PGGEQO": "banana",
    "PGSERVICE": "no-such-service",
}
"""Hostile values of the libpq variables no connection parameter cancels."""


@dataclass(frozen=True)
class Template:
    root: Path
    first_run: StartedCluster
    phases: tuple[Phase, ...]


@pytest.fixture(scope="session")
def runtime() -> PostgresRuntime:
    runtime = PostgresRuntime(settings.embedded_postgres_runtime_directory)
    try:
        runtime.manifest()
    except RuntimeInvalidError as error:
        pytest.skip(str(error))
    return runtime


@pytest.fixture(scope="session")
def template(
    runtime: PostgresRuntime, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Template]:
    """The one cluster this file creates: started once, then stopped."""
    root = tmp_path_factory.mktemp("embedded-template") / "SolidVision" / "postgres"
    cluster = EmbeddedPostgres(runtime, ClusterHome(root))
    phases: list[Phase] = []
    started = cluster.start(on_phase=phases.append)
    cluster.stop()
    yield Template(root=root, first_run=started, phases=tuple(phases))
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture()
def home_root(template: Template, tmp_path: Path) -> Path:
    root = tmp_path / "SolidVision" / "postgres"
    shutil.copytree(template.root, root)
    return root


@pytest.fixture()
def files(home_root: Path) -> ClusterFiles:
    return ClusterHome(home_root).files(16)


@pytest.fixture()
def cluster(
    runtime: PostgresRuntime, home_root: Path, files: ClusterFiles
) -> Iterator[EmbeddedPostgres]:
    cluster = EmbeddedPostgres(runtime, ClusterHome(home_root))
    try:
        yield cluster
    finally:
        _leave_nothing_running(cluster, files)
        shutil.rmtree(home_root, ignore_errors=True)


def _leave_nothing_running(cluster: EmbeddedPostgres, files: ClusterFiles) -> None:
    """The test's cleanup, not the adapter's: it may kill, the adapter may not."""
    try:
        cluster.stop()
    except EmbeddedPostgresError:
        pass
    pid_file = read_postmaster_pid(files.postmaster_pid)
    processes = WindowsProcesses()
    if pid_file is not None and processes.is_alive(pid_file.pid):
        for process in processes.snapshot():
            if process.parent_pid == pid_file.pid:
                processes.terminate(process.pid)
        processes.terminate(pid_file.pid)


def connect(connection: ClusterConnection, dbname: str = "postgres") -> Any:
    return psycopg.connect(connection.conninfo(dbname), autocommit=True)


# --------------------------------------------------------------------------
# First run (section 7)
# --------------------------------------------------------------------------


def test_the_first_run_created_the_cluster_and_said_so(template: Template) -> None:
    assert template.first_run.created is True
    assert template.first_run.started_here is True
    assert template.first_run.recovered is False
    assert template.phases == (Phase.INITDB, Phase.STARTING)


def test_the_first_run_left_a_whole_cluster_and_no_half_one(template: Template) -> None:
    files = ClusterHome(template.root).files(16)

    assert files.pg_version.read_text(encoding="ascii").strip() == "16"
    assert not files.data_initdb.exists()
    assert not files.password_tmp.exists()
    assert files.password.read_text(encoding="ascii").strip() == (
        template.first_run.connection.password
    )
    conf = files.postgresql_conf.read_text(encoding="utf-8")
    assert conf.rstrip().endswith(INCLUDE_LINE)


def test_a_copy_of_a_stopped_cluster_is_a_valid_cluster(
    cluster: EmbeddedPostgres, template: Template
) -> None:
    """What makes one `initdb` enough for this file -- and what an installer
    that shipped a ready `data\\` would be relying on (section 13)."""
    started = cluster.start()

    assert started.created is False
    assert started.connection.password == template.first_run.connection.password
    with connect(started.connection) as db:
        assert db.execute("SELECT 1").fetchone() == (1,)


# --------------------------------------------------------------------------
# Start, adopt, stop (section 8)
# --------------------------------------------------------------------------


def test_the_second_start_adopts_and_only_the_first_is_the_owner(
    cluster: EmbeddedPostgres,
) -> None:
    first = cluster.start()
    second = cluster.start()

    assert (first.started_here, second.started_here) == (True, False)
    assert second.connection == first.connection
    assert cluster.status().status is ClusterStatus.READY


def test_stop_stops_and_a_second_stop_finds_nothing_to_do(
    cluster: EmbeddedPostgres, files: ClusterFiles
) -> None:
    started = cluster.start()
    postmaster = read_postmaster_pid(files.postmaster_pid)
    assert postmaster is not None
    processes = WindowsProcesses()
    children = [
        process.pid
        for process in processes.snapshot()
        if process.parent_pid == postmaster.pid
    ]
    assert children, "a running server has helper processes"

    assert cluster.stop() is StopOutcome.FAST

    assert cluster.status().status is ClusterStatus.STOPPED
    assert not files.postmaster_pid.exists()
    assert not any(processes.is_alive(pid) for pid in [postmaster.pid, *children])
    assert cluster.stop() is StopOutcome.NOT_RUNNING
    # A plain socket: psycopg on Windows reports a refused connection only
    # when its own timeout runs out, which without one is two minutes.
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", started.connection.port), timeout=5)


def test_the_port_is_the_one_the_server_itself_reports(
    cluster: EmbeddedPostgres, runtime: PostgresRuntime
) -> None:
    started = cluster.start()

    with connect(started.connection) as db:
        port, listen, version = db.execute(
            "SELECT inet_server_port(), current_setting('listen_addresses'), "
            "current_setting('server_version')"
        ).fetchone()

    assert port == started.connection.port == cluster.status().port
    assert listen == "127.0.0.1"
    assert version == runtime.manifest().postgresql


def test_a_busy_last_port_moves_the_server_to_another(
    cluster: EmbeddedPostgres, files: ClusterFiles
) -> None:
    """Section 8.2: the port of the last start is taken by someone else."""
    last_port = cluster.start().connection.port
    cluster.stop()

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as squatter:
        squatter.bind(("127.0.0.1", last_port))
        squatter.listen()

        started = cluster.start()

        assert started.connection.port != last_port
        with connect(started.connection) as db:
            assert db.execute("SELECT inet_server_port()").fetchone() == (
                started.connection.port,
            )
    conf = files.server_conf.read_text(encoding="utf-8")
    assert f"port = {started.connection.port}\n" in conf


class ProbeThatSaysYes(PortProber):
    """The race of section 8.2, step 4, made certain: between the probe and
    the start, someone took the port. Here the probe never notices."""

    def usable(self, port: int) -> bool:
        return True


def test_a_port_taken_after_the_probe_is_recognized_from_the_servers_own_words(
    runtime: PostgresRuntime, home_root: Path, cluster: EmbeddedPostgres
) -> None:
    """The real server, really refused by `bind`, and the adapter reading the
    refusal where PostgreSQL 16 writes it: in the collector's log. This is
    the test that would have failed with the text read from `startup.log`."""
    last_port = cluster.start().connection.port
    cluster.stop()
    raced = EmbeddedPostgres(runtime, ClusterHome(home_root), ports=ProbeThatSaysYes())

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as squatter:
        squatter.bind(("127.0.0.1", last_port))
        squatter.listen()

        started = raced.start()

        assert started.connection.port != last_port
        with connect(started.connection) as db:
            assert db.execute("SELECT 1").fetchone() == (1,)


def test_a_changed_password_file_is_refused_on_adoption(
    cluster: EmbeddedPostgres, files: ClusterFiles
) -> None:
    """Section 8.4: the pair is lost, and nothing else is tried."""
    cluster.start()
    files.password.write_text("not-the-clusters-password\n", encoding="ascii")

    with pytest.raises(ClusterRefusedError, match="rejected the password"):
        cluster.start()

    assert cluster.status().status is ClusterStatus.READY


def test_an_environment_full_of_pg_variables_changes_nothing(
    cluster: EmbeddedPostgres,
    files: ClusterFiles,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Section 5.3, and the adapter's own connections too.

    `PGDATA` and `PGPORT` would send `pg_ctl` to another cluster; a read-only
    `PGOPTIONS` would make the `CREATE DATABASE` below fail if libpq picked
    it up from this process. The last four have no parameter that answers
    them, and each refuses the login on its own (RFC-034 section 16).
    """
    monkeypatch.setenv("PGDATA", str(tmp_path / "another-cluster"))
    monkeypatch.setenv("PGPORT", "1")
    monkeypatch.setenv("PGPASSWORD", "wrong")
    monkeypatch.setenv("PGOPTIONS", "-c default_transaction_read_only=on")
    for name, value in NO_PARAMETER_ANSWERS.items():
        monkeypatch.setenv(name, value)

    started = cluster.start()
    prepared = cluster.prepare(started.connection)

    assert prepared.created_database is True
    postmaster = read_postmaster_pid(files.postmaster_pid)
    assert postmaster is not None and postmaster.points_to(files.data)
    assert started.connection.port != 1


# --------------------------------------------------------------------------
# The database and the migrations (section 7.4)
# --------------------------------------------------------------------------


def test_prepare_creates_the_database_migrates_it_and_is_idempotent(
    cluster: EmbeddedPostgres, runtime: PostgresRuntime
) -> None:
    started = cluster.start()
    phases: list[Phase] = []
    heads, _ = code_revisions()

    first = cluster.prepare(started.connection, on_phase=phases.append)
    second = cluster.prepare(started.connection, on_phase=phases.append)

    assert first.created_database is True
    assert first.migrated_from == ()
    assert set(first.heads) == heads
    assert first.warnings == ()
    assert second.created_database is False
    assert second.migrated_from is None
    assert phases == [Phase.MIGRATING], "the second call had nothing to migrate"
    with connect(started.connection, "solidvision") as db:
        encoding, collate, ctype = db.execute(
            "SELECT pg_encoding_to_char(encoding), datcollate, datctype "
            "FROM pg_database WHERE datname = current_database()"
        ).fetchone()
        (vector,) = db.execute(
            "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
        ).fetchone()
        (revision,) = db.execute("SELECT version_num FROM alembic_version").fetchone()
        (images,) = db.execute("SELECT to_regclass('images')::text").fetchone()
    assert (encoding, collate, ctype) == ("UTF8", "C", "C")
    assert vector == runtime.manifest().pgvector
    assert {revision} == heads
    assert images == "images"


def test_a_database_newer_than_the_code_is_refused_and_not_written_to(
    cluster: EmbeddedPostgres,
) -> None:
    """Section 7.4: an older app opening a database a newer one migrated."""
    started = cluster.start()
    cluster.prepare(started.connection)
    with connect(started.connection, "solidvision") as db:
        db.execute("UPDATE alembic_version SET version_num = '0123456789ab'")

    with pytest.raises(ClusterRefusedError, match="does not know"):
        cluster.prepare(started.connection)

    with connect(started.connection, "solidvision") as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchall() == [
            ("0123456789ab",)
        ]
        assert db.execute("SELECT to_regclass('images')::text").fetchone() == (
            "images",
        )


# --------------------------------------------------------------------------
# Refusals and leftovers (section 9.1)
# --------------------------------------------------------------------------


def test_another_major_version_is_refused_and_nothing_is_written_over_it(
    cluster: EmbeddedPostgres, files: ClusterFiles
) -> None:
    files.pg_version.write_text("15\n", encoding="ascii")
    before = sorted(path.name for path in files.data.iterdir())

    with pytest.raises(ClusterRefusedError, match="another major version"):
        cluster.start()

    assert files.pg_version.read_text(encoding="ascii") == "15\n"
    assert sorted(path.name for path in files.data.iterdir()) == before
    assert not files.data_initdb.exists()
    assert cluster.status().status is ClusterStatus.OTHER_VERSION


def test_a_leftover_data_initdb_is_removed_on_the_way_up(
    cluster: EmbeddedPostgres, files: ClusterFiles
) -> None:
    files.data_initdb.mkdir()
    (files.data_initdb / "PG_VERSION").write_text("16\n", encoding="ascii")

    started = cluster.start()

    assert started.created is False
    assert not files.data_initdb.exists()


# --------------------------------------------------------------------------
# Dirty shutdown (section 9)
# --------------------------------------------------------------------------


def test_a_killed_postmaster_is_recovered_and_no_commit_is_lost(
    cluster: EmbeddedPostgres, files: ClusterFiles
) -> None:
    """The roadmap's criterion: tested, not presumed.

    Every row below was committed, and its commit confirmed to this client,
    before the postmaster was ended the way Task Manager ends it.
    """
    started = cluster.start()
    with connect(started.connection) as db:
        db.execute("CREATE TABLE confirmed (n integer PRIMARY KEY)")
        for n in range(200):
            db.execute("INSERT INTO confirmed VALUES (%s)", [n])
    postmaster = read_postmaster_pid(files.postmaster_pid)
    assert postmaster is not None

    assert WindowsProcesses().terminate(postmaster.pid) is True

    phases: list[Phase] = []
    recovered = cluster.start(on_phase=phases.append)

    assert files.postmaster_pid.exists(), "the adapter never deleted the lock"
    assert recovered.started_here is True
    assert recovered.recovered is True
    assert Phase.RECOVERING in phases
    with connect(recovered.connection) as db:
        assert db.execute("SELECT count(*), max(n) FROM confirmed").fetchone() == (
            200,
            199,
        )


class NoCommands:
    """`PostgresCommands` that a stop must not touch: at a logoff, `pg_ctl`
    cannot start (RFC-036 section 7.5)."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"the stop ran pg_ctl ({name})")


def test_a_stop_through_the_signal_pipe_is_a_clean_fast_shutdown(
    cluster: EmbeddedPostgres,
    runtime: PostgresRuntime,
    home_root: Path,
    files: ClusterFiles,
) -> None:
    cluster.start()
    postmaster = read_postmaster_pid(files.postmaster_pid)
    assert postmaster is not None
    processes = WindowsProcesses()
    children = [
        process.pid
        for process in processes.snapshot()
        if process.parent_pid == postmaster.pid
    ]
    without_pg_ctl = EmbeddedPostgres(
        runtime,
        ClusterHome(home_root),
        commands=NoCommands(),  # type: ignore[arg-type]
    )

    assert without_pg_ctl.stop_by_signal(10.0) is StopOutcome.FAST

    assert not any(processes.is_alive(pid) for pid in [postmaster.pid, *children])
    assert not files.postmaster_pid.exists()
    assert without_pg_ctl.stop_by_signal(10.0) is StopOutcome.NOT_RUNNING
    assert cluster.start().recovered is False, "a fast shutdown needs no recovery"


def test_a_postmaster_that_ignores_the_fast_signal_gets_the_immediate_one(
    cluster: EmbeddedPostgres, runtime: PostgresRuntime, home_root: Path
) -> None:
    """The fallback of `stop()`, by the pipe: the first signal is lost here."""
    cluster.start()
    sent: list[int] = []

    def lose_the_first(pid: int, signal: int) -> bool:
        sent.append(signal)
        return len(sent) > 1 and send(pid, signal)

    signalled = EmbeddedPostgres(
        runtime,
        ClusterHome(home_root),
        commands=NoCommands(),  # type: ignore[arg-type]
        signal=lose_the_first,
    )

    assert signalled.stop_by_signal(10.0) is StopOutcome.IMMEDIATE

    assert sent == [FAST, IMMEDIATE]
    assert cluster.start().recovered is True


def test_an_immediate_shutdown_is_recovered_on_the_next_start(
    cluster: EmbeddedPostgres, runtime: PostgresRuntime, files: ClusterFiles
) -> None:
    cluster.start()
    subprocess.run(
        [str(runtime.pg_ctl), "stop", "-m", "immediate", "-w", "-D", str(files.data)],
        env=runtime.child_environment(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )

    assert cluster.start().recovered is True


def test_a_start_that_arrives_while_the_server_reinitializes_waits_for_it(
    cluster: EmbeddedPostgres, files: ClusterFiles
) -> None:
    """One backend dies; the postmaster ends the others and reinitializes.

    For those seconds `postmaster.pid` still says `ready` and every
    connection is refused. A caller that arrives then must be made to wait,
    not handed an error -- and not a connection that does not work yet.
    """
    started = cluster.start()
    postmaster = read_postmaster_pid(files.postmaster_pid)
    assert postmaster is not None
    with connect(started.connection) as db:
        (backend,) = db.execute("SELECT pg_backend_pid()").fetchone()
        assert WindowsProcesses().terminate(backend) is True
    deadline = time.monotonic() + 30
    while True:
        try:
            connect(started.connection).close()
        except psycopg.OperationalError:
            break
        assert time.monotonic() < deadline, "the server never refused a connection"
    assert cluster.status().status is ClusterStatus.READY, "the lock file says so"

    phases: list[Phase] = []
    adopted = cluster.start(on_phase=phases.append)

    assert adopted.started_here is False
    assert phases == [Phase.RECOVERING]
    still = read_postmaster_pid(files.postmaster_pid)
    assert still is not None and still.pid == postmaster.pid, "the same postmaster"
    with connect(adopted.connection) as db:
        assert db.execute("SELECT 1").fetchone() == (1,)


# --------------------------------------------------------------------------
# Two callers at once (section 7.1), and the CLI (section 11)
# --------------------------------------------------------------------------

START_AND_REPORT = """
import json, sys
from pathlib import Path
from app.infrastructure.database.embedded.cluster import EmbeddedPostgres
from app.infrastructure.database.embedded.cluster_home import ClusterHome
from app.infrastructure.database.embedded.postmaster_pid import read_postmaster_pid
from app.infrastructure.database.embedded.runtime import PostgresRuntime

home = ClusterHome(Path(sys.argv[2]))
started = EmbeddedPostgres(PostgresRuntime(Path(sys.argv[1])), home).start()
pid_file = read_postmaster_pid(home.files(16).postmaster_pid)
print("RESULT " + json.dumps({
    "started_here": started.started_here,
    "port": started.connection.port,
    "postmaster": pid_file.pid,
}))
"""


def test_two_processes_starting_at_once_end_with_one_postmaster(
    cluster: EmbeddedPostgres, runtime: PostgresRuntime, home_root: Path
) -> None:
    callers = [
        subprocess.Popen(
            [sys.executable, "-c", START_AND_REPORT, str(runtime.root), str(home_root)],
            cwd=BACKEND,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        for _ in range(2)
    ]
    results = []
    for caller in callers:
        output, _ = caller.communicate(timeout=120)
        assert caller.returncode == 0, output
        (line,) = [text for text in output.splitlines() if text.startswith("RESULT ")]
        results.append(json.loads(line.removeprefix("RESULT ")))

    assert sorted(result["started_here"] for result in results) == [False, True]
    assert results[0]["port"] == results[1]["port"]
    assert results[0]["postmaster"] == results[1]["postmaster"]


PROBE = """
import json, os, sys
from sqlalchemy import text
from app.infrastructure.config.settings import settings
from app.infrastructure.database.embedded.cluster import EmbeddedPostgres
from app.infrastructure.database.embedded.cluster_home import ClusterHome
from app.infrastructure.database.embedded.runtime import PostgresRuntime
from app.infrastructure.persistence.engine import EngineInstance
located = EmbeddedPostgres(
    PostgresRuntime(settings.embedded_postgres_runtime_directory),
    ClusterHome(settings.embedded_postgres_directory),
).locate()
with EngineInstance.connect() as connection:
    port = connection.execute(text("SELECT inet_server_port()")).scalar()
print("RESULT " + json.dumps({
    "source": settings.database_source.value,
    "home": str(settings.embedded_postgres_directory),
    "located": located.port,
    "server": port,
    "url": EngineInstance.url.render_as_string(hide_password=False),
    "password_in_environment": located.password in "".join(os.environ.values()),
}))
sys.exit(5)
"""


def test_run_points_the_projects_own_settings_at_the_cluster_and_stops_it(
    cluster: EmbeddedPostgres, home_root: Path
) -> None:
    """Section 11, end to end: the project's `settings` and the project's
    engine, in a child of `run`, reach this cluster and no other.

    Since RFC-034 the child is told where the cluster is, not how to connect
    to it: the port its engine reached is the one `locate()` read from
    `postmaster.pid`, and neither is in the URL or in the environment."""
    environment = os.environ.copy()
    environment["EMBEDDED_POSTGRES_DIRECTORY"] = str(home_root)
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.infrastructure.database.embedded",
            "run",
            "--",
            sys.executable,
            "-c",
            PROBE,
        ],
        cwd=BACKEND,
        env=environment,
        capture_output=True,
        text=True,
        timeout=300,
    )

    assert completed.returncode == 5, completed.stderr
    (line,) = [
        text for text in completed.stdout.splitlines() if text.startswith("RESULT ")
    ]
    seen = json.loads(line.removeprefix("RESULT "))
    assert seen["source"] == "embedded"
    assert Path(seen["home"]) == home_root
    assert seen["located"] == seen["server"]
    assert seen["server"] != 5432
    assert seen["url"] == "postgresql+psycopg://solidvision@127.0.0.1/solidvision"
    assert seen["password_in_environment"] is False
    assert cluster.status().status is ClusterStatus.STOPPED


# --------------------------------------------------------------------------
# The password (section 7.2)
# --------------------------------------------------------------------------


def test_the_password_is_in_no_log_the_adapter_or_the_server_writes(
    cluster: EmbeddedPostgres, files: ClusterFiles
) -> None:
    started = cluster.start()
    cluster.prepare(started.connection)
    wrong = ClusterConnection(port=started.connection.port, password="a-wrong-one")
    with pytest.raises(psycopg.OperationalError):
        connect(wrong)
    cluster.stop()
    password = started.connection.password.encode("ascii")

    written = [files.startup_log, files.pg_ctl_log, files.server_conf]
    written += sorted((files.data / "log").glob("*"))

    assert len(written) > 3, "the server's own log must be among them"
    for path in written:
        assert password not in path.read_bytes(), path


# --------------------------------------------------------------------------
# A user directory outside the ANSI code page (section 10)
# --------------------------------------------------------------------------


def test_a_cluster_under_a_directory_outside_the_code_page_starts_and_stops(
    runtime: PostgresRuntime, template: Template, tmp_path: Path
) -> None:
    """`pg_ctl` and the server, given the cluster through its short name.

    The `initdb` of such a directory is in `measure_adapter.py`; here the
    cluster is a copy, so this costs no second `initdb`.
    """
    root = tmp_path / "Łukasz 東京" / "SolidVision" / "postgres"
    shutil.copytree(template.root, root)
    if fits_ansi_code_page(str(root)):
        pytest.skip("this Windows' code page holds the whole path")
    if not short_path(root).isascii():
        pytest.skip("this volume generates no 8.3 short names")
    cluster = EmbeddedPostgres(runtime, ClusterHome(root))
    long_files = ClusterHome(root).files(16)
    try:
        started = cluster.start()
        prepared = cluster.prepare(started.connection)
        adopted = cluster.start()
        report = cluster.status()

        assert (started.started_here, adopted.started_here) == (True, False)
        assert prepared.created_database is True
        assert report.status is ClusterStatus.READY
        assert report.data_directory == long_files.data, "shown as the user knows it"
        assert cluster.stop() is StopOutcome.FAST
        assert cluster.status().status is ClusterStatus.STOPPED
    finally:
        _leave_nothing_running(cluster, ClusterHome(root).for_commands(long_files))
        shutil.rmtree(root, ignore_errors=True)


# --------------------------------------------------------------------------
# locate(), and the engine that asks it (RFC-034 section 7)
# --------------------------------------------------------------------------


def a_checkout(root: Path, profile: Path) -> Layout:
    root.mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    return layout_at(root, profile)


def embedded_settings(where: Layout, runtime: PostgresRuntime, home: Path) -> Settings:
    """Constructor arguments, the strongest source in both modes: nothing a
    developer's `.env` or shell carries decides which cluster this reaches."""
    return load_settings(
        where,
        database_source="embedded",
        embedded_postgres_directory=home,
        embedded_postgres_runtime_directory=runtime.root,
    ).settings


def test_locate_reads_the_connection_of_a_running_cluster_and_starts_nothing(
    cluster: EmbeddedPostgres, files: ClusterFiles
) -> None:
    """Section 7.2, against a real server: the port is the one the server is
    using, the password is the one it accepts, and a stopped cluster is an
    error that leaves it stopped."""
    with pytest.raises(ClusterNotRunningError, match="stopped"):
        cluster.locate()
    assert not files.postmaster_pid.exists(), "locate() started nothing"

    started = cluster.start()
    located = cluster.locate()

    assert located == started.connection
    with connect(located) as db:
        assert db.execute("SELECT inet_server_port()").fetchone() == (located.port,)

    cluster.stop()

    with pytest.raises(ClusterNotRunningError, match="stopped"):
        cluster.locate()
    assert cluster.status().status is ClusterStatus.STOPPED


def test_the_engine_follows_the_cluster_to_another_port_without_being_rebuilt(
    cluster: EmbeddedPostgres,
    runtime: PostgresRuntime,
    home_root: Path,
    tmp_path: Path,
) -> None:
    """Section 7.3's hypothesis, with the pool the project uses.

    The cluster restarts on another port while the engine holds a pooled
    connection to the old one. `pool_pre_ping` finds that connection dead,
    and the one that replaces it asks `locate()` again. Nothing is rebuilt,
    which is what lets the API and the job runner outlive a restart of the
    database (section 14).
    """
    where = a_checkout(tmp_path / "checkout", tmp_path / "profile")
    engine = build_engine(embedded_settings(where, runtime, home_root))
    port = sqlalchemy.text("SELECT inet_server_port()")
    try:
        started = cluster.start()
        cluster.prepare(started.connection)
        first = started.connection.port
        with engine.connect() as connection:
            assert connection.execute(port).scalar() == first
        pool = engine.pool
        assert isinstance(pool, QueuePool)
        assert pool.checkedin() == 1, "the connection went back to the pool"

        cluster.stop()
        with pytest.raises(OperationalError, match="not ready to be connected to"):
            engine.connect()

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as squatter:
            squatter.bind(("127.0.0.1", first))
            squatter.listen()
            second = cluster.start().connection.port

            assert second != first
            with engine.connect() as connection:
                assert connection.execute(port).scalar() == second
    finally:
        engine.dispose()


def test_the_engine_reaches_the_cluster_whatever_the_pg_variables_say(
    cluster: EmbeddedPostgres,
    runtime: PostgresRuntime,
    home_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Section 7.5 against a real server: the variables the parameters answer,
    and the ones only their absence answers. Each of them, left in place,
    changes or refuses the connection (section 16)."""
    started = cluster.start()
    cluster.prepare(started.connection)
    hostile = {
        **NO_PARAMETER_ANSWERS,
        "PGOPTIONS": "-c default_transaction_read_only=on",
        "PGSSLMODE": "require",
        "PGCLIENTENCODING": "LATIN1",
        "PGTARGETSESSIONATTRS": "standby",
        "PGHOSTADDR": "192.0.2.1",
    }
    for name, value in hostile.items():
        monkeypatch.setenv(name, value)
    where = a_checkout(tmp_path / "checkout", tmp_path / "profile")
    engine = build_engine(embedded_settings(where, runtime, home_root))
    name = "Łódź/東京/✈.jpg"
    try:
        with engine.connect() as connection:
            port, zone, read_only, encoding = connection.execute(
                sqlalchemy.text(
                    "SELECT inet_server_port(), current_setting('TimeZone'), "
                    "current_setting('default_transaction_read_only'), "
                    "current_setting('client_encoding')"
                )
            ).one()
            connection.execute(sqlalchemy.text("CREATE TABLE names (name text)"))
            connection.execute(
                sqlalchemy.text("INSERT INTO names VALUES (:name)"), {"name": name}
            )
            back = connection.execute(sqlalchemy.text("SELECT name FROM names"))
            assert back.scalar() == name
    finally:
        engine.dispose()

    assert port == started.connection.port
    assert zone != "Mars/Phobos" and read_only == "off" and encoding == "UTF8"


def test_a_checkouts_cluster_and_an_installations_run_side_by_side(
    runtime: PostgresRuntime, template: Template, tmp_path: Path
) -> None:
    """Section 2.5 undone: one profile, two data directories, two servers.

    Before RFC-034 a checkout created its cluster where an installed app
    would look for its own. Here each layout's default is used as it comes,
    both clusters are up at once, each engine reaches its own server, and
    stopping one leaves the other answering.
    """
    profile = tmp_path / "AppData" / "Local"
    installation = tmp_path / "Program Files" / "SolidVision"
    installation.mkdir(parents=True)
    layouts = [
        a_checkout(tmp_path / "checkout", profile),
        layout_at(installation, profile),
    ]
    assert [where.environment for where in layouts] == [
        Environment.DEVELOPMENT,
        Environment.INSTALLED,
    ]
    homes = [where.postgres_directory for where in layouts]
    assert homes == [
        profile / "SolidVision-dev" / "postgres",
        profile / "SolidVision" / "postgres",
    ]
    clusters = []
    engines = []
    for where, home in zip(layouts, homes, strict=True):
        shutil.copytree(template.root, home)
        clusters.append(EmbeddedPostgres(runtime, ClusterHome(home)))
        engines.append(build_engine(embedded_settings(where, runtime, home)))
    seen = sqlalchemy.text(
        "SELECT inet_server_port(), current_setting('data_directory')"
    )
    try:
        started = [cluster.start() for cluster in clusters]
        for cluster, one in zip(clusters, started, strict=True):
            cluster.prepare(one.connection)

        assert [one.started_here for one in started] == [True, True], "none adopted"
        ports = [one.connection.port for one in started]
        assert ports[0] != ports[1]
        postmasters = [cluster.status().pid for cluster in clusters]
        assert None not in postmasters and postmasters[0] != postmasters[1]
        for engine, cluster, home, expected in zip(
            engines, clusters, homes, ports, strict=True
        ):
            with engine.connect() as connection:
                reached, data = connection.execute(seen).one()
            assert reached == expected == cluster.locate().port
            assert os.path.samefile(data, ClusterHome(home).files(16).data)

        clusters[1].stop()

        with pytest.raises(ClusterNotRunningError):
            clusters[1].locate()
        assert clusters[0].status().status is ClusterStatus.READY
        with engines[0].connect() as connection:
            assert connection.execute(seen).one()[0] == ports[0]
    finally:
        for engine in engines:
            engine.dispose()
        for cluster, home in zip(clusters, homes, strict=True):
            _leave_nothing_running(cluster, ClusterHome(home).files(16))
            shutil.rmtree(home, ignore_errors=True)
