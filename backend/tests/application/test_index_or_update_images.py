"""Tests for the batch indexing coordinator (RFC-024 sections 5-8, 11).

Everything here runs against fakes: a fake repository, a stub hasher that
touches no disk, and embedding models built on `FakeEmbeddingModel`. That is
deliberate -- the questions this module asks are about control flow (what gets
batched, what gets skipped, what survives a failure, what the counters say),
and answering them should not require a database, a filesystem, or a model.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Iterator, Sequence

import pytest

from app.application.use_cases.index_or_update_images import IndexOrUpdateImagesUseCase
from app.application.use_cases.indexing_plan import IndexCandidate
from app.domain.entities.image import Image
from app.domain.services.embedding_model_port import EmbeddingModelPort
from app.domain.value_objects.embedding_vector import EmbeddingVector
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.indexing_record import IndexingRecord
from app.infrastructure.ai.fake_embedding_model import FakeEmbeddingModel
from tests.application.fakes import FakeImageRepository, StubContentHasher

MODIFIED_AT = datetime.datetime(2026, 8, 21, 12, 0, tzinfo=datetime.UTC)


class _RecordingModel(EmbeddingModelPort):
    """Counts single and batched calls, inheriting the port's default loop."""

    def __init__(self) -> None:
        self._delegate = FakeEmbeddingModel()
        self.single_calls: list[Image] = []
        self.batch_sizes: list[int] = []

    def encode_image(self, image: Image) -> EmbeddingVector:
        self.single_calls.append(image)
        return self._delegate.encode_image(image)

    def encode_text(self, text: str) -> EmbeddingVector:
        return self._delegate.encode_text(text)

    def encode_images(self, images: Sequence[Image]) -> list[EmbeddingVector]:
        self.batch_sizes.append(len(images))
        return super().encode_images(images)


class _SingleImageOnlyModel(EmbeddingModelPort):
    """An implementation that never heard of batching.

    RFC-024 section 5 requires the Application layer to keep working
    against exactly this, which is why `encode_images` is concrete on the
    port rather than abstract.
    """

    def __init__(self) -> None:
        self._delegate = FakeEmbeddingModel()
        self.single_calls: list[Image] = []

    def encode_image(self, image: Image) -> EmbeddingVector:
        self.single_calls.append(image)
        return self._delegate.encode_image(image)

    def encode_text(self, text: str) -> EmbeddingVector:
        return self._delegate.encode_text(text)


class _BatchFailsForFilename(EmbeddingModelPort):
    """Raises for one filename, whether encoded alone or inside a batch."""

    def __init__(self, failing_filename: str) -> None:
        self._delegate = FakeEmbeddingModel()
        self._failing = failing_filename
        self.batch_sizes: list[int] = []
        self.single_calls: list[Image] = []

    def encode_image(self, image: Image) -> EmbeddingVector:
        self.single_calls.append(image)
        if image.filename == self._failing:
            raise ValueError(f"cannot decode {image.filename}")
        return self._delegate.encode_image(image)

    def encode_text(self, text: str) -> EmbeddingVector:
        return self._delegate.encode_text(text)

    def encode_images(self, images: Sequence[Image]) -> list[EmbeddingVector]:
        self.batch_sizes.append(len(images))
        return super().encode_images(images)


class _WrongLengthModel(EmbeddingModelPort):
    """Violates the port contract by returning the wrong number of vectors."""

    def encode_image(self, image: Image) -> EmbeddingVector:
        return EmbeddingVector([0.1] * 8)

    def encode_text(self, text: str) -> EmbeddingVector:
        return EmbeddingVector([0.1] * 8)

    def encode_images(self, images: Sequence[Image]) -> list[EmbeddingVector]:
        return [EmbeddingVector([0.1] * 8)]


def _image(name: str) -> Image:
    path = ImagePath(f"images/{name}.png")
    return Image(
        id=ImageId(uuid.uuid5(uuid.NAMESPACE_URL, str(path))),
        path=path,
        filename=name,
        extension="png",
    )


