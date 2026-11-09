"""A venv made only from the two lockfiles: `scripts/make_venv.py` (RFC-048 6).

`pip` is replaced by a double that writes down what it was asked, and the
venv by a folder: nothing here creates an interpreter or reaches the network.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"


def load(name: str) -> ModuleType:
    """A script, loaded from its file: `scripts/` is not a package."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def make() -> ModuleType:
    return load("make_venv")


SETUPTOOLS = "setuptools===78.1.0 --hash=sha256:" + "a" * 64


@pytest.fixture()
def lockfiles(tmp_path: Path) -> tuple[Path, Path]:
    runtime = tmp_path / "python-win-64.lock"
    runtime.write_text(
        "--index-url https://pypi.org/simple\n"
        f"{SETUPTOOLS}\n"
        "sqlalchemy===2.0.51 --hash=sha256:" + "b" * 64 + "\n",
        encoding="utf-8",
    )
    dev = tmp_path / "python-dev-win-64.lock"
    dev.write_text(
        "--index-url https://pypi.org/simple\n"
        "ruff===0.16.9 --hash=sha256:" + "c" * 64 + "\n",
        encoding="utf-8",
    )
    return runtime, dev


LOCKED = {"setuptools": "78.1.0", "sqlalchemy": "2.0.51", "ruff": "0.16.9"}


class Pip:
    """What the venv was asked, in order, with each requirements file's text
    as it was when `pip` read it."""

    def __init__(self) -> None:
        self.log: list[tuple[str, Any]] = []

    def steps(self, make: ModuleType, installed: dict[str, str] | None = None) -> Any:
        def create(target: Path) -> None:
            (target / "Scripts").mkdir(parents=True)
            self.log.append(("create", target))

        def pip(python: Path, arguments: Sequence[str | Path]) -> None:
            arguments = [str(argument) for argument in arguments]
            files = [
                Path(arguments[i + 1]).read_text(encoding="utf-8")
                for i, argument in enumerate(arguments)
                if argument == "-r"
            ]
            self.log.append(("pip", (python, arguments, files)))

        return make.Steps(
            create=create,
            pip=pip,
            installed=lambda python: dict(
                installed if installed is not None else {**LOCKED, "pip": "24.3.1"}
            ),
        )


def test_a_python_other_than_3_12_9_is_refused_before_anything_is_created(
    make: ModuleType, tmp_path: Path, lockfiles: tuple[Path, Path]
) -> None:
    pip = Pip()
    target = tmp_path / "venv"

    with pytest.raises(make.MakeVenvError, match="Python 3.12.9"):
        make.make_venv(target, pip.steps(make), "3.12.5", tmp_path, lockfiles)

    assert pip.log == []
    assert not target.exists()


def test_a_folder_that_exists_is_refused_and_nothing_in_it_changes(
    make: ModuleType, tmp_path: Path, lockfiles: tuple[Path, Path]
) -> None:
    pip = Pip()
    target = tmp_path / "venv"
    (target / "Lib").mkdir(parents=True)
    (target / "pyvenv.cfg").write_text("version = 3.12.5\n")

    with pytest.raises(make.MakeVenvError, match="exists"):
        make.make_venv(target, pip.steps(make), "3.12.9", tmp_path, lockfiles)

    assert pip.log == []
    assert sorted(path.name for path in target.iterdir()) == ["Lib", "pyvenv.cfg"]
    assert (target / "pyvenv.cfg").read_text() == "version = 3.12.5\n"


def test_setuptools_is_installed_alone_with_its_hash_before_the_two_lockfiles(
    make: ModuleType, tmp_path: Path, lockfiles: tuple[Path, Path]
) -> None:
    """Section 2.5: `langdetect` is an sdist, built without isolation, and a
    venv of Python 3.12 is born with `pip` only."""
    pip = Pip()
    target = tmp_path / "venv"

    report = make.make_venv(target, pip.steps(make), "3.12.9", tmp_path, lockfiles)

    assert [what for what, _ in pip.log] == ["create", "pip", "pip"]
    python, first, first_files = pip.log[1][1]
    assert python == target / "Scripts" / "python.exe"
    assert first[:3] == ["install", "--no-deps", "--require-hashes"]
    assert first_files == [f"--index-url https://pypi.org/simple\n{SETUPTOOLS}\n"]
    _, second, second_files = pip.log[2][1]
    assert second[:4] == [
        "install",
        "--no-deps",
        "--require-hashes",
        "--no-build-isolation",
    ]
    assert second[4:] == ["-r", str(lockfiles[0]), "-r", str(lockfiles[1])]
    assert second_files == [path.read_text(encoding="utf-8") for path in lockfiles]
    assert report.problems == ()
    assert report.extra == ("pip 24.3.1",)


def test_the_setuptools_line_is_the_real_runtime_lockfiles(make: ModuleType) -> None:
    vendor = load("vendor_python")

    line = make.bootstrap_line(vendor.read_lock(vendor.LOCKFILE))

    assert line.startswith("setuptools===")
    assert line in vendor.LOCKFILE.read_text(encoding="utf-8").splitlines()


@pytest.mark.parametrize(
    ("installed", "said"),
    [
        (
            {**LOCKED, "sqlalchemy": "2.1.1"},
            "sqlalchemy is 2.1.1, python-win-64.lock says 2.0.51",
        ),
        (
            {"setuptools": "78.1.0", "sqlalchemy": "2.0.51"},
            "ruff 0.16.9 is in python-dev-win-64.lock and not installed",
        ),
    ],
)
def test_a_venv_that_is_not_the_lockfiles_at_the_end_fails(
    make: ModuleType,
    tmp_path: Path,
    lockfiles: tuple[Path, Path],
    installed: dict[str, str],
    said: str,
) -> None:
    pip = Pip()
    target = tmp_path / "venv"

    with pytest.raises(make.MakeVenvError) as raised:
        make.make_venv(
            target, pip.steps(make, installed), "3.12.9", tmp_path, lockfiles
        )

    assert said in str(raised.value)
    assert "delete it by hand" in str(raised.value)
    assert target.exists(), "never deleted"


def test_the_two_lockfiles_are_the_real_ones_by_default(make: ModuleType) -> None:
    assert [path.name for path in make.LOCKFILES] == [
        "python-win-64.lock",
        "python-dev-win-64.lock",
    ]
    assert make.DEFAULT_TARGET == REPO / ".venv"
