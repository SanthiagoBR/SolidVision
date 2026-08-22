"""Measure RFC-025 retrieval quality end to end, and print the RFC's tables.

Runs the production path -- `SearchImagesUseCase` -> `ClipEmbeddingModel` ->
`PostgresImageRepository.search_similar()` -> pgvector -- over the 45-image
RFC-022 demo corpus and its 25 ground-truth queries, then prints the
per-query and aggregate tables that RFC-025 sections 9 and 11 quote.

The metric functions are imported from the end-to-end test rather than
reimplemented, so the numbers in the RFC and the numbers the test gates on
cannot drift apart.

Everything happens inside one transaction that is always rolled back, so
the development database is left exactly as it was found.

    python experiments/rfc-025-semantic-search/measure_retrieval.py
"""

from __future__ import annotations

import statistics
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "backend"))

from sqlalchemy import delete  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from tests.dataset.test_semantic_search_e2e import (  # noqa: E402
    CORPUS_SIZE,
    QueryOutcome,
    hard_negative_contamination_at_k,
    load_queries,
    pairwise_accuracy,
    recall_at_k,
    relevant_coverage_at_k,
    strict_top_1,
)

from app.application.use_cases.index_or_update_images import (  # noqa: E402
    IndexOrUpdateImagesUseCase,
)
from app.application.use_cases.search_images import SearchImagesUseCase  # noqa: E402
from app.domain.value_objects.image_path import ImagePath  # noqa: E402
from app.infrastructure.ai.clip_embedding_model import ClipEmbeddingModel  # noqa: E402
from app.infrastructure.config.constants import (  # noqa: E402
    SUPPORTED_IMAGE_EXTENSIONS,
)
from app.infrastructure.database.models.image_model import ImageModel  # noqa: E402
from app.infrastructure.filesystem.filesystem_image_provider import (  # noqa: E402
    FilesystemImageProvider,
)
from app.infrastructure.filesystem.image_identity import compute_image_id  # noqa: E402
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

PORTUGUESE_SMOKE_QUERY = "uma propriedade rural com um lago"


def main() -> None:
    manifest = load_manifest(DEMO_MANIFEST_PATH)
    manifest.verify_matches_directory(DEMO_CORPUS_ROOT)

    with tempfile.TemporaryDirectory() as tmp:
        root = materialize(manifest, DEMO_CORPUS_ROOT, Path(tmp) / "demo")
        paths_by_id = {
            str(compute_image_id(ImagePath(str(root / entry.relative_path)))): (
                entry.relative_path
            )
            for entry in manifest.images
        }

        model = ClipEmbeddingModel()
        connection = EngineInstance.connect()
        transaction = connection.begin()
        session = Session(bind=connection, join_transaction_mode="create_savepoint")

        try:
            session.execute(delete(ImageModel))
            session.commit()

            repository = PostgresImageRepository(session)
            worker = IndexingWorker(
                filesystem_provider=FilesystemImageProvider(
                    root, SUPPORTED_IMAGE_EXTENSIONS
                ),
                index_or_update_images_use_case=IndexOrUpdateImagesUseCase(
                    repository=repository,
                    embedding_model=model,
                    content_hasher=Sha256ContentHasher(),
                    batch_size=8,
                    metadata_prefetch_size=512,
                ),
            )
            summary = worker.run()
            print(
                f"\nindexed {summary.indexed} images in "
                f"{summary.elapsed_seconds:.1f}s\n"
            )

            use_case = SearchImagesUseCase(
                repository=repository, embedding_model=model, default_limit=5
            )

            outcomes = []
            for entry in load_queries():
                hits = use_case.execute(entry["query"], limit=CORPUS_SIZE)
                outcomes.append(
                    QueryOutcome(
                        query=entry["query"],
                        relevant=frozenset(entry["relevant"]),
                        hard_negatives=frozenset(entry["hard_negatives"]),
                        ranked_paths=tuple(
                            paths_by_id[str(hit.image.id)] for hit in hits
                        ),
                        similarities=tuple(hit.similarity for hit in hits),
                    )
                )

            report(outcomes)

            print("\n--- Portuguese smoke query -------------------------------")
            print(f"query        : {PORTUGUESE_SMOKE_QUERY}")
            print(f"translated   : {model.build_prompt(PORTUGUESE_SMOKE_QUERY)}")
            for hit in use_case.execute(PORTUGUESE_SMOKE_QUERY, limit=5):
                print(f"  {hit.similarity:+.4f}  {paths_by_id[str(hit.image.id)]}")
        finally:
            session.close()
            transaction.rollback()
            connection.close()


def report(outcomes: list[QueryOutcome]) -> None:
    print("=" * 118)
    print(
        f"{'#':>2}  {'query':<52} {'top-1 result':<38} "
        f"{'sim':>7} {'R@5':>4} {'HN@5':>5}"
    )
    print("-" * 118)
    for index, outcome in enumerate(outcomes, start=1):
        top_1 = outcome.ranked_paths[0]
        recalled = bool(outcome.relevant.intersection(outcome.ranked_paths[:5]))
        contaminating = len(
            outcome.hard_negatives.intersection(outcome.ranked_paths[:5])
        )
        marker = "OK " if top_1 in outcome.relevant else "   "
        print(
            f"{index:>2}  {outcome.query[:52]:<52} "
            f"{marker}{top_1.replace('images/', '')[:35]:<35} "
            f"{outcome.similarities[0]:+.4f} {'yes' if recalled else 'NO':>4} "
            f"{contaminating:>5}"
        )

    all_scores = [score for outcome in outcomes for score in outcome.similarities]
    top_scores = [outcome.similarities[0] for outcome in outcomes]

    print("=" * 118)
    print(f"queries                          : {len(outcomes)}")
    print(f"Recall@5 (>=1 relevant in top 5) : {recall_at_k(outcomes, 5):.1%}")
    print(f"Recall@1                         : {recall_at_k(outcomes, 1):.1%}")
    print(f"Recall@10                        : {recall_at_k(outcomes, 10):.1%}")
    print(
        f"relevant coverage@5              : "
        f"{relevant_coverage_at_k(outcomes, 5):.1%}"
    )
    print(
        f"hard-negative contamination@5    : "
        f"{hard_negative_contamination_at_k(outcomes, 5):.1%}"
    )
    print(f"strict top-1 (bake-off metric)   : {strict_top_1(outcomes):.1%}")
    print(f"pairwise (bake-off metric)       : {pairwise_accuracy(outcomes):.1%}")
    print("-" * 118)
    print(
        f"similarity, all {len(all_scores):>5} scores    : "
        f"min {min(all_scores):+.4f}  max {max(all_scores):+.4f}  "
        f"mean {statistics.mean(all_scores):+.4f}"
    )
    print(
        f"similarity, top-1 of each query  : "
        f"min {min(top_scores):+.4f}  max {max(top_scores):+.4f}  "
        f"mean {statistics.mean(top_scores):+.4f}"
    )


if __name__ == "__main__":
    main()
