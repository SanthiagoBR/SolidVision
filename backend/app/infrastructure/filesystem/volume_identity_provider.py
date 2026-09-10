"""Volume identity: the port, and the Windows adapter behind it (RFC-027).

This module answers two questions and refuses to answer a third.

*What volume is this path on?* -- `resolve()`, which returns an identifier
the operating system guarantees to be stable across remounts, reboots and
ports, together with everything else worth knowing while the disk happens
to be in front of us.

*What is mounted right now?* -- `mounted_volumes()`, which is how RFC-027
section 7 turns "is HD3 connected" into a question with a fresh answer
instead of a stale column.

*Where does device X live?* has no answer here on its own, and that is
deliberate: it is `mounted_volumes()` looked up by identity, so the code
that wants a drive letter has to go through the enumeration that was just
performed rather than through something remembered.

**Only the Windows adapter ships.** `VolumeKind` declares the Linux and
macOS discriminators so that adding those adapters needs no migration, but
an adapter nobody has run would be a portability claim nobody verified
(RFC-027 section 4.1).
"""

from __future__ import annotations

import ctypes
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.domain.value_objects.device_id import VolumeIdentity, VolumeKind


class VolumeIdentityError(RuntimeError):
    """Raised when the operating system will not identify a volume.

    An Infrastructure failure rather than a `DomainError`: the domain has
    no opinion about `kernel32`. It is raised, never swallowed into a
    `None`, because every caller is about to key data on the answer --
    guessing an identity would file a disk's photos under the wrong
    device, which is worse than failing.
    """


@dataclass(frozen=True)
class ResolvedVolume:
    """Everything known about a volume while it is actually mounted.

    Splits cleanly in two, and the split is the point. `identity` is
    durable and gets persisted. `mount_point` is not, and does not: it is
    valid until the user unplugs the disk or reboots, which is why it
    travels in a return value rather than in a column (RFC-027 section 4).
    """

    identity: VolumeIdentity
    mount_point: Path
    """Where the volume is attached *at this instant* -- `D:\\`, typically.

    Never persist this. It is a function of mount order, and storing it is
    the defect RFC-027 exists to remove.
    """

    filesystem_label: str | None = None
    total_bytes: int | None = None


class VolumeIdentityProvider(ABC):
    """Port for asking the operating system which volume a path lives on."""

    @abstractmethod
    def resolve(self, path: Path) -> ResolvedVolume:
        """Identify the volume holding `path`, which must exist and be mounted.

        Raises `VolumeIdentityError` when the platform cannot answer --
        a disconnected disk, a network share with no volume identity, a
        path that does not exist. There is no fallback to the drive
        letter: a letter standing in for an identity would be
        indistinguishable from a real one and would silently recreate the
        bug (RFC-027 section 2.1).
        """

    @abstractmethod
    def mounted_volumes(self) -> dict[VolumeIdentity, Path]:
        """Return every volume mounted right now, mapped to its mount point.

        The whole of RFC-027 section 7's connection check: a device is
        connected exactly when its `volume_identity` is a key of this
        mapping, and the value is where to find it. Callers may cache the
        result for the duration of one request and must not cache it
        across requests -- the answer changes when a user pulls a cable,
        and nothing notifies this process when they do.
        """


