"""Making a volume into a device this system knows (RFC-031 section 6).

The rule that used to live in `infrastructure/workers/indexing_worker.py`
and could only be reached through an `argparse`. A product whose premise
is *"the photographer points at their own disks"* cannot require a
terminal for a disk to exist, and RFC-031 gives the rule a third caller
that is an HTTP route -- at which point the layer stops being debatable.

**What moved, and what deliberately did not.** RFC-031 section 13 says
`register_device()` *"migrates"* here, and the literal migration is
impossible: its signature takes a `VolumeIdentityProvider`, returns a
`ResolvedVolume` and calls `compute_device_id()`, which were three
imports of `app.infrastructure` -- and `test_application_architecture.py`
fails the build on any one of them. So the function was split along the
line that was already there:

* **naming a volume** stays in Infrastructure, because the two callers
  ask the platform differently. The CLI has a path (`--root D:\\fotos`)
  and resolves it; the route has an opaque identity and looks it up in an
  enumeration. Neither question has an answer above the adapter;
* **deciding which row to write** -- the cascade for `label`,
  `first_seen_at` preserved, `last_seen_at` moved, the scan counters left
  alone -- comes here. That is the part the old docstring warned "drifts
  apart in two copies", and it is what RFC-031 wanted in the Application
  layer.

`indexing_worker.register_device()` still exists with the same public
signature, now a shell that resolves a root and calls `register()` below.
Six callers pass `root` and `label` and read a tuple back, and none of
them changed.
"""

from __future__ import annotations

import datetime
from collections.abc import Callable
from dataclasses import dataclass

from app.domain.entities.device import Device
from app.domain.exceptions import (
    DeviceNotConnectedError,
    InvalidVolumeIdentityError,
)
from app.domain.repositories.device_repository import DeviceRepository
from app.domain.services.device_identity import compute_device_id
from app.domain.services.volume_catalog import VolumeCatalog
from app.domain.value_objects.device_id import VolumeIdentity, VolumeKind
from app.domain.value_objects.mounted_volume import MountedVolume


@dataclass(frozen=True)
class RegisteredDevice:
    """A registered device, and whether this call is what created it."""

    device: Device

    created: bool
    """`False` when the identity already had a row -- nothing was created.

    Carried out of the use case rather than decided in the route, because
    the route has no way to know: `save()` is an upsert by design
    (RFC-027), so a second registration and a first are the same write.
    It costs no extra query -- `get_by_volume_identity()` is already
    called to preserve `first_seen_at` -- and it is what lets one route
    answer `201` the first time and `200` the second (RFC-031 section
    6.1).
    """


