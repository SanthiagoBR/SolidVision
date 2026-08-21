"""Measure the RFC-024 indexing pipeline against the real CLIP checkpoint.

`ARCHITECTURE.md` section 22 reserves `scripts/` for benchmark scripts and
sets the targets this reports against: index throughput above 1 image/sec on
CPU, and a supported collection of 100,000+ images.

Run it from the repository root:

    python scripts/benchmark_indexing.py                # everything
    python scripts/benchmark_indexing.py --skip-pipeline  # no database needed

Three measurements, each answering a question RFC-024 had to decide:

1. **Batch-size sweep** (section 3.1). Encodes the whole demo corpus at each
   batch size with the real adapter, reporting speed *and* peak RSS. The
   memory column is the one that was missing from the numbers RFC-024
   inherited: a speed plateau says nothing about where the memory cliff is.

2. **Embedding equivalence** (sections 3.1 and 5). Batching must change how
   the work is scheduled, never what is computed. Compares every batched
   embedding against its batch-of-1 counterpart.

3. **End-to-end pipeline share** (section 7.2). Runs the real pipeline into
   PostgreSQL and reports what fraction of wall-clock the database actually
   costs. That number decides whether bulk upsert writes are worth building,
   rather than assuming they are.

Writes nothing durable. The pipeline measurement opens a connection-level
transaction and rolls it back, the same technique `backend/tests/conftest.py`
uses, so a benchmark run leaves the development database exactly as it found
it.
"""

from __future__ import annotations

import argparse
import gc
import json
import statistics
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPOSITORY_ROOT), str(REPOSITORY_ROOT / "backend")]

import psutil  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.application.use_cases.index_or_update_images import (  # noqa: E402
    IndexOrUpdateImagesUseCase,
)
from app.domain.entities.image import Image  # noqa: E402
from app.domain.value_objects.embedding_vector import EmbeddingVector  # noqa: E402
from app.domain.value_objects.image_id import ImageId  # noqa: E402
from app.domain.value_objects.image_path import ImagePath  # noqa: E402
from app.infrastructure.ai.clip_embedding_model import ClipEmbeddingModel  # noqa: E402
from app.infrastructure.config.constants import (  # noqa: E402
    SUPPORTED_IMAGE_EXTENSIONS,
)
from app.infrastructure.filesystem.filesystem_image_provider import (  # noqa: E402
    FilesystemImageProvider,
)
from app.infrastructure.filesystem.sha256_content_hasher import (  # noqa: E402
    Sha256ContentHasher,
)
from app.infrastructure.persistence.engine import EngineInstance  # noqa: E402
from app.infrastructure.persistence.postgres_image_repository import (  # noqa: E402
    PostgresImageRepository,
)
from app.infrastructure.workers.indexing_worker import IndexingWorker  # noqa: E402
from dataset_tools.manifest import (  # noqa: E402
    DEMO_CORPUS_ROOT,
    DEMO_MANIFEST_PATH,
    load_manifest,
)
from dataset_tools.materialize import materialize  # noqa: E402

DEFAULT_BATCH_SIZES = (1, 2, 4, 8, 16, 32, 64)
TARGET_COLLECTION_SIZE = 100_000
MEGABYTE = 1024 * 1024


@dataclass
class BatchResult:
    """One row of the batch-size sweep."""

    batch_size: int
    images: int
    total_seconds: float
    seconds_per_image: float
    images_per_second: float
    speedup_versus_one: float
    peak_rss_mb: float
    rss_growth_mb: float
    hours_for_100k: float


