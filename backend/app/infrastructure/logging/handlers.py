"""Handler implementations for the logging infrastructure."""

from __future__ import annotations

import logging
import os
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .formatters import AppFormatter

ROLLOVER_RETRY_SECONDS = 1.0
"""How long a rotation that could not happen waits before it is tried again.

Measured (RFC-034 section 16): an attempt that fails -- close, the rename
Windows refuses, reopen -- costs 0.19 ms on the hard disk of the
measurement, against 0.01 ms for a record. Tried on every record, it made
600 records ten times slower. Once a second it is 0.02 % of the time, and
the criterion declared before measuring was the smallest of 1, 5 and 30 s
under 0.1 %: smaller is better past that point, because it is how long a
file stays over its limit after the other process has gone.
"""


class LosslessRotatingFileHandler(RotatingFileHandler):
    """A rotation that drops neither the record nor a backup (RFC-034 8.3).

    Windows refuses to rename a file another process holds open, and Python's
    `open()` does not ask to share that. The standard handler shifts the
    backups *before* trying the rename that fails, and then discards the
    record that triggered it: two processes on one file lost 381 records of
    600 and four of five backups (measured, section 2.6).

    Here the order is the other way around:

    1. The rename that can fail goes first -- the current file's.
    2. If it fails, the file is reopened, **the record is written**, and the
       next attempt is put off by `ROLLOVER_RETRY_SECONDS`. No backup moved.
    3. Only with that rename done are the backups shifted.

    The declared cost: while two live processes share a file it does not
    rotate, and it grows past the limit. The first rotation after one of
    them exits cuts it. It grows instead of losing lines.
    """

    _next_attempt = 0.0

    @property
    def _outgoing(self) -> str:
        """Where the current file waits while the backups are shifted."""
        return f"{self.baseFilename}.rotating"

    def shouldRollover(self, record: logging.LogRecord) -> bool:  # noqa: N802
        if self.backupCount <= 0 or time.monotonic() < self._next_attempt:
            return False
        return bool(super().shouldRollover(record))

    def doRollover(self) -> None:  # noqa: N802
        if self.stream:
            self.stream.close()
            self.stream = None
        try:
            # A rotation interrupted between its two steps left this behind.
            if os.path.exists(self._outgoing):
                self._shelve()
            os.rename(self.baseFilename, self._outgoing)
            self._shelve()
        except OSError:
            self._next_attempt = time.monotonic() + ROLLOVER_RETRY_SECONDS
        self.stream = self._open()

    def _shelve(self) -> None:
        """Shift `.1` to `.2` and so on, then make the outgoing file `.1`."""
        for number in range(self.backupCount - 1, 0, -1):
            source = f"{self.baseFilename}.{number}"
            if os.path.exists(source):
                os.replace(source, f"{self.baseFilename}.{number + 1}")
        os.replace(self._outgoing, f"{self.baseFilename}.1")


def build_console_handler() -> logging.Handler:
    """Create a console handler."""
    handler = logging.StreamHandler()
    handler.setFormatter(AppFormatter())
    return handler


def build_rotating_file_handler(
    log_directory: Path, log_filename: str
) -> logging.Handler:
    """Create a rotating file handler."""
    log_directory.mkdir(parents=True, exist_ok=True)
    file_path = log_directory / log_filename
    handler = LosslessRotatingFileHandler(
        filename=file_path,
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    handler.setFormatter(AppFormatter())
    return handler