def _candidate(
    name: str,
    file_size: int = 1024,
    file_modified_at: datetime.datetime = MODIFIED_AT,
) -> IndexCandidate:
    return IndexCandidate(
        image=_image(name),
        file_size=file_size,
        file_modified_at=file_modified_at,
    )


def _use_case(
    repository: FakeImageRepository,
    embedding_model: EmbeddingModelPort,
    content_hasher: StubContentHasher | None = None,
    batch_size: int = 4,
    metadata_prefetch_size: int = 512,
) -> IndexOrUpdateImagesUseCase:
    return IndexOrUpdateImagesUseCase(
        repository=repository,
        embedding_model=embedding_model,
        content_hasher=content_hasher or StubContentHasher(),
        batch_size=batch_size,
        metadata_prefetch_size=metadata_prefetch_size,
    )


class TestConstruction:
    @pytest.mark.parametrize("batch_size", [0, -1])
    def test_a_nonsensical_batch_size_is_rejected_at_construction(
        self, batch_size: int
    ) -> None:
        """Failing here beats silently indexing nothing at batch size 0."""
        with pytest.raises(ValueError, match="batch_size must be at least 1"):
            _use_case(FakeImageRepository(), _RecordingModel(), batch_size=batch_size)

    def test_a_nonsensical_prefetch_size_is_rejected_at_construction(self) -> None:
        with pytest.raises(ValueError, match="metadata_prefetch_size"):
            _use_case(
                FakeImageRepository(), _RecordingModel(), metadata_prefetch_size=0
            )


class TestBatching:
    def test_candidates_are_grouped_into_batches_of_the_configured_size(self) -> None:
        model = _RecordingModel()
        _use_case(FakeImageRepository(), model, batch_size=3).execute(
            [_candidate(f"photo_{index}") for index in range(7)]
        )

        # 3 + 3 + a remainder of 1. The remainder does not appear in
        # `batch_sizes` because a group of one is routed straight to
        # `encode_image()`; going through the batch path first would only
        # run the same call twice whenever it fails.
        assert model.batch_sizes == [3, 3]
        assert sum(model.batch_sizes) == 6

    def test_every_candidate_is_encoded_exactly_once(self) -> None:
        model = _RecordingModel()
        repository = FakeImageRepository()

        summary = _use_case(repository, model, batch_size=3).execute(
            [_candidate(f"photo_{index}") for index in range(7)]
        )

        assert summary.indexed == 7
        assert len(repository.save_indexed_calls) == 7
        assert len({str(r.image.path) for r in repository.save_indexed_calls}) == 7

    def test_batching_does_not_change_the_embeddings_that_get_persisted(self) -> None:
        """RFC-024's equivalence requirement, at the pipeline level.

        Batching may change how the work is scheduled; it must not change
        what is stored. The adapter-level version of this check, against
        the real checkpoint, lives in the slow CLIP tests.
        """
        candidates = [_candidate(f"photo_{index}") for index in range(6)]

        sequential = FakeImageRepository()
        _use_case(sequential, _RecordingModel(), batch_size=1).execute(candidates)

        batched = FakeImageRepository()
        _use_case(batched, _RecordingModel(), batch_size=4).execute(candidates)

        def by_path(repository: FakeImageRepository) -> dict[str, tuple[float, ...]]:
            return {
                str(record.image.path): record.embedding.values
                for record in repository.save_indexed_calls
            }

        assert by_path(batched) == by_path(sequential)

    def test_an_implementation_without_batching_support_still_works(self) -> None:
        """The port's default loop is the fallback, and it must be enough."""
        model = _SingleImageOnlyModel()
        repository = FakeImageRepository()

        summary = _use_case(repository, model, batch_size=4).execute(
            [_candidate(f"photo_{index}") for index in range(5)]
        )

        assert summary.indexed == 5
        assert len(model.single_calls) == 5

    def test_an_adapter_returning_the_wrong_count_stops_the_run(self) -> None:
        """A contract violation, not a bad file -- degrading would hide it."""
        with pytest.raises(ValueError, match="returned 1 embeddings for 4 images"):
            _use_case(FakeImageRepository(), _WrongLengthModel(), batch_size=4).execute(
                [_candidate(f"photo_{index}") for index in range(4)]
            )

    def test_candidates_are_consumed_lazily(self) -> None:
        """Bounded memory depends on never materializing the whole scan.

        The generator below refuses to produce more than one window ahead
        of what has been persisted, which is only possible if the pipeline
        really is streaming.
        """
        repository = FakeImageRepository()
        produced: list[str] = []

        def candidates() -> Iterator[IndexCandidate]:
            for index in range(6):
                assert len(produced) - len(repository.save_indexed_calls) <= 2
                produced.append(f"photo_{index}")
                yield _candidate(f"photo_{index}")

        summary = _use_case(
            repository,
            _RecordingModel(),
            batch_size=2,
            metadata_prefetch_size=2,
        ).execute(candidates())

        assert summary.indexed == 6