class _PeakMemorySampler:
    """Sample this process's RSS on a background thread.

    Checking RSS only between batches would miss the peak entirely: the
    interesting allocation -- decoded pixels plus the assembled input
    tensor -- exists only *during* a forward pass and is released before the
    next one starts. Sampling has to be concurrent with the work to see it.
    """

    def __init__(self, interval_seconds: float = 0.02) -> None:
        self._process = psutil.Process()
        self._interval = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.peak_bytes = 0

    def __enter__(self) -> _PeakMemorySampler:
        self.peak_bytes = self._process.memory_info().rss
        self._thread = threading.Thread(target=self._sample, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _sample(self) -> None:
        while not self._stop.wait(self._interval):
            self.peak_bytes = max(self.peak_bytes, self._process.memory_info().rss)


def _discovered_images(root: Path) -> list[Image]:
    """Build the domain entities the worker would build, in discovery order."""
    provider = FilesystemImageProvider(root, SUPPORTED_IMAGE_EXTENSIONS)
    images = []
    for discovered in provider.discover():
        path = ImagePath(str(discovered.path))
        images.append(
            Image(
                id=ImageId(uuid.uuid5(uuid.NAMESPACE_URL, str(path))),
                path=path,
                filename=discovered.filename,
                extension=discovered.extension,
            )
        )
    return images


def _cosine(left: EmbeddingVector, right: EmbeddingVector) -> float:
    return sum(x * y for x, y in zip(left.values, right.values, strict=True))


def sweep_batch_sizes(
    model: ClipEmbeddingModel,
    images: list[Image],
    batch_sizes: tuple[int, ...],
) -> tuple[list[BatchResult], dict[int, list[EmbeddingVector]]]:
    """Encode the corpus once per batch size, timing and watching memory."""
    results: list[BatchResult] = []
    embeddings_by_batch_size: dict[int, list[EmbeddingVector]] = {}
    baseline_seconds_per_image: float | None = None
    process = psutil.Process()

    for batch_size in batch_sizes:
        gc.collect()
        settled_rss = process.memory_info().rss
        produced: list[EmbeddingVector] = []

        with _PeakMemorySampler() as sampler:
            started = time.perf_counter()
            for offset in range(0, len(images), batch_size):
                produced.extend(
                    model.encode_images(images[offset : offset + batch_size])
                )
            total_seconds = time.perf_counter() - started

        seconds_per_image = total_seconds / len(images)
        if baseline_seconds_per_image is None:
            baseline_seconds_per_image = seconds_per_image

        embeddings_by_batch_size[batch_size] = produced
        results.append(
            BatchResult(
                batch_size=batch_size,
                images=len(images),
                total_seconds=total_seconds,
                seconds_per_image=seconds_per_image,
                images_per_second=1.0 / seconds_per_image,
                speedup_versus_one=baseline_seconds_per_image / seconds_per_image,
                peak_rss_mb=sampler.peak_bytes / MEGABYTE,
                rss_growth_mb=(sampler.peak_bytes - settled_rss) / MEGABYTE,
                hours_for_100k=seconds_per_image * TARGET_COLLECTION_SIZE / 3600.0,
            )
        )
        _print_batch_row(results[-1])

    return results, embeddings_by_batch_size


def _print_batch_row(result: BatchResult) -> None:
    print(
        f"  {result.batch_size:>4} "
        f"{result.total_seconds:>9.2f} "
        f"{result.seconds_per_image:>10.4f} "
        f"{result.images_per_second:>8.2f} "
        f"{result.speedup_versus_one:>8.2f}x "
        f"{result.peak_rss_mb:>10.1f} "
        f"{result.rss_growth_mb:>9.1f} "
        f"{result.hours_for_100k:>9.2f}"
    )


def check_equivalence(
    embeddings_by_batch_size: dict[int, list[EmbeddingVector]],
) -> dict[int, dict[str, float]]:
    """Compare every batched embedding against its batch-of-1 counterpart.

    Reports cosine similarity rather than raw component differences: cosine
    is the metric the HNSW index ranks on, so it is the measure under which
    "unchanged" actually means "cannot reorder a search result".
    """
    reference = embeddings_by_batch_size[1]
    report: dict[int, dict[str, float]] = {}

    for batch_size, produced in embeddings_by_batch_size.items():
        similarities = [
            _cosine(left, right)
            for left, right in zip(reference, produced, strict=True)
        ]
        largest_component_delta = max(
            abs(a - b)
            for left, right in zip(reference, produced, strict=True)
            for a, b in zip(left.values, right.values, strict=True)
        )
        report[batch_size] = {
            "mean_cosine": statistics.fmean(similarities),
            "min_cosine": min(similarities),
            "max_component_delta": largest_component_delta,
        }
        print(
            f"  {batch_size:>4} "
            f"mean {report[batch_size]['mean_cosine']:.6f} "
            f"min {report[batch_size]['min_cosine']:.6f} "
            f"max |delta| {largest_component_delta:.3e}"
        )

    return report


def measure_pipeline(root: Path, batch_size: int) -> dict[str, float]:
    """Run the real pipeline into PostgreSQL, then roll it back.

    Measures two passes. The first is a cold index of every file; the second
    re-scans the same unchanged corpus and is where the bulk metadata
    prefetch earns its keep. Both report the share of wall-clock spent in the
    database, which is what RFC-024 section 7.2 gates bulk writes on.
    """
    connection = EngineInstance.connect()
    transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")

    try:
        worker = IndexingWorker(
            filesystem_provider=FilesystemImageProvider(
                root, SUPPORTED_IMAGE_EXTENSIONS
            ),
            index_or_update_images_use_case=IndexOrUpdateImagesUseCase(
                repository=PostgresImageRepository(session),
                embedding_model=ClipEmbeddingModel(),
                content_hasher=Sha256ContentHasher(),
                batch_size=batch_size,
                metadata_prefetch_size=512,
            ),
        )

        first = worker.run()
        second = worker.run()
    finally:
        session.close()
        transaction.rollback()
        connection.close()

    print("\n--- cold index ---")
    print(first.format_report())
    print("\n--- re-scan of the unchanged corpus ---")
    print(second.format_report())

    return {
        "cold_elapsed_seconds": first.elapsed_seconds,
        "cold_inference_seconds": first.inference_seconds,
        "cold_persistence_seconds": first.persistence_seconds,
        "cold_database_share_percent": (
            100.0 * first.persistence_seconds / first.elapsed_seconds
        ),
        "cold_indexed": float(first.indexed),
        "rescan_elapsed_seconds": second.elapsed_seconds,
        "rescan_skipped_unchanged": float(second.skipped_unchanged),
        "rescan_inference_batches": float(second.inference_batches),
    }


def _materialize_corpus(into: Path) -> Path:
    """Copy the committed demo corpus out, with manifest mtimes stamped on.

    The same thing the `demo_corpus` test fixture does, and for the same
    reason: file mtimes decide what the incremental check does, and a fresh
    `git checkout` would otherwise make every run a cold one.
    """
    manifest = load_manifest(DEMO_MANIFEST_PATH)
    manifest.verify_matches_directory(DEMO_CORPUS_ROOT)
    return materialize(manifest, DEMO_CORPUS_ROOT, into / "demo_corpus")


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--batch-sizes",
        type=int,
        nargs="+",
        default=list(DEFAULT_BATCH_SIZES),
        help="Batch sizes to sweep (default: %(default)s)",
    )
    parser.add_argument(
        "--pipeline-batch-size",
        type=int,
        default=8,
        help="Batch size for the end-to-end pipeline run (default: %(default)s)",
    )
    parser.add_argument(
        "--skip-pipeline",
        action="store_true",
        help="Skip the end-to-end measurement, which needs a running database",
    )
    parser.add_argument(
        "--json",
        type=Path,
        default=None,
        help="Also write the raw results to this path",
    )
    return parser


