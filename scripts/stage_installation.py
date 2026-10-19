"""A staged installation: the code outside the repository (RFC-034 section 9).

"The backend starts from any directory, without a `.env`" cannot be tested
from inside a checkout. The checkout has a `.env` and a `pyproject.toml`, and
deleting either to test is tampering with the environment of whoever is
developing. So this copies the tree an installed app has (section 4.1) to a
directory outside the repository::

    <target>\\
        backend\\           app\\, alembic\\, alembic.ini
        runtime\\postgres\\  RFC-033
        runtime\\models\\    RFC-035

**without** `pyproject.toml`, `.env` and the tests, which is what makes the
copy an installation: the mode is read from the tree, and `pyproject.toml` is
what makes a directory a checkout (section 4.2).

    python scripts/stage_installation.py TARGET              # copy and compile
    python scripts/stage_installation.py TARGET --read-only  # ... and lock it
    python scripts/stage_installation.py TARGET --no-compile # leave no .pyc
    python scripts/stage_installation.py TARGET --unlock     # give it back
    python scripts/stage_installation.py TARGET --remove     # unlock and delete

`--read-only` leaves the tree read-and-execute for the user, by `icacls` and by
SID, as RFC-033 did with the cluster's DACL. It is what `C:\\Program Files` is
to an ordinary process. The `.pyc` files are compiled before the lock, because
after it Python cannot write them and would recompile the project's modules on
every start; `--no-compile` is how section 16 measures that cost.

**It is not the installer.** The code is run by the interpreter of the
development virtualenv, with `PYTHONPATH` pointing at the copy. Freezing the
runtime stays out of the roadmap.

No new dependency: `shutil`, `compileall` and `subprocess` are the standard
library's, and the SID comes from the adapter of RFC-033.
"""

from __future__ import annotations

import argparse
import compileall
import datetime
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "backend"))

from app.infrastructure.database.embedded.cluster_home import (  # noqa: E402
    CREATE_NO_WINDOW,
    SYSTEM_SID,
    current_user_sid,
)

COPIED = (
    "backend/app",
    "backend/alembic",
    "backend/alembic.ini",
    "runtime/postgres",
    "runtime/models",
)
"""The tree of section 4.1, and nothing else of the repository. Since RFC-035
that includes the 924 MB of models, copied like any other file."""

NEVER_STAGED = ("pyproject.toml", ".env")
"""Either one at the root would make the copy a checkout again: the first
turns the mode into `development`, and the second is the file that mode reads."""

MARKER_NAME = "SOLIDVISION-STAGED.json"
"""Written at the root of every staging. `--unlock` and `--remove` refuse a
directory without it, so neither can be pointed at anything else by mistake."""

VERIFY_SECONDS = 60
"""Every check on a read-only tree has a deadline: a write that is denied can
show up as a process that never returns instead of as an error. Measured with
`tempfile.mkdtemp` under `C:\\Program Files` (RFC-034 section 2.1)."""

WRITE_PROBE = """
import os, sys
denied = 0
for directory in sys.argv[1:]:
    file = os.path.join(directory, "written-by-the-probe")
    folder = os.path.join(directory, "made-by-the-probe")
    for attempt, undo in (
        (lambda: open(file, "w").close(), lambda: os.remove(file)),
        (lambda: os.mkdir(folder), lambda: os.rmdir(folder)),
    ):
        try:
            attempt()
        except PermissionError:
            denied += 1
        else:
            undo()
print(denied)
"""


class StagingError(Exception):
    """A step failed. The message says which."""


@dataclass(frozen=True)
class Staged:
    """One staged installation, and how to run the code that is in it."""

    root: Path
    compiled: bool
    read_only: bool

    @property
    def backend(self) -> Path:
        return self.root / "backend"

    def environment(
        self, local_app_data: Path, base: Mapping[str, str] | None = None
    ) -> dict[str, str]:
        """The environment of a process that runs the copy, not the checkout.

        `PYTHONPATH` is replaced, never extended: a `backend` left in it by a
        developer's shell would put the checkout's `app` back on the path.
        `LOCALAPPDATA` is the caller's choice, so that a first run is really
        the first and the real profile is not touched (section 9).
        """
        environment = dict(os.environ if base is None else base)
        environment["PYTHONPATH"] = str(self.backend)
        environment["LOCALAPPDATA"] = str(local_app_data)
        return environment