class RegisterDeviceUseCase:
    """Register a mounted volume, or refresh the device it already is."""

    def __init__(
        self,
        volume_catalog: VolumeCatalog,
        device_repository: DeviceRepository,
        clock: Callable[[], datetime.datetime] | None = None,
    ) -> None:
        """Compose the use case, optionally with a clock of the caller's choosing.

        `clock` follows `CreateIndexingJobUseCase`: a plain callable
        rather than a port, so a test can state what "now" is instead of
        asserting around it. The Application layer needs the current
        time, not an abstraction over time.

        `volume_catalog` is required even by the CLI path, which never
        touches it -- `register()` is handed a volume somebody else
        resolved. One object with two entry points is simpler than an
        optional dependency, and constructing the catalogue costs a
        reference: it is a shell over an enumeration it has not performed.
        """
        self._catalog = volume_catalog
        self._devices = device_repository
        self._clock = clock or _utc_now

    def execute(
        self, volume_identity: str, volume_kind: str, label: str = ""
    ) -> RegisteredDevice:
        """Register the mounted volume with this identity; 409 if it is not here.

        **The identity is a key, never a location.** It is compared for
        equality against a mapping the server itself produced moments
        ago; it is never parsed, never joined onto anything, and never
        turned into a path. The `root` the registration needs is the
        mount point *the server enumerated*, which is what preserves the
        property RFC-030 section 5.1 established for `/reveal` while
        still letting a user add a disk (RFC-031 section 5.1). An
        invented identity is therefore not a place -- it resolves to
        nothing at all, which is precisely the difference between this
        design and accepting a path.

        The two strings arrive unconverted because converting them is a
        domain rule with a domain error attached. Declaring
        `volume_kind: VolumeKind` on the request schema would have
        FastAPI refuse an unknown kind with a `422` *before* this method
        runs, and the `400 InvalidVolumeIdentityError` RFC-031 section 6
        specifies would never be raised. It is the argument
        `CreateJobRequestSchema` already makes for keeping `scopes` a
        list of plain strings: what makes a value legal here is a rule
        this layer owns, and answering it in Presentation produces a 422
        where a 400 with a readable message belongs.

        An empty identity needs no check of its own --
        `VolumeIdentity.__post_init__` already refuses it with the same
        error.

        `list_mounted()` rather than `mount_points()`: a new row has to
        record `filesystem_label` and `total_bytes`, and this is the one
        route where paying for them is the point.
        """
        identity = VolumeIdentity(value=volume_identity, kind=_kind(volume_kind))
        for volume in self._catalog.list_mounted():
            if volume.identity == identity:
                return self._merge(volume, label, persist=True)

        raise DeviceNotConnectedError(
            "No volume with that identity is mounted right now. Plug the "
            "disk in and try again."
        )

    def register(
        self, volume: MountedVolume, label: str, persist: bool = True
    ) -> Device:
        """Apply the merge rule to a volume the caller already resolved.

        The CLI's entry point: `indexing_worker.register_device()` asks
        the platform about a `--root` path, which is a question no HTTP
        client can ask, and then arrives here with the answer. Nothing in
        this method touches the operating system.

        `persist=False` computes the device without writing it, which is
        what `device_reconcile --dry-run` needs: a dry run that created a
        row would not be a dry run, and the device id is derived rather
        than allocated, so nothing has to be written for it to be known.
        """
        return self._merge(volume, label, persist).device

    def _merge(
        self, volume: MountedVolume, label: str, persist: bool
    ) -> RegisteredDevice:
        """Fold a mounted volume into the row it should have, and say if it is new.

        The rules about which fields survive an existing row, in one
        place because two copies of them drift and both callers mint
        image ids from the device they produce:

        * `label` prefers what the caller asked for, then what the user
          already named the disk, then the volume's own label, then the
          identity string. A disk already known keeps its name unless
          somebody asked to change it -- renaming is a user's decision,
          not a side effect of indexing;
        * `first_seen_at` is preserved and `last_seen_at` moves on every
          call, which is what makes the pair history rather than state;
        * `last_scan_at` and `last_scan_file_count` are carried over
          untouched. They are the result of a scan, and registering a
          disk is not one -- overwriting them here would reset the
          denominator of the "% indexed" figure to nothing every time
          somebody opened the sidebar (RFC-031 section 4.2).

        `filesystem_label` and `total_bytes` are taken from the volume
        rather than preserved: they are observations, and the freshest
        observation wins.
        """
        now = self._clock()
        existing = self._devices.get_by_volume_identity(volume.identity)

        device = Device(
            id=compute_device_id(volume.identity),
            volume_identity=volume.identity,
            label=(
                label
                or (existing.label if existing else None)
                or volume.filesystem_label
                or volume.identity.value
            ),
            filesystem_label=volume.filesystem_label,
            total_bytes=volume.total_bytes,
            first_seen_at=existing.first_seen_at if existing else now,
            last_seen_at=now,
            last_scan_at=existing.last_scan_at if existing else None,
            last_scan_file_count=existing.last_scan_file_count if existing else None,
        )
        if persist:
            self._devices.save(device)
        return RegisteredDevice(device=device, created=existing is None)


def _kind(value: str) -> VolumeKind:
    """Read a platform discriminator, refusing an unknown one as a domain error.

    `VolumeKind(value)` raises `ValueError`, which no handler dresses up
    and which would surface as a 500. `InvalidVolumeIdentityError` is the
    error RFC-031 section 6 names, it already exists, and it already
    answers 400.
    """
    try:
        return VolumeKind(value)
    except ValueError as exc:
        known = ", ".join(sorted(member.value for member in VolumeKind))
        raise InvalidVolumeIdentityError(
            f"{value!r} is not a volume kind this system knows. Expected one "
            f"of: {known}."
        ) from exc


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.UTC)
