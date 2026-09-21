r"""One level of a device tree, and what we know of each folder (RFC-031 §8).

The screen where a user picks what to index. It returns the folders
directly inside one path, each with an exact count of indexed images and
one of five words describing its state -- and it returns **what the
client will send back**, because what this lists becomes `scopes[]` on
the next `POST /api/v1/jobs`.

That last sentence is the constraint the whole module is built around,
and it has two consequences:

* **the same validation `POST /jobs` applies, applied here.** `JobScope`
  refuses `..`, a drive anchor and a UNC share without touching the disk;
  `DeviceLocator.resolve_scope()` then confirms the result is genuinely
  inside the device after NTFS junctions are resolved. A folder that were
  listable but not scopable would be a dead end in the interface
  (RFC-031 section 8);
* **the paths echoed back are the filesystem's spelling, never the
  client's.** `resolve_scope()` returns the canonical one, which is half
  the reason it exists -- Windows is case-insensitive while
  `normalize_scopes()` compares parts exactly, so a client that asked for
  `FOTOS/2018` and got its own spelling back would send it to `POST
  /jobs` and have it rewritten into something else.

**One level per request, not the whole tree and not a flat list.** A flat
first level turns "index the wedding" into "index all of 2018", which is
five hours of inference to reach three hundred photos; the whole tree
costs the size of the disk every time the screen opens, to draw four rows
(RFC-031 section 8.1).

**No percentage anywhere, and no count of files on disk.** There is no
denominator per folder and obtaining one means walking the subtree, which
is the read RFC-029 section 7.2 measured as dominant on a cold mechanical
disk. `indexed_images` is an exact fact about our own table instead:
*"1,204 indexed"* is true, *"30% indexed"* has nothing to be 30% of
(RFC-031 section 2.3).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.entities.device import Device
from app.domain.entities.indexing_job import IndexingJob, JobStatus
from app.domain.exceptions import DeviceNotConnectedError, DeviceNotFoundError
from app.domain.exceptions.job_errors import InvalidJobScopeError
from app.domain.repositories.device_repository import DeviceRepository
from app.domain.repositories.image_repository import ImageRepository
from app.domain.repositories.indexing_job_repository import IndexingJobRepository
from app.domain.services.device_locator import DeviceLocator
from app.domain.value_objects.device_id import DeviceId
from app.domain.value_objects.folder_state import FolderState
from app.domain.value_objects.job_scope import JobScope


@dataclass(frozen=True)
class FolderListing:
    """One folder, as a row in the picker."""

    scope: JobScope
    """The folder as a device-relative scope, in the filesystem's spelling.

    A `JobScope` rather than a string because that is what the client is
    going to send back, and because it is what `contains()` compares --
    in parts, not in text, so `2018` is not a prefix of `2018b`.
    """

    has_children: bool
    """Whether this folder holds folders of its own.

    Read from the disk by the locator, which pays one extra `readdir`
    per row for it. Without it the UI draws an expand arrow on a leaf and
    the user finds the mistake by clicking (RFC-031 section 4.4 of the
    build prompt).
    """

    indexed_images: int
    """Rows in `images` anywhere beneath this folder. Exact, and not a ratio."""

    state: FolderState
    job: IndexingJob | None
    """The job the state was read from, or `None` when no job explains it.

    Travels with the row because the interesting states are the ones
    where the job carries the detail: a cancelled job's
    `last_processed_relative_path` is *where it stopped*, which is what
    RFC-031 section 12 puts in place of the mockup's invented
    percentage.
    """

    @property
    def name(self) -> str:
        """The folder's own name -- the last part of its scope."""
        return self.scope.parts[-1]


@dataclass(frozen=True)
class DeviceFolders:
    """One level of one device's tree: where we are, and what is here."""

    device: Device
    scope: JobScope
    """The folder that was listed, canonically spelled. Empty is the device root."""

    folders: list[FolderListing]

    @property
    def parent(self) -> JobScope:
        """One level up, and the root's parent is the root.

        Derived from `parts` rather than by trimming text, for the reason
        `JobScope` holds parts at all: string surgery on a path is how
        `2018` becomes a prefix of `2018b`.
        """
        return JobScope(self.scope.parts[:-1])


