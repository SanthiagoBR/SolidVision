"""The hidden window of the end of the session (RFC-036 section 7.5).

The window is real, and so is its message loop. What a logoff does is sent to
it by hand, with `SendMessageW` from another thread, which waits for the
window's own thread to handle the message -- as Windows does. Whether Windows
sends `WM_ENDSESSION` before it ends the children is what only a real logoff
answers (section 16).
"""

from __future__ import annotations

import ctypes
import sys
import threading
from collections.abc import Iterator
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any

import pytest

from app.launcher.session_end import (
    WM_ENDSESSION,
    WM_QUERYENDSESSION,
    SessionEndWindow,
)

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="a Windows window")


def send(hwnd: int, message: int, wparam: int) -> int:
    user32: Any = ctypes.WinDLL("user32", use_last_error=True)
    user32.SendMessageW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    user32.SendMessageW.restype = wintypes.LPARAM
    return int(user32.SendMessageW(hwnd, message, wparam, 0))


@dataclass
class Window:
    hwnd: int
    calls: list[str]
    """The threads `on_session_end` was called in."""


@pytest.fixture()
def window() -> Iterator[Window]:
    calls: list[str] = []

    def stop_in_order() -> None:
        calls.append(threading.current_thread().name)

    created = SessionEndWindow(stop_in_order)
    assert created.start()
    assert created.hwnd is not None
    try:
        yield Window(created.hwnd, calls)
    finally:
        created.close()


def test_the_launcher_never_vetoes_the_end_of_a_session(window: Window) -> None:
    assert send(window.hwnd, WM_QUERYENDSESSION, 0) == 1
    send(window.hwnd, WM_ENDSESSION, 0)

    assert window.calls == []


def test_the_end_of_the_session_runs_the_stop_in_the_windows_thread(
    window: Window,
) -> None:
    """In the window's thread, and before the procedure returns: the process
    may be ended as soon as it does."""
    send(window.hwnd, WM_QUERYENDSESSION, 0)

    send(window.hwnd, WM_ENDSESSION, 1)

    assert window.calls == ["session-end-window"]


def test_a_cancelled_end_of_session_stops_nothing(window: Window) -> None:
    send(window.hwnd, WM_QUERYENDSESSION, 0)

    send(window.hwnd, WM_ENDSESSION, 0)

    assert window.calls == []


def test_the_window_is_a_top_level_one_never_shown() -> None:
    """Not a message-only window, which the documentation says receives no
    broadcast messages -- and the end of a session is one."""
    created = SessionEndWindow(lambda: None)
    assert created.start()
    try:
        user32: Any = ctypes.WinDLL("user32")
        user32.GetParent.argtypes = [wintypes.HWND]
        user32.GetParent.restype = wintypes.HWND
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        assert user32.GetParent(created.hwnd) is None
        assert not user32.IsWindowVisible(created.hwnd)
    finally:
        created.close()


def test_two_windows_in_one_process_do_not_share_a_procedure() -> None:
    first_calls: list[int] = []
    second_calls: list[int] = []
    first = SessionEndWindow(lambda: first_calls.append(1))
    second = SessionEndWindow(lambda: second_calls.append(2))
    assert first.start() and second.start()
    try:
        send(second.hwnd or 0, WM_ENDSESSION, 1)
    finally:
        first.close()
        second.close()

    assert first_calls == []
    assert second_calls == [2]
