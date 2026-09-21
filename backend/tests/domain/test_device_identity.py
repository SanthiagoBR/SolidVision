"""`compute_device_id()`, now that it is a Domain function (RFC-031 section 4.2).

These cases lived in `tests/infrastructure/filesystem/test_image_identity.py`
until RFC-031 moved the function they cover into
`app/domain/services/device_identity.py`. They moved with it rather than
being left behind: a test that describes Domain behaviour from an
Infrastructure directory is the kind of thing nobody finds when the
Domain changes.

What stayed there is everything about `compute_image_id()`, which did
**not** move -- it is not needed above Infrastructure, and refactoring it
"for symmetry" would be refactoring the identity of every image row in
the database.
"""

from __future__ import annotations

from tests.conftest import TEST_DEVICE_ID, TEST_VOLUME_IDENTITY

from app.domain.services.device_identity import (
    SOLIDVISION_VOLUME_NAMESPACE,
    compute_device_id,
)
from app.domain.value_objects.device_id import DeviceId, VolumeIdentity, VolumeKind


def test_volume_namespace_constant_is_frozen_to_its_expected_value() -> None:
    """Regenerating it orphans every image row; the move must not have touched it.

    The constant travelled from Infrastructure to the Domain with RFC-031,
    and this assertion is the whole reason that move is safe to make:
    `ImageId` is `uuid5` over `f"{device_id}/{relative_path}"`, so a
    namespace that changed in transit would silently re-key every photo
    in the database. If this fails because the literal changed, that
    change was not authorised by this suite.
    """
    assert str(SOLIDVISION_VOLUME_NAMESPACE) == "6a1f4c07-9f0c-4a2e-9d84-0f3f0d5c4b91"


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
