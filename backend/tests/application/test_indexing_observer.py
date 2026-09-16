"""The extension point RFC-029 needed, and the rule it exists to get right.

Three separable questions:

* does a run report progress often enough to be watched, including the
  run that produces no batches at all;
* does it stop when asked, between batches, without discarding inference
  already paid for;
* and **is the checkpoint it reports safe to resume from** -- which is the
  one that, done wrong, loses photographs rather than time.

The last one has a test that actually resumes: a run is killed with its
batch buffer full, a second run starts from the checkpoint the first one
reported, and every file has to end up indexed between the two.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Iterator

import pytest

from app.application.use_cases.index_or_update_images import (
    DurableFrontier,
    IndexingSummary,
    IndexOrUpdateImagesUseCase,
)
from app.application.use_cases.indexing_plan import IndexCandidate
from app.domain.entities.image import Image
from app.domain.services.indexing_observer import IndexingObserver
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.indexing_progress import IndexingProgress
from app.infrastructure.ai.fake_embedding_model import FakeEmbeddingModel
from app.infrastructure.filesystem.filesystem_image_provider import is_after_checkpoint
from tests.application.fakes import FakeImageRepository, StubContentHasher
from tests.conftest import TEST_DEVICE_ID

MODIFIED_AT = datetime.datetime(2026, 8, 21, 12, 0, tzinfo=datetime.UTC)


def image(name: str) -> Image:
    path = ImagePath(f"images/{name}.png")
    return Image(
        id=ImageId(uuid.uuid5(uuid.NAMESPACE_URL, str(path))),
        device_id=TEST_DEVICE_ID,
        relative_path=path,
        filename=name,
        extension="png",
    )


def candidate(name: str) -> IndexCandidate:
    return IndexCandidate(
        image=image(name), file_size=1024, file_modified_at=MODIFIED_AT
    )


def corpus(count: int) -> list[IndexCandidate]:
    """`count` candidates whose names sort in the order they are listed."""
    return [candidate(f"photo_{index:03d}") for index in range(count)]


def use_case(
    repository: FakeImageRepository,
    batch_size: int = 4,
    metadata_prefetch_size: int = 512,
) -> IndexOrUpdateImagesUseCase:
    return IndexOrUpdateImagesUseCase(
        repository=repository,
        embedding_model=FakeEmbeddingModel(),
        content_hasher=StubContentHasher(),
        batch_size=batch_size,
        metadata_prefetch_size=metadata_prefetch_size,
    )


class RecordingObserver(IndexingObserver):
    """Writes down every callback, and can be told to ask for a stop."""

    def __init__(self, stop_after_batches: int | None = None) -> None:
        self.reports: list[tuple[str, IndexingProgress, ImagePath | None]] = []
        """Every callback, in the order it happened.

        One chronological list rather than one list per callback kind,
        because the question the resume test asks is "what was the last
        checkpoint this run reported" -- and answering it from two lists
        concatenated means answering it in the wrong order, which makes
        the strongest test in this module quietly vacuous.
        """

        self.discoveries: list[tuple[int, bool]] = []
        self._stop_after_batches = stop_after_batches

    def discovered(self, files_seen: int, complete: bool) -> None:
        self.discoveries.append((files_seen, complete))

    def window_decided(
        self, progress: IndexingProgress, durable_through: ImagePath | None
    ) -> None:
        self.reports.append(("window", progress, durable_through))

    def batch_persisted(
        self, progress: IndexingProgress, durable_through: ImagePath | None
    ) -> None:
        self.reports.append(("batch", progress, durable_through))

    def should_stop(self) -> bool:
        if self._stop_after_batches is None:
            return False
        return len(self.batches) >= self._stop_after_batches

    @property
    def windows(self) -> list[tuple[IndexingProgress, ImagePath | None]]:
        return [
            (progress, path)
            for kind, progress, path in self.reports
            if kind == "window"
        ]

    @property
    def batches(self) -> list[tuple[IndexingProgress, ImagePath | None]]:
        return [
            (progress, path) for kind, progress, path in self.reports if kind == "batch"
        ]

    @property
    def last_checkpoint(self) -> ImagePath | None:
        """The checkpoint reported most recently, in real order."""
        return self.reports[-1][2] if self.reports else None


class TestTheDefaultObserver:
    def test_a_run_without_an_observer_still_works(self) -> None:
        """RFC-024's whole suite calls `execute()` with one argument."""
        repository = FakeImageRepository()

        summary = use_case(repository).execute(corpus(5))

        assert summary.indexed == 5
        assert summary.stopped is False


