"""The child's side of the contract, in real processes (RFC-036 sections 6.1, 7.1).

The pilot measured why this path and not another (section 2.2): from a thread,
`signal.raise_signal(SIGINT)` wakes a `time.sleep(60)` at once, where
`_thread.interrupt_main()` waits for the whole sleep. These tests hold the
implementation to that, in a process created the way the launcher creates its
children.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows processes")

BACKEND = Path(__file__).resolve().parents[3]
CREATE_NO_WINDOW = 0x08000000
CHILD_SECONDS = 60

ASLEEP = """
import sys, time
from pathlib import Path
from app.infrastructure.process.supervision import stop_on_stdin_eof
if "--supervised" in sys.argv:
    stop_on_stdin_eof()
print("asleep", flush=True)
if len(sys.argv) > 2:
    Path(sys.argv[2]).write_text("asleep")
try:
    time.sleep(60)
    print("woke up by itself", flush=True)
except KeyboardInterrupt:
    print("interrupted", flush=True)
"""


def start(source: str, *arguments: str) -> subprocess.Popen[str]:
    process = subprocess.Popen(
        [sys.executable, "-X", "utf8", "-c", source, *arguments],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, "PYTHONPATH": str(BACKEND)},
        creationflags=CREATE_NO_WINDOW,
        text=True,
    )
    assert process.stdout is not None
    assert process.stdout.readline().strip() == "asleep"
    return process


def test_the_end_of_stdin_interrupts_a_long_sleep_at_once() -> None:
    process = start(ASLEEP, "--supervised")
    assert process.stdin is not None

    closed_at = time.perf_counter()
    process.stdin.close()
    code = process.wait(CHILD_SECONDS)
    elapsed = time.perf_counter() - closed_at

    assert process.stdout is not None
    assert process.stdout.read().strip() == "interrupted"
    assert code == 0
    assert elapsed < 1.0


def test_the_death_of_the_parent_is_a_request_too(tmp_path: Path) -> None:
    """Section 7.4: Windows closes the dead parent's end of the pipe. Here the
    parent is a process of its own, ended by `TerminateProcess`."""
    marker = tmp_path / "asleep"
    parent_source = f"""
import subprocess, sys, time
from pathlib import Path
child = subprocess.Popen(
    [sys.executable, "-X", "utf8", "-c", {ASLEEP!r}, "--supervised", {str(marker)!r}],
    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
    creationflags={CREATE_NO_WINDOW},
)
deadline = time.monotonic() + 60
while not Path({str(marker)!r}).exists() and time.monotonic() < deadline:
    time.sleep(0.05)
print(child.pid, flush=True)
time.sleep(120)
"""
    parent = subprocess.Popen(
        [sys.executable, "-c", parent_source],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        env={**os.environ, "PYTHONPATH": str(BACKEND)},
        creationflags=CREATE_NO_WINDOW,
        text=True,
    )
    assert parent.stdout is not None
    child_pid = int(parent.stdout.readline())
    assert marker.exists()

    parent.kill()
    parent.wait(CHILD_SECONDS)

    assert _wait_for_exit(child_pid, CHILD_SECONDS) == 0


def test_without_the_argument_the_end_of_stdin_changes_nothing() -> None:
    """A developer running a program by hand does not want it to stop when
    the terminal's stdin ends (section 7.1)."""
    process = start(ASLEEP)
    assert process.stdin is not None

    process.stdin.close()
    time.sleep(1.5)

    try:
        assert process.poll() is None
    finally:
        process.kill()
        process.wait(CHILD_SECONDS)


def test_a_program_that_ends_on_its_own_keeps_its_exit_code() -> None:
    """The watcher is still waiting on stdin when the program ends by itself --
    uvicorn exiting with 3 over a taken port. Read through `sys.stdin.buffer`,
    the reader's lock stopped the interpreter's shutdown: a fatal error and
    `0xC0000005` where the launcher waits for 3 (found while implementing)."""
    process = subprocess.Popen(
        [
            sys.executable,
            "-X",
            "utf8",
            "-c",
            "import sys, time\n"
            "from app.infrastructure.process.supervision import stop_on_stdin_eof\n"
            "stop_on_stdin_eof()\n"
            "time.sleep(0.5)\n"
            "sys.exit(3)\n",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env={**os.environ, "PYTHONPATH": str(BACKEND)},
        creationflags=CREATE_NO_WINDOW,
    )
    try:
        code = process.wait(CHILD_SECONDS)
        assert process.stderr is not None
        error = process.stderr.read().decode("utf-8", "replace")
    finally:
        assert process.stdin is not None
        process.stdin.close()

    assert code == 3, error
    assert "Fatal Python error" not in error


def test_a_program_waiting_for_the_end_of_stdin_can_still_load_libraries() -> None:
    """The job runner imports `torch` after it starts listening, and so does the
    API's warm-up. With a read left pending on the stdin pipe, Windows held the
    load of numpy's DLL until stdin closed: the job runner never reached its
    loop under the launcher (found while implementing). Here the import has to
    finish while stdin is still open."""
    process = subprocess.Popen(
        [
            sys.executable,
            "-X",
            "utf8",
            "-c",
            "import time\n"
            "from app.infrastructure.process.supervision import stop_on_stdin_eof\n"
            "stop_on_stdin_eof()\n"
            "time.sleep(0.5)\n"
            "import numpy\n"
            "print('imported', flush=True)\n"
            "try:\n"
            "    time.sleep(60)\n"
            "except KeyboardInterrupt:\n"
            "    print('interrupted', flush=True)\n",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env={**os.environ, "PYTHONPATH": str(BACKEND)},
        creationflags=CREATE_NO_WINDOW,
        text=True,
    )
    assert process.stdout is not None
    assert process.stdin is not None
    stdout = process.stdout
    said: list[str] = []
    reader = threading.Thread(target=lambda: said.append(stdout.readline()))
    reader.start()
    reader.join(CHILD_SECONDS)
    try:
        assert said == ["imported\n"], "the import waited for stdin to close"
    finally:
        process.stdin.close()
        code = process.wait(CHILD_SECONDS)

    assert code == 0
    assert stdout.read().strip() == "interrupted"


def test_a_refusal_is_one_line_and_78() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from app.infrastructure.process.supervision import refuse_to_start\n"
            "refuse_to_start('SolidVision cannot start: a reason')",
        ],
        env={**os.environ, "PYTHONPATH": str(BACKEND)},
        capture_output=True,
        text=True,
        timeout=CHILD_SECONDS,
    )

    assert completed.returncode == 78
    assert completed.stderr == "SolidVision cannot start: a reason\n"
    assert completed.stdout == ""


def _wait_for_exit(pid: int, seconds: float) -> int | None:
    """The exit code of a process this test did not create, by its PID."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(0x1000 | 0x00100000, False, pid)
    if not handle:
        return None
    try:
        kernel32.WaitForSingleObject(handle, int(seconds * 1000))
        code = wintypes.DWORD()
        kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        return int(code.value)
    finally:
        kernel32.CloseHandle(handle)
