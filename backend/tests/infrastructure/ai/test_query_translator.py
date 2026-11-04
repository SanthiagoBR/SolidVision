"""Tests for Portuguese query handling (RFC-023 section 8, RFC-047 section 12).

Nothing here loads a model. The Marian checkpoint is substituted with a
stub so the routing decision -- translate or don't -- and the text Marian
receives can be asserted deterministically and offline; the real checkpoint
is exercised only by the `slow`-marked tests, in
`test_clip_embedding_model_slow.py` and `test_query_lexicon_slow.py`.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest
import torch
from langdetect import DetectorFactory

from app.infrastructure.ai.query_translator import (
    FUNCTION_WORDS,
    LEXICON,
    ORIGINS,
    LexiconError,
    LexiconForm,
    MarianQueryTranslator,
    QueryLexicon,
    QueryTranslator,
    fold,
    marian_input,
)

MODEL_DIRECTORY = Path("runtime", "models", "opus-mt-romance-en")
"""Where the translator is handed its model from. Never read: both loads are
stubs, and what is checked is what they were asked for."""


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
        self.requested: list[tuple[Any, dict[str, Any]]] = []


@pytest.fixture()
def stub_marian(monkeypatch: pytest.MonkeyPatch) -> StubLoads:
    stubs = StubLoads()

    def load_tokenizer(source: Any, **kwargs: Any) -> StubTokenizer:
        stubs.tokenizer_loads += 1
        stubs.requested.append((source, kwargs))
        return stubs.tokenizer

    def load_model(source: Any, **kwargs: Any) -> StubTranslationModel:
        stubs.model_loads += 1
        stubs.requested.append((source, kwargs))
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
    return MarianQueryTranslator(lambda: MODEL_DIRECTORY, device=torch.device("cpu"))


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


def test_portuguese_input_is_sent_untagged(stub_marian: StubLoads) -> None:
    """opus-mt-ROMANCE-en is many-to-one and has no source-language tokens.

    A leading `>>por<<` tag resolves to `<unk>` on this checkpoint, which was
    verified to knock greedy decoding out of distribution -- the decoder
    looped on "." until generation was cut off instead of translating.
    """
    query = "piscina em uma casa rural"
    _translator().to_english(query)

    assert stub_marian.tokenizer.encoded == [query]


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


def test_the_model_and_the_tokenizer_come_from_the_directory_it_was_handed(
    stub_marian: StubLoads,
) -> None:
    """RFC-035 section 5.1: a directory in place of the name, and the Hub out.

    `local_files_only` is on both calls, so a file the directory lacks is
    absent rather than looked for on the Hub. Which checkpoint this is, is
    the lockfile's business (`test_model_runtime.py`).
    """
    _translator().to_english("propriedade rural com um pequeno lago")

    assert stub_marian.requested == [
        (MODEL_DIRECTORY, {"local_files_only": True}),
        (MODEL_DIRECTORY, {"local_files_only": True}),
    ]


def test_the_directory_is_asked_for_on_the_first_load_and_not_before(
    stub_marian: StubLoads,
) -> None:
    """The directory function is where the files are checked: never on
    construction, never for English, and once however many queries follow."""
    asked: list[int] = []

    def directory() -> Path:
        asked.append(1)
        return MODEL_DIRECTORY

    translator = MarianQueryTranslator(directory, device=torch.device("cpu"))
    translator.to_english("rural property with a small lake")
    assert asked == []

    translator.to_english("propriedade rural com um pequeno lago")
    translator.to_english("galpao industrial ao lado de uma fazenda")
    assert asked == [1]


def test_two_first_loads_at_once_load_the_checkpoint_once(
    stub_marian: StubLoads, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RFC-036 section 5.3: the first Portuguese search can arrive while the
    warm-up, in its thread, is loading this model; it waits for that load.
    The load is slowed so that both callers are inside it at once."""
    loading = threading.Event()

    def slow_model(source: Any, **kwargs: Any) -> StubTranslationModel:
        loading.set()
        time.sleep(0.3)
        stub_marian.model_loads += 1
        return stub_marian.model

    monkeypatch.setattr(
        "app.infrastructure.ai.query_translator.AutoModelForSeq2SeqLM.from_pretrained",
        slow_model,
    )
    translator = _translator()
    query = "propriedade rural com um pequeno lago"
    first = threading.Thread(target=translator.to_english, args=(query,))
    first.start()
    assert loading.wait(5)
    second = threading.Thread(target=translator.to_english, args=(query,))
    second.start()
    first.join(5)
    second.join(5)

    assert stub_marian.model_loads == 1
    assert stub_marian.tokenizer_loads == 1
    assert stub_marian.model.generate_calls == 2


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


