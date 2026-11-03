"""A thread that reads each file once, ahead of the indexing pipeline (RFC-045).

The pipeline of RFC-024 is one thread: discover a file, hash it -- which reads
it whole -- then let CLIP decode it, then the thumbnail decode it again, and
only then look at the next one. On the user's external hard disk 59% of that
time is the disk, with the processor idle, and then the processor works with
the disk idle (RFC-045 section 1). This module overlaps the two without
touching the use case: it sits where the candidates come from.

    discovery -> [thread: stat, EXIF, read whole file, SHA-256] -> queue
              -> [pipeline thread: decide, CLIP, thumbnail, write]

**The order is the discovery order**, because the checkpoint is a position in
it (RFC-029 section 10). The queue is first in, first out, and so is the
replay of the scan's progress events, which travel in it between the
candidates.

**The thread reads only what the decision will ask for.** Before it starts,
the job loads, on the main thread, the stored size and mtime of every row of
the device (`ImageRepository.list_change_signals()`); a file whose signals
match is passed on without being read, as the pipeline would skip it -- one
`stat` per file, as before, on a re-scan of an indexed disk. The thread never
touches the database: the session belongs to the pipeline's thread
(section 5.2).

**Nothing that writes runs here.** The scan's `discovered()` and `listed()`
used to be called from inside discovery, and they write the job row and the
device row through the job's session; with discovery in this thread they are
queued instead, and the consumer -- the thread that owns the session -- calls
the real observer as it takes them out (section 5.4).
"""

from __future__ import annotations

import dataclasses
import queue
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

from app.application.use_cases.indexing_plan import (
    FileContent,
    IndexCandidate,
    is_unchanged,
)
from app.domain.services.indexing_observer import IndexingObserver
from app.domain.value_objects.change_signals import ChangeSignals
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.indexing_progress import IndexingProgress
from app.infrastructure.filesystem.sha256_content_hasher import sha256_of
from app.infrastructure.logging.logger import get_logger

logger = get_logger(__name__)

POLL_SECONDS = 0.05
"""How long either side waits on the queue before looking up again.

The consumer looks up to learn that the thread died without a word, and so
that an interrupt reaches it on a platform whose lock waits it cannot cut
short; the thread looks up to learn that it was asked to stop while the queue
was full. Short enough that neither is noticeable, long enough to cost
nothing.
"""

INTERRUPTED_JOIN_SECONDS = 2.0
"""How long an interrupted consumer waits for the thread before leaving it.

Every other way out of the `with` block waits for the thread to end, which
takes at most the read in progress (section 5.1). An interrupt -- Ctrl+C, the
launcher's stop -- is the process ending, and the thread can be inside a
place it cannot be stopped in: the listing of a whole cold disk, a
`sorted()` over an `rglob`, which the pipeline used to be interruptible in.
The thread is a daemon, so the process ends anyway; this bounds what the stop
waits for it.
"""


@dataclass(frozen=True)
class ReadAheadLimits:
    """How far ahead the thread may get, and how many bytes it may hold.

    The three settings of RFC-045 section 5.7 other than the switch, as one
    value, so that a runner either has all of them or reads nothing ahead.
    """

    files: int
    """The size of the queue, in items -- candidates and the scan's events."""
    budget_bytes: int
    """The most bytes read and not yet released, across every file."""
    max_file_bytes: int
    """A larger file is passed on without its bytes, to be read by path."""

    def __post_init__(self) -> None:
        for name in ("files", "budget_bytes", "max_file_bytes"):
            value = getattr(self, name)
            if value < 1:
                raise ValueError(f"{name} must be at least 1, got {value}")


class ByteBudget:
    """The bytes the read-ahead holds, counted from both threads.

    Taken by the reading thread before it reads a file; given back by
    `FileContent.release()` on the pipeline's thread, or by the garbage
    collector -- on whatever thread it runs -- when a content nobody released
    is collected. Hence the lock.
    """

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._held = 0
        self._peak = 0
        self._lock = threading.Lock()

    @property
    def held(self) -> int:
        """Bytes read and not yet given back."""
        with self._lock:
            return self._held

    @property
    def peak(self) -> int:
        """The most bytes held at once, since this budget was made."""
        with self._lock:
            return self._peak

    def try_take(self, size: int) -> bool:
        """Reserve `size` bytes if they fit under the limit; never waits."""
        with self._lock:
            if self._held + size > self._limit:
                return False
            self._held += size
            self._peak = max(self._peak, self._held)
            return True

    def settle(self, reserved: int, actual: int) -> None:
        """Replace a reservation with what the read actually returned.

        A file can change between its `stat` and its read. What is held is
        what was read, since that is what `FileContent` will give back.
        """
        with self._lock:
            self._held += actual - reserved
            self._peak = max(self._peak, self._held)

    def give_back(self, size: int) -> None:
        """Return `size` bytes to the budget."""
        with self._lock:
            self._held -= size


