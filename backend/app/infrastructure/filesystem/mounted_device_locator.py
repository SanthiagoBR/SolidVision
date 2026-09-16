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

from pathlib import Path

from app.domain.entities.device import Device
from app.domain.services.device_locator import DeviceLocator
from app.domain.value_objects.job_scope import JobScope
from app.infrastructure.filesystem.volume_identity_provider import (
    VolumeIdentityProvider,
)


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

        if resolved == root:
            return JobScope()
        return JobScope(resolved.relative_to(root))
