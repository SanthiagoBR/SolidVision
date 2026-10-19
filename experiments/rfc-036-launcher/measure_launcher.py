r"""The launcher, measured (RFC-036 section 16).

The pilot beside this script observed how Windows, `pg_ctl`, CPython and
uvicorn behave when one process asks another to stop, before any launcher
existed. This measures the launcher RFC-036 built, on a staged installation
(`scripts/stage_installation.py`, read-only) with a profile of its own: the
cold and the warm start, what "ready" costs and what the search costs after
it, each child killed in turn, a long job stopped three ways and resumed, the
launcher itself killed, the second opening, the ports, the refusals, a child
that dies in a loop, the pipe and the code page, the two modes side by side,
and what is written outside the data root.

    python measure_launcher.py > measure_launcher.log 2>&1

    --list           the checks
    --only a,b       just these (in the order of --list)
    --work PATH      the work directory (default: a temporary one)
    --keep           keep it

Every launcher is `python -m app.launcher --supervised`, started the way a
window would start it -- stdin and stdout on pipes, no window -- except the one
stopped by Ctrl+C, which gets a hidden console of its own so that a helper can
attach to it and send the event, as a terminal would.

**What is not measured here:** the logoff (by hand, RFC-036 section 16), and
the suite, which is run on its own. Nothing here touches the user's data:
every data root is under the work directory, and every process ended is one
this script created, by the PID it recorded.

No new dependency: `ctypes`, `urllib`, `subprocess`, and the project's scripts.
"""

from __future__ import annotations

import argparse
import ctypes
import datetime
import json
import os
import platform
import re
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
BACKEND = REPO / "backend"
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(SCRIPTS))

import stage_installation as staging  # noqa: E402

from app.infrastructure.database.embedded.runtime import PostgresRuntime  # noqa: E402
from app.infrastructure.database.embedded.windows_processes import (  # noqa: E402
    WindowsProcesses,
)
from dataset_tools.manifest import (  # noqa: E402
    DEMO_CORPUS_ROOT,
    DEMO_MANIFEST_PATH,
    load_manifest,
)
from dataset_tools.materialize import materialize  # noqa: E402

CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_CONSOLE = 0x00000010
STARTF_USESHOWWINDOW = 0x00000001
SW_HIDE = 0

WARM_CRITERION_SECONDS = 5.0
"""Declared before the measurement (section 16): from the command to `ready`,
warm, at most the 5 s of ARCHITECTURE.md section 22. Above it, the second
branch of section 5.1 enters, and the number is measured again."""

WARM_RUNS = 5
START_SECONDS = 600.0
STOP_SECONDS = 180.0
JOB_SECONDS = 900.0
DEMO_COUNT = 45
PORTUGUESE_QUERY = "propriedade rural com um pequeno lago"
UTF8_FOLDER = ("Łódź", "東京")
UTF8_FILE = "✈.jpg"

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

CTRL_C_HELPER = r"""
import ctypes, sys
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.FreeConsole()
if not kernel32.AttachConsole(int(sys.argv[1])):
    sys.exit(10)
kernel32.SetConsoleCtrlHandler(None, True)
sys.exit(0 if kernel32.GenerateConsoleCtrlEvent(0, 0) else 11)
"""
"""Attach to the launcher's console, ignore Ctrl+C here, and send it there:
what a Ctrl+C in a terminal is to every process sharing that console."""


# --------------------------------------------------------------------------
# Small things
# --------------------------------------------------------------------------


def say(text: str = "") -> None:
    print(f"    {text}" if text else "", flush=True)


def heading(title: str) -> None:
    print(f"\n## {title}\n", flush=True)


def seconds(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.3f} s" if value < 10 else f"{value:.1f} s"


def spread(values: Sequence[float]) -> str:
    if not values:
        return "no samples"
    if len(values) == 1:
        return seconds(values[0])
    return (
        f"median {seconds(statistics.median(values))} "
        f"(min {seconds(min(values))}, max {seconds(max(values))}, n={len(values)})"
    )


def tree_state(root: Path, skip: Path | None = None) -> dict[str, tuple[int, int]]:
    state: dict[str, tuple[int, int]] = {}
    if not root.exists():
        return state
    for directory, directories, files in os.walk(root):
        if skip is not None:
            directories[:] = [
                name
                for name in directories
                if Path(directory, name).resolve() != skip.resolve()
            ]
        relative = os.path.relpath(directory, root)
        state[relative + os.sep] = (-1, 0)
        for name in files:
            found = os.stat(os.path.join(directory, name))
            state[os.path.join(relative, name)] = (found.st_size, found.st_mtime_ns)
    return state


