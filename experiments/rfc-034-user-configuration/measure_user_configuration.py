"""Configuration and user directories, measured (RFC-034 section 16).

The pilot beside this script read how the code behaved *before* RFC-034. This
measures what RFC-034 built: a staged installation, read-only, run from an
empty directory with a profile of its own; where it writes; `/health` with the
cluster up, stopped, and restarted on another port under a running API; what
`locate()` costs; every environment variable libpq reads, with a hostile
value; the user's file, written the way a Windows editor writes it; the log of
two programs, and of one program twice; the two modes side by side; and
whether the cluster's password leaks anywhere.

Everything lives under a temporary work directory, the staged installation
included. The real profile is only read: every child gets a `LOCALAPPDATA` of
its own, except the two checks whose question is the real one, and those
import nothing that writes. Nothing is left running: the last check looks for
the processes this run started, and the work directory is removed.

Run it with the project's virtualenv, from this script's directory, with the
output redirected next to it (`*.log` is outside git, like every experiment's
log):

    python measure_user_configuration.py > measure_user_configuration.log 2>&1

    --list             the checks, and which are opt-in
    --only a,b         just these
    --skip a,b         all the default ones but these
    --with suite,...   add opt-in checks (they take minutes)
    --work PATH        use this work directory, and what is already staged in it
    --keep             keep the work directory

The opt-in checks are the three that run something long: `suite`,
`docker-suite` and `adapter-baseline`. Two rows of section 16 are manual by
nature and only print what to do: `notepad-txt`, which needs Notepad's own
Save dialog, and `elevated`, which needs a UAC prompt.

**Every child has a deadline.** A write denied under a read-only tree can
show up as a process that never returns instead of as an error (section 2.1).

The children's `%TEMP%` is a directory of their own, empty at the start. The
real one is shared with every program on the machine, and a new entry in it
would not say whose it is.

No new dependency: `ctypes`, `urllib` and `subprocess`, the `psycopg` the
project already has, and `scripts/stage_installation.py`.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import ctypes
import datetime
import hashlib
import json
import os
import platform
import re
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
BACKEND = REPO / "backend"
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(REPO / "scripts"))

import stage_installation as staging  # noqa: E402

from app.infrastructure.config.layout import local_app_data  # noqa: E402
from app.infrastructure.config.settings import (  # noqa: E402
    RETIRED_KEYS,
    Settings,
)
from app.infrastructure.database.embedded.postmaster_pid import (  # noqa: E402
    read_postmaster_pid,
)
from app.infrastructure.database.embedded.windows_processes import (  # noqa: E402
    WindowsProcesses,
)

MAJOR = 16
LOCATE_CRITERION_SECONDS = 0.010
"""Declared before the measurement (section 16): a tenth of the connection it
precedes. Above it, the second branch applies: keep the answer, and read it
again only after a connection fails."""

RETRY_CANDIDATES = (1.0, 5.0, 30.0)
RETRY_BUDGET = 0.001
"""Declared before the measurement: `ROLLOVER_RETRY_SECONDS` is the smallest
candidate at which a rotation that cannot happen costs under 0.1 % of the
time between two attempts. Smaller is better past that point: it is how long
a file stays over its limit after the other process has gone."""

SETTING_NAMES = {name.upper() for name in Settings.model_fields} | set(RETIRED_KEYS)
INTERFERING = {
    "PYTHONDONTWRITEBYTECODE",
    "PYTHONPYCACHEPREFIX",
    "PYTHONHOME",
    "PYTHONSTARTUP",
    "PYTHONOPTIMIZE",
}
"""Variables of the developer's shell that would change what is measured."""

NAME = "Łódź/東京/✈.jpg"
"""A file name outside code page 1252, written and read through each
connection (section 7.5)."""

MODEL_RUNTIME = ("torchinductor_",)
"""What `torch` makes in `%TEMP%` when the job runner loads the model:
`torchinductor_<user>`, found by the first run of this script. Reported
apart, as the models' cache is (section 9): where the model's runtime writes
is RFC-035's to decide. Anything else in `%TEMP%` fails the check."""

PROCESSES = WindowsProcesses()
CLI = ["-m", "app.infrastructure.database.embedded"]


# --------------------------------------------------------------------------
# What the children run. Each prints one line, `RESULT <json>`.
# --------------------------------------------------------------------------

PROBE = """
import hashlib, json
import app
from app.infrastructure.config.settings import configuration_report, settings

dump = settings.model_dump(mode="json", exclude={"database_password"})
print("RESULT " + json.dumps({
    "app": app.__file__,
    "environment": str(settings.environment),
    "source": settings.database_source.value,
    "config_file": str(configuration_report.config_file),
    "config_file_exists": configuration_report.config_file_exists,
    "data_directory": str(configuration_report.data_directory),
    "directories": {name: str(path) for name, path in configuration_report.directories},
    "warnings": list(configuration_report.warnings),
    "debug": settings.debug,
    "batch_size": settings.batch_size,
    "thumbnail_max_edge": settings.thumbnail_max_edge,
    "log_level": settings.log_level,
    "thumbnail_directory": str(settings.thumbnail_directory),
    "fingerprint": hashlib.sha256(
        json.dumps(dump, sort_keys=True).encode()).hexdigest()[:16],
}))
"""

ENGINE_PROBE = """
import hashlib, json
from sqlalchemy import text
from app.infrastructure.config.settings import settings
from app.infrastructure.database.embedded.cluster_home import ClusterHome
from app.infrastructure.database.embedded.postmaster_pid import read_postmaster_pid
from app.infrastructure.persistence.engine import EngineInstance

home = ClusterHome(settings.embedded_postgres_directory)
pid_file = read_postmaster_pid(home.files(16).postmaster_pid)
with EngineInstance.connect() as connection:
    port, version, commits, rollbacks = connection.execute(text(
        "SELECT inet_server_port(), version(), xact_commit, xact_rollback "
        "FROM pg_stat_database WHERE datname = current_database()")).one()
dump = settings.model_dump(mode="json", exclude={"database_password"})
print("RESULT " + json.dumps({
    "environment": str(settings.environment),
    "source": settings.database_source.value,
    "debug": settings.debug,
    "batch_size": settings.batch_size,
    "log_level": settings.log_level,
    "home": str(settings.embedded_postgres_directory),
    "pid_file_port": pid_file.port if pid_file else None,
    "server_port": port,
    "version": version,
    "xact_commit": commits,
    "xact_rollback": rollbacks,
    "url": EngineInstance.url.render_as_string(hide_password=False),
    "fingerprint": hashlib.sha256(
        json.dumps(dump, sort_keys=True).encode()).hexdigest()[:16],
}))
"""

THUMBNAIL_PROBE = """
import json, uuid
from app.domain.value_objects.image_id import ImageId
from app.presentation.dependencies import get_thumbnail_store

store = get_thumbnail_store()
picture = b"\\xff\\xd8 a probe, not a picture \\xff\\xd9"
location = store.save(ImageId(uuid.uuid4()), picture)
print("RESULT " + json.dumps({
    "directory": str(store.directory),
    "location": location,
    "path": str(store.locate(location)),
}))
"""

LOCATE_PROBE = """
import json, sys, time
from app.infrastructure.config.settings import settings
from app.infrastructure.database.embedded.cluster import EmbeddedPostgres
from app.infrastructure.database.embedded.cluster_home import ClusterHome
from app.infrastructure.database.embedded.runtime import PostgresRuntime

cluster = EmbeddedPostgres(
    PostgresRuntime(settings.embedded_postgres_runtime_directory),
    ClusterHome(settings.embedded_postgres_directory),
)
samples = []
for _ in range(int(sys.argv[1])):
    began = time.perf_counter()
    found = cluster.locate()
    samples.append(time.perf_counter() - began)
print("RESULT " + json.dumps({"samples": samples, "port": found.port}))
"""

CONNECTION_PROBE = """
import json, sys, time
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool
from app.infrastructure.config.settings import settings
from app.infrastructure.database.embedded.cluster import EmbeddedPostgres
from app.infrastructure.database.embedded.cluster_home import ClusterHome
from app.infrastructure.database.embedded.database_setup import (
    CONNECT_TIMEOUT_SECONDS, LIBPQ_OVERRIDES)
from app.infrastructure.database.embedded.runtime import PostgresRuntime
from app.infrastructure.persistence.engine import build_engine

asking = build_engine(settings, poolclass=NullPool)
located = EmbeddedPostgres(
    PostgresRuntime(settings.embedded_postgres_runtime_directory),
    ClusterHome(settings.embedded_postgres_directory),
).locate()
told = create_engine(
    located.url(), poolclass=NullPool,
    connect_args={"connect_timeout": CONNECT_TIMEOUT_SECONDS, **LIBPQ_OVERRIDES})

def once(engine):
    began = time.perf_counter()
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    return time.perf_counter() - began

once(asking), once(told)  # the dialect's first-connection queries, not counted
with_listener, without = [], []
for _ in range(int(sys.argv[1])):
    with_listener.append(once(asking))
    without.append(once(told))
print("RESULT " + json.dumps({"with": with_listener, "without": without}))
"""

OBSERVE = """
FACTS = (
    "SELECT inet_server_port(), current_setting('client_encoding'), "
    "current_setting('TimeZone'), current_setting('DateStyle'), "
    "current_setting('application_name'), "
    "current_setting('default_transaction_read_only'), current_setting('geqo'), "
    "(SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid())"
)
KEYS = ("server_port", "client_encoding", "TimeZone", "DateStyle",
        "application_name", "read_only", "geqo", "ssl")

def describe(error):
    lines = [
        line.strip() for line in str(error).splitlines()
        if line.strip() and not line.strip().startswith("(Background on")
    ]
    return type(error).__name__ + ": " + (lines[-1] if lines else "")[:150]
"""

PG_BARE = (
    """
import json, sys
import psycopg
"""
    + OBSERVE
    + """
target = json.loads(sys.stdin.readline())
try:
    with psycopg.connect(
        host="127.0.0.1", port=target["port"], user="solidvision",
        password=target["password"], dbname="solidvision", connect_timeout=10,
        autocommit=True,
    ) as db:
        seen = dict(zip(KEYS, db.execute(FACTS).fetchone()))
        db.execute("INSERT INTO rfc034_pg_probe (name) VALUES (%s)", [target["name"]])
        back = db.execute(
            "DELETE FROM rfc034_pg_probe WHERE name = %s RETURNING name",
            [target["name"]]).fetchone()
        seen["round_trip"] = back is not None and back[0] == target["name"]
except Exception as error:
    seen = {"error": describe(error)}
print("RESULT " + json.dumps(seen))
"""
)

PG_ENGINE = (
    """
import json, sys
from sqlalchemy import text
from app.infrastructure.persistence.engine import EngineInstance
"""
    + OBSERVE
    + """
target = json.loads(sys.stdin.readline())
try:
    with EngineInstance.connect() as connection:
        seen = dict(zip(KEYS, connection.execute(text(FACTS)).one()))
        connection.execute(
            text("INSERT INTO rfc034_pg_probe (name) VALUES (:name)"),
            {"name": target["name"]})
        back = connection.execute(
            text("DELETE FROM rfc034_pg_probe WHERE name = :name RETURNING name"),
            {"name": target["name"]}).first()
        connection.commit()
        seen["round_trip"] = back is not None and back[0] == target["name"]
except Exception as error:
    seen = {"error": describe(error)}
print("RESULT " + json.dumps(seen))
"""
)

LEAK_PROBE = """
import json, os
from app.infrastructure.config.settings import configuration_report, settings

print(repr(settings))
print("\\n".join(configuration_report.lines()))
password = (settings.embedded_postgres_directory / "16" / "password").read_text(
    encoding="ascii").strip()
print("RESULT " + json.dumps({
    "in_environment": sum(password in value for value in os.environ.values()),
    "exported": sorted(name for name in os.environ if name in (
        "DATABASE_SOURCE", "EMBEDDED_POSTGRES_DIRECTORY",
        "EMBEDDED_POSTGRES_RUNTIME_DIRECTORY", "DATABASE_HOST", "DATABASE_PORT",
        "DATABASE_USER", "DATABASE_PASSWORD", "DATABASE_NAME")),
    "in_repr": password in repr(settings),
    "in_report": password in "\\n".join(configuration_report.lines()),
}))
"""

