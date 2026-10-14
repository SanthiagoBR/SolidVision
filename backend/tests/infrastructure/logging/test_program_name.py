"""One log file per program, named after it (RFC-034 section 8.2)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.infrastructure.logging.program import program_name

PYTHON = r"C:\SolidVision\python\python.exe"
BACKEND = str(Path(__file__).resolve().parents[3])


@pytest.mark.parametrize(
    ("command", "name"),
    [
        (["-m", "uvicorn", "app.presentation.api:app"], "uvicorn"),
        (["-m", "app.infrastructure.workers.job_runner"], "job_runner"),
        (["-m", "app.infrastructure.database.embedded", "start"], "embedded"),
        (["-m", "app.cli", "search", "ponte"], "cli"),
        (["-m", "pytest", "-q"], "pytest"),
        ([r"C:\checkout\backend\.venv\Scripts\pytest.exe", "-q"], "pytest"),
        (["-c", "print(1)"], "python"),
        ([], "python"),
    ],
)
def test_the_table_of_section_8_2(command: list[str], name: str) -> None:
    assert program_name([PYTHON, *command]) == name


@pytest.mark.parametrize(
    ("command", "name"),
    [
        (["-X", "utf8", "-m", "app.infrastructure.workers.job_runner"], "job_runner"),
        (["-Xutf8", "-W", "error", "-m", "pytest"], "pytest"),
        (["-B", "-u", "-m", "uvicorn", "app.presentation.api:app"], "uvicorn"),
        (["-Bum", "uvicorn", "app.presentation.api:app"], "uvicorn"),
        (["-mpytest", "-q"], "pytest"),
        (["-I", "-c", "import app"], "python"),
        (["--check-hash-based-pycs", "never", "-m", "pytest"], "pytest"),
        (["-m", "app.cli.__main__"], "cli"),
        ([r"scripts\stage_installation.py", "--read-only"], "stage_installation"),
        (["-"], "python"),
        (["-m"], "python"),
    ],
)
def test_the_interpreters_own_options_are_not_the_program(
    command: list[str], name: str
) -> None:
    """`-m` ends the interpreter's options; what precedes it is not a name,
    and what follows the module is the program's own."""
    assert program_name([PYTHON, *command]) == name


def test_what_follows_the_module_is_not_read_as_the_program() -> None:
    command = ["-m", "app.infrastructure.database.embedded", "run", "--", "-m", "x"]

    assert program_name([PYTHON, *command]) == "embedded"


def test_a_name_is_always_fit_for_a_file() -> None:
    assert program_name([PYTHON, r"C:\tools\my tool (2).py"]) == "my_tool__2_"
    assert program_name([PYTHON, "-m", "a/b:c"]) == "a_b_c"


def test_the_running_process_has_a_name() -> None:
    """Whatever started the tests -- `python -m pytest`, `pytest`, an IDE's
    runner -- the answer is a usable file name, read without arguments."""
    name = program_name()

    assert name and name == program_name(sys.orig_argv)


READ_DURING_IMPORT = (
    "import sys\n"
    "from app.infrastructure.logging.program import program_name\n"
    "print('importing:', program_name(), '| argv[0] is', sys.argv[0])\n"
)


def test_the_name_is_known_while_the_packages_are_still_importing(
    tmp_path: Path,
) -> None:
    """The logger is configured by the first `get_logger()`, during import.

    A real `python -m package.tool`, whose package asks for the name in its
    `__init__` -- before `tool` has started, which is when a module-level
    `get_logger()` of the project runs. At that moment `sys.argv[0]` is the
    literal `-m`, and only the original command line knows the module.
    """
    package = tmp_path / "sv_probe_package"
    package.mkdir()
    (package / "__init__.py").write_text(READ_DURING_IMPORT, encoding="utf-8")
    (package / "tool.py").write_text("print('running')\n", encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, "-m", "sv_probe_package.tool"],
        env={**os.environ, "PYTHONPATH": os.pathsep.join([str(tmp_path), BACKEND])},
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.splitlines() == [
        "importing: tool | argv[0] is -m",
        "running",
    ]
