"""Tests for Portuguese query handling (RFC-023 section 8).

Nothing here downloads a model. The Marian checkpoint is substituted with a
stub so the routing decision -- translate or don't -- can be asserted
deterministically and offline; the real checkpoint is exercised only by the
`slow`-marked tests in `test_clip_embedding_model_slow.py`.
"""

from __future__ import annotations

from typing import Any

import pytest
import torch
from langdetect import DetectorFactory

from app.infrastructure.ai.query_translator import (
    PORTUGUESE_TOKEN,
    TRANSLATION_MODEL,
    MarianQueryTranslator,
    QueryTranslator,
)


class StubTokenizer:
    """Stands in for `MarianTokenizer`, recording what it was asked to encode."""

    def __init__(self) -> None:
        self.encoded: list[str] = []

    def __call__(self, texts: list[str], **kwargs: Any) -> dict[str, torch.Tensor]:
        self.encoded.extend(texts)
        return {"input_ids": torch.ones(1, 4, dtype=torch.long)}

    def batch_decode(self, generated: Any, **kwargs: Any) -> list[str]:
        return ["  a translated english query  "]


class StubTranslationModel:
    """Stands in for `MarianMTModel`, counting generate calls."""

    def __init__(self) -> None:
        self.generate_calls = 0

    def eval(self) -> StubTranslationModel:
        return self

    def to(self, device: torch.device) -> StubTranslationModel:
        return self

    def generate(self, **kwargs: Any) -> torch.Tensor:
        self.generate_calls += 1
        return torch.ones(1, 4, dtype=torch.long)


class StubLoads:
    """Bundles the stubs and counts how often each checkpoint was loaded."""

    def __init__(self) -> None:
        self.tokenizer = StubTokenizer()
        self.model = StubTranslationModel()
        self.tokenizer_loads = 0
        self.model_loads = 0
        self.requested_names: list[str] = []


@pytest.fixture()
def stub_marian(monkeypatch: pytest.MonkeyPatch) -> StubLoads:
    stubs = StubLoads()

    def load_tokenizer(name: str, **kwargs: Any) -> StubTokenizer:
        stubs.tokenizer_loads += 1
        stubs.requested_names.append(name)
        return stubs.tokenizer

    def load_model(name: str, **kwargs: Any) -> StubTranslationModel:
        stubs.model_loads += 1
        stubs.requested_names.append(name)
        return stubs.model

    # Patched by dotted path: mypy's strict no-implicit-reexport forbids
    # reaching through the translator module for the transformers symbols.
    monkeypatch.setattr(
        "app.infrastructure.ai.query_translator.AutoTokenizer.from_pretrained",
        load_tokenizer,
    )
    monkeypatch.setattr(
        "app.infrastructure.ai.query_translator.AutoModelForSeq2SeqLM.from_pretrained",
        load_model,
    )
    return stubs


def _translator() -> MarianQueryTranslator:
    return MarianQueryTranslator(device=torch.device("cpu"))


def test_marian_translator_implements_the_translator_contract() -> None:
    assert isinstance(_translator(), QueryTranslator)


def test_english_text_is_returned_unchanged(stub_marian: StubLoads) -> None:
    query = "rural property with a small lake"

    assert _translator().to_english(query) == query


def test_english_text_never_loads_the_translation_model(
    stub_marian: StubLoads,
) -> None:
    """An English-only deployment must not pay for a model it never uses."""
    _translator().to_english("commercial street with shops and storefronts")

    assert stub_marian.model_loads == 0
    assert stub_marian.model.generate_calls == 0


def test_portuguese_text_is_routed_through_translation(
    stub_marian: StubLoads,
) -> None:
    result = _translator().to_english("propriedade rural com um pequeno lago")

    assert stub_marian.model.generate_calls == 1
    assert result == "a translated english query"


def test_portuguese_input_is_tagged_with_the_source_language_token(
    stub_marian: StubLoads,
) -> None:
    """opus-mt-ROMANCE-en is many-to-one; `>>por<<` picks Portuguese."""
    query = "piscina em uma casa rural"
    _translator().to_english(query)

    assert stub_marian.tokenizer.encoded == [f"{PORTUGUESE_TOKEN} {query}"]


def test_translation_output_is_stripped(stub_marian: StubLoads) -> None:
    result = _translator().to_english("rua comercial com lojas e vitrines")

    assert result == result.strip()


def test_undetectable_text_is_treated_as_english(stub_marian: StubLoads) -> None:
    """`langdetect` raises on featureless input; that must not fail the query."""
    assert _translator().to_english("   ") == "   "
    assert _translator().to_english("12345") == "12345"
    assert stub_marian.model_loads == 0


def test_translation_model_is_loaded_once_and_reused(
    stub_marian: StubLoads,
) -> None:
    translator = _translator()

    translator.to_english("propriedade rural com um pequeno lago")
    translator.to_english("galpao industrial ao lado de uma fazenda")

    assert stub_marian.model_loads == 1
    assert stub_marian.tokenizer_loads == 1
    assert stub_marian.model.generate_calls == 2


def test_constructing_the_translator_loads_nothing(stub_marian: StubLoads) -> None:
    """Construction must stay free so dependency wiring can be eager-safe."""
    _translator()

    assert stub_marian.model_loads == 0
    assert stub_marian.tokenizer_loads == 0


def test_the_configured_checkpoint_is_the_one_the_rfc_selected(
    stub_marian: StubLoads,
) -> None:
    _translator().to_english("propriedade rural com um pequeno lago")

    assert set(stub_marian.requested_names) == {TRANSLATION_MODEL}
    assert TRANSLATION_MODEL == "Helsinki-NLP/opus-mt-ROMANCE-en"


def test_language_detection_seed_is_pinned() -> None:
    """Reproducibility across runs depends on this seed being fixed."""
    assert DetectorFactory.seed == 0


@pytest.mark.parametrize(
    "query",
    [
        "propriedade rural com um pequeno lago",
        "pequeno lago natural em uma fazenda",
        "galpao industrial ao lado de uma fazenda",
        "rua comercial com lojas e vitrines",
    ],
)
def test_full_sentence_portuguese_queries_are_detected(
    query: str, stub_marian: StubLoads
) -> None:
    _translator().to_english(query)

    assert stub_marian.model.generate_calls == 1


@pytest.mark.parametrize(
    "query",
    [
        "rural property with a small lake",
        "a bicycle parked on a street",
        "commercial street with shops and storefronts",
        "aerial view of a town",
    ],
)
def test_full_sentence_english_queries_are_not_translated(
    query: str, stub_marian: StubLoads
) -> None:
    assert _translator().to_english(query) == query
    assert stub_marian.model.generate_calls == 0


def test_single_word_portuguese_is_a_known_detection_gap(
    stub_marian: StubLoads,
) -> None:
    """Pins the limitation RFC-023 section 8 documents rather than hiding it.

    `langdetect` needs a few words. `'fazenda'` is classified Turkish and
    `'lago'` Tagalog, so neither is recognized as Portuguese and both reach
    CLIP untranslated. This test exists so that adopting a better detector
    shows up here as a deliberate, visible change rather than silently
    altering query behavior.
    """
    translator = _translator()

    assert translator.to_english("fazenda") == "fazenda"
    assert translator.to_english("lago") == "lago"
    assert stub_marian.model.generate_calls == 0