class WindowsVolumeIdentityProvider(VolumeIdentityProvider):
    r"""Windows adapter over `kernel32`, via `ctypes` and no new dependency.

    Uses `\\?\Volume{GUID}\` -- the name Windows itself uses for the volume
    internally, independent of where it is mounted. The two rejected
    candidates and why (RFC-027 section 4.1): the drive letter is not
    stable at all, and the 32-bit volume serial number from
    `GetVolumeInformation` is stable but only 32 bits, which is not enough
    to key a table on.

    The GUID changes when the volume is reformatted. That is accepted and
    needs no handling: reformatting destroys the photos, so orphaning
    their rows is the correct outcome.

    The one limitation left standing is that a byte-for-byte clone of a
    disk carries the same GUID, so the two are indistinguishable here
    (RFC-027 section 13). Declared, not solved.
    """

    _BUFFER_LENGTH = 512
    """Comfortably past `MAX_PATH` (260).

    A volume GUID name is 49 characters, but a mounted-folder path can be
    longer than `MAX_PATH` on a modern Windows, so the buffer is sized to
    make truncation impossible rather than unlikely.
    """

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise VolumeIdentityError(
                "WindowsVolumeIdentityProvider requires Windows; this process "
                f"is running on {sys.platform!r}."
            )

    def resolve(self, path: Path) -> ResolvedVolume:
        """Walk `path` back to its mount point, then name that volume."""
        mount_point = self._volume_path_name(path)
        identity = VolumeIdentity(
            value=self._volume_guid_name(mount_point),
            kind=VolumeKind.WINDOWS_VOLUME_GUID,
        )
        return ResolvedVolume(
            identity=identity,
            mount_point=mount_point,
            filesystem_label=self._filesystem_label(mount_point),
            total_bytes=self._total_bytes(mount_point),
        )

    def mounted_volumes(self) -> dict[VolumeIdentity, Path]:
        """Name every drive-letter mount point Windows currently reports.

        Built from `GetLogicalDriveStrings` rather than from
        `FindFirstVolume`, because a volume with no mount path is not
        something this application can do anything with: every caller
        either joins a `relative_path` onto it or shows it to a user.

        Volumes that refuse to be named are skipped rather than raised on.
        An empty optical drive and a locked BitLocker volume both fail
        here, and neither is a reason to refuse to report the disks that
        did answer.
        """
        mounted: dict[VolumeIdentity, Path] = {}
        for mount_point in self._logical_drive_mount_points():
            try:
                guid_name = self._volume_guid_name(mount_point)
            except VolumeIdentityError:
                continue
            identity = VolumeIdentity(
                value=guid_name, kind=VolumeKind.WINDOWS_VOLUME_GUID
            )
            mounted[identity] = mount_point
        return mounted

    @staticmethod
    def _kernel32() -> Any:
        """Return `kernel32`, loaded with the wide-character entry points.

        Looked up per call rather than cached in a module global so that
        importing this module stays free on any platform: most of the test
        suite reaches this package transitively, and `ctypes.WinDLL` does
        not exist off Windows.

        Typed `Any` because a `ctypes` DLL handle has no static signature
        to check against -- every attribute on it is resolved at runtime
        by name, so annotating it more tightly would describe a guarantee
        `ctypes` does not make.
        """
        return ctypes.WinDLL("kernel32", use_last_error=True)

    def _volume_path_name(self, path: Path) -> Path:
        r"""Return the mount point `path` sits under, e.g. `D:\`.

        `GetVolumePathName` is what makes a mounted folder work as well as
        a drive letter: for `D:\photos\2018` it answers `D:\`, and for a
        volume mounted at `C:\mnt\archive` it answers `C:\mnt\archive\`
        rather than `C:\`. Taking `path.anchor` instead would be right for
        drive letters and quietly wrong for every mounted folder.
        """
        buffer = ctypes.create_unicode_buffer(self._BUFFER_LENGTH)
        if not self._kernel32().GetVolumePathNameW(
            str(path), buffer, self._BUFFER_LENGTH
        ):
            raise VolumeIdentityError(
                f"Could not determine the mount point of {path} "
                f"(Windows error {ctypes.get_last_error()})."
            )
        return Path(buffer.value)

    def _volume_guid_name(self, mount_point: Path) -> str:
        r"""Return `\\?\Volume{GUID}\` for a mount point.

        The mount point must reach Windows with a trailing separator;
        `GetVolumeNameForVolumeMountPoint` fails outright without one, and
        `str(Path("D:/mnt/archive"))` has none, so it is appended here
        rather than assumed.
        """
        buffer = ctypes.create_unicode_buffer(self._BUFFER_LENGTH)
        if not self._kernel32().GetVolumeNameForVolumeMountPointW(
            self._with_trailing_separator(mount_point), buffer, self._BUFFER_LENGTH
        ):
            raise VolumeIdentityError(
                f"Could not read the volume identity of {mount_point} "
                f"(Windows error {ctypes.get_last_error()})."
            )
        return buffer.value

    def _filesystem_label(self, mount_point: Path) -> str | None:
        """Return the volume's own label, or `None` when it has none.

        Informative only. Empty is normal -- an unlabelled disk reports
        `""` -- and comes back as `None` rather than as an empty string,
        so that "no label" is one value rather than two.
        """
        buffer = ctypes.create_unicode_buffer(self._BUFFER_LENGTH)
        if not self._kernel32().GetVolumeInformationW(
            self._with_trailing_separator(mount_point),
            buffer,
            self._BUFFER_LENGTH,
            None,
            None,
            None,
            None,
            0,
        ):
            return None
        return buffer.value or None

    def _total_bytes(self, mount_point: Path) -> int | None:
        """Return the volume's capacity, or `None` when Windows will not say."""
        total = ctypes.c_ulonglong(0)
        if not self._kernel32().GetDiskFreeSpaceExW(
            self._with_trailing_separator(mount_point),
            None,
            ctypes.byref(total),
            None,
        ):
            return None
        return int(total.value)

    def _logical_drive_mount_points(self) -> list[Path]:
        """Return every drive-letter root Windows currently has, as paths.

        `GetLogicalDriveStrings` fills the buffer with a run of
        NUL-terminated strings closed by a second NUL, so the split below
        is the documented way to read the result rather than string
        manipulation standing in for a parser.
        """
        buffer = ctypes.create_unicode_buffer(self._BUFFER_LENGTH)
        length = self._kernel32().GetLogicalDriveStringsW(self._BUFFER_LENGTH, buffer)
        if not length:
            raise VolumeIdentityError(
                "Could not enumerate mounted drives "
                f"(Windows error {ctypes.get_last_error()})."
            )
        # Sliced and re-joined rather than read through `buffer.value`,
        # which stops at the first NUL and would therefore report only the
        # first drive. `"".join()` is correct whether the slice comes back
        # as a string or as a sequence of characters.
        raw = "".join(buffer[:length])
        return [Path(entry) for entry in raw.split("\0") if entry]

    @staticmethod
    def _with_trailing_separator(mount_point: Path) -> str:
        text = str(mount_point)
        return text if text.endswith(("\\", "/")) else text + "\\"
