"""The end of the Windows session: a hidden window, and the stop in it (RFC-036 7.5).

At logoff Windows ends whatever is left. A job runner ended in the middle of a
job leaves it to the reaper, one attempt the poorer (section 6.3): someone who
signs out every night with an indexing running would see the job fail on the
third night.

Console control events do not help. According to the documentation of
`SetConsoleCtrlHandler`, a console process that loads `user32.dll` or
`gdi32.dll` -- Python with `ctypes` does, and so does `torch` -- receives
neither `CTRL_LOGOFF_EVENT` nor `CTRL_SHUTDOWN_EVENT`: it is treated as a
windowed application. What is documented for those is a window. A top-level
one, never shown, and not a message-only window, which the same documentation
says receives no broadcast messages.

    message                  what this window does
    WM_QUERYENDSESSION       registers the reason (`ShutdownBlockReasonCreate`)
                             and answers yes: the launcher never vetoes the
                             end of a session
    WM_ENDSESSION, ending    asks for the stop of section 7.2, with its shorter
                             deadlines, and waits for it in this thread -- the
                             process may be ended as soon as this returns --
                             then withdraws the reason
    WM_ENDSESSION, not       withdraws the reason: somebody cancelled

**The window is not enough: the launcher must also come first.** Two real
logoffs on 2026-10-19 disagreed. In the first, the children were alive when
the stop began, and stopped in 0.3 s; in the second, the children and the
postmaster were already dead -- within one tick of the supervisor -- and the
job went to the reaper with an attempt spent. Windows ends the processes of a
session from the highest shutdown level down, and every process starts at the
same one, 0x280, where the order is not defined. `end_before_the_children()`
puts the launcher above it, so that its `WM_ENDSESSION` comes, and is waited
for, while the others still live: so it did in the six logoffs that followed
(section 16, by hand). If it ever does not, the cost is the first paragraph's,
and the next session's reaper recovers the job.

**And the stop it runs creates no process**, because none starts during a
logoff (`0xC000026B`): the job runner defers an interrupt that lands in a
database call (`app.infrastructure.process.interrupts`), and the cluster is
stopped through its signal pipe (section 17.1).
"""

from __future__ import annotations

import ctypes
import logging
import os
import threading
from collections.abc import Callable
from ctypes import wintypes
from typing import Any

logger = logging.getLogger("app.app.launcher.session_end")

WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_QUERYENDSESSION = 0x0011
WM_ENDSESSION = 0x0016

REASON = "SolidVision está devolvendo a indexação à fila"
"""What the shutdown screen shows while the stop runs (section 7.5)."""

READY_SECONDS = 10.0

DEFAULT_SHUTDOWN_LEVEL = 0x280
"""Where every process starts, according to `SetProcessShutdownParameters`:
the children, the postmaster, and whatever else the session runs."""

SHUTDOWN_LEVEL = 0x300
"""Where the launcher asks to be ended: the start of the applications' first
range (0x300-0x3FF), above the default."""

LRESULT = wintypes.LPARAM
WNDPROC = ctypes.WINFUNCTYPE(
    LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)