class ListDeviceFoldersUseCase:
    """List one level of a device's folders, with each folder's indexing state."""

    def __init__(
        self,
        device_repository: DeviceRepository,
        image_repository: ImageRepository,
        job_repository: IndexingJobRepository,
        device_locator: DeviceLocator,
    ) -> None:
        self._devices = device_repository
        self._images = image_repository
        self._jobs = job_repository
        self._locator = device_locator

    def execute(self, device_id: DeviceId, path: str = "") -> DeviceFolders:
        """List the folders inside `path` on `device_id`.

        The four refusals, in this order, each with a domain error the
        HTTP layer already knows how to answer:

        * a device nobody has ever seen -- 404;
        * a device that is not plugged in *right now* -- 409, and the
          message names the disk. Unlike the rename route this one needs
          bytes, and *which disk to plug in* is more useful than being
          told the folder is not there;
        * a scope that escapes the device, or is not a folder on it --
          400, refused by `JobScope` before the disk is touched and then
          by the locator after junctions are resolved;
        * a folder that vanished between being resolved and being read --
          400 as well, by the same rule: it is not a folder on this
          device.

        `path` absent, empty or `.` all list the device root, because
        that is what `JobScope("")` already decides -- `PurePosixPath`
        drops `.` during normalisation. There is deliberately no second
        handling of it here.

        Cost: one `SELECT` for the device, three enumerations of the
        mounted volumes (connection, resolution, listing -- 0.19 ms
        each), one `readdir` per folder plus one for the level itself,
        **one** grouped count query for every folder at once, and one
        `SELECT` for the job history.
        """
        device = self._devices.get(device_id)
        if device is None:
            raise DeviceNotFoundError(f"No device with id {device_id}.")

        if self._locator.mount_point(device) is None:
            raise DeviceNotConnectedError(
                f"Device {device.label!r} is not connected. Plug it in and "
                "try again."
            )

        resolved = self._locator.resolve_scope(device, JobScope(path))
        if resolved is None:
            raise InvalidJobScopeError(
                f"{path!r} is not a folder on device {device.label!r}."
            )

        entries = self._locator.list_folders(device, resolved)
        if entries is None:
            raise InvalidJobScopeError(
                f"{path!r} is not a folder on device {device.label!r}."
            )

        indexed = self._images.count_by_path_prefixes(device.id, resolved)
        history = self._jobs.list(device_id=device.id)

        folders = [
            self._describe(
                scope=JobScope(resolved.parts + (entry.name,)),
                has_children=entry.has_children,
                # `.get(..., 0)` rather than a lookup: a folder with no
                # indexed image has no group in the count, and reading
                # the mapping directly would either drop the folder or
                # raise. Zero is a real answer here, and it is the
                # answer for most folders on a fresh disk.
                indexed_images=indexed.get(entry.name, 0),
                history=history,
            )
            for entry in entries
        ]
        # Sorted by name **here**, in the layer that knows this is being
        # rendered. `scandir` has no order to promise and neither does
        # the port; this is also not the scan order, which
        # `FilesystemImageProvider` establishes with the platform's own
        # path comparison, and the two must not be confused.
        folders.sort(key=lambda folder: folder.name)
        return DeviceFolders(device=device, scope=resolved, folders=folders)

    def _describe(
        self,
        scope: JobScope,
        has_children: bool,
        indexed_images: int,
        history: list[IndexingJob],
    ) -> FolderListing:
        state, job = _state_of(scope, history, indexed_images)
        return FolderListing(
            scope=scope,
            has_children=has_children,
            indexed_images=indexed_images,
            state=state,
            job=job,
        )


def _state_of(
    folder: JobScope, history: list[IndexingJob], indexed_images: int
) -> tuple[FolderState, IndexingJob | None]:
    """Read one folder's state off the device's job history (RFC-031 section 8.2).

    Walks newest first -- which `IndexingJobRepository.list()` guarantees
    by contract -- and stops at the first job that covers the folder at
    all. That is correct rather than merely convenient: at most one job
    per device is active (the partial unique index of RFC-029 section 9),
    and a job can only be created while none is active, so an active job
    is always the newest one. There is no case where an older running job
    should outrank a newer finished one.

    "Covers" is a prefix relation **in both directions**, and the two
    directions mean different things:

    * a job over `2018` contains `2018/janeiro` -- full coverage, so the
      job's own outcome is the folder's state;
    * a job over `2018/janeiro/casamento` sits *inside* `2018/janeiro` --
      partial coverage, so even a `completed` job leaves the folder
      `partial`, which is literally what it is.

    Every comparison goes through `JobScope.contains()`, which compares
    parts. Doing it in text would make `2018` a prefix of `2018b`, which
    is a different folder.

    `scopes == ()` means the whole device and contains everything,
    falling out of the same prefix rule: an empty tuple is a prefix of
    every tuple.

    **No `list_covering()` was added to the job port for this.** RFC-031
    section 13 proposed one; writing it would mean reproducing this
    relation in SQL, in both directions, in two implementations, with the
    comparison done on text -- which is the exact mistake `JobScope`
    exists to prevent. The rule is already written, already tested, and
    already Domain. The declared cost is that a disk's history grows
    without bound and this list grows with it; the mitigation when that
    starts to matter is a `limit` on the port, measured first, not a
    prefix match in SQL (RFC-031 section 4.9 of the build prompt).
    """
    for job in history:
        fully = _covers_fully(job, folder)
        if not (fully or _covers_partially(job, folder)):
            continue
        if job.status is JobStatus.RUNNING:
            return FolderState.INDEXING, job
        if job.status is JobStatus.PENDING:
            return FolderState.QUEUED, job
        if job.status is JobStatus.COMPLETED:
            return (FolderState.INDEXED if fully else FolderState.PARTIAL), job
        # `cancelled` or `failed`: some of it was walked, none of it is
        # claimed to be finished. The job goes back with the row because
        # its checkpoint is *where it stopped*, which is the true and
        # useful thing the mockup's "Parcial · 30%" was reaching for.
        return FolderState.PARTIAL, job

    # No job in the history covers this folder. `never_indexed` requires
    # both halves of RFC-031 section 8.2 -- no covering job **and** no
    # indexed images -- so rows with no job to explain them are
    # `partial`: they exist, the search can already return them, and
    # saying "never indexed" about them would be false.
    if indexed_images == 0:
        return FolderState.NEVER_INDEXED, None
    return FolderState.PARTIAL, None


def _covers_fully(job: IndexingJob, folder: JobScope) -> bool:
    """Whether every file under `folder` is inside one of the job's scopes."""
    if not job.scopes:
        return True
    return any(scope.contains(folder) for scope in job.scopes)


def _covers_partially(job: IndexingJob, folder: JobScope) -> bool:
    """Whether one of the job's scopes is inside `folder` -- some of it, not all."""
    return any(folder.contains(scope) for scope in job.scopes)
