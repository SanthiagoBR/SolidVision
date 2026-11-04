"""Portuguese-to-English query normalization for the CLIP text encoder.

CLIP's text tower is English-only. Feeding it a Portuguese query directly
measurably degrades retrieval: in the RFC-023 bake-off the selected
checkpoint scored 44.0% strict accuracy on raw Portuguese against 52.0%
once the same queries were machine-translated to English first
(`clip_pt_translation_run_output.log`). Translation therefore happens
here, in Infrastructure, so that neither `EmbeddingModelPort` nor any
Application code has to learn that the current model speaks one language.

Since RFC-047 two things happen before the translator, both driven by the
lexicon next to this file, `query_lexicon.toml`: a query is translated when
`langdetect` calls it Portuguese *or* when it has a Portuguese marker
(section 6), and the lexicon's forms the translator gets wrong are replaced
by words it gets right before it sees them (section 5). Neither touches a
query that is not translated, and neither changes any stored vector: the
query's vector is computed on every search.
"""

from __future__ import annotations

import re
import threading
import tomllib
import unicodedata
from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

import torch
from langdetect import DetectorFactory, LangDetectException, detect
from transformers import AutoModelForSeq2SeqLM, AutoTokenizer, PreTrainedModel
from transformers.tokenization_utils_base import PreTrainedTokenizerBase

from app.infrastructure.ai.device import resolve_device
from app.infrastructure.ai.model_runtime import ModelDirectory, load_failed
from app.infrastructure.logging.logger import get_logger

logger = get_logger(__name__)

# `langdetect`'s algorithm is probabilistic and seeded per process. Pinning
# the seed is what makes `detect()` reproducible across runs, processes, and
# machines -- the same determinism requirement that rules out the salted
# built-in `hash()` in `FakeEmbeddingModel` (ARCHITECTURE.md section 20).
# Set at import time because `detect()` reads it lazily on first use.
DetectorFactory.seed = 0

# `TRANSLATION_MODEL` used to sit here, the name the checkpoint was loaded by.
# Since RFC-035 the translator is the lockfile's `translator` entry, loaded
# from its directory; the name is in `models.lock.json`.
PORTUGUESE = "pt"
MAX_TRANSLATION_TOKENS = 128

LEXICON_NAME = "query_lexicon.toml"
"""Beside this module. RFC-047 section 4: versioned with the code, not in
`Settings`. It changes
the vector of some queries, which is an RFC's decision, as the CLIP template
is; unlike the template it changes no stored vector."""

ORIGINS = frozenset({"geral", "sul"})
"""Where a form comes from: the general vocabulary, or the south of Brazil."""

FUNCTION_WORDS = frozenset(
    {
        "de", "da", "das", "dos", "com", "um", "uma", "uns", "umas",
        "em", "num", "numa", "na", "nas", "ao", "aos", "à", "às",
        "pelo", "pela", "pelos", "pelas", "por", "para", "entre", "perto",
        "sobre", "sem", "e", "ou", "nao", "não",
    }
)  # fmt: skip
"""RFC-047 section 6: the closed list of Portuguese function words.

None of them is also an English word -- "a", "as", "do", "no" and "on" are
left out for that reason -- so one of them in a query is enough to call it
Portuguese. Matched as typed, ignoring case but not accents: "à" is in the
list and "a" is not."""

_WORD = re.compile("[\\w\u0300-\u036f]+")
"""A word: letters and digits, with any combining accent kept inside it."""

_JOINER = re.compile("[\\s\\-'\u2019]+")
"""What may stand between the words of a multi-word form ("estrada de chão",
"cata-vento", "caixa d'água"): spaces, hyphens, apostrophes, and no other
punctuation."""


def fold(text: str) -> str:
    """Casefolded and without accents: how the lexicon's forms are matched.

    "Chacara", "chácara" and "CHÁCARA" all fold to "chacara".
    """
    decomposed = unicodedata.normalize("NFD", text.casefold())
    bare = "".join(c for c in decomposed if not unicodedata.combining(c))
    return unicodedata.normalize("NFC", bare)


