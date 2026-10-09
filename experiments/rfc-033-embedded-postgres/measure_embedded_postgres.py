"""PostgreSQL + pgvector on Windows, without Docker: the spike, rebuilt to stay.

RFC-033a section 4. The spike of 2026-09-22 left only a log; the script that
produced it was never committed. This one reproduces every step of that log
(RFC-033a Appendix A), against today's Alembic head, and records what the log
left out (section 2.2):

    1.  micromamba    pinned to 2.9.0, sha256 checked, extracted with
                      `filter="data"`
    2.  materialize   postgresql, pgvector and libpq pinned by version *and*
                      build; the explicit environment, with md5, goes to the log
    3.  prune         the .pdb files, size before and after
    4.  initdb        scram, password file, `--encoding=UTF8 --locale=C`
    5.  start         loopback, a port asked free of the OS, `log_line_prefix`
                      with milliseconds; listening -> ready from the server log
    6.  extension     CREATE EXTENSION vector, cosine `<=>`, an HNSW index
    7a. alembic       upgrade head: expected 6d77379a36a1, with RFC-032's three
                      columns and three CHECKs
    7b. suite         the default pytest suite, counted only if a subprocess
                      with the same environment proves -- through the
                      project's own `settings` and engine -- that it reaches
                      this cluster, and if `xact_commit` rises (section 4.2)
    8.  facts         the fields of section 6

`--parity` reads the same facts from the development database -- the one the
project's `.env` points at -- and prints one row per field, with the verdict.
The divergences section 6.1 accepts are declared below, before the
measurement, not after it.

The cluster is stopped in a `finally`, always, and the process list is then
checked for a `postgres.exe` left behind under the work directory. `--fail-at
STEP` forces a step to fail, so that the check can be exercised on the path
that matters.

Run it with the backend virtualenv, from the repository root, with the output
redirected (`> FILE 2>&1`) next to this script. Docker off for the run that
reproduces the spike, into `measure_embedded_postgres.log`
(`--initdb-defaults` adds section 5's extra initdb):

    backend/.venv/Scripts/python.exe SCRIPT --initdb-defaults

and Docker on, with the development database up, for the parity run, into
`measure_embedded_postgres_parity.log`:

    backend/.venv/Scripts/python.exe SCRIPT --parity

where SCRIPT is `experiments/rfc-033-embedded-postgres/measure_embedded_postgres.py`.

**What this does not measure (section 4.4):** a warm start under an adapter,
recovery from a dirty shutdown, a busy port, antivirus, accented paths in the
data directory. Those are RFC-033's criteria.

No new dependency: download, tar, subprocess and hashlib come from the
standard library, and the database is read with the `psycopg` the project
already has.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import hashlib
import json
import locale
import os
import platform
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"

MICROMAMBA_VERSION = "2.9.0"
MICROMAMBA_URL = (
    "https://api.anaconda.org/download/conda-forge/micromamba/"
    f"{MICROMAMBA_VERSION}/win-64/micromamba-{MICROMAMBA_VERSION}-0.tar.bz2"
)
MICROMAMBA_SHA256 = "97a336f4ab794bd96a6a4da5e6ed63e75a1d31830414a182419b23d3b36f3fe0"
"""From conda-forge's own file listing for `win-64/micromamba-2.9.0-0.tar.bz2`."""

MICROMAMBA_MEMBER = "Library/bin/micromamba.exe"

CHANNEL = "conda-forge"
SUBDIR = "win-64"
PINNED = {
    "postgresql": ("16.15", "he837cf3_0"),
    "pgvector": ("0.8.6", "h2466b09_0"),
    "libpq": ("16.15", "h43e12c5_0"),
}
"""Version and build. A version alone lets the solver pick another build."""

SHIPPED = (
    ("bin/postgres.exe", "postgres.exe"),
    ("bin/initdb.exe", "initdb.exe"),
    ("bin/pg_ctl.exe", "pg_ctl.exe"),
    ("bin/psql.exe", "psql.exe"),
    ("lib/vector.dll", "lib/vector.dll"),
)

DB_USER = "solidvision"
DB_NAME = "solidvision"
ENCODING = "UTF8"
LOCALE = "C"
LOOPBACK = "127.0.0.1"

EXPECTED_HEAD = "6d77379a36a1"
EXPECTED_SERVER = ("16.15", "Visual C++")
RFC032_COLUMNS = ("latitude", "longitude", "position_source")
RFC032_CHECKS = (
    "ck_images_latitude_range",
    "ck_images_longitude_range",
    "ck_images_position_pairing",
)
XACT_FLOOR = 10
"""The script's own reads commit a handful of transactions; the suite, thousands."""

COLLATION_PROBE = (
    "zebra",
    "Zebra",
    "ZEBRA",
    "água",
    "Água",
    "agua",
    "Agua",
    "árvore",
    "avião",
    "Ávila",
    "éclair",
    "Eclair",
    "eclair",
    "ção",
    "cão",
    "Çedilha",
    "cedilha",
    "Ñandu",
    "nandu",
    "_raw",
    "raw",
    "Raw",
    "raw_2",
    "raw 2",
    "raw10",
    "raw2",
    "10",
    "2",
    "02",
    "a b",
    "a_b",
    "ab",
    "AB",
    "a-b",
)
"""Section 6: capitals, lower case, accents, digits, `_` and space."""

WIN1252_PROBE = "Łódź/東京/✈.jpg"
"""Three characters a WIN1252 cluster refuses: Polish, CJK, a symbol."""

VERDICT_ORDER = (
    "initdb (once, per machine):",
    "installer payload (pruned):",
    "initdb without flags chooses:",
    "listening -> ready:",
    "alembic upgrade head:",
    "suite:",
    "suite target:",
    "port:",
    "encoding, locale:",
)

STEP_IDS = ("1", "2", "3", "4", "5", "6", "7a", "7b", "8")
STATE_FILE = "rfc033a-state.json"
CONF_FILE = "rfc033a.conf"


# --------------------------------------------------------------------------
# Section 6: the parity table, and the divergences 6.1 accepts. Declared
# here, before anything is measured.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Rule:
    name: str
    kind: str
    """`equal`: both sides the same. `both`: both sides `expected`.
    `recorded`: an accepted divergence (section 6.1), printed, not required."""
    expected: str | None = None