WRITER = """
import json, logging, sys, time
from logging.handlers import RotatingFileHandler

path, kind, tag = sys.argv[1], sys.argv[2], sys.argv[3]
count, pause = int(sys.argv[4]), float(sys.argv[5])
if kind == "standard":
    # RFC-005's handler, as it was before RFC-034: the control.
    handler = RotatingFileHandler(
        path, maxBytes=20_000, backupCount=5, encoding="utf-8")
else:
    from app.infrastructure.logging import handlers
    if kind == "lossless-every-record":
        handlers.ROLLOVER_RETRY_SECONDS = 0.0
    handler = handlers.LosslessRotatingFileHandler(
        path, maxBytes=20_000, backupCount=5, encoding="utf-8")
log = logging.getLogger("probe")
log.setLevel(logging.INFO)
log.propagate = False
log.addHandler(handler)
began = time.perf_counter()
for index in range(count):
    log.info("%s-%05d %s", tag, index, "x" * 80)
    if pause:
        time.sleep(pause)
print("RESULT " + json.dumps({"seconds": time.perf_counter() - began}))
"""

HOLDER = """
import sys, time
handle = open(sys.argv[1], "a", encoding="utf-8")
print("holding", flush=True)
time.sleep(600)
"""

ATTEMPT_PROBE = """
import json, logging, os, sys, time
from app.infrastructure.logging import handlers

path = sys.argv[1]
handler = handlers.LosslessRotatingFileHandler(
    path, maxBytes=1, backupCount=5, encoding="utf-8")
record = logging.LogRecord("probe", logging.INFO, "", 0, "x" * 100, None, None)
attempts = []
for _ in range(int(sys.argv[2])):
    began = time.perf_counter()
    handler.doRollover()
    attempts.append(time.perf_counter() - began)
rotated = os.path.exists(path + ".1")
plain = logging.FileHandler(path + ".plain", encoding="utf-8")
began = time.perf_counter()
for _ in range(2000):
    plain.emit(record)
per_record = (time.perf_counter() - began) / 2000
print("RESULT " + json.dumps(
    {"attempts": attempts, "rotated": rotated, "per_record": per_record}))
"""

IMPORT_STATEMENT = "import app.presentation.api"


# --------------------------------------------------------------------------
# Output: everything printed is also kept, so the last checks can search it.
# --------------------------------------------------------------------------


class Tee:
    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self.text: list[str] = []

    def write(self, data: str) -> int:
        self.text.append(data)
        return int(self._stream.write(data))

    def flush(self) -> None:
        self._stream.flush()


def say(text: str = "") -> None:
    print(f"    {text}" if text else "")
    sys.stdout.flush()


def seconds(value: float) -> str:
    return f"{value:.3f} s" if value < 10 else f"{value:.1f} s"


def millis(value: float) -> str:
    return f"{value * 1000:.2f} ms"


def spread(values: Sequence[float], unit: Callable[[float], str] = seconds) -> str:
    if not values:
        return "no samples"
    if len(values) == 1:
        return unit(values[0])
    return (
        f"median {unit(statistics.median(values))} "
        f"(min {unit(min(values))}, max {unit(max(values))}, n={len(values)})"
    )


def percentile(values: Sequence[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


def last_lines(text: str, count: int = 3) -> str:
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    return " / ".join(lines[-count:]) if lines else "(nothing)"


def size(count: float) -> str:
    return (
        f"{count / (1024 * 1024):.1f} MB"
        if count >= 1024 * 1024
        else f"{count / 1024:.1f} KB"
    )


class CheckFailedError(Exception):
    """A check's criterion was not met. The message is the result."""


@dataclass
class Service:
    """A long-running child: the API, or the job runner."""

    name: str
    process: subprocess.Popen[bytes]
    stdout: Path
    stderr: Path
    port: int | None = None

    def output(self) -> str:
        return "".join(
            path.read_text(encoding="utf-8", errors="replace")
            for path in (self.stdout, self.stderr)
            if path.is_file()
        )

    def alive(self) -> bool:
        return self.process.poll() is None

    def stop(self) -> None:
        if self.alive():
            self.process.terminate()
        try:
            self.process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.process.kill()


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def get_health(port: int, timeout: float = 120.0) -> tuple[int, Any, float]:
    """`GET /health`: the status, the body, and how long the answer took."""
    began = time.perf_counter()
    try:
        with OPENER.open(f"http://127.0.0.1:{port}/health", timeout=timeout) as answer:
            status, body = int(answer.status), json.load(answer)
    except urllib.error.HTTPError as error:
        status, body = int(error.code), json.load(error)
    return status, body, time.perf_counter() - began


def tree_state(root: Path) -> dict[str, tuple[int, int]]:
    """Every entry under `root`: a file's size and date, or a folder's presence."""
    state: dict[str, tuple[int, int]] = {}
    for directory, _, files in os.walk(root):
        relative = os.path.relpath(directory, root)
        state[relative + os.sep] = (-1, 0)
        for name in files:
            status = os.stat(os.path.join(directory, name))
            state[os.path.join(relative, name)] = (status.st_size, status.st_mtime_ns)
    return state


def tree_summary(root: Path) -> str:
    files = [path for path in root.rglob("*") if path.is_file()]
    total = sum(path.stat().st_size for path in files)
    return f"{len(files)} files, {size(total)}"


def count_lines(path: Path, needle: str) -> int:
    if not path.is_file():
        return 0
    text = path.read_text(encoding="utf-8", errors="replace")
    return sum(needle in line for line in text.splitlines())


def docker_status() -> str:
    if shutil.which("docker") is None:
        return "not installed"
    try:
        containers = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}} ({{.Image}})"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "installed, daemon not answering"
    if containers.returncode != 0:
        return "installed, NOT running"
    running = ", ".join(containers.stdout.split("\n")).strip(", ")
    return f"daemon running; {running or 'no container running'}"


def storage_of(path: Path) -> str:
    drive = path.resolve().drive.rstrip(":")
    command = (
        f"$d = Get-Partition -DriveLetter {drive} | Get-Disk; "
        "Get-PhysicalDisk | Where-Object DeviceId -eq $d.Number | "
        'ForEach-Object { "$($_.FriendlyName)|$($_.MediaType)|$($_.BusType)" }'
    )
    try:
        done = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return f"{drive}: medium unknown"
    found = done.stdout.strip().split("|")
    return (
        f"{drive}: on {found[0]} ({found[1]}, {found[2]})"
        if len(found) == 3
        else f"{drive}: medium unknown"
    )


# --------------------------------------------------------------------------
# The environment libpq reads (section 7.5).
# --------------------------------------------------------------------------

BEYOND_THE_OPTIONS = (
    "PGDATESTYLE",
    "PGTZ",
    "PGGEQO",
    "PGSERVICEFILE",
    "PGSYSCONFDIR",
    "PGLOCALEDIR",
    "PGREALM",
)
"""Variables libpq reads that are not the default of a connection option, so
`PQconndefaults` does not list them. The first three are sent to the server
with every connection, as parameters of the session."""

HOSTILE = {
    "PGSERVICE": ["no-such-service"],
    "PGUSER": ["somebody"],
    "PGPASSWORD": ["wrong"],
    "PGPASSFILE": [r"C:\no\such\pgpass.conf"],
    "PGDATABASE": ["postgres"],
    "PGHOST": ["192.0.2.1"],
    "PGHOSTADDR": ["192.0.2.1"],
    "PGPORT": ["1"],
    "PGCHANNELBINDING": ["require"],
    "PGCONNECT_TIMEOUT": ["1"],
    "PGCLIENTENCODING": ["LATIN1"],
    "PGOPTIONS": ["-c default_transaction_read_only=on"],
    "PGAPPNAME": ["somebody-elses-tool"],
    "PGSSLMODE": ["require"],
    "PGSSLNEGOTIATION": ["direct"],
    "PGSSLCOMPRESSION": ["1"],
    "PGSSLCERT": [r"C:\no\such\client.crt"],
    "PGSSLKEY": [r"C:\no\such\client.key"],
    "PGSSLCERTMODE": ["require"],
    "PGSSLROOTCERT": [r"C:\no\such\root.crt"],
    "PGSSLCRL": [r"C:\no\such\root.crl"],
    "PGSSLCRLDIR": [r"C:\no\such"],
    "PGSSLSNI": ["0"],
    "PGREQUIREPEER": ["nobody"],
    "PGREQUIREAUTH": ["md5"],
    "PGMINPROTOCOLVERSION": ["3.2"],
    "PGMAXPROTOCOLVERSION": ["banana"],
    "PGSSLMINPROTOCOLVERSION": ["TLSv1.3"],
    "PGSSLMAXPROTOCOLVERSION": ["TLSv1"],
    "PGGSSENCMODE": ["require"],
    "PGKRBSRVNAME": ["nobody"],
    "PGGSSLIB": ["sspi"],
    "PGGSSDELEGATION": ["1"],
    "PGTARGETSESSIONATTRS": ["standby"],
    "PGLOADBALANCEHOSTS": ["banana"],
    "PGDATESTYLE": ["banana", "SQL, DMY"],
    "PGTZ": ["Mars/Phobos", "Pacific/Kiritimati"],
    "PGGEQO": ["banana"],
    "PGSERVICEFILE": [r"C:\no\such\pg_service.conf"],
    "PGSYSCONFDIR": [r"C:\no\such"],
    "PGLOCALEDIR": [r"C:\no\such"],
    "PGREALM": ["NOWHERE"],
}
"""One value per variable that a connection to the cluster should not
survive unchanged, and for three of them a second, valid one: an invalid
value breaks the connection, and a valid one changes it in silence."""

HARMLESS_FACTS = {"application_name"}
"""A label in `pg_stat_activity`. Reported, and not counted against the engine."""


def libpq_variables() -> list[str]:
    """Every variable the libpq this process loaded reads, asked of libpq."""
    from psycopg import pq

    listed = [
        option.envvar.decode() for option in pq.Conninfo.get_defaults() if option.envvar
    ]
    return [*listed, *(name for name in BEYOND_THE_OPTIONS if name not in listed)]


# --------------------------------------------------------------------------
# The user's file (sections 2.4, 5.3 to 5.5 and 6.3).
# --------------------------------------------------------------------------

TWO_LINES = "LOG_LEVEL=DEBUG\nTHUMBNAIL_DIRECTORY=C:\\Users\\João\\thumbs\n"


def path_line(value: str) -> str:
    return f"THUMBNAIL_DIRECTORY={value}\n"


