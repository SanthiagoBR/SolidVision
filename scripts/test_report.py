"""Both test suites as one visual report, in the browser (Allure Report).

    python scripts/test_report.py             # run, write the report and open it
    python scripts/test_report.py --no-open   # the same, without the browser
    python scripts/test_report.py --coverage  # also the backend's coverage

The report is one file, `test-report/report/index.html`: every test of the
backend (pytest) and of the frontend (Vitest) in one tree, with its status, its
duration and, when it failed, the error. The charts and the timeline are
sections of the same page. It opens from the disk, with no server, so the file
can be sent to someone as it is. `test-report/history.jsonl` keeps one entry
per run, which is where the trend charts come from.

With `--coverage`, pytest also measures which lines of `backend/app` the suite
runs (pytest-cov, which the venv already has), and writes a second page,
`test-report/coverage/index.html`: the percentage of each file, and each file
with its lines in green or red. Line coverage, of the tests that run by
default: the `slow` ones, which load the real models, are out. The frontend's
coverage is not measured: it needs `@vitest/coverage-v8` in `package.json`.
The data file goes to `test-report/` (`COVERAGE_FILE`), never to the `.coverage`
at the root, which git tracks.

What runs is what `scripts/check.py` runs: `python -m pytest` with the
configuration of `pyproject.toml` (the `slow` and the `embedded` tests stay
out, as there) and `npm test` in `frontend/`. `python` is the interpreter
running this file: run it with the venv's. The database is the one of the
configuration; when it does not answer and `runtime/postgres` is there, pytest
runs inside the embedded cluster, which is stopped at the end if this command
started it.

Nothing here is a dependency of the project. The tools of the report are
installed on the first run in `test-report/tools/`, which git ignores, and
neither the venv nor `frontend/node_modules` changes:

    python/   allure-pytest and what it needs that the venv lacks, each wheel
              by its hash, on `PYTHONPATH` for the run of pytest only
    node/     allure, the command that writes the report (Allure 3), at an
              exact version; what it depends on is what npm resolves that
              day, with no lockfile

The frontend has no adapter: `allure-vitest` 3.14.0 imports `@vitest/runner`,
which Vitest 5 no longer publishes. Vitest writes its own JSON report instead,
and `vitest_results` turns it into Allure's results.

The exit code is 0 when both suites passed, 1 when a test failed (the report
is written, and shows which) and 2 when there is no report to show: a suite
that did not run to the end is never left out of one, and neither is a test.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import socket
import sys
import uuid
import webbrowser
from collections.abc import Mapping
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import check  # noqa: E402

REPO = check.REPO
FRONTEND = check.FRONTEND
EMBEDDED_RUNTIME = REPO / "runtime" / "postgres"

ROOT = REPO / "test-report"
PYTHON_TOOLS = ROOT / "tools" / "python"
NODE_TOOLS = ROOT / "tools" / "node"
ALLURE_CLI = NODE_TOOLS / "node_modules" / "allure" / "cli.js"
RESULTS = ROOT / "results"
VITEST_JSON = ROOT / "vitest.json"
CONFIG = ROOT / "allurerc.json"
HISTORY = ROOT / "history.jsonl"
REPORT = ROOT / "report"
INDEX = REPORT / "index.html"
COVERAGE = ROOT / "coverage"
COVERAGE_INDEX = COVERAGE / "index.html"
COVERAGE_JSON = ROOT / "coverage.json"
COVERAGE_DATA = ROOT / ".coverage"

REPORT_NAME = "Testes automatizados do SolidVision"

PYTHON_PACKAGES = (
    (
        "allure-pytest",
        "2.16.2",
        "b16790e2f3749ffb2d3737ba443ba2eede8efba5226978847358fe2fd3e31303",
    ),
    (
        "allure-python-commons",
        "2.16.2",
        "0445f2745dfa8af70da1d3261cde2c83d44e7278c105f113ed050b61252f79bd",
    ),
    (
        "attrs",
        "26.1.0",
        "c647aa4a12dfbad9333ca4e71fe62ddc36f4e63b2d260a37a8b83d2f043ac309",
    ),
)
"""(name, version, the wheel's SHA-256). Installed with `--no-deps`, so this is
the whole list: `pluggy` and `pytest`, which the adapter also needs, are the
venv's."""

ALLURE_VERSION = "3.33.0"

FINISHED = (0, 1)
"""The exit codes of a suite that ran to the end: every test passed, or some
failed. Anything else is a suite that was interrupted or never started."""


class ReportError(Exception):
    """There will be no report, and why."""


def _say(message: str) -> None:
    print(f"test_report: {message}", flush=True)


# --------------------------------------------------------------------------
# The tools of the report
# --------------------------------------------------------------------------


def missing_requirements(machine: check.Machine) -> list[str]:
    problems = []
    if importlib.util.find_spec("pytest") is None:
        problems.append(f"{machine.python} has no pytest: run this with the venv's")
    if machine.node_version() is None:
        problems.append("no node on PATH")
    if machine.npm is None:
        problems.append("no npm on PATH")
    if not machine.node_modules.is_dir():
        problems.append("frontend/node_modules is missing: run `npm ci` in frontend/")
    return problems


def install_python_tools(machine: check.Machine) -> None:
    installed = all(
        (PYTHON_TOOLS / f"{name.replace('-', '_')}-{version}.dist-info").is_dir()
        for name, version, _ in PYTHON_PACKAGES
    )
    if installed:
        return
    _say("installing the pytest adapter in test-report/tools/python (once)")
    shutil.rmtree(PYTHON_TOOLS, ignore_errors=True)
    PYTHON_TOOLS.parent.mkdir(parents=True, exist_ok=True)
    requirements = PYTHON_TOOLS.parent / "python-requirements.txt"
    requirements.write_text(
        "".join(
            f"{name}=={version} --hash=sha256:{digest}\n"
            for name, version, digest in PYTHON_PACKAGES
        ),
        encoding="utf-8",
    )
    command = [
        machine.python,
        "-m",
        "pip",
        "install",
        "--quiet",
        "--disable-pip-version-check",
        "--require-hashes",
        "--no-deps",
        "--only-binary",
        ":all:",
        "--target",
        str(PYTHON_TOOLS),
        "--requirement",
        str(requirements),
    ]
    if machine.run(command, REPO, machine.environment) != 0:
        raise ReportError("pip did not install the pytest adapter (see above)")


def install_allure(machine: check.Machine) -> None:
    manifest = ALLURE_CLI.parent / "package.json"
    try:
        found = json.loads(manifest.read_text(encoding="utf-8"))["version"]
    except (OSError, ValueError, KeyError):
        found = None
    if found == ALLURE_VERSION:
        return
    _say(f"installing allure {ALLURE_VERSION} in test-report/tools/node (once)")
    shutil.rmtree(NODE_TOOLS, ignore_errors=True)
    NODE_TOOLS.mkdir(parents=True, exist_ok=True)
    package = {
        "name": "solidvision-test-report-tools",
        "private": True,
        "dependencies": {"allure": ALLURE_VERSION},
    }
    (NODE_TOOLS / "package.json").write_text(
        json.dumps(package, indent=2) + "\n", encoding="utf-8"
    )
    command = [machine.npm or "npm", "install", "--no-audit", "--no-fund"]
    if machine.run(command, NODE_TOOLS, machine.environment) != 0:
        raise ReportError("npm did not install allure (see above)")


# --------------------------------------------------------------------------
# The backend: pytest, with Allure's adapter
# --------------------------------------------------------------------------


def backend_environment(environment: Mapping[str, str]) -> dict[str, str]:
    """The suite's environment, with the adapter importable.

    pytest finds the adapter by its entry point, which it reads from every
    folder of `sys.path`. `backend` is there for `python -m app...embedded`.
    """
    paths = [str(PYTHON_TOOLS), str(REPO / "backend")]
    if environment.get("PYTHONPATH"):
        paths.append(environment["PYTHONPATH"])
    return {
        **check.pytest_environment(environment),
        "PYTHONPATH": os.pathsep.join(paths),
    }


def distinguish_parameters(results: Path) -> int:
    """Give the cases of one test that the adapter wrote alike a parameter of
    their own. Returns how many results changed.

    Allure takes two results with one `fullName` and the same parameters for
    two attempts at one test, and the report shows only the last. The adapter
    writes a `bytes` parameter as `<class 'bytes'>`, whatever its value, so
    the cases of a test parametrized with `bytes` were all one test: seven
    tests of this suite were out of the first report. pytest's own id, which
    is in the result's name, is unique.
    """
    alike: dict[str, list[tuple[Path, dict[str, Any]]]] = {}
    for path in results.glob("*-result.json"):
        result = json.loads(path.read_text(encoding="utf-8"))
        key = json.dumps([result.get("fullName"), result.get("parameters")])
        alike.setdefault(key, []).append((path, result))
    changed = 0
    for cases in alike.values():
        if len(cases) < 2:
            continue
        for path, result in cases:
            parameters = [*result.get("parameters", [])]
            parameters.append({"name": "id", "value": result["name"]})
            result["parameters"] = parameters
            path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            changed += 1
    return changed


def coverage_arguments() -> list[str]:
    """pytest-cov's options: `app`, by line, as a page and as JSON (for the
    total), and nothing on the console, where it would be 300 lines."""
    return [
        "--cov=app",
        f"--cov-report=html:{COVERAGE}",
        f"--cov-report=json:{COVERAGE_JSON}",
        "--cov-report=",
    ]


def read_coverage() -> tuple[float, int, int]:
    """(percent, lines run, lines measured) of the last run."""
    totals = json.loads(COVERAGE_JSON.read_text(encoding="utf-8"))["totals"]
    return (
        float(totals["percent_covered"]),
        int(totals["covered_lines"]),
        int(totals["num_statements"]),
    )


def run_backend(machine: check.Machine, coverage: bool) -> tuple[int, int]:
    """(pytest's exit code, how many results it wrote)."""
    pytest = [machine.python, "-m", "pytest", f"--alluredir={RESULTS}"]
    environment = backend_environment(machine.environment)
    if coverage:
        for path in (COVERAGE, COVERAGE_JSON, COVERAGE_DATA):
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
        pytest += coverage_arguments()
        environment["COVERAGE_FILE"] = str(COVERAGE_DATA)
    connected, said = machine.database()
    if connected:
        _say(f"the database answers ({said})")
        command = pytest
    elif EMBEDDED_RUNTIME.is_dir():
        _say("no database answers: pytest runs inside the embedded cluster")
        embedded = "app.infrastructure.database.embedded"
        command = [machine.python, "-m", embedded, "run", "--", *pytest]
    else:
        raise ReportError(check.database_message(said))
    _say(f"> {' '.join(command)}")
    code = machine.run(command, REPO, environment)
    written = len(list(RESULTS.glob("*-result.json")))
    if code not in FINISHED or not written:
        raise ReportError(
            f"pytest did not run to the end (exit code {code}, {written} results)"
        )
    if coverage and not (COVERAGE_INDEX.is_file() and COVERAGE_JSON.is_file()):
        raise ReportError("pytest-cov wrote no coverage report (see above)")
    distinguish_parameters(RESULTS)
    return code, written


# --------------------------------------------------------------------------
# The frontend: Vitest's JSON report, as Allure's results
# --------------------------------------------------------------------------


def _vitest_result(
    path: tuple[str, ...],
    name: str,
    status: str,
    start: float,
    stop: float,
    labels: Mapping[str, str],
    failures: list[str],
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "uuid": str(uuid.uuid4()),
        "name": name,
        "status": status,
        "stage": "finished",
        "start": round(start),
        "stop": round(stop),
        "titlePath": list(path),
        "labels": [{"name": key, "value": value} for key, value in labels.items()],
    }
    if failures:
        first = failures[0].strip().splitlines()
        result["statusDetails"] = {
            "message": first[0] if first else "",
            "trace": "\n\n".join(failures),
        }
    return result


def vitest_results(report: Mapping[str, Any], host: str) -> list[dict[str, Any]]:
    """Vitest's JSON report (Jest's shape) as Allure results, one per test.

    The tree of the report follows `titlePath`: `frontend`, the folders, the
    file and the `describe` blocks, beside the `backend` of the other suite.
    A file that failed before any test ran (an import that throws) becomes one
    broken result, so that it shows in the report.

    Two tests with one name in one block get a number: Allure takes results
    with the same `historyId` for retries of one test, and shows only the last.

    Vitest reports when a file started and ended, and how long each test took,
    but not when each began. The tests are laid end to end from the file's
    start, and the files that overlap go to different lanes of the timeline,
    as the workers that ran them were.
    """
    results = []
    seen: dict[str, int] = {}
    lanes: list[float] = []
    for file in sorted(report["testResults"], key=lambda file: file["startTime"]):
        parts = Path(os.path.relpath(file["name"], FRONTEND)).parts
        start, end = file["startTime"], file["endTime"]
        lane = next((i for i, free in enumerate(lanes) if free <= start), len(lanes))
        lanes[lane : lane + 1] = [end]
        tests = [
            (
                tuple(test["ancestorTitles"]),
                test["title"],
                test["status"],
                test.get("duration") or 0,
                list(test.get("failureMessages") or []),
            )
            for test in file["assertionResults"]
        ]
        if not tests and file["status"] == "failed":
            message = file.get("message") or "the file failed before any test ran"
            tests = [((), parts[-1], "broken", end - start, [message])]
        for blocks, title, status, duration, failures in tests:
            path = ("frontend", *parts, *blocks)
            full_name = (
                f"{'/'.join(path[: 1 + len(parts)])}#{' > '.join((*blocks, title))}"
            )
            seen[full_name] = seen.get(full_name, 0) + 1
            if seen[full_name] > 1:
                title = f"{title} ({seen[full_name]})"
                full_name = f"{full_name} ({seen[full_name]})"
            identity = hashlib.md5(
                full_name.encode("utf-8"), usedforsecurity=False
            ).hexdigest()
            labels = {
                "framework": "vitest",
                "language": "javascript",
                "host": host,
                "thread": f"vitest-{lane + 1}",
                "parentSuite": ".".join(path[: len(parts)]),
                "suite": parts[-1],
                "package": ".".join(path[: 1 + len(parts)]),
            }
            if blocks:
                labels["subSuite"] = " > ".join(blocks)
            result = _vitest_result(
                path,
                title,
                status if status in ("passed", "failed", "broken") else "skipped",
                start,
                start + duration,
                labels,
                failures,
            )
            result.update(
                {"fullName": full_name, "historyId": identity, "testCaseId": identity}
            )
            results.append(result)
            start += duration
    return results


def run_frontend(machine: check.Machine) -> tuple[int, int]:
    """(Vitest's exit code, how many results were written)."""
    VITEST_JSON.unlink(missing_ok=True)
    # Relative, with `/`: `npm` is a `.cmd` on Windows, and this way no folder
    # above the repository, whatever its name, goes through `cmd.exe`.
    output = Path(os.path.relpath(VITEST_JSON, FRONTEND)).as_posix()
    command = [
        machine.npm or "npm",
        "test",
        "--",
        "--reporter=default",
        "--reporter=json",
        f"--outputFile.json={output}",
    ]
    _say(f"> {' '.join(command)}  (in frontend)")
    code = machine.run(command, FRONTEND, machine.environment)
    try:
        report = json.loads(VITEST_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ReportError(
            f"Vitest wrote no report (exit code {code}): {error}"
        ) from error
    results = vitest_results(report, socket.gethostname())
    for result in results:
        (RESULTS / f"{result['uuid']}-result.json").write_text(
            json.dumps(result, ensure_ascii=False), encoding="utf-8"
        )
    if code not in FINISHED or not results:
        raise ReportError(
            f"Vitest did not run to the end (exit code {code}, {len(results)} results)"
        )
    return code, len(results)


# --------------------------------------------------------------------------
# The report
# --------------------------------------------------------------------------


def generate(machine: check.Machine) -> dict[str, int]:
    """Write the report from the results. Returns Allure's count per status."""
    config = {
        "name": REPORT_NAME,
        "output": str(REPORT),
        "historyPath": str(HISTORY),
        "plugins": {
            "awesome": {
                "options": {
                    "reportName": REPORT_NAME,
                    "singleFile": True,
                    "reportLanguage": "pt",
                    "theme": "light",
                }
            }
        },
    }
    CONFIG.write_text(
        json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    shutil.rmtree(REPORT, ignore_errors=True)
    node = shutil.which("node") or "node"
    command = [node, str(ALLURE_CLI), "generate", str(RESULTS), "--config", str(CONFIG)]
    if machine.run(command, ROOT, machine.environment) != 0 or not INDEX.is_file():
        raise ReportError("allure wrote no report (see above)")
    summary = json.loads((REPORT / "summary.json").read_text(encoding="utf-8"))
    stats: dict[str, int] = summary["stats"]
    return stats


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(
        prog="python scripts/test_report.py",
        description=(__doc__ or "").splitlines()[0],
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="write the report without opening it in the browser",
    )
    parser.add_argument(
        "--coverage",
        action="store_true",
        help="also measure the backend's coverage, in test-report/coverage",
    )
    args = parser.parse_args(argv)
    machine = check.default_machine()
    try:
        problems = missing_requirements(machine)
        if problems:
            raise ReportError("\n  ".join(["cannot run:", *problems]))
        install_python_tools(machine)
        install_allure(machine)
        shutil.rmtree(RESULTS, ignore_errors=True)
        RESULTS.mkdir(parents=True, exist_ok=True)
        backend, backend_count = run_backend(machine, args.coverage)
        frontend, frontend_count = run_frontend(machine)
        stats = generate(machine)
        written = backend_count + frontend_count
        if stats.get("total") != written:
            raise ReportError(
                f"the suites wrote {written} results and the report has "
                f"{stats.get('total')} tests: it would leave some out"
            )
    except ReportError as error:
        print(f"test_report: {error}", file=sys.stderr)
        return 2
    for suite, code, count in (
        ("backend (pytest)", backend, backend_count),
        ("frontend (vitest)", frontend, frontend_count),
    ):
        _say(f"{suite:<18} {count:>5} tests, {'passed' if code == 0 else 'FAILED'}")
    counted = ", ".join(
        f"{count} {status}" for status, count in stats.items() if status != "total"
    )
    _say(f"{'in the report':<18} {stats.get('total', 0):>5} tests: {counted}")
    _say(str(INDEX))
    if args.coverage:
        percent, run, measured = read_coverage()
        _say(
            f"{'backend coverage':<18} {percent:.1f}% "
            f"({run} of {measured} lines of backend/app)"
        )
        _say(str(COVERAGE_INDEX))
    if not args.no_open:
        webbrowser.open(INDEX.as_uri())
        if args.coverage:
            webbrowser.open(COVERAGE_INDEX.as_uri())
    return 0 if backend == 0 and frontend == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