PARITY_RULES = (
    Rule("major version", "equal"),
    Rule("minor version", "recorded"),
    Rule("pgvector", "equal"),
    Rule("encoding", "both", ENCODING),
    Rule("provider, datcollate, datctype", "both", f"libc, {LOCALE}, {LOCALE}"),
    Rule("ordering", "equal"),
    Rule("lower()", "equal"),
    Rule("path outside WIN1252", "both", "byte-identical"),
    Rule("hnsw.ef_search", "both", "40"),
    Rule("alembic head", "equal"),
)

ACCEPTED_DIVERGENCES = (
    "minor version: 0.8.6-pg16 froze on the 16.x of its day; minor releases "
    "do not change the on-disk format or semantics (6.1)",
    "compiler and OS: gcc on Debian against MSVC on Windows -- the thing being "
    "compared, not a defect (6.1)",
    "transport: Docker's +43 ms above ~8 KB belongs to development; latency is "
    "taken server-side (6.1)",
)


# --------------------------------------------------------------------------
# Small helpers.
# --------------------------------------------------------------------------


def mb(size: int) -> int:
    return round(size / (1024 * 1024))


def dir_size(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run(
    args: list[Any],
    *,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    timeout: float | None = None,
    encoding: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(arg) for arg in args],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding=encoding or locale.getpreferredencoding(False),
        errors="replace",
        env=env,
        cwd=cwd,
        timeout=timeout,
    )


def run_to_file(
    args: list[Any], output: Path, *, env: dict[str, str]
) -> tuple[int, str]:
    """For `pg_ctl`: a pipe would be inherited by the server it starts, and
    `subprocess.run` would then wait for a server that never exits."""
    with output.open("w", encoding="utf-8") as handle:
        completed = subprocess.run(
            [str(arg) for arg in args],
            stdout=handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=env,
        )
    text = output.read_text(
        encoding=locale.getpreferredencoding(False), errors="replace"
    )
    return completed.returncode, text


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((LOOPBACK, 0))
        return int(probe.getsockname()[1])


def tail(text: str, lines: int = 25) -> str:
    return "\n".join("      " + line for line in text.strip().splitlines()[-lines:])


class CheckFailedError(Exception):
    """A check failed. The message says which, and the log keeps it."""


class ForcedFailureError(Exception):
    """`--fail-at`: a step made to fail on purpose (section 13)."""


# --------------------------------------------------------------------------
# Conditions.
# --------------------------------------------------------------------------


def docker_status() -> tuple[bool, str]:
    if shutil.which("docker") is None:
        return False, "not installed"
    try:
        completed = run(
            ["docker", "info", "--format", "{{.ServerVersion}}"], timeout=30
        )
    except subprocess.TimeoutExpired:
        return False, "installed, daemon not answering"
    if completed.returncode != 0 or not completed.stdout.strip():
        return False, "installed but NOT running"
    return True, f"RUNNING (engine {completed.stdout.strip()})"


def storage_of(path: Path) -> str:
    """The medium under `path`, read rather than asserted.

    The spike's log says "system SSD". The system disk of the machine it ran
    on is a SATA hard disk; the line was a constant in the script.
    """
    drive = path.resolve().drive.rstrip(":")
    command = (
        f"$d = Get-Partition -DriveLetter {drive} | Get-Disk; "
        "Get-PhysicalDisk | Where-Object DeviceId -eq $d.Number | "
        'ForEach-Object { "$($_.FriendlyName)|$($_.MediaType)|$($_.BusType)" }'
    )
    try:
        completed = run(["powershell", "-NoProfile", "-Command", command], timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return f"{drive}: medium unknown"
    found = completed.stdout.strip().split("|")
    if len(found) != 3:
        return f"{drive}: medium unknown"
    name, media, bus = found
    return f"{drive}: on {name} ({media}, {bus}, from Get-PhysicalDisk)"


def git_commit() -> str:
    try:
        head = run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO).stdout.strip()
        dirty = run(["git", "status", "--porcelain"], cwd=REPO).stdout.strip()
    except OSError:
        return "unknown"
    return f"{head}{' + uncommitted changes' if dirty else ''}"


def postgres_processes() -> list[tuple[int, str]]:
    command = (
        "Get-CimInstance Win32_Process -Filter \"Name='postgres.exe'\" | "
        'ForEach-Object { "$($_.ProcessId)|$($_.ExecutablePath)" }'
    )
    completed = run(["powershell", "-NoProfile", "-Command", command], timeout=60)
    found = []
    for line in completed.stdout.splitlines():
        pid, _, path = line.strip().partition("|")
        if pid.isdigit():
            found.append((int(pid), path))
    return found


# --------------------------------------------------------------------------
# The run.
# --------------------------------------------------------------------------