class TestSkipDecisionPrecedesBatchAssembly:
    """RFC-024 section 8."""

    def _index_once(
        self, repository: FakeImageRepository, names: Sequence[str]
    ) -> None:
        _use_case(repository, _RecordingModel(), batch_size=4).execute(
            [_candidate(name) for name in names]
        )

    def test_a_second_run_over_unchanged_candidates_encodes_nothing(self) -> None:
        repository = FakeImageRepository()
        names = [f"photo_{index}" for index in range(6)]
        self._index_once(repository, names)

        model = _RecordingModel()
        summary = _use_case(repository, model, batch_size=4).execute(
            [_candidate(name) for name in names]
        )

        assert model.batch_sizes == []
        assert model.single_calls == []
        assert summary.inference_batches == 0
        assert summary.skipped_unchanged == 6
        assert summary.indexed == 0

    def test_batches_contain_only_the_survivors_of_the_skip_decision(self) -> None:
        repository = FakeImageRepository()
        names = [f"photo_{index}" for index in range(6)]
        self._index_once(repository, names)

        model = _RecordingModel()
        changed = [
            _candidate(name, file_size=2048 if name in {"photo_1", "photo_4"} else 1024)
            for name in names
        ]
        summary = _use_case(
            repository,
            model,
            content_hasher=StubContentHasher(default="f" * 64),
            batch_size=4,
        ).execute(changed)

        assert model.batch_sizes == [2]
        assert summary.indexed == 2
        assert summary.skipped_unchanged == 4

    def test_an_unchanged_candidate_is_never_hashed(self) -> None:
        repository = FakeImageRepository()
        names = [f"photo_{index}" for index in range(4)]
        self._index_once(repository, names)

        hasher = StubContentHasher()
        _use_case(repository, _RecordingModel(), content_hasher=hasher).execute(
            [_candidate(name) for name in names]
        )

        assert hasher.hashed == []


class TestBulkMetadataPrefetch:
    """RFC-024 section 7.1: a Big-O fix, not a throughput tweak."""

    def test_metadata_is_read_once_per_window_not_once_per_candidate(self) -> None:
        repository = FakeImageRepository()
        candidates = [_candidate(f"photo_{index}") for index in range(10)]

        _use_case(
            repository, _RecordingModel(), batch_size=2, metadata_prefetch_size=4
        ).execute(candidates)

        assert repository.get_index_metadata_calls == []
        assert [len(ids) for ids in repository.get_index_metadata_many_calls] == [
            4,
            4,
            2,
        ]

    def test_the_bulk_read_agrees_with_the_per_id_reads(self) -> None:
        """The prefetch is only safe if it answers the same question."""
        repository = FakeImageRepository()
        names = [f"photo_{index}" for index in range(5)]
        _use_case(repository, _RecordingModel()).execute(
            [_candidate(name) for name in names]
        )
        ids = [_image(name).id for name in names] + [_image("never_indexed").id]

        bulk = repository.get_index_metadata_many(ids)
        individually = {
            image_id: repository.get_index_metadata(image_id)
            for image_id in ids
            if repository.get_index_metadata(image_id) is not None
        }

        assert bulk == individually
        assert _image("never_indexed").id not in bulk