FILE_CASES: list[tuple[str, str, str, str, dict[str, Any]]] = [
    # (what, file name, encoding, text, what must hold)
    ("no file at all", "", "utf-8", "", {"config_file_exists": False, "warnings": 0}),
    (
        "keys with almost the right name",
        "solidvision.env",
        "utf-8",
        "EMBEDDING_BATCH_SIZE=64\nTHUMBNAIL_SIZE=256\n",
        {"batch_size": 8, "thumbnail_max_edge": 512, "warnings": 2},
    ),
    (
        "saved as UTF-8, no BOM",
        "solidvision.env",
        "utf-8",
        TWO_LINES,
        {"log_level": "DEBUG", "thumbnail_directory": r"C:\Users\João\thumbs"},
    ),
    (
        "saved as UTF-8 with BOM",
        "solidvision.env",
        "utf-8-sig",
        TWO_LINES,
        {"log_level": "DEBUG", "thumbnail_directory": r"C:\Users\João\thumbs"},
    ),
    (
        "saved as UTF-16 LE",
        "solidvision.env",
        "utf-16",
        TWO_LINES,
        {"refused": "not UTF-8"},
    ),
    (
        "saved as ANSI (1252)",
        "solidvision.env",
        "cp1252",
        TWO_LINES,
        {"refused": "not UTF-8"},
    ),
    (
        r"D:\fotos\raw\thumbs",
        "solidvision.env",
        "utf-8",
        path_line(r"D:\fotos\raw\thumbs"),
        {"thumbnail_directory": r"D:\fotos\raw\thumbs"},
    ),
    (
        r'"D:\fotos\raw\thumbs"',
        "solidvision.env",
        "utf-8",
        path_line(r'"D:\fotos\raw\thumbs"'),
        {"refused": "control character"},
    ),
    (
        r"'D:\fotos\raw\thumbs'",
        "solidvision.env",
        "utf-8",
        path_line(r"'D:\fotos\raw\thumbs'"),
        {"thumbnail_directory": r"D:\fotos\raw\thumbs"},
    ),
    (
        r"D:\Meus thumbs\novos",
        "solidvision.env",
        "utf-8",
        path_line(r"D:\Meus thumbs\novos"),
        {"thumbnail_directory": r"D:\Meus thumbs\novos"},
    ),
    (
        r'"D:\Meus thumbs\novos"',
        "solidvision.env",
        "utf-8",
        path_line(r'"D:\Meus thumbs\novos"'),
        {"refused": "control character"},
    ),
    (
        r"D:\thumbs # o disco grande",
        "solidvision.env",
        "utf-8",
        path_line(r"D:\thumbs # o disco grande"),
        {"thumbnail_directory": r"D:\thumbs"},
    ),
    (
        r"%LOCALAPPDATA%\thumbs",
        "solidvision.env",
        "utf-8",
        path_line(r"%LOCALAPPDATA%\thumbs"),
        {"refused": "${LOCALAPPDATA}"},
    ),
    (
        r"${LOCALAPPDATA}\thumbs",
        "solidvision.env",
        "utf-8",
        path_line(r"${LOCALAPPDATA}\thumbs"),
        {"thumbnail_directory": r"{profile}\thumbs"},
    ),
    (
        "thumbs",
        "solidvision.env",
        "utf-8",
        path_line("thumbs"),
        {"thumbnail_directory": r"{profile}\SolidVision\thumbs"},
    ),
    (
        "D:thumbs",
        "solidvision.env",
        "utf-8",
        path_line("D:thumbs"),
        {"refused": "names a drive and no root"},
    ),
    (
        r"\thumbs",
        "solidvision.env",
        "utf-8",
        path_line(r"\thumbs"),
        {"refused": "names no drive"},
    ),
    (
        "an empty value",
        "solidvision.env",
        "utf-8",
        "THUMBNAIL_DIRECTORY=\n",
        {"refused": "is empty"},
    ),
    (
        "two retired keys",
        "solidvision.env",
        "utf-8",
        "ENVIRONMENT=production\nINDEXING_ROOT_PATH=D:\\fotos\n",
        {"environment": "installed", "warnings": 2},
    ),
    (
        "the database keys, one of them invalid",
        "solidvision.env",
        "utf-8",
        "DATABASE_SOURCE=external\nDATABASE_HOST=db.example\nDATABASE_PORT=abc\n",
        {"source": "embedded", "warnings": 3},
    ),
    (
        "a value that does not validate",
        "solidvision.env",
        "utf-8",
        "BATCH_SIZE=abc\n",
        {"refused": "BATCH_SIZE"},
    ),
    (
        "the file saved as solidvision.env.txt",
        "solidvision.env.txt",
        "utf-8",
        "LOG_LEVEL=DEBUG\n",
        {"log_level": "INFO", "config_file_exists": False, "warnings": 1},
    ),
]


# --------------------------------------------------------------------------
# The measurement.
# --------------------------------------------------------------------------


