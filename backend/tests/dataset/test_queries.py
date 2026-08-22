"""Structural tests for `dataset/demo/queries.json` (RFC-022 5.3).

What lives here is everything about the ground truth that is true
regardless of which embedding model is loaded: the referenced paths exist
in the manifest, relevant and hard-negative sets never overlap, query
texts are unique, and no aerial image is orphaned from the eval set.
Those checks depend on nothing but the two JSON files, so they belong in
the fast offline suite and run on every `pytest`.

**Retrieval quality moved out, and is no longer dormant.** RFC-022 shipped
this dataset with a hand-rolled cosine loop marked `xfail`, blocked first
on the absence of a semantic model (removed by RFC-023) and then on the
absence of a search: `SearchImagesUseCase` encoded the query, discarded
the embedding, and returned `repository.list()` unranked. Its docstring
said that when vector search landed, the test to write was one that
exercised `SearchImagesUseCase` end to end rather than a cosine loop
standing in for it.

RFC-025 landed that search, and that test is
`tests/dataset/test_semantic_search_e2e.py`: real CLIP, real PostgreSQL,
real pgvector, all 25 queries, scored as Recall@5 and hard-negative
contamination against measured regression floors. It is marked `slow`
because it loads real checkpoints -- which is exactly why the cosine loop
could not simply be pointed at the real model and left in this file.
"""

from __future__ import annotations

import json
from typing import Any

from dataset_tools.manifest import DEMO_MANIFEST_PATH, load_manifest

QUERIES_PATH = DEMO_MANIFEST_PATH.parent / "queries.json"


def _load_queries() -> list[dict[str, Any]]:
    raw = json.loads(QUERIES_PATH.read_text(encoding="utf-8"))
    queries: list[dict[str, Any]] = raw["queries"]
    return queries


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
