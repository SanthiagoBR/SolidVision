"""Marking the photos a whole-disk scan no longer finds (RFC-046 section 5.5).

Run by the job runner at the end of a job of the whole disk that completed,
with the disk still where it was listed (section 5.4). It compares the rows
of the device with the files of that listing:

    the row                        the listing                the sweep
    visible                        has its id                 nothing
    visible                        does not have its id       marks it
    visible, inside an unreadable  -- (the folder was not     nothing: protected
    folder                         read)
    marked                         has its id                 unmarks it
    marked                         does not have its id       nothing: keeps its date

**It marks and never deletes** (section 3). A false positive -- a folder
that vanished from the listing for a reason nobody saw -- costs one scan to
undo, without inference; a deletion would cost the vectors. Deleting is the
purge's, by a button.

**The ids come in already minted.** This layer does not know how a file
becomes an id: the job runner mints them from the listing with the function
that mints them during indexing (`listed_identity()`), and hands them over.
"""

from __future__ import annotations

import datetime
from collections.abc import Callable, Sequence, Set
from dataclasses import dataclass

from app.domain.repositories.image_repository import ImageRepository
from app.domain.value_objects.device_id import DeviceId
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.job_scope import JobScope


@dataclass(frozen=True)
class SweepReport:
    """What one sweep did, for the job's log.

    The three numbers of the INFO line (section 5.5, step 5), and, for the
    WARNING per unreadable folder, how many rows each one kept visible.
    """

    marked: int
    """Visible rows not in the listing, marked now."""
    unmarked: int
    """Marked rows in the listing again, unmarked now."""
    protected: int
    """Visible rows not in the listing, kept visible by an unreadable folder."""
    protected_by_folder: tuple[tuple[JobScope, int], ...] = ()
    """Each unreadable folder, in the order given, with the rows it protected."""


class SweepMissingImagesUseCase:
    """Compare a device's rows with a listing of its whole disk; mark the difference."""

    def __init__(
        self,
        repository: ImageRepository,
        clock: Callable[[], datetime.datetime] | None = None,
    ) -> None:
        """`clock` gives the date of the marks; the wall clock by default.

        A callable and not a port, as `CreateIndexingJobUseCase` takes one:
        the job runner hands its own, so a test that moves the job's time
        moves the marks' too.
        """
        self._repository = repository
        self._clock = clock or _utc_now

    def execute(
        self,
        device_id: DeviceId,
        listed: Set[ImageId],
        unreadable: Sequence[JobScope] = (),
    ) -> SweepReport:
        """Mark what is missing and unmark what came back, in one write.

        `listed` are the ids of every file the listing found -- the whole
        disk, including the files before a resume's checkpoint and the ones
        whose indexing failed: a file that is there is not missing (section
        4). `unreadable` are the folders the walk could not read; a row
        inside one is never marked.

        **Inside compares parts, not text** (section 4): `2018` unreadable
        does not protect `2018b/x.jpg`. It is `JobScope.contains()`, the
        rule RFC-029 section 10 set for the scopes of a job.

        The rows are read now, at the end of the job, so that the ones this
        job created are compared too (step 1).
        """
        missing: list[ImageId] = []
        returned: list[ImageId] = []
        protected = [0] * len(unreadable)

        for row in self._repository.list_presence(device_id):
            if row.image_id in listed:
                if row.missing:
                    returned.append(row.image_id)
                continue
            if row.missing:
                continue
            folder = _first_containing(unreadable, row.relative_path)
            if folder is None:
                missing.append(row.image_id)
            else:
                protected[folder] += 1

        if missing or returned:
            self._repository.mark_presence(missing, returned, self._clock())

        return SweepReport(
            marked=len(missing),
            unmarked=len(returned),
            protected=sum(protected),
            protected_by_folder=tuple(zip(unreadable, protected, strict=True)),
        )


def _first_containing(folders: Sequence[JobScope], relative_path: str) -> int | None:
    """The index of the first folder `relative_path` lies inside, or `None`.

    A row's path is turned into parts only when there is a folder to compare
    it with: a sweep with nothing unreadable -- every sweep, nearly -- builds
    no `JobScope` at all.
    """
    if not folders:
        return None
    parts = JobScope(relative_path)
    for index, folder in enumerate(folders):
        if folder.contains(parts):
            return index
    return None


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.UTC)
