"""A real child: its pipes, its last lines, its stop and its end (RFC-036 5.5, 7.1).

Each test starts a small Python program the way the launcher starts its
children -- `-X utf8`, `CREATE_NO_WINDOW`, the three handles on pipes -- and
looks at what the launcher's side sees.
"""

from __future__ import annotations

import sys
import threading
import time

import pytest

from app.launcher.children import (
    API,
    JOB_RUNNER,
    TAIL_LINES,
    ChildProcess,
    child_command,
    last_line,
)

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows processes")

PATH_OUTSIDE_THE_CODE_PAGE = "Łódź/東京/✈.jpg"
"""The name RFC-034 section 7.5 used: outside code page 1252 on every side."""

UNTIL_STDIN_ENDS = f"""
import sys
print("starting", flush=True)
print("indexing {PATH_OUTSIDE_THE_CODE_PAGE}", flush=True)
print("a warning on stderr", file=sys.stderr, flush=True)
for number in range({TAIL_LINES + 10}):
    print("line", number, flush=True)
sys.stdin.buffer.read()
print("stdin ended", flush=True)
"""

DEADLINE = 30.0


def start(source: str) -> tuple[ChildProcess, list[str]]:
    echoed: list[str] = []
    lock = threading.Lock()

    def echo(line: str) -> None:
        with lock:
            echoed.append(line)

    child = ChildProcess.start(
        "test", [sys.executable, "-X", "utf8", "-c", source], echo=echo
    )
    return child, echoed


def wait_for_exit(child: ChildProcess) -> int:
    deadline = time.monotonic() + DEADLINE
    while (code := child.poll()) is None:
        assert time.monotonic() < deadline, "the child did not exit"
        time.sleep(0.05)
    return code


def wait_for_line(echoed: list[str], line: str) -> None:
    deadline = time.monotonic() + DEADLINE
    while line not in echoed:
        assert time.monotonic() < deadline, f"never saw {line!r}"
        time.sleep(0.02)


def test_closing_stdin_is_the_end_of_file_the_child_sees() -> None:
    child, echoed = start(UNTIL_STDIN_ENDS)
    wait_for_line(echoed, "test | starting")

    child.request_stop()

    assert wait_for_exit(child) == 0
    assert child.drain()
    assert "test | stdin ended" in echoed


def test_every_line_reaches_the_launcher_with_the_childs_name() -> None:
    child, echoed = start(UNTIL_STDIN_ENDS)
    child.request_stop()
    wait_for_exit(child)
    child.drain()

    assert "test | a warning on stderr" in echoed
    assert f"test | indexing {PATH_OUTSIDE_THE_CODE_PAGE}" in echoed


def test_a_path_outside_the_code_page_crosses_the_pipe_intact() -> None:
    """Section 5.5: in UTF-8 mode the pipe carries everything."""
    child, echoed = start(UNTIL_STDIN_ENDS)
    child.request_stop()
    wait_for_exit(child)
    child.drain()

    assert not any("�" in line or "\\u" in line for line in echoed)


def test_the_last_fifty_lines_are_kept() -> None:
    child, _ = start(UNTIL_STDIN_ENDS)
    child.request_stop()
    wait_for_exit(child)
    child.drain()

    lines = child.last_lines()
    assert len(lines) == TAIL_LINES
    assert lines[-1] == "stdin ended"
    assert last_line(lines) == "stdin ended"


def test_the_readers_end_when_the_child_does() -> None:
    """The hypothesis of section 5.5: nobody else holds the pipes."""
    child, _ = start("print('bye')")

    wait_for_exit(child)

    assert child.drain(timeout=10)


def test_a_child_that_does_not_stop_is_terminated() -> None:
    child, echoed = start("import time\nprint('stubborn', flush=True)\ntime.sleep(120)")
    wait_for_line(echoed, "test | stubborn")
    child.request_stop()
    time.sleep(0.5)
    assert child.poll() is None

    child.terminate()

    assert child.poll() == 1


def test_the_last_line_is_the_last_that_says_something() -> None:
    assert last_line(("first", "the line", "", "  ")) == "the line"
    assert last_line(()) is None


def test_the_command_is_the_launchers_interpreter_in_utf8_mode() -> None:
    assert child_command(API, ["--port", "1", "--supervised"]) == [
        sys.executable,
        "-X",
        "utf8",
        "-m",
        "app.presentation.api",
        "--port",
        "1",
        "--supervised",
    ]
    assert child_command(JOB_RUNNER, ["--supervised"])[-2:] == [
        "app.infrastructure.workers.job_runner",
        "--supervised",
    ]
