"""Showing a file to the user in their own file manager (RFC-030 section 5).

A port with one adapter, on purpose. RFC-030 section 5.3 names three
platforms and delivers one: `explorer /select,` on Windows is implemented
and tested, while `open -R` on macOS and the FileManager1 D-Bus call on
Linux are declared and not written. An adapter nobody has run is a claim of
portability nobody checked (RFC-027 section 4.1).
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.domain.value_objects.image_path import ImagePath


class FileRevealerPort(ABC):
    """Open the platform's file manager with one file selected."""

    @abstractmethod
    def reveal(self, path: ImagePath) -> None:
        """Ask the file manager to show `path`, and return once that is launched.

        Returning means the request was *launched*, never that a window
        opened: `explorer.exe` exits non-zero even when it succeeds, so no
        exit status here could be trusted to say more (RFC-030 section
        5.1).

        `path` is never anything a client sent. It is built by the server
        from a row and a mount point resolved now, which is the entire
        security property of `/reveal` -- implementations must not accept a
        string, and must never pass one through a shell.

        Raises `FileNotFoundError` when nothing is at `path`, exactly as
        opening it would. A file manager asked to select a missing file
        does not fail; Explorer silently opens a default folder instead, so
        without this check the user would see an unrelated window and no
        error. What a missing file *means* -- a 410, because the disk is
        connected and the photo is not on it -- is the caller's decision,
        which is why this raises the I/O error rather than a domain one.
        """
