"""Attaching to each device what is only true right now (RFC-031 §4).

Four routes need the same decoration -- `GET /devices` for every disk,
`POST /devices` and `PATCH /devices/{id}` for the one they just touched --
and none of them may compute it:

* not the route, because "is this disk plugged in, how many of its
  photos are indexed, and is something running on it" is exactly the kind
  of rule `AI_Context.md` keeps out of Presentation;
* not `ListDevicesUseCase`, because decorating a device is not listing
  them. Putting it inside would leave the register and rename routes
  either duplicating it or answering `connected: false` for a disk they
  had just found mounted -- which is not a simplification, it is wrong
  data.

So it is its own small use case, on the model of
`ResolveImageLocationUseCase`, down to the `execute()` / `execute_one()`
pair: the plural form is the real one and the singular is its
one-element case, so a caller with one device cannot accidentally take a
cheaper, different path.

**Nothing here is persisted and nothing here is cached.** `mount_point`
lives on the returned summary for the length of one request. The answer
is wrong from the moment the user pulls the cable, and nothing tells this
process when that happens (RFC-027 §7).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from app.domain.entities.device import Device
from app.domain.entities.indexing_job import IndexingJob, JobStatus
from app.domain.repositories.image_repository import ImageRepository
from app.domain.repositories.indexing_job_repository import IndexingJobRepository
from app.domain.services.volume_catalog import VolumeCatalog
from app.domain.value_objects.device_id import DeviceId

ACTIVE_STATUSES: tuple[JobStatus, ...] = (JobStatus.RUNNING, JobStatus.PENDING)
"""Exactly the states `JobStatus.is_active` names, written as a query plan.

Two `SELECT`s that do not grow with the number of devices, instead of a
filter per device. Listed here rather than derived inline so that
`test_describe_devices.py` can assert this tuple *is*
`{status for status in JobStatus if status.is_active}` -- the set is
shared with the partial unique index of RFC-029 §9, and a state added to
one and not the other is drift nothing else would catch.

Running first. The index allows at most one active job per device, so the
order cannot matter today; it is written down as the honest tie-break
rather than left to whichever query happened to run first.
"""


@dataclass(frozen=True)
class DeviceSummary:
    """One disk, and everything about it that was true when we asked."""

    device: Device

    mount_point: Path | None
    """Where the disk is attached right now, or `None` for one in a drawer.

    Resolved per request and never stored -- the distinction RFC-027 §4
    draws, and the reason `Device` has no such field.
    """

    indexed_images: int
    """How many rows `images` holds for this device.

    One of the three parts of "% indexed". The API returns the parts and
    never the percentage: a `percent_indexed` field would be consumable
    without `last_scan_at` beside it, and the first client to render it
    alone would put back the confidently wrong number ARCHITECTURE.md §15
    forbids (RFC-031 §4.2).
    """

    active_job: IndexingJob | None
    """The `pending` or `running` job holding this disk, if any."""

    @property
    def connected(self) -> bool:
        """Whether the disk was mounted when this summary was built.

        Derived from `mount_point` rather than stored beside it, so the
        two cannot disagree: "connected with nowhere to be" is not a
        state this type can represent.
        """
        return self.mount_point is not None


class DescribeDevicesUseCase:
    """Resolve, for a set of devices, everything that is only true now."""

    def __init__(
        self,
        image_repository: ImageRepository,
        job_repository: IndexingJobRepository,
        volume_catalog: VolumeCatalog,
    ) -> None:
        self._images = image_repository
        self._jobs = job_repository
        self._catalog = volume_catalog

    def execute(self, devices: Sequence[Device]) -> list[DeviceSummary]:
        """Return one summary per device, in the order given.

        **The filesystem is enumerated exactly once, whatever N is**, and
        so is each of the queries. That bound is the design rather than a
        tuning choice (RFC-031 §4.1): a device list has N *distinct*
        devices by definition, so the grouping RFC-030 blessed one layer
        over -- `ResolveImageLocationUseCase` asking the locator once per
        distinct disk on a search page -- degenerates into N enumerations
        of the same filesystem here. `DeviceLocator` is therefore not
        used at all: every method on it takes one `Device`.

        The same trap sits under the other two columns and is easier to
        miss. A `COUNT(*)` per device is N queries, and
        `IndexingJobRepository.list(device_id=...)` per device is N more.
        Both are answered by asking for everything once and grouping in
        memory.

        The order and length of `devices` are preserved exactly, so a
        caller that sorted them keeps its order -- and so that a caller
        holding one device gets one summary back.
        """
        mounted = self._catalog.mount_points()
        indexed = self._images.count_by_device()
        active = self._active_jobs()

        return [
            DeviceSummary(
                device=device,
                mount_point=mounted.get(device.volume_identity),
                indexed_images=indexed.get(device.id, 0),
                active_job=active.get(device.id),
            )
            for device in devices
        ]

    def execute_one(self, device: Device) -> DeviceSummary:
        """Describe a single device; the one-element case of `execute()`."""
        (summary,) = self.execute([device])
        return summary

    def _active_jobs(self) -> dict[DeviceId, IndexingJob]:
        """Map each busy device to the job holding it, in two queries.

        The partial unique index of RFC-029 §9 allows at most one active
        job per device, so grouping in memory has no ambiguity to resolve
        and `setdefault()` never actually discards anything -- it is
        there so that a state added to `is_active` later degrades to "the
        first one found" instead of to a crash.

        **No method was added to `IndexingJobRepository` for this.** The
        status filter it already publishes answers the question; a port
        grows for a question it cannot answer, not for one that could be
        phrased more neatly.
        """
        active: dict[DeviceId, IndexingJob] = {}
        for status in ACTIVE_STATUSES:
            for job in self._jobs.list(status=status):
                active.setdefault(job.device_id, job)
        return active