def test_single_word_portuguese_outside_the_lexicon_is_a_known_detection_gap(
    stub_marian: StubLoads,
) -> None:
    """Pins the limitation RFC-023 section 8 documents rather than hiding it.

    `langdetect` needs a few words: `'janela'` is classified Finnish and
    `'varanda'` Swedish, so neither is recognized as Portuguese and both
    reach CLIP untranslated. RFC-047 closed the gap for the lexicon's forms
    -- `'fazenda'` (Turkish) and `'lago'` (Tagalog), the two words this test
    used to pin, are translated now, below -- and only for them.
    """
    translator = _translator()

    assert translator.to_english("janela") == "janela"
    assert translator.to_english("varanda") == "varanda"
    assert stub_marian.model.generate_calls == 0


@pytest.mark.parametrize("word", ["fazenda", "lago", "piscina", "morro"])
def test_single_word_portuguese_in_the_lexicon_is_translated(
    word: str, stub_marian: StubLoads
) -> None:
    """RFC-047 section 8: the deliberate, visible change RFC-023 asked for.

    `langdetect` calls these Turkish, Tagalog, Italian and Portuguese; they
    are translated because they are lexicon forms, whatever it says.
    """
    _translator().to_english(word)

    assert stub_marian.model.generate_calls == 1


# --------------------------------------------------------------- RFC-047


def _form(pt: str, troca: str | None = None, **fields: Any) -> LexiconForm:
    return LexiconForm(pt=pt, en=("x",), origem="geral", troca=troca, **fields)


SMALL = QueryLexicon(
    [
        _form("chácara", "pequena fazenda"),
        _form("estrada"),
        _form("estrada de chão", "estrada de terra batida"),
        _form("caixa d'água", "tanque de água"),
        _form("mato", "vegetação"),
        _form("Mato Grosso", tambem_ingles=True),
        _form("hotel", tambem_ingles=True),
    ]
)
"""A lexicon small enough to read, for the rules of section 5."""


def _small_translator() -> MarianQueryTranslator:
    return MarianQueryTranslator(
        lambda: MODEL_DIRECTORY, device=torch.device("cpu"), lexicon=SMALL
    )


def test_the_replacement_reaches_marian_in_place_of_the_form(
    stub_marian: StubLoads,
) -> None:
    """Section 5: after deciding to translate, before Marian."""
    _small_translator().to_english("chácara com piscina")

    assert stub_marian.tokenizer.encoded == ["pequena fazenda com piscina"]


def test_the_real_lexicon_replaces_before_marian(stub_marian: StubLoads) -> None:
    """The case section 2.4 measured: "casarão" was *will marry*."""
    _translator().to_english("casarão amarelo com jardim")

    assert stub_marian.tokenizer.encoded == ["casa grande amarelo com jardim"]


@pytest.mark.parametrize(
    "typed",
    ["chacara com piscina", "CHÁCARA com piscina", "Chacara com piscina"],
)
def test_matching_ignores_case_and_accents(typed: str) -> None:
    assert SMALL.rewrite(typed).casefold() == "pequena fazenda com piscina"


def test_a_form_typed_with_a_capital_keeps_it() -> None:
    """Section 15: Marian reads "porto Alegre" as *port Joy*."""
    assert SMALL.rewrite("Chacara com piscina") == "Pequena fazenda com piscina"
    assert LEXICON.rewrite("Galpao industrial") == "Galpão industrial"
    assert LEXICON.rewrite("Porto Alegre") == "Porto Alegre"


def test_an_unaccented_form_goes_to_marian_accented() -> None:
    assert LEXICON.rewrite("galpao industrial") == "galpão industrial"


def test_matching_is_by_whole_words() -> None:
    """ "mato" does not match inside "matogrossense"."""
    assert SMALL.rewrite("fazenda matogrossense") == "fazenda matogrossense"
    assert SMALL.rewrite("casa no mato") == "casa no vegetação"


