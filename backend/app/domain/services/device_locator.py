"""Asking the filesystem where a device is, without importing the filesystem.

Two questions the Application must be able to ask before it accepts a job
-- *is this disk plugged in right now?* and *is this folder actually on
it?* -- and both are questions for the operating system. The adapter that
answers them is `WindowsVolumeIdentityProvider`, which is an
Infrastructure ABC the Application is forbidden to import
(`test_application_architecture.py` fails the build if it does), so the
dependency is inverted here instead.

**Nothing may cache across calls.** A device's mount point is a fact about
this instant, and the whole of RFC-027 section 7 is that no stored value
can keep it -- the user pulls the cable and nothing notifies this process.
An implementation that remembered an answer would hand a job a drive
letter that now belongs to a different disk.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from app.domain.entities.device import Device
from app.domain.value_objects.job_scope import JobScope


class DeviceLocator(ABC):
    """Port for locating a device's files at the moment the question is asked."""

    @abstractmethod
    def mount_point(self, device: Device) -> Path | None:
        """Return where `device` is mounted right now, or `None` if nowhere.

        `None` is the answer for a disk in a drawer, and it is a first-
        class one rather than an error: the system knows the device
        perfectly well, it simply has no bytes to offer (RFC-027 section
        2.3). Callers turn it into `DeviceNotConnectedError` when they
        needed the bytes.

        The returned path is never persisted by anyone. It is valid until
        the user unplugs the disk, which is precisely why `Device` has no
        field for it.
        """

    @abstractmethod
    def resolve_scope(self, device: Device, scope: JobScope) -> JobScope | None:
        """Return `scope` as the filesystem spells it, or `None` if absent.

        Does two things that cannot be done above this layer, and both are
        why the port has this method rather than a plain `exists()`:

        * it confirms the scope resolves to a directory that is genuinely
          *inside* the device. `JobScope` has already refused `..`, a
          drive anchor and a UNC path, but an NTFS junction sitting inside
          the scope points somewhere else entirely and looks like an
          ordinary folder until it is resolved (RFC-029 section 7.1);
        * it returns the folder's canonical spelling. Windows paths are
          case-insensitive, so `Fotos` and `fotos` are one directory --
          and `normalize_scopes()` compares parts exactly, so without
          canonicalisation here two spellings of the same folder would
          both survive normalisation and be walked twice.

        `None` means the scope is not a directory on this device, which
        the caller reports as an invalid scope. A device that is not
        mounted at all answers `None` for every scope; callers check the
        connection first so that the user is told which disk to plug in
        rather than that their folder is wrong.
        """
