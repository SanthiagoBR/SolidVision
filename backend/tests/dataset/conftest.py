"""Shared fixtures for dataset-related tests (RFC-022)."""

from __future__ import annotations

from pathlib import Path

import pytest

from dataset_tools.manifest import DEMO_CORPUS_ROOT, DEMO_MANIFEST_PATH, load_manifest
from dataset_tools.materialize import materialize


@pytest.fixture()
def demo_corpus(tmp_path: Path) -> Path:
    """Materialize the committed demo corpus into an isolated directory.

    Copies every file the real manifest describes into `tmp_path` and
    stamps each with its manifest `file_modified_at`, so tests get a
    deterministic filesystem state regardless of how the repository was
    checked out (RFC-022 section 7.2).
    """
    manifest = load_manifest(DEMO_MANIFEST_PATH)
    manifest.verify_matches_directory(DEMO_CORPUS_ROOT)
    return materialize(manifest, DEMO_CORPUS_ROOT, tmp_path / "demo_corpus")
