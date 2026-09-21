"""Giving a disk the name its owner calls it (RFC-031 section 7).

The only editable thing about a `Device`, and the list of what is not
editable is the interesting half. Everything else is derived (`id`),
observed (`filesystem_label`, `total_bytes`, `last_seen_at`) or the
result of a scan (`last_scan_at`, `last_scan_file_count`). Accepting an
edit to any of them would let a client lie about the world -- and
`last_scan_file_count` in particular is the declared denominator of the
"% indexed" figure, so an editable one is an invented number wearing the
clothes of a measurement (RFC-031 section 4.2).

**The rename never requires the disk.** It is a fact about our table,
not about the volume, and RFC-027 section 2.3 is explicit that an
unplugged disk is still a device the system knows perfectly well. The
describer below is consulted only to *report* whether the disk happens to
be here, and answering "no" is as successful an outcome as answering
"yes".
"""

from __future__ import annotations

import dataclasses

from app.application.use_cases.describe_devices import (
    DescribeDevicesUseCase,
    DeviceSummary,
)
from app.domain.exceptions import DeviceNotFoundError
from app.domain.repositories.device_repository import DeviceRepository
from app.domain.value_objects.device_id import DeviceId


class RenameDeviceUseCase:
    """Change a device's user-facing label, and nothing else about it."""

    def __init__(
        self,
        device_repository: DeviceRepository,
        describer: DescribeDevicesUseCase,
    ) -> None:
        self._devices = device_repository
        self._describer = describer

    def execute(self, device_id: DeviceId, label: str) -> DeviceSummary:
        """Rename the device, or raise `DeviceNotFoundError` for an unknown id.

        Reads the row and writes a copy of it with one field replaced,
        rather than issuing a targeted update. `Device` is frozen, so
        `dataclasses.replace()` *is* the edit; and because it starts from
        the stored row, there is no way for this to carry a stale value
        of anything it did not mean to touch -- which is the failure mode
        a hand-built `Device(...)` here would have.

        `save()` is an upsert keyed on the derived id (RFC-027), so this
        updates the one row rather than creating a second.

        Returns a full summary rather than the bare entity so the route
        can answer in the same shape `GET /devices` uses, and a client
        can drop the response straight into the row it just renamed. The
        alternative -- publishing a device body with `connected` and
        `indexed_images` hardcoded because this route did not look --
        would be a response that is *wrong* rather than merely partial,
        and a client that trusted it would blank out a connected disk.
        """
        device = self._devices.get(device_id)
        if device is None:
            raise DeviceNotFoundError(f"No device with id {device_id}.")

        renamed = dataclasses.replace(device, label=label)
        self._devices.save(renamed)
        return self._describer.execute_one(renamed)