class TestReporting:
    def test_every_window_is_reported(self) -> None:
        observer = RecordingObserver()

        use_case(FakeImageRepository(), metadata_prefetch_size=4).execute(
            corpus(12), observer
        )

        assert len(observer.windows) == 3

    def test_every_flush_is_reported(self) -> None:
        observer = RecordingObserver()

        use_case(FakeImageRepository(), batch_size=4).execute(corpus(12), observer)

        assert len(observer.batches) == 3

    def test_a_run_that_flushes_nothing_still_reports(self) -> None:
        """The most common run there is, and the one a per-batch heartbeat kills.

        A re-scan of an indexed disk decides whole windows as
        `SKIP_UNCHANGED` and never flushes. Reporting only on flush would
        make the healthiest possible run indistinguishable from a dead
        one (RFC-029 section 9.1).
        """
        repository = FakeImageRepository()
        candidates = corpus(12)
        use_case(repository).execute(candidates)

        observer = RecordingObserver()
        summary = use_case(repository, metadata_prefetch_size=4).execute(
            candidates, observer
        )

        assert summary.skipped_unchanged == 12
        assert observer.batches == []
        assert len(observer.windows) == 3

    def test_the_counters_reported_are_the_ones_the_job_row_holds(self) -> None:
        observer = RecordingObserver()

        use_case(FakeImageRepository(), batch_size=4).execute(corpus(8), observer)

        final, _ = observer.windows[-1]
        assert final.discovered_files == 8
        assert final.processed_images == 8
        assert final.skipped_images == 0
        assert final.failed_images == 0

    def test_discovery_completeness_is_not_this_layer_s_to_claim(self) -> None:
        """The pipeline cannot know whether the stream feeding it has ended."""
        summary = IndexingSummary(discovered=10, indexed=10)

        assert summary.as_progress().discovery_complete is False


class TestStopping:
    def test_a_stop_is_observed_between_batches(self) -> None:
        observer = RecordingObserver(stop_after_batches=1)

        summary = use_case(FakeImageRepository(), batch_size=4).execute(
            corpus(20), observer
        )

        assert summary.stopped is True
        assert summary.indexed < 20

    def test_the_batch_in_flight_is_finished_rather_than_abandoned(self) -> None:
        """RFC-029 section 8: inference already paid for is not thrown away."""
        repository = FakeImageRepository()
        observer = RecordingObserver(stop_after_batches=1)

        summary = use_case(repository, batch_size=4).execute(corpus(20), observer)

        assert summary.indexed % 4 == 0
        assert summary.indexed >= 4
        assert len(repository.save_indexed_many_calls) >= 1

    def test_stopping_is_not_an_error(self) -> None:
        """A cancellation does not raise, and what was indexed stays indexed."""
        repository = FakeImageRepository()
        observer = RecordingObserver(stop_after_batches=1)

        summary = use_case(repository, batch_size=4).execute(corpus(20), observer)

        assert summary.failures == []
        assert summary.stopped is True

    def test_a_stop_before_the_first_window_indexes_nothing(self) -> None:
        class AlwaysStop(RecordingObserver):
            def should_stop(self) -> bool:
                return True

        summary = use_case(FakeImageRepository()).execute(corpus(20), AlwaysStop())

        assert summary.stopped is True
        assert summary.indexed == 0
        assert summary.discovered == 0


class TestTheDurableFrontier:
    def test_nothing_seen_means_nothing_durable(self) -> None:
        assert DurableFrontier().durable_through is None

    def test_a_decided_file_moves_the_frontier(self) -> None:
        frontier = DurableFrontier()

        frontier.decided(ImagePath("a.jpg"))

        assert frontier.durable_through == ImagePath("a.jpg")

    def test_a_buffered_file_freezes_the_frontier_behind_it(self) -> None:
        frontier = DurableFrontier()
        frontier.decided(ImagePath("a.jpg"))

        frontier.buffered(ImagePath("b.jpg"))

        assert frontier.durable_through == ImagePath("a.jpg")

    def test_the_frontier_stays_frozen_while_later_files_are_decided(self) -> None:
        """The case that makes "last file seen" wrong (RFC-029 section 10).

        `b.jpg` is still in the buffer while `c.jpg` and `d.jpg` are
        skipped. A checkpoint of `d.jpg` would step over `b.jpg`, and a
        crash here would lose it for good.
        """
        frontier = DurableFrontier()
        frontier.decided(ImagePath("a.jpg"))
        frontier.buffered(ImagePath("b.jpg"))

        frontier.decided(ImagePath("c.jpg"))
        frontier.decided(ImagePath("d.jpg"))

        assert frontier.durable_through == ImagePath("a.jpg")

    def test_flushing_releases_the_frontier_to_the_last_file_seen(self) -> None:
        frontier = DurableFrontier()
        frontier.buffered(ImagePath("b.jpg"))
        frontier.decided(ImagePath("c.jpg"))

        frontier.flushed()

        assert frontier.durable_through == ImagePath("c.jpg")

    def test_a_first_file_that_is_buffered_leaves_nothing_durable(self) -> None:
        frontier = DurableFrontier()

        frontier.buffered(ImagePath("a.jpg"))

        assert frontier.durable_through is None


