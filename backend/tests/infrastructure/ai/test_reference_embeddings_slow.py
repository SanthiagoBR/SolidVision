"""The vectors of the load before RFC-035, kept and compared (RFC-035 section 7.2).

`tests/fixtures/rfc035_reference_embeddings.npz` holds what the load by name
produced -- the 45 demo images one by one and in batches of 8, and the 25
ground-truth queries in English and in Portuguese -- written by
`experiments/rfc-035-offline-models/measure_offline_models.py
--write-reference` once the load by directory had matched it bit for bit, in
one process. Every load is compared with it here.

It catches a wrong tree, which the lockfile already refuses, and what the
lockfile does not pin: an update of `transformers` that changes the
preprocessing, or of `torch` that changes a kernel (section 11). For the
Portuguese queries the translated string is compared before the vector, so a
failure says which of the two moved.

Exact on the machine that wrote the file; within `TOLERANCE` on any other,
because nothing in `torch` promises the same bits across processors or
thread counts (section 7.3). Marked `slow`, and skipped without
`runtime\\models`.
"""

from __future__ import annotations

import json
import os
import platform
import uuid
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import torch
import transformers
from tests.conftest import materialized_embedding_model

from app.domain.entities.image import Image
from app.domain.value_objects.device_id import DeviceId
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.infrastructure.ai.clip_embedding_model import ClipEmbeddingModel
from app.infrastructure.ai.model_runtime import read_lockfile
from dataset_tools.manifest import DEMO_CORPUS_ROOT

pytestmark = pytest.mark.slow

REFERENCE = (
    Path(__file__).resolve().parents[2] / "fixtures" / "rfc035_reference_embeddings.npz"
)

TOLERANCE = 1e-5
"""The largest absolute difference accepted on another machine.

From the two measurements of section 7.3 (RFC section 15): one thread against
the default moved the batched vectors by at most 8.9e-8, and today's load lands
at most 1.5e-7 from the vectors the development database holds -- the last
bit of a float32 near 1. Both on this processor: another instruction set was
not measured, so the tolerance sits two orders of magnitude above the larger,
not one. What it exists to catch is far beyond it: a preprocessing that
changed, or another checkpoint, moves a vector by whole hundredths.
"""


@pytest.fixture(scope="module")
def reference() -> Any:
    return np.load(REFERENCE)


@pytest.fixture(scope="module")
def clip() -> ClipEmbeddingModel:
    return materialized_embedding_model()


def this_machine() -> dict[str, str]:
    return {
        "processor": platform.processor(),
        "cpu_count": str(os.cpu_count()),
        "threads": str(torch.get_num_threads()),
        "torch": torch.__version__,
        "transformers": transformers.__version__,
    }


def compare(found: np.ndarray, expected: np.ndarray, reference: Any) -> None:
    recorded = json.loads(str(reference["machine"]))
    if all(recorded.get(key) == value for key, value in this_machine().items()):
        rows = [
            i
            for i in range(len(expected))
            if found[i].tobytes() != expected[i].tobytes()
        ]
        assert (
            rows == []
        ), f"{len(rows)} vectors differ in their bits, first row {rows[:1]}"
    else:
        assert np.abs(found - expected).max() <= TOLERANCE


def image(relative_path: str) -> Image:
    path = ImagePath(str(DEMO_CORPUS_ROOT / relative_path))
    return Image(
        id=ImageId(uuid.uuid5(uuid.NAMESPACE_URL, relative_path)),
        device_id=DeviceId(uuid.UUID(int=0)),
        relative_path=path,
        absolute_path=path,
        filename=Path(relative_path).stem,
        extension=Path(relative_path).suffix.lstrip(".").lower(),
    )


def vectors(embeddings: list[Any]) -> np.ndarray:
    return np.asarray([embedding.values for embedding in embeddings], dtype=np.float32)


def test_the_reference_was_made_with_this_lockfile(reference: Any) -> None:
    """A new lockfile is a new model, and needs a new reference."""
    assert str(reference["lockfile_sha256"]) == read_lockfile().sha256


def test_the_images_one_by_one(clip: ClipEmbeddingModel, reference: Any) -> None:
    found = vectors(
        [clip.encode_image(image(str(p))) for p in reference["image_paths"]]
    )

    compare(found, reference["image_vectors"], reference)


def test_the_images_in_batches_of_eight(
    clip: ClipEmbeddingModel, reference: Any
) -> None:
    images = [image(str(path)) for path in reference["image_paths"]]
    found = vectors(
        [
            v
            for start in range(0, len(images), 8)
            for v in clip.encode_images(images[start : start + 8])
        ]
    )

    compare(found, reference["image_vectors_batched"], reference)


def test_the_queries_translate_to_the_same_strings(
    clip: ClipEmbeddingModel, reference: Any
) -> None:
    """Before the vectors: a different translation would explain a different
    vector, and the failure should say which of the two moved."""
    prompts = [clip.build_prompt(str(query)) for query in reference["queries"]]

    assert prompts == [str(prompt) for prompt in reference["prompts"]]


def test_the_queries_land_on_the_same_vectors(
    clip: ClipEmbeddingModel, reference: Any
) -> None:
    found = vectors([clip.encode_text(str(query)) for query in reference["queries"]])

    compare(found, reference["query_vectors"], reference)
