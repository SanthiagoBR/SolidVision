"""Locating a device's files right now, over the volume enumeration (RFC-029).

The adapter behind `DeviceLocator`: a thin shell on
`VolumeIdentityProvider.mounted_volumes()`, which is RFC-027 section 7's
answer to "is this disk connected" -- a question asked, never a column
read.

**Nothing here caches, and the absence is the design.** A cached mount
point is wrong from the moment the user pulls the cable, and Windows does
not tell this process when they do. Every call enumerates again. That is
also what makes the thing testable: a job created while a disk was plugged
in can be claimed after it is gone, and this is the layer that notices.

This is the only file in RFC-029 that knows what platform it is on, and
even that is at one remove -- it depends on the port, not on `kernel32`.
Jobs, claiming, the reaper and the checkpoint know nothing about Windows,
so a Linux volume adapter would be a new file rather than a rewrite
(RFC-029 section 6).
"""

from __future__ import annotations

import os
from pathlib import Path

from app.domain.entities.device import Device
from app.domain.services.device_locator import DeviceLocator, FolderEntry
from app.domain.value_objects.job_scope import JobScope
from app.infrastructure.filesystem.volume_identity_provider import (
    VolumeIdentityProvider,
)
from app.infrastructure.logging.logger import get_logger

logger = get_logger(__name__)