class TestTheReportedCheckpoint:
    def test_the_checkpoint_never_passes_a_buffered_file(self) -> None:
        """Checked at every callback of a real run, not just in the unit above."""
        repository = FakeImageRepository()
        observer = RecordingObserver()
        candidates = corpus(20)

        use_case(repository, batch_size=4, metadata_prefetch_size=8).execute(
            candidates, observer
        )

        written = {
            str(record.image.relative_path) for record in repository.save_indexed_calls
        }
        order = [str(entry.image.relative_path) for entry in candidates]

        for progress, checkpoint in observer.windows + observer.batches:
            del progress
            if checkpoint is None:
                continue
            covered = order[: order.index(str(checkpoint)) + 1]
            assert set(covered) <= written, f"checkpoint {checkpoint} ran ahead"


class TestResumingAfterACrash:
    def test_a_crash_with_a_full_buffer_loses_no_images(self) -> None:
        """The test RFC-029's checkpoint exists to pass.

        The run dies mid-stream with `EMBED` plans still buffered -- a
        power cut, a killed process -- so nothing flushes them. A second
        run starts from the checkpoint the first one last reported, and
        between the two every file must be indexed exactly once.

        A checkpoint taken from the last file *seen* fails this: the
        second run starts past the buffered files and they are never
        written by anyone.
        """
        repository = FakeImageRepository()
        observer = RecordingObserver()
        candidates = corpus(30)

        with pytest.raises(RuntimeError, match="power cut"):
            use_case(repository, batch_size=3, metadata_prefetch_size=10).execute(
                _dies_after(candidates, 25), observer
            )

        checkpoint = observer.last_checkpoint
        remaining = [
            entry
            for entry in candidates
            if is_after_checkpoint(
                entry.image.relative_path.value,
                checkpoint.value if checkpoint is not None else None,
            )
        ]
        use_case(repository, batch_size=4).execute(remaining)

        indexed = [
            str(record.image.relative_path) for record in repository.save_indexed_calls
        ]
        expected = [str(entry.image.relative_path) for entry in candidates]

        assert sorted(indexed) == sorted(expected)
        assert len(indexed) == len(set(indexed)), "a file was indexed twice"

    def test_the_crash_really_did_leave_the_buffer_full(self) -> None:
        """Without this, the test above could pass for entirely the wrong reason.

        A stream that raises always dies while the *next* window is being
        filled, so the pipeline has consumed a whole number of windows.
        If the window size is a multiple of the batch size the buffer is
        empty at that instant -- and then "the last file seen" is a safe
        checkpoint too, and the test above proves nothing about the rule.

        Hence 10 and 3. Two windows are consumed (20 files), six batches
        are written (18 files), and **two files are lost with the
        buffer**. The checkpoint has to name the last written file, not
        the last file seen.
        """
        repository = FakeImageRepository()
        observer = RecordingObserver()
        candidates = corpus(30)

        with pytest.raises(RuntimeError):
            use_case(repository, batch_size=3, metadata_prefetch_size=10).execute(
                _dies_after(candidates, 25), observer
            )

        written = {
            str(record.image.relative_path) for record in repository.save_indexed_calls
        }
        assert len(written) == 18, "the buffer was empty; the scenario is vacuous"
        assert "images/photo_018.png" not in written
        assert "images/photo_019.png" not in written

        assert str(observer.last_checkpoint) == "images/photo_017.png"


def _dies_after(
    candidates: list[IndexCandidate], count: int
) -> Iterator[IndexCandidate]:
    """Yield `count` candidates and then take the process down.

    Raising out of the stream is what a crash looks like from inside
    `execute()`: the exception propagates, and whatever was buffered is
    gone without being written.
    """
    for index, entry in enumerate(candidates):
        if index == count:
            raise RuntimeError("power cut")
        yield entry