class _WindowClass(ctypes.Structure):
    """`WNDCLASSW`, as `winuser.h` declares it."""

    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class SessionEndWindow:
    """The hidden window, in a thread of its own that pumps its messages.

    `on_session_end` is called in that thread when the session is really
    ending, and should return when the stop has ended or given up: Windows may
    end the process the moment the window procedure returns.
    """

    def __init__(self, on_session_end: Callable[[], None]) -> None:
        self._on_session_end = on_session_end
        # One class per window, so that a second window in the same process --
        # the tests make several -- never runs the procedure of the first.
        self._class_name = f"SolidVisionLauncher-{os.getpid()}-{id(self):x}"
        self._procedure = WNDPROC(self._handle)
        self._hwnd: int | None = None
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._failure: str | None = None

    @property
    def hwnd(self) -> int | None:
        return self._hwnd

    def start(self) -> bool:
        """Create the window and start its loop; `False` if Windows refused."""
        self._thread = threading.Thread(
            target=self._run, name="session-end-window", daemon=True
        )
        self._thread.start()
        self._ready.wait(READY_SECONDS)
        if self._hwnd is None:
            logger.warning(
                "The window that receives the end of the session could not be "
                "created (%s); a logoff ends the children without the ordered "
                "stop",
                self._failure or "no answer",
            )
            return False
        return True

    def close(self) -> None:
        """End the loop and destroy the window."""
        if self._hwnd is not None:
            _user32().PostMessageW(self._hwnd, WM_CLOSE, 0, 0)
        if self._thread is not None:
            self._thread.join(READY_SECONDS)

    def _run(self) -> None:
        user32 = _user32()
        instance = _kernel32().GetModuleHandleW(None)
        window_class = _WindowClass()
        window_class.lpfnWndProc = self._procedure
        window_class.hInstance = instance
        window_class.lpszClassName = self._class_name
        if not user32.RegisterClassW(ctypes.byref(window_class)):
            self._failure = f"RegisterClassW, Windows error {ctypes.get_last_error()}"
            self._ready.set()
            return
        try:
            hwnd = user32.CreateWindowExW(
                0,
                self._class_name,
                "SolidVision launcher",
                0,
                0,
                0,
                0,
                0,
                None,
                None,
                instance,
                None,
            )
            if not hwnd:
                self._failure = (
                    f"CreateWindowExW, Windows error {ctypes.get_last_error()}"
                )
                self._ready.set()
                return
            self._hwnd = hwnd
            self._ready.set()
            message = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
        finally:
            user32.UnregisterClassW(self._class_name, instance)

    def _handle(self, hwnd: int, message: int, wparam: int, lparam: int) -> int:
        user32 = _user32()
        if message == WM_QUERYENDSESSION:
            # The moment Windows began with this process: beside the deaths of
            # the children in the log, it says who went first.
            logger.info("Windows asked whether the session may end")
            user32.ShutdownBlockReasonCreate(hwnd, REASON)
            return 1
        if message == WM_ENDSESSION:
            try:
                if wparam:
                    logger.warning("The Windows session is ending; stopping in order")
                    self._on_session_end()
            except Exception:
                logger.exception("The stop at the end of the session failed")
            finally:
                user32.ShutdownBlockReasonDestroy(hwnd)
            return 0
        if message == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return int(user32.DefWindowProcW(hwnd, message, wparam, lparam))


def end_before_the_children() -> bool:
    """Ask Windows to end this process before the rest of the session.

    `SetProcessShutdownParameters`: the system ends processes from the highest
    level down. Not inherited -- a child created afterwards is at the default
    (tested) -- so the children and the postmaster stay below the launcher.
    `False`, with a line in the log, if Windows refused.
    """
    kernel32 = _kernel32()
    if not kernel32.SetProcessShutdownParameters(SHUTDOWN_LEVEL, 0):
        logger.warning(
            "The launcher could not ask to be ended before its children "
            "(Windows error %s); at a logoff, they may be ended first",
            ctypes.get_last_error(),
        )
        return False
    return True


def shutdown_level() -> int:
    """This process's shutdown level, as Windows has it."""
    level = wintypes.DWORD()
    flags = wintypes.DWORD()
    if not _kernel32().GetProcessShutdownParameters(
        ctypes.byref(level), ctypes.byref(flags)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return level.value


def _user32() -> Any:
    """`user32` with the signatures this module calls; 64-bit handles whole."""
    user32: Any = ctypes.WinDLL("user32", use_last_error=True)
    user32.RegisterClassW.argtypes = [ctypes.POINTER(_WindowClass)]
    user32.RegisterClassW.restype = wintypes.ATOM
    user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.HWND,
        wintypes.HMENU,
        wintypes.HINSTANCE,
        wintypes.LPVOID,
    ]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.GetMessageW.argtypes = [
        ctypes.POINTER(wintypes.MSG),
        wintypes.HWND,
        wintypes.UINT,
        wintypes.UINT,
    ]
    user32.GetMessageW.restype = wintypes.BOOL
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.restype = LRESULT
    user32.DefWindowProcW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    user32.DefWindowProcW.restype = LRESULT
    user32.PostMessageW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    user32.SendMessageW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    user32.SendMessageW.restype = LRESULT
    user32.PostQuitMessage.argtypes = [ctypes.c_int]
    user32.ShutdownBlockReasonCreate.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
    user32.ShutdownBlockReasonDestroy.argtypes = [wintypes.HWND]
    return user32


def _kernel32() -> Any:
    kernel32: Any = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    kernel32.SetProcessShutdownParameters.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.SetProcessShutdownParameters.restype = wintypes.BOOL
    kernel32.GetProcessShutdownParameters.argtypes = [
        wintypes.LPDWORD,
        wintypes.LPDWORD,
    ]
    kernel32.GetProcessShutdownParameters.restype = wintypes.BOOL
    return kernel32