@dataclass
class Baseline:
    work: Path
    keep: bool
    fail_at: str | None
    owns_work: bool
    password: str = ""
    port: int = 0
    running: list[Path] = field(default_factory=list)
    state: dict[str, Any] = field(default_factory=dict)
    verdict: dict[str, str] = field(default_factory=dict)

    @property
    def micromamba(self) -> Path:
        return self.work / "micromamba.exe"

    @property
    def mamba_root(self) -> Path:
        return self.work / "mamba-root"

    @property
    def env_dir(self) -> Path:
        return self.work / "env"

    @property
    def library(self) -> Path:
        return self.env_dir / "Library"

    @property
    def pgdata(self) -> Path:
        return self.work / "pgdata"

    @property
    def pwfile(self) -> Path:
        return self.work / "pwfile"

    def tool(self, name: str) -> Path:
        return self.library / "bin" / f"{name}.exe"

    def pg_env(self) -> dict[str, str]:
        env = os.environ.copy()
        for name in list(env):
            if name.upper().startswith("PG"):
                del env[name]
        env["PATH"] = os.pathsep.join(
            [str(self.env_dir), str(self.library / "bin"), env.get("PATH", "")]
        )
        env["PGPASSWORD"] = self.password
        return env

    def project_env(self) -> dict[str, str]:
        """The suite's view of this cluster: variables above `.env` (RFC-004)."""
        env = os.environ.copy()
        env.update(
            DATABASE_HOST=LOOPBACK,
            DATABASE_PORT=str(self.port),
            DATABASE_USER=DB_USER,
            DATABASE_PASSWORD=self.password,
            DATABASE_NAME=DB_NAME,
            PYTHONIOENCODING="utf-8",
        )
        return env

    def connect(self, dbname: str = DB_NAME) -> psycopg.Connection[Any]:
        return psycopg.connect(
            host=LOOPBACK,
            port=self.port,
            user=DB_USER,
            password=self.password,
            dbname=dbname,
            autocommit=True,
        )

    def save_state(self) -> None:
        (self.work / STATE_FILE).write_text(
            json.dumps(self.state, indent=2), encoding="utf-8"
        )

    @contextlib.contextmanager
    def step(self, step_id: str, title: str) -> Iterator[None]:
        label = f"{step_id}. {title}"
        print()
        print(f"--- {label}")
        sys.stdout.flush()
        started = time.perf_counter()
        try:
            if self.fail_at == step_id:
                raise ForcedFailureError(f"step {step_id} forced to fail (--fail-at)")
            yield
        except BaseException:
            print(f"    [{time.perf_counter() - started:.2f} s] {label} -- FAILED")
            sys.stdout.flush()
            raise
        print(f"    [{time.perf_counter() - started:.2f} s] {label}")
        sys.stdout.flush()

    def reused(self, what: str) -> bool:
        if self.keep and self.state.get(what):
            built = self.state.get("built", "an earlier run")
            print(f"    reused (--keep): built {built}")
            return True
        return False

    # -- 1 -----------------------------------------------------------------

    def fetch_micromamba(self) -> None:
        with self.step("1", "fetch micromamba (standalone, not installed)"):
            if self.reused("micromamba"):
                print(f"    {self.micromamba}")
                print(f"    {self.state['micromamba']}")
                return
            archive = self.work / MICROMAMBA_URL.rsplit("/", 1)[1]
            request = urllib.request.Request(
                MICROMAMBA_URL, headers={"User-Agent": "SolidVision RFC-033a"}
            )
            with (
                urllib.request.urlopen(request, timeout=300) as response,
                archive.open("wb") as handle,
            ):
                shutil.copyfileobj(response, handle)
            digest = sha256(archive)
            print(f"    {MICROMAMBA_URL}")
            if digest != MICROMAMBA_SHA256:
                raise CheckFailedError(f"sha256 {digest}, expected {MICROMAMBA_SHA256}")
            print(f"    sha256 {digest}  OK (pinned)")
            with tarfile.open(archive, "r:bz2") as tar:
                member = tar.getmember(MICROMAMBA_MEMBER)
                member.name = self.micromamba.name
                tar.extract(member, self.work, filter="data")
            archive.unlink()
            print(f"    {mb(self.micromamba.stat().st_size)} MB -> {self.micromamba}")
            version = run([self.micromamba, "--version"]).stdout.strip()
            print(f"    {version}")
            if version != MICROMAMBA_VERSION:
                raise CheckFailedError(
                    f"micromamba says {version}, pinned {MICROMAMBA_VERSION}"
                )
            self.state["micromamba"] = version
            self.save_state()

    # -- 2 -----------------------------------------------------------------

    def mamba_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.update(
            MAMBA_ROOT_PREFIX=str(self.mamba_root),
            CONDA_PKGS_DIRS=str(self.mamba_root / "pkgs"),
            MAMBA_NO_BANNER="1",
        )
        return env

    def materialize(self) -> None:
        specs = [f"{name}={ver}={build}" for name, (ver, build) in PINNED.items()]
        title = "materialize postgresql=16.15 + pgvector=0.8.6 (win-64)"
        with self.step("2", title):
            if self.reused("materialized"):
                self.print_environment()
            else:
                shutil.rmtree(self.env_dir, ignore_errors=True)  # a half-built one
                print(f"    specs: {' '.join(specs)}")
                print(f"    channel: {CHANNEL} only (--override-channels), {SUBDIR}")
                created = run(
                    [
                        self.micromamba,
                        "create",
                        "--yes",
                        "--no-rc",
                        "--root-prefix",
                        self.mamba_root,
                        "--prefix",
                        self.env_dir,
                        "--override-channels",
                        "--channel",
                        CHANNEL,
                        "--platform",
                        SUBDIR,
                        *specs,
                    ],
                    env=self.mamba_env(),
                    timeout=1800,
                )
                if created.returncode != 0:
                    print(tail(created.stdout + created.stderr))
                    raise CheckFailedError(f"micromamba create rc={created.returncode}")
                self.print_environment()
                self.state["materialized"] = True
                self.save_state()
        for relative, label in SHIPPED:
            present = (self.library / relative).is_file()
            print(f"    {'OK' if present else 'MISSING'} {label}")
            if not present:
                raise CheckFailedError(f"{relative} is not in the environment")

    def print_environment(self) -> None:
        listed = run(
            [self.micromamba, "list", "--prefix", self.env_dir, "--json"],
            env=self.mamba_env(),
            encoding="utf-8",
        )
        listing = json.loads(listed.stdout)
        if isinstance(listing, dict):  # micromamba 2.x wraps the list
            listing = listing["packages"]
        packages = {package["name"]: package for package in listing}
        for name in sorted(PINNED):
            package = packages[name]
            build = package.get("build_string") or package.get("build")
            print(f"    {name:<18} {package['version']}-{build}")
            if (package["version"], build) != PINNED[name]:
                raise CheckFailedError(
                    f"{name} resolved to {package['version']}-{build}"
                )
            if name == "pgvector":
                meta = next((self.env_dir / "conda-meta").glob("pgvector-*.json"))
                depends = json.loads(meta.read_text(encoding="utf-8"))["depends"]
                constraint = [d for d in depends if d.startswith("libpq ")]
                print(f"    {'pgvector->libpq':<18} {', '.join(constraint)}")
        icu = packages.get("icu")
        if icu:
            wanted_by = sorted(
                meta.stem
                for meta in (self.env_dir / "conda-meta").glob("*.json")
                if any(
                    d.split()[0] == "icu"
                    for d in json.loads(meta.read_text(encoding="utf-8"))["depends"]
                )
            )
            print(
                f"    {'icu':<18} {icu['version']}-{icu['build_string']}, required "
                f"by {', '.join(wanted_by) or 'nothing'} (section 5; step 8 says "
                "whether the server uses it)"
            )
        else:
            print(f"    {'icu':<18} not in the environment (section 5)")
        explicit = run(
            [self.micromamba, "list", "--prefix", self.env_dir, "--explicit", "--md5"],
            env=self.mamba_env(),
            encoding="utf-8",
        )
        lines = [
            line.strip()
            for line in explicit.stdout.splitlines()
            if line.strip().startswith("http")
        ]
        print(
            f"    explicit environment ({len(lines)} packages, "
            "micromamba list --explicit --md5):"
        )
        for line in lines:
            print(f"      {line}")
        if explicit.returncode != 0 or not lines:
            print(tail(explicit.stdout + explicit.stderr))
            raise CheckFailedError("micromamba list --explicit --md5 gave no packages")

    # -- 3 -----------------------------------------------------------------

    def prune(self) -> None:
        with self.step("3", "prune what an installer would not ship"):
            if self.reused("pruned"):
                pruned = self.state["pruned"]
            else:
                before = dir_size(self.env_dir)
                symbols = list(self.env_dir.rglob("*.pdb"))
                removed = sum(path.stat().st_size for path in symbols)
                for path in symbols:
                    path.unlink()
                after = dir_size(self.env_dir)
                pruned = {
                    "before": before,
                    "removed": removed,
                    "files": len(symbols),
                    "after": after,
                }
                self.state["pruned"] = pruned
                self.save_state()
            print(f"    before: {mb(pruned['before'])} MB")
            print(
                f"    debug symbols removed: {mb(pruned['removed'])} MB "
                f"({pruned['files']} .pdb files)"
            )
            print(f"    after:  {mb(pruned['after'])} MB   <- the installer payload")
            print(f"    payload by directory: {payload_breakdown(self.env_dir)}")
            self.verdict["installer payload (pruned):"] = f"{mb(pruned['after'])} MB"

    # -- 4 -----------------------------------------------------------------

    def initdb_args(self, target: Path, *, defaults: bool = False) -> list[Any]:
        args: list[Any] = [
            self.tool("initdb"),
            "-D",
            target,
            "-U",
            DB_USER,
            "--auth=scram-sha-256",
            f"--pwfile={self.pwfile}",
        ]
        if not defaults:
            args += [f"--encoding={ENCODING}", f"--locale={LOCALE}"]
        return args

    @staticmethod
    def announcement(output: str) -> list[str]:
        """The lines in which initdb says what it chose: the quoted ones, minus
        its closing hint on how to start the server."""
        return [
            line.strip()
            for line in output.splitlines()
            if '"' in line and "pg_ctl" not in line.replace("^", "")  # cmd escapes
        ]

    def initdb(self) -> None:
        with self.step("4", "initdb (scram auth, password file)"):
            if self.reused("initdb"):
                self.password = self.pwfile.read_text(encoding="ascii").strip()
                initdb = self.state["initdb"]
                print(f"    rc=0  cluster: {initdb['cluster_mb']} MB")
                print(f"    took {initdb['seconds']:.2f} s when it was built")
                for line in initdb["announced"]:
                    print(f"    initdb: {line}")
                self.verdict["initdb (once, per machine):"] = (
                    f"{initdb['seconds']:.1f} s (reused, --keep)"
                )
                return
            self.password = secrets.token_urlsafe(24)
            self.pwfile.write_text(self.password + "\n", encoding="ascii")
            print(
                f"    flags: --auth=scram-sha-256 --pwfile --encoding={ENCODING} "
                f"--locale={LOCALE}"
            )
            started = time.perf_counter()
            done = run(self.initdb_args(self.pgdata), env=self.pg_env(), timeout=900)
            seconds = time.perf_counter() - started
            cluster = mb(dir_size(self.pgdata)) if self.pgdata.exists() else 0
            print(f"    rc={done.returncode}  cluster: {cluster} MB")
            announced = self.announcement(done.stdout)
            for line in announced:
                print(f"    initdb: {line}")
            if done.returncode != 0:
                print(tail(done.stdout + done.stderr))
                raise CheckFailedError(f"initdb rc={done.returncode}")
            self.state["initdb"] = {
                "seconds": seconds,
                "cluster_mb": cluster,
                "announced": announced,
            }
            self.save_state()
            self.verdict["initdb (once, per machine):"] = f"{seconds:.1f} s"

    def initdb_defaults(self) -> None:
        """Section 5: what initdb picks on this machine when not told."""
        title = "initdb without --encoding or --locale (--initdb-defaults)"
        with self.step("4b", title):
            target = self.work / "pgdata-defaults"
            shutil.rmtree(target, ignore_errors=True)
            done = run(
                self.initdb_args(target, defaults=True), env=self.pg_env(), timeout=900
            )
            print(f"    rc={done.returncode}")
            for line in self.announcement(done.stdout):
                print(f"    initdb: {line}")
            if done.returncode != 0:
                print(tail(done.stdout + done.stderr))
                raise CheckFailedError(f"initdb (defaults) rc={done.returncode}")
            port = free_port()
            self.start(target, port, self.work / "server-defaults.log")
            try:
                with psycopg.connect(
                    host=LOOPBACK,
                    port=port,
                    user=DB_USER,
                    password=self.password,
                    dbname="postgres",
                    autocommit=True,
                ) as connection:
                    chosen = database_locale(connection, "template1")
            finally:
                self.stop(target)
            shutil.rmtree(target, ignore_errors=True)
            print(
                f"    chosen without flags: encoding {chosen['encoding']}, "
                f"provider {chosen['provider']}, datcollate {chosen['datcollate']}, "
                f"datctype {chosen['datctype']}"
            )
            self.verdict["initdb without flags chooses:"] = (
                f"{chosen['encoding']} / {chosen['datcollate']}"
            )

    # -- 5 -----------------------------------------------------------------

    def configure(self, target: Path, port: int) -> None:
        conf = target / "postgresql.conf"
        include = f"include_if_exists = '{CONF_FILE}'"
        if include not in conf.read_text(encoding="utf-8"):
            with conf.open("a", encoding="utf-8") as handle:
                handle.write(f"\n# RFC-033a measurement settings\n{include}\n")
        (target / CONF_FILE).write_text(
            f"listen_addresses = '{LOOPBACK}'\n"
            f"port = {port}\n"
            "log_line_prefix = '%m [%p] '\n"
            "logging_collector = off\n",
            encoding="utf-8",
        )

    def start(self, target: Path, port: int, server_log: Path) -> None:
        self.configure(target, port)
        server_log.unlink(missing_ok=True)
        rc, output = run_to_file(
            [
                self.tool("pg_ctl"),
                "-D",
                target,
                "-l",
                server_log,
                "-w",
                "-t",
                "120",
                "start",
            ],
            self.work / "pg_ctl-start.out",
            env=self.pg_env(),
        )
        if (target / "postmaster.pid").exists():
            self.running.append(target)
        if rc != 0:
            print(tail(output))
            if server_log.exists():
                print(tail(server_log.read_text(encoding="utf-8", errors="replace")))
            raise CheckFailedError(f"pg_ctl start rc={rc}")

    def stop(self, target: Path) -> str:
        if not (target / "postmaster.pid").exists():
            if target in self.running:
                self.running.remove(target)
            return "was not running"
        rc, output = run_to_file(
            [self.tool("pg_ctl"), "-D", target, "-m", "fast", "-w", "-t", "60", "stop"],
            self.work / "pg_ctl-stop.out",
            env=self.pg_env(),
        )
        if rc != 0:
            rc, output = run_to_file(
                [self.tool("pg_ctl"), "-D", target, "-m", "immediate", "-w", "stop"],
                self.work / "pg_ctl-stop.out",
                env=self.pg_env(),
            )
            if rc != 0:
                return f"pg_ctl stop rc={rc}: {output.strip()}"
            if target in self.running:
                self.running.remove(target)
            return "stopped (fast shutdown failed, immediate used)"
        if target in self.running:
            self.running.remove(target)
        return "stopped"

    def start_cluster(self) -> None:
        with self.step("5", "start on loopback, high port"):
            self.port = free_port()
            server_log = self.work / "server.log"
            self.start(self.pgdata, self.port, server_log)
            print(f"    port: {self.port} on {LOOPBACK} (asked free of the OS)")
            print("    log_line_prefix = '%m [%p] ' (milliseconds)")
            interval = listening_to_ready(server_log, self.port)
            print(
                f"    listening -> ready: {interval:.3f} s  (from the server's own log)"
            )
            with self.connect("postgres") as connection:
                version = connection.execute("SELECT version()").fetchone()[0]
                connection.execute(f"DROP DATABASE IF EXISTS {DB_NAME}")
                connection.execute(f"CREATE DATABASE {DB_NAME}")
            print(f"    {version}")
            print(f"    database {DB_NAME}: created fresh")
            for marker in EXPECTED_SERVER:
                if marker not in version:
                    raise CheckFailedError(
                        f"server version lacks {marker!r}: {version}"
                    )
            self.verdict["listening -> ready:"] = f"{interval:.3f} s"
            self.verdict["port:"] = f"{self.port} ({LOOPBACK}, asked free of the OS)"

    # -- 6 -----------------------------------------------------------------

    def psql(self, sql: str) -> subprocess.CompletedProcess[str]:
        return run(
            [
                self.tool("psql"),
                "-X",
                "-q",
                "-v",
                "ON_ERROR_STOP=1",
                "-h",
                LOOPBACK,
                "-p",
                self.port,
                "-U",
                DB_USER,
                "-d",
                DB_NAME,
                "-c",
                sql,
            ],
            env=self.pg_env(),
            timeout=120,
        )

    def extension(self) -> None:
        with self.step("6", "CREATE EXTENSION vector, then the RFC-018 index"):
            created = self.psql("CREATE EXTENSION vector")
            print(
                f"    CREATE EXTENSION rc={created.returncode} {created.stderr.strip()}"
            )
            if created.returncode != 0:
                raise CheckFailedError("CREATE EXTENSION vector failed")
            with self.connect() as connection:
                name, version = connection.execute(
                    "SELECT extname, extversion FROM pg_extension "
                    "WHERE extname = 'vector'"
                ).fetchone()
                distance = connection.execute(
                    "SELECT '[1,2,3]'::vector <=> '[3,2,1]'::vector"
                ).fetchone()[0]
            print(f"    installed: {name} {version}")
            print(f"    '<=>' cosine distance: {distance}")
            indexed = self.psql(
                "CREATE TEMP TABLE rfc033a_probe (embedding vector(3)); "
                "INSERT INTO rfc033a_probe VALUES ('[1,2,3]'), ('[3,2,1]'); "
                "CREATE INDEX ON rfc033a_probe USING hnsw "
                "(embedding vector_cosine_ops)"
            )
            print(
                f"    HNSW vector_cosine_ops rc={indexed.returncode} "
                f"{indexed.stderr.strip()}"
            )
            if indexed.returncode != 0:
                raise CheckFailedError("HNSW index with vector_cosine_ops failed")

    # -- 7a ----------------------------------------------------------------

    def alembic(self) -> None:
        with self.step("7a", "alembic upgrade head"):
            started = time.perf_counter()
            done = run(
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                cwd=BACKEND,
                env=self.project_env(),
                encoding="utf-8",
                timeout=600,
            )
            seconds = time.perf_counter() - started
            print(f"    rc={done.returncode}")
            if done.returncode != 0:
                print(tail(done.stdout + done.stderr))
                raise CheckFailedError(f"alembic upgrade head rc={done.returncode}")
            with self.connect() as connection:
                head = connection.execute(
                    "SELECT version_num FROM alembic_version"
                ).fetchone()[0]
                tables = [
                    row[0]
                    for row in connection.execute(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = 'public' ORDER BY 1"
                    )
                ]
                indexes = [
                    row[0]
                    for row in connection.execute(
                        "SELECT indexname FROM pg_indexes WHERE tablename = 'images' "
                        "AND indexdef LIKE '%USING hnsw%vector_cosine_ops%' "
                        "ORDER BY 1"
                    )
                ]
                columns = [
                    row[0]
                    for row in connection.execute(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'images' AND column_name = ANY(%s) "
                        "ORDER BY 1",
                        [list(RFC032_COLUMNS)],
                    )
                ]
                checks = [
                    row[0]
                    for row in connection.execute(
                        "SELECT conname FROM pg_constraint "
                        "WHERE conrelid = 'images'::regclass AND contype = 'c' "
                        "AND conname = ANY(%s) ORDER BY 1",
                        [list(RFC032_CHECKS)],
                    )
                ]
            print(f"    head:    {head}  (expected {EXPECTED_HEAD})")
            print(f"    tables:  {', '.join(tables)}")
            print(f"    index:   {', '.join(indexes) or 'NONE'}")
            print(f"    RFC-032 columns: {', '.join(columns) or 'NONE'}")
            print(f"    RFC-032 CHECKs:  {', '.join(checks) or 'NONE'}")
            print(f"    upgrade took {seconds:.2f} s")
            if head != EXPECTED_HEAD:
                raise CheckFailedError(f"head {head}, expected {EXPECTED_HEAD}")
            if tuple(sorted(columns)) != tuple(sorted(RFC032_COLUMNS)):
                raise CheckFailedError(f"RFC-032 columns: {columns}")
            if tuple(checks) != RFC032_CHECKS:
                raise CheckFailedError(f"RFC-032 CHECKs: {checks}")
            if not indexes:
                raise CheckFailedError("no HNSW vector_cosine_ops index on images")
            self.verdict["alembic upgrade head:"] = f"{seconds:.1f} s -> {head}"

    # -- 7b ----------------------------------------------------------------

    PROBE = (
        "import json\n"
        "from sqlalchemy import text\n"
        "from app.infrastructure.config.settings import settings\n"
        "from app.infrastructure.persistence.engine import EngineInstance\n"
        "with EngineInstance.connect() as connection:\n"
        "    port, version = connection.execute(\n"
        "        text('SELECT inet_server_port(), version()')\n"
        "    ).one()\n"
        "print('RFC033A-PROBE ' + json.dumps({\n"
        "    'settings': f'{settings.database_host}:{settings.database_port}'\n"
        "                f'/{settings.database_name}',\n"
        "    'port': port, 'version': version}))\n"
    )
    """Section 4.2: the project's own `settings` and engine, in a subprocess
    with exactly the environment the suite gets."""

    def xact_commit(self) -> int:
        with self.connect() as connection:
            return int(
                connection.execute(
                    "SELECT xact_commit FROM pg_stat_database WHERE datname = %s",
                    [DB_NAME],
                ).fetchone()[0]
            )

    def suite(self) -> None:
        with self.step("7b", "full default pytest suite against this cluster"):
            probe = run(
                [sys.executable, "-c", self.PROBE],
                cwd=BACKEND,
                env=self.project_env(),
                encoding="utf-8",
                timeout=300,
            )
            found = [
                line.split(" ", 1)[1]
                for line in probe.stdout.splitlines()
                if line.startswith("RFC033A-PROBE ")
            ]
            if probe.returncode != 0 or not found:
                print(tail(probe.stdout + probe.stderr))
                raise CheckFailedError("the target probe did not answer")
            target = json.loads(found[0])
            print(
                f"    target, as the project sees it: settings -> {target['settings']}"
            )
            print(f"      inet_server_port() = {target['port']}")
            print(f"      version() = {target['version']}")
            if target["port"] != self.port or not all(
                marker in target["version"] for marker in EXPECTED_SERVER
            ):
                raise CheckFailedError(
                    "the project's settings do not reach this cluster: the suite "
                    "would pass against another database (section 4.2)"
                )
            print(f"      -> this cluster (port {self.port}, 16.15, Visual C++)")
            before = self.xact_commit()
            done = run(
                [sys.executable, "-m", "pytest"],
                cwd=REPO,
                env=self.project_env(),
                encoding="utf-8",
                timeout=3600,
            )
            time.sleep(1.0)  # let the suite's backends flush their counters
            after = self.xact_commit()
            summary = next(
                (
                    line.strip("= ").strip()
                    for line in reversed(done.stdout.splitlines())
                    if re.search(r"\d+ (passed|failed|error)", line)
                ),
                "no summary line",
            )
            counts = {
                word.rstrip("s"): int(number)
                for number, word in re.findall(r"(\d+) (\w+)", summary)
            }
            print(f"    rc={done.returncode}")
            print(f"    {summary}")
            print(
                f"    counted: passed {counts.get('passed', 0)}, deselected "
                f"{counts.get('deselected', 0)}, failed {counts.get('failed', 0)}, "
                f"errors {counts.get('error', 0)}"
            )
            print(
                f"    xact_commit on this cluster: {before} -> {after} "
                f"(+{after - before})"
            )
            if done.returncode != 0:
                for line in done.stdout.splitlines():
                    if line.startswith(("FAILED ", "ERROR ")):
                        print(f"      {line}")
            if after - before <= XACT_FLOOR:
                raise CheckFailedError(
                    f"xact_commit rose by {after - before}: the suite did not talk "
                    "to this cluster (section 4.2)"
                )
            self.verdict["suite:"] = (
                f"{'PASSED' if done.returncode == 0 else 'FAILED'} "
                f"({counts.get('passed', 0)} passed, "
                f"{counts.get('deselected', 0)} deselected, "
                f"{counts.get('failed', 0)} failed)"
            )
            self.verdict["suite target:"] = (
                f"proven: port {target['port']}, xact_commit +{after - before}"
            )
            if done.returncode != 0:
                raise CheckFailedError(f"pytest rc={done.returncode}")

    # -- 8 -----------------------------------------------------------------

    def facts(self) -> dict[str, str]:
        with self.step("8", "facts for the parity table (section 6)"):
            with self.connect() as connection:
                facts = read_facts(connection)
                icu = icu_support(connection)
            for name, value in facts.items():
                print(f"    {name + ':':<32} {value}")
            print(f"    {'ICU provider in this build:':<32} {icu}")
            locale_row = facts["provider, datcollate, datctype"]
            self.verdict["encoding, locale:"] = f"{facts['encoding']}, {locale_row}"
            return facts


