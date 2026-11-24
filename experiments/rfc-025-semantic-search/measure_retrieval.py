"""Measure retrieval quality end to end, in English and in Portuguese.

Runs the production path -- `SearchImagesUseCase` -> `ClipEmbeddingModel`,
with its PT->EN translator -> `PostgresImageRepository.search_similar()` ->
pgvector -- over the 45-image RFC-022 demo corpus and its 25 ground-truth
queries, three times over:

- `en`: the English text (`query`), what RFC-025 section 11 measured;
- `pt`: the same question in Portuguese as a person types it (`query_pt`);
- `pt-sem-acento`: that Portuguese without accents, the spelling of the
  RFC-023 bake-off, which wrote the 25 translations by hand.

All three share one ground truth, so the difference between them is the
language path -- detection, translation, the prompt -- and nothing else. The
metric functions are imported from the end-to-end test rather than
reimplemented, so the numbers here and the floors the suite gates on cannot
drift apart. Each per-query rate is printed with its 95% Wilson interval: 25
queries is a small sample, and one query is 4 points. English against
Portuguese is compared query by query, with McNemar's exact test on the pairs
that disagree.

Everything happens inside one transaction that is always rolled back, so the
development database is left as it was found. The cluster must be up, as for
the suite:

    python -m app.infrastructure.database.embedded run -- \\
        python experiments/rfc-025-semantic-search/measure_retrieval.py

Written for RFC-025 (2026-08-22, English only). Brought up to the device
identity of RFC-027 and given the Portuguese half on 2026-10-26, which
RFC-025 section 11.6 reports.
"""

from __future__ import annotations

import math
import statistics
import sys
import tempfile
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "backend"))

from sqlalchemy import delete  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from tests.conftest import (  # noqa: E402
    make_test_device,
    materialized_embedding_model,
)
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
from app.infrastructure.database.models.device_model import DeviceModel  # noqa: E402
from app.infrastructure.database.models.image_model import ImageModel  # noqa: E402
from app.infrastructure.database.models.indexing_job_model import (  # noqa: E402
    IndexingJobModel,
)
from app.infrastructure.filesystem.filesystem_image_provider import (  # noqa: E402
    FilesystemImageProvider,
)
from app.infrastructure.filesystem.image_identity import compute_image_id  # noqa: E402
from app.infrastructure.filesystem.sha256_content_hasher import (  # noqa: E402
    Sha256ContentHasher,
)
from app.infrastructure.persistence.engine import EngineInstance  # noqa: E402
from app.infrastructure.persistence.postgres_device_repository import (  # noqa: E402
    PostgresDeviceRepository,
)
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

WILSON_Z = 1.959964
"""The 95% two-sided normal quantile."""

LANGUAGES = ("en", "pt", "pt-sem-acento")


@dataclass(frozen=True)
class Run:
    """One language's 25 rankings, with what each query became on the way."""

    language: str
    outcomes: list[QueryOutcome]
    prompts: list[str]
    seconds: list[float]


def strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text)
    return "".join(c for c in decomposed if unicodedata.category(c) != "Mn")


def text_of(entry: dict[str, object], language: str) -> str:
    if language == "en":
        return str(entry["query"])
    portuguese = str(entry["query_pt"])
    return portuguese if language == "pt" else strip_accents(portuguese)


def wilson(successes: int, total: int) -> tuple[float, float]:
    """The 95% Wilson score interval of a proportion."""
    if total == 0:
        return (0.0, 0.0)
    p = successes / total
    z2 = WILSON_Z * WILSON_Z
    centre = (p + z2 / (2 * total)) / (1 + z2 / total)
    half = (
        WILSON_Z
        * math.sqrt(p * (1 - p) / total + z2 / (4 * total * total))
        / (1 + z2 / total)
    )
    return (max(0.0, centre - half), min(1.0, centre + half))


def mcnemar_exact(only_first: int, only_second: int) -> float:
    """Two-sided exact McNemar p-value on the discordant pairs."""
    n = only_first + only_second
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(only_first, only_second) + 1))
    return min(1.0, 2 * tail / 2**n)


def hits_at(outcome: QueryOutcome, k: int) -> bool:
    return bool(outcome.relevant.intersection(outcome.ranked_paths[:k]))