@dataclass(frozen=True)
class LexiconForm:
    """One line of the lexicon: a Portuguese form, as typed, and its English.

    `en` lists what counts as a right translation; the slow tests check that
    what goes to Marian produces one of them. `troca` exists only when Marian
    gets the form wrong, and is what goes to Marian in its place. A form with
    `tambem_ingles` is an English word too ("hotel"), and is never a
    Portuguese marker.
    """

    pt: str
    en: tuple[str, ...]
    origem: str
    troca: str | None = None
    tambem_ingles: bool = False

    @property
    def sent(self) -> str:
        """What reaches Marian in place of the form (section 5, rule 3)."""
        return self.troca if self.troca is not None else self.pt

    @property
    def words(self) -> tuple[str, ...]:
        return tuple(_WORD.findall(fold(self.pt)))

    @property
    def is_name(self) -> bool:
        """A form written with a capital is a place name ("Mato Grosso").

        It is in the lexicon only so that the longest match takes it whole,
        before a form inside it is replaced (RFC-047 section 11, risk 2).
        """
        return self.pt[0].isupper()


class LexiconError(ValueError):
    """The lexicon file is malformed. The message names the line."""


_FIELDS = {"pt", "en", "origem", "troca", "tambem_ingles"}


def _parse_form(raw: Mapping[str, Any], position: int) -> LexiconForm:
    where = f"form {position} ({raw.get('pt')!r})"
    unknown = set(raw) - _FIELDS
    if unknown:
        raise LexiconError(f"{where}: unknown fields {sorted(unknown)}")
    pt, en, origem = raw.get("pt"), raw.get("en"), raw.get("origem")
    troca, tambem_ingles = raw.get("troca"), raw.get("tambem_ingles", False)
    if not isinstance(pt, str) or not _WORD.search(pt):
        raise LexiconError(f"{where}: `pt` must be a word")
    if (
        not isinstance(en, list)
        or not en
        or not all(isinstance(e, str) and e.strip() for e in en)
    ):
        raise LexiconError(f"{where}: `en` must be a non-empty list of strings")
    if origem not in ORIGINS:
        raise LexiconError(f"{where}: `origem` must be one of {sorted(ORIGINS)}")
    if troca is not None and (not isinstance(troca, str) or not _WORD.search(troca)):
        raise LexiconError(f"{where}: `troca` must be a word")
    if troca is not None and fold(troca) == fold(pt):
        raise LexiconError(f"{where}: `troca` is the form itself")
    if not isinstance(tambem_ingles, bool):
        raise LexiconError(f"{where}: `tambem_ingles` must be true or false")
    form = LexiconForm(
        pt=pt,
        en=tuple(en),
        origem=origem,
        troca=troca,
        tambem_ingles=tambem_ingles,
    )
    if form.is_name and (troca is not None or not tambem_ingles):
        raise LexiconError(
            f"{where}: a place name has no `troca` and is `tambem_ingles`"
        )
    return form


@dataclass(frozen=True)
class _Match:
    start: int
    end: int
    form: LexiconForm


