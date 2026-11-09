"""Every check in one command: `scripts/check.py` (RFC-048 section 4).

The machine is replaced by a double, as `test_vendor_python.py` replaces the
`Steps`: it records each command a step gave it and answers with the exit
code the test chose. Nothing here runs a tool, a `node`, or a database.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Callable, Mapping, Sequence
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
def check() -> ModuleType:
    return load("check")


@pytest.fixture(scope="module")
def vendor() -> ModuleType:
    return load("vendor_python")


PYTHON = "C:\\venv\\Scripts\\python.exe"


class Recorder:
    """The commands the steps ran, by step name, with what each was given."""

    def __init__(self, check: ModuleType) -> None:
        self.check = check
        self.ran: list[str] = []
        self.commands: dict[str, list[str]] = {}
        self.folders: dict[str, Path] = {}
        self.environments: dict[str, Mapping[str, str]] = {}

    def name_of(self, command: Sequence[str]) -> str:
        for step in self.check.STEPS:
            if step.kind == "npm" and tuple(command[1:]) == step.arguments:
                return str(step.name)
            if step.kind in ("python", "pytest") and command[:2] == [PYTHON, "-m"]:
                if tuple(command[2:]) == step.arguments:
                    return str(step.name)
        raise AssertionError(f"no step runs {command}")


def locks(vendor: ModuleType) -> dict[str, list[Any]]:
    return {
        "python-win-64.lock": [
            vendor.Locked("sqlalchemy", "2.0.51", "a" * 64),
            vendor.Locked("six", "1.17.0", "b" * 64),
        ],
        "python-dev-win-64.lock": [vendor.Locked("ruff", "0.16.9", "c" * 64)],
    }


INSTALLED = {"sqlalchemy": "2.0.51", "six": "1.17.0", "ruff": "0.16.9", "pip": "24.3.1"}


def never(what: str) -> Callable[[], Any]:
    def asked() -> Any:
        raise AssertionError(f"{what} was looked at")

    return asked


def machine(
    check: ModuleType,
    vendor: ModuleType,
    recorder: Recorder,
    tmp_path: Path,
    *,
    fail: Sequence[str] = (),
    installed: dict[str, str] | None = None,
    python_version: str = "3.12.9",
    in_venv: bool = True,
    node: str | None = "v24.15.0",
    node_modules: bool = True,
    database: tuple[bool, str] = (True, "DATABASE_SOURCE=external"),
    environment: Mapping[str, str] | None = None,
    python_untouched: bool = False,
) -> Any:
    def run(command: Sequence[str], folder: Path, env: Mapping[str, str]) -> int:
        name = recorder.name_of(list(command))
        recorder.ran.append(name)
        recorder.commands[name] = list(command)
        recorder.folders[name] = folder
        recorder.environments[name] = dict(env)
        return 1 if name in fail else 0

    modules = tmp_path / "node_modules"
    if node_modules:
        modules.mkdir(exist_ok=True)
    seconds = iter(range(1000))
    return check.Machine(
        run=run,
        python=PYTHON,
        python_version=python_version,
        in_venv=in_venv,
        installed=(
            never("the venv")
            if python_untouched
            else lambda: dict(INSTALLED if installed is None else installed)
        ),
        locks=never("the lockfiles") if python_untouched else lambda: locks(vendor),
        node_version=lambda: node,
        npm="C:\\node\\npm.cmd",
        node_modules=modules,
        expected_node=lambda: "24",
        database=lambda: database,
        environment=dict(environment or {"PATH": "C:\\node"}),
        clock=lambda: float(next(seconds)),
    )


def headed(output: str) -> list[str]:
    """The steps the command started, from the `==> name` lines."""
    return [line[4:] for line in output.splitlines() if line.startswith("==> ")]


# --------------------------------------------------------------------------
# Choosing and ordering
# --------------------------------------------------------------------------


def test_the_steps_are_the_tables_in_its_order(check: ModuleType) -> None:
    assert [step.name for step in check.STEPS] == [
        "env",
        "ruff",
        "black",
        "mypy",
        "eslint",
        "tsc",
        "prettier",
        "vitest",
        "pytest",
    ]
    assert check.GROUPS == {
        "python": ("ruff", "black", "mypy"),
        "frontend": ("eslint", "tsc", "prettier", "vitest"),
    }


def test_no_name_runs_every_step(
    check: ModuleType,
    vendor: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    recorder = Recorder(check)

    code = check.main([], machine(check, vendor, recorder, tmp_path))

    assert code == 0
    assert headed(capsys.readouterr().out) == [step.name for step in check.STEPS]
    assert recorder.ran == [step.name for step in check.STEPS][1:]


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        (["pytest", "mypy"], ["env", "mypy", "pytest"]),
        (["mypy", "pytest"], ["env", "mypy", "pytest"]),
        (
            ["pytest", "frontend", "black"],
            ["env", "black", "eslint", "tsc", "prettier", "vitest", "pytest"],
        ),
        (["python"], ["env", "ruff", "black", "mypy"]),
        (["vitest", "frontend"], ["env", "eslint", "tsc", "prettier", "vitest"]),
    ],
)
def test_steps_chosen_by_name_and_group_run_in_the_tables_order(
    check: ModuleType,
    vendor: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    typed: list[str],
    expected: list[str],
) -> None:
    recorder = Recorder(check)

    assert check.main(typed, machine(check, vendor, recorder, tmp_path)) == 0

    assert headed(capsys.readouterr().out) == expected
    assert recorder.ran == expected[1:]


def test_an_unknown_name_exits_with_2_before_any_step(
    check: ModuleType,
    vendor: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    recorder = Recorder(check)

    code = check.main(["ruff", "flake8"], machine(check, vendor, recorder, tmp_path))

    out, err = capsys.readouterr()
    assert code == 2
    assert recorder.ran == []
    assert headed(out) == []
    assert "flake8" in err


# --------------------------------------------------------------------------
# Failing
# --------------------------------------------------------------------------


def test_the_first_failure_stops_the_command(
    check: ModuleType,
    vendor: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    recorder = Recorder(check)

    code = check.main(
        [], machine(check, vendor, recorder, tmp_path, fail=["black", "tsc"])
    )

    out = capsys.readouterr().out
    assert code == 1
    assert recorder.ran == ["ruff", "black"]
    assert "<== black: FAILED" in out
    assert "  mypy      not run" in out
    assert out.rstrip().endswith("FAILED: black")


def test_keep_going_runs_every_step_and_the_summary_names_each_failure(
    check: ModuleType,
    vendor: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    recorder = Recorder(check)

    code = check.main(
        ["--keep-going"],
        machine(check, vendor, recorder, tmp_path, fail=["black", "tsc"]),
    )

    out = capsys.readouterr().out
    assert code == 1
    assert recorder.ran == [step.name for step in check.STEPS][1:]
    summary = out[out.index("summary:") :]
    assert "black     FAILED" in summary
    assert "tsc       FAILED" in summary
    assert "pytest    passed" in summary
    assert out.rstrip().endswith("FAILED: black, tsc")


# --------------------------------------------------------------------------
# The env step (section 4.3)
# --------------------------------------------------------------------------


def run_env(
    check: ModuleType,
    vendor: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    names: list[str],
    **options: Any,
) -> tuple[int, str, Recorder]:
    recorder = Recorder(check)
    code = check.main(names, machine(check, vendor, recorder, tmp_path, **options))
    return code, capsys.readouterr().out, recorder


def test_a_version_other_than_the_lockfiles_fails_with_the_name_and_both_versions(
    check: ModuleType,
    vendor: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    installed = {**INSTALLED, "sqlalchemy": "2.1.1"}

    code, out, recorder = run_env(
        check, vendor, tmp_path, capsys, ["python"], installed=installed
    )

    assert code == 1
    assert recorder.ran == []
    assert "env: sqlalchemy is 2.1.1, python-win-64.lock says 2.0.51" in out


def test_a_package_of_a_lockfile_that_is_missing_fails(
    check: ModuleType,
    vendor: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    installed = {name: v for name, v in INSTALLED.items() if name != "ruff"}

    code, out, _ = run_env(
        check, vendor, tmp_path, capsys, ["mypy"], installed=installed
    )

    assert code == 1
    assert "env: ruff 0.16.9 is in python-dev-win-64.lock and not installed" in out


def test_a_package_in_no_lockfile_passes_and_is_listed(
    check: ModuleType,
    vendor: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    installed = {**INSTALLED, "psutil": "7.0.0"}

    code, out, recorder = run_env(
        check, vendor, tmp_path, capsys, ["mypy"], installed=installed
    )

    assert code == 0
    assert recorder.ran == ["mypy"]
    listed = [line for line in out.splitlines() if "in no lockfile" in line]
    assert listed == ["env: in no lockfile, and allowed: pip 24.3.1, psutil 7.0.0"]


@pytest.mark.parametrize("version", ["3.12.5", "3.13.1"])
def test_a_python_other_than_3_12_9_fails(
    check: ModuleType,
    vendor: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    version: str,
) -> None:
    code, out, recorder = run_env(
        check, vendor, tmp_path, capsys, ["ruff"], python_version=version
    )

    assert code == 1
    assert recorder.ran == []
    assert f"env: Python is {version}, the checks run on 3.12.9" in out


def test_a_python_that_is_not_a_venv_fails(
    check: ModuleType,
    vendor: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, _ = run_env(check, vendor, tmp_path, capsys, ["pytest"], in_venv=False)

    assert code == 1
    assert "is not a venv: make one with scripts/make_venv.py" in out


def test_the_frontend_alone_looks_at_nothing_of_the_python_or_the_venv(
    check: ModuleType,
    vendor: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """RFC-049's frontend job is Linux, with any Python and no venv."""
    code, _, recorder = run_env(
        check,
        vendor,
        tmp_path,
        capsys,
        ["frontend"],
        python_version="3.10.0",
        in_venv=False,
        python_untouched=True,
    )

    assert code == 0
    assert recorder.ran == ["eslint", "tsc", "prettier", "vitest"]


