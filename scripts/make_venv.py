"""A virtualenv made only from the two lockfiles (RFC-048 section 6).

Run it with the Python that is to be the venv's, called by its path:

    C:\\<Python 3.12.9>\\python.exe scripts/make_venv.py           # .venv at the root
    C:\\<Python 3.12.9>\\python.exe scripts/make_venv.py D:\\other  # somewhere else

What it does, in order:

1. Refuses a Python other than 3.12.9 (RFC-048 D5), and a folder that
   exists. It never deletes a venv, and never installs into one.
2. Creates the venv with the `pip` that comes with that Python
   (`ensurepip`), and does not upgrade it.
3. Installs `setuptools`, alone and with its hash, from the runtime's
   lockfile. `langdetect` 1.0.9 is an sdist, built without isolation, and a
   Python 3.12 venv is born with `pip` only (RFC-048 section 2.5).
4. Installs `scripts/python-win-64.lock` and `scripts/python-dev-win-64.lock`
   in one call, `--no-deps --require-hashes --no-build-isolation`: nothing
   is resolved, and a file whose sha256 is not the lockfile's is refused by
   `pip` itself.
5. Holds the venv against both lockfiles -- the comparison `check.py`'s `env`
   step makes -- and fails if a package is at another version or missing.

A run that fails leaves the folder as far as it got, and says so: it is not
a venv the checks accept, and is deleted by hand.
"""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
DEFAULT_TARGET = REPO / ".venv"
sys.path.insert(0, str(Path(__file__).resolve().parent))

import vendor_python  # noqa: E402

PYTHON_VERSION = vendor_python.PYTHON_VERSION
LOCKFILES = (vendor_python.LOCKFILE, vendor_python.DEV_LOCKFILE)
BOOTSTRAP = "setuptools"
PIP_SECONDS = 3600


class MakeVenvError(Exception):
    """A step failed. The message says which, and what was left."""


def venv_python(target: Path) -> Path:
    return target / "Scripts" / "python.exe"


@dataclass(frozen=True)
class Steps:
    """What reaches outside this process; replaced in the tests."""

    create: Callable[[Path], None]
    """target -> a venv there, with `ensurepip`'s pip."""
    pip: Callable[[Path, Sequence[str | Path]], None]
    """(the venv's python, arguments) -> `pip` run by it, or an error."""
    installed: Callable[[Path], dict[str, str]]
    """The venv's python -> canonical name -> version, as it reads them."""


def _create(target: Path) -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "venv", str(target)], stdin=subprocess.DEVNULL
    )
    if completed.returncode != 0:
        raise MakeVenvError(f"python -m venv failed with {completed.returncode}")


def _pip(python: Path, arguments: Sequence[str | Path]) -> None:
    sys.stdout.flush()
    completed = subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            *(str(argument) for argument in arguments),
            "--disable-pip-version-check",
            "--no-input",
        ],
        stdin=subprocess.DEVNULL,
        timeout=PIP_SECONDS,
    )
    if completed.returncode != 0:
        raise MakeVenvError(f"pip {arguments[0]} failed with {completed.returncode}")


INSTALLED_PROBE = """
import importlib.metadata, json
try:
    from packaging.version import Version
except ImportError:
    Version = str
found = {}
for distribution in importlib.metadata.distributions():
    name = distribution.metadata["Name"]
    if name:
        found[name] = str(Version(distribution.version))
print(json.dumps(found))
"""


def _installed(python: Path) -> dict[str, str]:
    completed = subprocess.run(
        [str(python), "-I", "-c", INSTALLED_PROBE],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        timeout=300,
    )
    if completed.returncode != 0:
        raise MakeVenvError(
            f"{python} could not list its packages: {completed.stderr.strip()[-500:]}"
        )
    found: dict[str, str] = json.loads(completed.stdout)
    return {vendor_python.canonical(name): version for name, version in found.items()}