class TestContentHashOutcomes:
    def test_a_touched_but_identical_file_refreshes_metadata_without_embedding(
        self,
    ) -> None:
        repository = FakeImageRepository()
        _use_case(repository, _RecordingModel()).execute([_candidate("photo")])

        model = _RecordingModel()
        touched_at = MODIFIED_AT + datetime.timedelta(days=1)
        summary = _use_case(repository, model).execute(
            [_candidate("photo", file_modified_at=touched_at)]
        )

        assert model.single_calls == []
        assert summary.skipped_content_identical == 1
        assert summary.indexed == 0
        assert len(repository.update_index_metadata_calls) == 1

        stored = repository.get_index_metadata(_image("photo").id)
        assert stored is not None
        assert stored.file_modified_at == touched_at

    def test_a_genuinely_changed_file_is_reembedded(self) -> None:
        repository = FakeImageRepository()
        _use_case(repository, _RecordingModel()).execute([_candidate("photo")])

        model = _RecordingModel()
        summary = _use_case(
            repository, model, content_hasher=StubContentHasher(default="e" * 64)
        ).execute([_candidate("photo", file_size=4096)])

        assert len(model.single_calls) == 1
        assert summary.indexed == 1
        assert summary.skipped_content_identical == 0

    def test_a_null_stored_hash_forces_reembedding(self) -> None:
        repository = FakeImageRepository()
        image = _image("legacy")
        repository.save_indexed(
            IndexingRecord(
                image=image,
                embedding=EmbeddingVector([0.5] * 8),
                file_size=1024,
                file_modified_at=MODIFIED_AT,
                content_hash=None,
            )
        )

        model = _RecordingModel()
        summary = _use_case(repository, model).execute(
            [_candidate("legacy", file_size=2048)]
        )

        assert len(model.single_calls) == 1
        assert summary.indexed == 1
        assert summary.skipped_content_identical == 0

    def test_a_hashing_failure_is_isolated_to_its_own_file(self) -> None:
        """Hashing reads the file, so it can fail the way indexing can."""

        class _ExplodingHasher(StubContentHasher):
            def hash_image(self, image: Image) -> str:
                if image.filename == "unreadable":
                    raise PermissionError("denied")
                return super().hash_image(image)

        repository = FakeImageRepository()
        summary = _use_case(
            repository, _RecordingModel(), content_hasher=_ExplodingHasher()
        ).execute([_candidate("a"), _candidate("unreadable"), _candidate("b")])

        assert summary.indexed == 2
        assert summary.failed == 1
        (failure,) = summary.failures
        assert failure.path.endswith("unreadable.png")
        assert isinstance(failure.error, PermissionError)


