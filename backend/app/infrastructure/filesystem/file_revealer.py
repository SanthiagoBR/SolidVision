"""Opening Explorer with a file selected, and nothing else (RFC-030 section 5).

The adapter behind `FileRevealerPort`, which lives in the Domain beside the
other ports (`app/domain/services/file_revealer_port.py`) because the use
case that calls it may not import Infrastructure. RFC-030 section 11 listed
"port + Windows adapter" in this file; the port moved and the adapter did
not.

**Windows only, deliberately** (RFC-030 section 5.3). `open -R` on macOS and
the `org.freedesktop.FileManager1` D-Bus call on Linux are declared and not
written: an adapter that has never run is a claim nobody verified.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from app.domain.services.file_revealer_port import FileRevealerPort
from app.domain.value_objects.image_path import ImagePath

Launcher = Callable[..., object]
"""What starts the process: `subprocess.Popen`, or a recorder in tests."""


class WindowsFileRevealer(FileRevealerPort):
    """`explorer.exe /select,<path>`, without a shell and without waiting."""

    def __init__(self, launch: Launcher = subprocess.Popen) -> None:
        """Refuse to exist off Windows, like `WindowsVolumeIdentityProvider`.

        `launch` is the seam the tests use so that nothing in the suite
        opens a real Explorer window; production never passes it.
        """
        if sys.platform != "win32":
            raise RuntimeError(
                "WindowsFileRevealer requires Windows; this process is running "
                f"on {sys.platform!r}."
            )
        self._launch = launch

    def reveal(self, path: ImagePath) -> None:
        """Launch Explorer with `path` selected, or raise if nothing is there.

        Four decisions, each of which the obvious version gets wrong:

        * **An argument list, and `shell=False`.** No shell ever sees the
          path, so no character in it can mean anything but itself. The
          path came from the server's own row joined to a mount point, not
          from the client (RFC-030 section 5.1), and this is still the
          second wall rather than the only one.
        * **`/select,` and the path are two arguments, not one.** RFC-030
          section 5.1 wrote `f"/select,{path}"` as a single list item, and
          that works only until the path contains a space: `subprocess`
          then quotes the item whole, Explorer receives
          `"/select,F:\fotos\fazenda São João.jpg"`, does not recognise the
          switch inside the quotes, and opens the user's Documents folder
          with nothing selected. As two items the command line is
          `/select, "F:\fotos\fazenda São João.jpg"`, which selects the
          file. Checked on Windows 10 by opening both forms and reading
          Explorer's selection back through `Shell.Application`
          (`experiments/rfc-030-file-access/verify_explorer_select.log`).
        * **`explorer.exe` by absolute path, under `%SystemRoot%`.** With
          `shell=False`, Windows resolves a bare `explorer` by looking in
          the Python executable's directory and the current directory
          *before* the system ones -- so a stray `explorer.exe` in a
          virtualenv's `Scripts` folder would be what ran.
        * **`Popen`, never `run`.** RFC-030 section 5.1 wrote
          `subprocess.run(..., check=False)`, and the `check=False` half
          was right: Explorer exits non-zero on success, so its status
          means nothing. The `run` half waits for the process to exit, and
          with "launch folder windows in a separate process" enabled that
          process is the window -- the request would hang until the user
          closed it. 204 means *launched*, which is all anyone could know.

        The path is checked before launching because Explorer does not fail
        for a missing file: it silently opens a default folder, and the
        user would see an unrelated window with no explanation.
        """
        target = path.value
        if not target.is_file():
            raise FileNotFoundError(f"No file at {path}")

        self._launch(
            [str(_explorer_executable()), "/select,", os.fspath(target)],
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def _explorer_executable() -> Path:
    """The system's `explorer.exe`, never whichever one the search order finds."""
    return Path(os.environ.get("SystemRoot", r"C:\Windows")) / "explorer.exe"