def main() -> None:
    manifest = load_manifest(DEMO_MANIFEST_PATH)
    manifest.verify_matches_directory(DEMO_CORPUS_ROOT)
    queries = load_queries()

    with tempfile.TemporaryDirectory() as tmp:
        root = materialize(manifest, DEMO_CORPUS_ROOT, Path(tmp) / "demo")

        started = time.perf_counter()
        model = materialized_embedding_model()
        assert isinstance(model, ClipEmbeddingModel)
        print(f"model ready in {time.perf_counter() - started:.1f} s")

        connection = EngineInstance.connect()
        transaction = connection.begin()
        session = Session(bind=connection, join_transaction_mode="create_savepoint")

        try:
            # Inside the transaction that is rolled back below: every metric
            # is a top-K over the whole table, so nothing else may be in it.
            session.execute(delete(ImageModel))
            session.execute(delete(IndexingJobModel))
            session.execute(delete(DeviceModel))
            session.commit()

            device = make_test_device()
            PostgresDeviceRepository(session).save(device)
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
                device=device,
                mount_point=root,
            )
            summary = worker.run()
            print(
                f"indexed {summary.indexed} images in "
                f"{summary.elapsed_seconds:.1f} s, {len(summary.failures)} failures"
            )
            paths_by_id = {
                str(compute_image_id(device.id, ImagePath(entry.relative_path))): (
                    entry.relative_path
                )
                for entry in manifest.images
            }
            use_case = SearchImagesUseCase(
                repository=repository, embedding_model=model, default_limit=5
            )

            runs = []
            for language in LANGUAGES:
                outcomes, prompts, seconds = [], [], []
                for entry in queries:
                    text = text_of(entry, language)
                    prompts.append(model.build_prompt(text))
                    began = time.perf_counter()
                    hits = use_case.execute(text, limit=CORPUS_SIZE)
                    seconds.append(time.perf_counter() - began)
                    outcomes.append(
                        QueryOutcome(
                            query=text,
                            relevant=frozenset(entry["relevant"]),
                            hard_negatives=frozenset(entry["hard_negatives"]),
                            ranked_paths=tuple(
                                paths_by_id[str(hit.image.id)] for hit in hits
                            ),
                            similarities=tuple(hit.similarity for hit in hits),
                        )
                    )
                runs.append(Run(language, outcomes, prompts, seconds))
        finally:
            session.close()
            transaction.rollback()
            connection.close()

    report(runs)


def report(runs: list[Run]) -> None:
    by_language = {run.language: run for run in runs}
    english = by_language["en"]

    print("\n" + "=" * 112)
    print("Per query: OK = the first result is relevant; R@5 = a relevant one in 5")
    print("=" * 112)
    for index, entry in enumerate(english.outcomes):
        cells = []
        for language in LANGUAGES:
            outcome = by_language[language].outcomes[index]
            top = "OK" if outcome.ranked_paths[0] in outcome.relevant else "--"
            cells.append(f"{language}: {top} R@5 {'sim' if hits_at(outcome, 5) else 'NAO'}")
        print(f"{index + 1:>2}. {entry.query}")
        print(f"    {' | '.join(cells)}")
        for language in ("pt", "pt-sem-acento"):
            run = by_language[language]
            print(f"    {language:<13} {run.outcomes[index].query}")
            print(f"    {'':<13} -> {run.prompts[index]}")

    print("\n" + "=" * 112)
    print(f"{'metric':<34}" + "".join(f"{language:>26}" for language in LANGUAGES))
    print("-" * 112)
    count = len(english.outcomes)

    def rate_row(name: str, values: list[tuple[int, int]]) -> None:
        cells = []
        for successes, total in values:
            low, high = wilson(successes, total)
            cells.append(
                f"{successes / total:6.1%} ({low:5.1%}-{high:5.1%})".rjust(26)
            )
        print(f"{name:<34}" + "".join(cells))

    for k in (1, 5, 10):
        rate_row(
            f"Recall@{k} [95% Wilson]",
            [
                (sum(hits_at(o, k) for o in by_language[lang].outcomes), count)
                for lang in LANGUAGES
            ],
        )
    rate_row(
        "strict top-1 [95% Wilson]",
        [
            (
                sum(o.ranked_paths[0] in o.relevant for o in by_language[lang].outcomes),
                count,
            )
            for lang in LANGUAGES
        ],
    )

    def plain_row(name: str, values: list[float]) -> None:
        print(f"{name:<34}" + "".join(f"{value:>26.1%}" for value in values))

    plain_row(
        "relevant coverage@5",
        [relevant_coverage_at_k(by_language[lang].outcomes, 5) for lang in LANGUAGES],
    )
    plain_row(
        "hard-negative contamination@5",
        [
            hard_negative_contamination_at_k(by_language[lang].outcomes, 5)
            for lang in LANGUAGES
        ],
    )
    plain_row(
        "pairwise",
        [pairwise_accuracy(by_language[lang].outcomes) for lang in LANGUAGES],
    )
    plain_row(
        "check: strict_top_1() of the test",
        [strict_top_1(by_language[lang].outcomes) for lang in LANGUAGES],
    )
    plain_row(
        "check: recall_at_k(5) of the test",
        [recall_at_k(by_language[lang].outcomes, 5) for lang in LANGUAGES],
    )

    print("-" * 112)
    for language in LANGUAGES:
        seconds = by_language[language].seconds
        print(
            f"latency {language:<13}: median {statistics.median(seconds) * 1000:6.0f} ms, "
            f"max {max(seconds) * 1000:6.0f} ms (the first query pays any load)"
        )

    print("-" * 112)
    print("English against Portuguese, query by query (McNemar, exact, two-sided)")
    for language in ("pt", "pt-sem-acento"):
        other = by_language[language].outcomes
        for name, k in (("top-1", 1), ("Recall@5", 5)):
            only_english = sum(
                hits_at(e, k) and not hits_at(p, k)
                for e, p in zip(english.outcomes, other, strict=True)
            )
            only_other = sum(
                hits_at(p, k) and not hits_at(e, k)
                for e, p in zip(english.outcomes, other, strict=True)
            )
            print(
                f"  en vs {language:<13} {name:<9}: only en {only_english}, "
                f"only {language} {only_other}, "
                f"p = {mcnemar_exact(only_english, only_other):.3f}"
            )


if __name__ == "__main__":
    main()