def changes(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(n for n in set(before) & set(after) if before[n] != after[n])
    return (
        [f"+{n}" for n in added]
        + [f"-{n}" for n in removed]
        + [f"~{n}" for n in changed]
    )


def http(
    url: str,
    method: str = "GET",
    path: str = "/health",
    body: object = None,
    timeout: float = 60.0,
) -> tuple[int, Any, float]:
    """One request; `(0, None, elapsed)` when nothing answered."""
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url + path, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    began = time.perf_counter()
    try:
        with OPENER.open(request, timeout=timeout) as answer:
            return int(answer.status), json.load(answer), time.perf_counter() - began
    except urllib.error.HTTPError as error:
        try:
            payload = json.load(error)
        except ValueError:
            payload = None
        return int(error.code), payload, time.perf_counter() - began
    except (OSError, ValueError):
        return 0, None, time.perf_counter() - began


def wait_until(
    predicate: Callable[[], Any], seconds_: float, step: float = 0.1
) -> tuple[Any, float | None]:
    """The first truthy answer and how long it took; `(None, None)` at the end."""
    began = time.perf_counter()
    while time.perf_counter() - began < seconds_:
        answer = predicate()
        if answer:
            return answer, time.perf_counter() - began
        time.sleep(step)
    return None, None


class CheckFailedError(Exception):
    """A criterion was not met. The message is the result."""


# --------------------------------------------------------------------------
# A launcher
# --------------------------------------------------------------------------


class Launcher:
    """`python -m app.launcher --supervised`, and its events, timed."""

    def __init__(
        self,
        label: str,
        env: dict[str, str],
        cwd: Path,
        out: Path,
        *,
        console: bool = False,
        arguments: Sequence[str] = ("--supervised",),
    ) -> None:
        self.label = label
        self.stderr_path = out / f"{label}.stderr.txt"
        self.events: list[tuple[float, dict[str, Any]]] = []
        startupinfo = None
        flags = CREATE_NO_WINDOW
        if console:
            flags = CREATE_NEW_CONSOLE
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = SW_HIDE
        self._stderr = self.stderr_path.open("wb")
        self.created = time.perf_counter()
        self.process = subprocess.Popen(
            [sys.executable, "-m", "app.launcher", *arguments],
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr,
            creationflags=flags,
            startupinfo=startupinfo,
        )
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        assert self.process.stdout is not None
        for raw in self.process.stdout:
            moment = time.perf_counter() - self.created
            try:
                self.events.append((moment, json.loads(raw)))
            except ValueError:
                self.events.append(
                    (moment, {"event": "NOT JSON", "line": raw.decode()})
                )

    @property
    def pid(self) -> int:
        return self.process.pid

    def first(self, name: str) -> tuple[float, dict[str, Any]] | None:
        for moment, event in list(self.events):
            if event.get("event") == name:
                return moment, event
        return None

    def all(self, name: str) -> list[tuple[float, dict[str, Any]]]:
        return [(m, e) for m, e in list(self.events) if e.get("event") == name]

    def wait_for(
        self, name: str, seconds_: float = START_SECONDS
    ) -> tuple[float, dict[str, Any]]:
        deadline = time.perf_counter() + seconds_
        while time.perf_counter() < deadline:
            found = self.first(name)
            if found is not None:
                return found
            failed = self.first("failed")
            if failed is not None and name != "failed":
                raise CheckFailedError(
                    f"{self.label}: failed: {failed[1].get('detail')}"
                )
            if self.process.poll() is not None and found is None:
                time.sleep(0.2)
                found = self.first(name)
                if found is not None:
                    return found
                raise CheckFailedError(
                    f"{self.label} exited with {self.process.returncode} before "
                    f"{name}: {self.names()}; stderr: {self.stderr_tail()}"
                )
            time.sleep(0.05)
        raise CheckFailedError(
            f"{self.label}: no {name} in {seconds_:.0f} s: {self.names()}"
        )

    def names(self) -> list[str]:
        return [str(e.get("event")) for _, e in list(self.events)]

    def close_stdin(self) -> None:
        assert self.process.stdin is not None
        self.process.stdin.close()

    def wait(self, seconds_: float = STOP_SECONDS) -> int:
        try:
            code = self.process.wait(seconds_)
        except subprocess.TimeoutExpired:
            raise CheckFailedError(
                f"{self.label} still running after {seconds_:.0f} s"
            ) from None
        self._reader.join(10)
        self._stderr.close()
        return code

    def stop(self) -> int:
        if self.process.poll() is None:
            self.close_stdin()
        return self.wait()

    def stderr_text(self) -> str:
        try:
            return self.stderr_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def stderr_tail(self, count: int = 6) -> str:
        lines = [line for line in self.stderr_text().splitlines() if line.strip()]
        return " / ".join(lines[-count:])


# --------------------------------------------------------------------------
# The measurement
# --------------------------------------------------------------------------


class Measurement:
    def __init__(self, work: Path) -> None:
        self.work = work
        self.staged = work / "Program Files" / "SolidVision"
        self.home = work / "profile"
        self.local = self.home / "AppData" / "Local"
        self.roaming = self.home / "AppData" / "Roaming"
        self.data = self.local / "SolidVision"
        self.temp = work / "temp"
        self.empty = work / "empty"
        self.out = work / "out"
        self.corpora = work / "corpora"
        self.checkout_profile = work / "checkout-profile"
        for directory in (self.local, self.roaming, self.temp, self.empty, self.out):
            directory.mkdir(parents=True, exist_ok=True)
        self.table = WindowsProcesses()
        self.device_id: str | None = None
        self.results: list[tuple[str, str]] = []
        self.launchers: list[Launcher] = []

    # -- environments -------------------------------------------------------

    def stage(self) -> None:
        """The staged installation, read-only, made once for every check."""
        if not self.staged.exists():
            began = time.perf_counter()
            staging.stage(self.staged, read_only=True)
            elapsed = seconds(time.perf_counter() - began)
            say(f"staged {self.staged}, read-only, in {elapsed}")

    def installed_env(self) -> dict[str, str]:
        environment = {
            name: value
            for name, value in os.environ.items()
            if not name.upper().startswith(("PYTHON", "HF_", "TORCH"))
        }
        environment.update(
            PYTHONPATH=str(self.staged / "backend"),
            USERPROFILE=str(self.home),
            LOCALAPPDATA=str(self.local),
            APPDATA=str(self.roaming),
            TEMP=str(self.temp),
            TMP=str(self.temp),
        )
        return environment

    def checkout_env(self) -> dict[str, str]:
        environment = dict(os.environ)
        for name in list(environment):
            if name.upper().startswith(("EMBEDDED_", "LOG_", "THUMBNAIL_")):
                del environment[name]
        environment.update(
            PYTHONPATH=str(BACKEND),
            LOCALAPPDATA=str(self.checkout_profile),
            DATABASE_SOURCE="embedded",
        )
        return environment

    def user_file(self, text: str | None) -> None:
        """`solidvision.env`, or no such file."""
        path = self.data / "solidvision.env"
        if text is None:
            path.unlink(missing_ok=True)
        else:
            self.data.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

    # -- launchers ------------------------------------------------------------

    def launch(
        self, label: str, *, console: bool = False, env: dict[str, str] | None = None
    ) -> Launcher:
        if env is None:
            self.stage()
        launcher = Launcher(
            label, env or self.installed_env(), self.empty, self.out, console=console
        )
        self.launchers.append(launcher)
        return launcher

    def state(self, data: Path | None = None) -> dict[str, Any]:
        path = (data or self.data) / "launcher.json"
        try:
            return dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return {}

    def recorded(self, launcher: Launcher, data: Path | None = None) -> set[int]:
        """Every PID of this launcher's tree that is known now: the launcher,
        its children (the venv's redirectors and the interpreters under them),
        and the postmaster."""
        state = self.state(data)
        pids = {launcher.pid, *state.get("children", {}).values()}
        if state.get("postmaster_pid"):
            pids.add(int(state["postmaster_pid"]))
        rows = self.table.snapshot()
        grown = True
        while grown:
            grown = False
            for row in rows:
                if (
                    row.parent_pid in pids
                    and row.pid not in pids
                    and row.name.lower() == "python.exe"
                ):
                    pids.add(row.pid)
                    grown = True
        return pids

    def leftovers(self, pids: set[int], runtime_root: Path) -> list[str]:
        """The criterion of RFC-033 section 18, by identity: the PIDs recorded,
        and every process of this runtime, with the `cmd.exe` and `conhost.exe`
        whose parent is one of them."""
        runtime = PostgresRuntime(runtime_root)
        rows = self.table.snapshot()
        ours = {
            row.pid
            for row in rows
            if (executable := self.table.executable(row.pid)) is not None
            and runtime.contains(executable)
        }
        found = [
            f"recorded PID {pid}" for pid in sorted(pids) if self.table.is_alive(pid)
        ]
        found += [f"runtime process {pid}" for pid in sorted(ours)]
        found += [
            f"{row.name} {row.pid} (parent {row.parent_pid})"
            for row in rows
            if row.name.lower() in ("cmd.exe", "conhost.exe")
            and row.parent_pid in (ours | pids)
            and self.table.is_alive(row.pid)
        ]
        return found

    def stop_by(
        self, launcher: Launcher, how: str, env: dict[str, str] | None = None
    ) -> tuple[int, float]:
        """Stop a launcher the way named; its exit code and how long it took."""
        began = time.perf_counter()
        if how == "stdin":
            launcher.close_stdin()
        elif how == "stop":
            done = subprocess.run(
                [sys.executable, "-m", "app.launcher", "stop"],
                cwd=self.empty,
                env=env or self.installed_env(),
                capture_output=True,
                text=True,
                timeout=STOP_SECONDS,
                creationflags=CREATE_NO_WINDOW,
            )
            if done.returncode != 0:
                raise CheckFailedError(
                    f"`stop` exited {done.returncode}: {done.stderr.strip()}"
                )
        elif how == "ctrl-c":
            done = subprocess.run(
                [sys.executable, "-c", CTRL_C_HELPER, str(launcher.pid)],
                capture_output=True,
                timeout=60,
                creationflags=CREATE_NO_WINDOW,
            )
            if done.returncode != 0:
                raise CheckFailedError(f"the Ctrl+C helper exited {done.returncode}")
        else:
            raise ValueError(how)
        code = launcher.wait()
        return code, time.perf_counter() - began

    # -- the API --------------------------------------------------------------

    def corpus(self, name: str, copies: int = 1) -> Path:
        """Copies of the 45 images of RFC-022, of its own: new paths, new work."""
        target = self.corpora / name
        if not target.exists():
            manifest = load_manifest(DEMO_MANIFEST_PATH)
            for copy in range(copies):
                materialize(manifest, DEMO_CORPUS_ROOT, target / f"copy-{copy + 1}")
        return target

    def queue(self, url: str, folder: Path) -> str:
        status, volumes, _ = http(url, path="/api/v1/volumes")
        if status != 200:
            raise CheckFailedError(f"GET /api/v1/volumes: {status} {volumes}")
        lowered = str(folder).lower()
        candidates = [
            volume
            for volume in volumes["volumes"]
            if lowered.startswith(volume["mount_point"].lower().replace("/", "\\"))
        ]
        volume = max(candidates, key=lambda v: len(v["mount_point"]))
        if volume.get("device_id"):
            device_id = volume["device_id"]
        else:
            status, device, _ = http(
                url,
                "POST",
                "/api/v1/devices",
                {
                    "volume_identity": volume["volume_identity"],
                    "volume_kind": volume["volume_kind"],
                    "label": "measure",
                },
            )
            if status >= 300:
                raise CheckFailedError(f"POST /api/v1/devices: {status} {device}")
            device_id = device["id"]
        scope = folder.relative_to(Path(volume["mount_point"]))
        status, job, _ = http(
            url,
            "POST",
            "/api/v1/jobs",
            {"device_id": device_id, "scopes": [str(scope)]},
        )
        if status >= 300:
            raise CheckFailedError(f"POST /api/v1/jobs: {status} {job}")
        self.device_id = device_id
        return str(job["id"])

    @staticmethod
    def job(url: str, job_id: str) -> dict[str, Any]:
        status, job, _ = http(url, path=f"/api/v1/jobs/{job_id}")
        if status != 200:
            raise CheckFailedError(f"GET job: {status} {job}")
        return dict(job)

    @staticmethod
    def job_line(job: dict[str, Any]) -> str:
        return (
            f"{job['status']}, attempts {job['attempts']}, processed "
            f"{job['processed_images']}, skipped {job['skipped_images']}, failed "
            f"{job['failed_images']}, checkpoint "
            f"{job['last_processed_relative_path']!r}"
        )

    def record(self, name: str, result: str) -> None:
        self.results.append((name, result))
        say(f"RESULT {name}: {result}")


# --------------------------------------------------------------------------
# The checks
# --------------------------------------------------------------------------


def check_import(m: Measurement) -> None:
    """Section 5.3: the import of the API, without `torch`. Warm, five fresh
    processes; the import before RFC-036 was 20.2 s (`-X importtime`)."""
    script = (
        "import sys, time\n"
        "began = time.perf_counter()\n"
        "import app.presentation.api\n"
        "elapsed = time.perf_counter() - began\n"
        "print(elapsed, sorted(n for n in ('torch', 'transformers') if n in "
        "sys.modules))\n"
    )
    m.stage()
    times = []
    loaded = set()
    for _ in range(5):
        done = subprocess.run(
            [sys.executable, "-c", script],
            cwd=m.empty,
            env=m.installed_env(),
            capture_output=True,
            text=True,
            timeout=300,
            creationflags=CREATE_NO_WINDOW,
        )
        if done.returncode != 0:
            raise CheckFailedError(done.stderr[-800:])
        elapsed, modules = done.stdout.strip().split(" ", 1)
        times.append(float(elapsed))
        loaded |= set(json.loads(modules.replace("'", '"')))
    m.record(
        "import of the API, installed",
        f"{spread(times)}; model libraries loaded: {sorted(loaded) or 'none'}",
    )


def phases_of(launcher: Launcher) -> str:
    return ", ".join(
        f"{event.get('phase', event['event'])} {moment:.2f}"
        for moment, event in launcher.events
        if event["event"] in ("starting", "phase", "ready")
    )


def check_start(m: Measurement) -> None:
    """Cold (an empty data root, `initdb`) and warm (the cluster stopped, the
    files cached by the start before): from the command to `ready`, and each
    phase from the events. The stop after each, by stdin."""
    m.stage()
    before = {
        "profile outside the data root": tree_state(m.home, skip=m.data),
        "the children's TEMP": tree_state(m.temp),
        "the installed tree": tree_state(m.staged),
        "the working directory": tree_state(m.empty),
    }

    cold = m.launch("start-cold")
    ready_at, ready = cold.wait_for("ready")
    say(f"cold: {phases_of(cold)}")
    m.record(
        "cold start, command to ready",
        f"{seconds(ready_at)}; {phases_of(cold)}; search {ready.get('search')}",
    )
    pids = m.recorded(cold)
    code, elapsed = m.stop_by(cold, "stdin")
    left = m.leftovers(pids, m.staged / "runtime" / "postgres")
    m.record(
        "cold stop by stdin",
        f"exit {code} in {seconds(elapsed)}; {stop_detail(cold)}; left behind: "
        f"{left or 'nothing'}",
    )

    warm_times = []
    for run in range(WARM_RUNS):
        warm = m.launch(f"start-warm-{run}")
        ready_at, ready = warm.wait_for("ready")
        warm_times.append(ready_at)
        say(f"warm {run}: {seconds(ready_at)}: {phases_of(warm)}")
        if run in (0, WARM_RUNS - 1):
            # The first, right after the cold start; the last, with the files
            # of `torch` in the cache by then.
            search_after_ready(m, warm, ready, f"warm run {run}, empty index")
        pids = m.recorded(warm)
        code, elapsed = m.stop_by(warm, "stdin")
        left = m.leftovers(pids, m.staged / "runtime" / "postgres")
        say(
            f"warm {run} stop: exit {code} in {seconds(elapsed)}; {stop_detail(warm)}; "
            f"left: {left or 'nothing'}"
        )
    verdict = (
        "within" if statistics.median(warm_times) <= WARM_CRITERION_SECONDS else "ABOVE"
    )
    m.record(
        "warm start, command to ready",
        f"{spread(warm_times)}: {verdict} the {WARM_CRITERION_SECONDS:.0f} s criterion",
    )

    after = {
        "profile outside the data root": tree_state(m.home, skip=m.data),
        "the children's TEMP": tree_state(m.temp),
        "the installed tree": tree_state(m.staged),
        "the working directory": tree_state(m.empty),
    }
    written = {name: changes(before[name], after[name]) for name in before}
    for name, found in written.items():
        say(f"{name}: {found[:8] or 'unchanged'}")
    m.record(
        "nothing written outside the data root",
        "; ".join(f"{name}: {len(found)} change(s)" for name, found in written.items()),
    )
    root_entries = sorted(path.name for path in m.data.iterdir())
    m.record("the data root after the starts", ", ".join(root_entries))


def stop_detail(launcher: Launcher) -> str:
    text = launcher.stderr_text()
    parts = re.findall(r"(api|job_runner) stopped in ([\d.]+) s", text)
    database = re.findall(
        r"Database: (stopped \([a-z]+\)|not running) in ([\d.]+) s", text
    )
    ended = re.findall(r"Ended (\w+) with TerminateProcess", text)
    stopping = launcher.first("stopping")
    stopped = launcher.first("stopped")
    total = (stopped[0] - stopping[0]) if stopping and stopped else None
    pieces = [f"{name} {value} s" for name, value in parts]
    pieces += [f"database {what} in {value} s" for what, value in database]
    if ended:
        pieces.append(f"TERMINATED: {', '.join(ended)}")
    return (
        f"stopping->stopped {seconds(total)} "
        f"({', '.join(pieces) or 'no stage timings'})"
    )


def search_after_ready(
    m: Measurement, launcher: Launcher, ready: dict[str, Any], label: str
) -> None:
    """From `ready` to `search: ready`, the latency of `/health` meanwhile, and
    one Portuguese search sent right after `ready`, which waits for the load.
    And when the job runner entered its loop, from the launcher's command."""
    url = ready["url"]
    looping: list[float] = []

    def watch_the_job_runner() -> None:
        while time.perf_counter() - launcher.created < 600:
            if "Job executor started" in launcher.stderr_text():
                looping.append(time.perf_counter() - launcher.created)
                return
            time.sleep(0.2)

    watcher = threading.Thread(target=watch_the_job_runner, daemon=True)
    watcher.start()
    answer: dict[str, Any] = {}

    def search() -> None:
        query = urllib.parse.quote(PORTUGUESE_QUERY)
        answer["result"] = http(
            url, path=f"/api/v1/images/search?q={query}", timeout=600
        )

    began = time.perf_counter()
    searcher = threading.Thread(target=search)
    searcher.start()
    latencies = []
    states = []
    while time.perf_counter() - began < 600:
        status, body, elapsed = http(url, timeout=60)
        latencies.append(elapsed)
        state = (body or {}).get("search")
        states.append(state)
        if state in ("ready", "unavailable", "on_demand"):
            break
        time.sleep(0.2)
    to_search_ready = time.perf_counter() - began
    searcher.join(600)
    watcher.join(600)
    status, body, elapsed = answer.get("result", (0, None, 0.0))
    hits = len(body.get("results", [])) if isinstance(body, dict) else 0
    m.record(
        f"ready to search ready, {label}",
        f"{seconds(to_search_ready)}; final search state "
        f"{states[-1] if states else None}",
    )
    m.record(f"/health during the warm-up, {label}", f"{spread(latencies)}")
    m.record(
        f"a Portuguese search sent at ready, {label}",
        f"{status} in {seconds(elapsed)} ({hits} hits)",
    )
    m.record(
        f"the job runner in its loop, {label}",
        f"{seconds(looping[0]) if looping else 'not seen'} after the command",
    )


def check_second(m: Measurement) -> None:
    """Section 8: a second launcher with the first up, and with the first still
    starting. `already_running`, exit 0, and no process left of its own."""
    first = m.launch("second-first")
    first.wait_for("ready")
    times = []
    left: list[str] = []
    for _ in range(3):
        began = time.perf_counter()
        second = subprocess.Popen(
            [sys.executable, "-m", "app.launcher"],
            cwd=m.empty,
            env=m.installed_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=CREATE_NO_WINDOW,
            stdin=subprocess.DEVNULL,
        )
        out, _ = second.communicate(timeout=120)
        times.append(time.perf_counter() - began)
        # Its own processes: the venv's redirector and the interpreter under it.
        left += [
            f"{row.name} {row.pid}"
            for row in m.table.snapshot()
            if (row.pid == second.pid or row.parent_pid == second.pid)
            and m.table.is_alive(row.pid)
        ]
    m.record(
        "second opening, first up",
        f"{spread(times)}, exit {second.returncode}, {out.strip()}, its processes "
        f"left: {left or 'none'}",
    )
    m.stop_by(first, "stdin")

    starting = m.launch("second-starting")
    starting.wait_for("starting", 120)
    began = time.perf_counter()
    done = subprocess.run(
        [sys.executable, "-m", "app.launcher"],
        cwd=m.empty,
        env=m.installed_env(),
        capture_output=True,
        text=True,
        timeout=120,
        creationflags=CREATE_NO_WINDOW,
        stdin=subprocess.DEVNULL,
    )
    m.record(
        "second opening, first still starting",
        f"{seconds(time.perf_counter() - began)}, exit {done.returncode}, "
        f"{done.stdout.strip()}",
    )
    starting.wait_for("ready")
    m.stop_by(starting, "stdin")


def check_kills(m: Measurement) -> None:
    """Section 6: the API killed, the postmaster killed, a server process that
    is not the postmaster killed -- one launcher, the app ready each time."""
    launcher = m.launch("kills")
    _, ready = launcher.wait_for("ready")
    url = ready["url"]

    # The API.
    state = m.state()
    api = int(state["children"]["api"])
    began = time.perf_counter()
    m.table.terminate(api)
    current = [url]

    def answers() -> bool:
        addresses = launcher.all("address")
        if addresses:
            current[0] = addresses[-1][1]["url"]
        return http(current[0], timeout=5)[0] == 200

    _, back = wait_until(answers, 300)
    children = [e for _, e in launcher.all("child") if e.get("name") == "api"]
    m.record(
        "API killed: /health 200 again",
        f"{seconds(back)}; events {[e['state'] for e in children]}; address: "
        f"{bool(launcher.all('address'))}",
    )
    url = current[0]

    # The postmaster.
    state = m.state()
    before = dict(state["children"])
    postmaster = int(state["postmaster_pid"])
    began = time.perf_counter()
    m.table.terminate(postmaster)

    def database_event(kind: str) -> Callable[[], bool]:
        return lambda: any(
            e.get("name") == "database" and e.get("state") == kind
            for _, e in launcher.all("child")
        )

    _, noticed = wait_until(database_event("restarting"), 60, 0.02)
    _, restarted = wait_until(database_event("running"), 120, 0.02)
    restarted = None if restarted is None else (noticed or 0) + restarted
    _, health = wait_until(lambda: http(url, timeout=5)[0] == 200, 120)
    health_total = time.perf_counter() - began
    after = dict(m.state().get("children", {}))
    m.record(
        "postmaster killed",
        f"noticed {seconds(noticed)}, start() back {seconds(restarted)}, /health 200 "
        f"at {seconds(health_total)} "
        f"after the kill; children unchanged: {before == after}",
    )

    # A server process that is not the postmaster.
    postmaster = int(m.state()["postmaster_pid"])
    runtime = PostgresRuntime(m.staged / "runtime" / "postgres")
    children_of = [
        row.pid
        for row in m.table.snapshot()
        if row.parent_pid == postmaster
        and row.name.lower() == "postgres.exe"
        and (exe := m.table.executable(row.pid)) is not None
        and runtime.contains(exe)
    ]
    events_before = len(launcher.all("child"))
    # The newest child: a backend, most likely the API's pooled connection.
    # Its abnormal end makes the postmaster reinitialize every other one.
    began = time.perf_counter()
    m.table.terminate(children_of[-1])
    timeline: list[tuple[float, int]] = []
    while time.perf_counter() - began < 30:
        timeline.append((time.perf_counter() - began, http(url, timeout=10)[0]))
        time.sleep(0.1)
    first_503 = next((t for t, s in timeline if s == 503), None)
    back = next(
        (
            t
            for t, s in timeline
            if s == 200 and first_503 is not None and t > first_503
        ),
        None,
    )
    events_after = launcher.all("child")[events_before:]
    m.record(
        "a server process killed",
        f"/health 503 from {seconds(first_503)}, 200 again at {seconds(back)}; the "
        "launcher did "
        f"{[e for _, e in events_after] or 'nothing'}; postmaster unchanged: "
        f"{m.state().get('postmaster_pid') == postmaster}",
    )
    m.stop_by(launcher, "stdin")


def check_job(m: Measurement) -> None:
    """Section 7.1: a long job, stopped in the middle by `stop`, by Ctrl+C and
    by the end of stdin; each next start finds it handed back -- pending, the
    checkpoint, the same `attempts` -- and the last one finishes it."""
    # Twice the corpus: three stops of at least a batch each, and work left.
    folder = m.corpus("long-job", copies=2)
    job_id: str | None = None
    previous_how = ""
    for run, how in enumerate(("stop", "ctrl-c", "stdin")):
        launcher = m.launch(f"job-{how}", console=how == "ctrl-c")
        _, ready = launcher.wait_for("ready")
        url = ready["url"]
        if job_id is None:
            job_id = m.queue(url, folder)
        else:
            m.record(
                f"job at the start after `{previous_how}`",
                m.job_line(m.job(url, job_id)),
            )
        start_job = m.job(url, job_id)
        start_count = start_job["processed_images"]

        def advanced() -> bool:
            job = m.job(url, job_id)
            return (
                job["status"] == "running"
                and job["processed_images"] >= start_count + 8
            )

        _, waited = wait_until(advanced, JOB_SECONDS, 0.25)
        if waited is None:
            raise CheckFailedError(
                f"the job did not advance: {m.job_line(m.job(url, job_id))}"
            )
        before_stop = m.job(url, job_id)
        pids = m.recorded(launcher)
        code, elapsed = m.stop_by(launcher, how)
        left = m.leftovers(pids, m.staged / "runtime" / "postgres")
        m.record(
            f"job stopped by {how}",
            f"exit {code} in {seconds(elapsed)}; {stop_detail(launcher)}; job before "
            "the stop: "
            f"{m.job_line(before_stop)}; left behind: {left or 'nothing'}",
        )
        previous_how = how

    assert job_id is not None
    last = m.launch("job-resume")
    _, ready = last.wait_for("ready")
    url = ready["url"]
    m.record(f"job at the start after `{previous_how}`", m.job_line(m.job(url, job_id)))
    finished, waited = wait_until(
        lambda: (j := m.job(url, job_id))["status"]
        in ("completed", "failed", "cancelled")
        and j,
        JOB_SECONDS,
        0.5,
    )
    if not finished:
        raise CheckFailedError("the job did not finish")
    status, devices, _ = http(url, path="/api/v1/devices")
    indexed = sum(
        d["indexed_images"] for d in devices["devices"] if d["id"] == m.device_id
    )
    m.record(
        "job resumed to the end",
        f"{m.job_line(finished)}; images indexed on the device: {indexed} (the "
        f"scope holds {2 * DEMO_COUNT})",
    )
    m.stop_by(last, "stdin")


def check_search(m: Measurement) -> None:
    """Section 5.3: the search after `ready`, with the collection indexed -- the
    corpus of the check `job` -- and the cache warm: from `ready` to `search:
    ready`, and a Portuguese search sent at `ready`, which has to translate,
    and so waits for both models."""
    launcher = m.launch("search")
    _, ready = launcher.wait_for("ready")
    search_after_ready(m, launcher, ready, "collection indexed")
    m.stop_by(launcher, "stdin")


def check_launcher_killed(m: Measurement) -> None:
    """Section 7.4: the launcher killed with a job in hand. The children stop by
    themselves, the job goes back to the queue, the cluster stays; the next
    launcher adopts it, inherits it, and stops it at the end."""
    folder = m.corpus("launcher-killed")
    launcher = m.launch("launcher-killed")
    _, ready = launcher.wait_for("ready")
    url = ready["url"]
    job_id = m.queue(url, folder)
    wait_until(lambda: m.job(url, job_id)["processed_images"] >= 8, JOB_SECONDS, 0.25)
    before = m.job(url, job_id)
    state = m.state()
    children = [int(pid) for pid in state["children"].values()]
    children += [row.pid for row in m.table.snapshot() if row.parent_pid in children]
    postmaster = int(state["postmaster_pid"])
    m.table.terminate(launcher.pid)
    _, gone = wait_until(
        lambda: not any(m.table.is_alive(pid) for pid in children), 60, 0.02
    )
    cluster_alive = m.table.is_alive(postmaster)
    m.record(
        "launcher killed",
        f"children gone {seconds(gone)} after the kill; cluster still up: "
        f"{cluster_alive}; job before: {m.job_line(before)}",
    )
    nxt = m.launch("launcher-killed-next")
    _, ready = nxt.wait_for("ready")
    url = ready["url"]
    state = m.state()
    m.record(
        "the next launcher",
        f"cluster {state.get('cluster')} (postmaster {state.get('postmaster_pid')}, "
        f"was {postmaster}); job at its start: {m.job_line(m.job(url, job_id))}",
    )
    finished, _ = wait_until(
        lambda: (j := m.job(url, job_id))["status"] == "completed" and j,
        JOB_SECONDS,
        0.5,
    )
    pids = m.recorded(nxt)
    code, elapsed = m.stop_by(nxt, "stdin")
    left = m.leftovers(pids, m.staged / "runtime" / "postgres")
    m.record(
        "the next launcher's stop",
        f"exit {code}; job {m.job_line(finished) if finished else 'not finished'}; "
        f"{stop_detail(nxt)}; "
        f"postmaster {postmaster} alive: {m.table.is_alive(postmaster)}; left: "
        f"{left or 'nothing'}",
    )


def check_job_runner_killed(m: Measurement) -> None:
    """Section 6.3: the job runner killed with a job. A new executor at once; the
    job back to `running` only after the reaper (120 s), one attempt the poorer."""
    folder = m.corpus("job-runner-killed")
    launcher = m.launch("job-runner-killed")
    _, ready = launcher.wait_for("ready")
    url = ready["url"]
    job_id = m.queue(url, folder)
    wait_until(lambda: m.job(url, job_id)["processed_images"] >= 8, JOB_SECONDS, 0.25)
    runner = int(m.state()["children"]["job_runner"])
    offset = len(launcher.stderr_text())
    began = time.perf_counter()
    m.table.terminate(runner)

    def a_new_executor_loops() -> bool:
        # The child's own lines, echoed with its name at the start; the
        # launcher's log repeats the dead one's last lines after a timestamp.
        after = launcher.stderr_text()[offset:].splitlines()
        return any(
            line.startswith("job_runner | ") and "Job executor started" in line
            for line in after
        )

    _, looping = wait_until(a_new_executor_loops, 300, 0.2)
    back, _ = wait_until(
        lambda: (j := m.job(url, job_id))["status"] == "running"
        and j["attempts"] >= 1
        and j,
        600,
        0.5,
    )
    again = time.perf_counter() - began
    m.record(
        "job runner killed with a job",
        f"new executor in its loop after {seconds(looping)}; job running again after "
        f"{seconds(again)}: "
        f"{m.job_line(back) if back else 'never'}",
    )
    wait_until(lambda: m.job(url, job_id)["status"] == "completed", JOB_SECONDS, 0.5)
    m.stop_by(launcher, "stdin")


def check_refusals(m: Measurement) -> None:
    """Section 6.1: 78 is not restarted. The models missing: the job runner
    refuses, and the API starts with `search: unavailable`. An invalid
    `solidvision.env`: the launcher refuses with the line. `API_PORT` taken:
    the launcher refuses with the line."""
    nowhere = m.work / "no models here"
    nowhere.mkdir(exist_ok=True)
    m.user_file(f"MODEL_RUNTIME_DIRECTORY={nowhere}\n")
    try:
        launcher = m.launch("refusal-models")
        _, ready = launcher.wait_for("ready")
        url = ready["url"]
        wait_until(
            lambda: (http(url)[1] or {}).get("search") == "unavailable", 300, 0.5
        )
        _, health, _ = http(url)
        health = health or {}
        time.sleep(5)
        runners = [e for _, e in launcher.all("child") if e.get("name") == "job_runner"]
        m.record(
            "models missing",
            f"ready {ready}; job runner events {runners}; /health search "
            f"{health.get('search')}: {health.get('search_detail')}",
        )
        m.stop_by(launcher, "stdin")
    finally:
        m.user_file(None)

    m.user_file("BATCH_SIZE=auto\n")
    try:
        launcher = m.launch("refusal-configuration")
        code = launcher.wait(120)
        m.record(
            "invalid solidvision.env",
            f"exit {code}; events {[e for _, e in launcher.events]}",
        )
    finally:
        m.user_file(None)

    with socket.socket() as holder:
        holder.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        holder.bind(("127.0.0.1", 0))
        holder.listen()
        port = holder.getsockname()[1]
        m.user_file(f"API_PORT={port}\n")
        try:
            launcher = m.launch("refusal-api-port")
            code = launcher.wait(300)
            said = [f"{e['event']}: {e.get('detail', '')}" for _, e in launcher.events]
            m.record("API_PORT taken", f"exit {code}; events {said}")
        finally:
            m.user_file(None)


def check_loop(m: Measurement) -> None:
    """Section 6.1: a child that dies in a loop. `DEVICE=nonsense` makes the job
    runner raise as it builds its model, after importing `torch`, every time;
    the API stays up (its warm-up fails the same way, and search is on demand)."""
    m.user_file("DEVICE=nonsense\n")
    try:
        launcher = m.launch("loop")
        launcher.wait_for("ready")
        _, done = wait_until(
            lambda: any(
                e.get("name") == "job_runner" and e.get("state") == "unavailable"
                for _, e in launcher.all("child")
            ),
            900,
            0.5,
        )
        runners = [
            (round(t, 1), e["state"], e.get("delay"))
            for t, e in launcher.all("child")
            if e.get("name") == "job_runner"
        ]
        ready = launcher.first("ready")
        status = http(ready[1]["url"])[0] if ready else 0
        m.record(
            "a job runner that dies at every start",
            f"given up after {seconds(done)}; events (t, state, delay) {runners}; API "
            f"/health {status}",
        )
        m.stop_by(launcher, "stdin")
    finally:
        m.user_file(None)


def check_ports(m: Measurement) -> None:
    """Section 5.4: the last port, taken by another socket, gives way to another;
    a restarted API whose port was taken comes back elsewhere with `address`."""
    last = m.state().get("last_ready_port")
    with socket.socket() as holder:
        holder.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        holder.bind(("127.0.0.1", int(last)))
        holder.listen()
        launcher = m.launch("ports-last-taken")
        _, ready = launcher.wait_for("ready")
        m.record("last port taken at the start", f"last {last}, now {ready['url']}")
        m.stop_by(launcher, "stdin")

    launcher = m.launch("ports-address")
    _, ready = launcher.wait_for("ready")
    port = int(ready["url"].rsplit(":", 1)[1])
    # The first death restarts at once, and the new API takes its port back
    # before anybody else can. The second waits 2 s (section 6.1): the
    # measurement takes the port in that window.
    m.table.terminate(int(m.state()["children"]["api"]))
    wait_until(
        lambda: any(
            e.get("name") == "api" and e.get("state") == "running"
            for _, e in launcher.all("child")
        ),
        300,
        0.1,
    )
    m.table.terminate(int(m.state()["children"]["api"]))
    wait_until(
        lambda: [e.get("state") for _, e in launcher.all("child")].count("restarting")
        >= 2,
        60,
        0.02,
    )
    taken = False
    holder = socket.socket()
    for exclusive in (True, False):
        holder = socket.socket()
        if exclusive:
            holder.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            holder.bind(("127.0.0.1", port))
            holder.listen()
            taken = True
            break
        except OSError:
            holder.close()
    found, waited = wait_until(lambda: launcher.first("address"), 120, 0.05)
    m.record(
        "API restarted with its port taken",
        f"port {port} taken by the measurement before the restart: {taken}; address "
        f"{found[1] if found else None} after {seconds(waited)}",
    )
    holder.close()
    m.stop_by(launcher, "stdin")


def check_utf8(m: Measurement) -> None:
    """Section 5.5: a log line with `Łódź/東京/✈.jpg` through the pipe. The file is
    not an image, so the job runner logs its path when it fails to index it."""
    folder = m.corpora / "utf8"
    target = folder.joinpath(*UTF8_FOLDER)
    target.mkdir(parents=True, exist_ok=True)
    (target / UTF8_FILE).write_bytes(b"this is not a JPEG")
    launcher = m.launch("utf8")
    _, ready = launcher.wait_for("ready")
    url = ready["url"]
    job_id = m.queue(url, folder)
    finished, _ = wait_until(
        lambda: (j := m.job(url, job_id))["status"] in ("completed", "failed") and j,
        600,
        0.5,
    )
    m.stop_by(launcher, "stdin")
    text = launcher.stderr_text()
    name = "/".join(UTF8_FOLDER) + "/" + UTF8_FILE
    lines = [line for line in text.splitlines() if UTF8_FILE in line]
    m.record(
        "the pipe and the code page",
        f"job {m.job_line(finished) if finished else 'not finished'}; lines naming the "
        f"file: {len(lines)}; "
        f"intact: {any(name in line.replace(chr(92), '/') for line in lines)}; "
        f"replacement characters: {chr(0xFFFD) in text}; "
        f"e.g. {lines[0][:160] if lines else None!r}",
    )


def check_side_by_side(m: Measurement) -> None:
    """Section 8: a checkout's launcher and the installed one, at once."""
    checkout = m.launch("side-checkout", env=m.checkout_env())
    installed = m.launch("side-installed")
    checkout_ready = checkout.wait_for("ready")
    installed_ready = installed.wait_for("ready")
    m.record(
        "checkout and installed side by side",
        f"checkout {checkout_ready[1]['url']} at {seconds(checkout_ready[0])} (with "
        "initdb); "
        f"installed {installed_ready[1]['url']} at {seconds(installed_ready[0])}",
    )
    m.stop_by(installed, "stdin")
    m.stop_by(checkout, "stdin")


CHECKS: dict[str, Callable[[Measurement], None]] = {
    "import": check_import,
    "start": check_start,
    "second": check_second,
    "kills": check_kills,
    "job": check_job,
    "search": check_search,
    "launcher-killed": check_launcher_killed,
    "job-runner-killed": check_job_runner_killed,
    "refusals": check_refusals,
    "loop": check_loop,
    "ports": check_ports,
    "utf8": check_utf8,
    "side": check_side_by_side,
}


def describe_machine() -> None:
    say(f"date: {datetime.datetime.now().astimezone().isoformat(timespec='seconds')}")
    say(
        f"machine: {platform.platform()}, {platform.processor()}, {os.cpu_count()} "
        "logical CPUs"
    )
    say(f"python: {sys.version.split()[0]} ({sys.executable})")
    head = subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()
    say(f"head: {head} (with the working tree of RFC-036)")
    elevated = bool(ctypes.windll.shell32.IsUserAnAdmin())
    say(f"elevated: {elevated}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--only", default="")
    parser.add_argument("--work", type=Path)
    parser.add_argument("--keep", action="store_true")
    arguments = parser.parse_args()
    # The log names Łódź, 東京 and ✈: in UTF-8, wherever it is redirected.
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    if arguments.list:
        for name, check in CHECKS.items():
            print(f"{name:20} {(check.__doc__ or '').splitlines()[0]}")
        return 0
    chosen = [
        name
        for name in CHECKS
        if not arguments.only or name in arguments.only.split(",")
    ]
    work = arguments.work or Path(tempfile.mkdtemp(prefix="rfc036-measure-"))
    work.mkdir(parents=True, exist_ok=True)
    print("# RFC-036: the launcher, measured\n")
    describe_machine()
    say(f"work directory: {work}")
    say(f"checks: {', '.join(chosen)}")
    measurement = Measurement(work)
    failures = 0
    try:
        for name in chosen:
            heading(name)
            say((CHECKS[name].__doc__ or "").strip().splitlines()[0])
            began = time.perf_counter()
            try:
                CHECKS[name](measurement)
            except CheckFailedError as error:
                failures += 1
                measurement.record(name, f"FAILED: {error}")
            except Exception:
                failures += 1
                measurement.record(name, f"ERROR: {traceback.format_exc()}")
            finally:
                for launcher in measurement.launchers:
                    if launcher.process.poll() is None:
                        say(f"stopping {launcher.label}, left running")
                        try:
                            launcher.stop()
                        except CheckFailedError:
                            launcher.process.kill()
                measurement.launchers.clear()
            say(f"({name} took {seconds(time.perf_counter() - began)})")
        heading("results")
        for name, result in measurement.results:
            say(f"{name}: {result}")
    finally:
        if not arguments.keep:
            heading("cleanup")
            if measurement.staged.exists():
                try:
                    staging.remove(measurement.staged)
                    say("staged installation removed")
                except Exception as error:  # noqa: BLE001
                    say(f"staged installation: {error!r}")
            import logging

            logging.shutdown()
            for _ in range(10):
                shutil.rmtree(work, ignore_errors=True)
                if not work.exists():
                    break
                time.sleep(1)
            say(f"work directory removed: {not work.exists()}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