@dataclass
class ReadAheadStats:
    """What the thread did with each candidate, for the report and the tests.

    Written by the reading thread only and read after it ended, so it needs
    no lock.
    """

    read: int = 0
    """Files read whole, hashed and handed over with their bytes."""
    bytes_read: int = 0
    unchanged: int = 0
    """Files the snapshot said were unchanged, passed on without a read."""
    too_large: int = 0
    """Files above `max_file_bytes`, or with no size, left to be read by path."""
    over_budget: int = 0
    """Files that would have crossed `budget_bytes`, left to be read by path."""
    failed: int = 0
    """Reads that raised; the pipeline reads by path, fails alike, and reports it."""


@dataclass(frozen=True)
class _Discovered:
    files_seen: int
    complete: bool


@dataclass(frozen=True)
class _Listed:
    file_count: int


@dataclass(frozen=True)
class _Raised:
    error: BaseException


class _End:
    """The scan ended normally."""


_END = _End()

type _Item = IndexCandidate | _Discovered | _Listed | _Raised | _End


class _QueueingObserver(IndexingObserver):
    """The observer the scan is given in the thread: it queues, and writes nothing.

    Only `discovered()` and `listed()` are ever called by a scan. The other
    three belong to the pipeline, which calls them on the real observer, on
    its own thread; here they do nothing.
    """

    def __init__(self, put: Callable[[_Item], bool]) -> None:
        self._put = put

    def discovered(self, files_seen: int, complete: bool) -> None:
        self._put(_Discovered(files_seen, complete))

    def listed(self, file_count: int) -> None:
        self._put(_Listed(file_count))

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


def read_whole_file(path: Path) -> bytes:
    """The whole file, in one read (RFC-045 section 5.1)."""
    return path.read_bytes()