def default_steps() -> Steps:
    return Steps(create=_create, pip=_pip, installed=_installed)


def bootstrap_line(runtime: Sequence[Any]) -> str:
    """The lockfile's `setuptools` line, hash and all."""
    for entry in runtime:
        if entry.name == BOOTSTRAP:
            line: str = entry.line()
            return line
    raise MakeVenvError(f"{vendor_python.LOCKFILE.name} has no {BOOTSTRAP}")


def make_venv(
    target: Path,
    steps: Steps,
    python_version: str,
    work: Path,
    lockfiles: Sequence[Path] = LOCKFILES,
) -> Any:
    """Steps 1 to 5. Returns the `VenvReport`, whose `extra` is what no
    lockfile lists (`pip`)."""
    if python_version != PYTHON_VERSION:
        raise MakeVenvError(
            f"this is Python {python_version}: run this file with a Python "
            f"{PYTHON_VERSION}, by its path. Nothing was created."
        )
    if target.exists():
        raise MakeVenvError(
            f"{target} exists: this never deletes nor installs into a venv. Move "
            "it away, or name another folder. Nothing was changed."
        )
    try:
        locks = {path.name: vendor_python.read_lock(path) for path in lockfiles}
    except vendor_python.VendorError as error:
        raise MakeVenvError(f"{error}. Nothing was created.") from None
    bootstrap = work / "bootstrap.txt"
    bootstrap.write_text(
        f"--index-url {vendor_python.INDEX_URL}\n"
        f"{bootstrap_line(locks[lockfiles[0].name])}\n",
        encoding="utf-8",
    )

    print(f"1/4 the venv, with Python {python_version}: {target}", flush=True)
    steps.create(target)
    python = venv_python(target)
    try:
        print(f"2/4 {BOOTSTRAP}, alone and with its hash", flush=True)
        steps.pip(python, ["install", "--no-deps", "--require-hashes", "-r", bootstrap])
        print(f"3/4 {' and '.join(locks)}, in one call", flush=True)
        requirements: list[str | Path] = []
        for path in lockfiles:
            requirements += ["-r", path]
        steps.pip(
            python,
            [
                "install",
                "--no-deps",
                "--require-hashes",
                "--no-build-isolation",
                *requirements,
            ],
        )
        print("4/4 the venv against the lockfiles", flush=True)
        report = vendor_python.compare_venv_with_locks(steps.installed(python), locks)
        if report.problems:
            raise MakeVenvError(
                "the venv is not the lockfiles:\n  " + "\n  ".join(report.problems)
            )
    except MakeVenvError as error:
        raise MakeVenvError(
            f"{error}\n{target} is left as far as it got, and is not a venv the "
            "checks accept: delete it by hand."
        ) from None
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python scripts/make_venv.py",
        description=(__doc__ or "").splitlines()[0],
    )
    parser.add_argument(
        "target",
        nargs="?",
        type=Path,
        default=DEFAULT_TARGET,
        help=f"where the venv goes (default: {DEFAULT_TARGET})",
    )
    args = parser.parse_args(argv)
    if sys.platform != "win32" or sys.maxsize <= 2**32:
        print("The lockfiles are cp312 win_amd64: run this on 64-bit Windows.")
        return 2
    target: Path = args.target.resolve()
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="solidvision-venv-") as work:
        try:
            report = make_venv(
                target, default_steps(), platform.python_version(), Path(work)
            )
        except (MakeVenvError, OSError, subprocess.TimeoutExpired) as error:
            print(f"FAILED: {error}")
            return 1
    packages = sum(len(vendor_python.read_lock(path)) for path in LOCKFILES)
    print(
        f"{target}: Python {PYTHON_VERSION}, the {packages} packages of the "
        f"lockfiles, in {time.perf_counter() - started:.0f} s"
    )
    if report.extra:
        print(f"  and, in no lockfile: {', '.join(report.extra)}")
    print(f"  activate it with {target / 'Scripts' / 'Activate.ps1'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
