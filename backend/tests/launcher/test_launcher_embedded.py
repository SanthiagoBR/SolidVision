"""The supervisor against a real cluster, with children that only pretend (RFC-036).

Marked `embedded`, like the adapter's own integration tests: run them with
`pytest -m embedded`; without `runtime\\postgres` they are skipped with the
command that materializes it. One `initdb` and one migration for the file, in
a template that every test copies, in its own temporary directory.

The children are a few lines of Python each: an HTTP server that answers
`/health` with 200, and the real `stop_on_stdin_eof()`. What is real is
everything else: the processes and their pipes, the cluster's start, adoption
and stop, the state file, and the lock.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.infrastructure.config.settings import settings
from app.infrastructure.database.embedded.cluster import (
    ClusterStatus,
    EmbeddedPostgres,
    PortProber,
)
from app.infrastructure.database.embedded.cluster_home import ClusterHome
from app.infrastructure.database.embedded.errors import RuntimeInvalidError
from app.infrastructure.database.embedded.runtime import PostgresRuntime
from app.infrastructure.database.embedded.windows_processes import WindowsProcesses
from app.launcher.children import ChildProcess, Program
from app.launcher.events import Event
from app.launcher.instance import (
    ClusterArrival,
    InstanceRecord,
    StateFile,
    StopRequests,
)
from app.launcher.supervisor import ApiPorts, Supervisor

pytestmark = [
    pytest.mark.embedded,
    pytest.mark.skipif(
        sys.platform != "win32",
        reason="The embedded cluster is Windows-only (RFC-033 section 4.1)",
    ),
]

BACKEND = Path(__file__).resolve().parents[2]
CREATE_NO_WINDOW = 0x08000000
SECONDS = 120.0

PRETENDING_CHILD = f"""
import http.server, json, sys, threading, time
sys.path.insert(0, {str(BACKEND)!r})
from app.infrastructure.process.supervision import stop_on_stdin_eof


