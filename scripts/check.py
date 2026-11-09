"""Every check of a change, in one command (RFC-048).

    python scripts/check.py                  # everything
    python scripts/check.py python           # ruff, black and mypy
    python scripts/check.py frontend         # eslint, tsc, prettier and vitest
    python scripts/check.py mypy pytest      # two steps
    python scripts/check.py --keep-going     # everything, without stopping at a failure

The steps run in a fixed order, whatever order they are typed in, and the
command stops at the first one that fails:

    env       the venv against both lockfiles, the Python and the Node (4.3)
    ruff      python -m ruff check --no-fix backend scripts       (python)
    black     python -m black --check backend scripts             (python)
    mypy      python -m mypy                                      (python)
    eslint    npm run lint            in frontend/                (frontend)
    tsc       npm run typecheck       in frontend/                (frontend)
    prettier  npm run format:check    in frontend/                (frontend)
    vitest    npm test                in frontend/                (frontend)
    pytest    python -m pytest, after a connection to the database (4.4)

`python` is the interpreter running this file, never one from `PATH`: run it
with the venv's (`.venv\\Scripts\\python.exe`, or `python` once activated).
The fast checks come first, so a type error shows in seconds and not after
the suite; pytest is last, and is the only step that needs a database.

This command starts no database. Without one, the `pytest` step fails at its
test connection, in seconds, and says how to get one: Docker, or the
embedded cluster around the whole command, with `backend` on `PYTHONPATH`:

    python -m app.infrastructure.database.embedded run -- python scripts/check.py

The CI calls this command (RFC-049, RFC-050) rather than a list of its own.
The exit code is 0 when every step passed, 1 when one failed, and 2 for a
name this command does not know.
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
FRONTEND = REPO / "frontend"
NODE_VERSION_FILE = FRONTEND / ".node-version"

PYTHON_VERSION = "3.12.9"
"""The package's (RFC-041 section 2.8), which black runs on (RFC-048 D5)."""

PYTHON_PATHS = ("backend", "scripts")
"""What ruff and black check. `experiments/` is the record of measurements
that already ran, and stays as it was written (RFC-048 section 2.2)."""


@dataclass(frozen=True)
class Step:
    name: str
    group: str | None
    kind: str
    """`env`, `python` (`python -m ...` at the root), `npm` (in `frontend/`)
    or `pytest`."""
    arguments: tuple[str, ...] = ()


STEPS = (
    Step("env", None, "env"),
    Step("ruff", "python", "python", ("ruff", "check", "--no-fix", *PYTHON_PATHS)),
    Step("black", "python", "python", ("black", "--check", *PYTHON_PATHS)),
    Step("mypy", "python", "python", ("mypy",)),
    Step("eslint", "frontend", "npm", ("run", "lint")),
    Step("tsc", "frontend", "npm", ("run", "typecheck")),
    Step("prettier", "frontend", "npm", ("run", "format:check")),
    Step("vitest", "frontend", "npm", ("test",)),
    Step("pytest", None, "pytest", ("pytest",)),
)
BY_NAME = {step.name: step for step in STEPS}
GROUPS = {
    group: tuple(step.name for step in STEPS if step.group == group)
    for group in ("python", "frontend")
}
PYTHON_STEPS = frozenset(s.name for s in STEPS if s.kind in ("python", "pytest"))
FRONTEND_STEPS = frozenset(s.name for s in STEPS if s.kind == "npm")


class UsageError(Exception):
    """A name that is neither a step nor a group."""


# --------------------------------------------------------------------------
# What the steps reach outside this process
# --------------------------------------------------------------------------


Run = Callable[[Sequence[str], Path, Mapping[str, str]], int]


@dataclass(frozen=True)
class Machine:
    """Everything a check asks of the machine.

    Replaced in the tests, which run the command with doubles and look at
    what each step was given.
    """

    run: Run
    """(command, folder, environment) -> exit code, the output on the console."""
    python: str
    python_version: str
    in_venv: bool
    installed: Callable[[], dict[str, str]]
    """Canonical name -> version, of the running interpreter."""
    locks: Callable[[], dict[str, list[Any]]]
    """Lockfile name -> its `vendor_python.Locked` entries."""
    node_version: Callable[[], str | None]
    """`node --version`, or None without a `node`."""
    npm: str | None
    node_modules: Path
    expected_node: Callable[[], str]
    """The major version in `frontend/.node-version`."""
    database: Callable[[], tuple[bool, str]]
    """(connected, what to say): one test connection, by the app's engine."""
    environment: Mapping[str, str]
    clock: Callable[[], float]


def _vendor_python() -> Any:
    """`vendor_python`, imported only for the Python steps: the frontend's
    checks run with any Python 3 and no venv (RFC-049's job is Linux)."""
    scripts = str(Path(__file__).resolve().parent)
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import vendor_python

    return vendor_python


