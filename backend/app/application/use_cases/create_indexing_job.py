"""Accepting a request to index a disk, or some folders on it (RFC-029 section 7.1).

The use case that turns an HTTP body into a row, and **that is all it
does**. It opens no file, loads no model, and never calls
`IndexOrUpdateImagesUseCase`. The entire difference between this RFC and
the thing RFC-026 section 3 prohibited is that the request returns once
the intention is recorded, and the work happens in another process.

What it does before recording the intention is refuse the four requests
that could never succeed, each with a domain error the HTTP layer already
knows how to answer:

* a device nobody has ever seen -- 404;
* a device that is not plugged in *right now* -- 409, because the same
  request would be accepted with the disk connected;
* a scope that escapes the device, or is not a folder on it -- 400;
* a device that is already busy -- 409, and that one is refused by the
  database rather than here (see below).
"""

from __future__ import annotations

import datetime
from collections.abc import Callable, Iterable

from app.domain.entities.device import Device
from app.domain.entities.indexing_job import IndexingJob
from app.domain.exceptions import DeviceNotConnectedError, DeviceNotFoundError
from app.domain.exceptions.job_errors import InvalidJobScopeError
from app.domain.repositories.device_repository import DeviceRepository
from app.domain.repositories.indexing_job_repository import IndexingJobRepository
from app.domain.services.device_locator import DeviceLocator
from app.domain.value_objects.device_id import DeviceId
from app.domain.value_objects.job_id import JobId
from app.domain.value_objects.job_scope import JobScope, normalize_scopes


class CreateIndexingJobUseCase:
    """Validate a request to index, and queue it."""

    def __init__(
        self,
        job_repository: IndexingJobRepository,
        device_repository: DeviceRepository,
        device_locator: DeviceLocator,
        clock: Callable[[], datetime.datetime] | None = None,
    ) -> None:
        """Compose the use case, optionally with a clock of the caller's choosing.

        `clock` defaults to the wall clock and exists so that a test can
        state what "now" is instead of asserting around it. It is a plain
        callable rather than a port: the Application layer needs the
        current time, not an abstraction over time, and a one-method ABC
        here would be ceremony (`Settings` is injected the same way, as an
        argument rather than an import).
        """
        self._jobs = job_repository
        self._devices = device_repository
        self._locator = device_locator
        self._clock = clock or _utc_now

    def execute(self, device_id: DeviceId, scopes: Iterable[str] = ()) -> IndexingJob:
        """Queue a job for `device_id`, restricted to `scopes`.

        An empty `scopes` means the whole device, which is what zero scope
        rows already mean in the schema (RFC-029 section 5.2).

        The returned job carries the scopes that will *actually* be
        walked, not the ones that were asked for: overlapping requests are
        absorbed, and each is rewritten to the spelling the filesystem
        uses. The route echoes them back in the 202 so a client that asked
        for `2018` and `2018/junho` can see it got one.

        **There is no check for an existing active job here.** That
        refusal belongs to the partial unique index of RFC-029 section 9,
        and asking first would be a check-then-act: two concurrent
        requests for one disk would both find nothing and both insert.
        `IndexingJobRepository.create()` raises `DeviceBusyError` when the
        database refuses.
        """
        device = self._devices.get(device_id)
        if device is None:
            raise DeviceNotFoundError(f"No device with id {device_id}.")

        if self._locator.mount_point(device) is None:
            raise DeviceNotConnectedError(
                f"Device {device.label!r} is not connected. Plug it in and "
                "try again."
            )

        resolved = tuple(self._resolve(device, scope) for scope in scopes)

        return self._jobs.create(
            IndexingJob(
                id=JobId.new(),
                device_id=device.id,
                scopes=normalize_scopes(resolved),
                created_at=self._clock(),
            )
        )

    def _resolve(self, device: Device, raw: str) -> JobScope:
        """Turn one requested scope into the folder the filesystem agrees on.

        Two refusals, in this order, because the order is the guarantee.
        `JobScope` rejects an absolute path, a drive anchor, a UNC share
        and any `..` **without touching the disk** -- so an escaping scope
        is refused whether or not the folder exists, and never gets one
        chance to be resolved as a real path.

        Only then does the locator look: it resolves the scope under the
        mount point, confirms the result is still inside the device -- an
        NTFS junction inside the scope looks like an ordinary folder until
        it is resolved -- and returns the folder's canonical spelling.
        """
        scope = JobScope(raw)
        resolved = self._locator.resolve_scope(device, scope)
        if resolved is None:
            raise InvalidJobScopeError(
                f"{raw!r} is not a folder on device {device.label!r}."
            )
        return resolved


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.UTC)
