"""RFC-050 section 2 pilot: the two launcher tests that fail under load.

    .venv\\Scripts\\python.exe experiments/rfc-050-suite-do-backend/pilot_flaky.py

Runs the body of each test many times, first with the machine idle and then
with every logical CPU busy (one `while True: pass` process per CPU), and
records what each failure looks like. The bodies are the tests' own, with
counters added, so a failure says which assertion would have failed and
why:

- `test_the_last_fifty_lines_are_kept`: what `drain()` returned, how long
  it took, how many lines were kept, which was last, and the last line
  after a second, longer `drain()`.
- `TestTheStateFile::test_a_reader_never_sees_half_a_file`: reads, writes,
  refused opens, and the first exception the reader met, if any.

Writes `pilot_flaky.json` beside this file. Nothing here changes the code.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path[:0] = [str(REPO / "backend"), str(REPO / "backend" / "tests" / "launcher")]

import test_launcher_children as children_tests  # noqa: E402
import test_launcher_instance as instance_tests  # noqa: E402
from app.launcher.children import TAIL_LINES  # noqa: E402
from app.launcher.instance import (  # noqa: E402
    InstanceRecord,
    LauncherState,
    StateFile,
)

RUNS_LINES = 100
RUNS_STATE = 60


def last_fifty_once() -> dict[str, Any]:
    child, _ = children_tests.start(children_tests.UNTIL_STDIN_ENDS)
    child.request_stop()
    children_tests.wait_for_exit(child)
    started = time.perf_counter()
    drained = child.drain()
    drain_seconds = time.perf_counter() - started
    lines = child.last_lines()
    passed = len(lines) == TAIL_LINES and bool(lines) and lines[-1] == "stdin ended"
    record: dict[str, Any] = {
        "passed": passed,
        "drained": drained,
        "drain_seconds": round(drain_seconds, 3),
        "kept": len(lines),
        "last": lines[-1] if lines else None,
    }
    if not passed:
        late = child.drain(30)
        later = child.last_lines()
        record |= {
            "late_drained": late,
            "late_kept": len(later),
            "late_last": later[-1] if later else None,
            "warning_index": (
                later.index("a warning on stderr")
                if "a warning on stderr" in later
                else None
            ),
        }
    return record


def state_file_once() -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "launcher.json"
        state = StateFile(path)
        state.write(instance_tests.RECORD)
        stop = threading.Event()
        written: list[int] = []
        refused_writes = 0

        def writer() -> None:
            nonlocal refused_writes
            port = 50000
            while not stop.is_set():
                port += 1
                ok = state.write(
                    InstanceRecord(
                        state=LauncherState.READY,
                        url="http://127.0.0.1:" + str(port) + "/" + "x" * 20_000,
                        last_ready_port=port,
                    )
                )
                if not ok:
                    refused_writes += 1
                written.append(port)

        thread = threading.Thread(target=writer)
        thread.start()
        reads = 0
        denied = 0
        error: str | None = None
        deadline = time.monotonic() + 1.0
        try:
            while time.monotonic() < deadline:
                try:
                    text = path.read_text(encoding="utf-8")
                except PermissionError:
                    denied += 1
                    continue
                except OSError as caught:
                    error = f"{type(caught).__name__}: {caught}"
                    break
                try:
                    record = InstanceRecord.from_json(json.loads(text))
                except ValueError as caught:
                    error = f"{type(caught).__name__} on {len(text)} characters"
                    break
                if record.state is not LauncherState.READY:
                    error = f"state {record.state}"
                    break
                reads += 1
        finally:
            stop.set()
            thread.join(10)
    passed = error is None and reads > 10 and len(written) > 10
    return {
        "passed": passed,
        "reads": reads,
        "written": len(written),
        "refused_writes": refused_writes,
        "denied_opens": denied,
        "error": error,
    }


def busy(count: int) -> list[subprocess.Popen[bytes]]:
    return [
        subprocess.Popen([sys.executable, "-c", "while True: pass"])
        for _ in range(count)
    ]


def series(name: str, body: Any, runs: int) -> dict[str, Any]:
    records = []
    started = time.perf_counter()
    for _ in range(runs):
        records.append(body())
    seconds = time.perf_counter() - started
    failures = [r for r in records if not r["passed"]]
    summary: dict[str, Any] = {
        "runs": runs,
        "failures": len(failures),
        "seconds": round(seconds, 1),
        "failure_records": failures,
    }
    if name == "lines":
        summary["drained_false"] = sum(1 for r in records if not r["drained"])
        summary["last_lines"] = dict(Counter(str(r["last"]) for r in records))
        summary["max_drain_seconds"] = max(r["drain_seconds"] for r in records)
    else:
        summary["min_reads"] = min(r["reads"] for r in records)
        summary["min_written"] = min(r["written"] for r in records)
        summary["errors"] = dict(Counter(str(r["error"]) for r in records))
    print(f"{name}: {len(failures)} of {runs} failed in {seconds:.0f} s", flush=True)
    return summary


def main() -> int:
    cpus = os.cpu_count() or 1
    results: dict[str, Any] = {
        "machine": {"cpus": cpus, "python": sys.version.split()[0]},
        "conditions": {},
    }
    for condition, load in (("idle", 0), ("busy", cpus)):
        hogs = busy(load)
        try:
            time.sleep(1.0)
            results["conditions"][condition] = {
                "busy_processes": load,
                "lines": series("lines", last_fifty_once, RUNS_LINES),
                "state": series("state", state_file_once, RUNS_STATE),
            }
        finally:
            for hog in hogs:
                hog.kill()
            for hog in hogs:
                hog.wait()
        print(f"== {condition} done", flush=True)
    out = HERE / "pilot_flaky.json"
    out.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