def _run(command: Sequence[str], folder: Path, environment: Mapping[str, str]) -> int:
    sys.stdout.flush()
    sys.stderr.flush()
    return subprocess.run(
        list(command), cwd=folder, env=dict(environment), stdin=subprocess.DEVNULL
    ).returncode


def _installed() -> dict[str, str]:
    versions: dict[str, str] = _vendor_python().venv_versions()
    return versions


def _locks() -> dict[str, list[Any]]:
    vendor = _vendor_python()
    return {
        path.name: vendor.read_lock(path)
        for path in (vendor.LOCKFILE, vendor.DEV_LOCKFILE)
    }


def _node_version() -> str | None:
    node = shutil.which("node")
    if node is None:
        return None
    completed = subprocess.run(
        [node, "--version"],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=60,
    )
    return completed.stdout.strip() if completed.returncode == 0 else None


def _expected_node() -> str:
    return NODE_VERSION_FILE.read_text(encoding="utf-8").strip()


DATABASE_PROBE = """
import sys
from sqlalchemy import text
from app.infrastructure.config.settings import settings
from app.infrastructure.persistence.engine import EngineInstance
print(f"check.py:source DATABASE_SOURCE={settings.database_source.value}")
try:
    with EngineInstance.connect() as connection:
        connection.execute(text("SELECT 1"))
except Exception as error:
    said = str(error).strip()
    print(f"check.py:error {said.splitlines()[0] if said else repr(error)}")
    sys.exit(1)
"""
"""One connection, the way the suite's `conftest.py` makes them. The lines
this command reads are marked: the app's logger may write to the console."""

PROBE_SECONDS = 120


def pytest_environment(environment: Mapping[str, str]) -> dict[str, str]:
    """`WARM_UP_MODELS=false`, which `pydantic-settings` puts above the `.env`
    (RFC-004): two tests read it from the `.env` of whoever runs them
    (RFC-048 section 2.4)."""
    return {**environment, "WARM_UP_MODELS": "false"}


def _probe_environment(environment: Mapping[str, str]) -> dict[str, str]:
    paths = [str(REPO / "backend")]
    if environment.get("PYTHONPATH"):
        paths.append(environment["PYTHONPATH"])
    return {
        **pytest_environment(environment),
        "PYTHONPATH": os.pathsep.join(paths),
        "PYTHONIOENCODING": "utf-8",
    }


def _database() -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            [sys.executable, "-c", DATABASE_PROBE],
            cwd=REPO,
            env=_probe_environment(os.environ),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=PROBE_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return False, f"no answer in {PROBE_SECONDS} s"
    marked = {}
    for line in completed.stdout.splitlines():
        key, _, value = line.partition(" ")
        if key in ("check.py:source", "check.py:error"):
            marked[key] = value.strip()
    source = marked.get("check.py:source", "")
    if completed.returncode == 0:
        return True, source
    error = marked.get("check.py:error")
    if error is None:  # the app did not even load: its own last words
        tail = completed.stderr.strip().splitlines()
        error = tail[-1] if tail else f"exit code {completed.returncode}"
    return False, f"{source} {error}".strip()


def default_machine() -> Machine:
    return Machine(
        run=_run,
        python=sys.executable,
        python_version=platform.python_version(),
        in_venv=sys.prefix != sys.base_prefix,
        installed=_installed,
        locks=_locks,
        node_version=_node_version,
        npm=shutil.which("npm"),
        node_modules=FRONTEND / "node_modules",
        expected_node=_expected_node,
        database=_database,
        environment=dict(os.environ),
        clock=time.perf_counter,
    )


# --------------------------------------------------------------------------
# Choosing the steps
# --------------------------------------------------------------------------


def choose(names: Sequence[str]) -> list[Step]:
    """The steps the names ask for, in the table's order, `env` first.

    No name is every step. A name that is neither a step nor a group is
    refused before anything runs.
    """
    unknown = [name for name in names if name not in BY_NAME and name not in GROUPS]
    if unknown:
        known = ", ".join([*GROUPS, *BY_NAME])
        raise UsageError(f"unknown: {', '.join(unknown)}. Known: {known}")
    if not names:
        return list(STEPS)
    wanted = {"env"}
    for name in names:
        wanted.update(GROUPS.get(name, (name,)))
    return [step for step in STEPS if step.name in wanted]


# --------------------------------------------------------------------------
# The steps
# --------------------------------------------------------------------------


def check_environment(chosen: Sequence[Step], machine: Machine) -> list[str]:
    """Section 4.3: only what the chosen steps use. Returns the problems."""
    names = {step.name for step in chosen}
    problems: list[str] = []
    if names & PYTHON_STEPS:
        problems += _python_problems(machine)
    if names & FRONTEND_STEPS:
        problems += _frontend_problems(machine)
    return problems


