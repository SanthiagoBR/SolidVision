"""`postmaster.pid`, read as the server wrote it (RFC-033 sections 8.2 and 9).

The server's own lock on the data directory, and the one source of truth
about a running cluster: line 1 is the PID, line 2 the data directory, line 4
the port the server is *using* -- `solidvision.conf` only says which one the
adapter *asked for* -- and line 8 the state. A state file of the adapter's own
would be a third source able to disagree with both.

Read-only. The adapter never deletes this file (section 9.2): with a server
alive, deleting it lets a second postmaster start on the same `data\\`, and
two postmasters in one directory corrupt the cluster. Whether a lock left
behind is still valid is the server's decision, not this module's.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

STATUS_STARTING = "starting"
STATUS_READY = "ready"
STATUS_STOPPING = "stopping"
STATUS_STANDBY = "standby"


@dataclass(frozen=True)
class PostmasterPid:
    """The lines of `postmaster.pid` the adapter acts on."""

    pid: int
    data_directory: str
    port: int | None
    status: str | None
    """`starting`, `ready`, `stopping` or `standby`; `None` until line 8 exists.

    The server writes the file in stages while it starts, so a file read in
    the first milliseconds can have a PID and nothing after it.
    """

    def points_to(self, data: Path) -> bool:
        """Whether line 2 names this data directory (section 8.4).

        The server canonicalizes the path with forward slashes, and the case
        may differ from the one the adapter passed; `samefile` is the last
        word when both spellings exist.
        """
        if not self.data_directory:
            return False
        recorded = os.path.normcase(os.path.normpath(self.data_directory))
        expected = os.path.normcase(os.path.normpath(str(data)))
        if recorded == expected:
            return True
        try:
            return os.path.samefile(self.data_directory, data)
        except OSError:
            return False


def read_postmaster_pid(path: Path) -> PostmasterPid | None:
    """Parse `postmaster.pid`, or `None` when it is absent or has no PID yet."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    # The server writes paths in the ANSI code page, like every narrow-`main`
    # program on Windows (section 10).
    encoding = "mbcs" if sys.platform == "win32" else "utf-8"
    lines = [line.rstrip("\r") for line in raw.decode(encoding, "replace").split("\n")]
    if not lines or not lines[0].strip().isdigit():
        return None

    def line(number: int) -> str:
        return lines[number - 1].strip() if len(lines) >= number else ""

    port = line(4)
    return PostmasterPid(
        pid=int(lines[0].strip()),
        data_directory=line(2),
        port=int(port) if port.isdigit() else None,
        status=line(8) or None,
    )
