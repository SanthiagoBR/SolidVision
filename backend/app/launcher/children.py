r"""One child: how it is created, read, asked to stop, and ended (RFC-036 5.5, 7).

    <python> -X utf8 -m app.presentation.api --port 51234 --supervised
    <python> -X utf8 -m app.infrastructure.workers.job_runner --supervised

`<python>` is the launcher's own interpreter, and `child_command()` is the one
function to change when the runtime is frozen, as `detect_layout()` is for the
root (RFC-034 section 4.1).

* **`CREATE_NO_WINDOW`.** Each child gets a console nobody sees: no window
  opens when the launcher has no console, and a Ctrl+C in the developer's
  terminal reaches only the launcher, which orders the stop (section 7.2).
* **stdin is a pipe only the launcher holds, and never writes.** It exists to
  be closed: that is the request to stop (section 7.1). When the launcher
  dies, Windows closes it, and the request arrives just the same (7.4).
* **stdout and stderr are pipes**, each read by a thread that never stops
  reading -- a pipe nobody empties fills up, and blocks the child on its next
  write. Each line goes to the launcher's stderr with the child's name in
  front, and the last 50 stay in memory, for the `child` event and for the
  launcher's log when the child dies. The children still write their own
  logs (RFC-034 section 8.2).
* **`-X utf8`.** Through a pipe, Python writes in the ANSI code page, and a
  path outside it in a log line becomes an encoding error in the console
  handler; in UTF-8 mode the pipe carries everything, decoded here as UTF-8.
* **Nobody inherits these pipes.** `subprocess` hands a child only the
  handles it is given (`close_fds`, the default since Python 3.7 on Windows),
  and neither child starts a long-lived process of its own: the API's one is
  Explorer, for the reveal, with its three handles on `DEVNULL`. So the two
  readers end when the child does (section 16 checks it).
* **The environment is the launcher's, with nothing added.** The children
  compute the same layout and read the same file (RFC-034 section 5.2).
"""

from __future__ import annotations

import subprocess
import sys
import threading
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import IO, Protocol, TextIO

CREATE_NO_WINDOW = 0x08000000

TAIL_LINES = 50

DRAIN_SECONDS = 2.0
"""How long the readers of a child that exited get to deliver its last lines."""

TERMINATE_WAIT_SECONDS = 10.0


@dataclass(frozen=True)
class Program:
    """A child, by the name the events use and the module `-m` runs."""

    name: str
    module: str


API = Program("api", "app.presentation.api")
JOB_RUNNER = Program("job_runner", "app.infrastructure.workers.job_runner")


def child_command(program: Program, arguments: Sequence[str]) -> list[str]:
    """The command line of a child, built in one place."""
    return [sys.executable, "-X", "utf8", "-m", program.module, *arguments]


def last_line(lines: Sequence[str]) -> str | None:
    """The last line that says something: the `detail` of a `child` event."""
    for line in reversed(lines):
        if line.strip():
            return line.strip()
    return None


class Child(Protocol):
    """What the supervisor needs of a child; the tests stand in a double."""

    @property
    def name(self) -> str: ...

    @property
    def pid(self) -> int: ...

    def poll(self) -> int | None:
        """The exit code, or `None` while it runs."""

    def request_stop(self) -> None:
        """Close its stdin: the Ctrl+C of section 7.1."""

    def terminate(self) -> None:
        """`TerminateProcess`, and wait for it to be gone."""

    def drain(self, timeout: float = DRAIN_SECONDS) -> bool:
        """Wait for the readers of an exited child to reach the end."""

    def last_lines(self) -> tuple[str, ...]:
        """The last lines it wrote, oldest first."""


class StderrEcho:
    """Where the children's lines go: the launcher's stderr, a line at a time.

    One lock for every reader, so that two children's lines never interleave.
    A stderr that went away -- the window closed the pipe -- is ignored.
    """

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream
        self._lock = threading.Lock()

    def __call__(self, line: str) -> None:
        with self._lock:
            try:
                self._stream.write(line + "\n")
                self._stream.flush()
            except (OSError, ValueError):
                pass


class ChildProcess:
    """A real child process, with its two readers."""

    def __init__(
        self,
        name: str,
        process: subprocess.Popen[bytes],
        echo: Callable[[str], None],
    ) -> None:
        self._name = name
        self._process = process
        self._echo = echo
        self._lines: deque[str] = deque(maxlen=TAIL_LINES)
        self._lines_lock = threading.Lock()
        self._readers = [
            self._read_in_thread(process.stdout),
            self._read_in_thread(process.stderr),
        ]

    @classmethod
    def start(
        cls, name: str, command: Sequence[str], *, echo: Callable[[str], None]
    ) -> ChildProcess:
        process = subprocess.Popen(
            list(command),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW,
        )
        return cls(name, process, echo)

    @property
    def name(self) -> str:
        return self._name

    @property
    def pid(self) -> int:
        return self._process.pid

    def poll(self) -> int | None:
        return self._process.poll()

    def request_stop(self) -> None:
        stdin = self._process.stdin
        if stdin is not None and not stdin.closed:
            try:
                stdin.close()
            except OSError:
                pass

    def terminate(self) -> None:
        if self._process.poll() is None:
            try:
                self._process.kill()
            except OSError:
                pass
        try:
            self._process.wait(TERMINATE_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            pass
        self.request_stop()

    def drain(self, timeout: float = DRAIN_SECONDS) -> bool:
        for reader in self._readers:
            reader.join(timeout)
        return not any(reader.is_alive() for reader in self._readers)

    def last_lines(self) -> tuple[str, ...]:
        with self._lines_lock:
            return tuple(self._lines)

    def _read_in_thread(self, stream: IO[bytes] | None) -> threading.Thread:
        reader = threading.Thread(
            target=self._read,
            args=(stream,),
            name=f"{self._name}-output",
            daemon=True,
        )
        reader.start()
        return reader

    def _read(self, stream: IO[bytes] | None) -> None:
        if stream is None:
            return
        with stream:
            for raw in iter(stream.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                with self._lines_lock:
                    self._lines.append(line)
                self._echo(f"{self._name} | {line}")