class Measurement:
    def __init__(self, work: Path) -> None:
        self.work = work
        self.staged_root = work / "Program Files" / "SolidVision"
        self.profile = work / "profile" / "AppData" / "Local"
        self.installed_data = self.profile / "SolidVision"
        self.development_data = self.profile / "SolidVision-dev"
        self.empty = work / "empty"
        self.elsewhere = work / "somewhere else"
        self.temp = work / "temp"
        self.out = work / "out"
        self.verdict: dict[str, str] = {}
        self.captured: list[str] = []
        self.tee = Tee(sys.stdout)
        self.api: Service | None = None
        self.job_runner: Service | None = None
        self.services: list[Service] = []
        self.cluster_up = False
        self.before: dict[str, Any] | None = None
        self.suite_count: str | None = None
        for directory in (self.empty, self.elsewhere, self.temp, self.out):
            directory.mkdir(parents=True, exist_ok=True)

    # -- the staged installation, and the processes that run it --------------

    def staging(self, *, compiled: bool | None = None) -> Any:
        """The staged installation, read-only; made on first use.

        `compiled` unset takes the tree as it is, which matters to one check:
        the one that measures the import before the `.pyc` files exist.
        """
        root = self.staged_root
        if not root.exists():
            began = time.perf_counter()
            staging.stage(root, compile_bytecode=compiled is not False, read_only=True)
            say(
                f"staged {root} in {seconds(time.perf_counter() - began)}: "
                f"{tree_summary(root)}"
            )
        staged = staging.staged_at(root)
        if compiled is True and not staged.compiled:
            staging.unlock(root)
            staging.compile_tree(root)
            staging.lock(root)
        elif not staged.read_only:
            staging.lock(root)
        return staging.staged_at(root)

    def installed_env(
        self, profile: Path | None = None, **extra: str
    ) -> dict[str, str]:
        """A child that runs the copy. The caller's environment is passed on
        as it is: installed, it is not a source, and that is part of what is
        being measured."""
        base = {
            name: value
            for name, value in os.environ.items()
            if name.upper() not in INTERFERING
        }
        environment: dict[str, str] = self.staging().environment(
            profile or self.profile, base
        )
        environment.update(
            TEMP=str(self.temp), TMP=str(self.temp), PYTHONIOENCODING="utf-8"
        )
        environment.update(extra)
        return environment

    def checkout_env(self, **extra: str) -> dict[str, str]:
        """A child that runs the checkout, with the profile of this run.

        In development the environment is a source, so the variables that
        name a setting are taken out of the caller's first.
        """
        environment = {
            name: value
            for name, value in os.environ.items()
            if name.upper() not in SETTING_NAMES and name.upper() not in INTERFERING
        }
        environment.update(
            PYTHONPATH=str(BACKEND),
            LOCALAPPDATA=str(self.profile),
            TEMP=str(self.temp),
            TMP=str(self.temp),
            PYTHONIOENCODING="utf-8",
        )
        environment.update(extra)
        return environment

    def python(
        self,
        arguments: Sequence[str],
        *,
        env: dict[str, str],
        cwd: Path | None = None,
        timeout: float = 300.0,
        stdin: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """One child, to its end, within a deadline."""
        try:
            done = subprocess.run(
                [sys.executable, *arguments],
                cwd=cwd or self.empty,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                **(
                    {"stdin": subprocess.DEVNULL} if stdin is None else {"input": stdin}
                ),
            )
        except subprocess.TimeoutExpired:
            shown = " ".join(str(part) for part in arguments[:4])[:120]
            raise CheckFailedError(
                f"no answer in {timeout:.0f} s from `python {shown}`"
            ) from None
        self.captured.append(done.stdout + done.stderr)
        return done

    @staticmethod
    def answer(done: subprocess.CompletedProcess[str]) -> Any:
        for line in done.stdout.splitlines():
            if line.startswith("RESULT "):
                return json.loads(line.removeprefix("RESULT "))
        raise CheckFailedError(
            f"exit {done.returncode} and no result: {last_lines(done.stderr, 4)}"
        )

    def cli(
        self,
        action: str,
        *,
        env: dict[str, str],
        command: Sequence[str] = (),
        timeout: float = 900.0,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """`python -m app.infrastructure.database.embedded <action>`."""
        arguments = [*CLI, action, *(["--", *command] if command else [])]
        return self.python(arguments, env=env, timeout=timeout, cwd=cwd)

    def start_cluster(self, env: dict[str, str] | None = None) -> int:
        """Start the installed cluster, or adopt it; the port it is on."""
        done = self.cli("start", env=env or self.installed_env())
        ready = re.search(r"ready on [\d.]+:(\d+)", done.stderr)
        if done.returncode != 0 or ready is None:
            raise CheckFailedError(
                f"start exited {done.returncode}: {last_lines(done.stderr, 5)}"
            )
        if env is None:
            self.cluster_up = True
        return int(ready.group(1))

    def ensure_cluster(self) -> None:
        if not self.cluster_up:
            self.start_cluster()

    def stop_cluster(self) -> str:
        done = self.cli("stop", env=self.installed_env())
        self.cluster_up = False
        if done.returncode != 0:
            raise CheckFailedError(
                f"stop exited {done.returncode}: {last_lines(done.stderr, 5)}"
            )
        return last_lines(done.stderr, 1)

    def installed_files(self) -> Path:
        return self.installed_data / "postgres" / str(MAJOR)

    def installed_port(self) -> int:
        pid_file = read_postmaster_pid(
            self.installed_files() / "data" / "postmaster.pid"
        )
        if pid_file is None or pid_file.port is None:
            raise CheckFailedError("the installed cluster has no postmaster.pid")
        return pid_file.port

    def installed_password(self) -> str:
        return (self.installed_files() / "password").read_text(encoding="ascii").strip()

    def service(
        self, name: str, arguments: Sequence[str], env: dict[str, str]
    ) -> Service:
        stdout, stderr = self.out / f"{name}.stdout", self.out / f"{name}.stderr"
        with stdout.open("wb") as out, stderr.open("wb") as err:
            process = subprocess.Popen(
                [sys.executable, *arguments],
                cwd=self.empty,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
            )
        started = Service(name, process, stdout, stderr)
        self.services.append(started)
        return started

    def start_api(self, env: dict[str, str], name: str = "api") -> Service:
        port = free_port()
        api = self.service(
            name,
            [
                "-m",
                "uvicorn",
                "app.presentation.api:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            env,
        )
        api.port = port
        return api

    @staticmethod
    def wait_listening(started: Service, deadline: float = 600.0) -> None:
        """Until the server accepts a TCP connection, whatever it answers."""
        assert started.port is not None
        limit = time.monotonic() + deadline
        while True:
            if not started.alive():
                raise CheckFailedError(
                    f"{started.name} exited {started.process.returncode}: "
                    f"{last_lines(started.output(), 5)}"
                )
            try:
                socket.create_connection(("127.0.0.1", started.port), timeout=2).close()
                return
            except OSError:
                if time.monotonic() >= limit:
                    raise CheckFailedError(
                        f"{started.name} was not listening after {deadline:.0f} s"
                    ) from None
                time.sleep(0.1)

    def ensure_api(self) -> Service:
        self.ensure_cluster()
        if self.api is None or not self.api.alive():
            self.api = self.start_api(self.installed_env())
            self.wait_listening(self.api)
        return self.api

    def ensure_job_runner(self) -> Service:
        self.ensure_cluster()
        if self.job_runner is None or not self.job_runner.alive():
            self.job_runner = self.service(
                "job_runner",
                ["-m", "app.infrastructure.workers.job_runner"],
                self.installed_env(),
            )
            self._wait_for_line(
                self.job_runner,
                self.installed_data / "logs" / "job_runner.log",
                "Job executor started",
                deadline=900.0,
            )
        return self.job_runner

    @staticmethod
    def _wait_for_line(
        started: Service, log: Path, needle: str, deadline: float
    ) -> None:
        limit = time.monotonic() + deadline
        while not count_lines(log, needle):
            if not started.alive():
                raise CheckFailedError(
                    f"{started.name} exited {started.process.returncode}: "
                    f"{last_lines(started.output(), 5)}"
                )
            if time.monotonic() >= limit:
                raise CheckFailedError(
                    f"{started.name} did not log {needle!r} in {deadline:.0f} s"
                )
            time.sleep(0.2)

    def _fresh_profile(self) -> None:
        """A first run has to be the first: nothing of an earlier one is left."""
        for started in self.services:
            started.stop()
        self.api = self.job_runner = None
        if (self.installed_data / "postgres").exists():
            self.cli("stop", env=self.installed_env())
        if (self.development_data / "postgres").exists():
            self.cli("stop", env=self.checkout_env(DATABASE_SOURCE="embedded"))
        self.cluster_up = False
        for directory in (self.profile, self.temp, self.empty):
            shutil.rmtree(directory, ignore_errors=True)
            if directory.exists() and any(directory.iterdir()):
                raise CheckFailedError(f"{directory} could not be emptied")
            directory.mkdir(parents=True, exist_ok=True)

    def _watched(self) -> dict[str, Path]:
        return {
            name: Path(os.environ[name])
            for name in ("USERPROFILE", "APPDATA")
            if os.environ.get(name)
        }

    def _snapshot(self) -> dict[str, Any]:
        return {
            "tree": tree_state(self.staged_root),
            "first_level": {
                name: set(os.listdir(path)) for name, path in self._watched().items()
            },
        }

    # -- the first installed run, and where it writes (section 9) ------------

    def first_run(self) -> str:
        staged = self.staging(compiled=True)
        self._fresh_profile()
        self.before = self._snapshot()
        env = self.installed_env()
        say(f"installed tree:    {staged.root} (read-only, compiled)")
        say(f"working directory: {self.empty} (empty)")
        say(f"LOCALAPPDATA:      {self.profile} (empty)")

        began = time.perf_counter()
        port = self.start_cluster()
        start_seconds = time.perf_counter() - began
        for line in self.captured[-1].splitlines():
            if line.startswith("embedded postgres:"):
                say(f"  {line}")
        say(f"`start`, from nothing to a migrated database: {seconds(start_seconds)}")

        seen = self.answer(self.python(["-c", PROBE], env=env))
        under = Path(seen["app"]).resolve().is_relative_to(staged.root.resolve())
        say(f"app.__file__:      {seen['app']}")
        say(
            f"  under the staged tree: {under}; settings.environment: "
            f"{seen['environment']}"
        )
        say(
            f"  configuration file: {seen['config_file']} (exists: "
            f"{seen['config_file_exists']})"
        )
        if not under or seen["environment"] != "installed":
            raise CheckFailedError("the child did not run the staged installation")

        began = time.perf_counter()
        self.api = self.start_api(env)
        self.wait_listening(self.api)
        assert self.api.port is not None
        status, body, _ = get_health(self.api.port)
        api_seconds = time.perf_counter() - began
        say(
            f"the API, from spawn to its first /health: {seconds(api_seconds)} -> "
            f"{status} {body}"
        )
        if status != 200:
            raise CheckFailedError(f"the first /health answered {status}")

        began = time.perf_counter()
        self.ensure_job_runner()
        runner_seconds = time.perf_counter() - began
        say(
            f"the job runner, from spawn to polling (it loads the embedding model "
            f"first): {seconds(runner_seconds)}"
        )
        return (
            f"start {seconds(start_seconds)} (cluster created, port {port}); the API "
            f"answering in {seconds(api_seconds)}; the job runner polling in "
            f"{seconds(runner_seconds)}. app.__file__ under the staged tree, "
            f"environment `installed`"
        )

    def thumbnail(self) -> str:
        self.staging()
        done = self.python(
            ["-c", THUMBNAIL_PROBE], env=self.installed_env(), timeout=900.0
        )
        seen = self.answer(done)
        path = Path(seen["path"])
        expected = self.installed_data / "thumbnails"
        say(f"get_thumbnail_store().directory: {seen['directory']}")
        say(f"saved: {seen['location']} -> {path} ({path.stat().st_size} bytes)")
        if not path.is_file() or not path.resolve().is_relative_to(expected.resolve()):
            raise CheckFailedError(f"the thumbnail is not under {expected}")
        return f"written under <data>\\thumbnails, as {seen['location']}"

    def writes_everything(self) -> str:
        if self.before is None:
            raise CheckFailedError("this compares with `first-run`: run them together")
        after = self._snapshot()
        say(f"created under {self.profile}:")
        outside_the_root = []
        for entry in sorted(self.profile.iterdir()):
            say(f"  {entry.name}\\")
            if entry.name != "SolidVision":
                outside_the_root.append(entry.name)
                continue
            for child in sorted(entry.iterdir()):
                detail = (
                    tree_summary(child)
                    if child.is_dir()
                    else size(child.stat().st_size)
                )
                say(f"    {child.name:<16} {detail}")
        logs = sorted(path.name for path in (self.installed_data / "logs").iterdir())
        say(f"  logs: {', '.join(logs)}")

        before_tree, after_tree = self.before["tree"], after["tree"]
        added = sorted(set(after_tree) - set(before_tree))
        removed = sorted(set(before_tree) - set(after_tree))
        changed = sorted(
            name
            for name in set(before_tree) & set(after_tree)
            if before_tree[name] != after_tree[name]
        )
        working = sorted(os.listdir(self.empty))
        temporary = sorted(os.listdir(self.temp))
        say(f"the working directory: {working or 'still empty'}")
        say(
            f"the installed tree, {len(before_tree)} entries: {len(added)} added, "
            f"{len(removed)} removed, {len(changed)} with another size or date"
        )
        findings = []
        for name, path in self._watched().items():
            new = sorted(set(os.listdir(path)) - self.before["first_level"][name])
            say(f"first level of %{name}% ({path}): {new or 'nothing new'}")
            if new:
                findings.append(f"%{name}%: {new}")
        say(f"the children's own %TEMP%: {temporary or 'empty'}")
        models = [name for name in temporary if name.startswith(MODEL_RUNTIME)]
        for name in models:
            entry = self.temp / name
            inside = (
                tree_summary(entry) if entry.is_dir() else size(entry.stat().st_size)
            )
            say(
                f"  {name}: {inside}, written by the model's runtime when the job "
                f"runner loads it -- outside the `all` of section 9, with the "
                f"models' cache (RFC-035)"
            )
        if working:
            findings.append(f"working directory: {working}")
        if added or removed or changed:
            findings.append(f"installed tree: {(added + removed + changed)[:5]}")
        if outside_the_root:
            findings.append(f"beside the data root: {outside_the_root}")
        if set(temporary) - set(models):
            findings.append(f"%TEMP%: {sorted(set(temporary) - set(models))[:8]}")
        if findings:
            raise CheckFailedError(
                "written outside the data root -- " + "; ".join(findings)
            )
        created = ", ".join(
            f"{child.name}\\" if child.is_dir() else child.name
            for child in sorted(self.installed_data.iterdir())
        )
        temporary_note = (
            f"in the children's %TEMP%, only the model runtime's {', '.join(models)}"
            if models
            else "the children's %TEMP% empty"
        )
        return (
            f"created: SolidVision\\ with {created}. Outside it: the working "
            f"directory still empty; the installed tree with the same "
            f"{len(before_tree)} entries and dates; nothing new at the first level "
            f"of %USERPROFILE% and %APPDATA%; {temporary_note}"
        )

    def real_root(self) -> str:
        """The one read of the real profile: the paths, and nothing written."""
        real = local_app_data()
        expected = real / "SolidVision"

        def listing() -> dict[str, tuple[int, int]]:
            found = {}
            for folder in (expected, expected / "logs"):
                if folder.is_dir():
                    for entry in folder.iterdir():
                        status = entry.stat()
                        found[str(entry)] = (status.st_size, status.st_mtime_ns)
            return found

        before = listing()
        seen = self.answer(
            self.python(["-c", PROBE], env=self.installed_env(profile=real))
        )
        after = listing()
        say(f"data directory:  {seen['data_directory']}")
        for name, path in seen["directories"].items():
            say(f"  {name:<38} {path}")
        say(f"configuration file: {seen['config_file']}")
        say(
            f"{expected}, before and after: "
            f"{'unchanged' if before == after else 'CHANGED'}"
        )
        user = ("thumbnail_directory", "log_directory", "embedded_postgres_directory")
        inside = all(
            Path(seen["directories"][name]).is_relative_to(expected) for name in user
        )
        if Path(seen["data_directory"]) != expected or not inside or before != after:
            raise CheckFailedError("the real root is not %LOCALAPPDATA%\\SolidVision")
        return (
            f"{seen['data_directory']}, the three user directories under it; "
            "read, not written"
        )

    # -- /health (sections 7.3 to 7.5) ---------------------------------------

    def health_installed(self) -> str:
        api = self.ensure_api()
        assert api.port is not None
        times = []
        for _ in range(20):
            status, body, elapsed = get_health(api.port)
            times.append(elapsed)
        say(f"GET /health: {status} {body}")
        say(f"  20 requests: {spread(times, millis)}")
        if status != 200 or body.get("environment") != "installed":
            raise CheckFailedError(f"{status} {body}")
        return f'200, "environment": "installed"; {spread(times, millis)}'

    def health_cluster_stopped(self) -> str:
        api = self.ensure_api()
        assert api.port is not None
        get_health(api.port)  # the pool now holds a connection to the old server
        say(f"`stop`: {self.stop_cluster()}")
        answers = [get_health(api.port) for _ in range(6)]
        for status, body, elapsed in answers:
            say(f"GET /health: {status} in {millis(elapsed)} {body}")
        if any(status != 503 for status, _, _ in answers):
            raise CheckFailedError("a /health with the cluster stopped did not say 503")
        later = [elapsed for _, _, elapsed in answers[1:]]
        return (
            f"503, first in {millis(answers[0][2])} (it finds its pooled connection "
            f"dead), then {spread(later, millis)}"
        )

    def restart_other_port(self) -> str:
        api = self.ensure_api()
        assert api.port is not None
        self.ensure_cluster()
        status, _, _ = get_health(api.port)
        last = self.installed_port()
        api_pid = api.process.pid
        say(
            f"the API (PID {api_pid}) is up, /health {status}; the cluster is on port "
            f"{last}"
        )
        self.stop_cluster()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as squatter:
            limit = time.monotonic() + 10
            while True:
                try:
                    squatter.bind(("127.0.0.1", last))
                    break
                except OSError:
                    if time.monotonic() >= limit:
                        raise
                    time.sleep(0.1)
            squatter.listen()
            say(f"`stop`; port {last} is now held by a socket of this script")
            port = self.start_cluster()
            say(f"`start`: the cluster is on port {port}")
            status, body, elapsed = get_health(api.port)
            say(
                f"GET /health, the same API process: {status} in {millis(elapsed)} "
                f"{body}"
            )
        same = api.alive() and api.process.pid == api_pid
        if port == last or status != 200 or not same:
            raise CheckFailedError(
                f"port {last} -> {port}, /health {status}, same API process: {same}"
            )
        return (
            f"works without restarting the API: the cluster moved from port {last} to "
            f"{port}, and the next /health answered 200 in {millis(elapsed)}"
        )

    def health_reinitializing(self) -> str:
        """Section 13: `ready` in the file while the server reinitializes.

        A backend killed by force makes the postmaster end every other one
        and reinitialize; for that time `postmaster.pid` still says `ready`,
        `locate()` answers, and the server refuses the connection (RFC-033
        section 8.4). How long `/health` says 503 is the question.
        """
        api = self.ensure_api()
        assert api.port is not None
        status, _, _ = get_health(api.port)
        port, password = self.installed_port(), self.installed_password()
        with psycopg.connect(
            host="127.0.0.1",
            port=port,
            user="solidvision",
            password=password,
            dbname="solidvision",
            connect_timeout=10,
            options="",
            sslmode="disable",
            gssencmode="disable",
            autocommit=True,
        ) as db:
            row = db.execute("SELECT pg_backend_pid()").fetchone()
            assert row is not None
            backend = int(row[0])
            killed = time.perf_counter()
            PROCESSES.terminate(backend)
        answers: list[tuple[float, int]] = []
        deadline = killed + 120
        while time.perf_counter() < deadline:
            status, _, _ = get_health(api.port, timeout=30)
            answers.append((time.perf_counter() - killed, status))
            if status == 200 and any(code != 200 for _, code in answers):
                break
            time.sleep(0.1)
        refused = [moment for moment, code in answers if code != 200]
        last = seconds(refused[-1]) if refused else "-"
        again = seconds(answers[-1][0])
        say(f"/health before: 200; a backend (PID {backend}) ended by force")
        say(f"  {len(refused)} answers other than 200, the last at {last}")
        say(f"  the first 200 again at {again}, from the same API process")
        if not refused or answers[-1][1] != 200 or not api.alive():
            raise CheckFailedError(f"answers: {answers[:5]} ... {answers[-3:]}")
        codes = ", ".join(
            map(str, sorted({code for _, code in answers if code != 200}))
        )
        return (
            f"{codes} for {last} after a backend was killed ({len(refused)} "
            f"requests), then 200 at {again}, with nothing restarted"
        )

    def health_external_down(self) -> str:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            closed = int(probe.getsockname()[1])
        env = self.checkout_env(
            DATABASE_SOURCE="external",
            DATABASE_HOST="127.0.0.1",
            DATABASE_PORT=str(closed),
            WARM_UP_MODELS="false",
            LOG_DIRECTORY=str(self.development_data / "logs"),
        )
        api = self.start_api(env, name="api-external")
        try:
            self.wait_listening(api)
            assert api.port is not None
            answers = [get_health(api.port, timeout=300.0) for _ in range(3)]
        finally:
            api.stop()
        say(
            f"the checkout's API, source `external`, 127.0.0.1:{closed} where nobody "
            "listens"
        )
        for status, body, elapsed in answers:
            say(f"GET /health: {status} in {seconds(elapsed)} {body}")
        if any(status != 503 for status, _, _ in answers):
            raise CheckFailedError("a /health with the database down did not say 503")
        return f"503 in {spread([elapsed for _, _, elapsed in answers])}"

    # -- what locate() costs (section 7.3) -----------------------------------

    def locate_cost(self) -> str:
        self.ensure_cluster()
        first, warm = [], []
        for _ in range(3):
            seen = self.answer(
                self.python(["-c", LOCATE_PROBE, "301"], env=self.installed_env())
            )
            first.append(seen["samples"][0])
            warm.extend(seen["samples"][1:])
        median = statistics.median(warm)
        say(f"locate(), cluster up, {len(warm)} calls in 3 processes:")
        say(f"  {spread(warm, millis)}; p95 {millis(percentile(warm, 0.95))}")
        say(f"  the first call of each process: {spread(first, millis)}")
        say(f"  criterion: under {millis(LOCATE_CRITERION_SECONDS)} in the median")
        if median >= LOCATE_CRITERION_SECONDS:
            raise CheckFailedError(
                f"median {millis(median)}: the second branch of section 16 applies"
            )
        return (
            f"median {millis(median)}, p95 {millis(percentile(warm, 0.95))}, "
            f"n={len(warm)}; "
            f"first call of a process {millis(statistics.median(first))}; under the "
            f"{millis(LOCATE_CRITERION_SECONDS)} criterion"
        )

    def new_connection(self) -> str:
        self.ensure_cluster()
        seen = self.answer(
            self.python(["-c", CONNECTION_PROBE, "40"], env=self.installed_env())
        )
        asking, told = seen["with"], seen["without"]
        difference = statistics.median(asking) - statistics.median(told)
        say(
            "a new physical connection and one SELECT, NullPool, 40 times each, "
            "alternated:"
        )
        say(
            "  the project's engine, the listener asking locate(): "
            f"{spread(asking, millis)}"
        )
        say(
            "  the same parameters with the port and password given: "
            f"{spread(told, millis)}"
        )
        say(f"  difference of the medians: {millis(difference)}")
        return (
            f"{millis(statistics.median(asking))} with the listener and "
            f"{millis(statistics.median(told))} without: {millis(difference)} more"
        )

    # -- the environment (sections 2.3, 5.2 and 7.5) -------------------------

    def hostile_environment(self) -> str:
        self.ensure_cluster()
        hostile = {
            "DEBUG": "*",
            "BATCH_SIZE": "auto",
            "LOG_LEVEL": "ERROR",
            "ENVIRONMENT": " Production ",
            "DATABASE_SOURCE": "external",
            "DATABASE_HOST": "somebody-elses-postgres",
            "DATABASE_PORT": "5432",
            "THUMBNAIL_DIRECTORY": "thumbs",
        }
        clean = self.answer(self.python(["-c", ENGINE_PROBE], env=self.installed_env()))
        done = self.python(["-c", ENGINE_PROBE], env=self.installed_env(**hostile))
        seen = self.answer(done)
        say(
            f"in the environment: {', '.join(f'{k}={v!r}' for k, v in hostile.items())}"
        )
        for label, one in (("without them", clean), ("with them   ", seen)):
            say(
                f"  installed, {label}: exit 0; debug={one['debug']}, batch_size="
                f"{one['batch_size']}, log_level={one['log_level']!r}, environment="
                f"{one['environment']!r}, source={one['source']!r}, engine reached "
                f"port {one['server_port']}; settings {one['fingerprint']}"
            )
        control = self.python(
            ["-c", "from app.infrastructure.config.settings import settings"],
            env=self.checkout_env(**hostile),
        )
        say(
            f"  the control, the same variables in a checkout: exit "
            f"{control.returncode}: {last_lines(control.stderr, 1)}"
        )
        same = (
            seen["fingerprint"] == clean["fingerprint"]
            and seen["server_port"] == clean["server_port"] == self.installed_port()
        )
        if not same or control.returncode == 0:
            raise CheckFailedError("the environment changed an installed process")
        return (
            "starts the same: every setting equal with and without the variables, "
            "and the engine reaches the cluster's port. The same variables stop a "
            "checkout from importing, in one line that names the key"
        )

    def pg_variables(self) -> str:
        from psycopg import pq

        self.ensure_cluster()
        port, password = self.installed_port(), self.installed_password()
        with psycopg.connect(
            host="127.0.0.1",
            port=port,
            user="solidvision",
            password=password,
            dbname="solidvision",
            connect_timeout=10,
            options="",
            sslmode="disable",
            gssencmode="disable",
            autocommit=True,
        ) as db:
            db.execute("CREATE TABLE IF NOT EXISTS rfc034_pg_probe (name text)")
        target = json.dumps({"port": port, "password": password, "name": NAME}) + "\n"
        variables = libpq_variables()
        say(f"libpq {pq.version()} ({pq.__impl__}) reads {len(variables)} variables")
        cases = [("", "")] + [
            (name, value)
            for name in variables
            for value in HOSTILE.get(name, ["rfc034-hostile"])
        ]

        def probe(case: tuple[str, str], mode: str) -> dict[str, Any]:
            name, value = case
            extra = {name: value} if name else {}
            # The children are started with every PG* of the caller taken out,
            # so that one variable at a time is what is measured.
            env = {
                key: text
                for key, text in self.installed_env().items()
                if not key.upper().startswith("PG")
            }
            env.update(extra)
            try:
                if mode == "adapter":
                    # The adapter's own connection: a `start` that finds the
                    # cluster up logs in to adopt it (RFC-033 section 8.4).
                    done = self.cli("start", env=env, timeout=180.0)
                    if done.returncode == 0:
                        return {"adopted": True}
                    return {"error": last_lines(done.stderr, 1)[:170]}
                code = PG_BARE if mode == "bare" else PG_ENGINE
                done = self.python(["-c", code], env=env, stdin=target, timeout=180.0)
                return dict(self.answer(done))
            except CheckFailedError as error:
                return {"error": str(error)[:150]}

        modes = ("bare", "adapter", "engine")
        results: dict[tuple[str, str, str], dict[str, Any]] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = {
                pool.submit(probe, case, mode): (*case, mode)
                for case in cases
                for mode in modes
            }
            for future in concurrent.futures.as_completed(futures):
                results[futures[future]] = future.result()

        def outcome(seen: dict[str, Any], control: dict[str, Any]) -> tuple[str, bool]:
            if "error" in seen:
                return f"BREAKS -- {seen['error']}", True
            changes = [
                f"{key} {control.get(key)!r} -> {seen.get(key)!r}"
                for key in control
                if seen.get(key) != control.get(key)
            ]
            if not changes:
                return "no effect", False
            counted = any(
                change.split(" ", 1)[0] not in HARMLESS_FACTS for change in changes
            )
            return "changes " + "; ".join(changes), counted

        controls = {mode: results[("", "", mode)] for mode in modes}
        for mode, control in controls.items():
            say(f"control, no PG* variable, {mode}: {control}")
            if "error" in control or control.get("round_trip") is False:
                raise CheckFailedError(f"the {mode} control did not connect")
        labels = {
            "bare": "bare",
            "adapter": "the adapter",
            "engine": "the engine",
        }
        counted: dict[str, list[str]] = {mode: [] for mode in modes}
        for name, value in cases[1:]:
            say(f"{name}={value}")
            for mode in modes:
                text, counts = outcome(results[(name, value, mode)], controls[mode])
                say(f"    {labels[mode]:<12} {text}")
                if counts and name not in counted[mode]:
                    counted[mode].append(name)
        say(
            f"change or break a connection given only host, port, user, password "
            f"and database: {len(counted['bare'])} of {len(variables)} -- "
            f"{', '.join(counted['bare'])}"
        )
        for mode in ("adapter", "engine"):
            say(
                f"change or break {labels[mode]}: {len(counted[mode])} -- "
                f"{', '.join(counted[mode]) or 'none'}"
            )
        reached = sorted(set(counted["adapter"]) | set(counted["engine"]))
        if reached:
            raise CheckFailedError(
                f"still reach the adapter or the engine: {', '.join(reached)}"
            )
        return (
            f"of the {len(variables)} variables libpq {pq.version()} reads, "
            f"{len(counted['bare'])} change or break a bare connection "
            f"({', '.join(counted['bare'])}); none reaches the adapter's login or "
            f"the project's engine"
        )

    def user_file(self) -> str:
        self.staging()
        mismatches = []

        def one(index: int) -> tuple[str, dict[str, Any], str]:
            what, name, encoding, text, _ = FILE_CASES[index]
            profile = self.work / "files" / str(index) / "AppData" / "Local"
            shutil.rmtree(profile, ignore_errors=True)
            (profile / "SolidVision").mkdir(parents=True)
            if name:
                (profile / "SolidVision" / name).write_bytes(text.encode(encoding))
            done = self.python(["-c", PROBE], env=self.installed_env(profile=profile))
            if done.returncode != 0:
                return what, {"refused": last_lines(done.stderr, 1)}, str(profile)
            return what, dict(self.answer(done)), str(profile)

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            outcomes = list(pool.map(one, range(len(FILE_CASES))))
        for (what, seen, profile), case in zip(outcomes, FILE_CASES, strict=True):
            expected = case[4]
            say(f"{what}")
            if "refused" in seen:
                say(
                    "    refused, exit 1: "
                    f"{seen['refused'].replace(profile, '<profile>')}"
                )
            else:
                thumbnails = seen["thumbnail_directory"].replace(profile, "<profile>")
                say(
                    f"    read: log_level={seen['log_level']}, batch_size="
                    f"{seen['batch_size']}, thumbnail_directory={thumbnails}"
                )
                for warning in seen["warnings"]:
                    say(f"    warning: {warning.replace(profile, '<profile>')}")
            for key, wanted in expected.items():
                if key == "refused":
                    found = wanted in seen.get("refused", "")
                elif "refused" in seen:
                    found = False
                elif key == "warnings":
                    found = len(seen["warnings"]) == wanted
                elif key == "thumbnail_directory":
                    found = seen[key] == wanted.replace("{profile}", profile)
                else:
                    found = seen[key] == wanted
                if not found:
                    mismatches.append(f"{what}: {key} is not {wanted!r}")
        if mismatches:
            raise CheckFailedError("; ".join(mismatches))
        refused = sum("refused" in seen for _, seen, _ in outcomes)
        return (
            f"{len(FILE_CASES)} cases, each as sections 5 and 6.3 say: "
            f"{len(FILE_CASES) - refused} read, {refused} refused in one line that "
            f"names the file and the key; a BOM no longer drops the first key"
        )

    def notepad_txt(self) -> str:
        say("MANUAL. Notepad's own Save dialog cannot be driven from here.")
        say("  1. Open Notepad, type LOG_LEVEL=DEBUG, and choose Save As.")
        say("  2. In %LOCALAPPDATA%\\SolidVision, type the name solidvision.env and")
        say("     leave `Save as type` as it is. Save.")
        say("  3. `dir %LOCALAPPDATA%\\SolidVision` : is the file solidvision.env.txt?")
        say("The warning for that file is exercised by `user-file`, without Notepad.")
        return "manual: not run"

    def no_local_app_data(self) -> str:
        known = local_app_data({})
        results = {}
        for label, env in (
            ("installed", self.installed_env()),
            ("checkout", self.checkout_env()),
        ):
            env.pop("LOCALAPPDATA", None)
            seen = self.answer(self.python(["-c", PROBE], env=env))
            results[label] = Path(seen["data_directory"])
            say(f"{label}, a child without LOCALAPPDATA: {seen['data_directory']}")
        say(f"FOLDERID_LocalAppData, asked of Windows here: {known}")
        if results != {
            "installed": known / "SolidVision",
            "checkout": known / "SolidVision-dev",
        }:
            raise CheckFailedError("a child without the variable found another root")
        return f"the same root, {known}, asked of Windows; nothing was written there"

    def two_working_directories(self) -> str:
        answers = {}
        for label, env in (
            ("installed", self.installed_env()),
            ("checkout", self.checkout_env()),
        ):
            seen = [
                self.answer(self.python(["-c", PROBE], env=env, cwd=cwd))
                for cwd in (self.empty, self.elsewhere)
            ]
            keys = ("data_directory", "config_file", "directories")
            answers[label] = [{key: one[key] for key in keys} for one in seen]
            say(f"{label}, from {self.empty.name}\\ and from {self.elsewhere.name}\\:")
            for name, path in seen[0]["directories"].items():
                say(f"  {name:<38} {path}")
            say(f"  the same from both: {answers[label][0] == answers[label][1]}")
        if any(first != second for first, second in answers.values()):
            raise CheckFailedError("the working directory changed a path")
        return "equal, in both modes: the four directories, the data root and the file"

    # -- the bytecode (section 9) --------------------------------------------

    def _import_times(self, label: str) -> tuple[list[float], list[float], int]:
        """Wall time of a child that imports the API, and the project's own share."""
        profile = self.work / "pyc-profile" / "AppData" / "Local"
        env = self.installed_env(profile=profile)
        walls, own = [], []
        modules = 0
        for index in range(6):
            began = time.perf_counter()
            done = self.python(
                ["-X", "importtime", "-c", IMPORT_STATEMENT], env=env, timeout=900.0
            )
            wall = time.perf_counter() - began
            if done.returncode != 0:
                raise CheckFailedError(
                    f"the import failed: {last_lines(done.stderr, 3)}"
                )
            mine = 0
            modules = 0
            for line in done.stderr.splitlines():
                match = re.match(r"import time:\s+(\d+) \|\s+\d+ \|\s+(\S+)", line)
                if match and (
                    match.group(2) == "app" or match.group(2).startswith("app.")
                ):
                    mine += int(match.group(1))
                    modules += 1
            if index:  # the first run of each state warms the disk cache
                walls.append(wall)
                own.append(mine / 1_000_000)
        say(f"{label}:")
        say(f"  the whole child:                  {spread(walls)}")
        say(f"  the project's {modules} modules, own time: {spread(own)}")
        return walls, own, modules

    def bytecode(self) -> str:
        root = self.staged_root
        staged = self.staging(compiled=False)
        if staged.compiled:
            staging.unlock(root)
            staging.discard_bytecode(root)
            staging.lock(root)
        say(f"`{IMPORT_STATEMENT}`, in the read-only tree, 5 times after one warm-up")
        _, without, _ = self._import_times("no .pyc in the tree")
        written = len(list((root / "backend").rglob("__pycache__")))
        say(f"  __pycache__ folders Python managed to write: {written}")
        staging.unlock(root)
        staging.compile_tree(root)
        staging.lock(root)
        walls, compiled, modules = self._import_times("compiled before the lock")
        if written:
            raise CheckFailedError("Python wrote bytecode into the locked tree")
        saved = statistics.median(without) - statistics.median(compiled)
        return (
            f"the project's {modules} modules take "
            f"{seconds(statistics.median(without))} "
            f"to import without .pyc and {seconds(statistics.median(compiled))} with: "
            f"{seconds(saved)} recompiled on every start; the whole import is "
            f"{seconds(statistics.median(walls))}"
        )

    # -- the log (section 8) -------------------------------------------------

    def log_two_programs(self) -> str:
        api = self.ensure_api()
        runner = self.ensure_job_runner()
        assert api.port is not None
        logs = self.installed_data / "logs"
        api_log, runner_log = logs / "uvicorn.log", logs / "job_runner.log"
        requests = 300
        asked = count_lines(api_log, "Health check requested")
        answered = count_lines(api_log, "Health check succeeded")
        for _ in range(requests):
            get_health(api.port)
        asked = count_lines(api_log, "Health check requested") - asked
        answered = count_lines(api_log, "Health check succeeded") - answered
        runner_lines = len(runner_log.read_text(encoding="utf-8").splitlines())
        crossed = count_lines(runner_log, "Health check") + count_lines(
            api_log, "Job executor"
        )
        errors = (api.output() + runner.output()).count("--- Logging error ---")
        say(f"files in {logs}: {', '.join(sorted(p.name for p in logs.iterdir()))}")
        say(
            f"uvicorn.log: {requests} requests wrote {asked} + {answered} records "
            f"({2 * requests} expected)"
        )
        say(f"job_runner.log: {runner_lines} records, all its own")
        say(f"records in the other program's file: {crossed}; logging errors: {errors}")
        if asked != requests or answered != requests or crossed or errors:
            raise CheckFailedError("a record was lost, or landed in the other file")
        return (
            f"two files, uvicorn.log and job_runner.log: {2 * requests} of "
            f"{2 * requests} records of the API in its own, {runner_lines} of the job "
            f"runner in its own, none in the other's, no logging error"
        )

    def _rotation(
        self, kind: str, directory: Path, reader: bool = False
    ) -> dict[str, Any]:
        """The pilot's scenario (section 2.6), with one of the two handlers."""
        shutil.rmtree(directory, ignore_errors=True)
        directory.mkdir(parents=True)
        for index in range(1, 6):
            (directory / f"application.log.{index}").write_text(
                f"SEED-{index} an older backup\n", encoding="utf-8"
            )
        target = str(directory / "application.log")
        env = self.installed_env()
        quiet = None
        follower = None
        if reader:
            self.python(["-c", WRITER, target, kind, "A", "1", "0"], env=env)
            system_root = os.environ.get("SystemRoot", r"C:\Windows")
            follower = subprocess.Popen(
                [
                    str(
                        Path(system_root)
                        / "System32/WindowsPowerShell/v1.0/powershell.exe"
                    ),
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    f"Get-Content -LiteralPath '{target}' -Wait",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            time.sleep(8.0)
        else:
            quiet = subprocess.Popen(
                [sys.executable, "-c", WRITER, target, kind, "A", "16", "0.5"],
                env=env,
                cwd=self.empty,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            time.sleep(1.0)
        try:
            busy = self.python(["-c", WRITER, target, kind, "B", "600", "0"], env=env)
        finally:
            if follower is not None:
                follower.kill()
                follower.wait(timeout=30)
        quiet_errors = 0
        if quiet is not None:
            _, quiet_stderr = quiet.communicate(timeout=120)
            quiet_errors = quiet_stderr.count("--- Logging error ---")

        def records(tag: str) -> int:
            found = set()
            for file in directory.glob("application.log*"):
                for line in file.read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines():
                    if line.startswith(f"{tag}-"):
                        found.add(line.split(" ", 1)[0])
            return len(found)

        backups = {
            file.name: file.read_text(encoding="utf-8", errors="replace")
            for file in directory.glob("application.log.[1-5]")
        }
        seeds = sum(text.startswith("SEED-") for text in backups.values())
        # A backup that holds records, and not a seed, is what a rotation that
        # happened made: each one pushes the oldest seed out, as it should.
        # A seed missing without one is a backup lost (section 2.6).
        rotations = len(backups) - seeds
        result = {
            "busy": records("B"),
            "quiet": records("A"),
            "errors": busy.stderr.count("--- Logging error ---") + quiet_errors,
            "seeds": seeds,
            "rotations": rotations,
            "lost": max(0, 5 - rotations) - seeds,
            "seconds": self.answer(busy)["seconds"],
            "files": ", ".join(
                f"{file.name} {file.stat().st_size}"
                for file in sorted(directory.glob("application.log*"))
            ),
        }
        say(
            f"  busy writer {result['busy']} of 600, quiet writer {result['quiet']}, "
            f"logging errors {result['errors']}"
        )
        say(
            f"  older backups: {seeds} of 5 kept, {rotations} pushed out by rotations "
            f"that happened, {result['lost']} lost"
        )
        say(f"  files: {result['files']}")
        return result

    def log_same_program_twice(self) -> str:
        self.staging()
        say("maxBytes=20000, backupCount=5; a busy writer logs 600 records while a")
        say(
            "quiet one, logging twice a second, holds the same file (the pilot's "
            "scenario)"
        )
        say("the control, RFC-005's RotatingFileHandler:")
        standard = self._rotation("standard", self.work / "rotation" / "standard")
        say("RFC-034's LosslessRotatingFileHandler:")
        directory = self.work / "rotation" / "lossless"
        lossless = self._rotation("lossless", directory)
        # Both processes are gone now: the next record of anyone cuts the file.
        target = str(directory / "application.log")
        self.python(
            ["-c", WRITER, target, "lossless", "C", "5", "0"], env=self.installed_env()
        )
        after = ", ".join(
            f"{file.name} {file.stat().st_size}"
            for file in sorted(directory.glob("application.log*"))
        )
        say(f"  after both exit, five more records from a third process: {after}")
        cut = (directory / "application.log").stat().st_size <= 20_000
        if lossless["busy"] != 600 or lossless["lost"] or lossless["errors"] or not cut:
            raise CheckFailedError(
                f"{lossless['busy']} of 600 records, {lossless['lost']} backups lost"
            )
        return (
            f"{lossless['busy']} of 600, no backup lost ({lossless['seeds']} of 5 "
            f"kept, {lossless['rotations']} pushed out by a rotation that happened "
            f"once the busy writer had left); no rotation while both lived. The "
            f"control, RFC-005's handler, on the same day: {standard['busy']} of "
            f"600, and {standard['lost']} backups lost"
        )

    def log_reader(self) -> str:
        self.staging()
        say("one writer, 600 records, while `Get-Content -Wait` follows the file:")
        seen = self._rotation(
            "lossless", self.work / "rotation" / "followed", reader=True
        )
        if seen["busy"] != 600 or seen["errors"] or seen["lost"]:
            raise CheckFailedError(f"{seen['busy']} of 600 records")
        return (
            f"{seen['busy']} of 600, no logging error, no backup lost: "
            f"{seen['rotations']} rotations happened, and pushed out "
            f"{5 - seen['seeds']} of the 5 older backups, as they should"
        )

    def rotation_retry(self) -> str:
        self.staging()
        directory = self.work / "rotation" / "attempts"
        shutil.rmtree(directory, ignore_errors=True)
        directory.mkdir(parents=True)
        target = directory / "application.log"
        target.write_text("held\n", encoding="utf-8")
        env = self.installed_env()
        holder = subprocess.Popen(
            [sys.executable, "-c", HOLDER, str(target)],
            env=env,
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert holder.stdout is not None
            if holder.stdout.readline().strip() != "holding":
                raise CheckFailedError("the holder did not open the file")
            seen = self.answer(
                self.python(["-c", ATTEMPT_PROBE, str(target), "300"], env=env)
            )
        finally:
            holder.kill()
            holder.wait(timeout=30)
        attempt = statistics.median(seen["attempts"])
        say("a rotation that cannot happen (another process holds the file): close,")
        say(
            "  the rename that fails, reopen. 300 times: "
            f"{spread(seen['attempts'], millis)}"
        )
        say(f"  one record written with no rotation due: {millis(seen['per_record'])}")
        if seen["rotated"]:
            raise CheckFailedError("the file rotated while another process held it")
        every = self._rotation(
            "lossless-every-record", self.work / "rotation" / "every-record"
        )
        spaced = self._rotation("lossless", self.work / "rotation" / "spaced")
        say(
            "  600 records with an attempt at every record: "
            f"{seconds(every['seconds'])}; "
            f"with the attempts spaced: {seconds(spaced['seconds'])}"
        )
        chosen = next(
            (
                candidate
                for candidate in RETRY_CANDIDATES
                if attempt / candidate <= RETRY_BUDGET
            ),
            RETRY_CANDIDATES[-1],
        )
        for candidate in RETRY_CANDIDATES:
            say(
                f"  every {candidate:.0f} s, the attempts cost "
                f"{attempt / candidate:.4%} of the time"
            )
        say(f"  the smallest candidate under {RETRY_BUDGET:.1%}: {chosen:.0f} s")
        return (
            f"a failed attempt costs {millis(attempt)} (a record, "
            f"{millis(seen['per_record'])}); "
            f"the smallest of {', '.join(f'{c:.0f}' for c in RETRY_CANDIDATES)} s "
            "under "
            f"{RETRY_BUDGET:.1%} of the time is {chosen:.0f} s"
        )

    # -- the two modes at once (section 2.5) ---------------------------------

    def _status(self, env: dict[str, str]) -> dict[str, str]:
        done = self.cli("status", env=env)
        return {
            key.strip(): value.strip()
            for key, _, value in (
                line.partition(":") for line in done.stdout.splitlines()
            )
            if value
        }

    def side_by_side(self) -> str:
        self.ensure_cluster()
        installed, checkout = self.installed_env(), self.checkout_env(
            DATABASE_SOURCE="embedded"
        )
        began = time.perf_counter()
        port = self.start_cluster(checkout)
        say(
            f"the checkout's `start`, in the same profile: port {port}, in "
            f"{seconds(time.perf_counter() - began)}"
        )
        try:
            seen = {
                "installed": self._status(installed),
                "checkout": self._status(checkout),
            }
            for label, status in seen.items():
                executable = PROCESSES.executable(int(status["pid"]))
                status["executable"] = str(executable)
                say(
                    f"{label}: {status['status']}, port {status['port']}, pid "
                    f"{status['pid']}"
                )
                say(f"    data:     {status['data']}")
                say(f"    postgres: {executable}")
            again = {
                "installed": self.cli("start", env=installed).stderr,
                "checkout": self.cli("start", env=checkout).stderr,
            }
            adopted = {
                label: re.search(r"ready on [\d.]+:(\d+) \(adopted", text)
                for label, text in again.items()
            }
            for label, match in adopted.items():
                say(
                    f"a second `start` from the {label} side: "
                    f"{'adopted port ' + match.group(1) if match else 'DID NOT ADOPT'}"
                )
            distinct = all(
                seen["installed"][key] != seen["checkout"][key]
                for key in ("pid", "port", "data", "executable")
            )
            own = all(
                match is not None and match.group(1) == seen[label]["port"]
                for label, match in adopted.items()
            )
            roots = (
                Path(seen["installed"]["data"]).is_relative_to(self.installed_data),
                Path(seen["checkout"]["data"]).is_relative_to(self.development_data),
                Path(seen["installed"]["executable"]).is_relative_to(self.staged_root),
                Path(seen["checkout"]["executable"]).is_relative_to(REPO),
            )
        finally:
            stopped = self.cli("stop", env=checkout)
        say(f"the checkout's `stop`: {last_lines(stopped.stderr, 1)}")
        after = self._status(installed)
        say(
            f"the installed cluster afterwards: {after['status']}, port "
            f"{after.get('port')}"
        )
        if not distinct or not own or not all(roots) or after["status"] != "ready":
            raise CheckFailedError("the two clusters are not apart")
        return (
            f"two postmasters ({seen['installed']['pid']} and "
            f"{seen['checkout']['pid']}), "
            "two ports, two roots (SolidVision\\ and SolidVision-dev\\), each from its "
            ""
            f"own runtime; a second start on each side adopted its own, and stopping "
            f"the checkout's left the installed one ready"
        )

    def elevated(self) -> str:
        say("MANUAL. Elevation needs a UAC prompt.")
        say("  1. From a PowerShell opened as administrator, with the staged tree on")
        say(
            "     PYTHONPATH and an empty LOCALAPPDATA, run the embedded `start`, then "
            "`stop`."
        )
        say("  2. From an ordinary PowerShell, the same variables: `start`, the API,")
        say("     the job runner. Do the three run, and write their logs?")
        say(f"this process is elevated: {bool(ctypes.windll.shell32.IsUserAnAdmin())}")
        return "manual: not run"

    # -- the password (section 7.7) ------------------------------------------

    def password_leak(self) -> str:
        self.ensure_cluster()
        password = self.installed_password()
        env = self.installed_env()
        direct = self.answer(self.python(["-c", LEAK_PROBE], env=env))
        through_run = self.answer(
            self.cli("run", env=env, command=[sys.executable, "-c", LEAK_PROBE])
        )
        self.cluster_up = True  # `run` adopted the cluster and left it up
        say(
            f"repr(settings) holds it: {direct['in_repr']}; the report: "
            f"{direct['in_report']}"
        )
        say(
            f"a child of `run --`: {through_run['in_environment']} variables hold it; "
            f"`run` exported {through_run['exported']}"
        )
        scanned, found = 0, []
        for root in (self.profile, self.out, self.temp):
            for path in root.rglob("*"):
                if not path.is_file() or path == self.installed_files() / "password":
                    continue
                scanned += 1
                try:
                    if password.encode("ascii") in path.read_bytes():
                        found.append(str(path.relative_to(self.work)))
                except OSError:
                    continue
        said = sum(password in text for text in self.captured)
        printed = password in "".join(self.tee.text)
        say(
            "files under the profile, the children's output and their %TEMP%: "
            f"{scanned} scanned"
        )
        say(f"  holding the password: {found or 'none'}")
        say(
            f"  children whose stdout or stderr held it: {said} of {len(self.captured)}"
        )
        say(f"  this script's own output: {printed}")
        occurrences = (
            len(found)
            + said
            + int(printed)
            + int(direct["in_repr"])
            + int(direct["in_report"])
            + through_run["in_environment"]
        )
        if occurrences:
            raise CheckFailedError(f"{occurrences} occurrences")
        return (
            f"0 occurrences: not in repr(settings), not in the report, not in the "
            "environment of a child of `run --` (which got "
            f"{len(through_run['exported'])} "
            f"variables, none of them a credential), not in {scanned} files of logs "
            f"and data, not in the output of {len(self.captured)} children"
        )

    # -- the repository ------------------------------------------------------

    @staticmethod
    def _summary(output: str) -> str:
        for line in reversed(output.splitlines()):
            if re.search(r"\d+ (passed|failed|error)", line):
                return line.strip("= ").strip()
        return "no summary line"

    @staticmethod
    def _counts(summary: str) -> str:
        """`2460 passed, 79 deselected`: the outcomes, without warnings or time."""
        found = re.findall(r"\d+ (?:passed|failed|errors?|deselected|skipped)", summary)
        return ", ".join(found) or summary

    def architecture(self) -> str:
        done = self.python(
            [
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "backend/tests/test_path_anchors_architecture.py",
                "backend/tests/test_stage_installation.py",
            ],
            env=self.checkout_env(),
            cwd=REPO,
            timeout=900.0,
        )
        summary = self._summary(done.stdout)
        say(f"the anchors and the staging: exit {done.returncode} -- {summary}")
        if done.returncode != 0:
            raise CheckFailedError(summary)
        return summary

    def style(self) -> str:
        sources = ["backend/app", "backend/tests", "backend/dataset_tools"]
        scripts = [
            "scripts/stage_installation.py",
            "experiments/rfc-034-user-configuration/measure_user_configuration.py",
        ]
        tools = {
            "black --check": ["-m", "black", "--check", *sources, *scripts],
            "ruff check": ["-m", "ruff", "check", "--no-fix", *sources, *scripts],
            "mypy": ["-m", "mypy"],
        }
        results, failed = [], []
        for name, command in tools.items():
            done = self.python(
                command, env=self.checkout_env(), cwd=REPO, timeout=1800.0
            )
            last = (done.stdout + done.stderr).strip().splitlines()[-1:]
            say(f"{name}: exit {done.returncode} -- {last[0] if last else ''}")
            results.append(f"{name} {'clean' if done.returncode == 0 else 'FAILED'}")
            if done.returncode != 0:
                failed.append(name)
        if failed:
            raise CheckFailedError(f"{', '.join(failed)} not clean")
        return ", ".join(results)

    # -- the suites (opt-in) -------------------------------------------------

    @staticmethod
    def _env_database_counters() -> tuple[int, int] | None:
        """`xact_commit` and `xact_rollback` of the database the `.env` names:
        the one a suite that missed the cluster would have gone to."""
        configured = Settings()
        try:
            with psycopg.connect(
                host=configured.database_host,
                port=configured.database_port,
                user=configured.database_user,
                password=configured.database_password,
                dbname=configured.database_name,
                connect_timeout=5,
                autocommit=True,
            ) as db:
                row = db.execute(
                    "SELECT xact_commit, xact_rollback FROM pg_stat_database "
                    "WHERE datname = current_database()"
                ).fetchone()
        except psycopg.Error:
            return None
        return (int(row[0]), int(row[1])) if row else None

    def suite(self) -> str:
        env = self.checkout_env()
        say(f"docker: {docker_status()}")
        before = self.answer(
            self.cli("run", env=env, command=[sys.executable, "-c", ENGINE_PROBE])
        )
        say(
            f"`run -- <probe>`: source {before['source']}; line 4 of postmaster.pid "
            f"says port {before['pid_file_port']}; the server the project's engine "
            f"reached says port {before['server_port']}"
        )
        say(f"  {before['version']}")
        say(f"  the engine's URL: {before['url']}; the cluster: {before['home']}")
        proved = (
            before["source"] == "embedded"
            and before["pid_file_port"] == before["server_port"] != 5432
            and "Visual C++" in before["version"]
            and Path(before["home"]).is_relative_to(self.development_data)
        )
        if not proved:
            raise CheckFailedError("the project's engine does not reach the cluster")
        other_before = self._env_database_counters()
        began = time.perf_counter()
        done = self.cli(
            "run",
            env=env,
            command=[sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
            timeout=3600.0,
            cwd=REPO,
        )
        elapsed = time.perf_counter() - began
        other_after = self._env_database_counters()
        after = self.answer(
            self.cli("run", env=env, command=[sys.executable, "-c", ENGINE_PROBE])
        )
        summary = self._summary(done.stdout)
        self.suite_count = self._counts(summary)
        commits = after["xact_commit"] - before["xact_commit"]
        rollbacks = after["xact_rollback"] - before["xact_rollback"]
        say(
            "`run -- python -m pytest`, from the repository root: exit "
            f"{done.returncode}"
        )
        say(f"  {summary} (wall {seconds(elapsed)})")
        for line in done.stdout.splitlines():
            if line.startswith(("FAILED ", "ERROR ")):
                say(f"  {line}")
        say(
            f"  on the cluster: xact_commit {before['xact_commit']} -> "
            f"{after['xact_commit']} (+{commits}), xact_rollback +{rollbacks}"
        )
        if other_before is None or other_after is None:
            other = "the .env's database unreachable"
            say("  the .env's database was not reachable: nowhere else to go")
        else:
            moved = (other_after[0] - other_before[0], other_after[1] - other_before[1])
            other = (
                f"+{moved[0]} commits and +{moved[1]} rollbacks on the .env's database"
            )
            say(
                f"  on the .env's database meanwhile: +{moved[0]} commits, +{moved[1]} "
                "rollbacks"
            )
            # The suite rolls every database test back: rollbacks are its
            # signature, and nothing that runs alone in the other database
            # makes one.
            if moved[1] > 5:
                raise CheckFailedError("the suite also talked to the .env's database")
        if done.returncode != 0 or commits <= 10:
            raise CheckFailedError(f"{summary}; +{commits} commits on the cluster")
        return (
            f"{summary}; the engine reached port {before['server_port']}, the one in "
            f"postmaster.pid; +{commits} commits and +{rollbacks} rollbacks on the "
            f"cluster, {other}"
        )

    def docker_suite(self) -> str:
        docker = docker_status()
        say(f"docker: {docker}")
        began = time.perf_counter()
        done = self.python(
            ["-m", "pytest", "-q", "-p", "no:cacheprovider"],
            env=self.checkout_env(),
            cwd=REPO,
            timeout=3600.0,
        )
        summary = self._summary(done.stdout)
        say(
            f"pytest, plain, against the .env: exit {done.returncode} -- {summary} "
            f"(wall {seconds(time.perf_counter() - began)})"
        )
        for line in done.stdout.splitlines():
            if line.startswith(("FAILED ", "ERROR ")):
                say(f"  {line}")
        count = self._counts(summary)
        if self.suite_count is None:
            same = ""
        elif count == self.suite_count:
            same = "; the same count as through the cluster"
        else:
            same = f"; NOT the count through the cluster ({self.suite_count})"
        if done.returncode != 0 or "NOT" in same:
            raise CheckFailedError(f"{summary}{same}")
        return f"{summary}{same}; docker: {docker}"

    def adapter_baseline(self) -> str:
        """RFC-033's own script, on this disk, before any comparison with it."""
        script = (
            REPO / "experiments" / "rfc-033-embedded-postgres" / "measure_adapter.py"
        )
        done = self.python(
            [str(script), "--only", "first-run,warm-start,adoption"],
            env=self.checkout_env(),
            cwd=REPO,
            timeout=3600.0,
        )
        for line in done.stdout.splitlines():
            say(line)
        verdict = done.stdout.partition("VERDICT")[2]
        kept = [
            line.strip()
            for line in verdict.splitlines()
            if line.strip(" =")
            and "removed" not in line
            and "checks failed" not in line
        ]
        if done.returncode != 0 or not kept:
            raise CheckFailedError(f"exit {done.returncode}: {last_lines(done.stderr)}")
        return " | ".join(kept)

    # -- the last check ------------------------------------------------------

    def ours_running(self) -> list[tuple[int, str]]:
        """The processes this run is answerable for, and no others: a server
        whose lock file is under the work directory, its children, and
        anything running from the staged tree."""
        found: dict[int, str] = {}
        snapshot = PROCESSES.snapshot()
        for lock_file in self.work.rglob("postmaster.pid"):
            pid_file = read_postmaster_pid(lock_file)
            if pid_file is None or not PROCESSES.is_alive(pid_file.pid):
                continue
            executable = PROCESSES.executable(pid_file.pid)
            if executable is None or executable.name.lower() != "postgres.exe":
                continue
            found[pid_file.pid] = executable.name
            for process in snapshot:
                if process.parent_pid == pid_file.pid:
                    found[process.pid] = process.name
        for process in snapshot:
            executable = PROCESSES.executable(process.pid)
            if executable is not None and str(executable).lower().startswith(
                str(self.work).lower()
            ):
                found[process.pid] = executable.name
        for started in self.services:
            if started.alive():
                found[started.process.pid] = started.name
        return sorted(found.items())

    def shut_down(self) -> None:
        for started in self.services:
            started.stop()
        if self.staged_root.exists() and (self.installed_data / "postgres").exists():
            self.cli("stop", env=self.installed_env())
            self.cluster_up = False
        if (self.development_data / "postgres").exists():
            self.cli("stop", env=self.checkout_env(DATABASE_SOURCE="embedded"))

    def nothing_left(self) -> str:
        self.shut_down()
        time.sleep(1.0)
        ours = self.ours_running()
        say(
            f"the API, the job runner, servers with a lock file under the work "
            f"directory, and processes of the staged tree: {ours or 'none'}"
        )
        if ours:
            raise CheckFailedError(f"left running: {ours}")
        return "no process of this run left"


CHECKS: list[tuple[str, str, str, bool]] = [
    # (id, section 16 row, method, runs by default)
    ("pyc", "The .pyc files", "bytecode", True),
    ("first-run", "First installed run", "first_run", True),
    ("thumbnail", "The thumbnail", "thumbnail", True),
    ("writes-everything", '"Writes everything"', "writes_everything", True),
    ("real-root", "The real root", "real_root", True),
    ("health-installed", "/health, installed", "health_installed", True),
    ("log-two-programs", "Log, two programs", "log_two_programs", True),
    (
        "health-cluster-stopped",
        "/health with the cluster stopped",
        "health_cluster_stopped",
        True,
    ),
    ("restart-other-port", "Restart on another port", "restart_other_port", True),
    (
        "health-reinitializing",
        "/health while the server reinitializes",
        "health_reinitializing",
        True,
    ),
    (
        "health-external-down",
        "/health, external source, database down",
        "health_external_down",
        True,
    ),
    ("locate-cost", "The cost of locate()", "locate_cost", True),
    (
        "new-connection",
        "A new connection, with and without the listener",
        "new_connection",
        True,
    ),
    (
        "hostile-environment",
        "Hostile environment, installed",
        "hostile_environment",
        True,
    ),
    ("pg-variables", "The PG* variables", "pg_variables", True),
    ("user-file", "The user's file", "user_file", True),
    ("notepad-txt", "solidvision.env.txt", "notepad_txt", True),
    ("no-localappdata", "Without LOCALAPPDATA", "no_local_app_data", True),
    ("two-cwds", "Two working directories", "two_working_directories", True),
    (
        "log-same-program-twice",
        "Log, the same program twice",
        "log_same_program_twice",
        True,
    ),
    ("log-reader", "Log, a reader", "log_reader", True),
    ("rotation-retry", "The interval of the rotation's retry", "rotation_retry", True),
    ("side-by-side", "The two modes side by side", "side_by_side", True),
    ("elevated", "First run elevated", "elevated", True),
    ("suite", "The suite by the new path", "suite", False),
    ("docker-suite", "Docker still works", "docker_suite", False),
    (
        "adapter-baseline",
        "The adapter's baseline on this disk",
        "adapter_baseline",
        False,
    ),
    ("password-leak", "The password does not leak", "password_leak", True),
    ("architecture", "Architecture", "architecture", True),
    ("style", "Style and types", "style", True),
    ("nothing-left", "Nothing left running", "nothing_left", True),
]


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--only", default="")
    parser.add_argument("--skip", default="")
    parser.add_argument("--with", dest="extra", default="")
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--work", type=Path)
    args = parser.parse_args()
    if args.list:
        for name, title, _, default in CHECKS:
            print(f"{name:<24} {title}{'' if default else '   (opt-in: --with)'}")
        return 0
    only = {name for name in args.only.split(",") if name}
    skip = {name for name in args.skip.split(",") if name}
    extra = {name for name in args.extra.split(",") if name}
    unknown = (only | skip | extra) - {name for name, *_ in CHECKS}
    if unknown:
        print(f"unknown checks: {sorted(unknown)}")
        return 2

    work = (
        args.work.resolve() if args.work else Path(tempfile.mkdtemp(prefix="rfc034-"))
    )
    work.mkdir(parents=True, exist_ok=True)
    measurement = Measurement(work)
    sys.stdout = measurement.tee  # type: ignore[assignment]

    from psycopg import pq

    print("=" * 78)
    print("RFC-034 section 16: configuration and user directories, measured")
    print("=" * 78)
    print(f"when:     {datetime.datetime.now().astimezone():%Y-%m-%d %H:%M:%S %z}")
    print(f"work dir: {work}")
    print(f"repo:     {REPO}")
    commit = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=REPO,
        capture_output=True,
        text=True,
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "status", "--porcelain"], cwd=REPO, capture_output=True, text=True
    ).stdout.strip()
    print(f"commit:   {commit}{' + uncommitted changes' if dirty else ''}")
    print()
    print("conditions")
    print(f"    platform:   {platform.platform()}, {platform.machine()}")
    print(f"    python:     {platform.python_version()} ({sys.executable})")
    print(
        f"    libpq:      {pq.version()} ({pq.__impl__}), psycopg {psycopg.__version__}"
    )
    print(f"    cpu:        {platform.processor()} x{os.cpu_count()}")
    print(f"    storage:    {storage_of(work)}")
    print(f"    docker:     {docker_status()}")
    print(
        f"    ANSI code page: {ctypes.windll.kernel32.GetACP()}; this process "
        f"elevated: {bool(ctypes.windll.shell32.IsUserAnAdmin())}"
    )
    named = sorted(
        name
        for name in os.environ
        if name.upper() in SETTING_NAMES or name.upper().startswith("PG")
    )
    print(f"    settings and PG* variables in this environment: {named or 'none'}")
    dotenv = REPO / ".env"
    digest = (
        hashlib.sha256(dotenv.read_bytes()).hexdigest()[:12]
        if dotenv.is_file()
        else "absent"
    )
    print(f"    the checkout's .env: sha256 {digest} (its values are never printed)")

    failed = []
    try:
        for name, title, method, default in CHECKS:
            chosen = name in only if only else (default or name in extra)
            if not chosen or name in skip:
                continue
            print()
            print(f"--- {title}  [{name}]")
            sys.stdout.flush()
            began = time.perf_counter()
            try:
                measurement.verdict[name] = getattr(measurement, method)()
            except CheckFailedError as error:
                measurement.verdict[name] = f"FAILED: {error}"
                failed.append(name)
                say(f"FAILED: {error}")
            except Exception as error:  # the next check still has to run
                measurement.verdict[name] = (
                    f"ERROR: {type(error).__name__}: {str(error)[:200]}"
                )
                failed.append(name)
                say(f"ERROR: {type(error).__name__}: {error}")
                for line in traceback.format_exc().splitlines()[-8:]:
                    say(f"  {line}")
            say(f"[{time.perf_counter() - began:.1f} s]")
    finally:
        try:
            measurement.shut_down()
        except Exception as error:
            say(f"shutting down: {type(error).__name__}: {error}")
        for process_id, _ in measurement.ours_running():
            PROCESSES.terminate(process_id)

    print()
    print("=" * 78)
    print("VERDICT")
    print("=" * 78)
    for name, title, _, _ in CHECKS:
        if name in measurement.verdict:
            print(f"  {title}")
            print(f"      {measurement.verdict[name]}")
    print()
    if args.keep or args.work:
        print(f"  kept: {work}")
    else:
        time.sleep(1.0)
        if measurement.staged_root.exists():
            staging.remove(measurement.staged_root)
        shutil.rmtree(work, ignore_errors=True)
        print(f"  {'NOT fully removed' if work.exists() else 'removed'}: {work}")
    print(f"  checks failed: {', '.join(failed) or 'none'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
