"""The query lexicon against the real translator (RFC-047 sections 12 and 17).

Every line of `query_lexicon.toml` is a claim about what Marian does, and
this checks each one with the checkpoint the app ships, through the product's
own path (`LEXICON.rewrite()`, then Marian):

- a form typed alone, in "{forma} vista de cima" and in five sentences
  (`lexicon_frames.CONTEXTS`) comes back as one of its `en` and with no word
  that the frame, the `en` and a few function words do not explain (section
  17: a `troca` that adds words, "batida" -> *hit*, passed the first round),
  except the contexts of `lexicon_frames.KNOWN_MISSES`, which must be the
  wrong ones (section 18);
- a form with another sense keeps that sense ("sítio arqueológico");
- a `troca` is needed: the form left alone is wrong in some context;
- a place name reaches English whole inside a sentence, and would not
  without its line;
- the 30 English words of the pilot keep their prompt.

`experiments/rfc-047-traducao-das-consultas/probe_frames.py` chooses the
trocas by the same rules, from the same module; a new line passes through it
first. Marked `slow`, and skipped without `runtime\\models`. Several minutes
on CPU.
"""

from __future__ import annotations

import pytest
from tests.conftest import materialized_embedding_model
from tests.infrastructure.ai.lexicon_frames import (
    CONTEXTS,
    KNOWN_MISSES,
    OTHER_SENSES,
    judge,
    matched,
)

from app.infrastructure.ai.clip_embedding_model import ClipEmbeddingModel
from app.infrastructure.ai.model_runtime import (
    TRANSLATOR,
    ModelUnavailableError,
    default_runtime,
)
from app.infrastructure.ai.query_translator import (
    LEXICON,
    LexiconForm,
    MarianQueryTranslator,
    QueryLexicon,
)

pytestmark = pytest.mark.slow

FORMS = [form for form in LEXICON.forms if not form.is_name]
TROCAS = [form for form in FORMS if form.troca is not None]
NAMES = [form for form in LEXICON.forms if form.is_name]
NAME_SENTENCE = "vista aérea de {}"
UNCHANGED = QueryLexicon([])
"""The lexicon with no form: what Marian would receive before RFC-047."""

PILOT_ENGLISH_WORDS = (
    "farm", "lake", "pool", "river", "beach", "city", "church", "bridge",
    "road", "forest", "hill", "field", "house", "building", "stadium",
    "harbor", "island", "waterfall", "plantation", "reservoir", "shed",
    "parking lot", "street", "avenue", "sea", "mountain", "desert", "snow",
    "school", "power plant",
)  # fmt: skip
"""The English half of the pilot's 30 pairs (RFC-047 section 2.2)."""


@pytest.fixture(scope="module")
def marian() -> MarianQueryTranslator:
    try:
        default_runtime().check_presence()
    except ModelUnavailableError as error:
        pytest.skip(str(error))
    return MarianQueryTranslator(default_runtime().loader(TRANSLATOR))


@pytest.fixture(scope="module")
def clip() -> ClipEmbeddingModel:
    return materialized_embedding_model()


def wrong_in(
    form: LexiconForm,
    lexicon: QueryLexicon,
    marian: MarianQueryTranslator,
) -> dict[str, str]:
    """The contexts where `form`, typed as is, does not come back right."""
    wrong = {}
    for name, template in CONTEXTS.items():
        sent = lexicon.rewrite(template.format(X=form.pt))
        english = marian._translate(sent)
        ok, found, left = judge(english, name, form.en)
        if not ok:
            why = f"extra {left}" if found else "no en"
            wrong[name] = f"{sent!r} -> {english!r} ({why})"
    return wrong


@pytest.mark.parametrize("form", FORMS, ids=lambda form: form.pt)
def test_what_goes_to_marian_comes_back_right(
    form: LexiconForm, marian: MarianQueryTranslator
) -> None:
    """Right everywhere, except the contexts `KNOWN_MISSES` names."""
    wrong = wrong_in(form, LEXICON, marian)

    assert sorted(wrong) == sorted(KNOWN_MISSES.get(form.pt, {})), wrong


@pytest.mark.parametrize(
    ("form", "phrase", "word"),
    [
        (form, phrase, word)
        for form in FORMS
        for phrase, word in OTHER_SENSES.get(form.pt, ())
    ],
    ids=lambda value: value.pt if isinstance(value, LexiconForm) else str(value),
)
def test_a_form_with_another_sense_keeps_it(
    form: LexiconForm, phrase: str, word: str, marian: MarianQueryTranslator
) -> None:
    english = marian._translate(LEXICON.rewrite(phrase))

    assert word in english.casefold(), f"{phrase!r} -> {english!r}"


@pytest.mark.parametrize("form", TROCAS, ids=lambda form: form.pt)
def test_a_troca_is_needed(form: LexiconForm, marian: MarianQueryTranslator) -> None:
    """A troca where Marian gets the form right is noise (section 4.1): left
    as typed, it is wrong somewhere in the contexts."""
    assert wrong_in(
        form, UNCHANGED, marian
    ), f"{form.pt!r} comes back right in every context without the troca"


@pytest.mark.parametrize("form", NAMES, ids=lambda form: form.pt)
def test_a_place_name_is_kept_and_needs_its_line(
    form: LexiconForm, marian: MarianQueryTranslator
) -> None:
    sentence = NAME_SENTENCE.format(form.pt)
    without = QueryLexicon([other for other in LEXICON.forms if other is not form])

    kept = marian._translate(LEXICON.rewrite(sentence))
    broken = marian._translate(without.rewrite(sentence))

    assert matched(kept, form.en), f"{sentence!r} -> {kept!r}"
    assert not matched(broken, form.en), f"without the line it still is {broken!r}"


@pytest.mark.parametrize("word", PILOT_ENGLISH_WORDS)
def test_the_pilots_english_words_keep_their_prompt(
    word: str, clip: ClipEmbeddingModel
) -> None:
    """None of them is a form or a marker (section 9, criterion 4)."""
    assert clip.build_prompt(word) == f"a photo of {word}"
