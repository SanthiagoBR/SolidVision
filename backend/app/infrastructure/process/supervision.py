"""What a supervised program owes the launcher, and how it is asked to stop (RFC-036).

Two halves of one contract, both on the side of the child:

* **How it says it cannot start.** One line on stderr and exit code 78
  (`refuse_to_start`), where it used to be the same line and exit code 1. The
  line is for a person, and is unchanged; the code is for the launcher, which
  does not restart a program that left with it (section 6.1).
* **How it hears it is asked to stop.** The launcher closes the program's
  stdin, and `stop_on_stdin_eof()` turns the end of file into the Ctrl+C the
  program already handles (section 7.1): the job runner returns its job to the
  queue, uvicorn finishes the requests in flight.

Standard library only, and nothing of the project but the constants. The
`Settings` singleton refuses through `refuse_to_start`, so this module can
import neither it nor the logger, which imports it.
"""

from __future__ import annotations

import atexit
import ctypes
import os
import signal
import sys
import threading
import time
from ctypes import wintypes
from typing import Any, NoReturn

from app.infrastructure.config.constants import EXIT_CANNOT_START

SUPERVISED_FLAG = "--supervised"
"""The argument that turns the stdin watch on, in every supervised program.

An argument and not a variable of the environment: installed, the environment
is not a source of anything (RFC-034 section 5.2), and an argument shows in the
process list for whoever investigates (RFC-036 section 7.1).
"""

POLL_SECONDS = 0.1
"""How often a stdin pipe is asked whether its other end is still open."""

_FILE_TYPE_PIPE = 0x0003


class CannotStart(SystemExit):
    """`SystemExit` with code 78, carrying the line that was printed.

    Uncaught, it ends the process with 78 and nothing more, because the line is
    already on stderr. Caught -- the launcher does it around its own import of
    the `Settings` -- the line is still at hand, for the `failed` event.
    """

    def __init__(self, line: str) -> None:
        super().__init__(EXIT_CANNOT_START)
        self.line = line


def refuse_to_start(line: str) -> NoReturn:
    """Print `line` on stderr and exit with 78: restarting will not fix this.

    No traceback (RFC-034 section 5.5): the line already names the file, the
    key, the model, and what to do.
    """
    if sys.stderr is not None:
        sys.stderr.write(line + "\n")
        sys.stderr.flush()
    raise CannotStart(line) from None


def stop_on_stdin_eof(stdin: int | None = None) -> threading.Thread:
    """Read stdin to its end in a daemon thread, then raise SIGINT in the process.

    What the main thread receives is a Ctrl+C, and it is the pilot's fastest
    path to one (RFC-036 section 2.2): `signal.raise_signal` goes through the C
    handler, which also wakes a `time.sleep()` in the middle -- measured at
    0.000 s -- where `_thread.interrupt_main()` waits for the sleep to end.

    **The request comes first, and the thread does nothing after it.** Every
    step that needs the GIL is one more wait for a main thread that may hold
    it: when the pilot's watcher wrote its own record before asking, the stop
    took 8.48 s instead of 0.289 s.

    **A pipe is never read while nothing is in it: it is asked, every
    `POLL_SECONDS`, whether its other end is still open** (`PeekNamedPipe`,
    which does not wait). Measured while implementing RFC-036, both ways of
    reading it failed on Windows:

    * A synchronous read left pending on the pipe makes Windows hold every
      other call on that handle until it completes -- and loading a DLL whose
      runtime initialization looks at the standard handles is such a call.
      With a thread blocked in `os.read(0, 1)`, `import numpy` (and so
      `import torch`) hung for as long as stdin stayed open: more than 40 s in
      the reproduction, for ever under the launcher, against 0.27 s without
      the thread.
    * Through `sys.stdin.buffer`, the reader's lock is held for the whole
      read, and a program that ends on its own -- uvicorn exiting with 3 over
      a taken port -- cannot close stdin at the interpreter's shutdown:
      *"Fatal Python error: _enter_buffered_busy: could not acquire lock for
      <_io.BufferedReader name='<stdin>'>"*, and `0xC0000005` instead of 3.

    The poll costs a stop up to `POLL_SECONDS` of the pilot's 0.000 s. A stdin
    that is not a pipe -- `NUL`, a file -- is read to its end, which for those
    does not wait. And once the program is exiting, a request to stop is moot:
    from then on SIGINT is ignored, so that one arriving during the shutdown
    is not a traceback.

    The end of file arrives in two ways, and both are a request to stop: the
    launcher closes the pipe, or the launcher dies and Windows closes its end
    (section 7.4). A process without a stdin at all has nobody holding it, and
    stops at once.

    Only called with `--supervised`: a developer running a program by hand in
    a terminal does not want it to stop when the terminal's stdin ends. Call
    it inside the `try` that handles the `KeyboardInterrupt`: a stdin already
    at its end raises the signal before this function has returned.
    """
    descriptor = stdin if stdin is not None else _stdin_descriptor()
    atexit.register(_ignore_interrupts)
    thread = threading.Thread(
        target=_stop_at_end_of,
        args=(descriptor,),
        name="stop-on-stdin-eof",
        daemon=True,
    )
    thread.start()
    return thread


def _stdin_descriptor() -> int | None:
    if sys.stdin is None:
        return None
    try:
        return sys.stdin.fileno()
    except (AttributeError, OSError, ValueError):
        return None


def _stop_at_end_of(descriptor: int | None) -> None:
    if descriptor is not None:
        try:
            handle = _pipe_handle(descriptor)
            if handle is None:
                while os.read(descriptor, 1):
                    pass
            else:
                _wait_for_the_other_end_to_close(handle, descriptor)
        except OSError:
            # A handle that went away: the same news as the end of file,
            # nobody holds the other end.
            pass
    signal.raise_signal(signal.SIGINT)


def _pipe_handle(descriptor: int) -> int | None:
    """The Windows handle behind `descriptor`, when it is a pipe."""
    if sys.platform != "win32":
        return None
    import msvcrt

    handle = msvcrt.get_osfhandle(descriptor)
    if _kernel32().GetFileType(handle) != _FILE_TYPE_PIPE:
        return None
    return handle


def _wait_for_the_other_end_to_close(handle: int, descriptor: int) -> None:
    """Return when the pipe is broken: its writer closed it, or died.

    `PeekNamedPipe` fails with `ERROR_BROKEN_PIPE` then, and any other failure
    is taken the same way -- there is nobody left to listen to. Bytes in the
    pipe, which the launcher never sends, are read without waiting and dropped.
    """
    kernel32 = _kernel32()
    available = wintypes.DWORD()
    while kernel32.PeekNamedPipe(handle, None, 0, None, ctypes.byref(available), None):
        if available.value:
            os.read(descriptor, available.value)
        else:
            time.sleep(POLL_SECONDS)


def _ignore_interrupts() -> None:
    signal.signal(signal.SIGINT, signal.SIG_IGN)


def _kernel32() -> Any:
    """`kernel32` with the two signatures used here; 64-bit handles whole."""
    kernel32: Any = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetFileType.argtypes = [wintypes.HANDLE]
    kernel32.GetFileType.restype = wintypes.DWORD
    kernel32.PeekNamedPipe.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    kernel32.PeekNamedPipe.restype = wintypes.BOOL
    return kernel32
