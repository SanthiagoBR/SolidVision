from __future__ import annotations

import uuid

from tests.conftest import TEST_DEVICE_ID, TEST_VOLUME_IDENTITY

from app.domain.value_objects.device_id import DeviceId, VolumeIdentity, VolumeKind
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.infrastructure.filesystem.device_identity import (
    SOLIDVISION_VOLUME_NAMESPACE,
    compute_device_id,
)
from app.infrastructure.filesystem.image_identity import (
    SOLIDVISION_PATH_NAMESPACE,
    compute_image_id,
)

OTHER_DEVICE_ID = DeviceId(uuid.UUID(int=1234))


def test_namespace_constant_is_frozen_to_its_expected_value() -> None:
    """Guard against accidental regeneration of the namespace constant.

    Changing this constant would silently change the `ImageId` produced
    for every already-indexed file. If this test fails because the
    constant's literal value changed, that change was not authorized by
    this test suite and must not be merged without an explicit,
    documented migration strategy.

    RFC-027 changed what is *hashed* -- the device plus a device-relative
    path, instead of an absolute path -- and deliberately left the
    namespace alone. If both had changed, an old id could no longer be
    recognised as having come from the old scheme.
    """
    assert str(SOLIDVISION_PATH_NAMESPACE) == "5dc64f53-522e-4303-951e-ee6123b10dd8"


def test_volume_namespace_constant_is_frozen_to_its_expected_value() -> None:
    """The same guard for devices: regenerating it orphans every image row."""
    assert str(SOLIDVISION_VOLUME_NAMESPACE) == "6a1f4c07-9f0c-4a2e-9d84-0f3f0d5c4b91"


def test_same_path_produces_same_id_across_multiple_calls() -> None:
    relative_path = ImagePath("images/example.png")

    first = compute_image_id(TEST_DEVICE_ID, relative_path)
    second = compute_image_id(TEST_DEVICE_ID, relative_path)

    assert first == second


def test_different_paths_produce_different_ids() -> None:
    first = compute_image_id(TEST_DEVICE_ID, ImagePath("images/one.png"))
    second = compute_image_id(TEST_DEVICE_ID, ImagePath("images/two.png"))

    assert first != second


def test_the_same_relative_path_on_two_devices_produces_two_ids() -> None:
    """Two disks holding `fotos/2018/x.JPG` are two photos, not one.

    RFC-027 explicitly does not deduplicate across devices (section 12):
    the same picture on two external drives stays two rows, because they
    are two files a user may need to find in two places.
    """
    relative_path = ImagePath("fotos/2018/DJI_0042.JPG")

    assert compute_image_id(TEST_DEVICE_ID, relative_path) != compute_image_id(
        OTHER_DEVICE_ID, relative_path
    )


def test_backslash_and_forward_slash_paths_produce_the_same_id() -> None:
    windows_style = compute_image_id(TEST_DEVICE_ID, ImagePath("images\\example.png"))
    posix_style = compute_image_id(TEST_DEVICE_ID, ImagePath("images/example.png"))

    assert windows_style == posix_style


def test_compute_image_id_returns_image_id() -> None:
    result = compute_image_id(TEST_DEVICE_ID, ImagePath("images/example.png"))

    assert isinstance(result, ImageId)


def test_a_drive_letter_change_does_not_produce_a_new_id() -> None:
    """The defect RFC-027 section 2.1 exists to fix, as an executable check.

    The same disk mounted as `D:` on Monday and as `F:` on Tuesday used to
    produce two absolute paths, two `uuid5` ids, two rows, and a second
    full run of inference over the whole drive. Nothing about the file
    changed on either day, and nothing about the inputs below changes
    either -- which is the point: the mount point is not one of them.
    """
    monday_root = "D:/fotos"
    tuesday_root = "F:/fotos"
    absolute = "{root}/2018/DJI_0042.JPG"

    monday = _id_from_absolute(absolute.format(root=monday_root), mount="D:")
    tuesday = _id_from_absolute(absolute.format(root=tuesday_root), mount="F:")

    assert monday == tuesday


def _id_from_absolute(absolute_path: str, mount: str) -> ImageId:
    """Derive an id the way the indexing worker does, from a mounted path."""
    relative = ImagePath(absolute_path[len(mount) + 1 :])
    return compute_image_id(TEST_DEVICE_ID, relative)


def test_device_id_is_derived_from_the_volume_identity() -> None:
    assert compute_device_id(TEST_VOLUME_IDENTITY) == TEST_DEVICE_ID


def test_device_id_is_stable_across_calls() -> None:
    first = compute_device_id(TEST_VOLUME_IDENTITY)
    second = compute_device_id(TEST_VOLUME_IDENTITY)

    assert first == second
    assert isinstance(first, DeviceId)


def test_two_volumes_produce_two_device_ids() -> None:
    other = VolumeIdentity(
        value="\\\\?\\Volume{11111111-1111-1111-1111-111111111111}\\",
        kind=VolumeKind.WINDOWS_VOLUME_GUID,
    )

    assert compute_device_id(TEST_VOLUME_IDENTITY) != compute_device_id(other)


def test_the_platform_kind_is_part_of_the_device_id() -> None:
    """Two platforms naming a volume identically still describe two disks.

    This is the whole reason `VolumeKind` is a field rather than a
    comment: without it in the key, an adapter added later could collide
    with the Windows one on a shared string and silently merge two
    people's disks into one row.
    """
    same_string_other_platform = VolumeIdentity(
        value=TEST_VOLUME_IDENTITY.value, kind=VolumeKind.LINUX_FS_UUID
    )

    assert compute_device_id(TEST_VOLUME_IDENTITY) != compute_device_id(
        same_string_other_platform
    )
