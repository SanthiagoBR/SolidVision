"""Level 3 of RFC-026's three: a real HTTP request, all the way down.

Real CLIP, real PostgreSQL, real pgvector, real ground truth, over HTTP.
Marked `slow` and therefore deselected by the default `addopts`, because
it downloads and runs real checkpoints (RFC-023 section 12.1).

**Deliberately small.** `tests/dataset/test_semantic_search_e2e.py`
already runs all 25 ground-truth queries through the real stack and gates
on measured floors -- Recall@5, hard-negative contamination, strict top-1,
pairwise. Repeating any of that through HTTP would measure the model a
second time and call the result an API test. The question here is
narrower, and it is the one nothing else can answer:

    Does a real HTTP request reach real CLIP and real pgvector and come
    back as correct JSON?

The corpus fixtures are imported from that module rather than rebuilt.
A second four-image dataset would be a parallel ground truth to maintain,
drifting from the first, to answer a question the first already answers.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tests.dataset.test_semantic_search_e2e import (
    IndexedCorpus,
    embedding_model,  # noqa: F401  -- imported so pytest can resolve the fixture
    indexed_corpus,  # noqa: F401  -- module-scoped: the corpus is indexed once
    load_queries,
)

from app.infrastructure.ai.clip_embedding_model import ClipEmbeddingModel
from app.presentation.api import app
from app.presentation.dependencies import get_embedding_model, get_image_repository

pytestmark = pytest.mark.slow

SEARCH_URL = "/api/v1/images/search"

# Query 5 of the ground truth. Chosen because it is the most robust
# English query in the set rather than the most flattering: RFC-025's run
# recorded it correct at top-1 with zero hard negatives in the top five,
# and it declares five relevant images, so a single result changing place
# cannot break the assertion. This test is about the transport, and it
# should not fail on model noise the dataset suite is already gating.
ENGLISH_QUERY_INDEX = 4

# Full sentence on purpose. RFC-023 section 7.1 measured `langdetect`
# calling `fazenda` Turkish and `lago` Tagalog, so a one-word Portuguese
# query reaches CLIP untranslated -- a test built on one would exercise
# the raw-PT path while claiming to prove translation runs over HTTP.
PORTUGUESE_QUERY = "uma propriedade rural com um lago"
ENGLISH_EQUIVALENT = "rural property with a lake"


@pytest.fixture
def client(
    indexed_corpus: IndexedCorpus,  # noqa: F811 -- the fixture imported above
    embedding_model: ClipEmbeddingModel,  # noqa: F811 -- likewise
) -> Iterator[TestClient]:
    """The production app, pointed at the indexed corpus.

    Only the two seams are overridden, and both for isolation: the
    repository so that requests read the corpus indexed inside a
    transaction that is always rolled back, and the model so that the
    checkpoint loaded once for the module is the same one that produced
    the stored embeddings. The route, the composition root, the use case,
    the encoder, and the SQL are all the real thing.
    """
    app.dependency_overrides[get_image_repository] = lambda: indexed_corpus.repository
    app.dependency_overrides[get_embedding_model] = lambda: embedding_model
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def stems(relative_paths: list[str]) -> set[str]:
    """Ground truth records repository-relative paths; the API returns stems."""
    return {Path(path).stem for path in relative_paths}


def test_an_english_query_returns_a_relevant_image_in_the_top_five(
    client: TestClient,
) -> None:
    entry = load_queries()[ENGLISH_QUERY_INDEX]

    response = client.get(SEARCH_URL, params={"q": entry["query"], "limit": 5})

    assert response.status_code == 200
    body = response.json()
    assert body["query"] == entry["query"]
    assert body["limit"] == 5
    assert len(body["results"]) == 5

    returned = {result["filename"] for result in body["results"]}
    assert returned & stems(
        entry["relevant"]
    ), f"no relevant image in the top 5 for {entry['query']!r}: {sorted(returned)}"
    assert all(-1.0 <= result["similarity"] <= 1.0 for result in body["results"])


def test_a_portuguese_sentence_is_translated_on_the_http_path(
    client: TestClient,
) -> None:
    """The translation stage is reachable through the API, not just the use case.

    Asserted as an overlap with the English equivalent rather than
    against specific filenames: what proves translation ran is that a
    Portuguese sentence and its English counterpart retrieve the same
    neighbourhood. An untranslated Portuguese query goes to CLIP's text
    tower as out-of-distribution tokens and does not land there.
    """
    portuguese = client.get(SEARCH_URL, params={"q": PORTUGUESE_QUERY, "limit": 5})
    english = client.get(SEARCH_URL, params={"q": ENGLISH_EQUIVALENT, "limit": 5})

    assert portuguese.status_code == 200
    assert english.status_code == 200
    assert portuguese.json()["query"] == PORTUGUESE_QUERY

    portuguese_ids = {result["id"] for result in portuguese.json()["results"]}
    english_ids = {result["id"] for result in english.json()["results"]}
    assert (
        portuguese_ids & english_ids
    ), "a translated query and its English equivalent returned disjoint top-5 sets"
