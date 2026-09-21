"""`MountedVolumeCatalog`, the adapter behind the RFC-031 volume port.

Three things are worth holding here, and each of them is a decision
recorded in RFC-031 section 4.1 rather than an implementation detail:

* the two methods **cost differently**, and the cheap one must stay
  cheap. `mount_points()` is on the search path through
  `ResolveImageLocationUseCase`; `list_mounted()` reads a filesystem
  label and a capacity per volume, and reading a capacity can spin up an
  external disk that had parked its heads. A catalogue that answered
  `mount_points()` by calling `resolve()` would look identical from
  outside and put seconds into every query in the product;
* the identity in a `MountedVolume` is **the enumeration's**, so that a
  key from `mount_points()` and an `identity` from `list_mounted()`
  compare equal -- which is the whole mechanism by which `GET /volumes`
  knows a volume is already a device;
* a volume that will not describe itself is **skipped**, not raised on,
  exactly as `mounted_volumes()` skips one that will not be named
  (RFC-027 section 7).
"""

from __future__ import annotations

from pathlib import Path

from app.domain.value_objects.device_id import VolumeIdentity, VolumeKind
from app.infrastructure.filesystem.volume_catalog import (
    MountedVolumeCatalog,
    as_mounted_volume,
)
from app.infrastructure.filesystem.volume_identity_provider import (
    ResolvedVolume,
    VolumeIdentityError,
    VolumeIdentityProvider,
)


def identity(suffix: int) -> VolumeIdentity:
    return VolumeIdentity(
        value=f"\\\\?\\Volume{{00000000-0000-0000-0000-{suffix:012d}}}\\",
        kind=VolumeKind.WINDOWS_VOLUME_GUID,
    )


class RecordingProvider(VolumeIdentityProvider):
    """Answers about invented volumes, and counts what it was asked.

    `resolve()` is the expensive call in production -- two `kernel32`
    round trips per volume, one of which can wake a sleeping disk -- so
    counting it is the only way to tell the cheap method from the
    expensive one, which return different types but would be
    indistinguishable in behaviour otherwise.
    """

    def __init__(
        self,
        mounted: dict[VolumeIdentity, Path],
        undescribable: frozenset[VolumeIdentity] = frozenset(),
    ) -> None:
        self._mounted = mounted
        self._undescribable = undescribable
        self.mounted_volumes_calls = 0
        self.resolve_calls: list[Path] = []

    def resolve(self, path: Path) -> ResolvedVolume:
        self.resolve_calls.append(path)
        for known, mount_point in self._mounted.items():
            if mount_point == path:
                if known in self._undescribable:
                    raise VolumeIdentityError(f"{path} will not describe itself")
                return ResolvedVolume(
                    identity=known,
                    mount_point=mount_point,
                    filesystem_label=f"LABEL-{mount_point.drive or mount_point.name}",
                    total_bytes=1_000,
                )
        raise VolumeIdentityError(f"no volume at {path}")

    def mounted_volumes(self) -> dict[VolumeIdentity, Path]:
        self.mounted_volumes_calls += 1
        return dict(self._mounted)


def test_mount_points_is_the_enumeration_and_nothing_more() -> None:
    """One enumeration, no per-volume call: the hot path stays cheap."""
    provider = RecordingProvider({identity(1): Path("D:/"), identity(2): Path("F:/")})

    mounted = MountedVolumeCatalog(provider).mount_points()

    assert mounted == {identity(1): Path("D:/"), identity(2): Path("F:/")}
    assert provider.mounted_volumes_calls == 1
    assert provider.resolve_calls == []


def test_list_mounted_pays_one_identification_per_volume() -> None:
    """The cost that justifies there being two methods (RFC-031 section 4.1)."""
    provider = RecordingProvider({identity(1): Path("D:/"), identity(2): Path("F:/")})

    volumes = MountedVolumeCatalog(provider).list_mounted()

    assert len(volumes) == 2
    assert provider.resolve_calls == [Path("D:/"), Path("F:/")]


def test_list_mounted_carries_the_label_and_the_capacity() -> None:
    provider = RecordingProvider({identity(1): Path("D:/")})

    (volume,) = MountedVolumeCatalog(provider).list_mounted()

    assert volume.mount_point == Path("D:/")
    assert volume.filesystem_label == "LABEL-D:"
    assert volume.total_bytes == 1_000


def test_the_identity_is_the_enumeration_s_so_the_two_methods_agree() -> None:
    """What lets `GET /volumes` match a volume to a registered device."""
    provider = RecordingProvider({identity(1): Path("D:/")})
    catalog = MountedVolumeCatalog(provider)

    (volume,) = catalog.list_mounted()

    assert volume.identity in catalog.mount_points()


def test_a_volume_that_will_not_describe_itself_is_skipped() -> None:
    """RFC-027 section 7: one silent disk is no reason to report none of them."""
    provider = RecordingProvider(
        {identity(1): Path("D:/"), identity(2): Path("F:/")},
        undescribable=frozenset({identity(1)}),
    )

    volumes = MountedVolumeCatalog(provider).list_mounted()

    assert [volume.identity for volume in volumes] == [identity(2)]


def test_nothing_mounted_is_an_empty_answer_rather_than_an_error() -> None:
    catalog = MountedVolumeCatalog(RecordingProvider({}))

    assert catalog.mount_points() == {}
    assert catalog.list_mounted() == []


def test_nothing_is_cached_between_calls() -> None:
    """The rule both RFC-031 ports inherit from `DeviceLocator`.

    A cached answer is wrong from the moment a cable is pulled and
    nothing notifies this process, so every call must enumerate again.
    """
    provider = RecordingProvider({identity(1): Path("D:/")})
    catalog = MountedVolumeCatalog(provider)

    catalog.mount_points()
    catalog.mount_points()
    catalog.list_mounted()

    assert provider.mounted_volumes_calls == 3


def test_as_mounted_volume_translates_every_field() -> None:
    """The translation the CLI uses to reach the same Application use case."""
    resolved = ResolvedVolume(
        identity=identity(1),
        mount_point=Path("D:/"),
        filesystem_label="Seagate Backup",
        total_bytes=2_000_398_934_016,
    )

    volume = as_mounted_volume(resolved)

    assert volume.identity == resolved.identity
    assert volume.mount_point == resolved.mount_point
    assert volume.filesystem_label == resolved.filesystem_label
    assert volume.total_bytes == resolved.total_bytes