def payload_breakdown(env_dir: Path) -> str:
    """What the payload is made of: only .pdb files are removed, nothing else."""
    sizes: dict[str, int] = {}
    for path in env_dir.rglob("*"):
        if path.is_file():
            parts = path.relative_to(env_dir).parts
            key = "/".join(parts[:2]) if parts[0] == "Library" else parts[0]
            if len(parts) == 1:
                key = "(top-level files)"
            sizes[key] = sizes.get(key, 0) + path.stat().st_size
    ordered = sorted(sizes.items(), key=lambda item: -item[1])
    return ", ".join(f"{name} {size / (1024 * 1024):.1f}" for name, size in ordered)


def listening_to_ready(server_log: Path, port: int) -> float:
    stamp = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3})")
    listening = ready = None
    for line in server_log.read_text(encoding="utf-8", errors="replace").splitlines():
        found = stamp.match(line)
        if not found:
            continue
        moment = datetime.datetime.strptime(found.group(1), "%Y-%m-%d %H:%M:%S.%f")
        if f'listening on IPv4 address "{LOOPBACK}", port {port}' in line:
            listening = moment
        elif "database system is ready to accept connections" in line:
            ready = moment
    if listening is None or ready is None:
        raise CheckFailedError(f"listening/ready not found in {server_log}")
    return (ready - listening).total_seconds()