def test_the_python_alone_looks_at_nothing_of_the_node(
    check: ModuleType,
    vendor: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, _, _ = run_env(
        check, vendor, tmp_path, capsys, ["python"], node=None, node_modules=False
    )

    assert code == 0


@pytest.mark.parametrize(
    ("node", "node_modules", "said"),
    [
        ("v20.19.0", True, "env: node is v20.19.0, frontend/.node-version says 24"),
        (None, True, "env: no node on PATH"),
        ("v24.15.0", False, "frontend/node_modules is missing: run `npm ci`"),
    ],
)
def test_the_frontend_needs_the_node_of_node_version_and_its_modules(
    check: ModuleType,
    vendor: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    node: str | None,
    node_modules: bool,
    said: str,
) -> None:
    code, out, recorder = run_env(
        check,
        vendor,
        tmp_path,
        capsys,
        ["eslint"],
        node=node,
        node_modules=node_modules,
    )

    assert code == 1
    assert recorder.ran == []
    assert said in out


def test_the_node_version_file_is_the_one_the_env_step_reads(
    check: ModuleType,
) -> None:
    assert check.NODE_VERSION_FILE.read_text(encoding="utf-8").strip() == "24"


# --------------------------------------------------------------------------
# The commands
# --------------------------------------------------------------------------


def test_ruff_gets_no_fix_and_ruff_and_black_get_backend_and_scripts_only(
    check: ModuleType,
    vendor: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`pyproject.toml` says `fix = true`: without `--no-fix`, `ruff check`
    corrects instead of failing. `experiments/` stays out (section 2.2)."""
    recorder = Recorder(check)

    check.main(["python"], machine(check, vendor, recorder, tmp_path))

    assert recorder.commands["ruff"] == [
        PYTHON,
        "-m",
        "ruff",
        "check",
        "--no-fix",
        "backend",
        "scripts",
    ]
    assert recorder.commands["black"] == [
        PYTHON,
        "-m",
        "black",
        "--check",
        "backend",
        "scripts",
    ]
    assert recorder.commands["mypy"] == [PYTHON, "-m", "mypy"]
    assert {recorder.folders[name] for name in ("ruff", "black", "mypy")} == {
        check.REPO
    }


def test_the_python_steps_run_with_the_interpreter_running_the_command(
    check: ModuleType,
) -> None:
    assert check.default_machine().python == sys.executable


def test_the_npm_steps_run_in_the_frontend(
    check: ModuleType,
    vendor: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    recorder = Recorder(check)

    check.main(["frontend"], machine(check, vendor, recorder, tmp_path))

    assert recorder.commands["eslint"] == ["C:\\node\\npm.cmd", "run", "lint"]
    assert recorder.commands["vitest"] == ["C:\\node\\npm.cmd", "test"]
    assert set(recorder.folders.values()) == {check.FRONTEND}


def test_every_npm_step_is_a_script_of_the_frontend(check: ModuleType) -> None:
    scripts = json.loads((check.FRONTEND / "package.json").read_text("utf-8"))[
        "scripts"
    ]

    for step in check.STEPS:
        if step.kind == "npm":
            assert step.arguments[-1] in scripts, step.name


# --------------------------------------------------------------------------
# pytest and the database (section 4.4)
# --------------------------------------------------------------------------


STOPPED = (
    "DATABASE_SOURCE=embedded The embedded cluster at C:\\x is not ready to be "
    "connected to: stopped"
)


def test_pytest_does_not_run_when_the_test_connection_fails(
    check: ModuleType,
    vendor: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    recorder = Recorder(check)

    code = check.main(
        ["--keep-going"],
        machine(check, vendor, recorder, tmp_path, database=(False, STOPPED)),
    )

    out = capsys.readouterr().out
    assert code == 1
    assert "pytest" not in recorder.ran
    assert "pytest: the configuration's database does not answer " in out
    assert "(DATABASE_SOURCE=embedded)" in out
    assert "connected to: stopped" in out
    assert "docker compose up -d" in out
    assert (
        "python -m app.infrastructure.database.embedded run -- "
        "python scripts/check.py" in out
    )
    assert out.rstrip().endswith("FAILED: pytest")


def test_pytest_gets_warm_up_models_false(
    check: ModuleType,
    vendor: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Above a `.env` that says `true` (section 2.4)."""
    recorder = Recorder(check)

    check.main(
        ["mypy", "pytest"],
        machine(
            check,
            vendor,
            recorder,
            tmp_path,
            environment={"PATH": "C:\\node", "WARM_UP_MODELS": "true"},
        ),
    )

    assert recorder.commands["pytest"] == [PYTHON, "-m", "pytest"]
    assert recorder.environments["pytest"]["WARM_UP_MODELS"] == "false"
    assert recorder.environments["pytest"]["PATH"] == "C:\\node"
    assert recorder.environments["mypy"]["WARM_UP_MODELS"] == "true"
