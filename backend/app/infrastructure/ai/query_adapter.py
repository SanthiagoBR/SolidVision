"""RFC-042 level A: one matrix applied to the query's vector.

A search's text vector `t`, already normalized, becomes `normalize(t + A t)`
before it is compared with the photos. The photos' vectors are not touched,
so nothing in the index changes and nothing is reindexed (RFC-042 section 9).
A picture used as the query (search by image, "find similar") is not adapted
either, because `A` was trained on text.

**The matrix is code**, like the lexicon (RFC-047 section 4). It lives in
`query_adapter.safetensors`, beside this module, is versioned with it, and is
read as data of this package. Its header names what it was trained for:

- `clip_repository` and `clip_revision`: the lockfile's CLIP. Another
  checkpoint makes other vectors, on which this matrix means nothing, so
  the load refuses it.
- `lexicon_sha256`: the lexicon its training searches were translated with.
  A test holds this one, not the load. A lexicon change is a commit, and the
  test fails on that commit until the matrix is trained again and measured
  (RFC-042 section 16).

`experiments/rfc-042-fine-tuning/export_adapter.py` writes the file from a
`train_adapter.py` matrix.
"""

from __future__ import annotations

import hashlib
import json
import struct
from collections.abc import Mapping
from importlib import resources
from pathlib import Path

import torch
from safetensors.torch import load as load_tensors

from app.infrastructure.ai.model_runtime import CLIP, read_lockfile

ADAPTER_NAME = "query_adapter.safetensors"
MATRIX_KEY = "matrix"


class QueryAdapterError(Exception):
    """The adapter file is not one this version of the code can use."""


def lexicon_digest(content: bytes) -> str:
    """The lexicon's sha256 with normalized line endings, as the header records it."""
    return hashlib.sha256(content.replace(b"\r\n", b"\n")).hexdigest()


def header_metadata(content: bytes) -> dict[str, str]:
    """The `__metadata__` of a safetensors file.

    The file starts with the header's length (8 bytes, little-endian) and
    then the header itself, which is JSON.
    """
    (length,) = struct.unpack("<Q", content[:8])
    header = json.loads(content[8 : 8 + length])
    return {str(k): str(v) for k, v in header.get("__metadata__", {}).items()}


class QueryAdapter:
    """The level A matrix, checked against the CLIP it was trained for."""

    def __init__(self, matrix: torch.Tensor, metadata: Mapping[str, str]) -> None:
        if (
            matrix.ndim != 2
            or matrix.shape[0] != matrix.shape[1]
            or matrix.dtype != torch.float32
        ):
            raise QueryAdapterError(
                f"the adapter must be a square float32 matrix, not"
                f" {matrix.dtype} {tuple(matrix.shape)}"
            )
        self.matrix = matrix
        self.metadata = dict(metadata)

    @property
    def dimension(self) -> int:
        return int(self.matrix.shape[0])

    @classmethod
    def load(cls, path: Path | None = None) -> QueryAdapter:
        """The adapter in `path`, or by default the one of this version."""
        if path is None:
            source = resources.files("app.infrastructure.ai") / ADAPTER_NAME
            name, content = ADAPTER_NAME, source.read_bytes()
        else:
            name, content = path.name, path.read_bytes()
        metadata = header_metadata(content)
        clip = read_lockfile().models[CLIP]
        trained_for = (metadata.get("clip_repository"), metadata.get("clip_revision"))
        if trained_for != (clip.repository, clip.revision):
            raise QueryAdapterError(
                f"{name} was trained for CLIP {trained_for[0]} at {trained_for[1]},"
                f" and the lockfile's is {clip.repository} at {clip.revision}:"
                " train it again (RFC-042 section 16)"
            )
        matrix = load_tensors(content).get(MATRIX_KEY)
        if matrix is None:
            raise QueryAdapterError(f"{name} has no tensor named {MATRIX_KEY!r}")
        return cls(matrix, metadata)

    def apply(self, vectors: torch.Tensor) -> torch.Tensor:
        """`normalize(t + t A^T)` per row, what `train_adapter.adapt` trained."""
        matrix = self.matrix.to(vectors.device)
        return torch.nn.functional.normalize(
            vectors + vectors @ matrix.T, p=2.0, dim=-1
        )
