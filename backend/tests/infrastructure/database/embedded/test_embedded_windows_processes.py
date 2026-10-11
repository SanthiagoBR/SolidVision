"""The process table adapter, against the machine actually running.

Like the volume adapter's tests, these call `kernel32` for real and assert on
properties: this process is in the snapshot under its own PID, its image is
the interpreter running the test, a child started here names this process as
its parent, and a terminated child stops being alive. No PostgreSQL is
involved -- the adapter is about processes, not about servers.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.infrastructure.database.embedded.windows_processes import (
    ProcessTable,
    WindowsProcesses,
)

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="The embedded cluster is Windows-only (RFC-033 section 4.1)",
)


@pytest.fixture()
def processes() -> WindowsProcesses:
    return WindowsProcesses()


@pytest.fixture()
def sleeper() -> Iterator[subprocess.Popen[bytes]]:
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        yield child
    finally:
        child.kill()
        child.wait(timeout=10)


def test_the_adapter_implements_the_port() -> None:
    assert issubclass(WindowsProcesses, ProcessTable)


def test_this_process_is_in_the_snapshot(processes: WindowsProcesses) -> None:
    by_pid = {process.pid: process for process in processes.snapshot()}

    assert os.getpid() in by_pid
    assert by_pid[os.getpid()].name.lower().startswith("python")


def test_the_image_path_is_the_interpreter_running_the_test(
    processes: WindowsProcesses,
) -> None:
    executable = processes.executable(os.getpid())

    assert executable is not None
    assert os.path.samefile(executable, _base_executable())


def test_a_child_names_this_process_as_its_parent(
    processes: WindowsProcesses, sleeper: subprocess.Popen[bytes]
) -> None:
    """Section 9.3 rests on this: an orphan is found by its parent PID.

    Through a virtual environment the interpreter starts the real one as a
    child of its own, so the chain from the sleeper back to this process may
    be one PID long or two.
    """
    parents = {process.pid: process.parent_pid for process in processes.snapshot()}

    ancestors = []
    pid = sleeper.pid
    for _ in range(3):
        pid = parents.get(pid, 0)
        ancestors.append(pid)

    assert os.getpid() in ancestors


def test_a_running_process_is_alive_and_a_terminated_one_is_not(
    processes: WindowsProcesses, sleeper: subprocess.Popen[bytes]
) -> None:
    assert processes.is_alive(sleeper.pid) is True

    assert processes.terminate(sleeper.pid) is True
    sleeper.wait(timeout=10)

    assert processes.is_alive(sleeper.pid) is False


def test_a_pid_nobody_has_is_not_alive_and_has_no_image(
    processes: WindowsProcesses,
) -> None:
    taken = {process.pid for process in processes.snapshot()}
    # PIDs on Windows are multiples of four; an odd one is never handed out.
    nobody = next(pid for pid in range(999_999, 0, -2) if pid not in taken)

    assert processes.is_alive(nobody) is False
    assert processes.executable(nobody) is None
    assert processes.terminate(nobody) is False
    assert processes.is_alive(0) is False


def _base_executable() -> Path:
    """The image actually running: a venv's `python.exe` re-executes the base one."""
    return Path(getattr(sys, "_base_executable", sys.executable))