def test_the_longest_form_wins() -> None:
    assert SMALL.rewrite("estrada de chão") == "estrada de terra batida"
    assert SMALL.rewrite("estrada asfaltada") == "estrada asfaltada"


def test_a_place_name_protects_the_form_inside_it() -> None:
    """Section 11, risk 2: "Mato Grosso" is taken whole, "mato" is not
    replaced inside it."""
    assert SMALL.rewrite("fazenda no Mato Grosso") == "fazenda no Mato Grosso"
    assert LEXICON.rewrite("fazenda no Mato Grosso") == "fazenda no Mato Grosso"


def test_a_multi_word_form_spans_spaces_hyphens_and_apostrophes_only() -> None:
    assert SMALL.rewrite("caixa d’água no telhado") == "tanque de água no telhado"
    assert SMALL.rewrite("estrada, de chão") == "estrada, de chão"


def test_everything_outside_the_forms_is_left_as_typed() -> None:
    typed = "Chácara (com piscina), e uma estrada de chão!"

    assert SMALL.rewrite(typed) == (
        "Pequena fazenda (com piscina), e uma estrada de terra batida!"
    )


def test_without_a_match_marian_receives_the_text_as_typed(
    stub_marian: StubLoads,
) -> None:
    query = "uma piscina com escorregador"
    _small_translator().to_english(query)

    assert stub_marian.tokenizer.encoded == [query]


def test_the_same_query_always_sends_the_same_text() -> None:
    """RFC-023 section 6.2: same query, same vector."""
    query = "chácara com estrada de chão perto do mato"

    assert marian_input(query, SMALL) == marian_input(query, SMALL)


@pytest.mark.parametrize(
    "query",
    [
        "wooden pier on a lake",
        "hotel with a swimming pool",
        "colorful crop fields",
        "farm",
    ],
)
def test_an_english_query_reaches_neither_the_lexicon_nor_marian(
    query: str, stub_marian: StubLoads
) -> None:
    """ "pier" is "píer" without its accent, and "hotel" is a form: both are
    English too, so neither is a marker, and the query goes as typed."""
    assert _translator().to_english(query) == query
    assert stub_marian.model_loads == 0


@pytest.mark.parametrize("word", sorted(FUNCTION_WORDS))
def test_every_function_word_makes_a_short_query_portuguese(
    word: str, stub_marian: StubLoads
) -> None:
    _small_translator().to_english(f"zzyzx {word} qwfp")

    assert stub_marian.model.generate_calls == 1


def test_a_lexicon_form_makes_a_short_query_portuguese(
    stub_marian: StubLoads,
) -> None:
    _small_translator().to_english("chacara")

    assert stub_marian.tokenizer.encoded == ["pequena fazenda"]


def test_a_form_that_is_english_too_is_not_a_marker(
    stub_marian: StubLoads,
) -> None:
    assert _small_translator().to_english("hotel") == "hotel"
    assert stub_marian.model.generate_calls == 0


def test_a_word_outside_the_lexicon_and_the_list_is_not_a_marker() -> None:
    assert not SMALL.has_marker("zzyzx qwfp")
    assert not LEXICON.has_marker("janela")


ENGLISH_WORDS = (
    "a",
    "as",
    "do",
    "no",
    "on",
    "in",
    "to",
    "or",
    "and",
    "the",
    "of",
    "at",
)
"""Short English words a function word must never be (section 12)."""


def test_no_function_word_is_an_english_word() -> None:
    assert FUNCTION_WORDS.isdisjoint(ENGLISH_WORDS)


def test_function_words_are_matched_with_their_accents() -> None:
    """ "à" is in the list and "a" is not: accents count here."""
    assert LEXICON.has_marker("à beira")
    assert not QueryLexicon([]).has_marker("a pier")