class ReadAheadCandidates:
    """Candidates read ahead in a thread, consumed in the order they were found.

    A context manager that is also the iterable of candidates:

        with ReadAheadCandidates(source, observer, signals, limits) as candidates:
            summary = use_case.execute(candidates, observer)

    Entering starts the thread, which calls `source` with an observer that
    queues the scan's events, and puts in the queue -- bounded at
    `limits.files` items -- each candidate in order, with its bytes when it
    read them, the scan's events between them, and at the end a marker or the
    exception that ended the scan. Iterating takes them out, hands the events
    to `observer` and the candidates to the caller, and raises that exception
    where it happened.

    **What the thread does not read**, passing the candidate on without
    bytes to be read by its path as before: a file the snapshot says is
    unchanged; a file above `max_file_bytes`; a file that would take the
    bytes held above `budget_bytes` -- without waiting for them to come
    back, because the consumer may be waiting for this thread to fill a
    window (section 5.1); and a file whose read fails, whose error the
    pipeline then meets by path and reports, one place for failures.

    **Leaving the block ends the thread**, however it is left: the stop is
    signalled, the queue drained so a blocked `put` returns, and the thread
    awaited -- it looks at the signal between files, so the wait is at most
    the read in progress. An interrupt waits at most
    `INTERRUPTED_JOIN_SECONDS` (see there). Bytes still queued are released.
    """

    def __init__(
        self,
        source: Callable[[IndexingObserver], Iterable[IndexCandidate]],
        observer: IndexingObserver,
        change_signals: Mapping[ImageId, ChangeSignals],
        limits: ReadAheadLimits,
        read_file: Callable[[Path], bytes] = read_whole_file,
    ) -> None:
        """Prepare, without starting anything.

        `source` builds the scan with the observer it is given -- the one
        that queues -- and is called in the thread. `observer` is the real
        one, called only from the thread that iterates. `read_file` is a
        seam for the tests, which need a read that blocks or fails.
        """
        self._source = source
        self._observer = observer
        self._signals = change_signals
        self._limits = limits
        self._read_file = read_file
        self._queue: queue.Queue[_Item] = queue.Queue(maxsize=limits.files)
        self._stopping = threading.Event()
        self._relay = _QueueingObserver(self._put)
        self._thread: threading.Thread | None = None
        self.budget = ByteBudget(limits.budget_bytes)
        self.stats = ReadAheadStats()

    # ------------------------------------------------------------------
    # The consumer's side

    def __enter__(self) -> ReadAheadCandidates:
        if self._thread is not None:
            raise RuntimeError("A ReadAheadCandidates can be entered only once")
        self._thread = threading.Thread(
            target=self._read_ahead, name="solidvision-read-ahead", daemon=True
        )
        self._thread.start()
        return self

    def __iter__(self) -> Iterator[IndexCandidate]:
        if self._thread is None:
            raise RuntimeError("Iterate a ReadAheadCandidates inside its `with` block")
        while True:
            item = self._next_item()
            if isinstance(item, IndexCandidate):
                yield item
            elif isinstance(item, _Discovered):
                self._observer.discovered(item.files_seen, item.complete)
            elif isinstance(item, _Listed):
                self._observer.listed(item.file_count)
            elif isinstance(item, _Raised):
                raise item.error
            else:
                return

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close(
            interrupted=exc_type is not None and not issubclass(exc_type, Exception)
        )

    def close(self, interrupted: bool = False) -> None:
        """Stop the thread, release what is queued, and wait for it to end."""
        self._stopping.set()
        thread = self._thread
        if thread is None:
            return
        deadline = time.monotonic() + INTERRUPTED_JOIN_SECONDS if interrupted else None
        while thread.is_alive():
            self._drain()
            thread.join(timeout=POLL_SECONDS)
            if deadline is not None and time.monotonic() >= deadline:
                logger.warning(
                    "The read-ahead thread is still busy after an interrupt; "
                    "the process will end it"
                )
                break
        self._drain()

    def _next_item(self) -> _Item:
        thread = self._thread
        assert thread is not None
        while True:
            try:
                return self._queue.get(timeout=POLL_SECONDS)
            except queue.Empty:
                if not thread.is_alive() and self._queue.empty():
                    raise RuntimeError(
                        "The read-ahead thread ended without saying why"
                    ) from None

    def _drain(self) -> None:
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            _release(item)

    # ------------------------------------------------------------------
    # The thread's side

    def _read_ahead(self) -> None:
        candidates: Iterator[IndexCandidate] | None = None
        try:
            candidates = iter(self._source(self._relay))
            for candidate in candidates:
                if self._stopping.is_set():
                    return
                if not self._put(self._prepared(candidate)):
                    return
            self._put(_END)
        except BaseException as error:  # handed to the consumer, raised there
            self._put(_Raised(error))
        finally:
            # A generator left suspended is closed here, in the thread that
            # ran it, so that its own clean-up runs where it was written to.
            close = getattr(candidates, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:
                    logger.warning("Closing the scan raised", exc_info=True)

    def _put(self, item: _Item) -> bool:
        """Queue `item`, unless the consumer has asked the thread to stop."""
        while not self._stopping.is_set():
            try:
                self._queue.put(item, timeout=POLL_SECONDS)
                return True
            except queue.Full:
                continue
        _release(item)
        return False

    def _prepared(self, candidate: IndexCandidate) -> IndexCandidate:
        """`candidate`, with its bytes if the decision will want them and they fit."""
        if not self._will_be_hashed(candidate):
            self.stats.unchanged += 1
            return candidate

        size = candidate.file_size
        if size is None or size > self._limits.max_file_bytes:
            self.stats.too_large += 1
            return candidate
        if not self.budget.try_take(size):
            self.stats.over_budget += 1
            return candidate

        try:
            data = self._read_file(candidate.image.require_absolute_path().value)
        except Exception:
            # Not reported here: the pipeline hashes by path, meets the same
            # error, and reports it with the file (RFC-024 section 6).
            self.budget.give_back(size)
            self.stats.failed += 1
            return candidate

        self.budget.settle(size, len(data))
        self.stats.read += 1
        self.stats.bytes_read += len(data)
        content = FileContent(sha256_of(data), data, on_release=self.budget.give_back)
        return dataclasses.replace(candidate, content=content)

    def _will_be_hashed(self, candidate: IndexCandidate) -> bool:
        """Steps 1 and 2 of `plan_indexing()`, against the snapshot."""
        signals = self._signals.get(candidate.image.id)
        return signals is None or not is_unchanged(
            candidate, signals.file_size, signals.file_modified_at
        )


def _release(item: _Item) -> None:
    if isinstance(item, IndexCandidate) and item.content is not None:
        item.content.release()
