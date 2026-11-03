"""The pipeline's side of RFC-045: bytes that arrive with a candidate.

The read-ahead thread is Infrastructure and is tested there; what is asked
here is what the use case does with a candidate that carries its file's
bytes (`IndexCandidate.content`): hands them to the model and the thumbnail,
takes the hash from them, releases them on every way a file leaves the
pipeline (section 5.5), and counts what it saw (section 5.8). The images sit
at paths that do not exist, as everywhere in these tests: a file opened by
path would fail the test.
"""

from __future__ import annotations

import datetime
import time
import uuid
from collections.abc import Iterator, Sequence

from app.application.use_cases.index_or_update_images import (
    IndexingSummary,
    IndexOrUpdateImagesUseCase,
)
from app.application.use_cases.indexing_plan import FileContent, IndexCandidate
from app.application.use_cases.thumbnail_writer import ThumbnailWriter
from app.domain.entities.image import Image
from app.domain.services.embedding_model_port import EmbeddingModelPort
from app.domain.services.indexing_observer import NullIndexingObserver
from app.domain.value_objects.embedding_vector import EmbeddingVector
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.indexing_progress import IndexingProgress
from app.domain.value_objects.indexing_record import IndexingRecord
from app.infrastructure.ai.fake_embedding_model import FakeEmbeddingModel
from tests.application.fakes import (
    FakeImageRepository,
    InMemoryThumbnailStore,
    RecordingThumbnailGenerator,
    StubContentHasher,
)
from tests.conftest import TEST_DEVICE_ID

MODIFIED_AT = datetime.datetime(2026, 10, 28, 12, 0, tzinfo=datetime.UTC)


class _SeesBytes(EmbeddingModelPort):
    """The fake model, recording the bytes each image was encoded from."""

    def __init__(self, failing: frozenset[str] = frozenset()) -> None:
        self._delegate = FakeEmbeddingModel()
        self._failing = failing
        self.single: list[tuple[str, bytes | None]] = []
        self.batches: list[list[tuple[str, bytes | None]]] = []

    def encode_image(
        self, image: Image, content: bytes | None = None
    ) -> EmbeddingVector:
        self.single.append((image.filename, content))
        if image.filename in self._failing:
            raise ValueError(f"cannot decode {image.filename}")
        return self._delegate.encode_image(image)

    def encode_text(self, text: str) -> EmbeddingVector:
        return self._delegate.encode_text(text)

    def encode_images(
        self,
        images: Sequence[Image],
        contents: Sequence[bytes | None] | None = None,
    ) -> list[EmbeddingVector]:
        assert contents is not None
        self.batches.append(
            [(image.filename, data) for image, data in zip(images, contents)]
        )
        return super().encode_images(images, contents)


class _Counter:
    """Stands in for the read-ahead's budget: how many bytes are still out."""

    def __init__(self) -> None:
        self.held = 0

    def content(self, data: bytes, digest: str = "a" * 64) -> FileContent:
        self.held += len(data)
        return FileContent(digest, data, on_release=self._give_back)

    def _give_back(self, size: int) -> None:
        self.held -= size


def _image(name: str) -> Image:
    path = ImagePath(f"photos/{name}.jpg")
    return Image(
        id=ImageId(uuid.uuid5(uuid.NAMESPACE_URL, str(path))),
        device_id=TEST_DEVICE_ID,
        relative_path=path,
        filename=name,
        extension="jpg",
    )


def _candidate(
    name: str, content: FileContent | None, file_size: int = 1024
) -> IndexCandidate:
    return IndexCandidate(
        image=_image(name),
        file_size=file_size,
        file_modified_at=MODIFIED_AT,
        content=content,
    )


def _use_case(
    repository: FakeImageRepository,
    model: EmbeddingModelPort,
    hasher: StubContentHasher | None = None,
    batch_size: int = 2,
    window: int = 4,
    thumbnails: RecordingThumbnailGenerator | None = None,
) -> IndexOrUpdateImagesUseCase:
    return IndexOrUpdateImagesUseCase(
        repository=repository,
        embedding_model=model,
        content_hasher=hasher or StubContentHasher(),
        batch_size=batch_size,
        metadata_prefetch_size=window,
        thumbnail_writer=(
            ThumbnailWriter(thumbnails, InMemoryThumbnailStore(), max_edge=64)
            if thumbnails is not None
            else None
        ),
    )


def _indexed(repository: FakeImageRepository, name: str, digest: str) -> None:
    """A row as a previous run left it: same size and mtime as `_candidate()`."""
    repository.save_indexed(
        IndexingRecord(
            image=_image(name),
            embedding=FakeEmbeddingModel().encode_image(_image(name)),
            file_size=1024,
            file_modified_at=MODIFIED_AT,
            content_hash=digest,
        )
    )


class TestTheBytesReachTheirConsumers:
    def test_the_model_and_the_thumbnail_decode_the_bytes_that_were_read(
        self,
    ) -> None:
        counter = _Counter()
        model = _SeesBytes()
        thumbnails = RecordingThumbnailGenerator()

        _use_case(FakeImageRepository(), model, thumbnails=thumbnails).execute(
            [
                _candidate("a", counter.content(b"bytes of a")),
                _candidate("b", counter.content(b"bytes of b")),
                _candidate("c", None),
            ]
        )

        assert model.batches == [[("a", b"bytes of a"), ("b", b"bytes of b")]]
        assert model.single == [
            ("a", b"bytes of a"),
            ("b", b"bytes of b"),
            ("c", None),
        ]
        assert thumbnails.contents == [b"bytes of a", b"bytes of b", None]

    def test_the_hash_comes_from_the_bytes_and_the_hasher_is_not_asked(
        self,
    ) -> None:
        counter = _Counter()
        repository = FakeImageRepository()
        hasher = StubContentHasher(default="f" * 64)

        _use_case(repository, _SeesBytes(), hasher=hasher).execute(
            [_candidate("a", counter.content(b"x", digest="e" * 64))]
        )

        assert hasher.hashed == []
        (record,) = repository.save_indexed_calls
        assert record.content_hash == "e" * 64