class QueryLexicon:
    """The forms of `query_lexicon.toml`, matched in a query (RFC-047 section 5).

    A match is whole words, ignoring case and accents; the longest form
    starting at a word wins, so "estrada de chão" is taken whole before
    "chão" alone could be. Matching runs left to right and never overlaps.
    """

    def __init__(self, forms: Sequence[LexiconForm]) -> None:
        self.forms: tuple[LexiconForm, ...] = tuple(forms)
        seen: dict[tuple[str, ...], str] = {}
        by_first: dict[str, list[LexiconForm]] = {}
        for form in self.forms:
            key = form.words
            if key in seen:
                raise LexiconError(
                    f"{form.pt!r} and {seen[key]!r} are the same form"
                    " once case and accents are ignored"
                )
            seen[key] = form.pt
            by_first.setdefault(key[0], []).append(form)
        self._by_first = {
            first: sorted(group, key=lambda f: len(f.words), reverse=True)
            for first, group in by_first.items()
        }

    @classmethod
    def load(cls, path: Path | None = None) -> QueryLexicon:
        """The lexicon in `path`, or, by default, the one of this version.

        The default is read as data of this package, through
        `importlib.resources`, as `model_runtime` reads the lockfile: the
        file travels with the code, and only `layout` may know where the
        code is (RFC-034).
        """
        if path is None:
            source = resources.files("app.infrastructure.ai") / LEXICON_NAME
            name, content = LEXICON_NAME, source.read_bytes()
        else:
            name, content = path.name, path.read_bytes()
        document = tomllib.loads(content.decode("utf-8"))
        unknown = set(document) - {"forma"}
        if unknown:
            raise LexiconError(f"{name}: unknown tables {sorted(unknown)}")
        raw_forms = document.get("forma", [])
        return cls([_parse_form(raw, i + 1) for i, raw in enumerate(raw_forms)])

    def matches(self, text: str) -> Iterator[_Match]:
        words = list(_WORD.finditer(text))
        folded = [fold(word.group()) for word in words]
        index = 0
        while index < len(words):
            found = self._match_at(text, words, folded, index)
            if found is None:
                index += 1
                continue
            form, count = found
            yield _Match(words[index].start(), words[index + count - 1].end(), form)
            index += count

    def _match_at(
        self,
        text: str,
        words: Sequence[re.Match[str]],
        folded: Sequence[str],
        index: int,
    ) -> tuple[LexiconForm, int] | None:
        for form in self._by_first.get(folded[index], ()):
            count = len(form.words)
            if index + count > len(words):
                continue
            if tuple(folded[index : index + count]) != form.words:
                continue
            joined = all(
                _JOINER.fullmatch(text[words[k - 1].end() : words[k].start()])
                for k in range(index + 1, index + count)
            )
            if joined:
                return form, count
        return None

    def rewrite(self, text: str) -> str:
        """`text` as it goes to Marian: each form replaced by what it sends.

        Everything outside the matched forms -- punctuation, words the
        lexicon does not know -- is left exactly as typed. A form typed with
        a capital keeps it: Marian reads "Porto Alegre" as a name and "porto
        Alegre" as *port Joy* (RFC-047 section 15).
        """
        pieces: list[str] = []
        position = 0
        for match in self.matches(text):
            pieces.append(text[position : match.start])
            sent = match.form.sent
            if text[match.start].isupper():
                sent = sent[0].upper() + sent[1:]
            pieces.append(sent)
            position = match.end
        pieces.append(text[position:])
        return "".join(pieces)

    def has_marker(self, text: str) -> bool:
        """Whether `text` has a Portuguese marker (RFC-047 section 6, rule 2):
        a function word of the closed list, or a form that is not English too.
        """
        if any(word.casefold() in FUNCTION_WORDS for word in _WORD.findall(text)):
            return True
        return any(not match.form.tambem_ingles for match in self.matches(text))


LEXICON = QueryLexicon.load()
"""Loaded once, on import (RFC-047 section 13)."""


def _detected_as_portuguese(text: str) -> bool:
    try:
        return bool(detect(text) == PORTUGUESE)
    except LangDetectException:
        # Raised for input with no detectable features -- empty strings,
        # pure punctuation, bare digits. There is nothing to translate,
        # so hand it to CLIP untouched rather than failing the query.
        return False


def is_portuguese(text: str, lexicon: QueryLexicon = LEXICON) -> bool:
    """RFC-047 section 6: a marker, or `langdetect` saying `pt`.

    The markers are checked first because they are cheap, and because they
    decide the cases `langdetect` gets wrong: single words it calls Turkish
    or Tagalog, sentences it calls Spanish. It still decides the rest: whole
    Portuguese sentences without a marker.
    """
    return lexicon.has_marker(text) or _detected_as_portuguese(text)


def marian_input(text: str, lexicon: QueryLexicon = LEXICON) -> str | None:
    """What Marian receives for this query, or None when it is not translated."""
    if not is_portuguese(text, lexicon):
        return None
    return lexicon.rewrite(text)


class QueryTranslator(ABC):
    """Normalize a user query into the English the CLIP text encoder expects."""

    @abstractmethod
    def to_english(self, text: str) -> str:
        """Return `text` in English, translating only when necessary."""


