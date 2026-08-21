"""Application use case coordinating incremental indexing over many images.

This is where RFC-024's batching actually happens, and it exists because
`IndexOrUpdateImageUseCase.execute()` structurally could not host it: that
method takes one image and performs the skip check, the embed, and the
persist for that one image in one call. Looping over it N times to "fill a
batch" would still be N sequential single-image inferences with extra
bookkeeping around them.

The stages are explicit and composed as a stream, not as queues or threads
(RFC-024 section 10):

    candidates -> prefetch window -> skip decision -> inference batch
               -> persistence

Two window sizes appear below and they are deliberately different. Metadata
prefetch reads a handful of scalars per row, so it wants a large window;
inference holds decoded pixels in memory, so it wants a small one. See
`Settings.metadata_prefetch_size` for the full argument.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from itertools import islice

from app.application.use_cases.indexing_plan import (
    IndexAction,
    IndexCandidate,
    IndexPlan,
    plan_indexing,
)
from app.domain.repositories.image_repository import ImageRepository
from app.domain.services.content_hasher_port import ContentHasherPort
from app.domain.services.embedding_model_port import EmbeddingModelPort
from app.domain.value_objects.embedding_vector import EmbeddingVector
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.index_metadata import IndexMetadata
from app.domain.value_objects.indexing_record import IndexingRecord


@dataclass(frozen=True)
class IndexingFailure:
    """One file that could not be indexed, and the error that stopped it.

    Carries the original exception, never a wrapper. RFC-024 section 6 is
    explicit that the individual retry must surface the real error and the
    offending path; a generic wrapper would tell an operator nothing they
    could act on.
    """

    path: str
    error: BaseException


@dataclass(frozen=True)
class BatchFallback:
    """A batch that failed as a unit and was retried one item at a time.

    Recorded because the batch-level exception is otherwise lost: when the
    retry then succeeds for every item, the run reports no failures at all
    and the only trace that anything went wrong would be the missing time.

    Used for both batching dimensions -- inference and persistence -- which
    resolve the same tension the same way: attempt the batch, and on
    failure pay the sequential price once so that only the genuinely bad
    item is lost.
    """

    paths: tuple[str, ...]
    error: BaseException


@dataclass
class IndexingSummary:
    """Counters and timings for one indexing run.

    Machine-readable as it stands -- the fields are the data -- and
    rendered for humans by `format_report()`. Returned rather than logged
    so the Application layer stays free of a logging dependency; the
    Infrastructure caller decides what to do with it (RFC-024 section 11).
    """

    discovered: int = 0
    skipped_unchanged: int = 0
    skipped_content_identical: int = 0
    indexed: int = 0
    inference_batches: int = 0
    inference_seconds: float = 0.0
    persistence_batches: int = 0
    persistence_writes: int = 0
    persistence_seconds: float = 0.0
    elapsed_seconds: float = 0.0
    failures: list[IndexingFailure] = field(default_factory=list)
    inference_fallbacks: list[BatchFallback] = field(default_factory=list)
    persistence_fallbacks: list[BatchFallback] = field(default_factory=list)

    @property
    def failed(self) -> int:
        """How many individual files could not be indexed."""
        return len(self.failures)

    def format_report(self) -> str:
        """Render the run as the block RFC-024 section 11 specifies.

        The persistence percentage is not decoration. RFC-024 section 7.2
        gates bulk upsert writes on whether database time is still a
        visible fraction of a run once the mandatory metadata prefetch is
        in place, and this is the line that answers it from a real run
        instead of by assertion.
        """
        average_batch = self._average(self.inference_seconds, self.inference_batches)
        average_write = self._average(
            self.persistence_seconds, self.persistence_batches
        )
        database_share = self._share(self.persistence_seconds, self.elapsed_seconds)

        return "\n".join(
            [
                "Indexing finished",
                "",
                f"Discovered:     {self.discovered:6d}",
                f"Skipped:        {self.skipped_unchanged:6d}   (unchanged)",
                f"Skipped (hash): {self.skipped_content_identical:6d}   "
                "(mtime changed, content identical)",
                f"Indexed:        {self.indexed:6d}",
                f"Failed:         {self.failed:6d}",
                "",
                "Inference:",
                f"  images:       {self.indexed:6d}",
                f"  batches:      {self.inference_batches:6d}",
                f"  avg batch:    {average_batch:6.3f}s",
                f"  throughput:   "
                f"{self._rate(self.indexed, self.inference_seconds):6.1f} img/s",
                f"  fallbacks:    {len(self.inference_fallbacks):6d}   "
                "(batch failed, retried per image)",
                "",
                "Persistence:",
                f"  rows:         {self.persistence_writes:6d}",
                f"  batches:      {self.persistence_batches:6d}",
                f"  avg write:    {average_write:6.3f}s",
                f"  fallbacks:    {len(self.persistence_fallbacks):6d}   "
                "(bulk failed, degraded to per-row)",
                f"  total:        {self.persistence_seconds:6.2f}s   "
                f"({database_share:.1f}% of elapsed)",
                "",
                "Total:",
                f"  elapsed:      {self.elapsed_seconds:6.2f}s",
                f"  throughput:   "
                f"{self._rate(self.discovered, self.elapsed_seconds):6.1f} img/s",
            ]
        )

    @staticmethod
    def _average(total: float, count: int) -> float:
        return total / count if count else 0.0

    @staticmethod
    def _rate(count: int, seconds: float) -> float:
        return count / seconds if seconds > 0.0 else 0.0

    @staticmethod
    def _share(part: float, whole: float) -> float:
        return 100.0 * part / whole if whole > 0.0 else 0.0


class IndexOrUpdateImagesUseCase:
    """Index a stream of candidates in batches without giving up error isolation.

    Per-file isolation survives the move to batching (RFC-024 section 6).
    A batch is attempted as a unit; if it raises, the exact same images are
    retried one at a time, so N-1 of them still land and the one genuinely
    at fault is reported with its real path and its real exception.
    Hashing and persistence are guarded per file for the same reason.
    """

    def __init__(
        self,
        repository: ImageRepository,
        embedding_model: EmbeddingModelPort,
        content_hasher: ContentHasherPort,
        batch_size: int,
        metadata_prefetch_size: int,
    ) -> None:
        if batch_size < 1:
            raise ValueError(f"batch_size must be at least 1, got {batch_size}")
        if metadata_prefetch_size < 1:
            raise ValueError(
                "metadata_prefetch_size must be at least 1, "
                f"got {metadata_prefetch_size}"
            )

        self._repository = repository
        self._embedding_model = embedding_model
        self._content_hasher = content_hasher
        self._batch_size = batch_size
        self._metadata_prefetch_size = metadata_prefetch_size

    def execute(self, candidates: Iterable[IndexCandidate]) -> IndexingSummary:
        """Run the full pipeline over `candidates` and return what happened.

        `candidates` is consumed lazily and never materialized in full, so
        memory stays bounded by the two window sizes no matter how many
        files were discovered.
        """
        summary = IndexingSummary()
        started = time.perf_counter()
        pending: list[IndexPlan] = []

        for window in _windowed(candidates, self._metadata_prefetch_size):
            summary.discovered += len(window)
            existing = self._repository.get_index_metadata_many(
                [candidate.image.id for candidate in window]
            )

            for candidate in window:
                plan = self._plan(candidate, existing, summary)
                if plan is None:
                    continue

                if plan.action is IndexAction.SKIP_UNCHANGED:
                    summary.skipped_unchanged += 1
                elif plan.action is IndexAction.REFRESH_METADATA:
                    self._refresh_metadata(plan, summary)
                else:
                    pending.append(plan)
                    if len(pending) == self._batch_size:
                        self._flush(pending, summary)
                        pending = []

        if pending:
            self._flush(pending, summary)

        summary.elapsed_seconds = time.perf_counter() - started
        return summary

    def _plan(
        self,
        candidate: IndexCandidate,
        existing: dict[ImageId, IndexMetadata],
        summary: IndexingSummary,
    ) -> IndexPlan | None:
        """Decide one candidate, turning a hashing failure into a reported one.

        Hashing reads the file, so it can fail for exactly the reasons
        indexing can -- a file deleted between discovery and processing, a
        permission change, unreadable media -- and that must not abort the
        run any more than a corrupt JPEG does.
        """
        try:
            return plan_indexing(
                candidate,
                existing.get(candidate.image.id),
                self._content_hasher,
            )
        except Exception as exc:
            summary.failures.append(
                IndexingFailure(path=str(candidate.image.path), error=exc)
            )
            return None

    def _flush(self, plans: list[IndexPlan], summary: IndexingSummary) -> None:
        """Encode one assembled batch and persist whatever survived it.

        The persistence batch is the inference batch, not a separately
        sized buffer. RFC-024 section 7.2 required this to be a stated
        decision rather than an accident, and the reason to couple them is
        that the rows to write are exactly the output of the batch just
        encoded: holding them back to fill a larger persistence batch would
        keep N more 512-float embeddings alive for no measured gain, and
        would break the property that a batch's work is durable before the
        next batch starts -- which is what makes crash-restart cheap under
        the incremental skip (section 13). Should a future measurement
        favour decoupling, `Settings` is where the second knob goes.
        """
        self._persist_batch(self._encode_batch(plans, summary), summary)

    def _encode_batch(
        self, plans: list[IndexPlan], summary: IndexingSummary
    ) -> list[tuple[IndexPlan, EmbeddingVector]]:
        """Time one batch, however it ends up being encoded."""
        started = time.perf_counter()
        try:
            return self._encode_group(plans, summary)
        finally:
            summary.inference_seconds += time.perf_counter() - started
            summary.inference_batches += 1

    def _encode_group(
        self, plans: list[IndexPlan], summary: IndexingSummary
    ) -> list[tuple[IndexPlan, EmbeddingVector]]:
        if len(plans) == 1:
            # A batch of one has no per-call overhead to amortize, and
            # attempting it as a batch first would only run the same
            # failing call twice before reporting the same error.
            return self._encode_individually(plans, summary)

        images = [plan.candidate.image for plan in plans]
        try:
            embeddings = self._embedding_model.encode_images(images)
        except Exception as exc:
            summary.inference_fallbacks.append(
                BatchFallback(
                    paths=tuple(str(plan.candidate.image.path) for plan in plans),
                    error=exc,
                )
            )
            return self._encode_individually(plans, summary)

        if len(embeddings) != len(plans):
            # A contract violation in an adapter, not a bad input file.
            # Degrading to per-image here would hide it permanently, so
            # this one stops the run instead.
            raise ValueError(
                f"{type(self._embedding_model).__name__}.encode_images returned "
                f"{len(embeddings)} embeddings for {len(plans)} images"
            )

        return list(zip(plans, embeddings, strict=True))

    def _encode_individually(
        self, plans: list[IndexPlan], summary: IndexingSummary
    ) -> list[tuple[IndexPlan, EmbeddingVector]]:
        """Restore exact per-file isolation by paying the sequential price once."""
        encoded: list[tuple[IndexPlan, EmbeddingVector]] = []
        for plan in plans:
            try:
                embedding = self._embedding_model.encode_image(plan.candidate.image)
            except Exception as exc:
                summary.failures.append(
                    IndexingFailure(path=str(plan.candidate.image.path), error=exc)
                )
                continue
            encoded.append((plan, embedding))
        return encoded

    def _persist_batch(
        self,
        encoded: list[tuple[IndexPlan, EmbeddingVector]],
        summary: IndexingSummary,
    ) -> None:
        """Write a batch behind one transaction, degrading to per-row on failure.

        "One transaction per batch" and "isolate errors per file" genuinely
        conflict: a strict transactional batch discards every row when one
        of them violates a constraint, throwing away embeddings that cost
        real inference time. RFC-024 section 7.2 resolves it exactly as
        section 6 resolves the same tension for inference -- attempt the
        batch, and when it fails, pay the sequential price once so only the
        bad row is lost.
        """
        if not encoded:
            return

        records = [
            IndexingRecord(
                image=plan.candidate.image,
                embedding=embedding,
                file_size=plan.candidate.file_size,
                file_modified_at=plan.candidate.file_modified_at,
                content_hash=plan.content_hash,
            )
            for plan, embedding in encoded
        ]

        started = time.perf_counter()
        try:
            self._repository.save_indexed_many(records)
        except Exception as exc:
            summary.persistence_seconds += time.perf_counter() - started
            summary.persistence_fallbacks.append(
                BatchFallback(
                    paths=tuple(str(record.image.path) for record in records),
                    error=exc,
                )
            )
            self._persist_individually(records, summary)
            return

        summary.persistence_seconds += time.perf_counter() - started
        summary.persistence_batches += 1
        summary.persistence_writes += len(records)
        summary.indexed += len(records)

    def _persist_individually(
        self, records: list[IndexingRecord], summary: IndexingSummary
    ) -> None:
        for record in records:
            started = time.perf_counter()
            try:
                self._repository.save_indexed(record)
            except Exception as exc:
                summary.failures.append(
                    IndexingFailure(path=str(record.image.path), error=exc)
                )
                continue
            finally:
                summary.persistence_seconds += time.perf_counter() - started
                summary.persistence_batches += 1

            summary.persistence_writes += 1
            summary.indexed += 1

    def _refresh_metadata(self, plan: IndexPlan, summary: IndexingSummary) -> None:
        """Record the new mtime and size for a file whose bytes never changed."""
        started = time.perf_counter()
        try:
            self._repository.update_index_metadata(
                plan.candidate.image.id,
                IndexMetadata(
                    file_size=plan.candidate.file_size,
                    file_modified_at=plan.candidate.file_modified_at,
                    content_hash=plan.content_hash,
                ),
            )
        except Exception as exc:
            summary.failures.append(
                IndexingFailure(path=str(plan.candidate.image.path), error=exc)
            )
            return
        finally:
            summary.persistence_seconds += time.perf_counter() - started
            summary.persistence_batches += 1

        summary.persistence_writes += 1
        summary.skipped_content_identical += 1


def _windowed(
    candidates: Iterable[IndexCandidate], size: int
) -> Iterator[Sequence[IndexCandidate]]:
    """Yield consecutive fixed-size windows without materializing the source.

    `FilesystemImageProvider.discover()` sorts its output, and RFC-024
    section 10 relies on that: identical input produces identical windows,
    identical batches, and therefore a reproducible run.
    """
    iterator = iter(candidates)
    while window := list(islice(iterator, size)):
        yield window