class TestErrorIsolation:
    """RFC-024 section 6, the RFC's most important correctness requirement."""

    def test_a_batch_with_one_bad_file_still_indexes_the_rest(self) -> None:
        repository = FakeImageRepository()
        model = _BatchFailsForFilename("photo_2")

        summary = _use_case(repository, model, batch_size=4).execute(
            [_candidate(f"photo_{index}") for index in range(4)]
        )

        indexed = {record.image.filename for record in repository.save_indexed_calls}
        assert indexed == {"photo_0", "photo_1", "photo_3"}
        assert summary.indexed == 3
        assert summary.failed == 1

    def test_the_batch_is_retried_one_image_at_a_time(self) -> None:
        model = _BatchFailsForFilename("photo_2")

        _use_case(FakeImageRepository(), model, batch_size=4).execute(
            [_candidate(f"photo_{index}") for index in range(4)]
        )

        assert model.batch_sizes == [4]
        assert [image.filename for image in model.single_calls[-4:]] == [
            "photo_0",
            "photo_1",
            "photo_2",
            "photo_3",
        ]

    def test_the_failure_carries_the_original_exception_not_a_wrapper(self) -> None:
        summary = _use_case(
            FakeImageRepository(), _BatchFailsForFilename("photo_1"), batch_size=3
        ).execute([_candidate(f"photo_{index}") for index in range(3)])

        (failure,) = summary.failures
        assert failure.path == "images/photo_1.png"
        assert type(failure.error) is ValueError
        assert str(failure.error) == "cannot decode photo_1"

    def test_the_degraded_batch_is_recorded_with_its_paths(self) -> None:
        summary = _use_case(
            FakeImageRepository(), _BatchFailsForFilename("photo_1"), batch_size=3
        ).execute([_candidate(f"photo_{index}") for index in range(3)])

        (fallback,) = summary.inference_fallbacks
        assert fallback.paths == (
            "images/photo_0.png",
            "images/photo_1.png",
            "images/photo_2.png",
        )
        assert isinstance(fallback.error, ValueError)

    def test_a_batch_of_one_is_not_attempted_twice(self) -> None:
        """No batch call at all for a single image, failing or not."""
        model = _BatchFailsForFilename("photo_0")

        summary = _use_case(FakeImageRepository(), model, batch_size=4).execute(
            [_candidate("photo_0")]
        )

        assert model.batch_sizes == []
        assert len(model.single_calls) == 1
        assert summary.failed == 1
        assert summary.inference_fallbacks == []

    def test_a_persistence_failure_is_isolated_to_its_own_row(self) -> None:
        class _RejectsOneRow(FakeImageRepository):
            def save_indexed(self, record: IndexingRecord) -> None:
                if record.image.filename == "photo_1":
                    raise RuntimeError("constraint violation")
                super().save_indexed(record)

        repository = _RejectsOneRow()
        summary = _use_case(repository, _RecordingModel(), batch_size=4).execute(
            [_candidate(f"photo_{index}") for index in range(4)]
        )

        assert summary.indexed == 3
        assert summary.failed == 1
        assert summary.failures[0].path.endswith("photo_1.png")


class TestMetrics:
    """RFC-024 section 11, against a corpus with a known mix of outcomes."""

    def test_counters_are_accurate_across_every_outcome(self) -> None:
        repository = FakeImageRepository()
        already_indexed = ["unchanged_a", "unchanged_b", "touched"]
        _use_case(repository, _RecordingModel()).execute(
            [_candidate(name) for name in already_indexed]
        )

        model = _BatchFailsForFilename("corrupt")
        summary = _use_case(repository, model, batch_size=4).execute(
            [
                _candidate("unchanged_a"),
                _candidate("unchanged_b"),
                _candidate(
                    "touched", file_modified_at=MODIFIED_AT + datetime.timedelta(days=1)
                ),
                _candidate("brand_new"),
                _candidate("corrupt"),
            ]
        )

        assert summary.discovered == 5
        assert summary.skipped_unchanged == 2
        assert summary.skipped_content_identical == 1
        assert summary.indexed == 1
        assert summary.failed == 1
        # One metadata refresh plus one full write.
        assert summary.persistence_writes == 2

    def test_the_report_reads_back_the_counters_it_was_given(self) -> None:
        repository = FakeImageRepository()
        summary = _use_case(repository, _RecordingModel(), batch_size=2).execute(
            [_candidate(f"photo_{index}") for index in range(3)]
        )

        report = summary.format_report()

        assert "Indexing finished" in report
        assert "Discovered:          3" in report
        assert "Indexed:             3" in report
        assert "Failed:              0" in report
        assert "% of elapsed" in report

    def test_timings_are_recorded_for_both_stages(self) -> None:
        """The persistence share is what RFC-024 section 7.2 is decided on."""
        summary = _use_case(
            FakeImageRepository(), _RecordingModel(), batch_size=2
        ).execute([_candidate(f"photo_{index}") for index in range(4)])

        assert summary.elapsed_seconds > 0.0
        assert summary.inference_seconds > 0.0
        assert summary.persistence_seconds >= 0.0
        assert summary.inference_batches == 2

    def test_an_empty_run_reports_zeroes_without_dividing_by_zero(self) -> None:
        summary = _use_case(FakeImageRepository(), _RecordingModel()).execute([])

        assert summary.discovered == 0
        assert summary.indexed == 0
        assert "Discovered:          0" in summary.format_report()


