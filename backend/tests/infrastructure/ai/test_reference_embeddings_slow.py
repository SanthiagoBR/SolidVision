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

RFC-047 changed on purpose what some of the Portuguese queries send to the
translator, and the file is **not** rewritten (RFC-047 section 8): it is the
proof that the load by directory gave the vector the load by name gave. So
the two things are kept apart. The vector of every *recorded prompt* is
still compared, which proves the encoder; the prompt of each query is
compared with the recorded one only where RFC-047 does not touch it, and
with `CHANGED_BY_RFC_047` where it does.

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
from langdetect import LangDetectException, detect
from tests.conftest import materialized_embedding_model

from app.domain.entities.image import Image
from app.domain.value_objects.device_id import DeviceId
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.infrastructure.ai.clip_embedding_model import (
    TEXT_PROMPT_TEMPLATE,
    ClipEmbeddingModel,
)
from app.infrastructure.ai.model_runtime import CLIP, default_runtime, read_lockfile
from app.infrastructure.ai.query_translator import QueryTranslator, marian_input
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


CHANGED_BY_RFC_047 = {
    "propriedade em encosta com piscina": ("a photo of property on hillside with pool"),
    "piscina em uma casa rural, nao um corpo d'agua natural ou artificial": (
        "a photo of swimming pool in a rural house,"
        " not a natural or artificial body of water"
    ),
    "tanques artificiais de piscicultura em um vale": (
        "a photo of artificial fish tanks in a mountain valley"
    ),
    "galpao industrial ao lado de terras agricolas": (
        "a photo of Industrial shed next to agricultural land"
    ),
    "galpao ao lado de uma rodovia, nao um galpao dentro de uma cidade": (
        "a photo of shed next to a highway, not a shed inside a city"
    ),
    "terreno de obra limpo com terra vermelha exposta": (
        "a photo of clean construction land with exposed red land"
    ),
    "terreno de construcao recem limpo com terra vermelha em terracos": (
        "a photo of land of construction clean reme with red land on terracotta"
    ),
    "cruzamento na cidade com uma concessionaria de carros": (
        "a photo of crossing in the city with a car dealership"
    ),
    "cobertura de concessionaria com carros a venda,"
    " nao um estacionamento em uma rua comum": (
        "a photo of dealership coverage with cars for sale,"
        " not a parking lot on a common street"
    ),
    "oficinas e pequenos comercios ao longo de uma rua comercial": (
        "a photo of workshops and small shops along a shopping street"
    ),
    "rua residencial ladeada por casas": (
        "a photo of residential street flanked by houses"
    ),
    "vista geral de uma cidade em encosta com casas densas": (
        "a photo of overview of a city on hillside with dense houses"
    ),
    "vista ampla de drone de uma cidade em encosta"
    " com um grande complexo de predios": (
        "a photo of wide drone view of a city on hillside"
        " with a large complex of buildings"
    ),
    "uma xicara de cafe de ceramica com pires": (
        "a photo of a cup of ceramic coffee with pyres"
    ),
}
"""The recorded queries whose text to the translator RFC-047 changed, and
the prompt each gives now (RFC-047 section 8): 14 of the 25 Portuguese, of
which 9 change prompt. One of them for the worse -- "construcao" goes
accented next to an unaccented "recem" (RFC-047 section 15)."""


def touched_by_rfc_047(query: str) -> bool:
    """Whether RFC-047 changed what the translator receives for `query`:
    the rule before it was `langdetect` alone, and the text as typed."""
    try:
        before = query if detect(query) == "pt" else None
    except LangDetectException:
        before = None
    return marian_input(query) != before


class Verbatim(QueryTranslator):
    """Hands the text on as it is: to encode a recorded prompt itself."""

    def to_english(self, text: str) -> str:
        return text


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


def test_the_queries_rfc_047_touches_are_the_ones_listed(reference: Any) -> None:
    """No other recorded query changed what it sends, and none listed
    stopped changing: the list is the lexicon's effect, measured."""
    touched = {
        str(query) for query in reference["queries"] if touched_by_rfc_047(str(query))
    }

    assert touched == set(CHANGED_BY_RFC_047)


def test_the_queries_translate_to_the_same_strings(
    clip: ClipEmbeddingModel, reference: Any
) -> None:
    """Before the vectors: a different translation would explain a different
    vector, and the failure should say which of the two moved. A query
    RFC-047 touches is compared with what the lexicon sends now."""
    queries = [str(query) for query in reference["queries"]]
    expected = [
        CHANGED_BY_RFC_047.get(query, str(prompt))
        for query, prompt in zip(queries, reference["prompts"], strict=True)
    ]

    assert [clip.build_prompt(query) for query in queries] == expected


def test_the_recorded_prompts_land_on_the_same_vectors(reference: Any) -> None:
    """The encoder, apart from the translator: each recorded prompt, encoded
    as it is, against the vector recorded for it."""
    verbatim = ClipEmbeddingModel(default_runtime().loader(CLIP), translator=Verbatim())
    prefix = TEXT_PROMPT_TEMPLATE.format(query="")
    found = vectors(
        [
            verbatim.encode_text(str(prompt).removeprefix(prefix))
            for prompt in reference["prompts"]
        ]
    )

    compare(found, reference["query_vectors"], reference)


def test_the_untouched_queries_land_on_the_same_vectors(
    clip: ClipEmbeddingModel, reference: Any
) -> None:
    """The whole path, translator included, where RFC-047 changed nothing."""
    rows = [
        index
        for index, query in enumerate(reference["queries"])
        if str(query) not in CHANGED_BY_RFC_047
    ]
    found = vectors(
        [clip.encode_text(str(reference["queries"][index])) for index in rows]
    )

    compare(found, reference["query_vectors"][rows], reference)
