"""Tests for `dataset/demo/queries.json` (RFC-022 5.3): retrieval ground truth.

Structural validity (paths exist, no overlap, no duplicates) is checked
unconditionally. Actual retrieval quality is not -- `queries.json` ships
dormant, since `FakeEmbeddingModel` has no semantic understanding (RFC-022
7.3) and nothing in the codebase performs real vector-similarity search
yet (`SearchImagesUseCase` is a stub that returns every image
unranked). The dormant test below is expected to fail today; it exists so
that swapping `FakeEmbeddingModel` for a real `EmbeddingModelPort`
implementation activates it with no test-code changes.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.domain.entities.image import Image
from app.domain.value_objects.image_path import ImagePath
from app.infrastructure.ai.fake_embedding_model import FakeEmbeddingModel
from app.infrastructure.filesystem.image_identity import compute_image_id
from dataset_tools.manifest import DEMO_MANIFEST_PATH, load_manifest

QUERIES_PATH = DEMO_MANIFEST_PATH.parent / "queries.json"


def _load_queries() -> list[dict[str, Any]]:
    raw = json.loads(QUERIES_PATH.read_text(encoding="utf-8"))
    queries: list[dict[str, Any]] = raw["queries"]
    return queries


def _to_image(relative_path: str) -> Image:
    """Build the domain entity the pipeline would build for a manifest path.

    No materialized file is required: `FakeEmbeddingModel` never opens
    pixels (RFC-022 7.3), so the id/path pair alone is enough to reproduce
    the same embedding a real indexing run would have stored.
    """
    path = ImagePath(relative_path)
    return Image(
        id=compute_image_id(path),
        path=path,
        filename=relative_path.rsplit("/", 1)[-1],
        extension="jpg",
    )


class TestQueriesStructuralValidity:
    """These must hold regardless of which embedding model is in use."""

    def test_queries_file_parses(self) -> None:
        assert len(_load_queries()) > 0

    def test_every_query_text_is_unique(self) -> None:
        queries = _load_queries()
        texts = [entry["query"] for entry in queries]
        assert len(texts) == len(set(texts))

    def test_every_referenced_path_exists_in_the_manifest(self) -> None:
        manifest = load_manifest(DEMO_MANIFEST_PATH)
        known_paths = {image.relative_path for image in manifest.images}

        for entry in _load_queries():
            for path in entry["relevant"] + entry["hard_negatives"]:
                assert (
                    path in known_paths
                ), f"{path!r} in query {entry['query']!r} is not in manifest.json"

    def test_relevant_and_hard_negatives_never_overlap(self) -> None:
        for entry in _load_queries():
            overlap = set(entry["relevant"]) & set(entry["hard_negatives"])
            assert not overlap, f"query {entry['query']!r}: {overlap}"

    def test_every_query_has_at_least_one_relevant_and_one_hard_negative(
        self,
    ) -> None:
        for entry in _load_queries():
            assert len(entry["relevant"]) >= 1, entry["query"]
            assert len(entry["hard_negatives"]) >= 1, entry["query"]

    def test_every_domain_entry_is_relevant_for_at_least_one_query(self) -> None:
        """Nothing in the aerial corpus should be absent from the eval set.

        Images tagged `trivial-negative` (the `images/everyday/` bucket) are
        deliberately excluded: they exist to be irrelevant to every query,
        so requiring one to appear in some query's `relevant` list would
        contradict their purpose rather than test anything.
        """
        manifest = load_manifest(DEMO_MANIFEST_PATH)
        referenced: set[str] = set()
        for entry in _load_queries():
            referenced.update(entry["relevant"])

        domain_paths = {
            image.relative_path
            for image in manifest.images
            if "trivial-negative" not in image.tags
        }
        orphaned = domain_paths - referenced
        assert not orphaned, orphaned


@pytest.mark.xfail(
    reason=(
        "FakeEmbeddingModel has no semantic understanding of image content "
        "(RFC-022 7.3) -- activates once a real EmbeddingModelPort "
        "implementation (e.g. SigLIP) replaces it, with no test-code change."
    ),
    strict=False,
)
def test_relevant_images_rank_above_hard_negatives() -> None:
    """The retrieval quality this dataset exists to eventually measure."""
    model = FakeEmbeddingModel()

    for entry in _load_queries():
        query_vector = model.encode_text(entry["query"])

        def similarity(relative_path: str) -> float:
            image = _to_image(relative_path)
            embedding = model.encode_image(image)
            return sum(x * y for x, y in zip(query_vector.values, embedding.values))

        relevant_scores = [similarity(path) for path in entry["relevant"]]
        hard_negative_scores = [similarity(path) for path in entry["hard_negatives"]]

        assert min(relevant_scores) > max(hard_negative_scores), entry["query"]
