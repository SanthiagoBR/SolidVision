r"""What a supervisor of the three programs meets on Windows, before RFC-036 decides anything (pilot).

RFC-036 is written before its implementation, so every number of its own is
`TBM`. This is not one of those numbers. It reads how Windows, `pg_ctl`,
CPython 3.12 and uvicorn *already* behave on this machine, so that the
problem statement of the RFC rests on what was observed and not on what the
roadmap presumed:

    jobs      whether a process here runs inside a job object at all; where
              the postmaster the adapter starts ends up, job-wise; what a
              launcher's own job with KILL_ON_JOB_CLOSE does to the cluster
              when the launcher dies, and what happens without one
    stop      how a parent can ask a console-less Python child to stop the
              way Ctrl+C would: a watcher thread that turns stdin EOF into
              `_thread.interrupt_main()`, with the main thread in three
              states; the parent killed instead of closing the pipe; and
              CTRL_BREAK from a parent that does not share the child's console
    uvicorn   the same request inside uvicorn: graceful shutdown with a
              request in flight; a stop during a blocking lifespan startup;
              and whether the port accepts anything before startup ends
    ports     whether the API's habitual port 8000 is usable here

Nothing here touches the user's cluster, models or data. The cluster this
script starts lives in a temporary directory, as do the logs of every
process it starts (`LOCALAPPDATA` and `LOG_DIRECTORY` point there), and the
directory is removed at the end. Only processes this script created are ever
ended, each one by the PID it recorded when creating it.

Run it with the project's virtualenv, from anywhere, with the output
redirected next to this script (`*.log` is outside git, like every
experiment's log):

    python experiments/rfc-036-launcher/pilot_process_control_probe.py > LOG 2>&1

No new dependency: `ctypes` over `kernel32`, and the adapter of RFC-033.
"""

from __future__ import annotations

import ctypes
import datetime
import http.client
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from ctypes import wintypes
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPOSITORY = HERE.parents[1]
BACKEND = REPOSITORY / "backend"
RUNTIME = REPOSITORY / "runtime" / "postgres"
PYTHON = Path(sys.executable)
sys.path.insert(0, str(BACKEND))

CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200
CTRL_BREAK_EVENT = 1
PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
STILL_ACTIVE = 259
DEADLINE = 120.0
"""Every wait in this script has a deadline (RFC-034 section 2.1)."""

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
kernel32.IsProcessInJob.argtypes = (
    wintypes.HANDLE,
    wintypes.HANDLE,
    ctypes.POINTER(wintypes.BOOL),
)
kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
kernel32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
kernel32.GenerateConsoleCtrlEvent.argtypes = (wintypes.DWORD, wintypes.DWORD)


def say(text: str = "") -> None:
    print(text, flush=True)


def section(title: str) -> None:
    say()
    say(f"## {title}")
    say()


def in_any_job(pid: int | None = None) -> bool | None:
    handle = (
        wintypes.HANDLE(-1)
        if pid is None
        else kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    )
    if not handle:
        return None
    result = wintypes.BOOL()
    try:
        if not kernel32.IsProcessInJob(handle, None, ctypes.byref(result)):
            return None
        return bool(result.value)
    finally:
        if pid is not None:
            kernel32.CloseHandle(handle)


def is_alive(pid: int) -> bool:
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    code = wintypes.DWORD()
    try:
        kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def terminate(pid: int) -> bool:
    handle = kernel32.OpenProcess(PROCESS_TERMINATE | SYNCHRONIZE, False, pid)
    if not handle:
        return False
    try:
        return bool(kernel32.TerminateProcess(handle, 1))
    finally:
        kernel32.CloseHandle(handle)


def wait_for(predicate, deadline: float = DEADLINE, step: float = 0.02) -> float | None:
    """Seconds until `predicate()` held, or `None` at the deadline."""
    started = time.perf_counter()
    while time.perf_counter() - started < deadline:
        if predicate():
            return time.perf_counter() - started
        time.sleep(step)
    return None


def child_environment(work: Path) -> dict[str, str]:
    """The children log and keep data under `work`, never under the real profile."""
    environment = os.environ.copy()
    environment["LOCALAPPDATA"] = str(work / "localappdata")
    environment["LOG_DIRECTORY"] = str(work / "logs")
    environment["PYTHONPATH"] = str(BACKEND)
    return environment


