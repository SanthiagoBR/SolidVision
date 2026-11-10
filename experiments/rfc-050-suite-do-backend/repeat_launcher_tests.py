"""RFC-050 section 2 pilot: the two launcher tests, as pytest runs them, many times.

    .venv\\Scripts\\python.exe -m pytest experiments/rfc-050-suite-do-backend/repeat_launcher_tests.py -q -p no:cacheprovider
    $env:REPEAT_BUSY = "1"; (the same)        # with every logical CPU busy

The tests are the suite's own methods, called unchanged, so pytest's
assertion rewriting says which assertion failed. Not collected by the suite:
the name does not match `test_*.py`, and `experiments/` is outside
`testpaths`.

Section 10's series, recorded (RFC-050 implementation):

    .venv\\Scripts\\python.exe experiments/rfc-050-suite-do-backend/repeat_launcher_tests.py <label>

runs the file 300 times idle and 40 times with every CPU busy, each in its
own pytest, and adds `<label>` to `repeat_launcher_tests.json` beside this
file: the commit, a hash of the two suite files (which version of the tests
ran), and each test's runs, failures and failing assertions.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ElementTree
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO / "backend"), str(REPO / "backend" / "tests" / "launcher")]

import test_launcher_children as children_tests  # noqa: E402
import test_launcher_instance as instance_tests  # noqa: E402

RUNS = int(os.environ.get("REPEAT_RUNS", "40"))


@pytest.fixture(scope="session", autouse=True)
def load() -> Iterator[None]:
    hogs = []
    if os.environ.get("REPEAT_BUSY") == "1":
        hogs = [
            subprocess.Popen([sys.executable, "-c", "while True: pass"])
            for _ in range(os.cpu_count() or 1)
        ]
    yield
    for hog in hogs:
        hog.kill()
        hog.wait()


@pytest.mark.parametrize("run", range(RUNS))
def test_last_fifty(run: int) -> None:
    children_tests.test_the_last_fifty_lines_are_kept()


@pytest.mark.parametrize("run", range(RUNS))
def test_half_a_file(run: int, tmp_path: Path) -> None:
    instance_tests.TestTheStateFile().test_a_reader_never_sees_half_a_file(tmp_path)


# --------------------------------------------------------------------------
# The fixes RFC-050 section 5 proposes, as they would be written in the
# suite, to be run the same number of times.
# --------------------------------------------------------------------------

ONLY_STDOUT = f"""
import sys
for number in range({children_tests.TAIL_LINES + 10}):
    print("line", number, flush=True)
sys.stdin.buffer.read()
print("stdin ended", flush=True)
"""


@pytest.mark.parametrize("run", range(RUNS))
def test_last_fifty_fixed(run: int) -> None:
    child, _ = children_tests.start(ONLY_STDOUT)
    child.request_stop()
    children_tests.wait_for_exit(child)
    assert child.drain()

    lines = child.last_lines()
    assert len(lines) == children_tests.TAIL_LINES
    assert lines[-1] == "stdin ended"
    assert children_tests.last_line(lines) == "stdin ended"


DEADLINE = 30.0


@pytest.mark.parametrize("run", range(RUNS))
def test_half_a_file_fixed(run: int, tmp_path: Path) -> None:
    import json
    import threading
    import time

    from app.launcher.instance import InstanceRecord, LauncherState, StateFile

    state = StateFile(tmp_path / "launcher.json")
    state.write(instance_tests.RECORD)
    stop = threading.Event()
    written: list[int] = []

    def writer() -> None:
        port = 50000
        while not stop.is_set():
            port += 1
            state.write(
                InstanceRecord(
                    state=LauncherState.READY,
                    url="http://127.0.0.1:" + str(port) + "/" + "x" * 20_000,
                    last_ready_port=port,
                )
            )
            written.append(port)

    thread = threading.Thread(target=writer)
    thread.start()
    reads = 0
    deadline = time.monotonic() + DEADLINE
    try:
        while reads <= 10 or len(written) <= 10:
            assert time.monotonic() < deadline, (
                f"{reads} reads and {len(written)} writes in {DEADLINE:.0f} s"
            )
            try:
                text = (tmp_path / "launcher.json").read_text(encoding="utf-8")
            except PermissionError:
                continue
            record = InstanceRecord.from_json(json.loads(text))
            assert record.state is LauncherState.READY
            reads += 1
    finally:
        stop.set()
        thread.join(10)


# --------------------------------------------------------------------------
# The series of section 10, each in its own pytest, recorded
# --------------------------------------------------------------------------

HERE = Path(__file__).resolve().parent
OUT = HERE / "repeat_launcher_tests.json"
SUITE_FILES = [
    REPO / "backend" / "tests" / "launcher" / "test_launcher_children.py",
    REPO / "backend" / "tests" / "launcher" / "test_launcher_instance.py",
]
CONDITIONS = (("idle", 300, False), ("busy", 40, True))


def _git(*arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout.strip()


def _series(runs: int, busy: bool) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="rfc050-repeat-") as folder:
        report = Path(folder) / "junit.xml"
        environment = {
            **os.environ,
            "REPEAT_RUNS": str(runs),
            "REPEAT_BUSY": "1" if busy else "0",
        }
        started = time.perf_counter()
        subprocess.run(
            [
                sys.executable, "-m", "pytest", str(Path(__file__)), "-q",
                "-p", "no:cacheprovider", f"--junitxml={report}",
            ],
            cwd=REPO,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
        )
        wall = time.perf_counter() - started
        cases = ElementTree.parse(report).getroot().iter("testcase")
        tests: dict[str, dict[str, Any]] = {}
        for case in cases:
            name = case.get("name", "").split("[")[0]
            entry = tests.setdefault(
                name, {"runs": 0, "failures": 0, "max_seconds": 0.0, "assertions": Counter()}
            )
            entry["runs"] += 1
            entry["max_seconds"] = max(entry["max_seconds"], float(case.get("time", 0)))
            for problem in (*case.iter("failure"), *case.iter("error")):
                entry["failures"] += 1
                message = (problem.get("message") or "").splitlines()
                entry["assertions"][message[0][:200] if message else "?"] += 1
    for entry in tests.values():
        entry["max_seconds"] = round(entry["max_seconds"], 2)
        entry["assertions"] = dict(entry["assertions"])
    return {"runs": runs, "busy_processes": (os.cpu_count() or 1) if busy else 0,
            "wall_seconds": round(wall, 1), "tests": tests}


def main() -> int:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    label = sys.argv[1]
    result: dict[str, Any] = (
        json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    )
    result["machine"] = {"cpus": os.cpu_count(), "python": sys.version.split()[0]}
    series: dict[str, Any] = {
        "when": time.strftime("%Y-%m-%d %H:%M"),
        "commit": _git("rev-parse", "--short", "HEAD"),
        "suite_files_changed": _git("status", "--porcelain", "--", *map(str, SUITE_FILES)) != "",
        "suite_files_sha256": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()[:16]
            for path in SUITE_FILES
        },
        "conditions": {},
    }
    for name, runs, busy in CONDITIONS:
        series["conditions"][name] = _series(runs, busy)
        for test, entry in series["conditions"][name]["tests"].items():
            print(f"{label} {name}: {test} {entry['failures']} of {entry['runs']} failed "
                  f"{entry['assertions'] or ''}", flush=True)
    result.setdefault("series", {})[label] = series
    OUT.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
