"""RFC-042 level A: the matrix file the app ships, and the rules of its load.

The file is code, like the lexicon. The load refuses a matrix trained for
another CLIP, and these tests refuse one trained with another lexicon: a
lexicon change fails here until the matrix is trained again and measured
(RFC-042 section 16).
"""

from __future__ import annotations

import hashlib
from importlib import resources
from pathlib import Path

import pytest
import torch
from safetensors.torch import save_file

from app.infrastructure.ai.model_runtime import CLIP, read_lockfile
from app.infrastructure.ai.query_adapter import (
    ADAPTER_NAME,
    MATRIX_KEY,
    QueryAdapter,
    QueryAdapterError,
    header_metadata,
    lexicon_digest,
)
from app.infrastructure.ai.query_translator import LEXICON_NAME
from app.infrastructure.config.settings import settings


def _package_bytes(name: str) -> bytes:
    return (resources.files("app.infrastructure.ai") / name).read_bytes()


def _write(path: Path, matrix: torch.Tensor, **metadata: str) -> Path:
    clip = read_lockfile().models[CLIP]
    header = {"clip_repository": clip.repository, "clip_revision": clip.revision}
    save_file({MATRIX_KEY: matrix}, path, metadata=header | metadata)
    return path


class TestTheShippedAdapter:
    def test_it_loads_as_a_matrix_of_the_vectors_dimension(self) -> None:
        adapter = QueryAdapter.load()

        assert adapter.dimension == settings.embedding_dimension
        assert adapter.matrix.dtype == torch.float32
        assert bool(torch.isfinite(adapter.matrix).all())
        assert float(adapter.matrix.norm()) > 0

    def test_it_was_trained_for_the_lockfile_clip(self) -> None:
        clip = read_lockfile().models[CLIP]
        metadata = QueryAdapter.load().metadata

        assert metadata["clip_repository"] == clip.repository
        assert metadata["clip_revision"] == clip.revision

    def test_it_was_trained_with_this_lexicon(self) -> None:
        """Its training searches went through this lexicon. A changed lexicon
        sends other English to CLIP, and the matrix was not measured on it:
        train it again with `train_adapter.py`, measure, and write it with
        `export_adapter.py` (RFC-042 section 16)."""
        recorded = QueryAdapter.load().metadata["lexicon_sha256"]

        assert recorded == lexicon_digest(_package_bytes(LEXICON_NAME))

    def test_the_matrix_is_the_one_exported(self) -> None:
        adapter = QueryAdapter.load()
        digest = hashlib.sha256(adapter.matrix.numpy().tobytes()).hexdigest()

        assert digest == adapter.metadata["matrix_sha256"]

    def test_the_header_names_no_path(self) -> None:
        """It ships with the app, and the training photos are private."""
        metadata = header_metadata(_package_bytes(ADAPTER_NAME))

        assert not any(
            "\\" in value or ":/" in value or "Users" in value
            for value in metadata.values()
        )


class TestTheLoad:
    def test_a_matrix_for_another_clip_is_refused(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "a.safetensors", torch.zeros(4, 4), clip_revision="0")

        with pytest.raises(QueryAdapterError, match="train it again"):
            QueryAdapter.load(path)

    def test_a_file_without_the_matrix_is_refused(self, tmp_path: Path) -> None:
        clip = read_lockfile().models[CLIP]
        path = tmp_path / "a.safetensors"
        save_file(
            {"other": torch.zeros(4, 4)},
            path,
            metadata={
                "clip_repository": clip.repository,
                "clip_revision": clip.revision,
            },
        )

        with pytest.raises(QueryAdapterError, match=MATRIX_KEY):
            QueryAdapter.load(path)

    def test_a_matrix_that_is_not_square_is_refused(self) -> None:
        with pytest.raises(QueryAdapterError):
            QueryAdapter(torch.zeros(3, 4), {})

    def test_a_matrix_that_is_not_float32_is_refused(self) -> None:
        with pytest.raises(QueryAdapterError):
            QueryAdapter(torch.zeros(4, 4, dtype=torch.float64), {})

    def test_the_header_is_read_back(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "a.safetensors", torch.eye(4), note="kept")

        assert QueryAdapter.load(path).metadata["note"] == "kept"


class TestTheFormula:
    def test_a_zero_matrix_changes_nothing(self) -> None:
        vectors = torch.nn.functional.normalize(torch.randn(3, 8), dim=-1)

        adapted = QueryAdapter(torch.zeros(8, 8), {}).apply(vectors)

        assert torch.allclose(adapted, vectors, atol=1e-6)

    def test_it_is_normalize_t_plus_a_t(self) -> None:
        vectors = torch.nn.functional.normalize(torch.randn(3, 8), dim=-1)
        matrix = torch.randn(8, 8)

        adapted = QueryAdapter(matrix, {}).apply(vectors)

        by_hand = vectors + (matrix @ vectors.T).T
        expected = by_hand / by_hand.norm(dim=-1, keepdim=True)
        assert torch.allclose(adapted, expected, atol=1e-6)
        assert torch.allclose(adapted.norm(dim=-1), torch.ones(3), atol=1e-6)
