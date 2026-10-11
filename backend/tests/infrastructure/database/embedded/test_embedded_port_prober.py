"""Choosing the port (RFC-033 section 8.2, steps 2 and 3).

The two refusals that mean "this port will not do" are simulated, because a
reserved range is a property of the machine: Hyper-V, WSL and Docker Desktop
reserve them, and a machine without any has none to probe. The last tests use
real loopback sockets, which every machine has.
"""

from __future__ import annotations

import errno
import socket
import sys
from typing import Any

import pytest

from app.infrastructure.database.embedded.cluster import PortProber

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="The embedded cluster is Windows-only (RFC-033 section 4.1)",
)

WSAEACCES = 10013
WSAEADDRINUSE = 10048
WSAENOBUFS = 10055


def refuse_bind_with(monkeypatch: pytest.MonkeyPatch, error: OSError) -> None:
    class RefusingSocket:
        def __init__(self, *_: Any) -> None:
            pass

        def __enter__(self) -> RefusingSocket:
            return self

        def __exit__(self, *_: Any) -> None:
            return None

        def setsockopt(self, *_: Any) -> None:
            return None

        def bind(self, _: tuple[str, int]) -> None:
            raise error

    monkeypatch.setattr(socket, "socket", RefusingSocket)


def winsock_error(code: int, c_errno: int) -> OSError:
    """What `bind` raises on Windows: `errno` and `winerror` both set."""
    return OSError(c_errno, "simulated", None, code)


def test_a_port_in_use_will_not_do(monkeypatch: pytest.MonkeyPatch) -> None:
    refuse_bind_with(monkeypatch, winsock_error(WSAEADDRINUSE, errno.EADDRINUSE))

    assert PortProber().usable(51336) is False


def test_a_port_in_a_reserved_range_will_not_do(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Section 2.4: a reserved port is not "in use". The bind is denied.

    `WSAEACCES`, where a probe that only knew `WSAEADDRINUSE` would let the
    error through and take the whole start down with it.
    """
    refuse_bind_with(monkeypatch, winsock_error(WSAEACCES, errno.EACCES))

    assert PortProber().usable(49700) is False


def test_any_other_failure_is_not_mistaken_for_a_busy_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refuse_bind_with(monkeypatch, winsock_error(WSAENOBUFS, errno.ENOBUFS))

    with pytest.raises(OSError):
        PortProber().usable(51336)


def test_a_listening_socket_makes_its_port_unusable() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = int(listener.getsockname()[1])

        assert PortProber().usable(port) is False


def test_the_system_hands_out_a_port_that_can_be_bound() -> None:
    prober = PortProber()

    port = prober.free_port()

    assert 1024 < port <= 65535
    assert prober.usable(port) is True


def test_the_probe_leaves_the_port_free_for_the_server() -> None:
    """The probe's socket is closed before `usable` returns."""
    prober = PortProber()
    port = prober.free_port()

    assert prober.usable(port) is True
    assert prober.usable(port) is True
