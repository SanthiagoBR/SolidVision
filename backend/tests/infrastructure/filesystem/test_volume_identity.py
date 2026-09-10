"""The Windows volume-identity adapter, against the machine actually running.

These are the only tests in the suite that call `kernel32`. Everything
else in the device pipeline goes through `StubVolumeIdentityProvider`, so
this file is where the claim "the operating system will tell us a stable
name for a volume" is either true or it is not.

They therefore assert on *properties* rather than on values. Which volume
GUID this machine's `C:` has is not knowable in advance and is not the
point; that asking twice gives the same answer, that it survives being
asked about a nested path, and that it is not the drive letter -- those
are the whole of RFC-027 section 4.1, and they are checkable anywhere.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

from app.domain.value_objects.device_id import VolumeIdentity, VolumeKind
from app.infrastructure.filesystem.volume_identity_provider import (
    ResolvedVolume,
    VolumeIdentityError,
    VolumeIdentityProvider,
    WindowsVolumeIdentityProvider,
)

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="Only the Windows adapter ships (RFC-027 section 4.1)",
)

VOLUME_GUID_NAME = re.compile(
    r"^\\\\\?\\Volume\{[0-9a-fA-F-]{36}\}\\$",
)
r"""The exact shape of `\\?\Volume{GUID}\`.

Pinned because the string is a persisted key. A change in its shape is a
change in every `DeviceId` derived from it, so it must not be able to
drift silently -- a Windows API that started returning the name without
its trailing separator would produce a new identity for every disk, and
every image on them would look new.
"""


@pytest.fixture()
def provider() -> WindowsVolumeIdentityProvider:
    return WindowsVolumeIdentityProvider()


def test_the_adapter_implements_the_port() -> None:
    assert issubclass(WindowsVolumeIdentityProvider, VolumeIdentityProvider)


def test_resolving_a_real_path_returns_a_volume_guid_name(
    provider: WindowsVolumeIdentityProvider, tmp_path: Path
) -> None:
    resolved = provider.resolve(tmp_path)

    assert isinstance(resolved, ResolvedVolume)
    assert VOLUME_GUID_NAME.match(resolved.identity.value), resolved.identity.value
    assert resolved.identity.kind is VolumeKind.WINDOWS_VOLUME_GUID


def test_the_identity_is_not_the_drive_letter(
    provider: WindowsVolumeIdentityProvider, tmp_path: Path
) -> None:
    """The rejected candidate, checked rather than assumed (RFC-027 4.1).

    An adapter that fell back to the letter when the GUID lookup failed
    would be indistinguishable from a working one on the machine that
    wrote it, and would silently rebuild the bug on the first remount.
    """
    resolved = provider.resolve(tmp_path)

    assert resolved.identity.value != str(tmp_path.drive)
    assert resolved.identity.value != str(tmp_path.anchor)


def test_the_same_volume_resolves_identically_from_two_paths_on_it(
    provider: WindowsVolumeIdentityProvider, tmp_path: Path
) -> None:
    """Two files on one disk are on one device, however deep they sit."""
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)

    assert provider.resolve(tmp_path).identity == provider.resolve(nested).identity


def test_resolving_is_repeatable(
    provider: WindowsVolumeIdentityProvider, tmp_path: Path
) -> None:
    assert provider.resolve(tmp_path).identity == provider.resolve(tmp_path).identity


def test_the_mount_point_is_a_prefix_of_the_resolved_path(
    provider: WindowsVolumeIdentityProvider, tmp_path: Path
) -> None:
    """`relative_to()` in the worker depends on exactly this."""
    resolved = provider.resolve(tmp_path)

    assert tmp_path.is_relative_to(resolved.mount_point)


def test_the_mount_point_is_returned_rather_than_stored(
    provider: WindowsVolumeIdentityProvider, tmp_path: Path
) -> None:
    """It travels in a return value because it may not be written down.

    `ResolvedVolume` is the only type in the system carrying a mount
    point, and it is not persisted anywhere -- `Device` has no such field
    and `devices` has no such column.
    """
    resolved = provider.resolve(tmp_path)

    assert isinstance(resolved.mount_point, Path)
    assert not hasattr(resolved.identity, "mount_point")


def test_capacity_and_label_come_back_typed(
    provider: WindowsVolumeIdentityProvider, tmp_path: Path
) -> None:
    """Both are informative, and both are allowed to be absent.

    An unlabelled volume is normal -- most are -- and reports `None`
    rather than `""`, so "no label" is one value instead of two.
    """
    resolved = provider.resolve(tmp_path)

    assert resolved.total_bytes is None or resolved.total_bytes > 0
    assert resolved.filesystem_label is None or resolved.filesystem_label != ""


def test_resolving_a_path_on_no_volume_raises(
    provider: WindowsVolumeIdentityProvider,
) -> None:
    """No fallback to a letter, and no `None` (RFC-027 section 4.1).

    A caller is about to key a whole disk's photos on the answer, so
    guessing one would file them under the wrong device -- which is worse
    than failing, and much harder to notice.
    """
    with pytest.raises(VolumeIdentityError):
        provider.resolve(Path(r"\\no-such-server\no-such-share\file.jpg"))


def test_mounted_volumes_reports_at_least_the_system_drive(
    provider: WindowsVolumeIdentityProvider,
) -> None:
    mounted = provider.mounted_volumes()

    assert mounted
    assert all(isinstance(key, VolumeIdentity) for key in mounted)
    assert all(isinstance(value, Path) for value in mounted.values())


def test_a_resolved_volume_is_reported_as_mounted(
    provider: WindowsVolumeIdentityProvider, tmp_path: Path
) -> None:
    """The connection check of RFC-027 section 7, in one assertion.

    A device is connected exactly when its stored `volume_identity` is a
    key of this mapping. Nothing is persisted to answer that, and nothing
    can be: the answer changes when somebody pulls a cable, and Windows
    does not tell this process when they do.
    """
    resolved = provider.resolve(tmp_path)

    assert resolved.identity in provider.mounted_volumes()


def test_an_unknown_volume_is_reported_as_not_connected(
    provider: WindowsVolumeIdentityProvider,
) -> None:
    absent = VolumeIdentity(
        value="\\\\?\\Volume{deadbeef-0000-0000-0000-000000000000}\\",
        kind=VolumeKind.WINDOWS_VOLUME_GUID,
    )

    assert absent not in provider.mounted_volumes()


def test_enumeration_is_repeatable_while_nothing_is_plugged_or_pulled(
    provider: WindowsVolumeIdentityProvider,
) -> None:
    assert provider.mounted_volumes() == provider.mounted_volumes()
