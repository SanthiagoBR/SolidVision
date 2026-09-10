"""End-to-end semantic search: real CLIP, real PostgreSQL, real ground truth.

The whole RFC-025 path in one place -- `SearchImagesUseCase` ->
`ClipEmbeddingModel.encode_text()` -> `PostgresImageRepository.search_similar()`
-> pgvector -> ranked `SearchHit`s -- measured against the 25 ground-truth
queries and 45 images of the RFC-022 demo corpus.

Marked `slow` and therefore deselected by the default `addopts`, because it
downloads and runs real checkpoints (RFC-023 section 12.1). Run it with
`pytest -m slow`.

**These tests measure aggregates, never individual results.** Asserting
that a given query returns a given filename first would pin the model's
noise: the checkpoint takes 64.0% strict top-1 on this corpus, so nine of
these queries are *expected* to rank something else first, and which nine
is not a property anyone chose -- query 1 answers "rural property with a
small lake" with a photo of fish ponds, and that is a known cost of a
512-dimensional general-purpose checkpoint chosen for speed (RFC-023
section 3.6), not a defect in search. Recall@5 and hard-negative
contamination over all 25 queries do move when retrieval genuinely
breaks, and do not move when one photo of a pond overtakes another.

The floors below were measured before they were written down (RFC-025
section 11), never estimated first and confirmed afterwards.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import delete
from sqlalchemy.orm import Session
from tests.conftest import make_test_device

from app.application.use_cases.index_or_update_images import (
    IndexOrUpdateImagesUseCase,
)
from app.application.use_cases.search_images import SearchImagesUseCase
from app.domain.entities.image import Image
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.search_hit import SearchHit
from app.infrastructure.ai.clip_embedding_model import ClipEmbeddingModel
from app.infrastructure.config.constants import SUPPORTED_IMAGE_EXTENSIONS
from app.infrastructure.database.models.device_model import DeviceModel
from app.infrastructure.database.models.image_model import ImageModel
from app.infrastructure.filesystem.filesystem_image_provider import (
    FilesystemImageProvider,
)
from app.infrastructure.filesystem.image_identity import compute_image_id
from app.infrastructure.filesystem.sha256_content_hasher import Sha256ContentHasher
from app.infrastructure.persistence.engine import EngineInstance
from app.infrastructure.persistence.postgres_device_repository import (
    PostgresDeviceRepository,
)
from app.infrastructure.persistence.postgres_image_repository import (
    PostgresImageRepository,
)
from app.infrastructure.workers.indexing_worker import IndexingWorker
from dataset_tools.manifest import DEMO_CORPUS_ROOT, DEMO_MANIFEST_PATH, load_manifest
from dataset_tools.materialize import materialize

pytestmark = pytest.mark.slow

QUERIES_PATH = DEMO_MANIFEST_PATH.parent / "queries.json"

# Measured first, written down second (RFC-025 section 11.4). The run of
# 2026-08-22 with laion/CLIP-ViT-B-32-laion2B-s34B-b79K on CPU produced:
# Recall@5 84.0% (21/25), top-5 hard-negative contamination 12.8%
# (16/125 slots), strict top-1 64.0% (16/25), pairwise 80.4%.
#
# Each gate sits two queries away from what was measured -- 8 points, on a
# 25-query set where one query is worth 4. One query flipping is model
# noise (a torch or transformers upgrade moving float arithmetic, a
# re-materialized corpus); two flipping the same way is a signal. A real
# break in the query pipeline -- no translation, the wrong prompt
# template, the wrong distance operator, results returned unranked --
# does not cost two queries, it costs ten.
RECALL_AT_5_FLOOR = 0.76
TOP_5_CONTAMINATION_CEILING = 0.20
STRICT_TOP_1_FLOOR = 0.56

# The bake-off's strict top-1 for this checkpoint with the `a photo of
# {query}` template, from `clip_template_run_output.log`. RFC-025 computes
# the same quantity over the same corpus through pgvector instead of a
# numpy cosine loop, so a material gap here is an implementation defect,
# not a model result (RFC-025 section 11.3).
BAKEOFF_STRICT_TOP_1 = 0.60
BAKEOFF_PAIRWISE = 0.804
PAIRWISE_FLOOR = 0.72

# One image in the demo corpus is 45 rows, so a search that asks for all
# of them ranks the entire corpus -- which is what the pairwise metric
# needs, since it compares the position of specific hard negatives.
CORPUS_SIZE = 45


@dataclass(frozen=True)
class QueryOutcome:
    """One ground-truth query and the ranking search actually produced."""

    query: str
    relevant: frozenset[str]
    hard_negatives: frozenset[str]
    ranked_paths: tuple[str, ...]
    similarities: tuple[float, ...]

    def rank_of(self, relative_path: str) -> int:
        return self.ranked_paths.index(relative_path)


def load_queries() -> list[dict[str, Any]]:
    raw = json.loads(QUERIES_PATH.read_text(encoding="utf-8"))
    queries: list[dict[str, Any]] = raw["queries"]
    return queries


def recall_at_k(outcomes: list[QueryOutcome], k: int) -> float:
    """Fraction of queries whose top `k` contains at least one relevant image.

    The "did the user find what they searched for" reading of recall,
    chosen over `|relevant hit| / |relevant|` because the ground truth
    declares between one and five relevant images per query: the ratio
    form would score a query with five correct answers out of five
    possible lower than a query with one out of one, for reasons about
    how the dataset was written rather than about retrieval.
    `relevant_coverage_at_k()` reports the ratio form alongside it.
    """
    if not outcomes:
        return 0.0
    found = sum(
        1
        for outcome in outcomes
        if outcome.relevant.intersection(outcome.ranked_paths[:k])
    )
    return found / len(outcomes)


def relevant_coverage_at_k(outcomes: list[QueryOutcome], k: int) -> float:
    """Mean fraction of each query's relevant images that reached the top `k`.

    Capped by `k` itself: a query declaring five relevant images cannot
    place more than five of them in a top-5, so the denominator is
    `min(len(relevant), k)` and a perfect score stays reachable.
    """
    if not outcomes:
        return 0.0
    coverages = [
        len(outcome.relevant.intersection(outcome.ranked_paths[:k]))
        / min(len(outcome.relevant), k)
        for outcome in outcomes
    ]
    return sum(coverages) / len(coverages)


def hard_negative_contamination_at_k(outcomes: list[QueryOutcome], k: int) -> float:
    """Fraction of all top-`k` slots occupied by a declared hard negative.

    The hard negatives are the images chosen to be *plausibly* wrong --
    fish ponds for a lake query, a swimming pool for a water-body query.
    Recall alone cannot see them: a result list can hold a relevant image
    at rank 1 and four near-misses behind it, which is exactly the failure
    a semantic index is supposed to avoid.
    """
    if not outcomes:
        return 0.0
    contaminated = sum(
        len(outcome.hard_negatives.intersection(outcome.ranked_paths[:k]))
        for outcome in outcomes
    )
    return contaminated / (len(outcomes) * k)


def strict_top_1(outcomes: list[QueryOutcome]) -> float:
    """Fraction of queries whose first result is a declared relevant image.

    Deliberately the same definition the RFC-023 bake-off used, so the two
    numbers are comparable.
    """
    if not outcomes:
        return 0.0
    hits = sum(1 for outcome in outcomes if outcome.ranked_paths[0] in outcome.relevant)
    return hits / len(outcomes)


def pairwise_accuracy(outcomes: list[QueryOutcome]) -> float:
    """Fraction of (relevant, hard-negative) pairs ranked in the right order.

    The bake-off's second metric, and the more sensitive of the two: it
    reads the whole ranking rather than only its head, so a change that
    moves relevant images up without reaching rank 1 still shows.
    """
    correct = 0
    total = 0
    for outcome in outcomes:
        for relevant in outcome.relevant:
            for negative in outcome.hard_negatives:
                total += 1
                if outcome.rank_of(relevant) < outcome.rank_of(negative):
                    correct += 1
    return correct / total if total else 0.0


@pytest.fixture(scope="module")
def embedding_model() -> ClipEmbeddingModel:
    """One adapter for the whole module: indexing and every query share it.

    The checkpoint loads once (~5 s) and the Marian translator once more,
    on first use. A function-scoped model would pay both per test, and
    would also make the search tests measure a differently-loaded model
    than the one that produced the stored embeddings.
    """
    return ClipEmbeddingModel()


@dataclass(frozen=True)
class IndexedCorpus:
    """The demo corpus, indexed with real embeddings, in a real database."""

    repository: PostgresImageRepository
    root: Path
    paths_by_id: dict[str, str]

    def relative_path_of(self, image: Image) -> str:
        return self.paths_by_id[str(image.id)]

    def relative_paths(self, hits: list[SearchHit]) -> tuple[str, ...]:
        return tuple(self.relative_path_of(hit.image) for hit in hits)


@pytest.fixture(scope="module")
def indexed_corpus(
    tmp_path_factory: pytest.TempPathFactory, embedding_model: ClipEmbeddingModel
) -> Iterator[IndexedCorpus]:
    """Index all 45 demo photos with real CLIP once, into an emptied table.

    Module-scoped because inference is the entire cost: ~17 s for the
    corpus at RFC-024's measured throughput, which is not a price worth
    paying per test.

    Isolation is the same trick `db_session` uses, hoisted to module
    scope: one outer transaction, `DELETE FROM images` inside it, and an
    unconditional rollback at the end, so the development database is
    untouched. The delete is what makes the metrics meaningful at all --
    every assertion here is a top-K query over the whole table, so an
    unrelated committed row can displace a correct answer (see
    `empty_db_session` in `tests/conftest.py` for the full rationale and
    its caveat). Module rather than session scope keeps that lock from
    spanning other modules' database tests.
    """
    manifest = load_manifest(DEMO_MANIFEST_PATH)
    manifest.verify_matches_directory(DEMO_CORPUS_ROOT)
    root = materialize(
        manifest, DEMO_CORPUS_ROOT, tmp_path_factory.mktemp("corpus") / "demo"
    )

    connection = EngineInstance.connect()
    transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")

    try:
        session.execute(delete(ImageModel))
        session.execute(delete(DeviceModel))
        session.commit()
        # RFC-027: `images.device_id` is a NOT NULL foreign key, so the
        # corpus needs a device before a single row can be written. The
        # corpus root stands in for a whole volume, which also keeps the
        # ids below independent of where pytest put the temporary
        # directory.
        device = make_test_device()
        PostgresDeviceRepository(session).save(device)

        repository = PostgresImageRepository(session)
        worker = IndexingWorker(
            filesystem_provider=FilesystemImageProvider(
                root, SUPPORTED_IMAGE_EXTENSIONS
            ),
            index_or_update_images_use_case=IndexOrUpdateImagesUseCase(
                repository=repository,
                embedding_model=embedding_model,
                content_hasher=Sha256ContentHasher(),
                batch_size=8,
                metadata_prefetch_size=512,
            ),
            device=device,
            mount_point=root,
        )
        summary = worker.run()
        assert summary.indexed == len(manifest.images), summary
        assert not summary.failures, summary.failures

        paths_by_id = {
            str(compute_image_id(device.id, ImagePath(entry.relative_path))): (
                entry.relative_path
            )
            for entry in manifest.images
        }

        yield IndexedCorpus(repository=repository, root=root, paths_by_id=paths_by_id)
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture(scope="module")
def search_use_case(
    indexed_corpus: IndexedCorpus, embedding_model: ClipEmbeddingModel
) -> SearchImagesUseCase:
    return SearchImagesUseCase(
        repository=indexed_corpus.repository,
        embedding_model=embedding_model,
        default_limit=5,
    )


@pytest.fixture(scope="module")
def outcomes(
    indexed_corpus: IndexedCorpus, search_use_case: SearchImagesUseCase
) -> list[QueryOutcome]:
    """Run all 25 ground-truth queries once and keep the full ranking.

    Asks for the whole corpus rather than five results because the
    pairwise metric needs the position of images that never reach the top
    five. Every metric below is computed from these rankings, so the
    tests cannot disagree with each other about what search returned.
    """
    results = []
    for entry in load_queries():
        hits = search_use_case.execute(entry["query"], limit=CORPUS_SIZE)
        results.append(
            QueryOutcome(
                query=entry["query"],
                relevant=frozenset(entry["relevant"]),
                hard_negatives=frozenset(entry["hard_negatives"]),
                ranked_paths=indexed_corpus.relative_paths(hits),
                similarities=tuple(hit.similarity for hit in hits),
            )
        )
    return results


def test_every_indexed_image_is_reachable_by_search(
    outcomes: list[QueryOutcome],
) -> None:
    """A search for the whole corpus returns the whole corpus, once each.

    The cheapest possible check that the join between "what was indexed"
    and "what is searchable" is total: a `WHERE embedding IS NOT NULL`
    that silently excluded rows, or a limit applied before ordering,
    shows up here before any quality metric has to interpret it.
    """
    for outcome in outcomes:
        assert len(outcome.ranked_paths) == CORPUS_SIZE
        assert len(set(outcome.ranked_paths)) == CORPUS_SIZE


def test_results_are_ordered_by_descending_similarity(
    outcomes: list[QueryOutcome],
) -> None:
    for outcome in outcomes:
        scores = list(outcome.similarities)
        assert scores == sorted(scores, reverse=True), outcome.query


def test_similarities_are_cosine_scores_in_the_documented_range(
    outcomes: list[QueryOutcome],
) -> None:
    """CLIP text-image similarities are compressed, and that is expected.

    Measured over all 1,125 (query, image) pairs: scores span
    **[-0.1183, +0.3737]**, mean +0.1524, and the best hit for a query
    averages +0.3003 (RFC-025 section 9). Nowhere near the ~0.9 people
    expect from a "similarity", because CLIP's text and image towers
    occupy different cones of the shared space -- and genuinely negative,
    which is why nothing in this stack clamps the score into [0, 1].

    The assertion is deliberately wider than the measurement. Its job is
    to catch a number that is not a cosine at all -- a raw pgvector
    distance in [0, 2], a rescaled [0, 1] value, a dot product of
    unnormalized vectors -- not to pin the model to four decimal places.
    """
    for outcome in outcomes:
        for similarity in outcome.similarities:
            assert -1.0 <= similarity <= 1.0, outcome.query
        assert outcome.similarities[0] > 0.1, outcome.query


def test_recall_at_5_over_the_ground_truth_queries(
    outcomes: list[QueryOutcome],
) -> None:
    """The headline metric: does a five-result page contain a right answer?

    The floor sits below the measured value by more than two queries'
    worth (one query is 4 points), so ordinary churn -- a torch or
    transformers upgrade nudging float arithmetic, a re-materialized
    corpus -- cannot fail it, while a genuine break in the query pipeline
    (no translation, wrong template, wrong operator, unranked results)
    collapses it far past the floor.
    """
    measured = recall_at_k(outcomes, k=5)

    assert measured >= RECALL_AT_5_FLOOR, (
        f"Recall@5 fell to {measured:.1%}, below the {RECALL_AT_5_FLOOR:.0%} "
        f"floor measured for RFC-025"
    )


def test_hard_negative_contamination_stays_bounded(
    outcomes: list[QueryOutcome],
) -> None:
    """Recall can stay high while the rest of the page fills with near-misses.

    This is the metric that would catch an index that had degenerated
    into "anything vaguely aerial", which recall alone would not.
    """
    measured = hard_negative_contamination_at_k(outcomes, k=5)

    assert measured <= TOP_5_CONTAMINATION_CEILING, (
        f"{measured:.1%} of top-5 slots held hard negatives, above the "
        f"{TOP_5_CONTAMINATION_CEILING:.0%} ceiling measured for RFC-025"
    )


def test_ranking_agrees_with_the_rfc_023_bake_off(
    outcomes: list[QueryOutcome],
) -> None:
    """The same quantity, computed a second way, lands in the same place.

    The bake-off ranked these 45 images against these 25 queries with a
    numpy cosine loop in a separate virtualenv and recorded 60.0% strict
    top-1 for this checkpoint and template. RFC-025 recomputes it through
    the production path -- L2-normalized vectors in a `vector(512)`
    column, ordered by pgvector's `<=>` -- and measured 64.0%: one query
    of difference on a 25-query set, where one query is 4 points.

    That gap is the resolution of the instrument, not a disagreement. The
    pairwise metric, which reads the entire ranking rather than only its
    head, reproduced the bake-off's 80.4% exactly (see the test below) --
    which is the stronger statement, and the one that would have exposed
    a wrong operator, a lost normalization, or a truncated vector.
    """
    measured = strict_top_1(outcomes)

    assert measured >= STRICT_TOP_1_FLOOR, (
        f"strict top-1 is {measured:.1%}, against {BAKEOFF_STRICT_TOP_1:.0%} "
        f"measured by the RFC-023 bake-off over the same corpus and queries"
    )


def test_pairwise_ordering_reproduces_the_bake_off(
    outcomes: list[QueryOutcome],
) -> None:
    """The tightest cross-check RFC-025 has against an independent path.

    Pairwise accuracy asks, for every (relevant, hard-negative) pair,
    whether the relevant image outranks the near-miss -- 3,150 comparisons
    reading the whole 45-image ranking, not just its first slot. The
    bake-off's numpy loop scored 80.4%; this ranking, produced by
    pgvector over stored vectors, scores 80.4%.

    Two implementations agreeing to a tenth of a point over the same
    inputs is what "the database is computing the cosine we think it is"
    looks like as a measurement. The floor is loose because the metric is
    a check on the implementation, not a quality gate -- Recall@5 is the
    quality gate.
    """
    measured = pairwise_accuracy(outcomes)

    assert measured >= PAIRWISE_FLOOR, (
        f"pairwise ordering is {measured:.1%}, against "
        f"{BAKEOFF_PAIRWISE:.1%} measured by the RFC-023 bake-off"
    )


def test_a_full_sentence_portuguese_query_finds_the_same_images(
    search_use_case: SearchImagesUseCase, indexed_corpus: IndexedCorpus
) -> None:
    """One smoke test for the translation half of the pipeline.

    Full sentence, not a single word, and that is not incidental:
    RFC-023 section 7.1 measured `langdetect` classifying `fazenda` as
    Turkish and `lago` as Tagalog, so a one-word Portuguese query reaches
    CLIP untranslated and this test would be measuring the raw-Portuguese
    path while claiming to measure translation.

    Only one Portuguese query exists, deliberately. Ground truth in
    Portuguese would be a second dataset to maintain, and the bake-off
    already quantified the language gap (56.0% strict-PT(MT) against
    60.0% strict-EN); what is unproven until something like this runs is
    that the translator is wired into the *search* path at all.
    """
    portuguese = "uma propriedade rural com um lago"
    english = "a rural property with a lake"

    portuguese_hits = indexed_corpus.relative_paths(
        search_use_case.execute(portuguese, limit=5)
    )
    english_hits = indexed_corpus.relative_paths(
        search_use_case.execute(english, limit=5)
    )

    assert "images/aerial/rural/lake_property_01.jpg" in portuguese_hits
    # Not an identical ranking -- the translation is a different English
    # string ("Rural property with a lake") and CLIP is entitled to score
    # it differently. Overlap is the claim: the query was understood.
    assert len(set(portuguese_hits) & set(english_hits)) >= 3
