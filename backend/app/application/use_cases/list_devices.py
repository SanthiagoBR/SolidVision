"""Every disk the system knows, ready to be a sidebar (RFC-031 §4).

Two lines of work and one decision. The rows come from
`DeviceRepository.list()`; everything that is only true at this instant
comes from `DescribeDevicesUseCase`, which is where the "one enumeration
whatever N is" guarantee lives and is explained. What is left here is the
order.
"""

from __future__ import annotations

from app.application.use_cases.describe_devices import (
    DescribeDevicesUseCase,
    DeviceSummary,
)
from app.domain.repositories.device_repository import DeviceRepository


class ListDevicesUseCase:
    """List every known device, decorated with what is true right now."""

    def __init__(
        self,
        device_repository: DeviceRepository,
        describer: DescribeDevicesUseCase,
    ) -> None:
        """Compose from the repository and the describer the other routes share.

        Composed rather than self-sufficient, on the model of
        `GetImageDetailsUseCase` taking a `ResolveImageLocationUseCase`:
        the decoration is the same work `POST /devices` and `PATCH
        /devices/{id}` need for one device, and a copy of it here would
        be the copy that stops agreeing.
        """
        self._devices = device_repository
        self._describer = describer

    def execute(self) -> list[DeviceSummary]:
        """Return every device, newest facts attached, sorted for display.

        Sorted by label, because `DeviceRepository.list()` explicitly
        leaves the order to whoever renders the result -- and a sidebar
        whose disks changed places between two reads would be worse than
        one in any fixed order. Case-insensitive, because the user typed
        these names; with the id as a tie-break, so two disks sharing a
        name still come back in a stable order rather than in whatever
        order the database returned them.

        Sorting happens **after** the describing and not inside it, so
        that `DescribeDevicesUseCase` keeps its promise to preserve the
        order it was given -- the promise that lets the same object serve
        a caller holding exactly one device.
        """
        summaries = self._describer.execute(self._devices.list())
        return sorted(
            summaries,
            key=lambda summary: (
                summary.device.label.casefold(),
                str(summary.device.id),
            ),
        )