class MountedDeviceLocator(DeviceLocator):
    """Answers from what the operating system reports as mounted, every time."""

    def __init__(self, volume_provider: VolumeIdentityProvider) -> None:
        self._volumes = volume_provider

    def mount_point(self, device: Device) -> Path | None:
        """Return where `device` is attached at this instant, or `None`.

        A device is connected exactly when its volume identity is among
        the mounted ones -- the whole of RFC-027 section 7. `None` is the
        honest answer for a disk in a drawer, and callers turn it into
        `DeviceNotConnectedError` when they needed the bytes.
        """
        return self._volumes.mounted_volumes().get(device.volume_identity)

    def resolve_scope(self, device: Device, scope: JobScope) -> JobScope | None:
        """Return the scope as the filesystem spells it, or `None` if absent.

        Three things happen here, and the third is the one that is easy to
        leave out.

        1. The scope is joined onto the mount point and resolved. Resolving
           is what turns `Fotos` into the directory's real spelling, which
           `normalize_scopes()` needs: it compares parts exactly, so two
           spellings of one case-insensitive folder would otherwise both
           survive normalisation and be walked twice.
        2. It has to be a directory. A file is not a scope, and neither is
           a path that is simply not there.
        3. **It has to still be inside the device.** `JobScope` already
           refused `..`, a drive anchor and a UNC share -- but an NTFS
           junction sitting inside the scope points at another volume
           entirely and looks like an ordinary folder until it is
           resolved. `..` is not the only way out of a root, so the check
           is made against the resolved path rather than against the text.

        A device that is not mounted answers `None` for every scope.
        Callers check the connection first, so that the user is told which
        disk to plug in rather than that their folder is wrong.
        """
        located = self._locate(device, scope)
        if located is None:
            return None

        root, resolved = located
        if resolved == root:
            return JobScope()
        return JobScope(resolved.relative_to(root))

    def list_folders(self, device: Device, scope: JobScope) -> list[FolderEntry] | None:
        """Return the folders directly inside `scope`, with `has_children` filled.

        Goes through `_locate()` rather than joining the mount point onto
        the scope itself, so that a junction inside the scope is caught
        here exactly as it is caught by `resolve_scope()` -- a listing
        that escaped the device would be reading the user's `C:` drive
        through a route whose entire point is that no client-supplied
        path ever reaches a filesystem (RFC-031 section 5.1).

        **`has_children` is what this method costs.** Knowing whether
        `2018/janeiro` has subfolders of its own means opening
        `2018/janeiro`, so a listing of forty folders is forty-one
        `readdir` calls rather than one, and on a cold mechanical disk
        that is the read RFC-029 section 7.2 found dominant. Two things
        keep the bill down, and neither is optional:

        * the child scan short-circuits. `any()` over a `scandir`
          generator stops at the first entry that is a directory, so a
          folder whose first entry is a subfolder costs one entry rather
          than a full listing;
        * nothing recurses past that. The answer is a boolean, not a
          count, and RFC-031 section 2.3 refuses the subtree walk a count
          would need.

        A child that will not open -- a permission wall, a disk pulled
        mid-listing -- is reported as `has_children=False` and logged. It
        is not allowed to end the listing: a protected folder is no
        reason to stop showing the folders that answered, which is the
        rule `mounted_volumes()` already applies to volumes that refuse
        to be named.

        Files are skipped. What this feeds is a folder picker whose
        output goes back as `scopes[]`, and a file is not a scope.
        """
        located = self._locate(device, scope)
        if located is None:
            return None

        _, resolved = located
        entries: list[FolderEntry] = []
        try:
            with os.scandir(resolved) as scan:
                for entry in scan:
                    if not self._is_directory(entry):
                        continue
                    entries.append(
                        FolderEntry(
                            name=entry.name,
                            has_children=self._has_subdirectory(Path(entry.path)),
                        )
                    )
        except OSError:
            # The folder resolved a moment ago and will not open now --
            # the disk went away between the two calls. "Not a folder on
            # this device" is the honest answer and the one the caller
            # already knows how to report.
            return None
        return entries

    def _locate(self, device: Device, scope: JobScope) -> tuple[Path, Path] | None:
        """Return `(device root, resolved scope)`, or `None` if that is not a folder.

        Shared by `resolve_scope()` and `list_folders()` rather than
        written twice. The containment check below is the security
        property of both, and two copies of it would be two chances for
        one of them to lose it quietly.

        Three things happen here, and the third is the one that is easy
        to leave out.

        1. The scope is joined onto the mount point and resolved.
           Resolving is what turns `Fotos` into the directory's real
           spelling, which `normalize_scopes()` needs: it compares parts
           exactly, so two spellings of one case-insensitive folder would
           otherwise both survive normalisation and be walked twice.
        2. It has to be a directory. A file is not a scope, and neither is
           a path that is simply not there.
        3. **It has to still be inside the device.** `JobScope` already
           refused `..`, a drive anchor and a UNC share -- but an NTFS
           junction sitting inside the scope points at another volume
           entirely and looks like an ordinary folder until it is
           resolved. `..` is not the only way out of a root, so the check
           is made against the resolved path rather than against the
           text.
        """
        mount = self.mount_point(device)
        if mount is None:
            return None

        try:
            root = mount.resolve()
            target = (root / str(scope)) if scope.parts else root
            resolved = target.resolve()
            if not resolved.is_dir():
                return None
            if resolved != root and not resolved.is_relative_to(root):
                return None
        except OSError:
            # A path the platform will not answer about -- a disconnected
            # network mount, a permission wall -- is "not a folder on this
            # device" rather than a crash, which is what the caller is
            # asking and what it will report as an invalid scope.
            return None

        return root, resolved

    @staticmethod
    def _is_directory(entry: os.DirEntry[str]) -> bool:
        """Whether one listed entry is a folder, tolerating one that will not say."""
        try:
            return entry.is_dir()
        except OSError:
            logger.warning(
                "Skipping %s: the platform would not classify it", entry.path
            )
            return False

    @staticmethod
    def _has_subdirectory(folder: Path) -> bool:
        """Whether `folder` holds at least one folder, stopping at the first.

        The generator inside `any()` is what makes this cheap: `scandir`
        yields lazily, so a folder whose first entry is a directory costs
        one entry read rather than a full listing of a folder that may
        hold thousands of photos.
        """
        try:
            with os.scandir(folder) as scan:
                return any(entry.is_dir() for entry in scan)
        except OSError:
            logger.warning("Cannot look inside %s; reporting it as a leaf", folder)
            return False