# ---------------------------------------------------------------------------
# jobs


LAUNCHER_SIM = r'''
"""A stand-in for a launcher: optionally its own job, then the adapter's start()."""
import ctypes, json, sys, time
from ctypes import wintypes
from pathlib import Path

work, use_job, runtime = Path(sys.argv[1]), sys.argv[2] == "1", Path(sys.argv[3])
state_file = work / f"launcher-sim-{sys.argv[2]}.json"

k = ctypes.WinDLL("kernel32", use_last_error=True)
k.CreateJobObjectW.restype = wintypes.HANDLE
k.OpenProcess.restype = wintypes.HANDLE
k.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
k.IsProcessInJob.argtypes = (wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL))
k.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
k.QueryInformationJobObject.argtypes = (
    wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p)


class BASIC(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD)]


class IO(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in
                ("ReadOps", "WriteOps", "OtherOps", "ReadBytes", "WriteBytes", "OtherBytes")]


class EXTENDED(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


def in_job(pid, job=None):
    handle = k.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    result = wintypes.BOOL()
    ok = k.IsProcessInJob(handle, job, ctypes.byref(result))
    k.CloseHandle(handle)
    return bool(result.value) if ok else None


state = {"pid": __import__("os").getpid()}
self_handle = wintypes.HANDLE(-1)
result = wintypes.BOOL()
k.IsProcessInJob(self_handle, None, ctypes.byref(result))
state["self_in_any_job_before"] = bool(result.value)

job = None
if use_job:
    job = k.CreateJobObjectW(None, None)
    info = EXTENDED()
    info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    state["job_limits_set"] = bool(k.SetInformationJobObject(
        job, 9, ctypes.byref(info), ctypes.sizeof(info)))
    state["self_assigned"] = bool(k.AssignProcessToJobObject(job, self_handle))
    state["assign_error"] = ctypes.get_last_error()

from app.infrastructure.database.embedded.cluster import EmbeddedPostgres
from app.infrastructure.database.embedded.cluster_home import ClusterHome
from app.infrastructure.database.embedded.runtime import PostgresRuntime
from app.infrastructure.database.embedded.windows_processes import WindowsProcesses

cluster = EmbeddedPostgres(runtime=PostgresRuntime(runtime), home=ClusterHome(work / "postgres"))
started_at = time.perf_counter()
started = cluster.start()
state["start_seconds"] = round(time.perf_counter() - started_at, 3)
state["started_here"] = started.started_here
state["created"] = started.created
state["recovered"] = started.recovered
report = cluster.status()
postmaster = report.pid
state["postmaster"] = postmaster
table = WindowsProcesses()
rows = table.snapshot()
by_pid = {row.pid: row for row in rows}
state["postmaster_parent"] = by_pid[postmaster].parent_pid if postmaster in by_pid else None
parent = state["postmaster_parent"]
state["postmaster_parent_name"] = by_pid[parent].name if parent in by_pid else None
state["server_children"] = sorted(row.pid for row in rows if row.parent_pid == postmaster)
state["postmaster_in_any_job"] = in_job(postmaster)
if job:
    state["postmaster_in_launcher_job"] = in_job(postmaster, job)
    state["children_in_launcher_job"] = [in_job(pid, job) for pid in state["server_children"]]
    buffer = (ctypes.c_size_t * 130)()
    if k.QueryInformationJobObject(job, 3, buffer, ctypes.sizeof(buffer), None):
        count = ctypes.cast(buffer, ctypes.POINTER(wintypes.DWORD))[1]
        pids = [buffer[1 + index] for index in range(count)]
        state["job_members"] = sorted(f"{pid}:{by_pid[pid].name if pid in by_pid else '?'}" for pid in pids)
state_file.write_text(json.dumps(state))
while True:
    time.sleep(1)
'''


