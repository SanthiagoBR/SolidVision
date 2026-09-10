"""Device identity value objects for the domain layer (RFC-027).

Two value objects, and the distinction between them is the whole point of
this RFC. `VolumeIdentity` is *what the operating system reports* about a
volume -- an opaque string the platform guarantees to be stable across
remounts, reboots and ports. `DeviceId` is what SolidVision calls that
volume internally, derived from the identity so that the same disk always
lands on the same row.

Neither of them is a drive letter, and neither may ever become one. A
letter is a function of mount order, not of the volume (RFC-027 section
2.1), and persisting one is what makes a stationary file's path change on
its own.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from uuid import UUID

from app.domain.exceptions import (
    InvalidDeviceIdentifierError,
    InvalidVolumeIdentityError,
)


class VolumeKind(enum.Enum):
    """Which platform's identifier scheme a `VolumeIdentity` string came from.

    The discriminator exists so that the second adapter does not require a
    migration (RFC-027 section 4.1). Only `WINDOWS_VOLUME_GUID` has an
    adapter today; the other two members are declared and unimplemented on
    purpose, because a Linux adapter nobody has run would be a portability
    claim nobody verified.

    It is also what keeps the `UNIQUE` constraint on `volume_identity`
    honest: two platforms are free to mint identical-looking strings, and
    the kind is what says they are not the same volume.
    """

    WINDOWS_VOLUME_GUID = "windows-volume-guid"
    r"""`\\?\Volume{GUID}\`, from `GetVolumeNameForVolumeMountPoint`."""

    LINUX_FS_UUID = "linux-fs-uuid"
    """The filesystem UUID under `/dev/disk/by-uuid`. No adapter yet."""

    MACOS_VOLUME_UUID = "macos-volume-uuid"
    """The volume UUID reported by `diskutil`. No adapter yet."""


@dataclass(frozen=True)
class VolumeIdentity:
    """A platform-stable identifier for one physical volume.

    `value` is deliberately opaque: nothing above the adapter that
    produced it is allowed to parse it, compare it case-insensitively, or
    build a path out of it. It is a key, and the only operations on a key
    are equality and hashing.

    Stable until the volume is reformatted, which is the accepted
    limitation (RFC-027 section 4.1): reformatting destroys the photos, so
    losing the rows is the correct answer rather than a side effect.
    """

    value: str
    kind: VolumeKind

    def __post_init__(self) -> None:
        if not isinstance(self.kind, VolumeKind):
            raise InvalidVolumeIdentityError(
                "Volume identity kind must be a VolumeKind member"
            )
        if not isinstance(self.value, str) or not self.value.strip():
            raise InvalidVolumeIdentityError("Volume identity cannot be empty")

    def __str__(self) -> str:
        """Render as `kind:value`, the form the id derivation hashes.

        Both halves are included so that two platforms reporting the same
        opaque string still produce two different devices.
        """
        return f"{self.kind.value}:{self.value}"


@dataclass(frozen=True)
class DeviceId:
    """Strongly typed identifier for a device.

    Modelled on `ImageId` down to the normalisation, because the two are
    used the same way: a UUID that a repository stores as a UUID, wrapped
    so that a bare `uuid.UUID` cannot be passed where a device was meant.
    """

    value: UUID

    def __init__(self, value: UUID | str) -> None:
        normalized_value = self._normalize(value)
        object.__setattr__(self, "value", normalized_value)

    @staticmethod
    def _normalize(value: UUID | str) -> UUID:
        if isinstance(value, UUID):
            return value

        if isinstance(value, str):
            if not value.strip():
                raise InvalidDeviceIdentifierError("Device identifier cannot be empty")

            try:
                return UUID(value)
            except ValueError as exc:
                raise InvalidDeviceIdentifierError(
                    "Device identifier must be a valid UUID"
                ) from exc

        raise InvalidDeviceIdentifierError("Device identifier must be a UUID or string")

    def __str__(self) -> str:
        """Render as the plain UUID string.

        Load-bearing rather than cosmetic: `compute_image_id()` hashes
        `f"{device_id}/{relative_path}"`, so this is half of every image
        identity in the system. Changing it would change every id.
        """
        return str(self.value)