@pytest.mark.parametrize(
    ("typed", "sent"),
    [
        # The other sense of "sítio" is taken whole, before the farm's troca.
        ("sítio arqueológico perto do rio", "sítio arqueológico perto do rio"),
        ("sítio histórico", "sítio histórico"),
        ("sítio com casa e pomar", "pequena fazenda com casa e pomar de frutas"),
        # A complement the user types after a form is not doubled by the troca.
        ("estádio de futebol lotado", "estádio de futebol lotado"),
        ("estádio vazio", "estádio esportivo vazio"),
        (
            "estrada de chão batido entre as lavouras",
            "estrada de cascalho entre as lavouras",
        ),
        ("estrada de terra entre as lavouras", "estrada de terra entre as lavouras"),
        ("marina de iates perto do porto", "porto de iates perto do porto"),
        ("balsa de madeira no rio", "balsa de madeira no rio"),
        ("lava vulcânica", "lava vulcânica"),
        # The troca that adds nothing: Marian copies the English word.
        ("farol na ponta do molhe", "lighthouse na ponta do píer de rochas"),
        ("caiaque no lago", "kayak no lago"),
    ],
)
def test_what_the_second_round_sends_for_what_the_first_got_wrong(
    typed: str, sent: str
) -> None:
    """RFC-047 section 17: each of these came back wrong from Marian in the
    first round (*dirt road hit*, *small archaeological farm*, *beach
    lighthouse*), or was doubled when typed."""
    assert LEXICON.rewrite(typed) == sent


class TestTheLexiconFile:
    """Section 12: the shipped file is valid, and the loader refuses one
    that is not."""

    def test_the_shipped_lexicon_loads(self) -> None:
        assert QueryLexicon.load().forms == LEXICON.forms

    def test_its_forms_are_unique_without_case_and_accents(self) -> None:
        folded = [fold(form.pt) for form in LEXICON.forms]

        assert len(folded) == len(set(folded))

    def test_every_form_has_english_and_a_known_origin(self) -> None:
        assert all(form.en for form in LEXICON.forms)
        assert {form.origem for form in LEXICON.forms} <= ORIGINS

    def test_every_place_name_is_english_too_and_has_no_troca(self) -> None:
        names = [form for form in LEXICON.forms if form.is_name]

        assert names
        assert all(form.tambem_ingles and form.troca is None for form in names)

    def test_a_troca_typed_by_the_user_is_not_rewritten_again(self) -> None:
        """RFC-047 section 17: "marina de iates" typed went as "marina de iates
        de iates", because "marina" is a form with a troca and the troca
        begins with it. A troca that is itself typed must leave the lexicon
        unchanged, whether by being no form's word or by being a form (without
        a troca) that protects it."""
        again = {
            form.pt: LEXICON.rewrite(form.troca)
            for form in LEXICON.forms
            if form.troca is not None and LEXICON.rewrite(form.troca) != form.troca
        }

        assert again == {}

    @staticmethod
    def _load(tmp_path: Path, body: str) -> QueryLexicon:
        path = tmp_path / "lexicon.toml"
        path.write_text(body, encoding="utf-8")
        return QueryLexicon.load(path)

    @pytest.mark.parametrize(
        "body",
        [
            '[[forma]]\npt = "galpão"\nen = ["shed"]\norigem = "geral"\n'
            '[[forma]]\npt = "Galpao"\nen = ["shed"]\norigem = "geral"\n',
            '[[forma]]\npt = "galpão"\nen = []\norigem = "geral"\n',
            '[[forma]]\npt = "galpão"\nen = ["shed"]\norigem = "norte"\n',
            '[[forma]]\npt = "galpão"\nen = ["shed"]\norigem = "geral"\n'
            "tambem_ingels = true\n",
            '[[forma]]\npt = "galpão"\nen = ["shed"]\norigem = "geral"\n'
            'troca = "Galpao"\n',
            '[[forma]]\npt = "Mato Grosso"\nen = ["mato grosso"]\norigem = "geral"\n'
            'troca = "Mato"\ntambem_ingles = true\n',
            '[[forma]]\npt = "Mato Grosso"\nen = ["mato grosso"]\norigem = "geral"\n',
            '[[palavra]]\npt = "galpão"\nen = ["shed"]\norigem = "geral"\n',
        ],
        ids=[
            "duplicate",
            "no english",
            "unknown origin",
            "unknown field",
            "troca is the form",
            "name with troca",
            "name not english too",
            "unknown table",
        ],
    )
    def test_a_malformed_lexicon_is_refused(self, tmp_path: Path, body: str) -> None:
        with pytest.raises(LexiconError):
            self._load(tmp_path, body)