PROVIDERS = {"c": "libc", "i": "icu", "b": "builtin"}


def database_locale(connection: psycopg.Connection[Any], dbname: str) -> dict[str, str]:
    encoding, provider, collate, ctype = connection.execute(
        "SELECT pg_encoding_to_char(encoding), datlocprovider, datcollate, datctype "
        "FROM pg_database WHERE datname = %s",
        [dbname],
    ).fetchone()
    return {
        "encoding": encoding,
        "provider": PROVIDERS.get(provider, provider),
        "datcollate": collate,
        "datctype": ctype,
    }


def icu_support(connection: psycopg.Connection[Any]) -> str:
    """Section 5's ICU row, asked of the server rather than of the recipe."""
    try:
        with connection.transaction(force_rollback=True):
            connection.execute(
                "CREATE COLLATION rfc033a_icu_probe (provider = icu, locale = 'und')"
            )
    except psycopg.Error as error:
        return f"no ({str(error).strip()})"
    return "yes"


def read_facts(connection: psycopg.Connection[Any]) -> dict[str, str]:
    """Section 6, one value per field, read the same way from either database."""
    version_num = int(
        connection.execute("SELECT current_setting('server_version_num')").fetchone()[0]
    )
    minor = connection.execute("SELECT current_setting('server_version')").fetchone()[0]
    pgvector = connection.execute(
        "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
    ).fetchone()
    dbname = connection.execute("SELECT current_database()").fetchone()[0]
    own = database_locale(connection, dbname)
    ordered = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM unnest(%s::text[]) AS probe(name) ORDER BY name",
            [list(COLLATION_PROBE)],
        )
    ]
    lowered = [
        row[0]
        for row in connection.execute(
            "SELECT lower(name) FROM unnest(%s::text[]) WITH ORDINALITY "
            "AS probe(name, position) ORDER BY position",
            [list(COLLATION_PROBE)],
        )
    ]
    try:
        with connection.transaction():
            connection.execute(
                "CREATE TEMP TABLE rfc033a_path (relative_path varchar) "
                "ON COMMIT DROP"
            )
            connection.execute("INSERT INTO rfc033a_path VALUES (%s)", [WIN1252_PROBE])
            text, raw = connection.execute(
                "SELECT relative_path, convert_to(relative_path, 'UTF8') "
                "FROM rfc033a_path"
            ).fetchone()
        expected = WIN1252_PROBE.encode("utf-8")
        path = (
            "byte-identical"
            if text == WIN1252_PROBE and bytes(raw) == expected
            else f"differs: {text!r}"
        )
    except (psycopg.Error, UnicodeError) as error:
        path = f"refused: {type(error).__name__}: {str(error).strip()[:80]}"
    connection.execute("SELECT '[1]'::vector")  # loads the library: its GUCs exist
    ef_search = connection.execute("SHOW hnsw.ef_search").fetchone()[0]
    head = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    return {
        "major version": str(version_num // 10000),
        "minor version": minor,
        "pgvector": pgvector[0] if pgvector else "not installed",
        "encoding": own["encoding"],
        "provider, datcollate, datctype": (
            f"{own['provider']}, {own['datcollate']}, {own['datctype']}"
        ),
        "ordering": " | ".join(ordered),
        "lower()": " | ".join(lowered),
        "path outside WIN1252": path,
        "hnsw.ef_search": str(ef_search),
        "alembic head": head[0] if head else "none",
    }


def short(value: str) -> str:
    if len(value) <= 34:
        return value
    return f"md5:{hashlib.md5(value.encode('utf-8')).hexdigest()[:12]}"


def parity(embedded: dict[str, str], port: int) -> bool:
    sys.path.insert(0, str(BACKEND))
    from app.infrastructure.config.settings import Settings

    settings = Settings()
    print()
    print("--- parity: embedded against development (section 6)")
    print(
        f"    development, as the project's .env sees it: {settings.database_host}:"
        f"{settings.database_port}/{settings.database_name}"
    )
    print("    accepted divergences, declared before the measurement (6.1):")
    for line in ACCEPTED_DIVERGENCES:
        print(f"      - {line}")
    with psycopg.connect(
        host=settings.database_host,
        port=settings.database_port,
        user=settings.database_user,
        password=settings.database_password,
        dbname=settings.database_name,
        autocommit=True,
    ) as connection:
        server_port, version = connection.execute(
            "SELECT inet_server_port(), version()"
        ).fetchone()
        if "Visual C++" in version and server_port == port:
            raise CheckFailedError(
                "the development side is the embedded cluster itself"
            )
        print(f"    development server: {version}")
        development = read_facts(connection)
    print()
    print(f"    {'field':<32} {'embedded':<34} {'development':<34} verdict")
    defects = []
    for rule in PARITY_RULES:
        ours, theirs = embedded[rule.name], development[rule.name]
        if rule.kind == "recorded":
            verdict = "recorded (6.1)"
        elif rule.kind == "both":
            verdict = "OK" if ours == theirs == rule.expected else "DEFECT"
        else:
            verdict = "OK" if ours == theirs else "DEFECT"
        if verdict == "DEFECT":
            defects.append(rule.name)
        print(f"    {rule.name:<32} {short(ours):<34} {short(theirs):<34} {verdict}")
    for rule in PARITY_RULES:
        ours, theirs = embedded[rule.name], development[rule.name]
        if rule.name in defects and (len(ours) > 34 or len(theirs) > 34):
            print()
            print(f"    {rule.name}, in full:")
            print(f"      embedded:    {ours}")
            print(f"      development: {theirs}")
    print()
    if defects:
        print(f"    PARITY: {len(defects)} defect(s): {'; '.join(defects)}")
    else:
        print("    PARITY: every row equal, except the divergences accepted in 6.1")
    return not defects


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--work", type=Path, help="work directory (default: a new temporary one)"
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="keep binaries and cluster, and reuse them: steps 1 to 4 are not redone",
    )
    parser.add_argument(
        "--initdb-defaults",
        action="store_true",
        help="an extra initdb without encoding or locale flags (section 5)",
    )
    parser.add_argument(
        "--parity",
        action="store_true",
        help="compare the facts with the development database (section 6)",
    )
    parser.add_argument(
        "--fail-at",
        choices=STEP_IDS,
        help="force this step to fail, to check that nothing is left behind",
    )
    args = parser.parse_args()

    if args.work is None:
        work = Path(tempfile.mkdtemp(prefix="rfc033a-"))
        owns_work = True
    else:
        work = args.work.resolve()
        owns_work = not work.exists()
        work.mkdir(parents=True, exist_ok=True)
        if not args.keep and any(work.iterdir()):
            print(f"{work} is not empty: pass --keep to reuse it, or choose another")
            return 2
    baseline = Baseline(
        work=work, keep=args.keep, fail_at=args.fail_at, owns_work=owns_work
    )
    state_path = work / STATE_FILE
    if args.keep and state_path.exists():
        baseline.state = json.loads(state_path.read_text(encoding="utf-8"))
    else:
        baseline.state = {
            "built": datetime.datetime.now().astimezone().isoformat(timespec="seconds")
        }

    docker_running, docker = docker_status()
    print("=" * 72)
    print("RFC-033a baseline: PostgreSQL + pgvector on Windows, without Docker")
    print("=" * 72)
    print(f"when:     {datetime.datetime.now().astimezone():%Y-%m-%d %H:%M:%S %z}")
    print(f"work dir: {work}{' (--keep)' if args.keep else ''}")
    print(f"repo:     {REPO}")
    print(f"commit:   {git_commit()}")
    print(f"head:     {EXPECTED_HEAD} expected (RFC-032)")
    print()
    print("conditions")
    print(f"    platform:   {sys.platform}, {platform.machine()}")
    print(f"    python:     {platform.python_version()}")
    print(f"    cpu:        {platform.processor()} x{os.cpu_count()}")
    print(f"    docker:     {docker}")
    print(f"    storage:    {storage_of(work)}; files written by this run, so warm")
    os_locale = locale.setlocale(locale.LC_CTYPE)  # a query: changes nothing
    print(f"    os locale:  {os_locale} (what initdb inherits without flags)")

    status = 0
    stops: list[str] = []
    try:
        baseline.fetch_micromamba()
        baseline.materialize()
        baseline.prune()
        baseline.initdb()
        if args.initdb_defaults:
            baseline.initdb_defaults()
        baseline.start_cluster()
        baseline.extension()
        baseline.alembic()
        baseline.suite()
        facts = baseline.facts()
        if args.parity and not parity(facts, baseline.port):
            status = 1
    except (CheckFailedError, ForcedFailureError) as error:
        print()
        print(f"ABORTED: {error}")
        status = 1
    except Exception as error:  # anything else: the finally below must still run
        print()
        print(f"ABORTED: {type(error).__name__}: {error}")
        status = 1
    finally:
        sys.stdout.flush()
        for target in dict.fromkeys([*baseline.running, baseline.pgdata]):
            if (target / "postmaster.pid").exists():
                stops.append(f"{target.name} {baseline.stop(target)}")
        leftover = postgres_processes()
        ours = [
            (pid, path)
            for pid, path in leftover
            if path and Path(path).resolve().is_relative_to(work)
        ]
        others = [entry for entry in leftover if entry not in ours]

    print()
    print("=" * 72)
    print("VERDICT")
    print("=" * 72)
    for name in VERDICT_ORDER:
        if name in baseline.verdict:
            print(f"  {name:<30}{baseline.verdict[name]}")
    if not docker_running:
        docker_verdict = "not running, not required"
    elif "suite target:" in baseline.verdict:
        docker_verdict = "running -- the suite's target was proven, not assumed (4.2)"
    else:
        docker_verdict = "running"
    print(f"  {'Docker:':<30}{docker_verdict}")
    if stops == [f"{baseline.pgdata.name} stopped"]:
        print("  cluster stopped.")
    else:
        print(f"  {'clusters:':<30}{'; '.join(stops) or 'none was running'}")
    print(
        f"  {'postgres.exe left behind:':<30}"
        + (", ".join(f"pid {pid} {path}" for pid, path in ours) or "none")
    )
    if others:
        print(
            f"  {'postgres.exe not ours:':<30}"
            + ", ".join(f"pid {pid} {path or '?'}" for pid, path in others)
        )
    if ours:
        status = 1
    if args.keep:
        print(f"  kept: {work}")
    else:
        shutil.rmtree(work, ignore_errors=True)
        if work.exists():
            print(f"  NOT fully removed: {work}")
            status = 1
        else:
            print(f"  removed: {work}")
        if not owns_work:
            work.mkdir()
    print(f"  exit status: {status}")
    return status


if __name__ == "__main__":
    sys.exit(main())