def run_launcher_sim(work: Path, use_job: bool) -> tuple[subprocess.Popen[bytes], dict]:
    script = work / "launcher_sim.py"
    script.write_text(LAUNCHER_SIM, encoding="utf-8")
    state_file = work / f"launcher-sim-{int(use_job)}.json"
    state_file.unlink(missing_ok=True)
    with (work / f"launcher-sim-{int(use_job)}.out").open("wb") as output:
        process = subprocess.Popen(
            [
                str(PYTHON),
                str(script),
                str(work),
                "1" if use_job else "0",
                str(RUNTIME),
            ],
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            env=child_environment(work),
            creationflags=CREATE_NO_WINDOW,
        )
    waited = wait_for(
        lambda: state_file.exists() or process.poll() is not None, 600.0, 0.1
    )
    if not state_file.exists():
        tail = (work / f"launcher-sim-{int(use_job)}.out").read_text(errors="replace")[
            -2000:
        ]
        raise RuntimeError(f"launcher-sim did not report (waited {waited}):\n{tail}")
    return process, json.loads(state_file.read_text())


def server_alive(state: dict) -> list[int]:
    pids = [state["postmaster"], *state["server_children"]]
    return [pid for pid in pids if is_alive(pid)]


def probe_jobs(work: Path) -> None:
    section("jobs")
    from app.infrastructure.database.embedded.cluster import EmbeddedPostgres
    from app.infrastructure.database.embedded.cluster_home import ClusterHome
    from app.infrastructure.database.embedded.runtime import PostgresRuntime

    cluster = EmbeddedPostgres(
        runtime=PostgresRuntime(RUNTIME), home=ClusterHome(work / "postgres")
    )
    say(f"this script in a job object: {in_any_job()}")
    say(
        "A process that is already in a job cannot be taken out of it, and on "
        "Windows 8 and later a new job nests inside the old one."
    )

    for use_job in (False, True):
        label = (
            "with its own KILL_ON_JOB_CLOSE job"
            if use_job
            else "without a job of its own"
        )
        say()
        say(f"### launcher-sim {label}")
        process, state = run_launcher_sim(work, use_job)
        for key, value in state.items():
            say(f"  {key}: {value}")
        say(f"  server processes alive before the kill: {len(server_alive(state))}")
        killed_at = time.perf_counter()
        terminate(state["pid"])
        process.wait(timeout=30)
        gone = wait_for(lambda: not server_alive(state), 10.0)
        if gone is None:
            say(
                f"  launcher-sim killed; 10 s later {len(server_alive(state))} server "
                "processes are still alive"
            )
        else:
            say(
                f"  launcher-sim killed; every server process gone "
                f"{time.perf_counter() - killed_at:.3f} s after TerminateProcess"
            )
        started_at = time.perf_counter()
        again = cluster.start()
        say(
            f"  next start() from this script: {time.perf_counter() - started_at:.3f} s, "
            f"started_here={again.started_here}, recovered={again.recovered}"
        )
        outcome = cluster.stop()
        say(f"  stop(): {outcome.value}")
        say(f"  server processes alive after stop(): {len(server_alive(state))}")


# ---------------------------------------------------------------------------
# stop


CHILD = r'''
"""A child that stops as Ctrl+C would when its stdin reaches end of file."""
import _thread, os, signal, sys, threading, time
from pathlib import Path

marker, mode = Path(sys.argv[1]), sys.argv[2]
how = sys.argv[3] if len(sys.argv) > 3 else "interrupt_main"


_lock = threading.Lock()


def note(text):
    stamp = time.time()
    with _lock, marker.open("a", encoding="utf-8") as stream:
        stream.write(f"{stamp:.6f} {text}\n")


def watch_stdin():
    # The stop is requested first and recorded after: every step that
    # needs the GIL is one more wait for a main thread that holds it.
    try:
        while sys.stdin.buffer.read(1):
            pass
    except OSError:
        pass
    if how == "raise_signal":
        signal.raise_signal(signal.SIGINT)
    else:
        _thread.interrupt_main()
    note(f"stdin closed, {how} called")


if mode == "break":
    signal.signal(signal.SIGBREAK, signal.default_int_handler)
else:
    threading.Thread(target=watch_stdin, daemon=True).start()
note(f"started pid={os.getpid()}")
try:
    if mode == "c-call":
        while True:
            sum(range(60_000_000))
    elif mode == "sleep":
        time.sleep(60)
        note("sleep returned")
    else:
        while True:
            time.sleep(0.2)
except KeyboardInterrupt:
    note("KeyboardInterrupt handled")
finally:
    note("finally")
'''