class MarianQueryTranslator(QueryTranslator):
    """Detect Portuguese and translate it with `Helsinki-NLP/opus-mt-ROMANCE-en`.

    Loaded through `AutoTokenizer` / `AutoModelForSeq2SeqLM` rather than
    `pipeline("translation")`: this checkpoint is not reliably registered
    under that generic task in transformers 5.15, and the explicit pair also
    keeps the decoding parameters visible instead of buried in pipeline
    defaults.

    No leading `>>xx<<` language token is sent: this checkpoint is
    many-to-one (every Romance language into English), and many-to-one
    Marian models carry no such tokens in their vocabulary. Prepending one
    anyway -- e.g. `>>por<<` -- resolves to `<unk>`, which knocks greedy
    decoding out of distribution; verified on this checkpoint, that made the
    decoder loop on "." until `MAX_TRANSLATION_TOKENS` cut it off instead of
    producing a translation.

    Decoding is greedy (`num_beams=1`, `do_sample=False`) so the same query
    always yields the same English string, and therefore the same embedding.

    The weights are those of the pinned `main`, whose only copy on the Hub
    is `pytorch_model.bin`, a pickle. `vendor_models.py` converts it to a
    `model.safetensors` -- the same tensors, checked one by one, and 2.2 s
    faster to load cold -- so this never unpickles anything (RFC-035 section
    7.4). Not the `safetensors` of the Hub's `refs/pr/6`: that is a bot's
    pull request, which can change or vanish.

    Which queries are translated, and what Marian receives, is RFC-047's:
    `marian_input()`. `langdetect` needs a few words to work -- `'fazenda'`
    is detected as Turkish and `'lago'` as Tagalog -- so a query is also
    translated when it has a Portuguese marker, and a lexicon form Marian
    gets wrong ("morro" -> *Death*) reaches it as a word it gets right. A
    single word outside the lexicon is still RFC-023's known gap: anything
    neither marked nor detected as Portuguese is treated as English.
    """

    def __init__(
        self,
        model_directory: ModelDirectory,
        device: torch.device | None = None,
        lexicon: QueryLexicon | None = None,
    ) -> None:
        self._model_directory = model_directory
        self._device = device if device is not None else resolve_device()
        self._lexicon = lexicon if lexicon is not None else LEXICON
        """The product's, `query_lexicon.toml`; another only in tests."""
        self._tokenizer: PreTrainedTokenizerBase | None = None
        self._model: PreTrainedModel | None = None
        self._load_lock = threading.Lock()
        """One load at a time, as `ClipEmbeddingModel` does (RFC-036 5.3)."""

    def to_english(self, text: str) -> str:
        """Translate `text` when it is Portuguese, otherwise return it unchanged.

        A query that is not translated never reaches the lexicon either: it
        goes to CLIP exactly as typed, as before RFC-047.
        """
        source = marian_input(text, self._lexicon)
        if source is None:
            return text

        translated = self._translate(source)
        logger.debug("Translated query %r (as %r) -> %r", text, source, translated)
        return translated

    @staticmethod
    def _is_portuguese(text: str) -> bool:
        return is_portuguese(text)

    def _translate(self, text: str) -> str:
        tokenizer, model = self._ensure_loaded()

        batch = tokenizer(
            [text],
            return_tensors="pt",
            padding=True,
        )
        inputs = {key: value.to(self._device) for key, value in batch.items()}

        with torch.no_grad():
            # `PreTrainedModel` does not declare `generate` in transformers
            # 5.15, so mypy resolves it through `nn.Module.__getattr__` and
            # reports the resulting `Tensor` as not callable. An upstream
            # typing gap rather than a real one; `warn_unused_ignores` will
            # flag this line if a later release annotates the method.
            generated = model.generate(  # type: ignore[operator]
                **inputs,
                max_new_tokens=MAX_TRANSLATION_TOKENS,
                num_beams=1,
                do_sample=False,
            )

        decoded: str = tokenizer.batch_decode(generated, skip_special_tokens=True)[0]
        return decoded.strip()

    def _ensure_loaded(self) -> tuple[PreTrainedTokenizerBase, PreTrainedModel]:
        """Load the translation checkpoint once, on first Portuguese query.

        Lazy rather than eager so that an English-only deployment -- and the
        entire fast test suite -- never pays for a second model load or the
        memory it occupies (RFC-023 section 10). The directory is checked
        first, and `local_files_only` is written on both calls, for the
        reasons `ClipEmbeddingModel._ensure_loaded()` gives -- and the load
        happens once, under a lock, for the reason it gives too: the first
        Portuguese search can arrive while the warm-up is loading this.
        """
        if self._tokenizer is None or self._model is None:
            with self._load_lock:
                if self._tokenizer is None or self._model is None:
                    self._tokenizer, self._model = self._load()
        return self._tokenizer, self._model

    def _load(self) -> tuple[PreTrainedTokenizerBase, PreTrainedModel]:
        directory = self._model_directory()
        logger.info(
            "Loading translation model from %s onto %s", directory, self._device
        )
        try:
            model = AutoModelForSeq2SeqLM.from_pretrained(
                directory, local_files_only=True
            )
            model.eval()
            loaded = model.to(self._device)
            tokenizer = AutoTokenizer.from_pretrained(directory, local_files_only=True)
        except Exception as error:
            raise load_failed(directory) from error
        return tokenizer, loaded