class Health(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({{"status": "healthy", "search": "on_demand"}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *arguments):
        pass


try:
    stop_on_stdin_eof()
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
        server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Health)
        threading.Thread(target=server.serve_forever, daemon=True).start()
    print("up", flush=True)
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    print("stopped in order", flush=True)
"""

LAUNCHER_THAT_DIES = f"""
import json, sys
from pathlib import Path
sys.path.insert(0, {str(BACKEND)!r})
from app.infrastructure.database.embedded.cluster import EmbeddedPostgres, PortProber
from app.infrastructure.database.embedded.cluster_home import ClusterHome
from app.infrastructure.database.embedded.runtime import PostgresRuntime
from app.launcher.children import ChildProcess
from app.launcher.events import JsonLines
from app.launcher.instance import InstanceLock, StateFile, StopRequests
from app.launcher.supervisor import ApiPorts, Supervisor

runtime, home, data = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
child = sys.argv[4]
lock = InstanceLock(data / "launcher.lock")
assert lock.acquire()


def spawn(program, arguments):
    command = [sys.executable, "-X", "utf8", "-c", child, *arguments]
    return ChildProcess.start(program.name, command, echo=lambda line: None)


Supervisor(
    spawn=spawn,
    ports=ApiPorts(PortProber(), fixed=None, last=None),
    events=JsonLines(sys.stdout),
    state=StateFile(data / "launcher.json"),
    requests=StopRequests(),
    cluster=EmbeddedPostgres(PostgresRuntime(runtime), ClusterHome(home)),
).run()
"""


@dataclass
class Events:
    emitted: list[tuple[Event, dict[str, object]]]

    def emit(self, event: Event, **fields: object) -> None:
        self.emitted.append((event, fields))

    def names(self) -> list[str]:
        return [event.value for event, _ in self.emitted]


@pytest.fixture(scope="module")
def runtime() -> PostgresRuntime:
    runtime = PostgresRuntime(settings.embedded_postgres_runtime_directory)
    try:
        runtime.manifest()
    except RuntimeInvalidError as error:
        pytest.skip(str(error))
    return runtime


@pytest.fixture(scope="module")
def template(
    runtime: PostgresRuntime, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Path]:
    """Created, migrated and stopped once: a valid cluster to copy."""
    root = tmp_path_factory.mktemp("launcher-template") / "postgres"
    cluster = EmbeddedPostgres(runtime, ClusterHome(root))
    started = cluster.start()
    cluster.prepare(started.connection)
    cluster.stop()
    yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture()
def home(template: Path, tmp_path: Path) -> Iterator[Path]:
    root = tmp_path / "postgres"
    shutil.copytree(template, root)
    yield root
    # Whatever a failed test left up is stopped, never killed.
    EmbeddedPostgres(
        PostgresRuntime(settings.embedded_postgres_runtime_directory),
        ClusterHome(root),
    ).stop()


def spawn(program: Program, arguments: Sequence[str]) -> ChildProcess:
    command = [sys.executable, "-X", "utf8", "-c", PRETENDING_CHILD, *arguments]
    return ChildProcess.start(program.name, command, echo=lambda line: None)


def alive(pids: Sequence[int]) -> list[int]:
    table = WindowsProcesses()
    return [pid for pid in pids if table.is_alive(pid)]


def cluster_processes(runtime: PostgresRuntime, postmaster: int) -> list[int]:
    """The postmaster and its children, by identity (RFC-033 section 18).

    Not every `postgres.exe` of the runtime: the runtime is the checkout's,
    and a developer's own cluster may be up from it.
    """
    table = WindowsProcesses()
    return [
        process.pid
        for process in table.snapshot()
        if (process.pid == postmaster or process.parent_pid == postmaster)
        and (executable := table.executable(process.pid)) is not None
        and runtime.contains(executable)
    ]


def supervisor(
    runtime: PostgresRuntime,
    home: Path,
    data: Path,
    requests: StopRequests,
    events: Events,
    previous: InstanceRecord | None = None,
) -> Supervisor:
    return Supervisor(
        spawn=spawn,
        ports=ApiPorts(PortProber(), fixed=None, last=None),
        events=events,
        state=StateFile(data / "launcher.json"),
        requests=requests,
        cluster=EmbeddedPostgres(runtime, ClusterHome(home)),
        previous=previous,
    )


def test_a_start_and_an_ordered_stop_leave_no_process_behind(
    runtime: PostgresRuntime, home: Path, tmp_path: Path
) -> None:
    requests = StopRequests()
    events = Events([])
    machine = supervisor(runtime, home, tmp_path, requests, events)
    pids: list[int] = []

    def stop_once_ready() -> bool:
        if "ready" in events.names() and not pids:
            record = StateFile(tmp_path / "launcher.json").read()
            assert record is not None
            pids.extend(record.children.values())
            if record.postmaster_pid is not None:
                pids.append(record.postmaster_pid)
            return True
        return False

    requests.poll(stop_once_ready)
    started_at = time.monotonic()

    assert machine.run() == 0

    assert time.monotonic() - started_at < SECONDS
    assert events.names()[-3:] == ["ready", "stopping", "stopped"]
    api, job_runner, postmaster = pids
    assert alive([api, job_runner]) == []
    assert cluster_processes(runtime, postmaster) == []
    status = EmbeddedPostgres(runtime, ClusterHome(home)).status()
    assert status.status is ClusterStatus.STOPPED


def test_a_dead_launchers_cluster_is_adopted_and_inherited_by_the_next(
    runtime: PostgresRuntime, home: Path, tmp_path: Path
) -> None:
    """Section 7.4: the children stop by themselves when the launcher dies --
    their stdin ends -- and the cluster stays up, for the next launcher to
    adopt, inherit, and stop at the end."""
    data = tmp_path / "data"
    data.mkdir()
    dying = subprocess.Popen(
        [
            sys.executable,
            "-c",
            LAUNCHER_THAT_DIES,
            str(runtime.root),
            str(home),
            str(data),
            PRETENDING_CHILD,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env={**os.environ, "LOCALAPPDATA": str(tmp_path / "Local")},
        creationflags=CREATE_NO_WINDOW,
        text=True,
    )
    assert dying.stdout is not None
    deadline = time.monotonic() + SECONDS
    for line in dying.stdout:
        if json.loads(line)["event"] == "ready":
            break
        assert time.monotonic() < deadline
    record = StateFile(data / "launcher.json").read()
    assert record is not None and record.cluster is ClusterArrival.STARTED
    children = list(record.children.values())

    dying.kill()
    dying.wait(SECONDS)

    deadline = time.monotonic() + 10
    while alive(children) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert alive(children) == []
    cluster = EmbeddedPostgres(runtime, ClusterHome(home))
    assert cluster.status().status is ClusterStatus.READY

    left = StateFile(data / "launcher.json").read()
    requests = StopRequests()
    events = Events([])
    requests.poll(lambda: "ready" in events.names())
    nxt = supervisor(runtime, home, data, requests, events, previous=left)

    assert nxt.run() == 0

    assert cluster.status().status is ClusterStatus.STOPPED