class TestBulkPersistence:
    """RFC-024 section 7.2, which the measured 9.8% database share justified."""

    def test_each_inference_batch_is_written_in_one_call(self) -> None:
        repository = FakeImageRepository()

        summary = _use_case(repository, _RecordingModel(), batch_size=3).execute(
            [_candidate(f"photo_{index}") for index in range(7)]
        )

        # 3 + 3 + 1: the persistence batch is the inference batch, so the
        # groupings match exactly rather than being buffered separately.
        assert [len(batch) for batch in repository.save_indexed_many_calls] == [3, 3, 1]
        assert summary.persistence_batches == 3
        assert summary.persistence_writes == 7

    def test_no_rows_are_lost_by_batching_the_writes(self) -> None:
        repository = FakeImageRepository()

        _use_case(repository, _RecordingModel(), batch_size=4).execute(
            [_candidate(f"photo_{index}") for index in range(9)]
        )

        assert len(repository.save_indexed_calls) == 9
        assert len(repository.list()) == 9

    def test_a_failing_bulk_write_degrades_to_per_row(self) -> None:
        """One bad row must not discard the eight good embeddings beside it."""

        class _RejectsBulk(FakeImageRepository):
            def save_indexed_many(self, records: Sequence[IndexingRecord]) -> None:
                self.save_indexed_many_calls.append(list(records))
                if any(record.image.filename == "photo_2" for record in records):
                    raise RuntimeError("constraint violation")
                for record in records:
                    self.save_indexed(record)

            def save_indexed(self, record: IndexingRecord) -> None:
                if record.image.filename == "photo_2":
                    raise RuntimeError("constraint violation")
                super().save_indexed(record)

        repository = _RejectsBulk()
        summary = _use_case(repository, _RecordingModel(), batch_size=4).execute(
            [_candidate(f"photo_{index}") for index in range(4)]
        )

        persisted = {record.image.filename for record in repository.save_indexed_calls}
        assert persisted == {"photo_0", "photo_1", "photo_3"}
        assert summary.indexed == 3
        assert summary.failed == 1
        assert summary.failures[0].path.endswith("photo_2.png")

    def test_the_degraded_bulk_write_is_reported_separately(self) -> None:
        """Inference and persistence fall back for different reasons.

        Collapsing them into one counter would make an operator unable to
        tell "the model choked on a file" from "the database rejected a
        row", which are entirely different problems.
        """

        class _RejectsBulk(FakeImageRepository):
            def save_indexed_many(self, records: Sequence[IndexingRecord]) -> None:
                raise RuntimeError("deadlock detected")

        summary = _use_case(_RejectsBulk(), _RecordingModel(), batch_size=3).execute(
            [_candidate(f"photo_{index}") for index in range(3)]
        )

        assert summary.inference_fallbacks == []
        (fallback,) = summary.persistence_fallbacks
        assert len(fallback.paths) == 3
        assert isinstance(fallback.error, RuntimeError)
        # The per-row retry then succeeded for all three.
        assert summary.indexed == 3
        assert summary.failed == 0

    def test_a_metadata_refresh_is_not_batched_with_the_writes(self) -> None:
        """Refreshes are rare and carry no embedding, so they stay per-row."""
        repository = FakeImageRepository()
        _use_case(repository, _RecordingModel()).execute([_candidate("photo")])
        repository.save_indexed_many_calls.clear()

        summary = _use_case(repository, _RecordingModel()).execute(
            [
                _candidate(
                    "photo", file_modified_at=MODIFIED_AT + datetime.timedelta(days=1)
                )
            ]
        )

        assert repository.save_indexed_many_calls == []
        assert len(repository.update_index_metadata_calls) == 1
        assert summary.skipped_content_identical == 1

    def test_the_report_shows_both_fallback_kinds(self) -> None:
        summary = _use_case(
            FakeImageRepository(), _RecordingModel(), batch_size=2
        ).execute([_candidate(f"photo_{index}") for index in range(4)])

        report = summary.format_report()

        assert "(batch failed, retried per image)" in report
        assert "(bulk failed, degraded to per-row)" in report
