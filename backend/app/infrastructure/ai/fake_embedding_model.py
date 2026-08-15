"""Deterministic fake embedding model for development and tests."""

from __future__ import annotations

import hashlib
import math

from app.domain.entities.image import Image
from app.domain.services.embedding_model_port import EmbeddingModelPort
from app.domain.value_objects.embedding_vector import EmbeddingVector
from app.infrastructure.config.settings import settings


class FakeEmbeddingModel(EmbeddingModelPort):
    """Deterministic embedding adapter that does not use any AI libraries.

    Produces mean-centered, L2-normalized unit vectors so that cosine
    similarity between two *different* seeds spreads across a realistic
    range, the way a real embedding space does. An earlier version instead
    added a positional term that grew linearly with dimension index,
    reaching ~144 at the final dimension against a content term confined to
    (0, 1] -- every vector was effectively the same ramp plus a negligible
    perturbation, so cosine similarity between any two images was ~1.0
    regardless of content. That made the fake unsuitable for anything
    beyond pipeline plumbing tests: an HNSW recall benchmark run against it
    would have measured recall over indistinguishable vectors (see
    RFC-022 section 7.4). Vector *content* still carries no semantic
    meaning -- this is not a real embedding model -- but its geometry no
    longer trivially defeats similarity search.
    """

    def encode_image(self, image: Image) -> EmbeddingVector:
        seed = f"image::{image.id}::{image.path}".encode()
        return self._build_embedding(seed)

    def encode_text(self, text: str) -> EmbeddingVector:
        seed = f"text::{text}".encode()
        return self._build_embedding(seed)

    def _build_embedding(self, seed: bytes) -> EmbeddingVector:
        raw_values = self._deterministic_floats(seed, settings.embedding_dimension)

        mean = sum(raw_values) / len(raw_values)
        centered = [value - mean for value in raw_values]

        norm = math.sqrt(sum(value * value for value in centered))
        normalized = [value / norm for value in centered]

        return EmbeddingVector(normalized)

    @staticmethod
    def _deterministic_floats(seed: bytes, count: int) -> list[float]:
        """Expand `seed` into `count` reproducible floats spread across [-1, 1).

        Uses SHA-256 in counter mode rather than Python's built-in `hash()`,
        which is salted per-process via `PYTHONHASHSEED` and therefore not
        reproducible across runs, processes, or machines -- a hard
        requirement here, since the same image or text must always produce
        the same embedding (ARCHITECTURE.md section 20).
        """
        values: list[float] = []
        counter = 0
        while len(values) < count:
            block = hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
            for byte_value in block:
                values.append((byte_value / 127.5) - 1.0)
                if len(values) == count:
                    break
            counter += 1
        return values