PARENT_SIM = r'''
"""A parent that starts the child with a stdin pipe and then waits to be killed."""
import json, subprocess, sys, time
from pathlib import Path
child, marker, out = sys.argv[1], sys.argv[2], Path(sys.argv[3])
process = subprocess.Popen([sys.executable, child, marker, "loop", "raise_signal"], stdin=subprocess.PIPE,
                           creationflags=0x08000000)
out.write_text(json.dumps({"child": process.pid}))
while True:
    time.sleep(1)
'''


def marker_lines(marker: Path) -> list[tuple[float, str]]:
    if not marker.exists():
        return []
    lines = []
    for line in marker.read_text(encoding="utf-8").splitlines():
        stamp, _, text = line.partition(" ")
        try:
            lines.append((float(stamp), text))
        except ValueError:
            lines.append((float("nan"), f"(unparsable) {line}"))
    return lines


def wait_marker(marker: Path, text: str, deadline: float = 30.0) -> bool:
    return (
        wait_for(
            lambda: any(
                t == text or t.startswith(text) for _, t in marker_lines(marker)
            ),
            deadline,
        )
        is not None
    )


def probe_stop(work: Path) -> None:
    section("stop")
    child = work / "child.py"
    child.write_text(CHILD, encoding="utf-8")
    say(
        "Each child is created as the launcher would create it in an installed app: "
        "CREATE_NO_WINDOW, so it has a console of its own that nobody sees."
    )

    calibration_started = time.perf_counter()
    sum(range(60_000_000))
    say(
        f"one `sum(range(60_000_000))` in this process: {time.perf_counter() - calibration_started:.2f} s"
    )

    cases = [
        (mode, how, f"{description}; the watcher calls {how}")
        for how in ("interrupt_main", "raise_signal")
        for mode, description in (
            ("loop", "main thread in a loop of time.sleep(0.2)"),
            ("c-call", "main thread inside long C calls (sum over a range)"),
            ("sleep", "main thread in one time.sleep(60)"),
        )
    ]
    for mode, how, description in cases:
        marker = work / f"marker-{mode}-{how}.txt"
        process = subprocess.Popen(
            [str(PYTHON), str(child), str(marker), mode, how],
            stdin=subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW,
        )
        wait_marker(marker, "started")
        time.sleep(1.0)
        closed_at = time.time()
        assert process.stdin is not None
        process.stdin.close()
        try:
            code = process.wait(timeout=90)
        except subprocess.TimeoutExpired:
            terminate(process.pid)
            code = None
        exited_at = time.time()
        lines = marker_lines(marker)
        handled = [
            stamp for stamp, text in lines if text == "KeyboardInterrupt handled"
        ]
        say(f"### {description}")
        say(
            f"  stdin closed -> process exited: {exited_at - closed_at:.3f} s, exit code {code}"
        )
        if handled:
            say(
                f"  stdin closed -> except KeyboardInterrupt ran: {handled[0] - closed_at:.3f} s"
            )
        say(f"  child's record: {[text for _, text in lines]}")

    say("### the parent killed instead of closing the pipe")
    parent = work / "parent_sim.py"
    parent.write_text(PARENT_SIM, encoding="utf-8")
    marker = work / "marker-parent-killed.txt"
    out = work / "parent-sim.json"
    process = subprocess.Popen(
        [str(PYTHON), str(parent), str(child), str(marker), str(out)],
        stdin=subprocess.DEVNULL,
        creationflags=CREATE_NO_WINDOW,
    )
    wait_for(out.exists, 30.0)
    child_pid = json.loads(out.read_text())["child"]
    wait_marker(marker, "started")
    time.sleep(1.0)
    killed_at = time.time()
    terminate(process.pid)
    process.wait(timeout=30)
    gone = wait_for(lambda: not is_alive(child_pid), 30.0)
    lines = marker_lines(marker)
    handled = [stamp for stamp, text in lines if text == "KeyboardInterrupt handled"]
    say(
        f"  parent TerminateProcess -> child exited: {gone if gone is None else round(time.time() - killed_at, 3)} s"
    )
    if handled:
        say(
            f"  parent TerminateProcess -> child's except KeyboardInterrupt ran: {handled[0] - killed_at:.3f} s"
        )
    say(f"  child's record: {[text for _, text in lines]}")

    say("### CTRL_BREAK from a parent that does not share the child's console")
    marker = work / "marker-break.txt"
    process = subprocess.Popen(
        [str(PYTHON), str(child), str(marker), "break"],
        stdin=subprocess.DEVNULL,
        creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP,
    )
    wait_marker(marker, "started")
    time.sleep(1.0)
    has_console = bool(ctypes.WinDLL("kernel32").GetConsoleWindow())
    sent = kernel32.GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, process.pid)
    error = ctypes.get_last_error()
    handled = wait_marker(marker, "KeyboardInterrupt handled", 5.0)
    say(f"  this script has a console window: {has_console}")
    say(
        f"  GenerateConsoleCtrlEvent returned {sent} (last error {error}); child handled it: {handled}"
    )
    if process.poll() is None:
        terminate(process.pid)
        process.wait(timeout=30)
        say("  child ended by TerminateProcess afterwards")


