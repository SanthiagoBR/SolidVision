"""Interrupts that wait for the database call in flight (RFC-036 section 7.5).

The job runner is stopped by a Ctrl+C -- the launcher's, through the end of
its stdin (section 7.1) -- and the `KeyboardInterrupt` lands wherever the main
thread is. Inside a database call, it costs a new connection, one of two ways:

* psycopg, interrupted while a query runs, sends the server a cancel request
  (`Connection.wait`, psycopg 3.3), and a cancel request is a connection; or
* SQLAlchemy invalidates a connection an interrupt went through, and the next
  call -- the `release()` of the job -- opens another.

PostgreSQL on Windows serves each connection in a new process, and during a
logoff no new process starts: it dies with `0xC000026B`
(`STATUS_DLL_INIT_FAILED_LOGOFF`), the postmaster takes that for a crash and
ends every other server process, cannot start its startup process either, and
shuts down -- with the job still `running`, for the reaper. Measured in two
logoffs of three on 2026-10-20.

So, in a process that called `install()`, an interrupt that arrives while the
main thread is inside `deferred()` waits for the block to end, and is raised
there: after the call returned, outside psycopg and SQLAlchemy, with the
connection whole and free for the `release()`. Anywhere else it is raised at
once, as before -- in the import of `torch`, the encoding of a batch, the read
of a file -- so a stop takes no longer than it did. `deferring()` puts every
method of a repository inside `deferred()`.

The main thread only: Python runs signal handlers there, and only there can an
interrupt land in a call. Standard library only, like `supervision`.
"""

from __future__ import annotations

import functools
import signal
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from types import FrameType
from typing import Any, cast

_thread = threading.local()
"""`depth` and `pending`, per thread. The handler runs in the main thread, and
so reads the main thread's."""


def install() -> None:
    """Make SIGINT -- a Ctrl+C, or the stdin watch's -- wait for `deferred()`."""
    signal.signal(signal.SIGINT, _interrupt)


@contextmanager
def deferred() -> Iterator[None]:
    """A block no interrupt lands in: one that arrives is raised as it ends.

    Nested blocks raise at the outermost end. Raised while another exception
    leaves the block, the interrupt wins, with that exception as its context:
    it is what the operator asked for.
    """
    _thread.depth = _depth() + 1
    try:
        yield
    finally:
        _thread.depth -= 1
        if _thread.depth == 0 and getattr(_thread, "pending", False):
            _thread.pending = False
            raise KeyboardInterrupt


def deferring[T](target: T) -> T:
    """`target`, with each of its methods called inside `deferred()`."""
    return cast(T, _Deferring(target))


class _Deferring:
    def __init__(self, target: object) -> None:
        self._target = target

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._target, name)
        if not callable(attribute):
            return attribute
        return _deferred_call(attribute)

    def __repr__(self) -> str:
        return f"deferring({self._target!r})"


def _deferred_call(method: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(method)
    def call(*args: Any, **kwargs: Any) -> Any:
        with deferred():
            return method(*args, **kwargs)

    return call


def _depth() -> int:
    return int(getattr(_thread, "depth", 0))


def _interrupt(signum: int, frame: FrameType | None) -> None:
    if _depth():
        _thread.pending = True
        return
    raise KeyboardInterrupt
