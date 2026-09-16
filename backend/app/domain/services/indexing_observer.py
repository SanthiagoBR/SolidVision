"""The seam through which a long indexing run reports in, and is stopped.

`IndexOrUpdateImagesUseCase` had no extension point at all before
RFC-029: it took an iterable, ran until the iterable was exhausted, and
returned counters. Both halves of RFC-029 -- progress while running
(section 7.3) and cooperative cancellation (section 8) -- need one, and
this is it.

**The port says nothing about jobs.** No heartbeat, no database, no
`cancel_requested`, no id. That is invariant 3 of the build prompt in
port form: the incremental decision must not learn that jobs exist, or
"the CLI reindexes things the UI skips" becomes possible. The
Infrastructure adapter is where a `should_stop()` becomes a read of a
flag and a `batch_persisted()` becomes an `UPDATE` at most every N
seconds.

The alternative -- handing `IndexingJobRepository` to the use case -- was
rejected for exactly that reason.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.indexing_progress import IndexingProgress


class IndexingObserver(ABC):
    """Watches an indexing run and may ask it to stop.

    Implementations must not raise. An exception from any method here
    surfaces inside the pipeline, where it would abort a run for a reason
    that has nothing to do with the images -- and when raised during the
    lazy consumption of the discovery generator it closes that generator
    and truncates the scan in silence, which is the failure
    `discovered_candidates()` already documents.
    """

    @abstractmethod
    def discovered(self, files_seen: int, complete: bool) -> None:
        """Report how many files the scan has produced so far.

        Called from inside the discovery stream, which is why it exists
        separately from the two callbacks below: a scan of a cold
        mechanical disk, or a resume that skips everything before its
        checkpoint, can run for minutes without completing a single
        window, and a run that only reported per batch would look dead
        for that whole time (RFC-029 section 9.1).

        `complete` is true exactly once, when the scan is exhausted. Until
        then `files_seen` is a running count and not a total -- there is
        no total, because discovery is a generator (RFC-021) and counting
        ahead would mean reading the disk twice.
        """

    @abstractmethod
    def window_decided(
        self, progress: IndexingProgress, durable_through: ImagePath | None
    ) -> None:
        """Report the end of one metadata-prefetch window.

        The counterpart of `batch_persisted` for the case that produces no
        batches at all: a re-scan of an already-indexed disk decides whole
        windows as `SKIP_UNCHANGED` and never flushes anything. That is
        the most common shape of run there is, and a run that only
        reported on flush would be indistinguishable from a dead one.
        """

    @abstractmethod
    def batch_persisted(
        self, progress: IndexingProgress, durable_through: ImagePath | None
    ) -> None:
        """Report that a batch's rows are committed.

        `durable_through` is the checkpoint, and it is the file up to
        which *everything* is durable -- not the last file written. It can
        move backwards relative to the newest path processed, and it can
        be `None` while the pending buffer holds the oldest undecided
        file. Both are correct: a checkpoint that ran ahead of the buffer
        would silently drop those images on a resume.
        """

    @abstractmethod
    def should_stop(self) -> bool:
        """Whether the run should wind up at the next safe point.

        Consulted between batches and between windows, never inside a
        forward pass. A run that stops flushes what it has already
        inferred and returns normally -- stopping is not a failure, and
        discarding inference that has already been paid for buys nothing
        (RFC-029 section 8).
        """


class NullIndexingObserver(IndexingObserver):
    """The observer for every caller that does not want one.

    The default argument of `IndexOrUpdateImagesUseCase.execute()`, which
    is what lets RFC-024's entire existing test suite keep passing
    untouched: a run with nobody watching behaves exactly as it did
    before RFC-029, and `should_stop()` answering `False` for ever is the
    old unconditional loop.
    """

    def discovered(self, files_seen: int, complete: bool) -> None:
        return None

    def window_decided(
        self, progress: IndexingProgress, durable_through: ImagePath | None
    ) -> None:
        return None

    def batch_persisted(
        self, progress: IndexingProgress, durable_through: ImagePath | None
    ) -> None:
        return None

    def should_stop(self) -> bool:
        return False