# ---------------------------------------------------------------------------
# uvicorn


SERVER = r'''
"""uvicorn serving a bare ASGI app, stopping on stdin EOF the way a supervised child would."""
import asyncio, signal, sys, threading, time
from pathlib import Path
import uvicorn

marker, port, startup_seconds = Path(sys.argv[1]), int(sys.argv[2]), float(sys.argv[3])


_lock = threading.Lock()


def note(text):
    stamp = time.time()
    with _lock, marker.open("a", encoding="utf-8") as stream:
        stream.write(f"{stamp:.6f} {text}\n")


def watch_stdin():
    while sys.stdin.buffer.read(1):
        pass
    signal.raise_signal(signal.SIGINT)
    note("stdin closed")


async def app(scope, receive, send):
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                note("lifespan startup begins")
                time.sleep(startup_seconds)  # a synchronous warm-up, as the API's is
                note("lifespan startup ends")
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                note("lifespan shutdown")
                await send({"type": "lifespan.shutdown.complete"})
                return
    if scope["path"] == "/slow":
        await asyncio.sleep(2.0)
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
    await send({"type": "http.response.body", "body": b"ok"})


threading.Thread(target=watch_stdin, daemon=True).start()
note("process started")
try:
    uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="info")).run()
    note("Server.run returned")
except KeyboardInterrupt:
    note("KeyboardInterrupt after Server.run")
finally:
    note("finally")
'''


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def accepts(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


def probe_uvicorn(work: Path) -> None:
    section("uvicorn")
    import uvicorn

    say(f"uvicorn {uvicorn.__version__}")
    server = work / "server.py"
    server.write_text(SERVER, encoding="utf-8")

    say("### stop with a request in flight (startup 0 s)")
    marker = work / "marker-uvicorn.txt"
    port = free_port()
    with (work / "uvicorn-1.out").open("wb") as output:
        process = subprocess.Popen(
            [str(PYTHON), str(server), str(marker), str(port), "0"],
            stdin=subprocess.PIPE,
            stdout=output,
            stderr=subprocess.STDOUT,
            creationflags=CREATE_NO_WINDOW,
        )
    listening = wait_for(lambda: accepts(port), 60.0)
    say(f"  process created -> port accepts: {listening:.3f} s")
    answer: dict[str, object] = {}

    def slow_request() -> None:
        started = time.time()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            connection.request("GET", "/slow")
            response = connection.getresponse()
            answer["status"] = response.status
        except (
            Exception
        ) as error:  # noqa: BLE001 -- the pilot reports whatever happened
            answer["error"] = repr(error)
        answer["seconds"] = round(time.time() - started, 3)

    requester = threading.Thread(target=slow_request)
    requester.start()
    time.sleep(0.5)
    closed_at = time.time()
    assert process.stdin is not None
    process.stdin.close()
    code = process.wait(timeout=60)
    exited_at = time.time()
    requester.join(timeout=15)
    refused_after = not accepts(port)
    say(
        f"  stdin closed -> process exited: {exited_at - closed_at:.3f} s, exit code {code}"
    )
    say(f"  the /slow request sent 0.5 s before the stop: {answer}")
    say(f"  port refuses after exit: {refused_after}")
    say(f"  child's record: {[text for _, text in marker_lines(marker)]}")
    output_text = (
        (work / "uvicorn-1.out").read_text(errors="replace").strip().splitlines()
    )
    say("  uvicorn's output:")
    for line in output_text[-12:]:
        say(f"    {line}")

    say("### stop during a blocking lifespan startup of 4 s")
    marker = work / "marker-uvicorn-startup.txt"
    port = free_port()
    with (work / "uvicorn-2.out").open("wb") as output:
        process = subprocess.Popen(
            [str(PYTHON), str(server), str(marker), str(port), "4"],
            stdin=subprocess.PIPE,
            stdout=output,
            stderr=subprocess.STDOUT,
            creationflags=CREATE_NO_WINDOW,
        )
    wait_marker(marker, "lifespan startup begins", 60.0)
    refused_during_startup = not accepts(port)
    time.sleep(1.0)
    closed_at = time.time()
    assert process.stdin is not None
    process.stdin.close()
    accepted_at_some_point = False
    while process.poll() is None and time.time() - closed_at < 60:
        accepted_at_some_point = accepted_at_some_point or accepts(port)
        time.sleep(0.05)
    code = process.wait(timeout=60)
    exited_at = time.time()
    say(f"  port refuses during startup: {refused_during_startup}")
    say(
        f"  stdin closed 1 s into startup -> process exited: {exited_at - closed_at:.3f} s, exit code {code}"
    )
    say(
        f"  port accepted a connection between the stop and the exit: {accepted_at_some_point}"
    )
    say(f"  child's record: {[text for _, text in marker_lines(marker)]}")
    output_text = (
        (work / "uvicorn-2.out").read_text(errors="replace").strip().splitlines()
    )
    say("  uvicorn's output:")
    for line in output_text[-12:]:
        say(f"    {line}")


# ---------------------------------------------------------------------------
# ports


def probe_ports() -> None:
    section("ports")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            probe.bind(("127.0.0.1", 8000))
            say("127.0.0.1:8000 can be bound exclusively right now")
        except OSError as error:
            say(f"127.0.0.1:8000 cannot be bound: {error!r}")
    ranges = subprocess.run(
        ["netsh", "interface", "ipv4", "show", "excludedportrange", "protocol=tcp"],
        capture_output=True,
        timeout=30,
    )
    text = ranges.stdout.decode("cp850", errors="replace")
    say("excluded port ranges (netsh):")
    for line in text.splitlines():
        if line.strip():
            say(f"  {line.rstrip()}")


def main() -> None:
    say("# RFC-036 pilot: process control on Windows, before the launcher exists")
    say(f"date: {datetime.datetime.now().astimezone().isoformat(timespec='seconds')}")
    say(f"machine: {platform.platform()}, {platform.processor()}")
    say(f"python: {sys.version.split()[0]} ({PYTHON})")
    head = subprocess.run(
        ["git", "-C", str(REPOSITORY), "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()
    say(f"head: {head}")
    work = Path(tempfile.mkdtemp(prefix="rfc036-pilot-"))
    os.environ["LOCALAPPDATA"] = str(work / "localappdata")
    os.environ["LOG_DIRECTORY"] = str(work / "logs")
    say(f"work directory: {work}")
    try:
        probe_ports()
        probe_stop(work)
        probe_uvicorn(work)
        probe_jobs(work)
    finally:
        section("cleanup")
        try:
            from app.infrastructure.database.embedded.cluster import EmbeddedPostgres
            from app.infrastructure.database.embedded.cluster_home import ClusterHome
            from app.infrastructure.database.embedded.runtime import PostgresRuntime

            outcome = EmbeddedPostgres(
                runtime=PostgresRuntime(RUNTIME), home=ClusterHome(work / "postgres")
            ).stop()
            say(f"temporary cluster: {outcome.value}")
        except Exception as error:  # noqa: BLE001
            say(f"temporary cluster: {error!r}")
        # This process's own log file is under `work` (LOG_DIRECTORY), and
        # Windows will not delete a file that is still open.
        import logging

        logging.shutdown()
        removed = False
        for _ in range(20):
            shutil.rmtree(work, ignore_errors=True)
            if not work.exists():
                removed = True
                break
            time.sleep(0.5)
        say(f"work directory removed: {removed}")


if __name__ == "__main__":
    main()