class TestTheBytesAreReleased:
    """RFC-045 section 5.5: every way out of the pipeline gives them back."""

    def test_after_the_batch_is_written(self) -> None:
        counter = _Counter()
        contents = [counter.content(b"12345") for _ in range(4)]

        _use_case(FakeImageRepository(), _SeesBytes()).execute(
            [_candidate(f"p{index}", content) for index, content in enumerate(contents)]
        )

        assert all(content.released for content in contents)
        assert counter.held == 0

    def test_when_the_decision_skips_or_refreshes(self) -> None:
        counter = _Counter()
        repository = FakeImageRepository()
        _indexed(repository, "unchanged", "a" * 64)
        _indexed(repository, "touched", "a" * 64)
        unchanged = counter.content(b"unchanged bytes")
        touched = counter.content(b"touched bytes", digest="a" * 64)

        summary = _use_case(repository, _SeesBytes()).execute(
            [
                _candidate("unchanged", unchanged),
                # A newer mtime than the row's: hashed, identical, refreshed.
                IndexCandidate(
                    image=_image("touched"),
                    file_size=1024,
                    file_modified_at=MODIFIED_AT + datetime.timedelta(hours=1),
                    content=touched,
                ),
            ]
        )

        assert summary.skipped_unchanged == 1
        assert summary.skipped_content_identical == 1
        assert unchanged.released and touched.released
        assert counter.held == 0

    def test_when_the_write_of_the_batch_fails(self) -> None:
        class _RejectsEverything(FakeImageRepository):
            def save_indexed_many(self, records: Sequence[IndexingRecord]) -> None:
                raise RuntimeError("bulk refused")

            def save_indexed(self, record: IndexingRecord) -> None:
                raise RuntimeError("row refused")

        counter = _Counter()
        contents = [counter.content(b"123") for _ in range(2)]

        summary = _use_case(_RejectsEverything(), _SeesBytes()).execute(
            [_candidate(f"p{index}", content) for index, content in enumerate(contents)]
        )

        assert summary.failed == 2
        assert counter.held == 0

    def test_when_an_image_fails_to_encode(self) -> None:
        counter = _Counter()
        contents = [counter.content(b"123") for _ in range(2)]

        summary = _use_case(
            FakeImageRepository(), _SeesBytes(failing=frozenset({"p0"}))
        ).execute(
            [_candidate(f"p{index}", content) for index, content in enumerate(contents)]
        )

        assert summary.failed == 1
        assert counter.held == 0

    def test_when_a_stop_abandons_the_rest_of_a_window(self) -> None:
        class _StopsAfterTheFirstBatch(NullIndexingObserver):
            def __init__(self) -> None:
                self.batches = 0

            def batch_persisted(
                self, progress: IndexingProgress, durable_through: ImagePath | None
            ) -> None:
                self.batches += 1

            def should_stop(self) -> bool:
                return self.batches > 0

        counter = _Counter()
        contents = [counter.content(b"123") for _ in range(8)]

        summary = _use_case(FakeImageRepository(), _SeesBytes(), window=8).execute(
            [
                _candidate(f"p{index}", content)
                for index, content in enumerate(contents)
            ],
            _StopsAfterTheFirstBatch(),
        )

        assert summary.stopped is True
        assert summary.indexed == 2
        assert all(content.released for content in contents)
        assert counter.held == 0


class TestTheReport:
    """RFC-045 section 5.8: what the pilots had to measure from outside."""

    def test_bytes_from_the_read_ahead_and_reads_by_path_are_counted_apart(
        self,
    ) -> None:
        counter = _Counter()
        repository = FakeImageRepository()
        _indexed(repository, "unchanged", "a" * 64)

        summary = _use_case(repository, _SeesBytes()).execute(
            [
                _candidate("read ahead", counter.content(b"1")),
                _candidate("read late", None),
                _candidate("unchanged", None),
            ]
        )

        assert summary.contents_prefetched == 1
        assert summary.contents_read_late == 1

    def test_a_failed_read_by_path_counts_as_read_late(self) -> None:
        class _Unreadable(StubContentHasher):
            def hash_image(self, image: Image) -> str:
                raise OSError("unreadable")

        summary = _use_case(
            FakeImageRepository(), _SeesBytes(), hasher=_Unreadable()
        ).execute([_candidate("a", None)])

        assert summary.failed == 1
        assert summary.contents_read_late == 1

    def test_the_wait_for_candidates_is_the_time_spent_in_the_source(self) -> None:
        def candidates() -> Iterator[IndexCandidate]:
            time.sleep(0.05)
            yield _candidate("a", None)
            time.sleep(0.05)

        summary = _use_case(FakeImageRepository(), _SeesBytes()).execute(candidates())

        assert summary.candidate_wait_seconds >= 0.1
        assert summary.candidate_wait_seconds <= summary.elapsed_seconds

    def test_the_report_has_the_three_lines(self) -> None:
        report = IndexingSummary(
            contents_prefetched=7, contents_read_late=2, candidate_wait_seconds=1.5
        ).format_report()

        assert "prefetched:        7" in report
        assert "read late:         2" in report
        assert "waiting:        1.50s" in report
