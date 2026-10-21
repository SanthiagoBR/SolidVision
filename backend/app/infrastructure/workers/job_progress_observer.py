"""The adapter that turns pipeline callbacks into job rows (RFC-029).

`IndexingObserver` is a Domain port that knows nothing about jobs. This is
the half that knows about nothing else: it holds a job id, writes the
counters, keeps the heartbeat moving, and reads back whether somebody has
asked the run to stop.

Two policies live here and nowhere else.

**When to write.** The end of a prefetch window and the end of a batch are
written every time -- that is already at most one write per 512 files or
per 8 images, which is the rate RFC-029 section 7.3 chose. The scan
callback fires per file, so that one is rate-limited by time.

**When to stop.** `should_stop()` never queries: it answers from what the
last write read back. That works because the write happens at every batch
and every window, which is exactly where RFC-029 section 8 says the flag
should be observed, so the answer is never more than one batch old.

And one relay that writes nothing here: the size of the listing of a whole
disk, which `listed()` hands to whatever function the runner gave it --
the runner knows the device and where it was mounted, and this object
knows neither (RFC-039 section 4.1).
"""

from __future__ import annotations

import datetime
import time
from collections.abc import Callable

from app.domain.repositories.indexing_job_repository import IndexingJobRepository
from app.domain.services.indexing_observer import IndexingObserver
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.indexing_progress import IndexingProgress
from app.domain.value_objects.job_id import JobId
from app.infrastructure.logging.logger import get_logger

logger = get_logger(__name__)


class JobProgressObserver(IndexingObserver):
    """Reports one run's progress onto its job row, and relays cancellation."""

    def __init__(
        self,
        repository: IndexingJobRepository,
        job_id: JobId,
        clock: Callable[[], datetime.datetime],
        heartbeat_interval: float,
    ) -> None:
        self._repository = repository
        self._job_id = job_id
        self._clock = clock
        self._heartbeat_interval = heartbeat_interval

        self._scanned = 0
        self._scan_complete = False
        self._pipeline = IndexingProgress()
        self._checkpoint: ImagePath | None = None
        self._last_write = 0.0
        self._on_listed: Callable[[int], None] | None = None

        self.cancelled = False
        """Whether the route asked this job to stop."""

        self.lost = False
        """Whether this job stopped being ours -- the reaper took it back.

        Distinct from `cancelled` because the outcomes differ: a cancelled
        job is ours to mark `cancelled`, and a lost one must not be
        written at all, because another worker may already be running it.
        """

    def discovered(self, files_seen: int, complete: bool) -> None:
        """Record how far the scan has got, at most every heartbeat interval.

        The callback that keeps a job alive when nothing else is
        happening: a re-scan of an indexed disk completes no batches, and
        a resume skips most of its files here without yielding any
        (RFC-029 section 9.1). Rate-limited because it fires per file and
        a write per file would be 40,000 writes to watch 40,000 files.

        `complete` always writes: it is the moment `discovered_files`
        stops being a running count and becomes a total, which is the one
        thing a client needs to render a percentage instead of "scanning
        N files".
        """
        self._scanned = files_seen
        self._scan_complete = complete
        if complete or self._heartbeat_is_due():
            self._write()

    def relay_listing_to(self, record: Callable[[int], None]) -> None:
        """Send the size of the whole disk's listing to `record`.

        Given by the runner once it has resolved the device and its mount
        point, which is after this object exists: the runner builds the
        observer first so that every way out of a run can read `lost`.
        Only a job without scopes is given one, and only such a job's scan
        calls `listed()`.
        """
        self._on_listed = record

    def listed(self, file_count: int) -> None:
        """Pass the denominator on, and swallow whatever goes wrong with it.

        **Never raises**, for the reason `_write()` gives: this runs
        inside the discovery generator, and an exception there would close
        it and truncate the scan in silence. A disk whose percentage
        could not be recorded is a disk without a percentage; a job cut
        short by it would be a job that calls itself complete with half
        the disk unread.
        """
        record = self._on_listed
        if record is None:
            return
        try:
            record(file_count)
        except Exception:
            logger.warning(
                "Could not record the scan of job %s's device; the run continues",
                self._job_id,
                exc_info=True,
            )

    def window_decided(
        self, progress: IndexingProgress, durable_through: ImagePath | None
    ) -> None:
        self._record(progress, durable_through)

    def batch_persisted(
        self, progress: IndexingProgress, durable_through: ImagePath | None
    ) -> None:
        self._record(progress, durable_through)

    def should_stop(self) -> bool:
        """Whether this run should wind up at the next safe point."""
        return self.cancelled or self.lost

    def _record(
        self, progress: IndexingProgress, durable_through: ImagePath | None
    ) -> None:
        """Take the pipeline's counters and checkpoint, and write them out.

        A `durable_through` of `None` is kept rather than stored: it means
        the batch buffer still holds the oldest undecided file, so the
        previous checkpoint is the furthest point that is actually safe to
        resume from. Overwriting it with `None` would throw away a
        position a crash still needs.
        """
        self._pipeline = progress
        if durable_through is not None:
            self._checkpoint = durable_through
        self._write()

    def _heartbeat_is_due(self) -> bool:
        return (time.monotonic() - self._last_write) >= self._heartbeat_interval

    def _merged(self) -> IndexingProgress:
        """Combine what the scan knows with what the pipeline knows.

        Two sources, because neither has the whole picture. The scan is
        the only thing that knows how many files exist and whether it has
        finished looking; the pipeline is the only thing that knows how
        many of them were embedded, skipped or failed. The scan always
        runs ahead of the pipeline, so its count is the larger of the two
        -- but `max` rather than a bare assignment, because a counter a
        client is watching must never go backwards.
        """
        return IndexingProgress(
            discovered_files=max(self._scanned, self._pipeline.discovered_files),
            processed_images=self._pipeline.processed_images,
            skipped_images=self._pipeline.skipped_images,
            failed_images=self._pipeline.failed_images,
            discovery_complete=self._scan_complete,
        )

    def _write(self) -> None:
        """Write progress, and learn from the same call whether to stop.

        **Never raises**, and the reason is specific rather than general
        caution: these callbacks run inside the lazy consumption of the
        discovery generator, and an exception raised there closes the
        generator and truncates the scan in silence -- the same failure
        `discovered_candidates()` already documents. A job that cannot
        report its progress should finish and be slightly out of date, not
        stop half way and call itself complete.
        """
        self._last_write = time.monotonic()
        try:
            stored = self._repository.record_progress(
                self._job_id, self._merged(), self._checkpoint, self._clock()
            )
        except Exception:
            logger.warning(
                "Could not record progress for job %s; the run continues",
                self._job_id,
                exc_info=True,
            )
            return

        if stored is None:
            # The row is no longer `running`: the reaper decided this
            # worker was dead and put the job back in the queue, or
            # finished it. Another worker may already have it.
            self.lost = True
            logger.warning(
                "Job %s is no longer assigned to this worker; winding up",
                self._job_id,
            )
            return

        if stored.cancel_requested and not self.cancelled:
            self.cancelled = True
            logger.info("Job %s was asked to stop; finishing the batch", self._job_id)