def stage(
    target: Path,
    *,
    source: Path = REPO,
    compile_bytecode: bool = True,
    read_only: bool = False,
) -> Staged:
    """Copy the installed tree of `source` to `target`, which must not exist."""
    target = _checked_target(target, source)
    missing = [name for name in COPIED if not (source / name).exists()]
    if missing:
        raise StagingError(
            f"{source} has no {', '.join(missing)}. The PostgreSQL runtime is "
            "materialized with `python scripts/vendor_postgres.py`, and the "
            "models with `python scripts/vendor_models.py`."
        )
    target.mkdir(parents=True)
    try:
        for name in COPIED:
            origin, copy = source / name, target / name
            if origin.is_dir():
                shutil.copytree(
                    origin, copy, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
                )
            else:
                copy.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(origin, copy)
        _refuse_a_checkout(target)
        _write_marker(target, source)
        if compile_bytecode:
            compile_tree(target)
    except BaseException:
        # Created here a moment ago, and not locked yet: nothing half-built
        # is left under a name that looks like a staging.
        shutil.rmtree(target, ignore_errors=True)
        raise
    if read_only:
        lock(target)
    return staged_at(target)


def staged_at(target: Path) -> Staged:
    """A staging made earlier, read back from its marker."""
    marker = _read_marker(target)
    return Staged(
        target.resolve(),
        compiled=bool(marker["compiled"]),
        read_only=bool(marker["read_only"]),
    )


def compile_tree(target: Path) -> None:
    """Write the `.pyc` of every module of the copy, for this interpreter."""
    _read_marker(target)
    if not compileall.compile_dir(str(target / "backend"), quiet=1):
        raise StagingError(f"Some module under {target / 'backend'} did not compile.")
    _update_marker(target, compiled=True)


def discard_bytecode(target: Path) -> None:
    """Remove every `__pycache__` of the copy: what `--no-compile` leaves."""
    _read_marker(target)
    for cache in sorted((target / "backend").rglob("__pycache__")):
        shutil.rmtree(cache)
    _update_marker(target, compiled=False)


def lock(target: Path) -> None:
    """Leave the tree read-and-execute for the user, and check that it is.

    Inheritance is cut at the root and the two entries are inheritable, so
    Windows recomputes every file and folder below from them. The user is
    named by the SID of this process's token: in a Portuguese Windows the
    group names are translated, and a name must not decide a permission.
    """
    # Before the lock: after it, this process cannot write the marker either.
    _update_marker(target, read_only=True)
    _icacls(
        target,
        "/inheritance:r",
        "/grant:r",
        f"*{current_user_sid()}:(OI)(CI)RX",
        f"*{SYSTEM_SID}:(OI)(CI)F",
    )
    directories = _probed_directories(target)
    denied = _denied_writes(directories)
    expected = 2 * len(directories)
    if denied != expected:
        raise StagingError(
            f"{target} was locked, and {expected - denied} of {expected} attempts "
            "to write in it still succeeded."
        )


def unlock(target: Path) -> None:
    """Give the tree back to whatever its parent directory allows.

    The user owns the tree, and an owner may always rewrite the DACL, which is
    why a tree this user cannot write to can still be unlocked by them.
    """
    _read_marker(target)
    _icacls(target, "/reset")
    _update_marker(target, read_only=False)


def remove(target: Path) -> None:
    """Unlock a staging and delete it."""
    unlock(target)
    shutil.rmtree(target)


def _checked_target(target: Path, source: Path) -> Path:
    if not target.is_absolute():
        raise StagingError(
            f"The target must be absolute, not {target}: a relative one would "
            "depend on where this was run from."
        )
    target = target.resolve()
    source = source.resolve()
    if target == source or source in target.parents:
        raise StagingError(
            f"{target} is inside {source}. A staged installation lives outside "
            "the repository, or it is not a test of running without one."
        )
    if target.exists():
        hint = (
            "Remove it with --remove first."
            if (target / MARKER_NAME).is_file()
            else "It is not a staging, so it is left alone: choose another directory."
        )
        raise StagingError(f"{target} already exists. {hint}")
    return target


def _refuse_a_checkout(target: Path) -> None:
    """The copy must not be a checkout, whatever the source held.

    The guard on section 13's first risk: an installer that carried
    `pyproject.toml` would ship an app that takes itself for development.
    """
    found = [name for name in NEVER_STAGED if (target / name).exists()]
    found += [
        str(path.relative_to(target))
        for path in target.joinpath("backend").rglob("tests")
        if path.is_dir()
    ]
    if found:
        raise StagingError(f"{target} holds {', '.join(found)}, which is never staged.")