def _python_problems(machine: Machine) -> list[str]:
    problems = []
    if not machine.in_venv:
        problems.append(
            f"{machine.python} is not a venv: make one with scripts/make_venv.py"
        )
    if machine.python_version != PYTHON_VERSION:
        problems.append(
            f"Python is {machine.python_version}, the checks run on {PYTHON_VERSION}"
        )
    vendor = _vendor_python()
    try:
        locks = machine.locks()
    except vendor.VendorError as error:
        return [*problems, str(error)]
    report = vendor.compare_venv_with_locks(machine.installed(), locks)
    problems += report.problems
    print(f"env: {sum(len(e) for e in locks.values())} packages of {', '.join(locks)}")
    if report.extra:
        print(f"env: in no lockfile, and allowed: {', '.join(report.extra)}")
    return problems


def _frontend_problems(machine: Machine) -> list[str]:
    problems = []
    expected = machine.expected_node()
    found = machine.node_version()
    if found is None:
        problems.append(f"no node on PATH: install Node {expected}")
    elif found.lstrip("v").split(".")[0] != expected.split(".")[0]:
        problems.append(f"node is {found}, frontend/.node-version says {expected}")
    if machine.npm is None:
        problems.append("no npm on PATH")
    if not machine.node_modules.is_dir():
        problems.append("frontend/node_modules is missing: run `npm ci` in frontend/")
    return problems


def database_message(said: str) -> str:
    """Section 4.4: what failed, and the two ways to a database."""
    source, _, error = said.partition(" ")
    if not source.startswith("DATABASE_SOURCE="):
        source, error = "", said
    where = f" ({source})" if source else ""
    return (
        f"pytest: the configuration's database does not answer{where}.\n"
        f"  {error}\n"
        "  Start Docker (docker compose up -d), or run the checks inside the "
        "embedded cluster,\n"
        "  with backend on PYTHONPATH:\n"
        "  python -m app.infrastructure.database.embedded run -- "
        "python scripts/check.py"
    )


def run_step(step: Step, chosen: Sequence[Step], machine: Machine) -> bool:
    if step.kind == "env":
        problems = check_environment(chosen, machine)
        for problem in problems:
            print(f"env: {problem}")
        return not problems
    if step.kind == "npm":
        command = [machine.npm or "npm", *step.arguments]
        print(f"> {' '.join(command)}  (in frontend)")
        return machine.run(command, FRONTEND, machine.environment) == 0
    environment = machine.environment
    if step.kind == "pytest":
        connected, said = machine.database()
        if not connected:
            print(database_message(said))
            return False
        print(f"pytest: the database answers ({said})")
        environment = pytest_environment(environment)
    command = [machine.python, "-m", *step.arguments]
    print(f"> {' '.join(command)}")
    return machine.run(command, REPO, environment) == 0


@dataclass(frozen=True)
class Outcome:
    name: str
    passed: bool
    seconds: float


def run_steps(
    chosen: Sequence[Step], machine: Machine, keep_going: bool
) -> list[Outcome]:
    outcomes = []
    for step in chosen:
        print(f"==> {step.name}", flush=True)
        started = machine.clock()
        passed = run_step(step, chosen, machine)
        seconds = machine.clock() - started
        outcomes.append(Outcome(step.name, passed, seconds))
        verdict = "passed" if passed else "FAILED"
        print(f"<== {step.name}: {verdict} in {seconds:.1f} s", flush=True)
        if not passed and not keep_going:
            break
    return outcomes


def summary(chosen: Sequence[Step], outcomes: Sequence[Outcome]) -> list[str]:
    ran = {outcome.name for outcome in outcomes}
    lines = ["summary:"]
    for outcome in outcomes:
        verdict = "passed" if outcome.passed else "FAILED"
        lines.append(f"  {outcome.name:<9} {verdict:<7} {outcome.seconds:7.1f} s")
    lines += [f"  {s.name:<9} not run" for s in chosen if s.name not in ran]
    total = sum(outcome.seconds for outcome in outcomes)
    failed = [outcome.name for outcome in outcomes if not outcome.passed]
    lines.append(f"  total             {total:7.1f} s")
    lines.append(f"FAILED: {', '.join(failed)}" if failed else "every step passed")
    return lines


def main(argv: Sequence[str] | None = None, machine: Machine | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(
        prog="python scripts/check.py",
        description=(__doc__ or "").splitlines()[0],
    )
    parser.add_argument(
        "names",
        nargs="*",
        help=f"steps or groups (default: all): {', '.join([*GROUPS, *BY_NAME])}",
    )
    parser.add_argument(
        "--keep-going",
        action="store_true",
        help="run every chosen step, and say which failed at the end",
    )
    args = parser.parse_args(argv)
    try:
        chosen = choose(args.names)
    except UsageError as error:
        print(f"check.py: {error}", file=sys.stderr)
        return 2
    outcomes = run_steps(chosen, machine or default_machine(), args.keep_going)
    print("\n".join(summary(chosen, outcomes)))
    return 0 if all(outcome.passed for outcome in outcomes) else 1


if __name__ == "__main__":
    sys.exit(main())