def main() -> None:
    args = _build_arg_parser().parse_args()

    with tempfile.TemporaryDirectory(prefix="solidvision-benchmark-") as scratch:
        root = _materialize_corpus(Path(scratch))
        images = _discovered_images(root)
        print(f"Corpus: {len(images)} images from {DEMO_CORPUS_ROOT}")

        model = ClipEmbeddingModel()
        load_started = time.perf_counter()
        model.encode_image(images[0])
        print(f"Model load + first encode: {time.perf_counter() - load_started:.2f}s\n")

        print("Batch-size sweep (real CLIP adapter, real corpus)")
        print(
            "  size   total(s)  s/image   img/s  speedup  peak RSS MB"
            "  growth MB   h/100k"
        )
        results, embeddings = sweep_batch_sizes(model, images, tuple(args.batch_sizes))

        print("\nEmbedding equivalence versus batch of 1")
        equivalence = check_equivalence(embeddings)

        pipeline: dict[str, float] = {}
        if not args.skip_pipeline:
            print("\nEnd-to-end pipeline against PostgreSQL (rolled back)")
            pipeline = measure_pipeline(root, args.pipeline_batch_size)

    if args.json is not None:
        args.json.write_text(
            json.dumps(
                {
                    "corpus_images": len(images),
                    "batch_sweep": [asdict(result) for result in results],
                    "equivalence": {
                        str(size): values for size, values in equivalence.items()
                    },
                    "pipeline": pipeline,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\nWrote raw results to {args.json}")


if __name__ == "__main__":
    main()