def _write_marker(target: Path, source: Path) -> None:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=source,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
        ).stdout.strip()
    except OSError:
        commit = ""
    record = {
        "staged_from": str(source),
        "commit": commit or None,
        "staged_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "python": sys.version.split()[0],
        "compiled": False,
        "read_only": False,
    }
    (target / MARKER_NAME).write_text(
        json.dumps(record, indent=2) + "\n", encoding="utf-8"
    )


def _update_marker(target: Path, **facts: bool) -> None:
    record = _read_marker(target)
    record.update(facts)
    (target / MARKER_NAME).write_text(
        json.dumps(record, indent=2) + "\n", encoding="utf-8"
    )


def _read_marker(target: Path) -> dict[str, object]:
    marker = target / MARKER_NAME
    try:
        record = json.loads(marker.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise StagingError(
            f"{target} has no {MARKER_NAME}: it is not a staging this script made, "
            "and it is left alone."
        ) from None
    if not isinstance(record, dict):
        raise StagingError(f"{marker} is not a staging marker.")
    return record


def _probed_directories(target: Path) -> list[Path]:
    """The root, the two top folders, and the deepest folder of the code."""
    packages = [
        path for path in (target / "backend" / "app").rglob("*") if path.is_dir()
    ]
    deepest = max(packages, key=lambda path: len(path.parts), default=target)
    return [target, target / "backend", target / "runtime", deepest]


def _denied_writes(directories: list[Path]) -> int:
    """How many attempts to write were denied, asked of a child with a deadline."""
    try:
        completed = subprocess.run(
            [sys.executable, "-c", WRITE_PROBE, *(str(path) for path in directories)],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=VERIFY_SECONDS,
        )
    except subprocess.TimeoutExpired:
        raise StagingError(
            f"The check of the lock did not return in {VERIFY_SECONDS} s."
        ) from None
    answer = completed.stdout.strip()
    if completed.returncode != 0 or not answer.isdigit():
        raise StagingError(
            f"The check of the lock failed: {completed.stderr.strip()[-300:]}"
        )
    return int(answer)


def _icacls(target: Path, *arguments: str) -> None:
    system_root = Path(os.environ.get("SYSTEMROOT", r"C:\Windows"))
    completed = subprocess.run(
        [str(system_root / "System32" / "icacls.exe"), str(target), *arguments],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        creationflags=CREATE_NO_WINDOW,
        timeout=VERIFY_SECONDS * 5,
    )
    if completed.returncode != 0:
        output = (completed.stdout + completed.stderr).decode("oem", "replace")
        raise StagingError(
            f"icacls {' '.join(arguments)} failed on {target} "
            f"(exit {completed.returncode}): {output.strip()}"
        )


def _how_to_run(staged: Staged) -> str:
    return (
        "Run the copy from an empty directory, with a profile of its own "
        "(PowerShell):\n"
        f'    $env:PYTHONPATH = "{staged.backend}"\n'
        '    $env:LOCALAPPDATA = "<an empty directory>"\n'
        "    python -m app.launcher"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Stage an installation outside the repository (RFC-034 section 9)."
    )
    parser.add_argument("target", type=Path, help="a directory that does not exist")
    parser.add_argument(
        "--read-only",
        action="store_true",
        help="lock the tree, as Program Files is to an ordinary process",
    )
    parser.add_argument(
        "--no-compile", action="store_true", help="do not write the .pyc files"
    )
    parser.add_argument(
        "--unlock", action="store_true", help="make a locked staging writable again"
    )
    parser.add_argument(
        "--remove", action="store_true", help="unlock a staging and delete it"
    )
    arguments = parser.parse_args()
    target = arguments.target.absolute()
    try:
        if arguments.remove:
            remove(target)
            print(f"removed {target}")
        elif arguments.unlock:
            unlock(target)
            print(f"unlocked {target}")
        else:
            staged = stage(
                target,
                compile_bytecode=not arguments.no_compile,
                read_only=arguments.read_only,
            )
            facts = [
                "compiled" if staged.compiled else "not compiled",
                "read-only" if staged.read_only else "writable",
            ]
            print(f"staged {staged.root} ({', '.join(facts)})")
            print(_how_to_run(staged))
    except StagingError as error:
        print(f"stage_installation: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
